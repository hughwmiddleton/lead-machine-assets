#!/usr/bin/env python3
# 1) Launch Chrome manually:
# /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
#   --remote-debugging-port=9222 \
#   --user-data-dir="/Users/.../night_fb_profile" \
#   --profile-directory=Default
#
# 2) Log into Facebook manually in that Chrome window.
#
# 3) Run:
# FB_TEST_QUERY="Lizzie Reid" python3 scripts/fb_attach_smoke.py

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.debugger_address = "127.0.0.1:9222"
    return webdriver.Chrome(options=options)


def save_screenshot(driver: webdriver.Chrome, filename: str) -> None:
    screenshot_path = Path.cwd() / filename
    driver.save_screenshot(str(screenshot_path))


def main() -> int:
    query = os.environ.get("FB_TEST_QUERY", "Lizzie Reid")
    search_url = f"https://www.facebook.com/search/pages?q={quote_plus(query)}"

    driver: webdriver.Chrome | None = None
    try:
        driver = build_driver()

        print(f"CURRENT URL: {driver.current_url}", flush=True)
        print(f"CURRENT TITLE: {driver.title}", flush=True)

        driver.get("https://www.facebook.com/")
        time.sleep(5)
        print(f"HOME URL: {driver.current_url}", flush=True)
        print(f"HOME TITLE: {driver.title}", flush=True)
        save_screenshot(driver, "fb_attach_home.png")

        driver.get(search_url)
        time.sleep(8)
        print(f"SEARCH URL AFTER LOAD: {driver.current_url}", flush=True)
        print(f"SEARCH TITLE: {driver.title}", flush=True)
        save_screenshot(driver, "fb_attach_search.png")

        input("Inspect browser, then press Enter to quit...")
        return 0
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
