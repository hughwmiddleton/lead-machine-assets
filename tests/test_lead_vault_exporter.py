import csv

import pytest

from lead_vault.exporter import FINAL_EXPORT_PRESET, WOODPECKER_EXPORT_PRESET, export_with_preset
from lead_vault.schema import get_canonical_master_schema


@pytest.fixture
def mock_master_csv(tmp_path):
    master_path = tmp_path / "master.csv"
    rows = []
    for artist, email, status, provenance_url, provenance_type in [
        ("First Act", "first@example.com", "OK", "https://firstact.example.com/contact", "website_enrich"),
        ("Second Act", "", "WARN", "", ""),
        ("Third Act", "", "PENDING", "https://thirdact.example.com/contact", "website_enrich"),
    ]:
        row = {field: "" for field in get_canonical_master_schema()}
        row.update(
            {
                "Artist": artist,
                "Primary_Email": email,
                "All_Emails": email,
                "Location": "Melbourne",
                "Country": "Australia",
                "Primary_Genre": "indie",
                "Website": f"https://{artist.lower().replace(' ', '')}.example.com",
                "Domain": f"{artist.lower().replace(' ', '')}.example.com",
                "Spotify_URL": f"https://spotify.com/{artist.lower().replace(' ', '')}",
                "SoundCloud_URL": f"https://soundcloud.com/{artist.lower().replace(' ', '')}",
                "Instagram_URL": f"https://instagram.com/{artist.lower().replace(' ', '')}",
                "External_Links": "https://example.com/link1;https://example.com/link2",
                "Discovery_Source": "Playlist Alpha",
                "Source_Directory": "spotify",
                "Source_URL": "https://example.com/source",
                "Played_On_Triple_J": "Yes",
                "Played_On_Unearthed": "No",
                "Release_Date": "2026-01-15",
                "Date_Added": "2026-03-01",
                "Final_Status": status,
                "Needs_Review": "",
                "Review_Reason": "",
                "Review_Urls": "",
                "Email_Source_URL": provenance_url,
                "Email_Source_Type": provenance_type,
                "Email_Extract_Method": "regex" if provenance_url else "",
                "Notes": "Priority",
            }
        )
        rows.append(row)

    with open(master_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=get_canonical_master_schema())
        writer.writeheader()
        writer.writerows(rows)

    return master_path


def test_woodpecker_export_header_order(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        assert next(reader) == WOODPECKER_EXPORT_PRESET["headers"]


def test_woodpecker_export_filters_missing_email(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["Artist Name"] == "First Act"


def test_final_export_header_order(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "final_export.csv"

    export_with_preset(FINAL_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        assert next(reader) == FINAL_EXPORT_PRESET["headers"]


def test_final_export_preserves_legacy_review_policy_and_status_filtering(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "final_export.csv"

    result = export_with_preset(FINAL_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["Artist Name"] for row in rows] == ["First Act", "Second Act"]
    assert rows[0]["Email Source"] == "Website"
    assert rows[0]["Needs_Review"] == "FALSE"
    assert rows[0]["final_status"] == "OK"
    assert rows[1]["Needs_Review"] == "TRUE"
    assert result["rows_read"] == 3
    assert result["rows_exported"] == 2
    assert result["rows_skipped"] == 1


def test_final_export_bridge_maps_canonical_fields_to_legacy_schema(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "final_export.csv"

    export_with_preset(FINAL_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["Artist Name"] == "First Act"
    assert row["Primary Email"] == "first@example.com"
    assert row["Primary Genre"] == "indie"
    assert row["Source Directory"] == "spotify"
    assert row["Discovery Source"] == "Playlist Alpha (spotify)"


def test_final_export_bridge_recomputes_post_enrichment_status_when_supported(tmp_path) -> None:
    output_path = tmp_path / "final_export.csv"
    input_path = tmp_path / "master.csv"
    rows = [
        {
            "Artist": "Status Bridge",
            "Primary_Email": "team@statusbridge.com",
            "All_Emails": "team@statusbridge.com",
            "Final_Status": "BLOCK",
            "Needs_Review": "",
            "Email_Source_URL": "https://statusbridge.com/contact",
            "Email_Source_Type": "website_enrich",
            "Email_Extract_Method": "regex",
            "Source_Directory": "website",
            "Source_URL": "https://statusbridge.com",
            "origin_match_flag": "0",
            "directory_conflict_flag": "0",
            "name_consistency_flag": "1",
            "duplicate_email_flag": "0",
            "duplicate_artist_flag": "0",
        }
    ]

    with open(input_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    export_with_preset(FINAL_EXPORT_PRESET, input_path, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["final_status"] == "WARN"
    assert row["Needs_Review"] == "TRUE"
    assert row["FB_Review_Reason"] == "origin_mismatch_downgraded"


def test_final_export_bridge_strips_rejected_fb_emails_when_metadata_present(tmp_path) -> None:
    output_path = tmp_path / "final_export.csv"
    input_path = tmp_path / "master.csv"
    rows = [
        {
            "Artist": "FB Reject",
            "Primary_Email": "fb@test.com",
            "All_Emails": "fb@test.com;other@test.com",
            "Final_Status": "OK",
            "Email_Source_URL": "https://example.com/contact",
            "Email_Source_Type": "website_enrich",
            "FB_Status": "blocked_403",
            "__fb_emails_applied": "fb@test.com",
        }
    ]

    with open(input_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    export_with_preset(FINAL_EXPORT_PRESET, input_path, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["Primary Email"] == "other@test.com"
    assert "fb@test.com" not in row["All Emails"]


def test_woodpecker_field_mapping(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["Artist Name"] == "First Act"
    assert row["Primary Email"] == "first@example.com"
    assert row["Primary Genre"] == "indie"
    assert row["Source Directory"] == "spotify"
    assert row["Final_Status"] == "OK"


def test_export_summary_counts(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    result = export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    assert result == {
        "preset": "woodpecker",
        "rows_read": 3,
        "rows_exported": 1,
        "rows_skipped": 2,
        "output_file": str(output_path),
    }


def test_invalid_row_filter_raises_error(mock_master_csv, tmp_path) -> None:
    with pytest.raises(ValueError):
        export_with_preset(
            {
                "name": "bad",
                "headers": ["Artist"],
                "field_map": {"Artist": "Artist"},
                "row_filter": "not_real",
                "filename_pattern": "bad.csv",
            },
            mock_master_csv,
            tmp_path / "bad.csv",
        )


def test_utf8_sig_output(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    assert output_path.read_bytes().startswith(b"\xef\xbb\xbf")
