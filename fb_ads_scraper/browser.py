"""
Selenium-based scraper for the Facebook Ads Library public website.
No API key, no Meta approval, no login required.

Install:  pip install selenium
Chrome driver is auto-managed by Selenium 4.x.
"""

import json
import logging
import os
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
COOKIES_FILE = "fb_cookies.json"

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
        var pageName = clean(link.textContent);

        var followerText = '';
        var spans = Array.prototype.slice.call(container.querySelectorAll('span,div'));
        spans.forEach(function(el) {
            var t = (el.textContent || '').trim();
            if (/[\d][\d,.]*\s*[KkMm]?\s*(likes?|followers?)/i.test(t) && t.length < 40) {
                if (t.length > followerText.length) followerText = t;
            }
        });

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

        var dateText = '';
        var dm = fullText.match(/(?:Started running|Active since|running on)[^\n]{0,60}/i);
        if (dm) dateText = dm[0];
        if (!dateText) {
            var dm2 = fullText.match(/(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}/i);
            if (dm2) dateText = dm2[0];
        }

        var snapshotUrl = '';
        var snapLinks = Array.prototype.slice.call(
            container.querySelectorAll('a[href*="ads/archive"]'));
        if (snapLinks.length) snapshotUrl = snapLinks[0].href || '';

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
            cta_url:       ctaUrl,
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


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
const orig = navigator.permissions.query;
navigator.permissions.query = (params) =>
    params.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : orig(params);
"""


def _make_driver(headless: bool = False) -> webdriver.Chrome:
    # Prefer undetected-chromedriver — patches Chrome binary fingerprints that
    # Facebook's bot detection specifically checks.
    try:
        import undetected_chromedriver as uc
        import sys, io

        opts = uc.ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1366,900")
        opts.add_argument("--lang=en-US")

        # Suppress uc's stdout progress bar (it uses \r which corrupts Rich output)
        _old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            driver = uc.Chrome(options=opts)
        finally:
            sys.stdout = _old_stdout

        logger.info("Using undetected-chromedriver (stealth mode)")
        return driver
    except ImportError as e:
        logger.warning(f"undetected-chromedriver import error: {e} — pip install undetected-chromedriver")
    except Exception as e:
        logger.warning(f"undetected-chromedriver failed: {type(e).__name__}: {e!s:.200}")

    # Fallback: regular Selenium with manual stealth patches
    logger.info("Using standard selenium (bot detection may block results)")
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
        {"source": _STEALTH_JS},
    )
    return driver


class AdsLibraryBrowser:
    def __init__(self, countries: list[str], headless: bool = False):
        self.countries = countries
        self.headless = headless
        self._driver: Optional[webdriver.Chrome] = None
        self._cookies_loaded = False

    def start(self):
        logger.info("Starting Chrome browser...")
        self._driver = _make_driver(headless=self.headless)
        if not self.headless:
            logger.info("Chrome window opened — don't close it during the scan.")

        # Navigate to Facebook first (required before loading cookies)
        self._driver.get("https://www.facebook.com/ads/library/")
        time.sleep(3)
        self._load_cookies()
        if self._cookies_loaded:
            # Reload with cookies applied
            self._driver.get("https://www.facebook.com/ads/library/")
            time.sleep(3)
        self._dismiss_dialogs()

    def stop(self):
        if self._driver:
            try:
                self._save_cookies()
                self._driver.quit()
            except Exception:
                pass
        logger.info("Browser closed.")

    def _save_cookies(self):
        """Save cookies so consent is remembered across runs."""
        try:
            cookies = self._driver.get_cookies()
            with open(COOKIES_FILE, "w") as f:
                json.dump(cookies, f)
            logger.debug(f"Saved {len(cookies)} cookies to {COOKIES_FILE}")
        except Exception as e:
            logger.debug(f"Cookie save failed: {e}")

    def _load_cookies(self):
        """Load previously saved cookies to skip consent wall."""
        if not os.path.exists(COOKIES_FILE):
            return
        try:
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)
            for cookie in cookies:
                # Selenium requires domain to match
                cookie.pop("sameSite", None)
                try:
                    self._driver.add_cookie(cookie)
                except Exception:
                    pass
            self._cookies_loaded = True
            logger.debug(f"Loaded {len(cookies)} cookies from {COOKIES_FILE}")
        except Exception as e:
            logger.debug(f"Cookie load failed: {e}")

    def _dismiss_dialogs(self):
        """Aggressively dismiss cookie / GDPR consent walls."""
        for attempt in range(5):
            dismissed = self._try_dismiss()
            if dismissed:
                time.sleep(1.5)
                self._save_cookies()  # save immediately after accepting
                return
            if attempt < 4:
                time.sleep(1.5)

    def _try_dismiss(self) -> bool:
        """Single attempt to find and click a consent button. Returns True if clicked."""

        # Strategy 1: exact + partial text match on buttons
        for text in [
            "Allow all cookies", "Accept all", "Allow All", "Accept All",
            "Allow essential and optional cookies",
            "Only allow essential cookies", "Decline optional cookies",
            "OK", "Got it", "I Accept", "Continue", "Allow cookies",
            "Accept and continue", "Allow all",
        ]:
            try:
                btns = self._driver.find_elements(
                    By.XPATH,
                    f"//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"
                )
                for btn in btns:
                    if btn.is_displayed():
                        btn.click()
                        logger.debug(f"Dismissed via button text '{text}'")
                        return True
            except Exception:
                pass

        # Strategy 2: JS — click any visible button whose text matches consent keywords
        try:
            clicked = self._driver.execute_script("""
                var keywords = ['allow','accept','cookie','decline','continue','got it','ok'];
                var btns = Array.prototype.slice.call(document.querySelectorAll('button'));
                for (var i = 0; i < btns.length; i++) {
                    var b = btns[i];
                    var t = (b.innerText || '').toLowerCase().trim();
                    if (t.length < 60 && keywords.some(function(k){ return t.indexOf(k) !== -1; })) {
                        if (b.offsetParent !== null) {
                            b.click();
                            return t;
                        }
                    }
                }
                return null;
            """)
            if clicked:
                logger.debug(f"Dismissed via JS click: '{clicked}'")
                return True
        except Exception:
            pass

        # Strategy 3: any button inside an overlay / dialog
        try:
            btns = self._driver.find_elements(
                By.XPATH,
                "//div[@role='dialog']//button | //*[@data-testid='cookie-policy-dialog']//button"
            )
            for btn in btns:
                if btn.is_displayed():
                    btn.click()
                    logger.debug("Dismissed via role=dialog button")
                    return True
        except Exception:
            pass

        return False

    def _wait_for_ads(self, timeout: int = 20) -> bool:
        """
        Wait until real ad cards appear. Retries consent dismissal if wall detected.
        Returns True if ads loaded.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                text = self._driver.execute_script(
                    "return document.body ? document.body.innerText : '';"
                ) or ""

                if any(x in text for x in [
                    "Started running", "running on", "Active since",
                    "ads use this creative", "See ad details",
                    "library result", "results for",
                ]):
                    return True

                # Still showing consent wall — try again
                if any(x in text.lower() for x in [
                    "allow all cookies", "accept all", "before you continue",
                    "cookie", "privacy policy",
                ]):
                    logger.debug("  Consent wall detected mid-wait, retrying dismiss...")
                    self._try_dismiss()

            except Exception:
                pass
            time.sleep(1.5)

        # Log what's on the page to help diagnose
        try:
            text = self._driver.execute_script(
                "return (document.body ? document.body.innerText : '').slice(0, 400);"
            ) or ""
            logger.warning(f"  No ads loaded after {timeout}s. Page shows: {text[:200]!r}")
        except Exception:
            pass
        return False

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

            # Nudge-scroll: triggers React lazy-load without moving past results
            try:
                self._driver.execute_script("window.scrollBy(0, 400);")
                time.sleep(0.4)
                self._driver.execute_script("window.scrollBy(0, -400);")
            except Exception:
                pass

            if not self._wait_for_ads(timeout=25):
                logger.warning(f"  Skipping '{keyword}' — page never loaded ads")
                return []

            title = self._driver.title
            logger.debug(f"  Page title: {title}")

            no_new_rounds = 0
            max_scrolls = max(10, max_ads // 15)

            for scroll_n in range(max_scrolls):
                try:
                    raw = self._driver.execute_script(_EXTRACT_JS) or []
                except WebDriverException as js_err:
                    logger.debug(f"  JS error scroll {scroll_n}: {str(js_err)[:80]}")
                    self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                    time.sleep(SCROLL_DELAY)
                    continue

                for item in raw:
                    if "_error" in item:
                        logger.warning(f"  JS: {item['_error']}")

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
                        break
                else:
                    no_new_rounds = 0

                if len(all_ads) >= max_ads:
                    break

                self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                time.sleep(SCROLL_DELAY)

        except WebDriverException as e:
            logger.warning(f"  Browser error '{keyword}': {e.msg[:200] if hasattr(e,'msg') else str(e)[:200]}")

        logger.info(f"  Scraped {len(all_ads)} ads for '{keyword}'")
        return all_ads

    def get_page_ads(self, page_id: str, max_ads: int = 200) -> tuple[int, list[dict]]:
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
            time.sleep(PAGE_LOAD_WAIT)
            self._dismiss_dialogs()
            self._wait_for_ads(timeout=12)

            # Extract follower count — retry a few times for header to render.
            # The Ads Library page sometimes shows it in the advertiser header.
            for _attempt in range(4):
                try:
                    follower_text = self._driver.execute_script(r"""
                        var t = document.body.innerText || '';
                        // Multiple patterns Facebook uses for follower display
                        var patterns = [
                            /([\d][\d,\.]*\s*[KkMm]?)\s*(people like this|followers?|likes?)/i,
                            /followers?\s*[:\u00b7\u2022]?\s*([\d][\d,\.]*\s*[KkMm]?)/i,
                            /([\d][\d,\.]*\s*[KkMm]?)\s*(?:people follow)/i
                        ];
                        for (var i = 0; i < patterns.length; i++) {
                            var m = t.match(patterns[i]);
                            if (m) return m[0];
                        }
                        return '';
                    """) or ""
                    if follower_text:
                        follower_count = parse_follower_count(follower_text)
                        logger.debug(f"  Followers {page_id}: {follower_text} → {follower_count}")
                        break
                    time.sleep(1.5)
                except Exception:
                    break

            # If still 0, try the actual Facebook page (shows follower count prominently)
            if follower_count == 0 and not page_id.isdigit():
                try:
                    prev_url = self._driver.current_url
                    self._driver.get(f"https://www.facebook.com/{page_id}")
                    time.sleep(3)
                    follower_text = self._driver.execute_script(r"""
                        var t = document.body.innerText || '';
                        var m = t.match(/([\d][\d,\.]*\s*[KkMm]?)\s*(followers?|people follow)/i);
                        return m ? m[0] : '';
                    """) or ""
                    if follower_text:
                        follower_count = parse_follower_count(follower_text)
                        logger.debug(f"  Followers (fb page) {page_id}: {follower_text} → {follower_count}")
                    # Navigate back to the Ads Library page we were on
                    self._driver.get(prev_url)
                    time.sleep(PAGE_LOAD_WAIT)
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
            logger.debug(f"  get_page_ads error {page_id}: {str(e)[:100]}")

        return follower_count, all_ads

    def check_shopify_via_browser(self, url: str) -> tuple[bool, str]:
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
            return False, "browser error"

        finally:
            try:
                self._driver.get(previous_url)
                time.sleep(1.5)
            except Exception:
                pass
