"""
Playwright-based scraper for the Facebook Ads Library public website.
No API key or Meta identity verification required.

URL format:
  https://www.facebook.com/ads/library/?active_status=active&ad_type=all
  &country=US&q=buy+now&search_type=keyword_unordered&media_type=all
"""

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)

ADS_LIBRARY_BASE = "https://www.facebook.com/ads/library/"

# Delays (seconds) — be polite to avoid blocks
SCROLL_DELAY = 2.5
SEARCH_DELAY = 3.0
PAGE_LOAD_WAIT = 4000  # ms after navigation

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def parse_follower_count(text: str) -> int:
    """
    Parse follower/like counts from strings like:
      '1,234 likes', '1.2K followers', '2.5M likes'
    Returns 0 if not parseable.
    """
    t = text.lower().replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([km])?\s*(?:likes?|followers?)", t)
    if not m:
        return 0
    try:
        num = float(m.group(1))
        suffix = m.group(2)
        if suffix == "k":
            num *= 1_000
        elif suffix == "m":
            num *= 1_000_000
        return int(num)
    except ValueError:
        return 0


def parse_date_text(text: str) -> Optional[str]:
    """
    Extract YYYY-MM-DD from Ads Library date strings such as:
      'Started running on March 1, 2024'
      'Mar 1 – Mar 7, 2024'
    Returns None if unparseable.
    """
    t = text.lower()
    # "Month Day, Year" or "Month Day Year"
    m = re.search(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        mon = MONTH_MAP.get(m.group(1)[:3])
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
    # MM/DD/YYYY or DD/MM/YYYY
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    return None


# ---------------------------------------------------------------------------
# JavaScript injected into the page to extract ad card data from the DOM.
# This runs inside the browser so it can traverse the live React-rendered DOM.
# ---------------------------------------------------------------------------
_EXTRACT_JS = """
() => {
    const results = [];
    const seen = new Set();

    // Strategy: walk every element looking for the repeating ad-card structure.
    // Each card contains:
    //   • A link to a Facebook page (page name + URL)
    //   • A span with "X likes" or "X followers"
    //   • Ad body text
    //   • A "Started running on …" date string
    //   • Optionally a <video> element
    //   • Optionally a CTA button ("Shop Now", "Buy Now", etc.)

    const CTA_LABELS = new Set([
        'shop now', 'buy now', 'order now', 'get yours', 'get it now',
        'learn more', 'get offer', 'sign up', 'subscribe', 'get quote',
        'contact us', 'get started', 'apply now', 'download',
    ]);

    // Find candidate page links (links that point to a FB page, not UI chrome)
    const pageLinks = Array.from(document.querySelectorAll('a[href]')).filter(a => {
        const href = a.href || '';
        const text = (a.textContent || '').trim();
        return (
            text.length >= 2 && text.length <= 120 &&
            (href.includes('facebook.com/') || href.includes('fb.com/')) &&
            !href.includes('/ads/library') &&
            !href.includes('facebook.com/help') &&
            !href.includes('facebook.com/login') &&
            !href.includes('facebook.com/policies') &&
            !href.includes('facebook.com/privacy') &&
            !href.includes('/l.php')     // FB outbound redirect links
        );
    });

    pageLinks.forEach(link => {
        // Walk up to find the ad-card container.
        // Heuristic: the container has "Started running" or a year and is
        // large enough to be a full ad card (> 200 chars of text).
        let container = link.parentElement;
        let found = false;
        for (let i = 0; i < 12; i++) {
            if (!container) break;
            const t = container.innerText || '';
            if (
                t.length > 200 &&
                (t.includes('Started running') || t.includes('running on') ||
                 /\\b202[3-9]\\b/.test(t))
            ) {
                found = true;
                break;
            }
            container = container.parentElement;
        }
        if (!found || !container) return;

        // De-duplicate by container reference
        if (seen.has(container)) return;
        seen.add(container);

        const fullText = container.innerText || '';
        const fullHtml = container.innerHTML || '';

        // --- Page name & URL ---
        const pageName = link.textContent.trim();
        const pageUrl = link.href;

        // --- Follower / like count ---
        let followerText = '';
        container.querySelectorAll('span, div').forEach(el => {
            const t = (el.textContent || '').trim();
            if (/[\\d,\\.]+\\s*[KkMm]?\\s*(likes?|followers?)/i.test(t) && t.length < 40) {
                if (t.length > followerText.length) followerText = t;
            }
        });

        // --- Media type ---
        const hasVideo = (
            container.querySelector('video') !== null ||
            fullHtml.includes('<video') ||
            fullText.toLowerCase().includes('video')
        );

        // --- CTA button ---
        let ctaButton = '';
        container.querySelectorAll('a, div[role="button"], button').forEach(el => {
            const t = (el.textContent || '').trim().toLowerCase();
            if (CTA_LABELS.has(t)) ctaButton = el.textContent.trim();
        });

        // Fallback: check full text for CTA phrases
        const lowerText = fullText.toLowerCase();
        const hasShopNow = (
            ctaButton.toLowerCase().includes('shop') ||
            ctaButton.toLowerCase().includes('buy') ||
            ctaButton.toLowerCase().includes('order') ||
            lowerText.includes('shop now') ||
            lowerText.includes('buy now') ||
            lowerText.includes('order now') ||
            lowerText.includes('get yours')
        );

        // --- Ad start date ---
        let dateText = '';
        const dateMatch = fullText.match(
            /(?:Started running|Active since|running on)[^\\n]{0,60}/i
        );
        if (dateMatch) dateText = dateMatch[0].trim();

        // Fallback: bare date like "March 1, 2024"
        if (!dateText) {
            const bareDate = fullText.match(
                /(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\.?\\s+\\d{1,2},?\\s+\\d{4}/i
            );
            if (bareDate) dateText = bareDate[0].trim();
        }

        // --- Ad snapshot URL (sometimes present as a link) ---
        let snapshotUrl = '';
        container.querySelectorAll('a[href*="facebook.com/ads/archive"]').forEach(el => {
            snapshotUrl = el.href;
        });

        // --- Ad body (longest non-trivial text block in the card) ---
        let adBody = '';
        container.querySelectorAll('div, p, span').forEach(el => {
            // Only look at leaf-ish nodes (not giant containers)
            if (el.children.length > 5) return;
            const t = (el.textContent || '').trim();
            if (
                t.length > adBody.length &&
                t.length < 2000 &&
                t !== pageName &&
                !t.includes('Started running') &&
                !t.match(/^\\d/)  // not just a number
            ) {
                adBody = t;
            }
        });

        const key = pageUrl + '|' + adBody.slice(0, 60);
        results.push({
            _key: key,
            page_name: pageName,
            page_url: pageUrl,
            follower_text: followerText,
            has_video: hasVideo,
            has_shop_now: hasShopNow,
            cta_button: ctaButton,
            date_text: dateText,
            snapshot_url: snapshotUrl,
            ad_body: adBody.slice(0, 400),
        });
    });

    return results;
}
"""


class AdsLibraryBrowser:
    """Playwright-driven scraper for the public Facebook Ads Library."""

    def __init__(
        self,
        countries: list[str],
        headless: bool = False,
        slow_mo: int = 50,
    ):
        self.countries = countries
        self.headless = headless
        self.slow_mo = slow_mo
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        logger.info(f"Browser launched (headless={self.headless})")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def search_keyword(
        self,
        keyword: str,
        max_ads: int = 120,
    ) -> list[dict]:
        """
        Open the Ads Library, search `keyword`, scroll to collect ads,
        and return a list of raw ad dicts.
        """
        country = self.countries[0] if self.countries else "US"
        url = ADS_LIBRARY_BASE + "?" + urlencode({
            "active_status": "active",
            "ad_type": "all",
            "country": country,
            "q": keyword,
            "search_type": "keyword_unordered",
            "media_type": "all",
        })

        context = await self._browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        all_ads: list[dict] = []
        seen_keys: set[str] = set()

        try:
            logger.debug(f"  → {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(PAGE_LOAD_WAIT)

            await self._dismiss_dialogs(page)
            await page.wait_for_timeout(1000)

            no_new_rounds = 0
            scroll_round = 0
            max_scrolls = max(10, max_ads // 15)

            while scroll_round < max_scrolls:
                raw = await page.evaluate(_EXTRACT_JS)
                added = 0
                for ad in (raw or []):
                    k = ad.get("_key", "")
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        all_ads.append(ad)
                        added += 1

                if added == 0:
                    no_new_rounds += 1
                    if no_new_rounds >= 3:
                        logger.debug(f"  No new ads after 3 scrolls — done ({len(all_ads)} total)")
                        break
                else:
                    no_new_rounds = 0

                if len(all_ads) >= max_ads:
                    break

                # Scroll down and wait for more content
                await page.evaluate("window.scrollBy(0, window.innerHeight * 2.5)")
                await page.wait_for_timeout(int(SCROLL_DELAY * 1000))
                scroll_round += 1

        except PWTimeout:
            logger.warning(f"  Timeout while scraping '{keyword}'")
        except Exception as e:
            logger.warning(f"  Error scraping '{keyword}': {e}")
        finally:
            await context.close()

        logger.info(f"  Scraped {len(all_ads)} ads for '{keyword}'")
        return all_ads

    async def _dismiss_dialogs(self, page: Page):
        """Dismiss cookie consent banners and login prompts."""
        dismiss_texts = [
            "Allow all cookies",
            "Accept all",
            "Allow essential and optional cookies",
            "Only allow essential cookies",
            "Close",
            "OK",
        ]
        for text in dismiss_texts:
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.I))
                if await btn.first.is_visible(timeout=1500):
                    await btn.first.click()
                    await page.wait_for_timeout(600)
                    logger.debug(f"  Dismissed dialog: '{text}'")
                    return
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Sync wrapper so the rest of the codebase (scraper.py) stays synchronous
# ---------------------------------------------------------------------------

def run_search(browser: "AdsLibraryBrowser", keyword: str, max_ads: int = 120) -> list[dict]:
    """Synchronous wrapper around the async search_keyword coroutine."""
    return asyncio.run(_async_search(browser, keyword, max_ads))


async def _async_search(browser: "AdsLibraryBrowser", keyword: str, max_ads: int) -> list[dict]:
    return await browser.search_keyword(keyword, max_ads=max_ads)


def create_browser(countries: list[str], headless: bool = False) -> "AdsLibraryBrowser":
    """Create and start a browser instance synchronously."""
    browser = AdsLibraryBrowser(countries=countries, headless=headless)
    asyncio.run(browser.start())
    return browser


def close_browser(browser: "AdsLibraryBrowser"):
    asyncio.run(browser.stop())
