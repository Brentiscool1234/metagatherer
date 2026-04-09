"""
TikTok scraper — finds dropshipping product videos by keyword.

Searches TikTok's public search for each keyword discovered during the
Facebook scan.  Filters for videos with ≥ 50 k views uploaded in the last
30 days, then checks whether the creator's bio contains a Shopify store.

Session cookies are saved to tiktok_cookies.json after a successful login
so subsequent runs skip the login step automatically.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote

from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
VIEW_THRESHOLD = 50_000
DAYS_LOOKBACK  = 30
MAX_VIDEOS_PER_KEYWORD = 20   # stop after this many qualifying videos
SCROLL_ROUNDS  = 6            # scrolls to load more results per keyword
PAGE_LOAD_WAIT = 5            # seconds after navigation
LOGIN_WAIT_SECS = 300         # 5 minutes max wait for manual login
TIKTOK_COOKIES_FILE = "tiktok_cookies.json"


# ── Data class ────────────────────────────────────────────────────────────────
@dataclass
class TikTokResult:
    video_url:   str
    views:       int
    views_text:  str
    caption:     str
    username:    str
    keyword:     str
    upload_date: str = ""          # YYYY-MM-DD, empty = unknown
    bio_url:     str = ""          # website found in creator bio
    is_shopify:  bool = False
    shopify_reason: str = ""


# ── View-count parser ─────────────────────────────────────────────────────────
def parse_view_count(text: str) -> int:
    """'1.2M' → 1_200_000,  '50.3K' → 50_300,  '5,000' → 5_000."""
    if not text:
        return 0
    t = text.strip().upper().replace(",", "")
    m = re.match(r"([\d.]+)([KMB]?)", t)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    suffix = m.group(2)
    if suffix == "K":
        num *= 1_000
    elif suffix == "M":
        num *= 1_000_000
    elif suffix == "B":
        num *= 1_000_000_000
    return int(num)


# ── JavaScript injected into TikTok search results page ──────────────────────
# TikTok's class names are obfuscated and change frequently, so we rely on
# structural signals (a[href*="/video/"], nearby text, @username links).
_TIKTOK_SEARCH_JS = """
var results = [];
var seen = {};

var links = Array.prototype.slice.call(document.querySelectorAll('a[href*="/video/"]'));

links.forEach(function(link) {
    var url = (link.href || '').split('?')[0];
    if (!url || seen[url]) return;
    seen[url] = true;

    var viewsText = '';
    var caption   = '';
    var username  = '';

    // Walk up to 12 levels to find the card container
    var node = link;
    for (var i = 0; i < 12; i++) {
        if (!node.parentElement) break;
        node = node.parentElement;
        var text = node.innerText || '';

        // View count: looks like "1.2M", "50.3K", "5,000"
        if (!viewsText) {
            var vm = text.match(/\b([\d,.]+[KkMmBb]?)\s*(?:views?)\b/i);
            if (!vm) vm = text.match(/\b([\d,.]+[KkMm])\b/);  // bare number
            if (vm) viewsText = vm[1];
        }

        // Username from an @-style link
        if (!username) {
            var uLinks = Array.prototype.slice.call(node.querySelectorAll('a[href*="/@"]'));
            if (uLinks.length) {
                var um = (uLinks[0].href || '').match(/\/@([^/?#]+)/);
                if (um) username = um[1];
            }
        }

        // Caption from any element that looks like a description
        if (!caption) {
            var descEls = Array.prototype.slice.call(
                node.querySelectorAll('[class*="desc"], [class*="caption"], [class*="Caption"]')
            );
            if (descEls.length) caption = (descEls[0].textContent || '').trim().slice(0, 200);
        }
    }

    results.push({
        url:        url,
        views_text: viewsText,
        caption:    caption,
        username:   username,
    });
});

return results;
"""

# ── JavaScript to extract date + bio link from an individual video page ───────
_TIKTOK_VIDEO_JS = """
var result = {date: '', bio_url: ''};

// Upload date — TikTok embeds it in a <time> tag or as data
var timeEl = document.querySelector('time[datetime]');
if (timeEl) {
    result.date = timeEl.getAttribute('datetime') || timeEl.textContent || '';
}
if (!result.date) {
    // Some layouts put it as plain text "2025-12-25"
    var m = (document.body.innerText || '').match(/\b(202\d[-/]\d{1,2}[-/]\d{1,2})\b/);
    if (m) result.date = m[1];
}

// Creator bio link — any external link that isn't TikTok itself
var links = Array.prototype.slice.call(document.querySelectorAll('a[href]'));
for (var i = 0; i < links.length; i++) {
    var href = links[i].href || '';
    if (href.startsWith('http') &&
        href.indexOf('tiktok.com') === -1 &&
        href.indexOf('javascript') === -1 &&
        href.indexOf('mailto') === -1) {
        result.bio_url = href;
        break;
    }
}

return result;
"""


# ── Main scraper class ────────────────────────────────────────────────────────
class TikTokScraper:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._driver = None

    def start(self):
        from .browser import _make_driver
        logger.info("Starting TikTok browser...")
        self._driver = _make_driver(headless=self.headless)
        # Navigate to TikTok first so the domain is set before loading cookies
        self._driver.get("https://www.tiktok.com")
        time.sleep(2)
        self._load_cookies()
        logger.info("TikTok browser ready.")

    def stop(self):
        if self._driver:
            try:
                self._save_cookies()
                self._driver.quit()
            except Exception:
                pass
        logger.info("TikTok browser closed.")

    # ── Cookie persistence ────────────────────────────────────────────────────

    def _save_cookies(self):
        try:
            cookies = self._driver.get_cookies()
            with open(TIKTOK_COOKIES_FILE, "w") as f:
                json.dump(cookies, f)
            logger.info(f"Saved {len(cookies)} TikTok cookies → {TIKTOK_COOKIES_FILE}")
        except Exception as e:
            logger.debug(f"TikTok cookie save failed: {e}")

    def _load_cookies(self):
        if not os.path.exists(TIKTOK_COOKIES_FILE):
            return
        try:
            with open(TIKTOK_COOKIES_FILE) as f:
                cookies = json.load(f)
            loaded = 0
            for cookie in cookies:
                cookie.pop("sameSite", None)
                try:
                    self._driver.add_cookie(cookie)
                    loaded += 1
                except Exception:
                    pass
            if loaded:
                logger.info(f"Loaded {loaded} TikTok cookies — refreshing session...")
                self._driver.get("https://www.tiktok.com")
                time.sleep(2)
        except Exception as e:
            logger.debug(f"TikTok cookie load failed: {e}")

    def _is_logged_in(self) -> bool:
        """Return True if TikTok session appears active."""
        try:
            url = self._driver.current_url or ""
            source = self._driver.page_source or ""
            if "/login" in url or "/signup" in url:
                return False
            # TikTok embeds user info in the page when logged in
            logged_in_hints = [
                '"isLoginedUser":true' in source,
                'data-e2e="profile-icon"' in source,
                '"loginType"' in source,
                'uploadButton' in source,
            ]
            return any(logged_in_hints)
        except Exception:
            return False

    def wait_for_login(self) -> bool:
        """
        Navigate to the TikTok login page and wait up to 5 minutes
        for the user to complete login manually.  Saves cookies on success.
        """
        logger.info(
            "TikTok login required — opening login page in the browser window. "
            "Please log in with your TikTok account (you have 5 minutes)."
        )
        self._driver.get("https://www.tiktok.com/login")
        time.sleep(3)

        deadline = time.time() + LOGIN_WAIT_SECS
        while time.time() < deadline:
            try:
                url = self._driver.current_url or ""
                if "tiktok.com" in url and "/login" not in url and "/signup" not in url:
                    time.sleep(3)  # Let cookies settle after redirect
                    self._save_cookies()
                    logger.info("TikTok login successful — session saved.")
                    return True
            except Exception:
                pass
            time.sleep(2)

        logger.warning("TikTok login timed out after 5 minutes.")
        return False

    # ── Public API ────────────────────────────────────────────────────────────

    def search_keyword(self, keyword: str) -> list[TikTokResult]:
        """
        Search TikTok for `keyword`, return videos with ≥ 50 k views.
        Does NOT visit individual video pages (fast path).
        """
        url = f"https://www.tiktok.com/search?q={quote(keyword)}&type=0"
        results: list[TikTokResult] = []

        try:
            self._driver.get(url)
            time.sleep(PAGE_LOAD_WAIT)

            for _ in range(SCROLL_ROUNDS):
                self._driver.execute_script(
                    "window.scrollBy(0, window.innerHeight * 2.5);"
                )
                time.sleep(2.0)

            raw: list[dict] = self._driver.execute_script(_TIKTOK_SEARCH_JS) or []
            logger.debug(f"  TikTok '{keyword}': {len(raw)} cards found")

            for item in raw:
                views = parse_view_count(item.get("views_text", ""))
                if views < VIEW_THRESHOLD:
                    continue
                results.append(TikTokResult(
                    video_url=item["url"],
                    views=views,
                    views_text=item.get("views_text", ""),
                    caption=item.get("caption", ""),
                    username=item.get("username", ""),
                    keyword=keyword,
                ))
                if len(results) >= MAX_VIDEOS_PER_KEYWORD:
                    break

        except WebDriverException as e:
            logger.warning(
                f"  TikTok browser error for '{keyword}': "
                f"{e.msg[:120] if hasattr(e, 'msg') else str(e)[:120]}"
            )

        logger.info(
            f"  TikTok '{keyword}': {len(results)} videos ≥ {VIEW_THRESHOLD // 1000}k views"
        )
        return results

    def enrich_result(self, result: TikTokResult) -> TikTokResult:
        """
        Visit the video page to get upload date + creator bio link.
        Checks bio link for Shopify.  Mutates and returns the result.
        """
        from .shopify import is_shopify_store, decode_facebook_redirect

        try:
            self._driver.get(result.video_url)
            time.sleep(PAGE_LOAD_WAIT)

            data: dict = self._driver.execute_script(_TIKTOK_VIDEO_JS) or {}

            # Parse upload date
            raw_date = data.get("date", "")
            result.upload_date = _parse_tiktok_date(raw_date)

            # Bio URL
            bio_url = data.get("bio_url", "")
            if bio_url:
                bio_url = decode_facebook_redirect(bio_url)
                result.bio_url = bio_url
                result.is_shopify, result.shopify_reason = is_shopify_store(bio_url)

        except WebDriverException as e:
            logger.debug(f"  enrich_result error for {result.video_url}: {str(e)[:80]}")

        return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

def do_tiktok_login(headless: bool = False):
    """
    Standalone login helper: open a browser, load any saved cookies, and if the
    session isn't active, open the TikTok login page and wait for the user to
    log in manually.  Cookies are saved to tiktok_cookies.json on success.
    """
    scraper = TikTokScraper(headless=headless)
    scraper.start()
    try:
        if scraper._is_logged_in():
            logger.info("Already logged in to TikTok (saved session is valid).")
        else:
            scraper.wait_for_login()
    finally:
        scraper.stop()


def run_tiktok_scan(
    keywords: set[str],
    headless: bool = False,
    enrich: bool = True,
) -> list[TikTokResult]:
    """
    Scan TikTok for all `keywords`.  Returns qualifying videos sorted by views.

    `enrich=True` visits each video page to get upload date + Shopify check
    (slower but required for the 30-day filter).
    """
    scraper = TikTokScraper(headless=headless)
    scraper.start()

    # Check login status; prompt for manual login if not headless
    if not scraper._is_logged_in():
        if headless:
            logger.warning(
                "TikTok session not found (no saved cookies). "
                "Run 'python main.py --tiktok-login' first to save your session, "
                "or use the 'TikTok Login' button in the GUI."
            )
        else:
            logger.info("TikTok session not found — opening login page...")
            scraper.wait_for_login()

    all_results: list[TikTokResult] = []
    seen_urls: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)

    try:
        for kw in sorted(keywords):
            for r in scraper.search_keyword(kw):
                if r.video_url not in seen_urls:
                    seen_urls.add(r.video_url)
                    all_results.append(r)

        if enrich and all_results:
            logger.info(
                f"TikTok: enriching {len(all_results)} videos "
                f"(date + Shopify check)..."
            )
            for r in all_results:
                scraper.enrich_result(r)

        # Apply 30-day filter now that we have dates
        # Videos with unknown dates are kept (benefit of the doubt)
        filtered = []
        for r in all_results:
            if r.upload_date:
                try:
                    dt = datetime.strptime(r.upload_date, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if dt < cutoff:
                        logger.debug(
                            f"  TikTok skip {r.video_url}: "
                            f"uploaded {r.upload_date} (> {DAYS_LOOKBACK}d ago)"
                        )
                        continue
                except ValueError:
                    pass  # unknown date format → keep
            filtered.append(r)

        filtered.sort(key=lambda r: -r.views)
        logger.info(
            f"TikTok scan complete: {len(filtered)} videos "
            f"(≥{VIEW_THRESHOLD // 1000}k views, last {DAYS_LOOKBACK}d)"
        )
        return filtered

    except KeyboardInterrupt:
        logger.warning("TikTok scan interrupted.")
        return sorted(all_results, key=lambda r: -r.views)
    finally:
        scraper.stop()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_tiktok_date(raw: str) -> str:
    """Return 'YYYY-MM-DD' from various TikTok date formats, or ''."""
    if not raw:
        return ""
    raw = raw.strip()
    # ISO format: 2025-12-25T...
    m = re.match(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})", raw)
    if m:
        return m.group(1).replace("/", "-")
    # "Dec 25, 2025"
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    m2 = re.match(r"(\w{3})\w*\s+(\d{1,2}),?\s+(20\d{2})", raw, re.I)
    if m2:
        mon = months.get(m2.group(1).lower())
        if mon:
            return f"{m2.group(3)}-{mon}-{int(m2.group(2)):02d}"
    return ""
