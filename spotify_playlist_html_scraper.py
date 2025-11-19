"""HTML-based Spotify playlist scraper used as an API fallback."""
from __future__ import annotations

import contextlib
from typing import Callable, Dict, List, Optional, Set

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover - optional dependency
    PlaywrightTimeoutError = Exception  # type: ignore
    Page = None  # type: ignore
    sync_playwright = None  # type: ignore

from spotify_client import _clean_playlist_id

LoggerFn = Optional[Callable[[str], None]]
ProgressFn = Optional[Callable[[int, int], None]]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SCROLL_WAIT_MS = 750
MAX_SCROLL_ATTEMPTS = 60
STALE_SCROLL_LIMIT = 8


def _log(logger: LoggerFn, message: str) -> None:
    if not logger or not message:
        return
    try:
        logger(message)
    except Exception:
        pass


def _extract_playlist_title(page: Page) -> str:
    selectors = [
        "h1[data-testid='entityTitle']",
        "h1[data-testid='playlist-page-title']",
        "[data-testid='profile-header'] h1",
        "h1[dir='auto']",
    ]
    for selector in selectors:
        try:
            element = page.query_selector(selector)
            if element:
                text = (element.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def _parse_artist_id(artist_url: str) -> str:
    if not artist_url:
        return ""
    parts = artist_url.rstrip("/").split("/")
    if parts:
        return parts[-1]
    return ""


# Updated selectors so track rows yield reliable track names in the HTML fallback.
def _collect_artist_entries(page: Page) -> Dict[str, List[Dict[str, str]]]:
    script = """
    () => {
        const selectors = [
            "div[data-testid='tracklist-row']",
            "div[role='row'][aria-rowindex]"
        ];
        let rows = [];
        for (const selector of selectors) {
            rows = Array.from(document.querySelectorAll(selector));
            if (rows.length) {
                break;
            }
        }
        return rows.map((row) => {
            const trackLinkSelectors = [
                "a[data-testid='internal-track-link']",
                "[data-testid='tracklist-row__track-name'] a[href*='/track/']",
                "a[href*='/track/']"
            ];
            let trackName = "";
            for (const selector of trackLinkSelectors) {
                const link = row.querySelector(selector);
                if (link && link.textContent && link.textContent.trim()) {
                    trackName = link.textContent.trim();
                    break;
                }
            }
            if (!trackName) {
                const fallback = row.querySelector("[data-testid='tracklist-row__track-name']");
                if (fallback && fallback.textContent && fallback.textContent.trim()) {
                    trackName = fallback.textContent.trim();
                }
            }
            if (!trackName) {
                const fallback = row.querySelector("div[dir='auto']");
                if (fallback && fallback.textContent && fallback.textContent.trim()) {
                    trackName = fallback.textContent.trim();
                }
            }
            const artistLinks = Array.from(row.querySelectorAll("a[href*='/artist/']"));
            const artists = artistLinks.map((link) => ({
                name: (link.textContent || "").trim(),
                url: link.href || ""
            }));
            return { trackName, artists };
        });
    }
    """
    data = page.evaluate(script)
    return {
        "rows": data or [],
    }


def _merge_artist_entries(
    current_rows: List[Dict[str, str]],
    playlist_name: str,
    seen_ids: Set[str],
    results: List[Dict[str, str]],
    max_artists: int,
    progress_callback: ProgressFn,
) -> int:
    added = 0
    for row in current_rows:
        track_name = (row.get("trackName") or "").strip()
        for artist in row.get("artists") or []:
            artist_url = (artist.get("url") or "").strip()
            artist_name = (artist.get("name") or "").strip()
            if not artist_url:
                continue
            artist_id = _parse_artist_id(artist_url)
            dedupe_key = artist_id or artist_url
            if not dedupe_key or dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            artist_record = {
                "artist_name": artist_name,
                "artist_url": artist_url,
                "artist_id": artist_id,
                "track_name": track_name,
                "playlist_name": playlist_name,
            }
            results.append(artist_record)
            added += 1
            if progress_callback:
                with contextlib.suppress(Exception):
                    progress_callback(len(results), max_artists)
            if len(results) >= max_artists:
                return added
    return added


def _scroll_playlist(page: Page) -> None:
    try:
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(SCROLL_WAIT_MS)
    except Exception:
        pass


def scrape_playlist_artists_via_html(
    playlist_id: str,
    max_artists: int = 200,
    logger: LoggerFn = None,
    progress_callback: ProgressFn = None,
) -> List[Dict[str, str]]:
    """
    Use Playwright to load https://open.spotify.com/playlist/{playlist_id},
    scroll until enough tracks are loaded, and return artist dicts.
    """
    playlist_id_clean = _clean_playlist_id(playlist_id)
    if not playlist_id_clean:
        _log(logger, "[Spotify HTML] Invalid playlist ID provided.")
        return []
    if sync_playwright is None:
        _log(logger, "[Spotify HTML] Playwright is not installed.")
        return []

    playlist_url = f"https://open.spotify.com/playlist/{playlist_id_clean}"
    _log(logger, f"[Spotify HTML] Starting scrape for {playlist_url}")

    artists: List[Dict[str, str]] = []
    seen_ids: Set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        context = None
        page = None
        playlist_name = ""
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(playlist_url, wait_until="networkidle", timeout=30000)
            try:
                page.wait_for_selector("div[data-testid='tracklist-row']", timeout=20000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(2000)
            playlist_name = _extract_playlist_title(page)
            if not playlist_name:
                playlist_name = playlist_id_clean

            # Initial extraction before scrolling.
            payload = _collect_artist_entries(page)
            _merge_artist_entries(
                payload.get("rows", []),
                playlist_name,
                seen_ids,
                artists,
                max_artists,
                progress_callback,
            )

            scroll_attempts = 0
            stale_attempts = 0
            while len(artists) < max_artists and scroll_attempts < MAX_SCROLL_ATTEMPTS:
                _scroll_playlist(page)
                payload = _collect_artist_entries(page)
                before = len(artists)
                _merge_artist_entries(
                    payload.get("rows", []),
                    playlist_name,
                    seen_ids,
                    artists,
                    max_artists,
                    progress_callback,
                )
                if len(artists) == before:
                    stale_attempts += 1
                    if stale_attempts >= STALE_SCROLL_LIMIT:
                        break
                else:
                    stale_attempts = 0
                scroll_attempts += 1
        except PlaywrightTimeoutError:
            _log(logger, "[Spotify HTML] Timed out while loading playlist page.")
        except Exception as exc:  # pragma: no cover - defensive
            _log(logger, f"[Spotify HTML] Unexpected error: {exc}")
        finally:
            with contextlib.suppress(Exception):
                if context:
                    context.close()
            with contextlib.suppress(Exception):
                browser.close()

    _log(logger, f"[Spotify HTML] Extracted {len(artists)} artists via HTML.")
    return artists
