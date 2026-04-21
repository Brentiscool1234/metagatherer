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
SCROLL_DELAY = 1.2       # was 2.5 — enough for React lazy-load without over-waiting
PAGE_LOAD_WAIT = 2.5     # was 5 — _wait_for_ads() does the real waiting
COOKIES_FILE = "fb_cookies.json"

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def parse_follower_count(text: str) -> int:
    """
    Parse a follower/like count string into an integer.
    Handles:
      "54K followers"        / "54K follow this"  / "818 likes"  (number first)
      "Followers · 54.2K"   / "Followers: 1.2M"                  (label first)
      "12345 followers"      plain integer from JSON extraction
    """
    t = text.lower().replace(",", "").strip()
    # Plain integer followed by "followers" (from JSON strategy)
    import re as _re
    m0 = _re.match(r"^(\d+)\s+followers?$", t)
    if m0:
        try:
            return int(m0.group(1))
        except ValueError:
            pass

    def _parse_num(num_str: str, suffix: str) -> int:
        try:
            n = float(num_str)
            s = (suffix or "").lower()
            if s == "k":
                n *= 1_000
            elif s == "m":
                n *= 1_000_000
            return int(n)
        except (ValueError, TypeError):
            return 0

    # Format A: "54K followers", "54K follow this", "818 likes", "7.8K people follow"
    m = re.search(
        r"([\d.]+)\s*([km])?\s*"
        r"(?:follow(?:ers?)?(?:\s+this)?|likes?|people\s+follow)",
        t,
    )
    if m:
        v = _parse_num(m.group(1), m.group(2))
        if v:
            return v

    # Format B: "followers · 54.2K", "followers: 1.2M", "likes · 818"
    m2 = re.search(
        r"(?:follow(?:ers?)?|likes?)\s*[·:\-\u00b7\u2022]?\s*([\d.]+)\s*([km]?)",
        t,
    )
    if m2:
        v = _parse_num(m2.group(1), m2.group(2))
        if v:
            return v

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
        var spans = Array.prototype.slice.call(container.querySelectorAll('span,div,p'));
        spans.forEach(function(el) {
            var t = (el.textContent || '').trim();
            if (t.length < 2 || t.length > 60) return;
            // "54K followers" / "54K follow this" / "818 likes" (number first)
            var ok1 = /[\d][\d,.]*\s*[KkMm]?\s*(likes?|followers?|follow this)/i.test(t);
            // "followers · 54K" / "followers: 1.2M" (label first — current FB format)
            var ok2 = /followers?\s*[·:\-\u00b7]?\s*[\d][\d,.]*\s*[KkMm]/i.test(t);
            if ((ok1 || ok2) && t.length > followerText.length) {
                followerText = t;
            }
        });

        var hasVideo = container.querySelector('video') !== null
                    || (container.innerHTML || '').indexOf('<video') !== -1;

        /* CTA — capture text AND destination URL.
           Strategy 1: l.php redirect links (Facebook wraps all external URLs this way).
           Strategy 2: any href on a CTA-text element.
           Strategy 3: any non-Facebook http link in the card (last resort). */
        var ctaButton = '';
        var ctaUrl = '';

        // Strategy 1: grab all l.php links — these ARE the shop/store URLs
        var lphpLinks = Array.prototype.slice.call(
            container.querySelectorAll('a[href*="l.php"]'));
        if (lphpLinks.length) {
            ctaUrl = lphpLinks[0].href || '';
        }

        // Strategy 2: look for CTA-text buttons and grab their href or parent href
        var btns = Array.prototype.slice.call(
            container.querySelectorAll('a,div[role="button"],button,span[role="button"]'));
        btns.forEach(function(el) {
            var t = (el.textContent || '').trim().toLowerCase();
            if (CTA.indexOf(t) !== -1) {
                ctaButton = (el.textContent || '').trim();
                // Walk up to find enclosing <a>
                if (!ctaUrl) {
                    var node = el;
                    for (var up = 0; up < 4; up++) {
                        if (node && node.tagName === 'A' && node.href) {
                            ctaUrl = node.href; break;
                        }
                        node = node && node.parentElement;
                    }
                }
            }
        });

        // Strategy 3: any non-Facebook http link as fallback
        if (!ctaUrl) {
            var allLinks = Array.prototype.slice.call(container.querySelectorAll('a[href^="http"]'));
            for (var li = 0; li < allLinks.length; li++) {
                var h = allLinks[li].href || '';
                if (h.indexOf('facebook.com') === -1 && h.indexOf('fbcdn.net') === -1
                        && h.indexOf('fb.com') === -1) {
                    ctaUrl = h; break;
                }
            }
        }

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

                # Detect empty result pages early — no point waiting the full timeout
                if any(x in text for x in [
                    "No results found", "no results found",
                    "0 results", "didn't find any ads",
                ]):
                    logger.debug("  Empty results page detected — skipping keyword")
                    return False

                # Still showing consent wall — try again
                if any(x in text.lower() for x in [
                    "allow all cookies", "accept all", "before you continue",
                    "cookie", "privacy policy",
                ]):
                    logger.debug("  Consent wall detected mid-wait, retrying dismiss...")
                    self._try_dismiss()

            except Exception:
                pass
            time.sleep(0.8)   # was 1.5

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

        all_ads: list[dict] = []
        seen_keys: set[str] = set()

        # Two-pass strategy: search VIDEO then IMAGE separately.
        # Facebook's public Ads Library caps results at ~150 per search request.
        # Splitting by media_type gives each pass its own ~150-slot quota, so we
        # can collect up to ~300 total per keyword instead of ~150.
        #
        # Sort by total_impressions desc: within each 150-cap window we get the
        # highest-performing ads first — not random order.  This means even when
        # we can't collect everything, what we DO collect is the most valuable.
        #
        # active_status=active: only currently running ads are returned, so every
        # collected ad is a live product (no need to filter by is_active later).
        # is_targeted_country=false: include ads reaching the country but not
        # specifically targeted there — broadens the result pool.
        for media_type in ("video", "image"):
            if len(all_ads) >= max_ads:
                break

            url = (
                ADS_LIBRARY_BASE + "?" +
                urlencode({
                    "active_status": "active",
                    "ad_type": "all",
                    "country": country,
                    "q": keyword,
                    "search_type": "keyword_unordered",
                    "media_type": media_type,
                    "is_targeted_country": "false",
                }) +
                "&sort_data%5Bdirection%5D=desc&sort_data%5Bmode%5D=total_impressions"
            )

            try:
                self._driver.get(url)
                time.sleep(PAGE_LOAD_WAIT)
                self._dismiss_dialogs()

                if not self._wait_for_ads(timeout=18):
                    logger.debug(f"  '{keyword}' ({media_type}): no results")
                    continue

                no_new_rounds = 0
                # Allow enough scrolls to exhaust each media-type pass.
                max_scrolls = max(15, max_ads // 10)

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
                        if no_new_rounds >= 2:
                            break
                    else:
                        no_new_rounds = 0

                    if len(all_ads) >= max_ads:
                        break

                    self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                    time.sleep(SCROLL_DELAY)

            except WebDriverException as e:
                logger.warning(f"  Browser error '{keyword}' ({media_type}): "
                               f"{e.msg[:200] if hasattr(e,'msg') else str(e)[:200]}")

        logger.info(f"  Scraped {len(all_ads)} ads for '{keyword}'")
        return all_ads

    def get_page_ads(self, page_id: str, max_ads: int = 200, known_followers: int = 0) -> tuple[int, list[dict]]:
        country = self.countries[0] if self.countries else "US"
        if page_id.isdigit():
            url = ADS_LIBRARY_BASE + "?" + urlencode({
                "active_status": "all",   # match Apify — see full ad history
                "ad_type": "all",
                "country": country,
                "search_type": "page",
                "view_all_page_id": page_id,
            })
        else:
            url = ADS_LIBRARY_BASE + "?" + urlencode({
                "active_status": "all",
                "ad_type": "all",
                "country": country,
                "q": page_id,
                "search_type": "page",
            })

        all_ads: list[dict] = []
        seen_keys: set[str] = set()
        # Use Phase-1 follower count (Apify/API) when available — avoids the
        # autocomplete fallback which navigates away and triggers "No ads loaded" warnings.
        follower_count = known_followers

        try:
            self._driver.get(url)
            time.sleep(PAGE_LOAD_WAIT)
            self._dismiss_dialogs()
            self._wait_for_ads(timeout=10)

            # Extract follower count — skip if already known from Phase 1 data.
            # Poll for up to 8s: React renders the advertiser panel independently
            # from the ad cards, so _wait_for_ads() returning doesn't mean the
            # follower count is in the DOM yet.  Scroll to top first so the panel
            # is in the viewport (lazy renderers may skip off-screen content).
            _FOLLOWER_JS = r"""
                // Strategy 0: embedded JSON data blobs — most reliable source.
                // Facebook hydrates React from <script> tags as JSON; fan_count
                // and follower_count are present even when the UI text is in a
                // lazy or portal component that hasn't rendered yet.
                var scripts = Array.prototype.slice.call(document.querySelectorAll('script'));
                for (var si = 0; si < scripts.length; si++) {
                    var sc = scripts[si].textContent || '';
                    if (sc.length < 20 || sc.length > 3000000) continue;
                    var mfc = sc.match(/"fan_count"\s*:\s*(\d+)/);
                    if (mfc && parseInt(mfc[1]) > 0) return mfc[1] + ' followers';
                    var mfl = sc.match(/"follower_count"\s*:\s*(\d+)/);
                    if (mfl && parseInt(mfl[1]) > 0) return mfl[1] + ' followers';
                    var mli = sc.match(/"likers?"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)/);
                    if (mli && parseInt(mli[1]) > 0) return mli[1] + ' followers';
                    var mpl = sc.match(/"page_likers?"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)/);
                    if (mpl && parseInt(mpl[1]) > 0) return mpl[1] + ' followers';
                    var msc2 = sc.match(/"subscribers_count"\s*:\s*(\d+)/);
                    if (msc2 && parseInt(msc2[1]) > 0) return msc2[1] + ' followers';
                }
                // Strategy 1: short DOM element text
                var panelCandidates = Array.prototype.slice.call(
                    document.querySelectorAll('aside,header,[role="complementary"],[role="banner"]'));
                panelCandidates = panelCandidates.concat(
                    Array.prototype.slice.call(document.querySelectorAll('span,div,p')));
                for (var pi = 0; pi < panelCandidates.length; pi++) {
                    var pt = (panelCandidates[pi].textContent || '').trim();
                    if (pt.length > 80 || pt.length < 2) continue;
                    var pm = pt.match(/(\d[\d,.]*\s*[KkMm]?)\s*(followers?|follow this|likes?|people follow)/i);
                    if (pm) return pt;
                    var pm2 = pt.match(/followers?\s*[\u00b7:\-]?\s*(\d[\d,.]*\s*[KkMm])/i);
                    if (pm2) return pt;
                }
                // Strategy 2: full page text scan
                var t = document.body.innerText || '';
                var patterns2 = [
                    /(\d[\d,.]*\s*[KkMm]?)\s*follow this/i,
                    /(\d[\d,.]*\s*[KkMm]?)\s*(people like this|followers?|likes?)/i,
                    /(\d[\d,.]*\s*[KkMm]?)\s*people follow/i,
                    /followers?\s*[:·\xb7\-]\s*(\d[\d,.]*\s*[KkMm]?)/i,
                    /[·\xb7]\s*(\d[\d,.]*\s*[KkMm]?)\s*(follow(?:ers?)?|likes?)/i,
                ];
                for (var i = 0; i < patterns2.length; i++) {
                    var m = t.match(patterns2[i]);
                    if (m) return m[0];
                }
                // Strategy 3: aria-labels
                var metas = Array.prototype.slice.call(document.querySelectorAll('[aria-label]'));
                for (var j = 0; j < metas.length; j++) {
                    var al = (metas[j].getAttribute('aria-label') || '');
                    var fm = al.match(/(\d[\d,.]*\s*[KkMm]?)\s*(follow(?:ers?)?|likes?)/i);
                    if (fm) return fm[0];
                    var fm2 = al.match(/followers?\s*[\u00b7:\-]?\s*(\d[\d,.]*\s*[KkMm])/i);
                    if (fm2) return fm2[0];
                }
                return '';
            """
            try:
                self._driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(0.3)
            except Exception:
                pass

            _fl_deadline = time.time() + 8.0
            while time.time() < _fl_deadline and follower_count == 0:
                try:
                    follower_text = self._driver.execute_script(_FOLLOWER_JS) or ""
                    if follower_text:
                        follower_count = parse_follower_count(follower_text)
                        if follower_count:
                            logger.debug(
                                f"  Followers {page_id}: {follower_text!r} → {follower_count}"
                            )
                            break
                except Exception:
                    break
                time.sleep(0.8)

            # Fallback: type the page name into the Ads Library search box and
            # read the follower count from the autocomplete "Advertisers" panel.
            # Only trigger this if follower count is still unknown — if known_followers
            # was passed by the caller (from Phase-1 Apify/API data), skip entirely.
            if follower_count == 0:
                try:
                    page_name_from_dom = self._driver.execute_script(r"""
                        var links = Array.prototype.slice.call(
                            document.querySelectorAll('a[href*="facebook.com/"]'));
                        for (var i = 0; i < links.length; i++) {
                            var t = (links[i].textContent || '').trim();
                            if (t.length >= 3 && t.length <= 80
                                    && links[i].href.indexOf('/ads/library') === -1
                                    && links[i].href.indexOf('/help') === -1) {
                                return t;
                            }
                        }
                        return '';
                    """) or ""
                except Exception:
                    page_name_from_dom = ""
                search_name = page_name_from_dom or page_id
                follower_count = self.get_followers_from_autocomplete(search_name)
                # get_followers_from_autocomplete navigates to ADS_LIBRARY_BASE —
                # return to this page's URL so the ad collection loop runs correctly.
                try:
                    self._driver.get(url)
                    time.sleep(PAGE_LOAD_WAIT)
                    self._dismiss_dialogs()
                    self._wait_for_ads(timeout=10)
                except Exception:
                    pass

            # Scroll to collect all ads on this page
            no_new = 0
            for _ in range(12):   # was 15 × 2.0s; now 12 × 1.2s
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
                    if no_new >= 2:   # was 3
                        break
                else:
                    no_new = 0

                if len(all_ads) >= max_ads:
                    break

                self._driver.execute_script("window.scrollBy(0, window.innerHeight * 2.5);")
                time.sleep(1.2)   # was 2.0

        except WebDriverException as e:
            logger.debug(f"  get_page_ads error {page_id}: {str(e)[:100]}")

        # Last resort: extract follower count from the ad cards themselves.
        # Each card returned by _EXTRACT_JS has a follower_text field scraped
        # from the card's "54K followers" label. Use the first non-zero one.
        if follower_count == 0 and all_ads:
            for _ad in all_ads:
                _ft = _ad.get("follower_text", "")
                if _ft:
                    _fc = parse_follower_count(_ft)
                    if _fc:
                        follower_count = _fc
                        logger.debug(
                            f"  Followers {page_id}: from ad card "
                            f"{_ft!r} → {follower_count}"
                        )
                        break

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
            time.sleep(1.5)   # was 3 — store pages load faster than FB

            final_url = self._driver.current_url
            if "myshopify.com" in final_url:
                return True, "browser: myshopify.com in URL"

            source = self._driver.page_source or ""
            for pattern in _SHOPIFY_HTML_PATTERNS:
                if pattern.search(source):
                    return True, f"browser: {pattern.pattern[:30]}"

            return False, "browser: no shopify signals"

        except WebDriverException:
            return False, "browser error"

        finally:
            try:
                self._driver.get(previous_url)
                time.sleep(0.8)   # was 1.5
            except Exception:
                pass

    def get_followers_from_autocomplete(self, page_name: str) -> int:
        """
        Find the follower count for a Facebook page via the Ads Library search
        autocomplete ("Advertisers" mode shows "4K follow this" in results).

        Uses JavaScript + ActionChains throughout — no Selenium element objects —
        so Facebook's disabled-until-clicked input never triggers is_enabled() checks.

        Returns 0 on failure.
        """
        if not page_name or not self._driver:
            return 0

        try:
            _ = self._driver.title
        except Exception as _e:
            logger.debug(f"  Autocomplete: browser session dead — skipping")
            return 0

        try:
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.keys import Keys

            _country = self.countries[0] if self.countries else "US"
            _ac_url = (
                f"{ADS_LIBRARY_BASE}?active_status=active&ad_type=all"
                f"&country={_country}&media_type=all"
            )
            self._driver.get(_ac_url)
            time.sleep(1.8)
            self._dismiss_dialogs()

            # Step 1: JS activates the first visible input (removes disabled,
            # focuses, fires synthetic mouse events for FB's React handlers).
            _found = self._driver.execute_script("""
                var inputs = Array.prototype.slice.call(document.querySelectorAll('input'));
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    if (!inp.offsetParent) continue;
                    inp.removeAttribute('disabled');
                    inp.removeAttribute('readonly');
                    inp.focus();
                    inp.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                    inp.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true}));
                    inp.dispatchEvent(new MouseEvent('click',     {bubbles:true}));
                    return true;
                }
                return false;
            """)
            if not _found:
                logger.debug(f"  Autocomplete: no visible input on page for {page_name!r}")
                return 0

            time.sleep(0.8)  # wait for FB JS to open Keywords/Advertisers tabs

            # Step 2: click the "Advertisers" tab so autocomplete shows page names
            # with follower counts instead of keyword matches.
            _switched = False
            for _js in [
                """
                var els = Array.prototype.slice.call(document.querySelectorAll(
                    'div[role="tab"],div[role="button"],button,li,span,div'));
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || els[i].textContent || '').trim();
                    if (/^advertiser/i.test(t) && t.length < 50 && els[i].offsetParent) {
                        els[i].click(); return 'tab:' + t;
                    }
                }
                return null;
                """,
                """
                var els = Array.prototype.slice.call(document.querySelectorAll('[aria-label]'));
                for (var i = 0; i < els.length; i++) {
                    var a = (els[i].getAttribute('aria-label') || '').toLowerCase();
                    if (a.indexOf('advertiser') !== -1 && els[i].offsetParent) {
                        els[i].click(); return 'aria:' + a;
                    }
                }
                return null;
                """,
            ]:
                try:
                    _res = self._driver.execute_script(_js)
                    if _res:
                        logger.debug(f"  Autocomplete: Advertisers tab clicked ({_res})")
                        _switched = True
                        time.sleep(0.5)
                        break
                except Exception:
                    pass

            if not _switched:
                logger.debug("  Autocomplete: Advertisers tab not found — trying anyway")

            # Step 3: re-activate the input (mode switch may have moved focus),
            # then type via ActionChains (sends to whatever has focus — no element needed).
            self._driver.execute_script("""
                var inputs = Array.prototype.slice.call(document.querySelectorAll('input'));
                for (var i = 0; i < inputs.length; i++) {
                    if (!inputs[i].offsetParent) continue;
                    inputs[i].removeAttribute('disabled');
                    inputs[i].removeAttribute('readonly');
                    inputs[i].focus();
                    break;
                }
            """)
            time.sleep(0.2)

            # Count headings before typing to detect when dropdown appears
            try:
                _pre = len(self._driver.find_elements(
                    By.CSS_SELECTOR, '[role="heading"]'))
            except Exception:
                _pre = 0

            # Select-all + type — ActionChains targets the focused element,
            # completely bypassing Selenium element interactability checks.
            (ActionChains(self._driver)
                .key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL)
                .send_keys(page_name)
                .perform())

            # Step 4: wait for autocomplete dropdown (new headings = it appeared)
            _deadline = time.time() + 4.5
            while time.time() < _deadline:
                try:
                    _cur = len(self._driver.find_elements(
                        By.CSS_SELECTOR, '[role="heading"]'))
                    if _cur > _pre:
                        logger.debug(f"  Autocomplete: dropdown appeared ({_pre}→{_cur} headings)")
                        break
                except Exception:
                    pass
                time.sleep(0.35)
            else:
                time.sleep(0.5)

            # Step 5: extract follower count from the dropdown DOM
            follower_text = self._driver.execute_script("""
                var targetName = arguments[0] || '';
                function normName(s) { return s.toLowerCase().replace(/[\s\-_]+/g, ''); }
                var tNorm = normName(targetName);

                function extractFollow(text) {
                    var m1 = text.match(/(\d[\d,\.]*\s*[KkMm]?)\s*follow(?:ers?)?(?:\s+this)?/i);
                    if (m1) return m1[0];
                    var m2 = text.match(/followers?\s*[\u00b7\xb7:\-]\s*(\d[\d,\.]*\s*[KkMm])/i);
                    if (m2) return m2[0];
                    return null;
                }

                // Pass 1: heading matching page name → walk up → extract follow count
                var headings = Array.prototype.slice.call(
                    document.querySelectorAll('[role="heading"]'));
                for (var h = 0; h < headings.length; h++) {
                    var hText = (headings[h].innerText || headings[h].textContent || '').trim();
                    if (!hText) continue;
                    var hNorm = normName(hText);
                    if (hNorm.indexOf(tNorm) === -1 && tNorm.indexOf(hNorm) === -1) continue;
                    var el = headings[h];
                    for (var d = 0; d < 8; d++) {
                        el = el.parentElement;
                        if (!el) break;
                        var res = extractFollow(el.innerText || el.textContent || '');
                        if (res) return res;
                    }
                }

                // Pass 2: shallow divs containing "follow this" + page name match
                var allDivs = Array.prototype.slice.call(document.querySelectorAll('div'));
                var best = '', bestN = 0;
                for (var d2 = 0; d2 < allDivs.length; d2++) {
                    if ((allDivs[d2].childElementCount || 0) > 3) continue;
                    var dt = (allDivs[d2].innerText || allDivs[d2].textContent || '').trim();
                    if (!dt || dt.length > 200) continue;
                    var res2 = extractFollow(dt);
                    if (!res2) continue;
                    if (tNorm && normName(dt).indexOf(tNorm) !== -1) return res2;
                    var nm = res2.match(/(\d[\d,\.]*(?:\.\d+)?)\s*([KkMm]?)/);
                    if (nm) {
                        var n = parseFloat(nm[1].replace(/,/g,'')),
                            s = nm[2].toLowerCase();
                        if (s==='k') n*=1000; if (s==='m') n*=1000000;
                        if (n > bestN) { bestN = n; best = res2; }
                    }
                }
                if (best) return best;

                // Pass 3: body text last resort
                var bt = document.body.innerText || '';
                var bm = bt.match(/(\d[\d,\.]*\s*[KkMm]?)\s*follow(?:ers?)?(?:\s+this)?/i);
                return bm ? bm[0] : '';
            """, page_name) or ""

            if follower_text:
                count = parse_follower_count(follower_text)
                logger.debug(
                    f"  Autocomplete followers {page_name!r}: "
                    f"{follower_text!r} → {count}"
                )
                return count

            logger.debug(f"  Autocomplete: no follower text found for {page_name!r}")
            return 0

        except Exception as e:
            logger.debug(f"  Autocomplete follower lookup failed ({page_name!r}): {e}")
            return 0
