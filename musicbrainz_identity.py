"""Shadow-only MusicBrainz identity resolution for Spotify-origin rows."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import pandas as pd
import requests

from source_scheduler import is_spotify_origin_row


MUSICBRAINZ_API_BASE = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_RESOLVER_VERSION = "musicbrainz_spotify_identity_v1"
MUSICBRAINZ_SHADOW_COLUMNS: Tuple[str, ...] = (
    "MusicBrainz_MBID",
    "MusicBrainz_Status",
    "Identity_Match_Method",
    "Identity_Confidence",
    "Identity_Evidence_JSON",
)
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def musicbrainz_shadow_enabled() -> bool:
    return _env_flag("MUSICBRAINZ_SHADOW_ENABLED")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def _spotify_artist_id_from_value(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    if raw.lower().startswith("spotify:artist:"):
        raw = raw.split(":", 2)[-1]
    elif "://" in raw or "spotify.com" in raw.lower():
        try:
            parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        if host not in {"open.spotify.com", "play.spotify.com"}:
            return ""
        parts = [part for part in (parsed.path or "").split("/") if part]
        for index, part in enumerate(parts[:-1]):
            if part.lower() == "artist":
                raw = parts[index + 1]
                break
        else:
            return ""
    raw = raw.strip().strip("/")
    if not raw or not _SPOTIFY_ID_RE.fullmatch(raw):
        return ""
    return raw


def normalize_spotify_artist_identity(
    artist_id: Any = "",
    artist_url: Any = "",
) -> Tuple[str, str]:
    """Return a preferred Spotify artist ID and canonical artist URL."""
    normalized_id = _spotify_artist_id_from_value(artist_id)
    if not normalized_id:
        normalized_id = _spotify_artist_id_from_value(artist_url)
    if not normalized_id:
        return ("", "")
    return (normalized_id, f"https://open.spotify.com/artist/{normalized_id}")


def spotify_identity_from_row(row: Mapping[str, Any]) -> Tuple[str, str]:
    artist_id = row.get("Spotify_Artist_ID", "") or row.get("Spotify Artist ID", "")
    artist_url = row.get("Spotify_URL", "") or row.get("Spotify URL", "")
    return normalize_spotify_artist_identity(artist_id, artist_url)


@dataclass(frozen=True)
class MusicBrainzIdentityResult:
    spotify_artist_id: str
    spotify_artist_url: str
    status: str
    mbid: str = ""
    match_method: str = ""
    identity_confidence: Optional[float] = None
    artist: Mapping[str, Any] = field(default_factory=dict)
    relationships: Mapping[str, Any] = field(default_factory=dict)
    candidate_mbids: Tuple[str, ...] = ()
    error: str = ""

    def evidence(self) -> Dict[str, Any]:
        musicbrainz: Dict[str, Any] = {
            "mbid": self.mbid,
            "status": self.status,
            "match_method": self.match_method,
            "relationships": dict(self.relationships),
        }
        if self.artist:
            musicbrainz["artist"] = dict(self.artist)
        if self.candidate_mbids:
            musicbrainz["candidate_mbids"] = list(self.candidate_mbids)
        if self.error:
            musicbrainz["error"] = self.error
        return {
            "resolver_version": MUSICBRAINZ_RESOLVER_VERSION,
            "spotify": {
                "artist_id": self.spotify_artist_id,
                "artist_url": self.spotify_artist_url,
            },
            "musicbrainz": musicbrainz,
        }

    def shadow_fields(self) -> Dict[str, str]:
        confidence = "" if self.identity_confidence is None else str(self.identity_confidence)
        return {
            "MusicBrainz_MBID": self.mbid,
            "MusicBrainz_Status": self.status,
            "Identity_Match_Method": self.match_method,
            "Identity_Confidence": confidence,
            "Identity_Evidence_JSON": json.dumps(
                self.evidence(),
                sort_keys=True,
                separators=(",", ":"),
            ),
        }


class MusicBrainzClient:
    """Conservative MusicBrainz client with per-Spotify-identity caching."""

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        user_agent: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        min_interval_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        session: Optional[requests.Session] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        requested_enabled = musicbrainz_shadow_enabled() if enabled is None else bool(enabled)
        configured_user_agent = os.getenv("MUSICBRAINZ_USER_AGENT") if user_agent is None else user_agent
        self.user_agent = _clean(configured_user_agent)
        self.enabled = bool(requested_enabled and self.user_agent)
        self.disabled_reason = "" if self.enabled else (
            "feature_disabled" if not requested_enabled else "missing_user_agent"
        )
        self.timeout_seconds = max(
            0.1,
            float(timeout_seconds if timeout_seconds is not None else os.getenv("MUSICBRAINZ_TIMEOUT_SECONDS", "10")),
        )
        self.min_interval_seconds = max(
            1.0,
            float(
                min_interval_seconds
                if min_interval_seconds is not None
                else os.getenv("MUSICBRAINZ_MIN_INTERVAL_SECONDS", "1.1")
            ),
        )
        self.max_retries = max(
            0,
            int(max_retries if max_retries is not None else os.getenv("MUSICBRAINZ_MAX_RETRIES", "1")),
        )
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": self.user_agent or "LeadMachine-MusicBrainz-Shadow/1.0",
            }
        )
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._last_request_at: Optional[float] = None
        self._cache: Dict[str, MusicBrainzIdentityResult] = {}
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        wait_seconds = self.min_interval_seconds - (self._clock() - self._last_request_at)
        if wait_seconds > 0:
            self._sleep(wait_seconds)

    def _request_json(self, path: str, params: Mapping[str, str]) -> Tuple[str, Any]:
        url = f"{MUSICBRAINZ_API_BASE}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=dict(params), timeout=self.timeout_seconds)
                self._last_request_at = self._clock()
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self._sleep(self.min_interval_seconds)
                    self._last_request_at = None
                    continue
                return ("error", f"network:{type(exc).__name__}")

            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code == 404:
                return ("not_found", None)
            if status_code in _TRANSIENT_HTTP_STATUSES and attempt < self.max_retries:
                retry_after = 0.0
                try:
                    retry_after = float(response.headers.get("Retry-After", "0") or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                self._sleep(max(self.min_interval_seconds, retry_after))
                self._last_request_at = None
                continue
            if status_code < 200 or status_code >= 300:
                return ("error", f"http:{status_code or 'unknown'}")
            try:
                payload = response.json()
            except (TypeError, ValueError):
                return ("error", "malformed_json")
            if not isinstance(payload, dict):
                return ("error", "malformed_payload")
            return ("ok", payload)
        return ("error", "retry_exhausted")

    def resolve(self, artist_id: Any = "", artist_url: Any = "") -> MusicBrainzIdentityResult:
        spotify_id, spotify_url = normalize_spotify_artist_identity(artist_id, artist_url)
        cache_key = spotify_url
        if cache_key and cache_key in self._cache:
            return self._cache[cache_key]

        if not self.enabled:
            result = MusicBrainzIdentityResult(
                spotify_id,
                spotify_url,
                "disabled",
                error=self.disabled_reason,
            )
        elif not spotify_url:
            result = MusicBrainzIdentityResult(spotify_id, spotify_url, "no_match")
        else:
            try:
                result = self._resolve_exact_relationship(spotify_id, spotify_url)
            except Exception as exc:  # Pipeline safety boundary.
                result = MusicBrainzIdentityResult(
                    spotify_id,
                    spotify_url,
                    "error",
                    error=f"unexpected:{type(exc).__name__}",
                )
        if cache_key:
            self._cache[cache_key] = result
        return result

    def _resolve_exact_relationship(
        self,
        spotify_id: str,
        spotify_url: str,
    ) -> MusicBrainzIdentityResult:
        request_status, payload = self._request_json(
            "url",
            {"resource": spotify_url, "inc": "artist-rels", "fmt": "json"},
        )
        if request_status == "not_found":
            return MusicBrainzIdentityResult(spotify_id, spotify_url, "no_match")
        if request_status != "ok":
            return MusicBrainzIdentityResult(spotify_id, spotify_url, "error", error=_clean(payload))

        candidate_mbids = self._extract_artist_mbids(payload)
        if not candidate_mbids:
            return MusicBrainzIdentityResult(spotify_id, spotify_url, "no_match")
        if len(candidate_mbids) > 1:
            return MusicBrainzIdentityResult(
                spotify_id,
                spotify_url,
                "ambiguous",
                candidate_mbids=tuple(candidate_mbids),
            )

        mbid = candidate_mbids[0]
        artist_status, artist_payload = self._request_json(
            f"artist/{mbid}",
            {"inc": "url-rels", "fmt": "json"},
        )
        if artist_status != "ok":
            return MusicBrainzIdentityResult(
                spotify_id,
                spotify_url,
                "error",
                mbid=mbid,
                match_method="spotify_url_relationship",
                error=_clean(artist_payload) or artist_status,
            )
        if _clean(artist_payload.get("id")) != mbid:
            return MusicBrainzIdentityResult(
                spotify_id,
                spotify_url,
                "error",
                error="artist_payload_mismatch",
            )
        artist = {
            key: artist_payload.get(key)
            for key in ("name", "sort-name", "disambiguation", "type", "country")
            if artist_payload.get(key) not in (None, "")
        }
        return MusicBrainzIdentityResult(
            spotify_id,
            spotify_url,
            "matched",
            mbid=mbid,
            match_method="spotify_url_relationship",
            identity_confidence=1.0,
            artist=artist,
            relationships=self._extract_url_relationships(artist_payload),
        )

    @staticmethod
    def _extract_artist_mbids(payload: Mapping[str, Any]) -> List[str]:
        mbids = set()
        relations = payload.get("relations", [])
        if not isinstance(relations, list):
            return []
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            artist = relation.get("artist")
            if relation.get("target-type") != "artist" and not isinstance(artist, dict):
                continue
            mbid = _clean(artist.get("id") if isinstance(artist, dict) else relation.get("target"))
            if mbid:
                mbids.add(mbid)
        return sorted(mbids)

    @staticmethod
    def _extract_url_relationships(payload: Mapping[str, Any]) -> Dict[str, List[Dict[str, str]]]:
        grouped: Dict[str, List[Dict[str, str]]] = {}
        relations = payload.get("relations", [])
        if not isinstance(relations, list):
            return grouped
        seen = set()
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            url_info = relation.get("url")
            resource = _clean(url_info.get("resource") if isinstance(url_info, dict) else "")
            if not resource:
                continue
            relation_type = _clean(relation.get("type"))
            category = MusicBrainzClient._relationship_category(resource, relation_type)
            key = (category, resource, relation_type)
            if key in seen:
                continue
            seen.add(key)
            grouped.setdefault(category, []).append({"url": resource, "type": relation_type})
        for category in grouped:
            grouped[category].sort(key=lambda item: (item["url"], item["type"]))
        return dict(sorted(grouped.items()))

    @staticmethod
    def _relationship_category(resource: str, relation_type: str) -> str:
        try:
            host = (urlsplit(resource).hostname or "").lower()
        except ValueError:
            host = ""
        if host.endswith("bandcamp.com"):
            return "bandcamp"
        if host.endswith("soundcloud.com"):
            return "soundcloud"
        if host.endswith("facebook.com"):
            return "facebook"
        if host.endswith("instagram.com"):
            return "instagram"
        if host.endswith("youtube.com") or host == "youtu.be":
            return "youtube"
        if host.endswith("discogs.com"):
            return "discogs"
        if relation_type.lower() in {"official homepage", "official site"}:
            return "official_homepage"
        return "other"


def apply_musicbrainz_shadow(
    dataframe: pd.DataFrame,
    client: MusicBrainzClient,
    logger: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    """Write only compact MusicBrainz diagnostics for Spotify-origin rows."""
    result_df = dataframe.copy()
    for column in MUSICBRAINZ_SHADOW_COLUMNS:
        if column not in result_df.columns:
            result_df[column] = ""
        else:
            result_df[column] = result_df[column].fillna("").astype(str)

    for row_index in result_df.index:
        row = result_df.loc[row_index]
        if not is_spotify_origin_row(row):
            continue
        spotify_id, spotify_url = spotify_identity_from_row(row)
        try:
            resolution = client.resolve(spotify_id, spotify_url)
        except Exception as exc:
            resolution = MusicBrainzIdentityResult(
                spotify_id,
                spotify_url,
                "error",
                error=f"unexpected:{type(exc).__name__}",
            )
        for column, value in resolution.shadow_fields().items():
            result_df.at[row_index, column] = value
        if logger:
            try:
                logger(
                    f"[MusicBrainz Shadow] row={row_index} status={resolution.status} "
                    f"spotify_artist_id={spotify_id or '<missing>'} mbid={resolution.mbid or '<none>'}"
                )
            except Exception:
                pass
    return result_df


def run_musicbrainz_shadow_csv(
    input_csv_path: str,
    output_csv_path: str,
    *,
    client: Optional[MusicBrainzClient] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> str:
    dataframe = pd.read_csv(input_csv_path, dtype=str, keep_default_na=False).fillna("")
    resolver = client or MusicBrainzClient(enabled=True, logger=logger)
    enriched = apply_musicbrainz_shadow(dataframe, resolver, logger=logger)
    Path(output_csv_path).parent.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
    return output_csv_path
