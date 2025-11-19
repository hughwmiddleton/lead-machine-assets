"""
Spotify playlist discovery scraper for Lead Machine.

Phase 2 wires into the Spotify Web API using editorial playlists (e.g. Fresh
Finds) and returns real artist rows that match the shared CSV schema.
"""
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from spotify_about_scraper import enrich_spotify_rows_with_about_links
from spotify_client import SpotifyClient
from spotify_playlist_html_scraper import scrape_playlist_artists_via_html
from website_email_scraper import enrich_rows_with_website_emails

Row = Dict[str, str]
LoggerFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

DATE_FORMAT = "%d/%m/%Y"

# TODO: Fill these with real playlist IDs for Fresh Finds / region-specific lists.
FRESH_FINDS_PLAYLIST_IDS = [
    "TODO_FRESH_FINDS_US_PLAYLIST_ID",
    "TODO_FRESH_FINDS_UK_IE_PLAYLIST_ID",
    "TODO_FRESH_FINDS_AU_NZ_PLAYLIST_ID",
]


def _extract_playlist_id(value: str) -> Optional[str]:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("spotify:playlist:"):
        return candidate.split(":")[-1]
    if "spotify.com" in candidate:
        try:
            parsed = urlparse(candidate)
            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0].lower() == "playlist":
                return path_parts[1]
        except Exception:
            return None
    if re.fullmatch(r"[A-Za-z0-9]{16,40}", candidate):
        return candidate
    return None


def _should_use_html_fallback(error: Exception) -> bool:
    message = str(error or "")
    lowered = message.lower()
    return "404" in lowered or "not found" in lowered


def _build_spotify_row(
    artist_name: str,
    track_name: str,
    playlist_label: str,
    primary_genre: str,
    timestamp: str,
    spotify_url: str,
    artist_id: str,
) -> Row:
    return {
        "Artist Name": artist_name or "",
        "Location": "",
        "Song Title": track_name or "",
        "Sounds Like": "",
        "Social Link": "",
        "SoundCloud Link": "",
        "Played on triple J": "",
        "Played on Unearthed": "",
        "Release Date": "",
        "Primary Genre": primary_genre or "",
        "Date Added": timestamp,
        "Spotify Playlist": playlist_label or "",
        "External Links": "",
        "Email": "",
        "Spotify_URL": spotify_url or "",
        "Spotify_Artist_ID": artist_id or "",
        "Spotify_Website_URL": "",
    }


def _collapse_spotify_socials_into_social_link(row: Row) -> None:
    social_fields = [
        "Spotify_Website_URL",
    ]
    candidates: List[str] = []
    existing_socials = [
        token.strip()
        for token in re.split(r"\s*\|\s*|,\s*", (row.get("Social Link") or ""))
        if token.strip()
    ]
    for field in social_fields:
        value = (row.get(field) or "").strip()
        if value:
            candidates.append(value)
    deduped: List[str] = []
    for value in existing_socials + candidates:
        if not value:
            continue
        lowered = value.lower()
        if "spotify.com" in lowered:
            continue
        if value not in deduped:
            deduped.append(value)
    if deduped:
        row["Social Link"] = " | ".join(deduped)
        current = (row.get("External Links") or "").strip()
        if not current:
            row["External Links"] = "; ".join(deduped)


def _collapse_spotify_socials(rows: List[Row]) -> List[Row]:
    for row in rows or []:
        _collapse_spotify_socials_into_social_link(row)
    return rows


def _resolve_playlist_label(
    client: SpotifyClient,
    playlist_id: str,
    fallback_label: str,
    logger: Optional[LoggerFn],
) -> str:
    try:
        details = client.get_playlist_metadata(playlist_id)
        return details.get("name") or fallback_label
    except Exception as exc:
        if logger:
            try:
                logger(f"[Spotify] Unable to fetch playlist metadata for {playlist_id}: {exc}")
            except Exception:
                pass
        return fallback_label


def scrape_spotify(
    scrape_amount: int,
    params: Dict,
    logger: Optional[LoggerFn] = None,
    progress_callback: Optional[ProgressFn] = None,
) -> List[Row]:
    """
    Fetch artists from configured Spotify playlists (Fresh Finds variants).

    MUST NOT modify behaviour of other scrapers when Spotify is not selected.
    """

    params = params or {}
    target_count = max(int(scrape_amount or 0), 0)
    if target_count == 0:
        if logger:
            logger("[Spotify] No scrape amount provided; returning zero rows.")
        return []

    try:
        client = SpotifyClient(
            client_id=params.get("spotify_client_id"),
            client_secret=params.get("spotify_client_secret"),
            logger=logger,
        )
    except ValueError as exc:
        if logger:
            logger(f"[Spotify] {exc}")
        return []
    except Exception as exc:  # pragma: no cover - defensive
        if logger:
            logger(f"[Spotify] Failed to initialize Spotify client: {exc}")
        return []

    if logger:
        logger(f"[Spotify] Starting playlist discovery for {target_count} artists.")

    playlist_ids_param = params.get("playlist_ids")
    search_input = (params.get("search_term") or "").strip()
    playlist_from_input = _extract_playlist_id(search_input)

    if playlist_ids_param:
        if isinstance(playlist_ids_param, str):
            playlist_ids = [playlist_ids_param]
        else:
            playlist_ids = list(playlist_ids_param)
    elif playlist_from_input:
        playlist_ids = [playlist_from_input]
        if logger:
            logger(f"[Spotify] Using playlist from input field: {playlist_from_input}")
    else:
        playlist_ids = FRESH_FINDS_PLAYLIST_IDS
    resolved_playlist_ids = [pid for pid in playlist_ids if pid and not pid.startswith("TODO_")]
    if not resolved_playlist_ids:
        if logger:
            logger("[Spotify] No playlist IDs configured. Update FRESH_FINDS_PLAYLIST_IDS or pass playlist_ids in params.")
        return []

    artists_by_id: Dict[str, Dict[str, Any]] = {}
    total_tracks_scanned = 0

    for playlist_id in resolved_playlist_ids:
        if len(artists_by_id) >= target_count:
            break
        playlist_id_clean = _extract_playlist_id(playlist_id) or playlist_id
        if not playlist_id_clean:
            continue
        playlist_label = _resolve_playlist_label(client, playlist_id_clean, playlist_id_clean, logger)
        try:
            if logger:
                logger(f"[Spotify] Attempting API playlist fetch for {playlist_id_clean}...")
            tracks = client.get_playlist_tracks(playlist_id_clean, limit=100, max_items=max(target_count * 3, 300))
        except Exception as exc:
            if logger:
                logger(f"[Spotify] API playlist fetch failed for {playlist_id_clean}: {exc}")
            if _should_use_html_fallback(exc):
                if logger:
                    logger(f"[Spotify] Falling back to HTML playlist scrape for {playlist_id_clean}...")
                remaining_target = max(target_count - len(artists_by_id), 1)
                html_limit = min(max(remaining_target * 2, remaining_target), 500)
                html_artists = scrape_playlist_artists_via_html(
                    playlist_id_clean,
                    max_artists=html_limit,
                    logger=logger,
                    progress_callback=progress_callback,
                )
                if not html_artists:
                    continue
                fallback_label = html_artists[0].get("playlist_name") or playlist_label
                for artist in html_artists:
                    if len(artists_by_id) >= target_count:
                        break
                    artist_id = (artist.get("artist_id") or "").strip()
                    if not artist_id or artist_id in artists_by_id:
                        continue
                    artists_by_id[artist_id] = {
                        "artist_stub": {"name": artist.get("artist_name") or "", "id": artist_id},
                        "sample_track": artist.get("track_name") or "",
                        "playlist_label": artist.get("playlist_name") or fallback_label,
                        "artist_url": artist.get("artist_url") or "",
                    }
                continue
            else:
                if logger:
                    logger(f"[Spotify] Not using HTML fallback for playlist {playlist_id_clean}; skipping.")
                continue

        total_tracks_scanned += len(tracks)
        for entry in tracks:
            if len(artists_by_id) >= target_count:
                break
            track_obj = entry.get("track") if isinstance(entry, dict) else entry
            if not track_obj:
                continue
            track_name = track_obj.get("name", "")
            artists = track_obj.get("artists") or []
            if not artists:
                continue
            primary_artist = artists[0]
            artist_id = (primary_artist or {}).get("id")
            if not artist_id or artist_id in artists_by_id:
                continue
            spotify_url = ((primary_artist or {}).get("external_urls") or {}).get("spotify", "")
            artists_by_id[artist_id] = {
                "artist_stub": primary_artist,
                "sample_track": track_name,
                "playlist_label": playlist_label,
                "artist_url": spotify_url,
            }

    if logger:
        logger(f"[Spotify] Scanned {total_tracks_scanned} tracks across playlists. Unique artists: {len(artists_by_id)}")

    if not artists_by_id:
        return []

    try:
        artist_details = client.get_artists_details(list(artists_by_id.keys()))
    except Exception as exc:
        if logger:
            logger(f"[Spotify] Failed to fetch artist details: {exc}")
        artist_details = {}

    rows: List[Row] = []
    timestamp = time.strftime(DATE_FORMAT)
    total_artists = len(artists_by_id)

    for idx, (artist_id, stub_info) in enumerate(artists_by_id.items(), start=1):
        artist_payload = artist_details.get(artist_id, {})
        stub = stub_info.get("artist_stub") or {}
        artist_name = artist_payload.get("name") or stub.get("name") or "Unknown Artist"
        spotify_url = (artist_payload.get("external_urls") or {}).get("spotify") or stub_info.get("artist_url") or ""
        genres = artist_payload.get("genres") or []
        primary_genre = ", ".join(genres[:3])
        track_name = stub_info.get("sample_track") or ""
        playlist_label = stub_info.get("playlist_label") or ""

        row = _build_spotify_row(
            artist_name=artist_name,
            track_name=track_name,
            playlist_label=playlist_label,
            primary_genre=primary_genre,
            timestamp=timestamp,
            spotify_url=spotify_url,
            artist_id=artist_id,
        )
        rows.append(row)

        if progress_callback:
            try:
                progress_callback(idx, total_artists)
            except Exception:
                pass

    if logger:
        logger(f"[Spotify] Discovery complete — returning {len(rows)} rows.")

    if logger:
        logger("[Spotify] Starting About-page enrichment...")
    try:
        rows = enrich_spotify_rows_with_about_links(
            rows,
            logger=logger,
            progress_callback=progress_callback,
        )
    except Exception as exc:  # pragma: no cover - defensive
        if logger:
            logger(f"[Spotify] Warning: About-page enrichment failed: {exc}")
    else:
        if logger:
            logger("[Spotify] About-page enrichment completed.")

    if logger:
        logger("[Spotify] Starting website -> email enrichment...")
    try:
        rows = enrich_rows_with_website_emails(
            rows,
            logger=logger,
            progress_callback=progress_callback,
        )
    except Exception as exc:  # pragma: no cover - defensive
        if logger:
            logger(f"[Spotify] Warning: website -> email enrichment failed: {exc}")
    else:
        if logger:
            logger("[Spotify] Website -> email enrichment completed.")

    rows = _collapse_spotify_socials(rows)
    return rows
