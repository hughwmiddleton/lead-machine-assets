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
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bandcamp_profile_engine import (
    PROFILE_ACCEPTED as BANDCAMP_PROFILE_ACCEPTED,
    canonicalize_bandcamp_profile_url,
    fetch_bandcamp_profile,
)
from link_surface_hygiene import (
    is_artist_link_hub_profile,
    is_artist_platform_profile,
    is_useful_artist_link,
)
from spotify_client import SpotifyClient
from spotify_latest_release import (
    SpotifyLatestReleaseEnricher,
    extract_spotify_artist_identity,
)

AMRAP_DIRECTORY_URL = "https://amrap.org.au/api/search/artist"
AMRAP_PROFILE_URL_TEMPLATE = "https://amrap.org.au/api/profile/artist/{slug}"
AMRAP_PUBLIC_PROFILE_URL_TEMPLATE = "https://amrap.org.au/artist/{slug}"
AMRAP_RELEASES_URL_TEMPLATE = "https://amrap.org.au/api/track/release/artist/{slug}"
AMRAP_PAGE_SIZE = 18
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Australian state abbreviations exposed by AMRAP public directory
AMRAP_STATES = {"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"}

_LOGGER = logging.getLogger(__name__)

AMRAP_CSV_FIELDS: List[str] = [
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


def extract_amrap_spotify_artist_identity(row: Dict[str, Any]) -> tuple[str, str, str]:
    """Return a strict direct Spotify artist identity from AMRAP link fields.

    AMRAP aggregates artist-supplied URLs in ``Social Link``. Only identities
    accepted by the shared Spotify validator qualify; track, album, playlist,
    shortened, and malformed URLs fail closed. The status supports aggregate
    logging without exposing or guessing an artist identity.
    """
    spotify_values: List[str] = []
    for field in ("Social Link", "External Links"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        for value in re.split(r"\s*(?:;|\|)\s*", raw):
            value = value.strip()
            if not value:
                continue
            if "spotify." not in value.lower() and not value.lower().startswith("spotify:"):
                continue
            spotify_values.append(value)
            artist_id, canonical_url = extract_spotify_artist_identity(value)
            if artist_id:
                return artist_id, canonical_url, "valid"
    return "", "", "malformed" if spotify_values else "missing"


def enrich_amrap_spotify_releases(
    rows: List[Dict[str, Any]],
    *,
    spotify_client: Optional[SpotifyClient] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """Fill blank AMRAP release pairs from strict Spotify artist identities.

    Native AMRAP title/date pairs are authoritative. Partial native metadata
    is also left untouched so values from different releases cannot be paired.
    Only the shared Spotify latest-release selector performs API lookups.
    """
    log = logger or _LOGGER.info
    enricher: Optional[SpotifyLatestReleaseEnricher] = None
    spotify_unavailable = False
    native_complete = 0
    partial_native = 0
    fallback_candidates = 0
    valid_identities = 0
    malformed_identities = 0
    filled = 0

    for row in rows:
        song_title = str(row.get("Song Title") or "").strip()
        release_date = str(row.get("Release Date") or "").strip()
        if song_title and release_date:
            native_complete += 1
            continue
        if song_title or release_date:
            partial_native += 1
            continue

        fallback_candidates += 1
        artist_id, _canonical_url, identity_status = extract_amrap_spotify_artist_identity(row)
        if identity_status == "malformed":
            malformed_identities += 1
        if not artist_id:
            continue

        valid_identities += 1
        if enricher is None and not spotify_unavailable:
            try:
                client = spotify_client or SpotifyClient(logger=log)
                enricher = SpotifyLatestReleaseEnricher(client, logger=log)
            except Exception as exc:
                spotify_unavailable = True
                log(f"[Spotify Latest Release][AMRAP] unavailable: {exc}")
        if enricher is None:
            continue

        latest = enricher.lookup(artist_id)
        if latest.song_title and latest.release_date:
            row["Song Title"] = latest.song_title
            row["Release Date"] = latest.release_date
            filled += 1

    log(
        "[Spotify Latest Release][AMRAP] "
        f"native_complete={native_complete}, "
        f"partial_native={partial_native}, "
        f"fallback_candidates={fallback_candidates}, "
        f"valid_identities={valid_identities}, "
        f"successful_lookups={enricher.successful_lookups if enricher else 0}, "
        f"filled={filled}, "
        f"malformed_identities={malformed_identities}, "
        f"lookup_failures={enricher.lookup_failures if enricher else 0}"
    )
    return rows


def _amrap_link_values(row: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    seen = set()
    for field in ("Social Link", "External Links"):
        raw = str(row.get(field) or "").strip()
        for value in re.split(r"\s*(?:;|\|)\s*", raw):
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _bandcamp_identity_url(value: str) -> str:
    return canonicalize_bandcamp_profile_url(value)


def _owned_page_candidates(row: Dict[str, Any], authorities: Any) -> List[str]:
    artist_name = str(row.get("Artist Name") or "").strip()
    candidates: List[str] = []
    seen = set()
    excluded_music_hosts = {
        "bandcamp.com", "deezer.com", "music.apple.com", "soundcloud.com",
        "spotify.com", "open.spotify.com", "play.spotify.com",
    }
    for raw in _amrap_link_values(row):
        normalised = authorities._normalise_url(raw) or ""
        if not normalised or normalised in seen:
            continue
        if _bandcamp_identity_url(normalised) or extract_spotify_artist_identity(normalised)[0]:
            continue
        try:
            host = (urllib.parse.urlparse(normalised).hostname or "").lower()
        except Exception:
            continue
        host = host[4:] if host.startswith("www.") else host
        if any(host == domain or host.endswith("." + domain) for domain in excluded_music_hosts):
            continue
        if is_artist_platform_profile(normalised):
            continue
        if not is_useful_artist_link(normalised, artist_name=artist_name):
            continue
        if host in authorities.LINK_HUB_HOSTS and not is_artist_link_hub_profile(normalised):
            continue
        seen.add(normalised)
        candidates.append(normalised)
    return candidates


class _BoundedBandcampBrowserFetcher:
    """Run-scoped adapter over the existing bounded Bandcamp browser seam."""

    def __init__(self, authorities: Any) -> None:
        self.authorities = authorities
        self.driver = None
        self.disabled = False

    def __call__(self, url: str) -> str:
        if self.disabled:
            return ""
        setup = getattr(self.authorities, "setup_bandcamp_driver", None)
        quick_visit = getattr(self.authorities, "bandcamp_quick_visit", None)
        if not callable(setup) or not callable(quick_visit):
            self.disabled = True
            return ""
        if self.driver is None:
            try:
                self.driver = setup()
            except Exception:
                self.disabled = True
                return ""
        try:
            return quick_visit(self.driver, url) or ""
        except Exception:
            return ""

    def close(self) -> None:
        driver = self.driver
        self.driver = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _validated_bandcamp_track(
    artist_name: str,
    profile_url: str,
    *,
    authorities: Any,
    session: Optional[requests.Session],
    browser_fetcher: Optional[Callable[[str], str]],
) -> tuple[str, str, str]:
    result = fetch_bandcamp_profile(
        profile_url,
        session=session,
        browser_fetcher=browser_fetcher,
        browser_on_empty=False,
    )
    if result.status != BANDCAMP_PROFILE_ACCEPTED or not result.profile:
        return "", "", "unavailable"
    page_artist = str(
        (result.identity_evidence or {}).get("page_artist")
        or result.profile.get("artist_name")
        or ""
    ).strip()
    confidence = authorities._bandcamp_confidence(
        artist_name,
        page_artist,
        result.canonical_url or profile_url,
    )
    if (
        confidence < authorities.MIN_BC_CONFIDENCE
        or not authorities._bc_slug_has_strong_artist_name_confirmation(
            artist_name,
            page_artist,
        )
    ):
        return "", "", "identity_rejected"
    track_title = str(result.profile.get("latest_track_title") or "").strip()
    track_date = str(result.profile.get("latest_track_date") or "").strip()
    if not track_title or not track_date:
        return "", "", "track_semantic_rejected"
    return track_title, track_date, "accepted"


def enrich_amrap_release_cascade(
    rows: List[Dict[str, Any]],
    *,
    spotify_client: Optional[SpotifyClient] = None,
    bandcamp_session: Optional[requests.Session] = None,
    website_session: Optional[requests.Session] = None,
    browser_fetcher: Optional[Callable[[str], str]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """Fill missing AMRAP release pairs through the bounded identity cascade."""
    log = logger or _LOGGER.info
    native_complete = sum(
        1 for row in rows
        if str(row.get("Song Title") or "").strip()
        and str(row.get("Release Date") or "").strip()
    )
    initially_blank = {
        index for index, row in enumerate(rows)
        if not str(row.get("Song Title") or "").strip()
        and not str(row.get("Release Date") or "").strip()
    }
    enrich_amrap_spotify_releases(rows, spotify_client=spotify_client, logger=logger)
    direct_spotify_filled = sum(
        1 for index in initially_blank
        if str(rows[index].get("Song Title") or "").strip()
        and str(rows[index].get("Release Date") or "").strip()
    )

    direct_bandcamp_candidates = 0
    direct_bandcamp_filled = 0
    owned_page_candidates = 0
    owned_page_spotify_filled = 0
    owned_page_bandcamp_filled = 0
    track_semantic_rejections = 0

    authorities = None
    owned_spotify_enricher: Optional[SpotifyLatestReleaseEnricher] = None
    owned_spotify_unavailable = False
    active_website_session = website_session
    owned_website_session = False
    active_browser_fetcher = browser_fetcher
    browser_adapter: Optional[_BoundedBandcampBrowserFetcher] = None

    def load_authorities():
        nonlocal authorities, active_browser_fetcher, browser_adapter
        if authorities is None:
            import cross_directory_enricher as shared_authorities
            authorities = shared_authorities
        if active_browser_fetcher is None:
            browser_adapter = _BoundedBandcampBrowserFetcher(authorities)
            active_browser_fetcher = browser_adapter
        return authorities

    def bandcamp_lookup(row: Dict[str, Any], url: str) -> bool:
        nonlocal track_semantic_rejections
        shared = load_authorities()
        title, date, status = _validated_bandcamp_track(
            str(row.get("Artist Name") or "").strip(),
            url,
            authorities=shared,
            session=bandcamp_session,
            browser_fetcher=active_browser_fetcher,
        )
        if status == "track_semantic_rejected":
            track_semantic_rejections += 1
        if status != "accepted":
            return False
        row["Song Title"] = title
        row["Release Date"] = date
        return True

    try:
        for row in rows:
            song_title = str(row.get("Song Title") or "").strip()
            release_date = str(row.get("Release Date") or "").strip()
            if song_title or release_date:
                continue

            direct_bandcamp_urls: List[str] = []
            seen_bandcamp = set()
            for value in _amrap_link_values(row):
                canonical = _bandcamp_identity_url(value)
                if canonical and canonical not in seen_bandcamp:
                    seen_bandcamp.add(canonical)
                    direct_bandcamp_urls.append(value)
            if direct_bandcamp_urls:
                direct_bandcamp_candidates += 1
            if any(bandcamp_lookup(row, url) for url in direct_bandcamp_urls):
                direct_bandcamp_filled += 1
                continue

            shared = load_authorities()
            page_candidates = _owned_page_candidates(row, shared)
            if not page_candidates:
                continue
            owned_page_candidates += 1
            if active_website_session is None:
                active_website_session = requests.Session()
                active_website_session.headers.update({"User-Agent": _DEFAULT_USER_AGENT})
                owned_website_session = True

            # One artist-owned page at most; outbound identities are not crawled.
            page_url = page_candidates[0]
            page = shared._fetch_website_html_bounded(active_website_session, page_url)
            if not page.is_html or not page.html:
                continue
            socials, _websites, _emails, _link_hubs = shared._extract_links_from_profile(
                page.html,
                "website",
                page.final_url or page_url,
            )
            outbound = sorted(socials | _websites)

            spotify_id = ""
            for value in outbound:
                spotify_id, _canonical = extract_spotify_artist_identity(value)
                if spotify_id:
                    break
            if spotify_id:
                if owned_spotify_enricher is None and not owned_spotify_unavailable:
                    try:
                        client = spotify_client or SpotifyClient(logger=log)
                        owned_spotify_enricher = SpotifyLatestReleaseEnricher(client, logger=log)
                    except Exception as exc:
                        owned_spotify_unavailable = True
                        log(f"[Spotify Latest Release][AMRAP Owned Page] unavailable: {exc}")
                if owned_spotify_enricher is not None:
                    latest = owned_spotify_enricher.lookup(spotify_id)
                    if latest.song_title and latest.release_date:
                        row["Song Title"] = latest.song_title
                        row["Release Date"] = latest.release_date
                        owned_page_spotify_filled += 1
                        continue

            outbound_bandcamp = []
            seen_outbound_bandcamp = set()
            for value in outbound:
                canonical = _bandcamp_identity_url(value)
                if canonical and canonical not in seen_outbound_bandcamp:
                    seen_outbound_bandcamp.add(canonical)
                    outbound_bandcamp.append(value)
            if any(bandcamp_lookup(row, url) for url in outbound_bandcamp):
                owned_page_bandcamp_filled += 1
    finally:
        if browser_adapter is not None:
            browser_adapter.close()
        if owned_website_session and active_website_session is not None:
            active_website_session.close()

    unresolved = sum(
        1 for row in rows
        if not str(row.get("Song Title") or "").strip()
        or not str(row.get("Release Date") or "").strip()
    )
    log(
        "[AMRAP Release Cascade] "
        f"native_complete={native_complete}, "
        f"direct_spotify_filled={direct_spotify_filled}, "
        f"direct_bandcamp_candidates={direct_bandcamp_candidates}, "
        f"direct_bandcamp_filled={direct_bandcamp_filled}, "
        f"owned_page_candidates={owned_page_candidates}, "
        f"owned_page_spotify_filled={owned_page_spotify_filled}, "
        f"owned_page_bandcamp_filled={owned_page_bandcamp_filled}, "
        f"track_semantic_rejections={track_semantic_rejections}, "
        f"unresolved={unresolved}"
    )
    return rows


def _make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": _DEFAULT_USER_AGENT,
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


def normalize_amrap_external_url(value: Any) -> str:
    """Return a safe AMRAP external URL, repairing only deterministic defects."""
    candidate = str(value or "").strip()
    if not candidate or any(char.isspace() for char in candidate):
        return ""
    supplied_scheme = "://" in candidate
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    try:
        parsed = urlsplit(candidate)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""

    netloc = parsed.netloc
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or ""

    # AMRAP sometimes stores a complete SoundCloud host as the first path
    # segment beneath another SoundCloud host.
    if host in {"soundcloud.com", "www.soundcloud.com"}:
        nested = re.match(r"^/(?:www\.)?soundcloud\.com(/.*)$", path, flags=re.IGNORECASE)
        if nested:
            host = "soundcloud.com"
            netloc = host
            path = nested.group(1)

    # Repair duplicated Bandcamp hosts, including the observed malformed
    # userinfo form.  Only a single-label artist subdomain is accepted.
    bandcamp_source = netloc.lower()
    if "@" in bandcamp_source:
        userinfo, _, netloc_host = bandcamp_source.rpartition("@")
        if netloc_host == "bandcamp.com.bandcamp.com":
            bandcamp_source = f"{userinfo}.bandcamp.com.bandcamp.com"
    bandcamp_match = re.fullmatch(
        r"(?:www\.)?([a-z0-9][a-z0-9-]{0,62})\.bandcamp\.com\.bandcamp\.com(?::\d+)?",
        bandcamp_source,
        flags=re.IGNORECASE,
    )
    if bandcamp_match:
        netloc = f"{bandcamp_match.group(1).lower()}.bandcamp.com"
        host = netloc

    if not host or "." not in host or "@" in netloc:
        return ""
    if host.endswith(".bandcamp.com.bandcamp.com") or (
        host in {"soundcloud.com", "www.soundcloud.com"}
        and re.match(r"^/(?:www\.)?soundcloud\.com(?:/|$)", path, flags=re.IGNORECASE)
    ):
        return ""

    scheme = parsed.scheme.lower() if supplied_scheme else "https"
    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


def _release_track_title(row: Dict[str, Any]) -> str:
    for value in (row.get("title"), row.get("track"), row.get("track_title"), row.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    track = row.get("track")
    if isinstance(track, dict):
        for key in ("title", "name"):
            value = track.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _parse_release_date(value: Any) -> Optional[datetime.date]:
    text = str(value or "").strip()
    if not text:
        return None
    iso_candidate = text[:10]
    try:
        return datetime.date.fromisoformat(iso_candidate)
    except ValueError:
        pass
    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%Y/%m/%d",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}", text):
        try:
            return datetime.date(int(text), 1, 1)
        except ValueError:
            return None
    return None


def extract_latest_amrap_release(release_rows: Any) -> tuple[str, str]:
    """Select the first profile-order track on the newest safely parsed date."""
    if not isinstance(release_rows, list):
        return "", ""
    newest: Optional[tuple[datetime.date, str]] = None
    for item in release_rows:
        if not isinstance(item, dict):
            continue
        title = _release_track_title(item)
        if not title:
            continue
        release = item.get("release") if isinstance(item.get("release"), dict) else {}
        date_candidates = (
            item.get("release_date"),
            item.get("released_at"),
            item.get("date"),
            item.get("year"),
            release.get("release_date"),
            release.get("released_at"),
            release.get("date"),
            release.get("year"),
        )
        parsed_date = next(
            (parsed for parsed in (_parse_release_date(value) for value in date_candidates) if parsed is not None),
            None,
        )
        if parsed_date is None:
            continue
        if newest is None or parsed_date > newest[0]:
            newest = (parsed_date, title)
    if newest is None:
        return "", ""
    return newest[1], newest[0].isoformat()


def parse_amrap_profile(
    profile_data: Dict[str, Any], release_rows: Optional[List[Dict[str, Any]]] = None
) -> Optional[Dict[str, Any]]:
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
    seen_links = set()
    for key in ("websites", "socials", "music_accounts"):
        for item in profile_data.get(key, []):
            if isinstance(item, dict) and item.get("link"):
                link = normalize_amrap_external_url(item["link"])
                if link and link not in seen_links:
                    links.append(link)
                    seen_links.add(link)
    social_link = "; ".join(links)

    if release_rows is None:
        for key in ("releases", "release_rows", "tracks"):
            candidate_rows = profile_data.get(key)
            if isinstance(candidate_rows, list):
                release_rows = candidate_rows
                break
    song_title, release_date = extract_latest_amrap_release(release_rows or [])

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
        "Song Title": song_title,
        "Sounds Like": "",
        "SoundCloud Link": "",
        "Release Date": release_date,
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


def fetch_release_rows(session: requests.Session, slug: str) -> List[Dict[str, Any]]:
    """Fetch every public Available Releases page for an AMRAP artist."""
    rows: List[Dict[str, Any]] = []
    page = 1
    while True:
        url = AMRAP_RELEASES_URL_TEMPLATE.format(slug=slug)
        response = session.get(url, params={"page": page}, timeout=30)
        if response.status_code == 404:
            return rows
        response.raise_for_status()
        payload = response.json()
        page_rows = payload.get("data", []) if isinstance(payload, dict) else []
        rows.extend(item for item in page_rows if isinstance(item, dict))
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        try:
            last_page = int(meta.get("last_page") or page)
        except (TypeError, ValueError):
            last_page = page
        if page >= last_page:
            return rows
        page += 1


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

            try:
                release_rows = fetch_release_rows(session, slug)
            except (requests.RequestException, ValueError) as exc:
                log(f"[AMRAP] Release fetch failed for {slug}: {exc}")
                release_rows = []
            song_title, release_date = extract_latest_amrap_release(release_rows)
            row["Song Title"] = song_title
            row["Release Date"] = release_date

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

    enrich_amrap_release_cascade(results, logger=logger)
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

    # Existing Legacy output headers are authoritative.  Rewrite by field name
    # so an AMRAP-specific ordering can never shift values beneath that header.
    existing_rows: List[Dict[str, Any]] = []
    existing_columns: List[str] = []
    append_existing = bool(existing_csv and os.path.exists(existing_csv))
    if append_existing:
        try:
            with open(existing_csv, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    existing_columns = list(reader.fieldnames)
                    existing_rows = list(reader)
        except (OSError, csv.Error) as exc:
            log(f"[AMRAP] Warning: could not preserve existing CSV schema: {exc}")

    all_columns = existing_columns + [field for field in AMRAP_CSV_FIELDS if field not in existing_columns]
    if not all_columns:
        all_columns = AMRAP_CSV_FIELDS.copy()

    with open(output_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
        writer.writeheader()
        for row in [*existing_rows, *rows]:
            writer.writerow(row)

    log(f"[AMRAP] Wrote {len(rows)} rows to {output_csv}")
    return output_csv
