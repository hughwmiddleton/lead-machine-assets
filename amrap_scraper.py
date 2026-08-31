"""Focused AMRAP V1 public-directory scraper.

Discovery surface:
- Directory: https://amrap.org.au/api/search/artist?page={N}
- Profile:   https://amrap.org.au/api/profile/artist/{slug}
- Canonical public profile URL: https://amrap.org.au/artist/{slug}

Privacy rules enforced:
- phone and contact fields from the API are NEVER ingested.
- identity/demographic tags are NEVER ingested.
- Email is left blank; enrichment system discovers it later.
"""

from __future__ import annotations

import csv
import datetime
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

AMRAP_DIRECTORY_URL = "https://amrap.org.au/api/search/artist"
AMRAP_PROFILE_URL_TEMPLATE = "https://amrap.org.au/api/profile/artist/{slug}"
AMRAP_PUBLIC_PROFILE_URL_TEMPLATE = "https://amrap.org.au/artist/{slug}"
AMRAP_PAGE_SIZE = 18

# Australian state abbreviations exposed by AMRAP public directory
AMRAP_STATES = {"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"}

_LOGGER = logging.getLogger(__name__)


def _make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
    )
    return session


def extract_state_from_location(location_str: str) -> str:
    """Return the state abbreviation from an AMRAP location.as_string value.

    Examples:
        "NSW" -> "NSW"
        "NSW, Sydney" -> "NSW"
        "WA, Perth" -> "WA"
        "" -> ""
    """
    if not location_str:
        return ""
    parts = [p.strip() for p in str(location_str).split(",")]
    if not parts:
        return ""
    candidate = parts[0].upper()
    return candidate if candidate in AMRAP_STATES else ""


def parse_amrap_profile(profile_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse public AMRAP profile JSON into a canonical Lead Machine row dict.

    Returns None for non-public profiles so they can be skipped.
    """
    if not isinstance(profile_data, dict):
        return None

    visibility = str(profile_data.get("visibility") or "").strip().lower()
    if visibility != "public":
        return None

    slug = str(profile_data.get("slug") or "").strip()
    name = str(profile_data.get("name") or "").strip()
    if not slug or not name:
        return None

    # Location / state
    location_obj = profile_data.get("location") or {}
    location_str = str(location_obj.get("as_string") or "").strip()
    state = extract_state_from_location(location_str)

    # Genres (ignore identity/demographic tags)
    genres = [g["name"] for g in profile_data.get("genres", []) if isinstance(g, dict) and g.get("type") == "genre"]
    primary_genre = genres[0] if genres else ""

    # Links: websites + socials + music_accounts
    links: List[str] = []
    for key in ("websites", "socials", "music_accounts"):
        for item in profile_data.get(key, []):
            if isinstance(item, dict) and item.get("link"):
                links.append(str(item["link"]).strip())
    social_link = "; ".join(links)

    canonical_url = AMRAP_PUBLIC_PROFILE_URL_TEMPLATE.format(slug=slug)
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    return {
        "Artist Name": name,
        "Location": location_str,
        "Primary Genre": primary_genre,
        "Social Link": social_link,
        "Source URL": canonical_url,
        "Source_URL": canonical_url,
        "Lead_Source": "AMRAP",
        "Source_Directory": "amrap",
        "Source Directory": "AMRAP",
        "Date Added": current_date,
        # Fields left blank for downstream enrichment
        "Song Title": "",
        "Sounds Like": "",
        "SoundCloud Link": "",
        "Release Date": "",
        "External Links": "",
        "Email": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
    }


def fetch_directory_page(session: requests.Session, page: int) -> Dict[str, Any]:
    """Fetch a single page of the AMRAP public artist directory."""
    url = f"{AMRAP_DIRECTORY_URL}?page={page}"
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_profile(session: requests.Session, slug: str) -> Optional[Dict[str, Any]]:
    """Fetch public profile JSON for a single artist by slug."""
    url = AMRAP_PROFILE_URL_TEMPLATE.format(slug=slug)
    response = session.get(url, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _passes_filters(row: Dict[str, Any], state_filter: str, genre_filter: str) -> bool:
    """Client-side state/genre filter applied after profile enrichment."""
    if state_filter:
        loc = str(row.get("Location") or "").upper()
        if state_filter.upper() not in loc:
            return False
    if genre_filter:
        genre = str(row.get("Primary Genre") or "").lower()
        if genre_filter.lower() not in genre:
            return False
    return True


def scrape_amrap(
    target_count: int,
    state_filter: str = "",
    genre_filter: str = "",
    existing_csv: str = "",
    sleep_between_requests: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """Scrape AMRAP public directory up to *target_count* artists.

    Parameters
    ----------
    target_count:
        Maximum number of artists to return.
    state_filter:
        Optional state abbreviation (e.g. "NSW") to keep only matching artists.
    genre_filter:
        Optional genre substring (e.g. "rock") to keep only matching artists.
    existing_csv:
        Path to an existing CSV to append to (used for deduplication).
    sleep_between_requests:
        Seconds to sleep between profile requests.
    logger:
        Optional log callable.

    Returns
    -------
    List of row dicts ready for the Lead Machine pipeline.
    """
    log = logger or _LOGGER.info

    # Load existing slugs for deduplication
    seen_slugs: set = set()
    if existing_csv and os.path.exists(existing_csv):
        try:
            with open(existing_csv, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    source_url = str(row.get("Source URL") or row.get("Source_URL") or "").strip()
                    if source_url:
                        # Extract slug from canonical URL
                        slug = source_url.rstrip("/").split("/")[-1]
                        if slug:
                            seen_slugs.add(slug)
        except Exception as exc:
            log(f"[AMRAP] Warning: could not read existing CSV for dedupe: {exc}")

    session = _make_session()
    results: List[Dict[str, Any]] = []
    page = 1

    while len(results) < target_count:
        log(f"[AMRAP] Fetching directory page {page}")
        try:
            directory_data = fetch_directory_page(session, page)
        except requests.RequestException as exc:
            log(f"[AMRAP] Directory page {page} failed: {exc}")
            break

        artists = directory_data.get("data", [])
        if not artists:
            log("[AMRAP] No more artists in directory.")
            break

        for artist_summary in artists:
            if len(results) >= target_count:
                break

            slug = str(artist_summary.get("slug") or "").strip()
            if not slug or slug in seen_slugs:
                continue

            time.sleep(sleep_between_requests)

            try:
                profile_json = fetch_profile(session, slug)
            except requests.RequestException as exc:
                log(f"[AMRAP] Profile fetch failed for {slug}: {exc}")
                continue

            if not profile_json or not isinstance(profile_json, dict):
                continue

            profile_data = profile_json.get("data")
            if not profile_data:
                continue

            row = parse_amrap_profile(profile_data)
            if row is None:
                continue

            if not _passes_filters(row, state_filter, genre_filter):
                continue

            seen_slugs.add(slug)
            results.append(row)
            log(f"[AMRAP] Accepted {row['Artist Name']} ({slug}) — count {len(results)}/{target_count}")

        # Pagination guard
        meta = directory_data.get("meta", {})
        last_page = meta.get("last_page", page)
        if page >= last_page:
            log("[AMRAP] Reached last directory page.")
            break
        page += 1

    log(f"[AMRAP] Scraping complete. {len(results)} artists collected.")
    return results


def scrape_amrap_to_csv(
    target_count: int,
    output_csv: str,
    state_filter: str = "",
    genre_filter: str = "",
    existing_csv: str = "",
    sleep_between_requests: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
) -> str:
    """Scrape AMRAP and write rows directly to *output_csv*.

    Returns the output path.
    """
    log = logger or _LOGGER.info
    rows = scrape_amrap(
        target_count=target_count,
        state_filter=state_filter,
        genre_filter=genre_filter,
        existing_csv=existing_csv or output_csv,
        sleep_between_requests=sleep_between_requests,
        logger=logger,
    )

    # Ensure parent directory exists
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "Artist Name",
        "Location",
        "Song Title",
        "Sounds Like",
        "Social Link",
        "SoundCloud Link",
        "Release Date",
        "Primary Genre",
        "Date Added",
        "External Links",
        "Email",
        "Email_Source_URL",
        "Email_Source_Type",
        "Email_Extract_Method",
        "Lead_Source",
        "Source_Directory",
        "Source Directory",
        "Source URL",
        "Source_URL",
    ]

    # If appending to an existing file, preserve any extra columns
    extra_columns: List[str] = []
    if existing_csv and os.path.exists(existing_csv):
        try:
            with open(existing_csv, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    extra_columns = [c for c in reader.fieldnames if c not in fieldnames]
        except Exception:
            pass

    all_columns = fieldnames + extra_columns
    write_mode = "a" if existing_csv and os.path.exists(existing_csv) else "w"
    write_header = not (existing_csv and os.path.exists(existing_csv))

    with open(output_csv, write_mode, encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)

    log(f"[AMRAP] Wrote {len(rows)} rows to {output_csv}")
    return output_csv
