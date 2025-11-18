"""Spotify About tab scraper for artist social links.

Phase 3 adds an optional enrichment step that pulls basic social links from
the Spotify artist "About" page so that later phases (email scraping, social
automation) have richer context.
"""
from __future__ import annotations

import contextlib
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

try:  # Playwright (preferred)
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional dependency
    PlaywrightTimeoutError = Exception  # type: ignore
    sync_playwright = None  # type: ignore

try:  # Selenium fallback when Playwright is unavailable
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:  # pragma: no cover - optional dependency
    webdriver = None  # type: ignore
    ChromeOptions = ChromeService = By = EC = WebDriverWait = ChromeDriverManager = None  # type: ignore


Row = Dict[str, str]
LoggerFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

SOCIAL_FIELDS = (
    "Spotify_Instagram_URL",
    "Spotify_Facebook_URL",
    "Spotify_Twitter_URL",
    "Spotify_Website_URL",
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _log(logger: Optional[LoggerFn], message: str) -> None:
    if not logger or not message:
        return
    try:
        logger(message)
    except Exception:
        pass


def extract_social_links_from_page(page_html: str) -> Dict[str, str]:
    """Return detected social URLs from the About page HTML."""
    results = {
        "instagram": "",
        "facebook": "",
        "twitter": "",
        "website": "",
    }
    if not page_html:
        return results

    soup = BeautifulSoup(page_html, "html.parser")
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("javascript:"):
            continue
        lowered = href.lower()
        parsed = urlparse(href)
        domain = (parsed.netloc or "").lower()
        if not domain:
            continue
        # Normalize to avoid duplicate query params across buttons.
        normalized = href.split("?")[0]

        if "instagram.com" in domain or "instagr.am" in domain:
            if not results["instagram"]:
                results["instagram"] = normalized
            continue
        if "facebook.com" in domain:
            if not results["facebook"]:
                results["facebook"] = normalized
            continue
        if "twitter.com" in domain or "x.com" in domain:
            if not results["twitter"]:
                results["twitter"] = normalized
            continue
        if "spotify.com" in domain or domain.endswith("scdn.co"):
            continue
        if lowered.startswith("mailto:"):
            continue
        if not results["website"]:
            results["website"] = normalized

    return results


def enrich_spotify_rows_with_about_links(
    rows: List[Row],
    logger: Optional[LoggerFn] = None,
    progress_callback: Optional[ProgressFn] = None,
) -> List[Row]:
    """Visit the Spotify About tab for each artist and extract social links."""

    if not rows:
        return rows

    if sync_playwright is not None:
        try:
            return _enrich_with_playwright(rows, logger, progress_callback)
        except Exception as exc:  # pragma: no cover - defensive
            _log(logger, f"[Spotify About] Playwright scraping failed: {exc}")

    if webdriver is not None:
        try:
            return _enrich_with_selenium(rows, logger, progress_callback)
        except Exception as exc:  # pragma: no cover - defensive
            _log(logger, f"[Spotify About] Selenium scraping failed: {exc}")

    _log(logger, "[Spotify About] No supported browser automation backend available; skipping.")
    return rows


def _enrich_with_playwright(
    rows: List[Row],
    logger: Optional[LoggerFn],
    progress_callback: Optional[ProgressFn],
) -> List[Row]:
    total = len(rows)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US", viewport={"width": 1280, "height": 720})
        page = context.new_page()
        try:
            for idx, row in enumerate(rows, start=1):
                _populate_defaults(row)
                artist_id = _resolve_artist_id(row)
                if not artist_id:
                    _log(logger, "[Spotify About] Missing artist ID; skipping row.")
                    _apply_socials(row, {})
                    _emit_progress(progress_callback, idx, total)
                    continue
                about_url = f"https://open.spotify.com/artist/{artist_id}/about"
                try:
                    page.goto(about_url, wait_until="domcontentloaded", timeout=20000)
                    with contextlib.suppress(PlaywrightTimeoutError):
                        page.wait_for_selector('text="About"', timeout=8000)
                    page.wait_for_timeout(1200)
                    html = page.content()
                    links = extract_social_links_from_page(html)
                    _apply_socials(row, links)
                except PlaywrightTimeoutError:
                    _log(logger, f"[Spotify About] Timeout while loading {about_url}")
                    _apply_socials(row, {})
                except Exception as exc:  # pragma: no cover - defensive
                    _log(logger, f"[Spotify About] Error scraping {about_url}: {exc}")
                    _apply_socials(row, {})
                _emit_progress(progress_callback, idx, total)
        finally:
            context.close()
            browser.close()
    return rows


def _enrich_with_selenium(
    rows: List[Row],
    logger: Optional[LoggerFn],
    progress_callback: Optional[ProgressFn],
) -> List[Row]:
    if webdriver is None:
        return rows
    total = len(rows)
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument(f"--user-agent={USER_AGENT}")
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    try:
        for idx, row in enumerate(rows, start=1):
            _populate_defaults(row)
            artist_id = _resolve_artist_id(row)
            if not artist_id:
                _log(logger, "[Spotify About] Missing artist ID; skipping row.")
                _apply_socials(row, {})
                _emit_progress(progress_callback, idx, total)
                continue
            about_url = f"https://open.spotify.com/artist/{artist_id}/about"
            try:
                driver.get(about_url)
                WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(1.0)
                html = driver.page_source
                links = extract_social_links_from_page(html)
                _apply_socials(row, links)
            except Exception as exc:
                _log(logger, f"[Spotify About] Selenium error on {about_url}: {exc}")
                _apply_socials(row, {})
            _emit_progress(progress_callback, idx, total)
    finally:
        driver.quit()
    return rows


def _populate_defaults(row: Row) -> None:
    for key in SOCIAL_FIELDS:
        row.setdefault(key, "")
    row.setdefault("Social Link", "")


def _resolve_artist_id(row: Row) -> str:
    artist_id = (row.get("Spotify_Artist_ID") or "").strip()
    if artist_id:
        return artist_id
    url = (row.get("Spotify_URL") or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "artist":
        return path_parts[1]
    return ""


def _apply_socials(row: Row, data: Dict[str, str]) -> None:
    instagram = data.get("instagram", "")
    facebook = data.get("facebook", "")
    twitter = data.get("twitter", "")
    website = data.get("website", "")

    row["Spotify_Instagram_URL"] = instagram
    row["Spotify_Facebook_URL"] = facebook
    row["Spotify_Twitter_URL"] = twitter
    row["Spotify_Website_URL"] = website

    primary_social = instagram or facebook or twitter or website
    if primary_social:
        row["Social Link"] = primary_social


def _emit_progress(callback: Optional[ProgressFn], current: int, total: int) -> None:
    if not callback:
        return
    try:
        callback(current, total)
    except Exception:
        pass
