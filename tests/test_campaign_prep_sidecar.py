import csv
import hashlib
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from lead_engine import lead_record_from_row, source_occurrence_from_row
from lead_engine import campaign_prep_sidecar


CREATED_AT = "2026-08-11T14:30:00+10:00"
OPERATION = "campaign-prep:test-operation"


def _load_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_campaign_sidecar", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _fixture_input(path: Path) -> bytes:
    source = {
        "Artist": "Strong Artist",
        "Location": "VIC",
        "Email": "one@example.com, two@example.com",
        "Lead_Source": "Spotify",
        "Source_Directory": "spotify",
        "Source Directory": "Spotify",
        "Source_URL": "https://open.spotify.com/artist/Strong123",
        "Release_Date": "2026-08-01",
    }
    occurrence = source_occurrence_from_row(source)
    lead = lead_record_from_row(source)
    rows = [
        {
            **source,
            "lead_id": lead.lead_id,
            "source_occurrence_id": occurrence.source_occurrence_id,
        },
        {
            **source,
            "Email": "one@example.com",
            "lead_id": lead.lead_id,
            "source_occurrence_id": occurrence.source_occurrence_id,
        },
        {
            "Artist": "Weak Artist",
            "Location": "VIC",
            "Email": "weak@example.com",
            "Lead_Source": "Legacy Import",
            "Source_Directory": "legacy_import",
            "Source Directory": "Legacy Import",
            "Source_URL": "https://example.com/artists/weak",
            "Release_Date": "2026-08-01",
            "lead_id": "",
            "source_occurrence_id": "",
        },
    ]
    columns = list(rows[0])
    _write_csv(path, rows, columns)
    return path.read_bytes()


def _run(module, input_path: Path, output_dir: Path, **kwargs):
    return module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        export_format="woodpecker",
        remove_rows_without_emails=True,
        run_reference_date=datetime.fromisoformat("2026-08-11T04:30:00+00:00"),
        ledger_operation_reference=kwargs.pop("ledger_operation_reference", OPERATION),
        ledger_created_at=kwargs.pop("ledger_created_at", CREATED_AT),
        **kwargs,
    )


def _payload(output_dir: Path) -> dict:
    return json.loads((output_dir / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME).read_text())


def _legacy_bytes(output_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(output_dir).as_posix(): path.read_bytes()
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME
    }


def test_sidecar_is_additive_and_preserves_every_existing_output_byte(monkeypatch, tmp_path):
    module = _load_module()
    input_path = tmp_path / "master.csv"
    _fixture_input(input_path)
    baseline_dir = tmp_path / "baseline"
    integrated_dir = tmp_path / "integrated"

    original_writer = module.campaign_prep_sidecar.write_campaign_export_sidecar
    monkeypatch.setattr(module.campaign_prep_sidecar, "write_campaign_export_sidecar", lambda *args, **kwargs: {})
    baseline_result = _run(module, input_path, baseline_dir)
    monkeypatch.setattr(module.campaign_prep_sidecar, "write_campaign_export_sidecar", original_writer)
    integrated_result = _run(module, input_path, integrated_dir)

    assert baseline_result == integrated_result
    assert _legacy_bytes(baseline_dir) == _legacy_bytes(integrated_dir)
    assert (integrated_dir / module.CAMPAIGN_PREP_MANIFEST_FILENAME).read_bytes() == (
        baseline_dir / module.CAMPAIGN_PREP_MANIFEST_FILENAME
    ).read_bytes()


def test_sidecar_maps_actual_artifacts_rows_checksums_lineage_and_duplicates(tmp_path):
    module = _load_module()
    input_path = tmp_path / "master.csv"
    input_bytes = _fixture_input(input_path)
    output_dir = tmp_path / "campaign"

    result = _run(module, input_path, output_dir)
    payload = _payload(output_dir)
    export = payload["ledger"]["exports"][0]
    artifacts = payload["artifacts"]
    rows = export["rows"]

    assert result.diagnostics["ledger_sidecar_status"] == "written"
    assert payload["schema_version"] == "lead-engine-campaign-export-sidecar/v1"
    assert payload["ledger"]["schema_version"] == "lead-engine-campaign-export/v1"
    assert export["operation_reference"] == OPERATION
    assert export["created_at"] == "2026-08-11T04:30:00Z"
    assert datetime.fromisoformat(export["created_at"].replace("Z", "+00:00")).tzinfo is not None
    assert {artifact["export_id"] for artifact in artifacts} == {export["export_id"]}
    assert export["row_count"] == sum(artifact["row_count"] for artifact in artifacts) == len(rows)
    assert [row["row_position"] for row in rows] == list(range(1, len(rows) + 1))
    assert [row["contact_destination"]["raw_value"] for row in rows[:4]] == [
        "one@example.com",
        "two@example.com",
        "one@example.com",
        "weak@example.com",
    ]
    assert rows[0]["lineage"]["status"] == "RESOLVED"
    assert rows[0]["lineage"]["lead_id"] == rows[1]["lineage"]["lead_id"]
    assert rows[3]["lineage"]["status"] == "UNRESOLVED"
    assert rows[3]["lineage"]["lead_id"] == ""
    assert rows[0]["row_fingerprint"] == rows[2]["row_fingerprint"]
    assert rows[0]["export_row_id"] != rows[2]["export_row_id"]

    mapped_ids = []
    for artifact in artifacts:
        path = output_dir / artifact["filename"]
        assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert artifact["byte_size"] == len(path.read_bytes())
        assert artifact["row_count"] == len(artifact["export_row_ids"])
        mapped_ids.extend(artifact["export_row_ids"])
    assert mapped_ids == [row["export_row_id"] for row in rows]
    assert input_path.read_bytes() == input_bytes

    serialized = json.dumps(payload)
    for forbidden in (
        "woodpecker_campaign_id",
        "woodpecker_prospect_id",
        "analytics_event",
        "canonical_entity_id",
    ):
        assert forbidden not in serialized


def test_operation_identity_is_shared_within_run_distinct_between_actions_and_retryable(tmp_path):
    module = _load_module()
    input_path = tmp_path / "master.csv"
    _fixture_input(input_path)

    first = tmp_path / "first"
    second = tmp_path / "second"
    module.generate_campaign_csvs(str(input_path), str(first), export_format="woodpecker")
    module.generate_campaign_csvs(str(input_path), str(second), export_format="woodpecker")
    first_export = _payload(first)["ledger"]["exports"][0]
    second_export = _payload(second)["ledger"]["exports"][0]
    assert first_export["operation_reference"].startswith("campaign-prep:")
    assert first_export["operation_reference"] != second_export["operation_reference"]
    assert first_export["export_id"] != second_export["export_id"]

    retry_one = tmp_path / "retry_one"
    retry_two = tmp_path / "retry_two"
    _run(module, input_path, retry_one)
    _run(module, input_path, retry_two)
    assert _payload(retry_one) == _payload(retry_two)
    assert (retry_one / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME).read_bytes() == (
        retry_two / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME
    ).read_bytes()


def test_empty_export_writes_no_sidecar_or_false_rows(tmp_path):
    module = _load_module()
    input_path = tmp_path / "empty.csv"
    _write_csv(input_path, [{"Artist": "No Email", "Location": "VIC", "Email": ""}], ["Artist", "Location", "Email"])
    output_dir = tmp_path / "campaign"

    result = _run(module, input_path, output_dir)

    assert result == {}
    assert result.diagnostics["ledger_sidecar_status"] == "not_written_empty"
    assert not (output_dir / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME).exists()


def test_csv_failure_never_leaves_completed_sidecar_and_cleans_csv_temp(monkeypatch, tmp_path):
    module = _load_module()
    input_path = tmp_path / "master.csv"
    _fixture_input(input_path)
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    sidecar = output_dir / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME
    sidecar.write_text("old ledger", encoding="utf-8")
    original = module._campaign_prep_atomic_write_csv
    calls = 0

    def fail_second(path, rows, columns):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated campaign CSV failure")
        return original(path, rows, columns)

    monkeypatch.setattr(module, "_campaign_prep_atomic_write_csv", fail_second)
    with pytest.raises(OSError, match="simulated campaign CSV failure"):
        _run(module, input_path, output_dir)

    assert (output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME).exists()
    assert not sidecar.exists()
    assert not list(output_dir.rglob("*.tmp"))


def test_ledger_failure_is_visible_degraded_success_and_keeps_csvs(monkeypatch, tmp_path, capsys):
    module = _load_module()
    input_path = tmp_path / "master.csv"
    _fixture_input(input_path)
    output_dir = tmp_path / "campaign"

    def fail_ledger(*args, **kwargs):
        tmp = output_dir / f"{campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME}.tmp"
        tmp.write_text("partial", encoding="utf-8")
        raise OSError("simulated ledger failure")

    monkeypatch.setattr(module.campaign_prep_sidecar, "write_campaign_export_sidecar", fail_ledger)
    result = _run(module, input_path, output_dir)

    assert result
    assert result.diagnostics["ledger_sidecar_status"] == "failed"
    assert "simulated ledger failure" in result.diagnostics["ledger_sidecar_error"]
    assert "Campaign CSVs were generated" in capsys.readouterr().out
    assert all((output_dir / filename).exists() for filename in result)
    assert (output_dir / module.CAMPAIGN_PREP_MANIFEST_FILENAME).exists()
    assert not (output_dir / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME).exists()
    assert not list(output_dir.rglob("*.tmp"))


def test_atomic_sidecar_replaces_existing_file_and_is_reload_stable(tmp_path):
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    csv_path = output_dir / "artifact.csv"
    _write_csv(csv_path, [{"Email": "a@example.com", "Artist": "A"}], ["Email", "Artist"])
    sidecar = output_dir / campaign_prep_sidecar.CAMPAIGN_PREP_LEDGER_SIDECAR_FILENAME
    sidecar.write_text("obsolete", encoding="utf-8")

    payload = campaign_prep_sidecar.write_campaign_export_sidecar(
        output_dir,
        [{"filename": "artifact.csv", "lineage_rows": [{}]}],
        operation_reference=OPERATION,
        created_at=CREATED_AT,
    )

    assert json.loads(sidecar.read_text(encoding="utf-8")) == payload
    assert sidecar.read_text(encoding="utf-8") == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    assert not (output_dir / "campaign_export_ledger.json.tmp").exists()
