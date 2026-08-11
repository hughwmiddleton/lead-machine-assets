"""Serializable, immutable Lead Engine V1 contract types."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

CONTRACT_SCHEMA_VERSION = "lead-engine-contract/v1"


class _SerializableContract:
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SourceOccurrence(_SerializableContract):
    """One source observation, not a canonical real-world artist identity."""

    source_occurrence_id: str
    source_type: str
    identity_kind: str
    identity_strength: str
    identity_value: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    source_name: str = ""
    source_url: str = ""
    source_directory: str = ""
    source_job: str = ""
    source_native_id: str = ""
    artist_name_raw: str = ""
    location_raw: str = ""
    discovered_at: str = ""
    run_id: str = ""
    job_id: str = ""


@dataclass(frozen=True)
class Evidence(_SerializableContract):
    """Inspectable evidence for one fact, preserving its original scalar value."""

    fact_name: str
    value: Optional[str]
    source_occurrence_id: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    source_type: str = ""
    source_url: str = ""
    evidence_type: str = "row_field"
    observed_at: str = ""
    confidence_class: str = ""
    extraction_method: str = ""


@dataclass(frozen=True)
class LeadRecord(_SerializableContract):
    """A normalized record derived from a source occurrence.

    V1 is deliberately one-to-one with its primary source occurrence. No
    canonical_entity_id is present because entity resolution is out of scope.
    """

    lead_id: str
    source_occurrence_ids: Tuple[str, ...]
    schema_version: str = CONTRACT_SCHEMA_VERSION
    artist_name_normalized: str = ""
    location_normalized: str = ""
    country_normalized: str = ""
    email: str = ""
    spotify_artist_id: str = ""
    spotify_url: str = ""
    bandcamp_url: str = ""
    soundcloud_url: str = ""
    lastfm_url: str = ""
    final_status: str = ""
    needs_review: str = ""
    provenance: Tuple[Evidence, ...] = ()
