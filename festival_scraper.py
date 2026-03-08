"""Festival lineup seed scraper for Lead Machine.

Fetches a single configured lineup page per festival, extracts artist names via
static CSS selectors, and returns row dicts that fit the existing raw seed
pipeline.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from html_fetcher import fetch_html

Row = Dict[str, str]
LoggerFn = Optional[Callable[[str], None]]

DATE_FORMAT = "%Y-%m-%d"


FESTIVAL_CONFIG: Dict[str, Dict[str, str]] = {
    "bigsound": {
        "url": "https://www.bigsound.org.au/artists",
        "selector": ".artist-card h3",
    },
    "sxsw": {
        "url": "https://www.sxsw.com/music/festival-lineup",
        "selector": ".lineup-item h3",
    },
    "great_escape": {
        "url": "https://greatescapefestival.com/artists",
        "selector": ".artist-card h3",
    },
    "laneway": {
        "url": "https://lanewayfestival.com/lineup",
        "selector": ".lineup-card h3",
    },
}


def _log(logger: LoggerFn, message: str) -> None:
    if not logger or not message:
        return
    try:
        logger(message)
    except Exception:
        pass


def _split_requested_sources(raw_value: Any) -> List[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        pieces = re.split(r"[\n,]+", raw_value)
        return [piece.strip() for piece in pieces if piece and piece.strip()]
    if isinstance(raw_value, (list, tuple, set)):
        resolved: List[str] = []
        for entry in raw_value:
            resolved.extend(_split_requested_sources(entry))
        return resolved
    return [str(raw_value).strip()] if str(raw_value).strip() else []


def _resolve_source_keys(params: Dict[str, Any]) -> List[str]:
    requested: List[str] = []
    for field in ("festival_keys", "festival_sources", "festival_source", "festival"):
        requested.extend(_split_requested_sources(params.get(field)))

    if not requested:
        requested.extend(_split_requested_sources(params.get("input_seed_csv")))

    if not requested:
        return list(FESTIVAL_CONFIG.keys())

    url_to_key = {cfg.get("url", "").rstrip("/"): key for key, cfg in FESTIVAL_CONFIG.items()}
    resolved: List[str] = []
    seen = set()
    for token in requested:
        lowered = token.strip().lower()
        if not lowered:
            continue
        key = lowered
        if key not in FESTIVAL_CONFIG:
            key = url_to_key.get(token.strip().rstrip("/"), "")
        if key and key not in seen:
            seen.add(key)
            resolved.append(key)
    return resolved


def _normalize_artist_name(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return cleaned


def _parse_artist_names(html: str, selector: str) -> List[str]:
    if not html or not selector:
        return []
    soup = BeautifulSoup(html, "html.parser")
    names: List[str] = []
    seen = set()
    for node in soup.select(selector):
        name = _normalize_artist_name(node.get_text(" ", strip=True))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _build_row(artist_name: str, source_key: str, source_url: str, timestamp: str) -> Row:
    return {
        "Artist Name": artist_name,
        "Location": "",
        "Song Title": "",
        "Sounds Like": "",
        "Social Link": "",
        "SoundCloud Link": "",
        "Played on triple J": "",
        "Played on Unearthed": "",
        "Release Date": "",
        "Primary Genre": "",
        "Date Added": timestamp,
        "External Links": "",
        "Email": "",
        "Source Directory": f"festival_{source_key}",
        "Source URL": source_url,
    }


def scrape_festivals(
    max_artists: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
    logger: LoggerFn = None,
) -> List[Row]:
    params = dict(params or {})
    source_keys = _resolve_source_keys(params)
    if not source_keys:
        _log(logger, "[FestivalSeed] No valid festival sources configured.")
        return []

    try:
        cap = int(max_artists) if max_artists is not None else 0
    except Exception:
        cap = 0
    cap = max(cap, 0)

    rows: List[Row] = []
    timestamp = time.strftime(DATE_FORMAT)

    for source_key in source_keys:
        cfg = FESTIVAL_CONFIG.get(source_key)
        if not cfg:
            _log(logger, f"[FestivalSeed] Unknown source skipped: {source_key}")
            continue

        source_url = cfg.get("url", "").strip()
        selector = cfg.get("selector", "").strip()
        if not source_url or not selector:
            _log(logger, f"[FestivalSeed] {source_key} skipped: incomplete config")
            continue

        try:
            result = fetch_html(
                source_url,
                directory="festival",
                required_selectors=[selector],
                allow_browser_fallback=True,
                timeout_s=20,
            )
        except Exception as exc:
            _log(logger, f"[FestivalSeed] {source_key} failed: {exc}")
            continue

        html = str(result.get("html") or "")
        if not html:
            reason = str(result.get("reason") or "empty_html")
            _log(logger, f"[FestivalSeed] {source_key} failed: {reason}")
            continue

        artist_names = _parse_artist_names(html, selector)
        _log(logger, f"[FestivalSeed] {source_key} -> {len(artist_names)} artists")
        for artist_name in artist_names:
            rows.append(_build_row(artist_name, source_key, source_url, timestamp))
            if cap and len(rows) >= cap:
                _log(logger, f"[FestivalSeed] total -> {len(rows)} rows")
                return rows

    _log(logger, f"[FestivalSeed] total -> {len(rows)} rows")
    return rows


__all__ = ["FESTIVAL_CONFIG", "scrape_festivals"]
