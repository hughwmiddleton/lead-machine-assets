"""
Spotify playlist discovery scraper for Lead Machine.

Phase 2 wires into the Spotify Web API using editorial playlists (e.g. Fresh
Finds) and returns real artist rows that match the shared CSV schema.
"""
import csv
import os
import re
import time
import unicodedata
import requests
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from spotify_about_scraper import enrich_spotify_rows_with_about_links
from spotify_client import SpotifyClient
from spotify_playlist_html_scraper import scrape_playlist_artists_via_html
from website_email_scraper import enrich_rows_with_website_emails

Row = Dict[str, str]
LoggerFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

DATE_FORMAT = "%d/%m/%Y"
_LASTFM_GENRE_CACHE: Dict[str, str] = {}

# TODO: Fill these with real playlist IDs for Fresh Finds / region-specific lists.
FRESH_FINDS_PLAYLIST_IDS = [
    "TODO_FRESH_FINDS_US_PLAYLIST_ID",
    "TODO_FRESH_FINDS_UK_IE_PLAYLIST_ID",
    "TODO_FRESH_FINDS_AU_NZ_PLAYLIST_ID",
]

GENRE_COLUMN_CANDIDATES: Sequence[str] = (
    "Primary Genre",
    "primary_genre",
    "Genres",
    "Genre",
    "genre",
    "seed_genre",
)

DIRECTORY_GENRE_FILES: Sequence[Sequence[Any]] = [
    ("bandcamp_enriched.csv", GENRE_COLUMN_CANDIDATES),
    ("bandcamp_output.csv", GENRE_COLUMN_CANDIDATES),
    ("soundcloud_enriched.csv", GENRE_COLUMN_CANDIDATES),
    ("soundcloud_output.csv", GENRE_COLUMN_CANDIDATES),
    ("lastfm_output.csv", GENRE_COLUMN_CANDIDATES),
    ("unearthed_output.csv", GENRE_COLUMN_CANDIDATES),
]

SCRIPT_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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


def _normalize_artist_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    value = name.strip().lower()
    if not value:
        return ""
    for suffix in (" - topic", " official"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
    value = re.sub(r"^(the|a)\s+", "", value)
    value = re.sub(r"[^\w\s]", "", value)
    value = re.sub(r"\s+", " ", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.strip()


def _normalize_track_title(title: str) -> str:
    if not isinstance(title, str):
        return ""
    t = title.strip().lower()
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def _find_unused_track_via_api(
    spotify_client: Optional[SpotifyClient],
    artist_id: str,
    used_song_titles: Set[str],
    logger: Optional[LoggerFn],
) -> Optional[Dict[str, Any]]:
    """
    Attempt to retrieve an unused track for the artist via the Spotify API.
    Tries top tracks first, then scans a limited set of album/single tracks.
    """
    if not spotify_client or not artist_id:
        return None

    def _log_debug(message: str) -> None:
        if logger:
            try:
                logger(message)
            except Exception:
                pass

    # Top tracks
    try:
        top_resp = spotify_client._authorized_request(  # type: ignore[attr-defined]
            "GET",
            f"{spotify_client.API_BASE}/artists/{artist_id}/top-tracks",
            params={"market": "US"},
        )
        for track in top_resp.get("tracks", []) or []:
            title = (track.get("name") or "").strip()
            norm = _normalize_track_title(title)
            if norm and norm not in used_song_titles:
                _log_debug(f"[Spotify S1] uniqueness fallback: using top track '{title}' for artist_id={artist_id}")
                return track
    except Exception as exc:
        _log_debug(f"[Spotify S1] uniqueness fallback: top-tracks lookup failed for artist_id={artist_id}: {exc}")

    # Album/single tracks (limited)
    try:
        albums_resp = spotify_client._authorized_request(  # type: ignore[attr-defined]
            "GET",
            f"{spotify_client.API_BASE}/artists/{artist_id}/albums",
            params={"include_groups": "single,album", "limit": 10},
        )
        for album in albums_resp.get("items", []) or []:
            album_id = album.get("id")
            if not album_id:
                continue
            try:
                tracks_resp = spotify_client._authorized_request(  # type: ignore[attr-defined]
                    "GET",
                    f"{spotify_client.API_BASE}/albums/{album_id}/tracks",
                    params={"limit": 50},
                )
            except Exception:
                continue
            for track in tracks_resp.get("items", []) or []:
                title = (track.get("name") or "").strip()
                norm = _normalize_track_title(title)
                if norm and norm not in used_song_titles:
                    _log_debug(
                        f"[Spotify S1] uniqueness fallback: using album track '{title}' for artist_id={artist_id} (album_id={album_id})"
                    )
                    return track
    except Exception as exc:
        _log_debug(f"[Spotify S1] uniqueness fallback: album-tracks lookup failed for artist_id={artist_id}: {exc}")

    return None


def _fetch_additional_genres(
    spotify_client: Optional[SpotifyClient],
    artist_id: str,
    logger: Optional[LoggerFn],
) -> str:
    """
    Try to infer genres via related artists as a lightweight fallback.
    Returns a comma-separated genre string or empty string.
    """
    if not spotify_client or not artist_id:
        return ""

    def _log_debug(message: str) -> None:
        if logger:
            try:
                logger(message)
            except Exception:
                pass

    try:
        payload = spotify_client._authorized_request(  # type: ignore[attr-defined]
            "GET",
            f"{spotify_client.API_BASE}/artists/{artist_id}/related-artists",
        )
    except Exception as exc:
        _log_debug(f"[Spotify S1] genre fallback: related-artists lookup failed for artist_id={artist_id}: {exc}")
        return ""

    genre_counts: Dict[str, int] = {}
    for artist in payload.get("artists", []) or []:
        for genre in artist.get("genres") or []:
            cleaned = (genre or "").strip()
            if not cleaned:
                continue
            genre_counts[cleaned] = genre_counts.get(cleaned, 0) + 1

    if not genre_counts:
        return ""

    sorted_genres = sorted(genre_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    top = [name for name, _ in sorted_genres[:3]]
    primary = ", ".join(top)
    if primary and logger:
        _log_debug(f"[Spotify S1] genre fallback: using related-artist genres for {artist_id}: {primary}")
    return primary


def _fetch_genres_from_top_tracks(
    spotify_client: Optional[SpotifyClient],
    artist_id: str,
    logger: Optional[LoggerFn],
) -> str:
    """
    Fallback: pull genres from artists that appear on the artist's top tracks.
    """
    if not spotify_client or not artist_id:
        return ""

    def _log_debug(message: str) -> None:
        if logger:
            try:
                logger(message)
            except Exception:
                pass

    try:
        top_resp = spotify_client._authorized_request(  # type: ignore[attr-defined]
            "GET",
            f"{spotify_client.API_BASE}/artists/{artist_id}/top-tracks",
            params={"market": "US"},
        )
    except Exception as exc:
        _log_debug(f"[Spotify S1] genre fallback: top-tracks lookup failed for artist_id={artist_id}: {exc}")
        return ""

    track_artist_ids: Set[str] = set()
    for track in top_resp.get("tracks", []) or []:
        for artist in track.get("artists", []) or []:
            aid = (artist.get("id") or "").strip()
            if aid:
                track_artist_ids.add(aid)

    if not track_artist_ids:
        return ""

    try:
        details = spotify_client.get_artists_details(list(track_artist_ids)[:50])
    except Exception as exc:
        _log_debug(f"[Spotify S1] genre fallback: artist details lookup failed for top-track artists of {artist_id}: {exc}")
        return ""

    genre_counts: Dict[str, int] = {}
    for artist in details.values():
        for genre in artist.get("genres") or []:
            cleaned = (genre or "").strip()
            if not cleaned:
                continue
            genre_counts[cleaned] = genre_counts.get(cleaned, 0) + 1

    if not genre_counts:
        return ""

    sorted_genres = sorted(genre_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    top = [name for name, _ in sorted_genres[:3]]
    primary = ", ".join(top)
    if primary:
        _log_debug(f"[Spotify S1] genre fallback: using top-track artist genres for {artist_id}: {primary}")
    return primary


def _most_common_playlist_genre(genre_pool: List[str]) -> str:
    """
    Lightweight fallback: return the most common genre seen so far in the playlist run.
    """
    if not genre_pool:
        return ""
    counts: Dict[str, int] = {}
    for genre in genre_pool:
        key = (genre or "").strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    sorted_genres = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return sorted_genres[0][0]


def _fetch_genres_from_lastfm(
    artist_name: str,
    api_key: Optional[str],
    logger: Optional[LoggerFn],
) -> str:
    """
    Fallback: ask Last.fm for top tags on the artist and treat them as genres.
    """
    if not artist_name or not api_key:
        return ""

    cache_key = artist_name.lower().strip()
    if cache_key in _LASTFM_GENRE_CACHE:
        return _LASTFM_GENRE_CACHE[cache_key]

    def _log_debug(message: str) -> None:
        if logger:
            try:
                logger(message)
            except Exception:
                pass

    params = {
        "method": "artist.getTopTags",
        "artist": artist_name,
        "api_key": api_key,
        "format": "json",
    }
    try:
        resp = requests.get("https://ws.audioscrobbler.com/2.0", params=params, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        _log_debug(f"[Spotify S1] genre fallback: Last.fm lookup failed for '{artist_name}': {exc}")
        return ""

    tags = (payload.get("toptags") or {}).get("tag") or []
    dedup: List[str] = []
    for tag in tags:
        name = (tag.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in ("seen live",):
            continue
        if name not in dedup:
            dedup.append(name)
        if len(dedup) >= 3:
            break

    primary = ", ".join(dedup)
    if primary:
        _log_debug(f"[Spotify S1] genre fallback: using Last.fm tags for '{artist_name}': {primary}")
        _LASTFM_GENRE_CACHE[cache_key] = primary
    return primary


def _resolve_directory_csv(filename: str, base_dir: str) -> Optional[str]:
    if not filename:
        return None
    candidates = []
    if os.path.isabs(filename):
        candidates.append(filename)
    else:
        candidates.append(os.path.join(base_dir, filename))
        candidates.append(os.path.join(SCRIPT_BASE_DIR, filename))
        candidates.append(os.path.abspath(filename))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _extract_first_value(row: Dict[str, Any], columns: Iterable[str]) -> str:
    for column in columns:
        value = row.get(column) if isinstance(row, dict) else None
        if value is None:
            continue
        text = str(value).strip()
        if text:
            cleaned = text.split("|")[0].split(";")[0].strip()
            if cleaned:
                return cleaned
    return ""


def _load_directory_genre_map(base_dir: str) -> Dict[str, str]:
    genre_map: Dict[str, str] = {}
    seen_paths: Set[str] = set()
    for entry in DIRECTORY_GENRE_FILES:
        if not entry:
            continue
        if len(entry) == 2:
            filename, genre_columns = entry  # type: ignore
        else:
            filename = entry[0]
            genre_columns = entry[1:]
        path = _resolve_directory_csv(filename, base_dir)
        if not path:
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    artist = (row.get("Artist Name") or row.get("artist") or "").strip()
                    if not artist:
                        continue
                    key = _normalize_artist_name(artist)
                    if not key or key in genre_map:
                        continue
                    genre_value = _extract_first_value(row, genre_columns)
                    if genre_value:
                        genre_map[key] = genre_value
        except Exception:
            continue
    return genre_map


def _apply_directory_genres(rows: List[Row], genre_map: Dict[str, str]) -> None:
    if not rows or not genre_map:
        return
    for row in rows:
        current = (row.get("Primary Genre") or "").strip()
        if current:
            continue
        key = _normalize_artist_name(row.get("Artist Name"))
        if not key:
            continue
        genre_value = genre_map.get(key)
        if genre_value:
            row["Primary Genre"] = genre_value


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


def _select_track_for_artist(
    artist_id: str,
    artist_name: str,
    stub_info: Dict[str, Any],
    tracks_by_artist_id: Dict[str, List[Dict[str, Any]]],
    tracks_by_artist_name: Dict[str, List[Dict[str, Any]]],
    used_song_titles: Set[str],
    spotify_client: Optional[SpotifyClient],
    logger: Optional[LoggerFn],
) -> Tuple[str, str]:
    """
    Choose a track for the given artist based on playlist membership.
    Returns (track_name, playlist_label) with empty track_name when no match is found.
    """
    playlist_label = stub_info.get("playlist_label") or ""
    track_name = ""
    track_id = ""

    candidates: List[Dict[str, Any]] = []

    if artist_id:
        candidates = tracks_by_artist_id.get(artist_id, []) or []
    if (not candidates) and artist_name:
        normalized = _normalize_artist_name(artist_name)
        if normalized and not candidates:
            candidates = tracks_by_artist_name.get(normalized, []) or []

    selected_track: Dict[str, Any] = {}
    if candidates:
        # Prefer tracks where this artist is the primary credited artist, otherwise first in playlist order.
        sorted_candidates = sorted(candidates, key=lambda c: c.get("order", 0))
        primaries = [c for c in sorted_candidates if c.get("primary_for_artist")]
        ordered_candidates = primaries or sorted_candidates
        for candidate in ordered_candidates:
            norm_title = _normalize_track_title(candidate.get("track_name") or "")
            if norm_title and norm_title not in used_song_titles:
                selected_track = candidate
                break
        if not selected_track:
            # Try API fallback for a unique title before reusing a duplicate.
            if logger:
                try:
                    logger(
                        f"[Spotify S1] playlist candidates for artist '{artist_name}' are duplicates; trying API fallback for a unique track"
                    )
                except Exception:
                    pass
            api_track = _find_unused_track_via_api(
                spotify_client=spotify_client,
                artist_id=artist_id or "",
                used_song_titles=used_song_titles,
                logger=logger,
            )
            if api_track:
                selected_track = {
                    "track_id": api_track.get("id") or "",
                    "track_name": api_track.get("name") or "",
                    "artist_id": artist_id,
                    "artist_name": artist_name,
                    "playlist_label": stub_info.get("playlist_label") or "",
                    "order": ordered_candidates[0].get("order", 0) if ordered_candidates else 0,
                    "primary_for_artist": True,
                }
        if not selected_track and ordered_candidates:
            selected_track = ordered_candidates[0]

    if selected_track:
        track_name = selected_track.get("track_name") or ""
        track_id = selected_track.get("track_id") or ""
        playlist_label = selected_track.get("playlist_label") or playlist_label
        norm_title = _normalize_track_title(track_name)
        already_used = norm_title in used_song_titles if norm_title else False
        if norm_title:
            used_song_titles.add(norm_title)
        else:
            norm_title = ""
    else:
        # HTML fallback may only provide a sample track name without full track metadata.
        track_name = stub_info.get("sample_track") or ""
        norm_title = _normalize_track_title(track_name)
        already_used = norm_title in used_song_titles if norm_title else False
        if norm_title:
            used_song_titles.add(norm_title)

    if logger:
        prefix = "[Spotify S1]"
        if track_name:
            logger(
                f"{prefix} selected track '{track_name}' for artist '{artist_name}' "
                f"(artist_id={artist_id or 'n/a'}, track_id={track_id or 'n/a'})"
            )
            if already_used:
                try:
                    logger(
                        f"{prefix} note: all candidate tracks for artist '{artist_name}' already used; reusing title '{track_name}'"
                    )
                except Exception:
                    pass
        else:
            logger(
                f"{prefix} no track found for artist '{artist_name}' "
                f"(artist_id={artist_id or 'n/a'}), leaving Song Title blank"
            )

    return track_name, playlist_label


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
    tracks_by_artist_id: Dict[str, List[Dict[str, Any]]] = {}
    tracks_by_artist_name: Dict[str, List[Dict[str, Any]]] = {}
    used_song_titles: Set[str] = set()
    total_tracks_scanned = 0
    track_order = 0

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
            msg = str(exc)
            is_404 = "404" in msg
            if logger:
                if is_404:
                    logger(f"[Spotify] playlist tracks 404 for {playlist_id_clean} - falling back to HTML")
                else:
                    logger(f"[Spotify] API playlist fetch failed for {playlist_id_clean}: {exc}")
            if _should_use_html_fallback(exc) or is_404:
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
                    if not artist_id:
                        continue
                    # Map HTML rows as track candidates so Song Title selection remains per-artist.
                    track_order += 1
                    track_name = (artist.get("track_name") or "").strip()
                    track_info = {
                        "track_id": "",
                        "track_name": track_name,
                        "artist_id": artist_id,
                        "artist_name": artist.get("artist_name") or "",
                        "playlist_label": fallback_label,
                        "order": artist.get("track_position") or track_order,
                        "primary_for_artist": bool(artist.get("is_primary")),
                    }
                    tracks_by_artist_id.setdefault(artist_id, []).append(track_info)
                    normalized_html_name = _normalize_artist_name(artist.get("artist_name"))
                    if normalized_html_name:
                        tracks_by_artist_name.setdefault(normalized_html_name, []).append(track_info)
                    if artist_id in artists_by_id:
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
            track_order += 1
            track_name = track_obj.get("name", "")
            artists = track_obj.get("artists") or []
            if not artists:
                continue
            track_id = track_obj.get("id") or ""

            # Map tracks to all participating artists to avoid cross-artist carry-over.
            for track_artist in artists:
                ta_id = (track_artist or {}).get("id") or ""
                ta_name = (track_artist or {}).get("name") or ""
                normalized_name = _normalize_artist_name(ta_name)
                track_info = {
                    "track_id": track_id,
                    "track_name": track_name,
                    "artist_id": ta_id,
                    "artist_name": ta_name,
                    "playlist_label": playlist_label,
                    "order": track_order,
                    "primary_for_artist": track_artist is artists[0],
                }
                if ta_id:
                    tracks_by_artist_id.setdefault(ta_id, []).append(track_info)
                if normalized_name:
                    tracks_by_artist_name.setdefault(normalized_name, []).append(track_info)

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
    playlist_genre_pool: List[str] = []
    lastfm_api_key = os.getenv("LASTFM_API_KEY") or ""

    for idx, (artist_id, stub_info) in enumerate(artists_by_id.items(), start=1):
        artist_payload = artist_details.get(artist_id, {})
        stub = stub_info.get("artist_stub") or {}
        artist_name = artist_payload.get("name") or stub.get("name") or "Unknown Artist"
        spotify_url = (artist_payload.get("external_urls") or {}).get("spotify") or stub_info.get("artist_url") or ""
        genres = artist_payload.get("genres") or []
        primary_genre = ", ".join(genres[:3])
        if not primary_genre:
            primary_genre = _fetch_additional_genres(spotify_client=client, artist_id=artist_id, logger=logger)
        if not primary_genre:
            primary_genre = _fetch_genres_from_top_tracks(spotify_client=client, artist_id=artist_id, logger=logger)
        if not primary_genre:
            primary_genre = _fetch_genres_from_lastfm(artist_name=artist_name, api_key=lastfm_api_key, logger=logger)
        if not primary_genre:
            primary_genre = _most_common_playlist_genre(playlist_genre_pool)
        track_name = ""
        playlist_label = stub_info.get("playlist_label") or ""

        track_name, playlist_label = _select_track_for_artist(
            artist_id=artist_id,
            artist_name=artist_name,
            stub_info=stub_info,
            tracks_by_artist_id=tracks_by_artist_id,
            tracks_by_artist_name=tracks_by_artist_name,
            used_song_titles=used_song_titles,
            spotify_client=client,
            logger=logger,
        )

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
        for token in (primary_genre or "").split(","):
            if token.strip():
                playlist_genre_pool.append(token.strip())

        if progress_callback:
            try:
                progress_callback(idx, total_artists)
            except Exception:
                pass

    if logger:
        logger(f"[Spotify] Discovery complete — returning {len(rows)} rows.")

    directory_base = params.get("directory_base") or os.getcwd()
    genre_map = _load_directory_genre_map(directory_base)
    if genre_map and logger:
        logger(f"[Spotify] Applied genre fallbacks for {len(genre_map)} artists from existing CSVs.")
    _apply_directory_genres(rows, genre_map)

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
