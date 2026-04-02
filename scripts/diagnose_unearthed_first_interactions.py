#!/usr/bin/env python3
"""Standalone probe for the first real legacy Unearthed interactions."""

from __future__ import annotations

import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


UNEARTHED_FIRST_URL = "https://www.abc.net.au/triplejunearthed/music/"
LEGACY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CARD_SELECTOR = "a.HU3iy.p1_Ju.mqDRk.FQED6.O_grP[href]"
LOAD_MORE_XPATH = '//button[contains(text(), "Load more")]'
CDP_STEALTH_SOURCE = """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """


def format_exception(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def build_options() -> Options:
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US,en")
    opts.add_argument(f"--user-agent={LEGACY_UA}")
    opts.page_load_strategy = "eager"
    return opts


def print_step_success(step_name: str, action: str, details: dict[str, object] | None = None) -> None:
    print(f"{step_name}")
    print(f"action: {action}")
    print("result: SUCCESS")
    if details:
        for key, value in details.items():
            print(f"{key}: {value}")


def print_step_failure(step_name: str, action: str, exc: Exception) -> None:
    print(f"{step_name}")
    print(f"action: {action}")
    print("result: FAILURE")
    print(f"exception: {format_exception(exc)}")


def absolute_unearthed_profile_urls(raw_hrefs: list[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href in raw_hrefs:
        if not href.startswith("/triplejunearthed/artist/"):
            continue
        absolute = f"https://www.abc.net.au{href}"
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
    return urls


def main() -> None:
    driver = None
    failing_step = None
    collected_profile_urls: list[str] = []
    all_steps_succeeded = False

    try:
        try:
            driver = webdriver.Chrome(options=build_options())
            print_step_success(
                "Step 0 - Launch Chrome",
                "Start Selenium Chrome with the proven legacy headless option bundle.",
            )
        except Exception as exc:
            failing_step = "Step 0 - Launch Chrome"
            print_step_failure(
                failing_step,
                "Start Selenium Chrome with the proven legacy headless option bundle.",
                exc,
            )

        if failing_step is None:
            try:
                driver.set_page_load_timeout(35)
                driver.set_script_timeout(35)
                print_step_success(
                    "Step 1 - Apply timeouts",
                    "Apply the same legacy page-load and script timeouts used by setup_driver().",
                )
            except Exception as exc:
                failing_step = "Step 1 - Apply timeouts"
                print_step_failure(
                    failing_step,
                    "Apply the same legacy page-load and script timeouts used by setup_driver().",
                    exc,
                )

        if failing_step is None:
            try:
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": CDP_STEALTH_SOURCE},
                )
                print_step_success(
                    "Step 2 - Apply CDP anti-detection script",
                    "Register the same legacy webdriver-hiding CDP script before document creation.",
                )
            except Exception as exc:
                failing_step = "Step 2 - Apply CDP anti-detection script"
                print_step_failure(
                    failing_step,
                    "Register the same legacy webdriver-hiding CDP script before document creation.",
                    exc,
                )

        if failing_step is None:
            try:
                driver.get(UNEARTHED_FIRST_URL)
                print_step_success(
                    "Step 3 - Navigate to the first Unearthed URL",
                    "Load the same initial Unearthed listing URL used in the legacy scraper.",
                    {
                        "current_url": driver.current_url,
                        "title": driver.title,
                    },
                )
            except Exception as exc:
                failing_step = "Step 3 - Navigate to the first Unearthed URL"
                print_step_failure(
                    failing_step,
                    "Load the same initial Unearthed listing URL used in the legacy scraper.",
                    exc,
                )

        if failing_step is None:
            try:
                waited_element = WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "HU3iy"))
                )
                print_step_success(
                    "Step 4 - First explicit wait",
                    "Mirror scrape_website(): wait for the first HU3iy listing element to exist.",
                    {
                        "waited_tag": waited_element.tag_name,
                        "current_url": driver.current_url,
                    },
                )
            except Exception as exc:
                failing_step = "Step 4 - First explicit wait"
                print_step_failure(
                    failing_step,
                    "Mirror scrape_website(): wait for the first HU3iy listing element to exist.",
                    exc,
                )

        if failing_step is None:
            try:
                card_elements = driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)
                raw_hrefs = [(element.get_attribute("href") or "") for element in card_elements]
                collected_profile_urls = absolute_unearthed_profile_urls(raw_hrefs)
                print_step_success(
                    "Step 5 - First listing/card collection read",
                    "Mirror the first page-level card extraction using the legacy artist-link selector.",
                    {
                        "selector_count": len(card_elements),
                        "href_count": len(collected_profile_urls),
                        "html_length": len(driver.page_source or ""),
                    },
                )
            except Exception as exc:
                failing_step = "Step 5 - First listing/card collection read"
                print_step_failure(
                    failing_step,
                    "Mirror the first page-level card extraction using the legacy artist-link selector.",
                    exc,
                )

        if failing_step is None:
            try:
                first_profile_url = collected_profile_urls[0] if collected_profile_urls else ""
                print_step_success(
                    "Step 6 - First detail-page href extraction",
                    "Mirror the first profile URL normalization from collected artist links.",
                    {
                        "href_count": len(collected_profile_urls),
                        "first_href": first_profile_url or "(none)",
                    },
                )
            except Exception as exc:
                failing_step = "Step 6 - First detail-page href extraction"
                print_step_failure(
                    failing_step,
                    "Mirror the first profile URL normalization from collected artist links.",
                    exc,
                )

        if failing_step is None:
            try:
                load_more_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, LOAD_MORE_XPATH))
                )
                print_step_success(
                    "Step 7 - First pagination lookup",
                    "Mirror scrape_website(): wait for the first clickable 'Load more' button.",
                    {
                        "button_text": load_more_button.text.strip() or "(blank)",
                        "current_url": driver.current_url,
                    },
                )
            except Exception as exc:
                failing_step = "Step 7 - First pagination lookup"
                print_step_failure(
                    failing_step,
                    "Mirror scrape_website(): wait for the first clickable 'Load more' button.",
                    exc,
                )

        if failing_step is None:
            try:
                load_more_button = driver.find_element(By.XPATH, LOAD_MORE_XPATH)
                load_more_button.click()
                print_step_success(
                    "Step 8 - First pagination click",
                    "Mirror scrape_website(): click the first 'Load more' button.",
                    {
                        "current_url": driver.current_url,
                        "title": driver.title,
                    },
                )
            except Exception as exc:
                failing_step = "Step 8 - First pagination click"
                print_step_failure(
                    failing_step,
                    "Mirror scrape_website(): click the first 'Load more' button.",
                    exc,
                )

        if failing_step is None:
            try:
                time.sleep(3.0)
                post_click_cards = driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)
                print_step_success(
                    "Step 9 - First post-pagination read",
                    "Mirror the immediate post-click settle/read boundary after the legacy load-more action.",
                    {
                        "selector_count": len(post_click_cards),
                        "current_url": driver.current_url,
                        "title": driver.title,
                        "html_length": len(driver.page_source or ""),
                    },
                )
                all_steps_succeeded = True
            except Exception as exc:
                failing_step = "Step 9 - First post-pagination read"
                print_step_failure(
                    failing_step,
                    "Mirror the immediate post-click settle/read boundary after the legacy load-more action.",
                    exc,
                )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    if all_steps_succeeded:
        print("CONCLUSION: no early interaction failure reproduced")
    elif failing_step is not None:
        print(f"CONCLUSION: first failing step = {failing_step}")


if __name__ == "__main__":
    main()
