"""Pure adapters from legacy Lead Machine row-shaped values to V1 contracts."""

from __future__ import annotations

import math
import re
from typing import Mapping, Sequence

from .contracts import Evidence, LeadRecord, SourceOccurrence
from .identity import (
    canonicalize_url,
    lead_id,
    normalize_source_type,
    normalize_text,
    select_identity_evidence,
    source_occurrence_id,
)


def _header_key(value: object) -> str:
    text = normalize_text(value, comparison=True).lstrip("\ufeff")
    return re.sub(r"[^a-z0-9]+", "", text)


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    value_type = type(value)
    if value_type.__name__ in {"NAType", "NaTType"} and value_type.__module__.startswith("pandas"):
        return True
    if isinstance(value, float):
        return math.isnan(value)
    try:
        result = value != value
        return bool(result) if isinstance(result, bool) else False
    except Exception:
        return False


def _raw_scalar(value: object) -> str:
    return "" if _is_missing(value) else str(value)


class _RowView:
    def __init__(self, row: Mapping[object, object]):
        # Copy references into a normalized-key lookup; never modify the input.
        self._values = {}
        for key, value in row.items():
            self._values.setdefault(_header_key(key), value)

    def get(self, aliases: Sequence[str]) -> str:
        for alias in aliases:
            key = _header_key(alias)
            if key in self._values and not _is_missing(self._values[key]):
                value = _raw_scalar(self._values[key])
                if value.strip():
                    return value
        return ""


ALIASES = {
    "artist": ("Artist Name", "Artist", "Band Name"),
    "location": ("Location",),
    "country": ("Country",),
    "source_directory": ("Source Directory", "Source_Directory", "Lead Source", "Lead_Source"),
    "source_name": ("Discovery Source", "Discovery_Source"),
    "source_url": ("Source URL", "Source_URL", "Profile URL", "profile_url", "artist_url"),
    "source_job": ("Source Job", "Source_Job", "__source_job"),
    "run_id": ("Run ID", "Run_ID", "run_id", "Import Batch", "Import_Batch"),
    "job_id": ("Job ID", "Job_ID", "job_id", "Source Job", "Source_Job", "__source_job"),
    "discovered_at": ("First Discovered Date", "First_Discovered_Date", "Date Added", "Date_Added"),
    "spotify_id": ("Spotify Artist ID", "Spotify_Artist_ID", "spotify_artist_id"),
    "source_native_id": ("Source Native ID", "Source_Native_ID", "source_native_id"),
    "spotify_url": ("Spotify URL", "Spotify_URL", "Spotify Link"),
    "bandcamp_url": ("Bandcamp URL", "Bandcamp_URL", "Bandcamp Link"),
    "soundcloud_url": ("SoundCloud URL", "SoundCloud_URL", "SoundCloud Link"),
    "lastfm_url": ("LastFM URL", "Last.fm URL", "LastFM_URL", "lastfm_url"),
    "email": ("Primary Email", "Primary_Email", "Email", "E-mail"),
    "final_status": ("final_status", "Final_Status", "Final Status"),
    "needs_review": ("Needs_Review", "Needs Review"),
}


def _source_type(view: _RowView) -> str:
    directory = view.get(ALIASES["source_directory"])
    name = view.get(ALIASES["source_name"])
    direct = normalize_source_type(directory or name)
    if direct:
        return direct
    urls = " ".join(
        view.get(ALIASES[key])
        for key in ("source_url", "spotify_url", "bandcamp_url", "soundcloud_url", "lastfm_url")
    ).casefold()
    inferred = normalize_source_type(urls)
    known_sources = {"triple_j_unearthed", "bandcamp", "soundcloud", "spotify", "lastfm", "festival"}
    return inferred if inferred in known_sources else ""


def _identity_url(view: _RowView, source_type: str) -> str:
    source_url = view.get(ALIASES["source_url"])
    if source_url:
        return source_url
    platform_alias = {
        "spotify": "spotify_url",
        "bandcamp": "bandcamp_url",
        "soundcloud": "soundcloud_url",
        "lastfm": "lastfm_url",
    }.get(source_type)
    return view.get(ALIASES[platform_alias]) if platform_alias else ""


def source_occurrence_from_row(row: Mapping[object, object]) -> SourceOccurrence:
    view = _RowView(row)
    source_type = _source_type(view)
    source_url_raw = _identity_url(view, source_type)
    source_native_id = view.get(ALIASES["source_native_id"])
    if source_type == "spotify" and not source_native_id:
        source_native_id = view.get(ALIASES["spotify_id"])
    artist_raw = view.get(ALIASES["artist"])
    location_raw = view.get(ALIASES["location"])
    directory_raw = view.get(ALIASES["source_directory"])
    identity = select_identity_evidence(
        source_type=source_type,
        source_native_id=source_native_id,
        source_url=source_url_raw,
        artist_name_raw=artist_raw,
        location_raw=location_raw,
        source_directory=directory_raw,
    )
    occurrence_id = source_occurrence_id(identity)
    return SourceOccurrence(
        source_occurrence_id=occurrence_id,
        source_type=source_type,
        identity_kind=identity.kind,
        identity_strength=identity.strength,
        identity_value=identity.value,
        source_name=view.get(ALIASES["source_name"]),
        # Preserve the row's source evidence verbatim. The separately exposed
        # identity_value contains the canonical form used for hashing.
        source_url=source_url_raw,
        source_directory=directory_raw,
        source_job=view.get(ALIASES["source_job"]),
        source_native_id=source_native_id,
        artist_name_raw=artist_raw,
        location_raw=location_raw,
        discovered_at=view.get(ALIASES["discovered_at"]),
        run_id=view.get(ALIASES["run_id"]),
        job_id=view.get(ALIASES["job_id"]),
    )


def _evidence(view: _RowView, occurrence: SourceOccurrence) -> tuple[Evidence, ...]:
    evidence = []
    representative_fields = (
        ("artist_name", "artist", "row_field"),
        ("location", "location", "row_field"),
        ("country", "country", "row_field"),
        ("email", "email", "row_field"),
        ("spotify_artist_id", "spotify_id", "source_native_id"),
        ("source_native_id", "source_native_id", "source_native_id"),
    )
    for fact_name, alias_key, evidence_type in representative_fields:
        raw = view.get(ALIASES[alias_key])
        if not raw:
            continue
        evidence.append(
            Evidence(
                fact_name=fact_name,
                value=raw,
                source_occurrence_id=occurrence.source_occurrence_id,
                source_type=occurrence.source_type,
                source_url=occurrence.source_url,
                evidence_type=evidence_type,
                observed_at=occurrence.discovered_at,
            )
        )
    return tuple(evidence)


def lead_record_from_row(row: Mapping[object, object]) -> LeadRecord:
    view = _RowView(row)
    occurrence = source_occurrence_from_row(row)
    return LeadRecord(
        lead_id=lead_id(occurrence.source_occurrence_id),
        source_occurrence_ids=(occurrence.source_occurrence_id,),
        artist_name_normalized=normalize_text(view.get(ALIASES["artist"]), comparison=True),
        location_normalized=normalize_text(view.get(ALIASES["location"]), comparison=True),
        country_normalized=normalize_text(view.get(ALIASES["country"]), comparison=True),
        email=view.get(ALIASES["email"]),
        spotify_artist_id=view.get(ALIASES["spotify_id"]),
        spotify_url=canonicalize_url(view.get(ALIASES["spotify_url"])),
        bandcamp_url=canonicalize_url(view.get(ALIASES["bandcamp_url"])),
        soundcloud_url=canonicalize_url(view.get(ALIASES["soundcloud_url"])),
        lastfm_url=canonicalize_url(view.get(ALIASES["lastfm_url"])),
        final_status=view.get(ALIASES["final_status"]),
        needs_review=view.get(ALIASES["needs_review"]),
        provenance=_evidence(view, occurrence),
    )
