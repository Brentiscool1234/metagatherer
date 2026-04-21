#!/usr/bin/env python3
"""Quick diagnostic test for get_followers_from_autocomplete.

Usage:
    python test_autocomplete.py
    python test_autocomplete.py "Aqua-Cats-USA"
    python test_autocomplete.py "SomePage" "AnotherPage"
    python test_autocomplete.py "SomePage" --headless
"""
import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from fb_ads_scraper.browser import AdsLibraryBrowser

page_names = [a for a in sys.argv[1:] if not a.startswith("--") and not a.startswith("--country")]
headless = "--headless" in sys.argv
country_arg = next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--country=")), "BE")

if not page_names:
    page_names = ["Aqua-Cats-USA"]

browser = AdsLibraryBrowser(countries=[country_arg], headless=headless)
print("Starting browser...")
browser.start()
print("Browser started OK — Chrome is open")

try:
    for name in page_names:
        print(f"\n{'='*60}")
        print(f"Testing: {name!r}")
        print('='*60)

        # Check session is still alive before calling
        try:
            _ = browser._driver.title
        except Exception as e:
            print(f"ERROR: Browser session is dead before test: {e}")
            break

        count = browser.get_followers_from_autocomplete(name)
        print(f"\nRESULT: {count} followers for {name!r}")

        # Small pause between lookups
        if len(page_names) > 1:
            time.sleep(1.5)

finally:
    print("\nClosing browser...")
    browser.stop()
    print("Done.")
