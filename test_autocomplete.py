#!/usr/bin/env python3
"""Test follower extraction by navigating to the FB page URL found in the Ads Library.

Usage:
    python test_autocomplete.py PAGE_ID
    python test_autocomplete.py 939088612625692 --country=BE
"""
import logging, sys, time
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from fb_ads_scraper.browser import AdsLibraryBrowser, parse_follower_count, ADS_LIBRARY_BASE
from urllib.parse import urlencode

args = [a for a in sys.argv[1:] if not a.startswith("--")]
headless = "--headless" in sys.argv
country = next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--country=")), "BE")
page_ids = args if args else ["939088612625692"]

browser = AdsLibraryBrowser(countries=[country], headless=headless)
print("Starting browser..."); browser.start(); print("Browser started OK")

try:
    for pid in page_ids:
        print(f"\n{'='*60}\nPage ID: {pid}\n{'='*60}")
        driver = browser._driver

        # Step 1: navigate to the Ads Library page for this advertiser
        ads_url = ADS_LIBRARY_BASE + "?" + urlencode({
            "active_status": "all", "ad_type": "all",
            "country": country, "search_type": "page",
            "view_all_page_id": pid,
        })
        print(f"\n[1] Loading Ads Library: {ads_url}")
        driver.get(ads_url)
        time.sleep(3.0)
        browser._dismiss_dialogs()
        print(f"    Title: {driver.title!r}")

        # Step 2: find the advertiser's Facebook page link
        page_url = driver.execute_script(r"""
            var skip = ['/ads/library','/help','/policies','/login',
                        '/l.php','facebook.com/ads'];
            var links = Array.prototype.slice.call(
                document.querySelectorAll('a[href*="facebook.com/"]'));
            for (var i = 0; i < links.length; i++) {
                var h = links[i].href || '';
                var bad = false;
                for (var s=0; s<skip.length; s++) {
                    if (h.indexOf(skip[s]) !== -1) { bad=true; break; }
                }
                if (bad) continue;
                var t = (links[i].textContent || '').trim();
                if (t.length >= 2 && t.length <= 80) return h;
            }
            return '';
        """) or ""
        print(f"[2] Found page URL: {page_url!r}")

        if not page_url:
            print("    No page URL found — dumping all FB links:")
            all_links = driver.execute_script("""
                return Array.prototype.slice.call(
                    document.querySelectorAll('a[href*="facebook.com/"]')
                ).map(function(a){ return {href: a.href, text: (a.textContent||'').trim().slice(0,40)}; })
                .slice(0, 20);
            """)
            for lnk in (all_links or []):
                print(f"      {lnk}")
            continue

        # Step 3: navigate to that page
        print(f"[3] Navigating to FB page...")
        driver.get(page_url)
        time.sleep(3.0)
        print(f"    Title: {driver.title!r}")
        print(f"    URL:   {driver.current_url!r}")

        # Step 4: extract followers
        result = driver.execute_script(r"""
            var scripts = Array.prototype.slice.call(document.querySelectorAll('script'));
            for (var si = 0; si < scripts.length; si++) {
                var sc = scripts[si].textContent || '';
                if (sc.length < 20 || sc.length > 3000000) continue;
                var mfc = sc.match(/"fan_count"\s*:\s*(\d+)/);
                if (mfc && parseInt(mfc[1]) > 0) return 'json:fan_count=' + mfc[1];
                var mfl = sc.match(/"follower_count"\s*:\s*(\d+)/);
                if (mfl && parseInt(mfl[1]) > 0) return 'json:follower_count=' + mfl[1];
            }
            var t = document.body.innerText || '';
            var m = t.match(/(\d[\d,.]*\s*[KkMm]?)\s*(followers?|follow this|people follow)/i);
            if (m) return 'text:' + m[0];
            return 'NOTHING - snippet: ' + t.slice(0, 300);
        """)
        print(f"[4] Extraction: {result!r}")

finally:
    print("\nClosing browser..."); browser.stop(); print("Done.")
