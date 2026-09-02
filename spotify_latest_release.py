"""Shared, read-only Spotify latest-release selection for accepted artist rows."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from spotify_client import SpotifyClient


_SPOTIFY_ARTIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_UNRELATED_RELEASE_RE = re.compile(
    r"\b(?:karaoke(?: version)?|tribute to|a tribute|in the style of)\b", re.I
)


def extract_spotify_artist_identity(value: Any) -> Tuple[str, str]:
    """Return a strict Spotify artist ID and canonical URL, or two blanks.

    Only exact ``spotify:artist:<id>`` URIs and open/play Spotify artist URLs
    are accepted. Spotify artist IDs are 22-character base62 identifiers.
    Extra path components, user-info, ports, fragments, and non-artist routes
    fail closed so malformed native links cannot reach the API.
    """
    raw = str(value or "").strip()
    if not raw:
        return "", ""

    artist_id = ""
    if raw.lower().startswith("spotify:artist:"):
        parts = raw.split(":")
        if len(parts) == 3 and parts[0].lower() == "spotify" and parts[1].lower() == "artist":
            artist_id = parts[2]
    else:
        try:
            parsed = urlsplit(raw)
            has_forbidden_authority = bool(parsed.username or parsed.password or parsed.port)
        except ValueError:
            return "", ""
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or has_forbidden_authority
            or (parsed.hostname or "").lower() not in {"open.spotify.com", "play.spotify.com"}
            or parsed.fragment
        ):
            return "", ""
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0].lower() != "artist":
            return "", ""
        artist_id = parts[1]

    if not _SPOTIFY_ARTIST_ID_RE.fullmatch(artist_id):
        return "", ""
    return artist_id, f"https://open.spotify.com/artist/{artist_id}"


def _release_date_key(release: Mapping[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    """Return a deterministic lower-bound key respecting Spotify precision.

    ``year`` maps to January 1, ``month`` to the month's first day, and ``day``
    to its exact date. Greater declared precision wins an otherwise equal key.
    """
    raw = str(release.get("release_date") or "").strip()
    precision = str(release.get("release_date_precision") or "").strip().lower()
    formats = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}
    if precision not in formats:
        return None
    try:
        parsed = dt.datetime.strptime(raw, formats[precision]).date()
    except (TypeError, ValueError):
        return None
    rank = {"year": 1, "month": 2, "day": 3}[precision]
    return parsed.year, parsed.month, parsed.day, rank


def _artist_ids(items: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        str(item.get("id") or "").strip()
        for item in (items or [])
        if str(item.get("id") or "").strip()
    }


def _eligible_release(release: Mapping[str, Any], artist_id: str) -> bool:
    if not release.get("id") or not _release_date_key(release):
        return False
    release_type = str(release.get("album_type") or release.get("album_group") or "").lower()
    if release_type not in {"single", "album"}:
        return False
    if artist_id not in _artist_ids(release.get("artists") or []):
        return False
    return not _UNRELATED_RELEASE_RE.search(str(release.get("name") or ""))


def _release_sort_key(release: Mapping[str, Any]) -> Tuple[Any, ...]:
    date_key = _release_date_key(release) or (0, 0, 0, 0)
    release_type = str(release.get("album_type") or release.get("album_group") or "").lower()
    # For an equal represented date, prefer a single over an album/EP.
    single_rank = 1 if release_type == "single" else 0
    return (*date_key, single_rank, str(release.get("id") or ""))


def _select_artist_track(tracks: Iterable[Mapping[str, Any]], artist_id: str) -> str:
    """Choose the first artist-associated track in Spotify disc/track order."""
    eligible = [
        track
        for track in (tracks or [])
        if str(track.get("name") or "").strip()
        and artist_id in _artist_ids(track.get("artists") or [])
    ]
    eligible.sort(
        key=lambda track: (
            int(track.get("disc_number") or 1),
            int(track.get("track_number") or 0),
            str(track.get("id") or ""),
            str(track.get("name") or "").casefold(),
        )
    )
    return str(eligible[0].get("name") or "").strip() if eligible else ""


@dataclass(frozen=True)
class LatestReleaseResult:
    song_title: str = ""
    release_date: str = ""


def select_latest_artist_release(client: SpotifyClient, artist_id: str) -> LatestReleaseResult:
    """Select the newest eligible release and an actual track from that release.

    Releases must name the exact target artist as a release artist. Response
    order is ignored. Tied represented dates prefer singles. For an album/EP,
    the deterministic fallback is its first target-artist-associated track in
    disc number then track number order. If a selected release has no eligible
    track, the selector continues to the next-newest eligible release.
    """
    releases = [
        release
        for release in client.get_artist_releases(artist_id)
        if _eligible_release(release, artist_id)
    ]
    releases.sort(key=_release_sort_key, reverse=True)
    for release in releases:
        tracks = client.get_album_tracks(str(release.get("id") or ""))
        title = _select_artist_track(tracks, artist_id)
        if title:
            return LatestReleaseResult(title, str(release.get("release_date") or "").strip())
    return LatestReleaseResult()


class SpotifyLatestReleaseEnricher:
    """Graceful run-scoped Spotify enrichment with artist-ID result caching."""

    def __init__(
        self,
        client: SpotifyClient,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client = client
        self.logger = logger
        self._cache: Dict[str, LatestReleaseResult] = {}
        self.lookup_failures = 0
        self.successful_lookups = 0

    def lookup(self, artist_id: str) -> LatestReleaseResult:
        if artist_id in self._cache:
            return self._cache[artist_id]
        try:
            result = select_latest_artist_release(self.client, artist_id)
        except Exception as exc:
            self.lookup_failures += 1
            if self.logger:
                self.logger(f"[Spotify Latest Release] lookup failed artist_id={artist_id}: {exc}")
            result = LatestReleaseResult()
        if result.song_title and result.release_date:
            self.successful_lookups += 1
        self._cache[artist_id] = result
        return result
