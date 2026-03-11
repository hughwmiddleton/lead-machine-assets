#!/usr/bin/env python3
"""Temporary diagnostic tool for Facebook internal page search in Selenium."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


SEARCH_BOX_SELECTORS: Tuple[Tuple[str, str], ...] = (
    (By.CSS_SELECTOR, 'input[aria-label="Search Facebook"]'),
    (By.CSS_SELECTOR, 'input[placeholder="Search Facebook"]'),
    (By.CSS_SELECTOR, 'input[type="search"]'),
    (By.CSS_SELECTOR, 'input[role="combobox"]'),
    (
        By.XPATH,
        "//input[contains(@aria-label, 'Search') or contains(@placeholder, 'Search')]",
    ),
)


def log(message: str) -> None:
    print(f"[debug-fb-search] {message}", flush=True)


def build_driver() -> Chrome:
    log("Attempting to reuse the Facebook enrichment Chrome driver setup.")
    try:
        from night_mode_fb import _create_fb_driver_night_mode  # type: ignore

        driver = _create_fb_driver_night_mode(headless=False, logger=print)
        log("Using night-mode Facebook driver with persistent Chrome profile.")
        return driver
    except Exception as exc:
        log(f"Night-mode driver setup unavailable: {exc}")

    log("Falling back to a local Chrome driver with minimal options.")
    options = ChromeOptions()
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.page_load_strategy = "eager"
    return webdriver.Chrome(options=options)


def wait_for_page_ready(driver: Chrome, timeout: float = 20.0) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def try_maximize(driver: Chrome) -> None:
    try:
        driver.maximize_window()
        log("Browser window maximized.")
    except Exception as exc:
        log(f"Could not maximize the browser window: {exc}")


def find_search_input(driver: Chrome, timeout: float = 12.0) -> Optional[WebElement]:
    for by, selector in SEARCH_BOX_SELECTORS:
        log(f"Waiting for search input using selector: {by} -> {selector}")
        try:
            return WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, selector))
            )
        except TimeoutException:
            continue
        except Exception as exc:
            log(f"Selector failed unexpectedly ({selector}): {exc}")
    return None


def type_search_query(search_input: WebElement, query: str) -> None:
    search_input.click()
    search_input.send_keys(Keys.COMMAND, "a")
    search_input.send_keys(Keys.DELETE)
    search_input.send_keys(query)
    search_input.send_keys(Keys.ENTER)


def open_homepage(driver: Chrome) -> None:
    log("Opening Facebook homepage.")
    driver.get("https://www.facebook.com")
    wait_for_page_ready(driver)
    log(f"Homepage loaded. Current URL: {driver.current_url}")


def run_search_box_flow(driver: Chrome, query: str, inspect_pause_s: float) -> None:
    log(f"Trying homepage search-box flow for query: {query!r}")
    search_input = find_search_input(driver)
    if search_input is None:
        log("Search input was not found. The selector may have changed, or Facebook may require login.")
        return

    try:
        type_search_query(search_input, query)
        log("Search submitted from the Facebook search box.")
        time.sleep(inspect_pause_s)
        wait_for_page_ready(driver, timeout=15.0)
        log(f"After search-box submission, current URL: {driver.current_url}")
    except Exception as exc:
        log(f"Search-box submission failed: {exc}")


def open_page_search_route(driver: Chrome, query: str, inspect_pause_s: float) -> None:
    search_url = f"https://www.facebook.com/search/pages/?q={quote_plus(query)}"
    log(f"Opening direct page-search route: {search_url}")
    driver.get(search_url)
    wait_for_page_ready(driver)
    log(f"Direct page-search route loaded. Current URL: {driver.current_url}")
    time.sleep(inspect_pause_s)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Temporary Selenium diagnostic for Facebook internal page search."
    )
    parser.add_argument("query", help='Artist query to test, for example: "Sade Olutola"')
    parser.add_argument(
        "--pause",
        type=float,
        default=5.0,
        help="Seconds to pause after each search step for visual inspection. Default: 5",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    driver: Optional[Chrome] = None

    log(f"Starting Facebook search diagnostic for query: {args.query!r}")
    try:
        driver = build_driver()
        try_maximize(driver)
        open_homepage(driver)
        run_search_box_flow(driver, args.query, inspect_pause_s=max(args.pause, 0.0))
        open_page_search_route(driver, args.query, inspect_pause_s=max(args.pause, 0.0))
        log("Diagnostic flow complete. Browser will stay open until ENTER is pressed.")
        input("Press ENTER to close the browser...")
        return 0
    except KeyboardInterrupt:
        log("Interrupted by user.")
        return 130
    except WebDriverException as exc:
        log(f"WebDriver error: {exc}")
        return 1
    except Exception as exc:
        log(f"Unexpected error: {exc}")
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
                log("Browser closed.")
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
