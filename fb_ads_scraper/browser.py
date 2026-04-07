"""
Selenium-based scraper for the Facebook Ads Library public website.
No API key, no Meta approval, no login required.

Install:  pip install selenium
Chrome is auto-managed by Selenium 4.x — no separate driver download needed.
"""

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

logger = logging.getLogger(__name__)

ADS_LIBRARY_BASE = "https://www.facebook.com/ads/library/"

SCROLL_DELAY = 2.5   # seconds between scrolls
PAGE_LOAD_WAIT = 5   # seconds after navigation before extracting

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def parse_follower_count(text: str) -> int:
    """Parse '1,234 likes', '1.2K followers', '2.5M likes' → int."""
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
    """Extract YYYY-MM-DD from strings like 'Started running on March 1, 2024'."""
    if not text:
        return None
    t = text.lower()
    m = re.search(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        mon = MONTH_MAP.get(m.group(1)[:3])
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    return None


# JavaScript that runs inside the browser to extract ad card data.
# Reused across scroll rounds — fast since it only reads the live DOM.
_EXTRACT_JS = """
() => {
    const results = [];
    const seen = new Set();

    const CTA_LABELS = new Set([
        'shop now','buy now','order now','get yours','get it now',
        'learn more','get offer','sign up','subscribe','get quote',
        'contact us','get started','apply now','download',
    ]);

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
            !href.includes('/l.php')
        );
    });

    pageLinks.forEach(link => {
        let container = link.parentElement;
        let found = false;
        for (let i = 0; i < 12; i++) {
            if (!container) break;
            const t = container.innerText || '';
            if (t.length > 200 && (
                t.includes('Started running') || t.includes('running on') ||
                /\\b202[3-9]\\b/.test(t)
            )) {
                found = true;
                break;
            }
            container = container.parentElement;
        }
        if (!found || !container) return;
        if (seen.has(container)) return;
        seen.add(container);

        const fullText = container.innerText || '';
        const fullHtml = container.innerHTML || '';
        const pageName = link.textContent.trim();
        const pageUrl = link.href;

        let followerText = '';
        container.querySelectorAll('span, div').forEach(el => {
            const t = (el.textContent || '').trim();
            if (/[\\d,\\.]+\\s*[KkMm]?\\s*(likes?|followers?)/i.test(t) && t.length < 40) {
                if (t.length > followerText.length) followerText = t;
            }
        });

        const hasVideo = (
            container.querySelector('video') !== null ||
            fullHtml.includes('<video') ||
            fullText.toLowerCase().includes('video')
        );

        let ctaButton = '';
        container.querySelectorAll('a, div[role="button"], button').forEach(el => {
            const t = (el.textContent || '').trim().toLowerCase();
            if (CTA_LABELS.has(t)) ctaButton = el.textContent.trim();
        });

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

        let dateText = '';
        const dateMatch = fullText.match(/(?:Started running|Active since|running on)[^\\n]{0,60}/i);
        if (dateMatch) dateText = dateMatch[0].trim();
        if (!dateText) {
            const bare = fullText.match(/(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\.?\\s+\\d{1,2},?\\s+\\d{4}/i);
            if (bare) dateText = bare[0].trim();
        }

        let snapshotUrl = '';
        container.querySelectorAll('a[href*="facebook.com/ads/archive"]').forEach(el => {
            snapshotUrl = el.href;
        });

        let adBody = '';
        container.querySelectorAll('div, p, span').forEach(el => {
            if (el.children.length > 5) return;
            const t = (el.textContent || '').trim();
            if (
                t.length > adBody.length && t.length < 2000 &&
                t !== pageName &&
                !t.includes('Started running') &&
                !t.match(/^\\d/)
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


def _make_driver(headless: bool = False) -> webdriver.Chrome:
    """Create a Chrome WebDriver configured to blend in with normal traffic."""
    opts = Options()

    if headless:
        opts.add_argument("--headless=new")

    # Blend in — remove obvious automation signals
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=opts)
    # Patch the navigator.webdriver flag
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


class AdsLibraryBrowser:
    """Selenium-driven scraper for the public Facebook Ads Library."""

    def __init__(self, countries: list[str], headless: bool = False):
        self.countries = countries
        self.headless = headless
        self._driver: Optional[webdriver.Chrome] = None

    def start(self):
        logger.info("Starting Chrome browser...")
        self._driver = _make_driver(headless=self.headless)
        if not self.headless:
            logger.info("Chrome window opened — don't close it during the scan.")

    def stop(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
        logger.info("Browser closed.")

    def search_keyword(self, keyword: str, max_ads: int = 120) -> list[dict]:
        """Navigate to the Ads Library, search keyword, scroll, return ad dicts."""
        country = self.countries[0] if self.countries else "US"
        url = ADS_LIBRARY_BASE + "?" + urlencode({
            "active_status": "active",
            "ad_type": "all",
            "country": country,
            "q": keyword,
            "search_type": "keyword_unordered",
            "media_type": "all",
        })

        try:
            logger.debug(f"  Navigating: {url}")
            self._driver.get(url)
            time.sleep(PAGE_LOAD_WAIT)

            self._dismiss_dialogs()
            time.sleep(1)

            all_ads: list[dict] = []
            seen_keys: set[str] = set()
            no_new_rounds = 0
            max_scrolls = max(10, max_ads // 15)

            for _ in range(max_scrolls):
                raw = self._driver.execute_script(
                    "return (" + _EXTRACT_JS + ")();"
                ) or []

                added = 0
                for ad in raw:
                    k = ad.get("_key", "")
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        all_ads.append(ad)
                        added += 1

                if added == 0:
                    no_new_rounds += 1
                    if no_new_rounds >= 3:
                        break
                else:
                    no_new_rounds = 0

                if len(all_ads) >= max_ads:
                    break

                self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                time.sleep(SCROLL_DELAY)

        except WebDriverException as e:
            logger.warning(f"  Browser error scraping '{keyword}': {e}")
            all_ads = []

        logger.info(f"  Scraped {len(all_ads)} ads for '{keyword}'")
        return all_ads

    def _dismiss_dialogs(self):
        """Dismiss cookie banners and login prompts."""
        dismiss_texts = [
            "Allow all cookies", "Accept all",
            "Allow essential and optional cookies",
            "Only allow essential cookies", "Close", "OK",
        ]
        for text in dismiss_texts:
            try:
                btn = self._driver.find_element(
                    By.XPATH,
                    f"//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    f"'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"
                )
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.8)
                    logger.debug(f"  Dismissed: '{text}'")
                    return
            except Exception:
                pass
