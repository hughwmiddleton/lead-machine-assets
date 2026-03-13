#!/usr/bin/env python3
# Example:
# cd lead-machine-assets
# NIGHT_FB_PROFILE_DIR="/Users/.../night_fb_profile" \
# FB_TEST_QUERY="Lizzie Reid" \
# python3 scripts/fb_nav_smoke.py

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def build_driver(profile_dir: Path) -> webdriver.Chrome:
    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    return webdriver.Chrome(options=options)


def save_screenshot(driver: webdriver.Chrome, filename: str) -> None:
    screenshot_path = Path.cwd() / filename
    driver.save_screenshot(str(screenshot_path))


def print_location(label: str, driver: webdriver.Chrome) -> None:
    print(f"{label} URL: {driver.current_url}", flush=True)
    print(f"{label} TITLE: {driver.title}", flush=True)


def print_search_location(driver: webdriver.Chrome) -> None:
    print(f"SEARCH URL AFTER LOAD: {driver.current_url}", flush=True)
    print(f"SEARCH TITLE: {driver.title}", flush=True)


def wait_for_manual_auth(driver: webdriver.Chrome) -> None:
    print(
        "Log into Facebook / complete checkpoint if needed, then press Enter to continue...",
        flush=True,
    )
    input()
    print_location("POST-AUTH", driver)
    has_auth_cookie = any(cookie.get("name") == "c_user" for cookie in driver.get_cookies())
    print(f"AUTH COOKIE PRESENT: {'yes' if has_auth_cookie else 'no'}", flush=True)
    print(flush=True)


def main() -> int:
    profile_dir_raw = os.environ.get("NIGHT_FB_PROFILE_DIR", "").strip()
    if not profile_dir_raw:
        print("NIGHT_FB_PROFILE_DIR is required.", file=sys.stderr, flush=True)
        return 1

    profile_dir = Path(profile_dir_raw).expanduser()
    if not profile_dir.exists():
        print(f"NIGHT_FB_PROFILE_DIR does not exist: {profile_dir}", file=sys.stderr, flush=True)
        return 1

    query = os.environ.get("FB_TEST_QUERY", "Lizzie Reid")
    search_url = f"https://www.facebook.com/search/pages?q={quote_plus(query)}"

    driver: webdriver.Chrome | None = None
    try:
        driver = build_driver(profile_dir)

        print("[1] Opening Facebook home...", flush=True)
        driver.get("https://www.facebook.com/")
        time.sleep(8)
        print_location("HOME", driver)
        save_screenshot(driver, "fb_home.png")
        print(flush=True)
        wait_for_manual_auth(driver)

        print("[2] Opening known page...", flush=True)
        driver.get("https://www.facebook.com/facebook")
        time.sleep(6)
        print_location("KNOWN", driver)
        save_screenshot(driver, "fb_known_page.png")
        print(flush=True)

        print("[3] Opening search route...", flush=True)
        driver.get(search_url)
        time.sleep(10)
        print_search_location(driver)
        save_screenshot(driver, "fb_search.png")
        print(flush=True)

        print("Screenshots saved:", flush=True)
        print("fb_home.png", flush=True)
        print("fb_known_page.png", flush=True)
        print("fb_search.png", flush=True)
        print(flush=True)

        input("Inspect browser, then press Enter to quit...")
        return 0
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
