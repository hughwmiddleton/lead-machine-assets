#!/usr/bin/env python3
"""Manual verification harness for Facebook rendered-text email capture."""

from __future__ import annotations

import sys
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from night_mode_fb import (  # noqa: E402
    EMAIL_REGEX,
    _create_fb_driver_night_mode,
    _extract_rendered_visible_text_from_driver,
)


TARGET_URL = "https://www.facebook.com/nightlightau"


def main() -> int:
    driver = None
    try:
        driver = _create_fb_driver_night_mode(headless=False, logger=print)
        driver.get(TARGET_URL)
        WebDriverWait(driver, 20).until(
            lambda drv: drv.execute_script("return document.readyState") in ("interactive", "complete")
        )
        WebDriverWait(driver, 20).until(lambda drv: drv.find_element(By.TAG_NAME, "body"))

        rendered_text = _extract_rendered_visible_text_from_driver(driver)
        match = EMAIL_REGEX.search(rendered_text or "")
        preview = " ".join((rendered_text or "").split())[:500]

        print(f"current_url: {driver.current_url}")
        print(f"rendered_text_preview: {preview}")
        print(f"email_matched: {bool(match)}")
        print(f"matched_email: {match.group(0) if match else ''}")
        return 0
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
