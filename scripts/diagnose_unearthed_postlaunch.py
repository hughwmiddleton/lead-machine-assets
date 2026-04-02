#!/usr/bin/env python3
"""Standalone probe for the legacy Unearthed post-launch crash boundary."""

from __future__ import annotations

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


UNEARTHED_FIRST_URL = "https://www.abc.net.au/triplejunearthed/music/"
LEGACY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CDP_STEALTH_SOURCE = """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """


def format_exception(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def print_step_success(step_name: str) -> None:
    print(f"{step_name}: SUCCESS")


def print_step_failure(step_name: str, exc: Exception) -> None:
    print(f"{step_name}: FAILURE")
    print(f"exception: {format_exception(exc)}")


def build_options() -> Options:
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--headless")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US,en")
    opts.add_argument(f"--user-agent={LEGACY_UA}")
    opts.page_load_strategy = "eager"
    return opts


def main() -> None:
    driver = None
    failing_step = None
    all_steps_succeeded = False

    try:
        try:
            driver = webdriver.Chrome(options=build_options())
            print_step_success("Step 0 - Launch Chrome")
        except Exception as exc:
            failing_step = "Step 0 - Launch Chrome"
            print_step_failure(failing_step, exc)

        if failing_step is None:
            try:
                driver.set_page_load_timeout(35)
                driver.set_script_timeout(35)
                print_step_success("Step 1 - Apply timeouts")
            except Exception as exc:
                failing_step = "Step 1 - Apply timeouts"
                print_step_failure(failing_step, exc)

        if failing_step is None:
            try:
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": CDP_STEALTH_SOURCE},
                )
                print_step_success("Step 2 - Apply CDP anti-detection script")
            except Exception as exc:
                failing_step = "Step 2 - Apply CDP anti-detection script"
                print_step_failure(failing_step, exc)

        if failing_step is None:
            try:
                driver.get(UNEARTHED_FIRST_URL)
                print_step_success("Step 3 - Navigate to first Unearthed URL")
            except Exception as exc:
                failing_step = "Step 3 - Navigate to first Unearthed URL"
                print_step_failure(failing_step, exc)

        if failing_step is None:
            try:
                page_source = driver.page_source or ""
                body_text = driver.find_element("tag name", "body").text if page_source else ""
                print_step_success("Step 4 - Tiny sanity read")
                print(f"current_url: {driver.current_url}")
                print(f"title: {driver.title}")
                print(f"body_text_length: {len(body_text)}")
                print(f"html_length: {len(page_source)}")
                all_steps_succeeded = True
            except Exception as exc:
                failing_step = "Step 4 - Tiny sanity read"
                print_step_failure(failing_step, exc)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    if all_steps_succeeded:
        print("CONCLUSION: no post-launch failure reproduced")
    elif failing_step is not None:
        print(f"CONCLUSION: first failing step = {failing_step}")


if __name__ == "__main__":
    main()
