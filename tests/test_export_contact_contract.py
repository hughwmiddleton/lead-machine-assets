import csv
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("PyQt5")

import pipeline_runner


AUTHORITATIVE_FIELDS = {
    "Artist_Identity_Status": "matched",
    "Identity_Assessment_JSON": '{"status":"matched","confidence":0.99}',
    "Preferred_Outreach_Email": "preferred@example.com",
    "Alternate_Emails": "alternate@example.com;third@example.com",
    "Preferred_Contact_Status": "OK",
    "Preferred_Contact_Reason": "contact_attributable",
    "Review_Approved": "TRUE",
    "Approved_Contact_Email": "preferred@example.com",
}


def _export_row() -> dict[str, str]:
    return {
        "Artist Name": "Export Contract Artist",
        "Location": "Melbourne",
        "Email": "preferred@example.com",
        "Email_All": "preferred@example.com;alternate@example.com;third@example.com",
        "Email_Provenance_JSON": (
            '{"alternate@example.com":{"role":"unknown","role_evidence":"insufficient_evidence"},'
            '"preferred@example.com":{"preferred":"True","role":"unknown",'
            '"role_evidence":"insufficient_evidence"},'
            '"third@example.com":{"role":"unknown","role_evidence":"insufficient_evidence"}}'
        ),
        "final_status": "OK",
        "Final_Status_Reason": "preferred_contact_accepted",
        "Lead_Source": "test",
        "Source_Directory": "test",
        "Source Directory": "test",
        "Source URL": "https://example.com/artist",
        "match_score_overall": "0.99",
        "name_consistency_flag": "1.0",
        "name_consistency_flag_polarity": "consistent_is_1",
        "directory_conflict_flag": "0.0",
        "duplicate_email_flag": "0.0",
        "duplicate_artist_flag": "0.0",
        "genre_outlier_flag": "0.0",
        **AUTHORITATIVE_FIELDS,
    }


def _load_campaign_prep_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_export_contract_campaign_prep", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_default_export_preserves_authoritative_contact_contract(tmp_path: Path):
    input_path = tmp_path / "master_final.csv"
    output_path = tmp_path / "master_export_leads.csv"
    source = _export_row()
    pd.DataFrame([source]).to_csv(input_path, index=False)

    pipeline_runner.export_master_leads(input_path.as_posix(), output_path.as_posix(), export_profile="full_dump")

    exported = _read_rows(output_path)[0]
    for field, value in AUTHORITATIVE_FIELDS.items():
        assert exported[field] == value
    assert exported["Email_Provenance_JSON"] == source["Email_Provenance_JSON"]
    assert exported["final_status"] == source["final_status"]
    assert exported["Final_Status_Reason"] == source["Final_Status_Reason"]


def test_legacy_custom_export_columns_remain_compatible(tmp_path: Path):
    input_path = tmp_path / "master_final.csv"
    output_path = tmp_path / "legacy_export.csv"
    pd.DataFrame([_export_row()]).to_csv(input_path, index=False)
    legacy_columns = ["Artist Name", "Email", "Email_All", "final_status"]

    pipeline_runner.export_master_leads(
        input_path.as_posix(), output_path.as_posix(), export_columns=legacy_columns, export_profile="full_dump"
    )

    with output_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == legacy_columns
        assert list(reader)[0]["Email"] == "preferred@example.com"


def test_campaign_prep_uses_preferred_contact_from_exported_csv(tmp_path: Path):
    input_path = tmp_path / "master_final.csv"
    exported_path = tmp_path / "master_export_leads.csv"
    campaign_dir = tmp_path / "campaign"
    source = _export_row()
    pd.DataFrame([source]).to_csv(input_path, index=False)
    pipeline_runner.export_master_leads(input_path.as_posix(), exported_path.as_posix(), export_profile="full_dump")

    module = _load_campaign_prep_module()
    module.generate_campaign_csvs(
        exported_path.as_posix(),
        campaign_dir.as_posix(),
        split_multiple_emails=True,
        export_format="woodpecker",
    )

    rows = _read_rows(campaign_dir / module.CAMPAIGN_PREP_WOODPECKER_MASTER_FILENAME)
    assert [row["Email"] for row in rows] == ["preferred@example.com"]
    processed = _read_rows(campaign_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert processed[0]["Alternate_Emails"] == AUTHORITATIVE_FIELDS["Alternate_Emails"]
