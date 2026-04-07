"""
Selenium-based scraper for the Facebook Ads Library public website.
No API key, no Meta approval, no login required.

Install:  pip install selenium
Chrome driver is auto-managed by Selenium 4.x.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urlencode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

ADS_LIBRARY_BASE = "https://www.facebook.com/ads/library/"
SCROLL_DELAY = 2.5
PAGE_LOAD_WAIT = 5

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def parse_follower_count(text: str) -> int:
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


# ---------------------------------------------------------------------------
# Injected JavaScript — returns a plain JSON-safe array of ad objects.
# Wrapped in try/catch so any JS error returns [] instead of crashing.
# All strings are cleaned of control characters before returning.
# ---------------------------------------------------------------------------
_EXTRACT_JS = r"""
var clean = function(s) {
    if (!s) return '';
    return String(s).replace(/[\u0000-\u001F\u007F\uFFFD]/g, ' ')
                    .replace(/\s+/g, ' ').trim().slice(0, 300);
};

var results = [];
var seen = [];

try {
    var CTA = ['shop now','buy now','order now','get yours','get it now',
               'learn more','get offer','sign up','subscribe'];

    var links = Array.prototype.slice.call(document.querySelectorAll('a[href]'));
    var pageLinks = links.filter(function(a) {
        var href = a.href || '';
        var txt = (a.textContent || '').trim();
        return txt.length >= 2 && txt.length <= 100
            && href.indexOf('facebook.com/') !== -1
            && href.indexOf('/ads/library') === -1
            && href.indexOf('facebook.com/help') === -1
            && href.indexOf('facebook.com/login') === -1
            && href.indexOf('/l.php') === -1
            && href.indexOf('facebook.com/policies') === -1;
    });

    pageLinks.forEach(function(link) {
        var container = link.parentElement;
        var found = false;
        for (var i = 0; i < 12; i++) {
            if (!container) break;
            var t = container.innerText || '';
            if (t.length > 150 && (
                t.indexOf('Started running') !== -1 ||
                t.indexOf('running on') !== -1 ||
                /202[3-9]/.test(t)
            )) {
                found = true;
                break;
            }
            container = container.parentElement;
        }
        if (!found || !container) return;
        if (seen.indexOf(container) !== -1) return;
        seen.push(container);

        var fullText = container.innerText || '';

        /* page name */
        var pageName = clean(link.textContent);

        /* follower text */
        var followerText = '';
        var spans = Array.prototype.slice.call(container.querySelectorAll('span,div'));
        spans.forEach(function(el) {
            var t = (el.textContent || '').trim();
            if (/[\d][\d,.]*\s*[KkMm]?\s*(likes?|followers?)/i.test(t) && t.length < 40) {
                if (t.length > followerText.length) followerText = t;
            }
        });

        /* video */
        var hasVideo = container.querySelector('video') !== null
                    || (container.innerHTML || '').indexOf('<video') !== -1;

        /* CTA — capture text AND destination href */
        var ctaButton = '';
        var ctaUrl = '';
        var btns = Array.prototype.slice.call(
            container.querySelectorAll('a,div[role="button"],button'));
        btns.forEach(function(el) {
            var t = (el.textContent || '').trim().toLowerCase();
            if (CTA.indexOf(t) !== -1) {
                ctaButton = (el.textContent || '').trim();
                /* Only grab href from real anchor tags — divs have no href */
                if (!ctaUrl && el.tagName === 'A' && el.href) {
                    ctaUrl = el.href;
                }
            }
        });

        var lowerText = fullText.toLowerCase();
        var hasShopNow = ctaButton.toLowerCase().indexOf('shop') !== -1
            || ctaButton.toLowerCase().indexOf('buy') !== -1
            || ctaButton.toLowerCase().indexOf('order') !== -1
            || lowerText.indexOf('shop now') !== -1
            || lowerText.indexOf('buy now') !== -1
            || lowerText.indexOf('order now') !== -1
            || lowerText.indexOf('get yours') !== -1;

        /* start date */
        var dateText = '';
        var dm = fullText.match(/(?:Started running|Active since|running on)[^\n]{0,60}/i);
        if (dm) dateText = dm[0];
        if (!dateText) {
            var dm2 = fullText.match(/(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}/i);
            if (dm2) dateText = dm2[0];
        }

        /* snapshot url */
        var snapshotUrl = '';
        var snapLinks = Array.prototype.slice.call(
            container.querySelectorAll('a[href*="ads/archive"]'));
        if (snapLinks.length) snapshotUrl = snapLinks[0].href || '';

        /* ad body — longest single-child text block */
        var adBody = '';
        var nodes = Array.prototype.slice.call(
            container.querySelectorAll('div,p,span'));
        nodes.forEach(function(el) {
            if (el.children.length > 4) return;
            var t = (el.textContent || '').trim();
            if (t.length > adBody.length && t.length < 800
                && t !== pageName
                && t.indexOf('Started running') === -1
                && !/^\d/.test(t)) {
                adBody = t;
            }
        });

        /* "N ads use this creative and text" — the real ad count signal */
        var adVersions = 1;
        var versionMatch = fullText.match(/(\d+)\s+ads?\s+use\s+this/i);
        if (versionMatch) adVersions = parseInt(versionMatch[1], 10) || 1;

        var key = (link.href || '') + '|' + adBody.slice(0, 50);

        results.push({
            _key:          clean(key),
            page_name:     pageName,
            page_url:      clean(link.href),
            follower_text: clean(followerText),
            has_video:     hasVideo ? true : false,
            has_shop_now:  hasShopNow ? true : false,
            cta_button:    clean(ctaButton),
            cta_url:       ctaUrl,   /* raw href — do NOT clean, preserves l.php encoding */
            date_text:     clean(dateText),
            snapshot_url:  clean(snapshotUrl),
            ad_body:       clean(adBody),
            ad_versions:   adVersions
        });
    });
} catch(e) {
    results.push({_error: String(e).slice(0, 200)});
}
return results;
"""


def _make_driver(headless: bool = False) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
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
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


class AdsLibraryBrowser:
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
        country = self.countries[0] if self.countries else "US"
        url = ADS_LIBRARY_BASE + "?" + urlencode({
            "active_status": "active",
            "ad_type": "all",
            "country": country,
            "q": keyword,
            "search_type": "keyword_unordered",
            "media_type": "all",
        })

        all_ads: list[dict] = []
        seen_keys: set[str] = set()

        try:
            self._driver.get(url)
            time.sleep(PAGE_LOAD_WAIT)
            self._dismiss_dialogs()
            time.sleep(1)

            # Log what page loaded so we can diagnose issues
            title = self._driver.title
            logger.debug(f"  Page title: {title}")

            no_new_rounds = 0
            max_scrolls = max(10, max_ads // 15)

            for scroll_n in range(max_scrolls):
                try:
                    raw = self._driver.execute_script(_EXTRACT_JS) or []
                except WebDriverException as js_err:
                    logger.debug(f"  JS error on scroll {scroll_n} (skipping): {str(js_err)[:80]}")
                    self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                    time.sleep(SCROLL_DELAY)
                    continue

                # Check for JS-level errors
                for item in raw:
                    if "_error" in item:
                        logger.warning(f"  JS reported: {item['_error']}")

                added = 0
                for ad in raw:
                    if "_error" in ad:
                        continue
                    k = ad.get("_key", "")
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        all_ads.append(ad)
                        added += 1

                if added == 0:
                    no_new_rounds += 1
                    if no_new_rounds >= 3:
                        logger.debug(f"  No new ads after 3 scrolls — stopping")
                        break
                else:
                    no_new_rounds = 0

                if len(all_ads) >= max_ads:
                    break

                self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                time.sleep(SCROLL_DELAY)

        except WebDriverException as e:
            logger.warning(f"  Browser error scraping '{keyword}': {e.msg[:200] if hasattr(e,'msg') else str(e)[:200]}")

        logger.info(f"  Scraped {len(all_ads)} ads for '{keyword}'")
        return all_ads

    def get_page_ads(self, page_id: str, max_ads: int = 200) -> tuple[int, list[dict]]:
        """
        Visit a page's own Ads Library view.
        Returns (follower_count, ads_list).
        Follower count is extracted from the page header shown on this view.
        """
        country = self.countries[0] if self.countries else "US"
        if page_id.isdigit():
            url = ADS_LIBRARY_BASE + "?" + urlencode({
                "active_status": "active",
                "ad_type": "all",
                "country": country,
                "search_type": "page",
                "view_all_page_id": page_id,
            })
        else:
            url = ADS_LIBRARY_BASE + "?" + urlencode({
                "active_status": "active",
                "ad_type": "all",
                "country": country,
                "q": page_id,
                "search_type": "page",
            })

        all_ads: list[dict] = []
        seen_keys: set[str] = set()
        follower_count = 0

        try:
            self._driver.get(url)
            time.sleep(4)

            # Extract follower count from the page header shown on this view.
            # The dedicated page view shows "X followers" or "X people like this"
            # in the header above the ad results.
            try:
                follower_text = self._driver.execute_script("""
                    var t = document.body.innerText || '';
                    var m = t.match(/([\d][\d,\\.]*\\s*[KkMm]?)\\s*(people like this|followers?|likes?)/i);
                    return m ? m[0] : '';
                """) or ""
                if follower_text:
                    follower_count = parse_follower_count(follower_text)
                    logger.debug(f"  Followers for {page_id}: {follower_text} → {follower_count}")
            except Exception:
                pass

            no_new = 0
            for _ in range(15):
                try:
                    raw = self._driver.execute_script(_EXTRACT_JS) or []
                except WebDriverException:
                    raw = []

                added = 0
                for ad in raw:
                    if "_error" in ad:
                        continue
                    k = ad.get("_key", "")
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        all_ads.append(ad)
                        added += 1

                if added == 0:
                    no_new += 1
                    if no_new >= 3:
                        break
                else:
                    no_new = 0

                if len(all_ads) >= max_ads:
                    break

                self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                time.sleep(2.0)

        except WebDriverException as e:
            logger.debug(f"  get_page_ads error for {page_id}: {str(e)[:100]}")

        return follower_count, all_ads

    def check_shopify_via_browser(self, url: str) -> tuple[bool, str]:
        """
        Navigate to the actual store URL and check the live page source for
        Shopify signals. Returns (is_shopify: bool, reason: str).
        Always navigates back to the previous Ads Library page when done.
        """
        from .shopify import decode_facebook_redirect, _SHOPIFY_HTML_PATTERNS
        if not url or not self._driver:
            return False, "no url"

        destination = decode_facebook_redirect(url)
        if not destination or not destination.startswith("http"):
            return False, "could not decode url"

        previous_url = self._driver.current_url
        try:
            self._driver.get(destination)
            time.sleep(3)

            final_url = self._driver.current_url
            if "myshopify.com" in final_url:
                return True, "browser: myshopify.com in URL"

            source = self._driver.page_source or ""
            for pattern in _SHOPIFY_HTML_PATTERNS:
                if pattern.search(source):
                    return True, f"browser: {pattern.pattern[:30]}"

            return False, "browser: no shopify signals"

        except WebDriverException as e:
            msg = e.msg[:100] if hasattr(e, "msg") else str(e)[:100]
            logger.debug(f"check_shopify_via_browser error: {msg}")
            return False, f"browser error"

        finally:
            try:
                self._driver.get(previous_url)
                time.sleep(1.5)
            except Exception:
                pass

    def _dismiss_dialogs(self):
        for text in ["Allow all cookies", "Accept all",
                     "Allow essential and optional cookies",
                     "Only allow essential cookies"]:
            try:
                btns = self._driver.find_elements(By.XPATH, f"//button[contains(.,'{text}')]")
                for btn in btns:
                    if btn.is_displayed():
                        btn.click()
                        time.sleep(0.8)
                        return
            except Exception:
                pass
