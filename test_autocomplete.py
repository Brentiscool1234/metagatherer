#!/usr/bin/env python3
"""Quick test for get_followers_from_autocomplete.

Usage:
    python test_autocomplete.py
    python test_autocomplete.py "Aqua-Cats-USA"
    python test_autocomplete.py "SomePage" --headless
"""
import logging
import sys

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from fb_ads_scraper.browser import AdsLibraryBrowser

page_names = [a for a in sys.argv[1:] if not a.startswith("--")]
headless = "--headless" in sys.argv

if not page_names:
    # Default test cases — pick pages known from the FB screenshot
    page_names = ["Aqua-Cats-USA", "aquacatsusa"]

browser = AdsLibraryBrowser(countries=["US"], headless=headless)
browser.start()

try:
    for name in page_names:
        print(f"\n--- Testing: {name!r} ---")
        count = browser.get_followers_from_autocomplete(name)
        print(f"Result: {count} followers")
finally:
    browser.stop()
