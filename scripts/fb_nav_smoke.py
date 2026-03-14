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


def wait_for_settle(seconds: int = 2) -> None:
    time.sleep(seconds)


def prompt_continue(message: str) -> None:
    input(f"{message}\nPress Enter to continue...")


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
        wait_for_settle(3)
        print_location("HOME_INITIAL", driver)
        print(flush=True)

        prompt_continue(
            "Log into Facebook if needed.\n"
            "If Facebook shows a captcha, checkpoint, or verification flow, finish that now.\n"
            "Once the page is in the state you want to inspect, continue."
        )

        wait_for_settle(2)
        print_location("HOME_READY", driver)
        save_screenshot(driver, "fb_home.png")
        print("Saved screenshot: fb_home.png", flush=True)
        print(flush=True)

        print("[2] Opening known page...", flush=True)
        driver.get("https://www.facebook.com/facebook")
        wait_for_settle(4)
        print_location("KNOWN_INITIAL", driver)
        print(flush=True)

        prompt_continue(
            "Inspect the known page.\n"
            "If Facebook redirected, partially loaded, or asked for more verification, resolve that now."
        )

        wait_for_settle(2)
        print_location("KNOWN_READY", driver)
        save_screenshot(driver, "fb_known_page.png")
        print("Saved screenshot: fb_known_page.png", flush=True)
        print(flush=True)

        print("[3] Opening search route...", flush=True)
        driver.get(search_url)
        wait_for_settle(5)
        print_location("SEARCH_INITIAL", driver)
        print(flush=True)

        prompt_continue(
            "Inspect the search route.\n"
            "Wait for results, redirects, login walls, Not Found pages, or checkpoints to fully appear before continuing."
        )

        wait_for_settle(2)
        print_location("SEARCH_READY", driver)
        save_screenshot(driver, "fb_search.png")
        print("Saved screenshot: fb_search.png", flush=True)
        print(flush=True)

        print("Screenshots saved:", flush=True)
        print("fb_home.png", flush=True)
        print("fb_known_page.png", flush=True)
        print("fb_search.png", flush=True)
        print(flush=True)

        input("Final inspection complete. Press Enter to quit...")
        return 0
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())