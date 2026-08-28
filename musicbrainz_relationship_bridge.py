"""Conservative policy for operational MusicBrainz relationship candidates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence, Tuple


KNOWN_PROFILE_ACCEPTED = "accepted"
KNOWN_PROFILE_IDENTITY_REJECTED = "identity_rejected"
KNOWN_PROFILE_CHALLENGE_UNAVAILABLE = "challenge_unavailable"
KNOWN_PROFILE_ERROR = "error"


def musicbrainz_relationship_bridge_enabled() -> bool:
    raw = str(os.getenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def _row_value(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean(row.get(key, ""))
        if value:
            return value
    return ""


def _alias_names(artist: Mapping[str, Any]) -> Tuple[str, ...]:
    aliases = artist.get("aliases", [])
    if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
        return ()
    names = []
    for alias in aliases:
        name = _clean(alias.get("name", "")) if isinstance(alias, Mapping) else _clean(alias)
        if name and name not in names:
            names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class RelationshipBridgePlan:
    eligible: bool
    reason: str
    row_artist: str = ""
    musicbrainz_artist: str = ""
    bandcamp_urls: Tuple[str, ...] = ()
    soundcloud_urls: Tuple[str, ...] = ()


@dataclass(frozen=True)
class KnownProfileFetchResult:
    status: str
    payload: Any = None
    reason: str = ""


def build_relationship_bridge_plan(
    row: Mapping[str, Any],
    *,
    normalize_name: Callable[[str], str],
    canonicalize_bandcamp: Callable[[str], str],
    canonicalize_soundcloud: Callable[[str], str],
    valid_bandcamp: Callable[[str], bool],
    valid_soundcloud: Callable[[str], bool],
) -> RelationshipBridgePlan:
    """Return validated, deduplicated known-profile candidates without mutating the row."""
    if _row_value(row, "MusicBrainz_Status") != "matched":
        return RelationshipBridgePlan(False, "musicbrainz_not_matched")
    if _row_value(row, "Identity_Match_Method") != "spotify_url_relationship":
        return RelationshipBridgePlan(False, "identity_method_not_exact")
    raw_evidence = _row_value(row, "Identity_Evidence_JSON")
    try:
        evidence = json.loads(raw_evidence)
    except (TypeError, ValueError):
        return RelationshipBridgePlan(False, "invalid_identity_evidence")
    musicbrainz = evidence.get("musicbrainz", {}) if isinstance(evidence, Mapping) else {}
    if not isinstance(musicbrainz, Mapping) or musicbrainz.get("status") != "matched":
        return RelationshipBridgePlan(False, "evidence_not_matched")
    if musicbrainz.get("match_method") != "spotify_url_relationship":
        return RelationshipBridgePlan(False, "evidence_method_not_exact")
    artist = musicbrainz.get("artist", {})
    if not isinstance(artist, Mapping):
        return RelationshipBridgePlan(False, "missing_musicbrainz_artist")
    row_artist = _row_value(row, "Artist Name", "Artist", "artist_name")
    mb_artist = _clean(artist.get("name", ""))
    row_key = normalize_name(row_artist)
    accepted_names = {normalize_name(mb_artist)}
    accepted_names.update(normalize_name(alias) for alias in _alias_names(artist))
    accepted_names.discard("")
    if not row_key or row_key not in accepted_names:
        return RelationshipBridgePlan(False, "row_musicbrainz_identity_mismatch", row_artist, mb_artist)

    relationships = musicbrainz.get("relationships", {})
    relationships = relationships if isinstance(relationships, Mapping) else {}

    def _candidates(
        platform: str,
        canonicalize: Callable[[str], str],
        validate: Callable[[str], bool],
    ) -> Tuple[str, ...]:
        entries = relationships.get(platform, [])
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            return ()
        seen = set()
        output = []
        for entry in entries:
            raw_url = entry.get("url", "") if isinstance(entry, Mapping) else ""
            canonical = canonicalize(_clean(raw_url))
            if not canonical or not validate(canonical) or canonical in seen:
                continue
            seen.add(canonical)
            output.append(canonical)
        return tuple(output)

    return RelationshipBridgePlan(
        True,
        "exact_name" if normalize_name(mb_artist) == row_key else "exact_alias",
        row_artist,
        mb_artist,
        _candidates("bandcamp", canonicalize_bandcamp, valid_bandcamp),
        _candidates("soundcloud", canonicalize_soundcloud, valid_soundcloud),
    )
