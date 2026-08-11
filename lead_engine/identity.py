"""Pure, versioned identity normalization for Lead Engine contracts only."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import unquote, urlsplit, urlunsplit

IDENTITY_VERSION = "v1"
SOURCE_OCCURRENCE_ID_PREFIX = f"le:source-occurrence:{IDENTITY_VERSION}"
LEAD_ID_PREFIX = f"le:lead:{IDENTITY_VERSION}"


class InsufficientIdentityEvidence(ValueError):
    """Raised when a row has no stable evidence from which to derive an ID."""


@dataclass(frozen=True)
class IdentityEvidence:
    source_type: str
    kind: str
    value: str
    strength: str


def normalize_text(value: object, *, comparison: bool = False) -> str:
    """Normalize Unicode and whitespace, optionally producing a casefolded key."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join(text.split())
    return text.casefold() if comparison else text


def normalize_source_type(value: object) -> str:
    text = normalize_text(value, comparison=True)
    compact = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if "unearthed" in compact:
        return "triple_j_unearthed"
    if "bandcamp" in compact:
        return "bandcamp"
    if "soundcloud" in compact:
        return "soundcloud"
    if "spotify" in compact:
        return "spotify"
    if "lastfm" in compact or "last_fm" in compact:
        return "lastfm"
    if "festival" in compact:
        return "festival"
    return compact


def canonicalize_url(value: object) -> str:
    """Conservatively normalize an HTTP(S) URL without changing page meaning."""
    raw = normalize_text(value)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            scheme not in {"http", "https"}
            or not host
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or ".." in host
        ):
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    # Query parameters can identify distinct pages. Preserve them; fragments never
    # identify the server-side source observation.
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _url_parts(value: object):
    canonical = canonicalize_url(value)
    if not canonical:
        return None
    return urlsplit(canonical)


def _path_segments(value: object) -> tuple[str, ...]:
    parsed = _url_parts(value)
    if parsed is None:
        return ()
    return tuple(unquote(part) for part in parsed.path.split("/") if part)


def _spotify_id(source_native_id: object, source_url: object) -> str:
    explicit = normalize_text(source_native_id)
    if explicit:
        return explicit
    parsed = _url_parts(source_url)
    segments = _path_segments(source_url)
    if parsed and parsed.hostname in {"open.spotify.com", "www.open.spotify.com"}:
        if len(segments) >= 2 and segments[0].casefold() == "artist":
            return segments[1]
    return ""


def _platform_identity(source_type: str, source_native_id: object, source_url: object) -> Optional[IdentityEvidence]:
    parsed = _url_parts(source_url)
    segments = _path_segments(source_url)
    host = (parsed.hostname or "").lower() if parsed else ""
    explicit_native_id = normalize_text(source_native_id)

    if source_type == "spotify":
        artist_id = _spotify_id(source_native_id, source_url)
        if artist_id:
            return IdentityEvidence(source_type, "spotify_artist_id", artist_id, "strong")
    elif explicit_native_id:
        return IdentityEvidence(source_type, "source_native_id", explicit_native_id, "strong")

    if source_type == "bandcamp" and parsed:
        if host.endswith(".bandcamp.com") and host not in {"www.bandcamp.com", "bandcamp.com"}:
            return IdentityEvidence(source_type, "bandcamp_artist_host", host, "strong")
        if host and segments:
            return IdentityEvidence(source_type, "bandcamp_profile_url", canonicalize_url(source_url), "strong")
    elif source_type == "soundcloud" and host in {"soundcloud.com", "www.soundcloud.com"} and segments:
        excluded = {"charts", "discover", "search", "stream", "you"}
        handle = segments[0].casefold()
        if handle not in excluded:
            return IdentityEvidence(source_type, "soundcloud_handle", handle, "strong")
    elif source_type == "triple_j_unearthed" and host:
        lowered = tuple(segment.casefold() for segment in segments)
        if "artist" in lowered:
            index = lowered.index("artist")
            if index + 1 < len(segments):
                return IdentityEvidence(source_type, "unearthed_artist_slug", segments[index + 1].casefold(), "strong")
    elif source_type == "lastfm" and host in {"last.fm", "www.last.fm"} and segments:
        lowered = tuple(segment.casefold() for segment in segments)
        if "music" in lowered:
            index = lowered.index("music")
            if index + 1 < len(segments):
                artist_key = normalize_text(segments[index + 1].replace("+", " "), comparison=True)
                return IdentityEvidence(source_type, "lastfm_artist_path", artist_key, "strong")
    return None


def select_identity_evidence(
    *,
    source_type: object,
    source_native_id: object = "",
    source_url: object = "",
    artist_name_raw: object = "",
    location_raw: object = "",
    source_directory: object = "",
) -> IdentityEvidence:
    """Select the strongest available source evidence, with explicit weak fallbacks."""
    normalized_source = normalize_source_type(source_type)
    platform = _platform_identity(normalized_source, source_native_id, source_url)
    if platform is not None:
        return platform

    canonical_url = canonicalize_url(source_url)
    artist_key = normalize_text(artist_name_raw, comparison=True)
    location_key = normalize_text(location_raw, comparison=True)
    directory_key = normalize_text(source_directory, comparison=True)

    if normalized_source == "festival" and canonical_url and artist_key:
        return IdentityEvidence(
            normalized_source,
            "festival_lineup_url_and_artist_name",
            _canonical_json({"artist": artist_key, "lineup_url": canonical_url}),
            "weak",
        )
    if canonical_url:
        return IdentityEvidence(normalized_source, "source_url", canonical_url, "strong")
    if artist_key and (normalized_source or directory_key):
        # This is deliberately marked weak: a name/location/directory tuple is an
        # observation fallback, never a claim about a canonical real-world entity.
        return IdentityEvidence(
            normalized_source,
            "artist_location_source_fallback",
            _canonical_json({"artist": artist_key, "directory": directory_key, "location": location_key}),
            "weak",
        )
    raise InsufficientIdentityEvidence("source occurrence requires a source URL/native ID or artist plus source context")


def source_occurrence_id(evidence: IdentityEvidence) -> str:
    payload = {
        "identity_version": IDENTITY_VERSION,
        "kind": evidence.kind,
        "source_type": evidence.source_type,
        "value": evidence.value,
    }
    namespace = evidence.source_type or "unspecified"
    return f"{SOURCE_OCCURRENCE_ID_PREFIX}:{namespace}:{_digest(payload)}"


def lead_id(source_occurrence_identifier: str) -> str:
    return f"{LEAD_ID_PREFIX}:{_digest({'source_occurrence_id': source_occurrence_identifier})}"


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
