#!/usr/bin/env python3
"""Quick test for follower extraction.

Usage:
    python test_autocomplete.py PAGE_ID
    python test_autocomplete.py 939088612625692
    python test_autocomplete.py 939088612625692 --country=BE
"""
import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from fb_ads_scraper.browser import AdsLibraryBrowser, parse_follower_count

args = [a for a in sys.argv[1:] if not a.startswith("--")]
headless = "--headless" in sys.argv
country = next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--country=")), "BE")

page_ids = args if args else ["939088612625692"]

browser = AdsLibraryBrowser(countries=[country], headless=headless)
print("Starting browser...")
browser.start()
print("Browser started OK")

try:
    for pid in page_ids:
        print(f"\n{'='*60}")
        print(f"Testing page_id: {pid!r}")
        print('='*60)

        driver = browser._driver

        # Test 1: profile.php page
        print("\n--- Test: facebook.com/profile.php?id=... ---")
        url = f"https://www.facebook.com/profile.php?id={pid}"
        driver.get(url)
        time.sleep(2.5)
        browser._dismiss_dialogs()
        print(f"Title: {driver.title!r}")

        # Run the same extraction JS used in get_page_ads
        from fb_ads_scraper.browser import AdsLibraryBrowser
        result = driver.execute_script(r"""
            // Strategy 0: JSON script blobs
            var scripts = Array.prototype.slice.call(document.querySelectorAll('script'));
            for (var si = 0; si < scripts.length; si++) {
                var sc = scripts[si].textContent || '';
                if (sc.length < 20 || sc.length > 3000000) continue;
                var mfc = sc.match(/"fan_count"\s*:\s*(\d+)/);
                if (mfc && parseInt(mfc[1]) > 0) return 'json:fan_count=' + mfc[1];
                var mfl = sc.match(/"follower_count"\s*:\s*(\d+)/);
                if (mfl && parseInt(mfl[1]) > 0) return 'json:follower_count=' + mfl[1];
                var msc2 = sc.match(/"subscribers_count"\s*:\s*(\d+)/);
                if (msc2 && parseInt(msc2[1]) > 0) return 'json:subscribers_count=' + msc2[1];
            }
            // Strategy 1: DOM text
            var t = document.body.innerText || '';
            var m = t.match(/(\d[\d,.]*\s*[KkMm]?)\s*(followers?|follow this|people follow|likes?)/i);
            if (m) return 'text:' + m[0];
            // Strategy 2: aria-labels
            var metas = Array.prototype.slice.call(document.querySelectorAll('[aria-label]'));
            for (var j = 0; j < metas.length; j++) {
                var al = metas[j].getAttribute('aria-label') || '';
                var fm = al.match(/(\d[\d,.]*\s*[KkMm]?)\s*(follow(?:ers?)?|likes?)/i);
                if (fm) return 'aria:' + fm[0];
            }
            return 'NOTHING FOUND - body snippet: ' + t.slice(0, 200);
        """)
        print(f"Extraction result: {result!r}")
        count = parse_follower_count(str(result)) if result and ':' not in str(result)[:5] else 0
        print(f"Parsed count: {count}")

finally:
    print("\nClosing browser...")
    browser.stop()
    print("Done.")
