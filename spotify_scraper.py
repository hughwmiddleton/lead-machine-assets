"""
Spotify playlist discovery scraper for Lead Machine.

Phase 2 wires into the Spotify Web API using editorial playlists (e.g. Fresh
Finds) and returns real artist rows that match the shared CSV schema.
"""
import re
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from spotify_about_scraper import enrich_spotify_rows_with_about_links
from spotify_client import SpotifyClient
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

    artists_by_id: Dict[str, Dict] = {}
    total_tracks_scanned = 0

    for playlist_id in resolved_playlist_ids:
        try:
            tracks = client.get_playlist_tracks(playlist_id, limit=100, max_items=max(target_count * 3, 300))
        except Exception as exc:
            if logger:
                logger(f"[Spotify] Failed to fetch playlist {playlist_id}: {exc}")
            continue

        total_tracks_scanned += len(tracks)
        for entry in tracks:
            track_obj = entry.get("track") if isinstance(entry, dict) else entry
            if not track_obj:
                continue
            track_name = track_obj.get("name", "")
            artists = track_obj.get("artists") or []
            if not artists:
                continue
            # Focus on the primary artist (index 0) for now.
            primary_artist = artists[0]
            artist_id = (primary_artist or {}).get("id")
            if not artist_id or artist_id in artists_by_id:
                continue
            artists_by_id[artist_id] = {
                "artist_stub": primary_artist,
                "sample_track": track_name,
            }
            if len(artists_by_id) >= target_count:
                break
        if len(artists_by_id) >= target_count:
            break

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
        artist_name = artist_payload.get("name") or stub_info.get("artist_stub", {}).get("name") or "Unknown Artist"
        spotify_url = (artist_payload.get("external_urls") or {}).get("spotify", "")
        genres = artist_payload.get("genres") or []
        primary_genre = ", ".join(genres[:3])
        followers = (artist_payload.get("followers") or {}).get("total")
        popularity = artist_payload.get("popularity")

        row: Row = {
            "Artist Name": artist_name,
            "Location": "",
            "Song Title": "",  # TODO: tie specific track later if desired
            "Sounds Like": "",
            "Social Link": "",
            "SoundCloud Link": "",
            "Played on triple J": "",
            "Played on Unearthed": "",
            "Release Date": "",
            "Primary Genre": primary_genre,
            "Date Added": timestamp,
            "External Links": "",
            "Email": "",
            "Spotify_URL": spotify_url,
            "Spotify_Artist_ID": artist_id,
            "Spotify_Instagram_URL": "",  # TODO: populate via About-tab scraper in Phase 3
            "Spotify_Facebook_URL": "",
            "Spotify_Twitter_URL": "",
            "Spotify_Website_URL": "",
            "Spotify_Followers": str(followers or ""),
            "Spotify_Popularity": str(popularity or ""),
        }
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

    return rows
