"""Campaign Prep persistence adapter for the Campaign Export Ledger."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .export_ledger import CampaignExportLedger, build_campaign_export


CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME = "campaign_export_ledger.json"
CAMPAIGN_PREP_SIDECAR_SCHEMA_VERSION = "lead-engine-campaign-export-sidecar/v1"


def new_operation_reference() -> str:
    """Return a new event identity for one intentional Campaign Prep action."""
    return f"campaign-prep:{uuid.uuid4()}"


def operation_timestamp() -> str:
    """Capture one timezone-aware timestamp for an export action."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def invalidate_existing_sidecar(output_dir: Path) -> None:
    """Prevent a prior operation's sidecar from describing newly overwritten CSVs."""
    sidecar_path = Path(output_dir) / CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME
    tmp_path = sidecar_path.with_name(f"{sidecar_path.name}.tmp")
    for path in (sidecar_path, tmp_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _read_actual_rows(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(_canonical_json(payload))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_campaign_export_sidecar(
    output_dir: Path,
    artifacts: Sequence[Mapping[str, object]],
    *,
    operation_reference: str,
    created_at: str,
    source_dataset_reference: str = "",
) -> dict:
    """Build from final on-disk CSV rows and atomically persist one operation sidecar."""
    output_path = Path(output_dir)
    ledger_input_rows: list[dict[str, object]] = []
    artifact_evidence: list[dict[str, object]] = []

    for artifact in artifacts:
        filename = str(artifact["filename"])
        path = output_path / filename
        actual_rows = _read_actual_rows(path)
        lineage_rows = list(artifact.get("lineage_rows", ()))
        if len(actual_rows) != len(lineage_rows):
            raise ValueError(
                f"Campaign Prep ledger row mismatch for {filename}: "
                f"written={len(actual_rows)} lineage={len(lineage_rows)}"
            )
        first_position = len(ledger_input_rows) + 1
        for actual_row, lineage_row in zip(actual_rows, lineage_rows):
            ledger_row = dict(lineage_row)
            # Actual serialized outbound values are authoritative for ledger content.
            ledger_row.update(actual_row)
            ledger_input_rows.append(ledger_row)
        artifact_evidence.append(
            {
                "filename": filename,
                "row_count": len(actual_rows),
                "first_row_position": first_position,
                "last_row_position": len(ledger_input_rows),
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
            }
        )

    export = build_campaign_export(
        ledger_input_rows,
        operation_reference=operation_reference,
        created_at=created_at,
        source_dataset_reference=source_dataset_reference,
    )
    ledger = CampaignExportLedger()
    ledger.add_export(export)

    for artifact in artifact_evidence:
        start = int(artifact["first_row_position"])
        end = int(artifact["last_row_position"])
        artifact["export_id"] = export.export_id
        artifact["export_row_ids"] = [
            export.rows[position - 1].export_row_id for position in range(start, end + 1)
        ]

    payload = {
        "schema_version": CAMPAIGN_PREP_SIDECAR_SCHEMA_VERSION,
        "ledger": ledger.to_dict(),
        "artifacts": artifact_evidence,
    }
    _write_json_atomic(output_path / CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME, payload)
    # Return the same JSON-native shape a subsequent reload will produce.
    return json.loads(_canonical_json(payload))
