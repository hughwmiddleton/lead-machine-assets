"""Pure Campaign Export Ledger contracts for the CSV custody boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Sequence, Tuple

from email_normalizer import normalize_email_value

from .adapters import source_occurrence_from_row
from .identity import InsufficientIdentityEvidence, canonicalize_url, lead_id as lead_id_from_occurrence

EXPORT_SCHEMA_VERSION = "lead-engine-campaign-export/v1"
EXPORT_ID_PREFIX = "le:campaign-export:v1"
EXPORT_ROW_ID_PREFIX = "le:campaign-export-row:v1"
ROW_FINGERPRINT_PREFIX = "le:outbound-row:v1"
CONTENT_FINGERPRINT_PREFIX = "le:export-content:v1"

DEFAULT_EXPORT_PROFILE = "woodpecker"
DEFAULT_EXPORT_PROFILE_VERSION = "campaign-prep-woodpecker/v1"
DEFAULT_DESTINATION_TYPE = "woodpecker_csv"


@dataclass(frozen=True)
class ExportedContactDestination:
    channel: str
    raw_value: str
    normalized_value: str
    contact_type: str = ""
    provenance_source_type: str = ""
    provenance_source_url: str = ""
    extraction_method: str = ""
    schema_version: str = EXPORT_SCHEMA_VERSION


@dataclass(frozen=True)
class ExportLineage:
    status: str
    lead_id: str = ""
    source_occurrence_id: str = ""
    resolution_method: str = ""
    reason: str = ""
    raw_lead_reference: str = ""
    raw_source_occurrence_reference: str = ""
    schema_version: str = EXPORT_SCHEMA_VERSION


@dataclass(frozen=True)
class ExportField:
    name: str
    raw_value: str
    normalized_value: str
    schema_version: str = EXPORT_SCHEMA_VERSION


@dataclass(frozen=True)
class CampaignExportRow:
    export_row_id: str
    export_id: str
    row_position: int
    row_fingerprint: str
    contact_destination: ExportedContactDestination
    lineage: ExportLineage
    exported_fields: Tuple[ExportField, ...]
    export_profile: str
    export_profile_version: str
    destination_type: str
    exported_at: str
    source_row_reference: str = ""
    schema_version: str = EXPORT_SCHEMA_VERSION

    @property
    def lead_id(self) -> str:
        return self.lineage.lead_id

    @property
    def source_occurrence_id(self) -> str:
        return self.lineage.source_occurrence_id

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class CampaignExport:
    export_id: str
    operation_reference: str
    created_at: str
    export_profile: str
    export_profile_version: str
    destination_type: str
    row_count: int
    content_fingerprint: str
    rows: Tuple[CampaignExportRow, ...]
    campaign_label: str = ""
    source_dataset_reference: str = ""
    output_filename: str = ""
    schema_version: str = EXPORT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


_OUTBOUND_FIELDS: Tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Email", ("Email", "emails", "Email_All", "Primary_Email", "All_Emails", "Primary Email", "All Emails"), "email"),
    ("First Name", ("First Name", "Contact_Name", "Artist"), "text"),
    ("Company", ("Company", "Organization", "Artist"), "text"),
    ("Artist", ("Artist", "Artist Name", "Artist_Name"), "text"),
    ("Location", ("Location", "location", "City", "city", "State", "state"), "text"),
    ("Song Title", ("Song Title", "Song_Title"), "text"),
    ("Sounds Like", ("Sounds Like", "Sounds_Like"), "text"),
    ("Website", ("Website",), "url"),
    ("Instagram", ("Instagram", "Instagram_URL"), "url"),
    ("Facebook", ("Facebook", "Facebook_URL"), "url"),
    ("Source URL", ("Source URL", "Source_URL", "Social Link"), "url"),
    ("Lead_Source", ("Lead_Source", "Lead Source"), "token"),
    ("Source_Directory", ("Source_Directory",), "token"),
    ("Source Directory", ("Source Directory", "Source_Directory"), "token"),
    ("Release Date", ("Release Date", "Release_Date"), "text"),
    ("Upload Date", ("Upload Date", "Upload_Date", "Date Added", "Date_Added"), "text"),
    ("Notes", ("Notes",), "text"),
    ("Recency_Bucket", ("Recency_Bucket", "Recency Bucket"), "token"),
)

_LINEAGE_NATIVE_KINDS = {
    "spotify_artist_id",
    "bandcamp_artist_host",
    "bandcamp_profile_url",
    "soundcloud_handle",
    "unearthed_artist_slug",
    "lastfm_artist_path",
    "source_native_id",
}


def _header_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    value_type = type(value)
    return value_type.__name__ in {"NAType", "NaTType"} and value_type.__module__.startswith("pandas")


def _raw(value: object) -> str:
    return "" if _is_missing(value) else str(value)


class _Row:
    def __init__(self, row: Mapping[object, object]):
        self._exact: dict[str, object] = {}
        self._normalized: dict[str, object] = {}
        for key, value in row.items():
            exact = unicodedata.normalize("NFKC", str(key or "")).strip().casefold()
            self._exact.setdefault(exact, value)
            self._normalized.setdefault(_header_key(key), value)

    def resolve(self, aliases: Sequence[str]) -> str:
        for alias in aliases:
            exact = unicodedata.normalize("NFKC", alias).strip().casefold()
            if exact in self._exact:
                return _raw(self._exact[exact])
        for alias in aliases:
            key = _header_key(alias)
            if key in self._normalized:
                return _raw(self._normalized[key])
        return ""


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _normalize_field(value: str, field_type: str) -> str:
    if field_type == "email":
        return normalize_email_value(value) or _normalize_text(value).casefold()
    if field_type == "url":
        return canonicalize_url(value) or _normalize_text(value)
    if field_type == "token":
        return _normalize_text(value).casefold()
    return _normalize_text(value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(prefix: str, payload: object) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def export_id(operation_reference: str) -> str:
    reference = _normalize_text(operation_reference)
    if not reference:
        raise ValueError("operation_reference is required to identify an intentional export")
    return _digest(EXPORT_ID_PREFIX, {"operation_reference": reference})


def export_row_id(export_identifier: str, row_position: int) -> str:
    if not str(export_identifier).startswith(f"{EXPORT_ID_PREFIX}:"):
        raise ValueError("export_identifier is not a Campaign Export ID")
    if int(row_position) < 1:
        raise ValueError("row_position must be one-based")
    return _digest(EXPORT_ROW_ID_PREFIX, {"export_id": export_identifier, "row_position": int(row_position)})


def _normalize_created_at(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("created_at must be supplied; the pure ledger does not read the system clock")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_reference(value: str, prefix: str) -> bool:
    return re.fullmatch(re.escape(prefix) + r":[0-9a-f]{64}", str(value or "")) is not None


def _resolve_lineage(row: Mapping[object, object], view: _Row) -> ExportLineage:
    raw_lead = view.resolve(("lead_id", "Lead ID", "Lead_ID"))
    raw_occurrence = view.resolve(("source_occurrence_id", "Source Occurrence ID", "Source_Occurrence_ID"))
    lead_valid = _valid_reference(raw_lead.strip(), "le:lead:v1")
    # Source occurrence IDs contain a source namespace, so validate separately.
    occurrence_valid = re.fullmatch(
        r"le:source-occurrence:v1:[a-z0-9_]+:[0-9a-f]{64}", raw_occurrence.strip()
    ) is not None
    if raw_lead or raw_occurrence:
        if lead_valid and occurrence_valid:
            expected_lead = lead_id_from_occurrence(raw_occurrence.strip())
            if raw_lead.strip() == expected_lead:
                return ExportLineage(
                    status="RESOLVED",
                    lead_id=raw_lead.strip(),
                    source_occurrence_id=raw_occurrence.strip(),
                    resolution_method="explicit_validated",
                    raw_lead_reference=raw_lead,
                    raw_source_occurrence_reference=raw_occurrence,
                )
            return ExportLineage(
                status="UNRESOLVED",
                resolution_method="explicit_conflict",
                reason="lead_id does not derive from source_occurrence_id",
                raw_lead_reference=raw_lead,
                raw_source_occurrence_reference=raw_occurrence,
            )
        if occurrence_valid and not raw_lead.strip():
            occurrence_id = raw_occurrence.strip()
            return ExportLineage(
                status="RESOLVED",
                lead_id=lead_id_from_occurrence(occurrence_id),
                source_occurrence_id=occurrence_id,
                resolution_method="lead_derived_from_explicit_occurrence",
                raw_source_occurrence_reference=raw_occurrence,
            )
        if lead_valid and not raw_occurrence.strip():
            return ExportLineage(
                status="PARTIAL",
                lead_id=raw_lead.strip(),
                resolution_method="explicit_lead_only",
                reason="source_occurrence_id unavailable",
                raw_lead_reference=raw_lead,
            )
        return ExportLineage(
            status="UNRESOLVED",
            resolution_method="invalid_explicit_reference",
            reason="provided lineage reference is invalid or incomplete",
            raw_lead_reference=raw_lead,
            raw_source_occurrence_reference=raw_occurrence,
        )

    try:
        occurrence = source_occurrence_from_row(row)
    except InsufficientIdentityEvidence:
        return ExportLineage(
            status="UNRESOLVED",
            resolution_method="legacy_row",
            reason="insufficient source identity evidence",
        )
    if occurrence.identity_strength != "strong" or occurrence.identity_kind not in _LINEAGE_NATIVE_KINDS:
        return ExportLineage(
            status="UNRESOLVED",
            resolution_method="legacy_row",
            reason=f"legacy identity evidence is not source-specific strong evidence: {occurrence.identity_kind}",
        )
    return ExportLineage(
        status="RESOLVED",
        lead_id=lead_id_from_occurrence(occurrence.source_occurrence_id),
        source_occurrence_id=occurrence.source_occurrence_id,
        resolution_method="derived_from_strong_legacy_source_evidence",
    )


def _contact_destination(view: _Row) -> ExportedContactDestination:
    raw_email = view.resolve(_OUTBOUND_FIELDS[0][1])
    return ExportedContactDestination(
        channel="email",
        raw_value=raw_email,
        normalized_value=normalize_email_value(raw_email),
        contact_type=view.resolve(("Contact Type", "Contact_Type")),
        provenance_source_type=view.resolve(("Email Source Type", "Email_Source_Type", "Email_Type")),
        provenance_source_url=view.resolve(("Email Source URL", "Email_Source_URL")),
        extraction_method=view.resolve(("Email Extract Method", "Email_Extract_Method")),
    )


def _export_fields(view: _Row) -> Tuple[ExportField, ...]:
    fields = []
    for name, aliases, field_type in _OUTBOUND_FIELDS:
        raw_value = view.resolve(aliases)
        fields.append(ExportField(name, raw_value, _normalize_field(raw_value, field_type)))
    return tuple(fields)


def _row_fingerprint(
    fields: Tuple[ExportField, ...],
    *,
    export_profile: str,
    export_profile_version: str,
    destination_type: str,
) -> str:
    payload = {
        "destination_type": destination_type,
        "export_profile": export_profile,
        "export_profile_version": export_profile_version,
        "fields": {field.name: field.normalized_value for field in fields},
    }
    return _digest(ROW_FINGERPRINT_PREFIX, payload)


def _content_fingerprint(rows: Sequence[CampaignExportRow], **profile: str) -> str:
    payload = {
        **profile,
        # Sorting makes CSV row ordering irrelevant while retaining duplicates.
        "row_fingerprints": sorted(row.row_fingerprint for row in rows),
    }
    return _digest(CONTENT_FINGERPRINT_PREFIX, payload)


def build_campaign_export(
    rows: Sequence[Mapping[object, object]],
    *,
    operation_reference: str,
    created_at: str,
    export_profile: str = DEFAULT_EXPORT_PROFILE,
    export_profile_version: str = DEFAULT_EXPORT_PROFILE_VERSION,
    destination_type: str = DEFAULT_DESTINATION_TYPE,
    campaign_label: str = "",
    source_dataset_reference: str = "",
    output_filename: str = "",
) -> CampaignExport:
    """Build an immutable ledger export without writing files or contacting a destination."""
    operation_id = export_id(operation_reference)
    exported_at = _normalize_created_at(created_at)
    profile = _normalize_text(export_profile)
    profile_version = _normalize_text(export_profile_version)
    destination = _normalize_text(destination_type)
    if not profile or not profile_version or not destination:
        raise ValueError("export profile, profile version, and destination type are required")

    export_rows = []
    for position, row in enumerate(rows, start=1):
        view = _Row(row)
        fields = _export_fields(view)
        fingerprint = _row_fingerprint(
            fields,
            export_profile=profile,
            export_profile_version=profile_version,
            destination_type=destination,
        )
        export_rows.append(
            CampaignExportRow(
                export_row_id=export_row_id(operation_id, position),
                export_id=operation_id,
                row_position=position,
                row_fingerprint=fingerprint,
                contact_destination=_contact_destination(view),
                lineage=_resolve_lineage(row, view),
                exported_fields=fields,
                export_profile=profile,
                export_profile_version=profile_version,
                destination_type=destination,
                exported_at=exported_at,
                source_row_reference=view.resolve(("Source Row Reference", "source_row_reference", "__row_id")),
            )
        )
    frozen_rows = tuple(export_rows)
    content_fingerprint = _content_fingerprint(
        frozen_rows,
        destination_type=destination,
        export_profile=profile,
        export_profile_version=profile_version,
    )
    return CampaignExport(
        export_id=operation_id,
        operation_reference=_normalize_text(operation_reference),
        created_at=exported_at,
        export_profile=profile,
        export_profile_version=profile_version,
        destination_type=destination,
        row_count=len(frozen_rows),
        content_fingerprint=content_fingerprint,
        rows=frozen_rows,
        campaign_label=str(campaign_label or ""),
        source_dataset_reference=str(source_dataset_reference or ""),
        output_filename=str(output_filename or ""),
    )


class CampaignExportLedger:
    """Small in-memory export/row index. It never deduplicates or emits events."""

    def __init__(self) -> None:
        self._exports: Dict[str, CampaignExport] = {}
        self._rows: Dict[str, CampaignExportRow] = {}

    def add_export(self, export: CampaignExport) -> CampaignExport:
        existing = self._exports.get(export.export_id)
        if existing is not None and existing != export:
            raise ValueError("export_id already exists with different operation content")
        for row in export.rows:
            if row.export_id != export.export_id:
                raise ValueError("export row references a different export_id")
            existing_row = self._rows.get(row.export_row_id)
            if existing_row is not None and existing_row != row:
                raise ValueError("export_row_id collision")
        self._exports[export.export_id] = export
        for row in export.rows:
            self._rows[row.export_row_id] = row
        return export

    def get_export(self, export_identifier: str) -> Optional[CampaignExport]:
        return self._exports.get(export_identifier)

    def get_row(self, export_row_identifier: str) -> Optional[CampaignExportRow]:
        return self._rows.get(export_row_identifier)

    def find_rows_by_lead_id(self, lead_identifier: str) -> Tuple[CampaignExportRow, ...]:
        return self._find(lambda row: row.lead_id == lead_identifier)

    def find_rows_by_email(self, email: str) -> Tuple[CampaignExportRow, ...]:
        normalized = normalize_email_value(email)
        return self._find(lambda row: row.contact_destination.normalized_value == normalized)

    def find_rows_by_fingerprint(self, fingerprint: str) -> Tuple[CampaignExportRow, ...]:
        return self._find(lambda row: row.row_fingerprint == fingerprint)

    def find_exports_by_content_fingerprint(self, fingerprint: str) -> Tuple[CampaignExport, ...]:
        return tuple(
            self._exports[key]
            for key in sorted(self._exports)
            if self._exports[key].content_fingerprint == fingerprint
        )

    def _find(self, predicate) -> Tuple[CampaignExportRow, ...]:
        matches = (row for row in self._rows.values() if predicate(row))
        return tuple(sorted(matches, key=lambda row: (row.export_id, row.row_position, row.export_row_id)))

    def to_dict(self) -> dict:
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exports": [self._exports[key].to_dict() for key in sorted(self._exports)],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())
