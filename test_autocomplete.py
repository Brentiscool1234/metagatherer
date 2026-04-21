#!/usr/bin/env python3
"""Quick diagnostic test for get_followers_from_autocomplete.

Usage:
    python test_autocomplete.py
    python test_autocomplete.py "Aqua-Cats-USA"
    python test_autocomplete.py "SomePage" --headless
"""
import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from selenium.webdriver.common.by import By
from fb_ads_scraper.browser import AdsLibraryBrowser, ADS_LIBRARY_BASE, parse_follower_count

page_names = [a for a in sys.argv[1:] if not a.startswith("--")]
headless = "--headless" in sys.argv

if not page_names:
    page_names = ["Aqua-Cats-USA"]

browser = AdsLibraryBrowser(countries=["US"], headless=headless)
browser.start()

try:
    for name in page_names:
        print(f"\n{'='*60}")
        print(f"Testing: {name!r}")
        print('='*60)

        driver = browser._driver

        # Navigate to Ads Library home
        driver.get(ADS_LIBRARY_BASE)
        time.sleep(2.0)
        browser._dismiss_dialogs()

        print(f"Page title: {driver.title!r}")
        print(f"Current URL: {driver.current_url}")

        # Find search box
        search_box = None
        for selector in [
            'input[placeholder*="Search ads"]',
            'input[placeholder*="Search"]',
            'input[type="search"]',
            'input[aria-label*="Search"]',
            'input[data-testid*="search"]',
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    search_box = el
                    print(f"Found search box via: {selector!r}")
                    break
            if search_box:
                break

        if not search_box:
            # XPath fallback
            els = driver.find_elements(By.XPATH, "//input[@type='text' or @type='search']")
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    search_box = el
                    print("Found search box via XPath fallback")
                    break

        if not search_box:
            print("ERROR: search box NOT found")
            # Dump all input elements
            all_inputs = driver.find_elements(By.TAG_NAME, "input")
            print(f"  All inputs on page: {len(all_inputs)}")
            for inp in all_inputs[:10]:
                try:
                    print(f"    type={inp.get_attribute('type')!r} "
                          f"placeholder={inp.get_attribute('placeholder')!r} "
                          f"aria-label={inp.get_attribute('aria-label')!r} "
                          f"visible={inp.is_displayed()}")
                except Exception:
                    pass
            continue

        # Count headings before typing
        pre_count = len(driver.find_elements(By.CSS_SELECTOR, '[role="heading"]'))
        print(f"Headings before typing: {pre_count}")

        # Click + type
        try:
            search_box.click()
            time.sleep(0.4)
        except Exception as e:
            print(f"Click failed: {e}")
        search_box.clear()
        search_box.send_keys(name)
        print(f"Typed {name!r} into search box")

        # Wait for new headings
        deadline = time.time() + 5.0
        while time.time() < deadline:
            cur = len(driver.find_elements(By.CSS_SELECTOR, '[role="heading"]'))
            if cur > pre_count:
                print(f"Dropdown appeared! Headings: {pre_count} → {cur}")
                break
            time.sleep(0.35)
        else:
            cur = len(driver.find_elements(By.CSS_SELECTOR, '[role="heading"]'))
            print(f"Timeout — headings after wait: {cur} (was {pre_count})")
            time.sleep(0.5)

        # Dump all headings found
        headings = driver.find_elements(By.CSS_SELECTOR, '[role="heading"]')
        print(f"\nAll headings ({len(headings)}):")
        for h in headings:
            try:
                print(f"  [{h.get_attribute('aria-level')}] {h.text!r}")
            except Exception:
                pass

        # Dump any "follow this" text visible on page
        follow_text = driver.execute_script("""
            var all = Array.prototype.slice.call(document.querySelectorAll('*'));
            var results = [];
            for (var i = 0; i < all.length; i++) {
                var t = (all[i].childElementCount === 0 ?
                    (all[i].innerText || all[i].textContent || '') : '').trim();
                if (/follow/i.test(t) && t.length < 200) results.push(t);
            }
            return results.slice(0, 20);
        """)
        print(f"\nLeaf elements containing 'follow' ({len(follow_text or [])}):")
        for ft in (follow_text or []):
            print(f"  {ft!r}")

        # Run the actual function
        print(f"\n--- get_followers_from_autocomplete({name!r}) ---")
        count = browser.get_followers_from_autocomplete(name)
        print(f"RESULT: {count} followers")

finally:
    browser.stop()
