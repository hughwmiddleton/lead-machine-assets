import csv
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy_campaign_prep", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
        app.setQuitOnLastWindowClosed(False)
    return app


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _campaign_path(output_dir: Path, region: str, bucket: str, radio_bucket: str) -> Path:
    return output_dir / bucket / f"{region}_{radio_bucket}.csv"


def _campaign_result_key(region: str, bucket: str, radio_bucket: str) -> str:
    return f"{bucket}/{region}_{radio_bucket}.csv"


def _all_campaign_rows(output_dir: Path, processed_filename: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(output_dir.glob("**/*.csv")):
        if path.name == processed_filename:
            continue
        _, path_rows = _read_csv(path)
        rows.extend(path_rows)
    return rows


def test_generate_campaign_csvs_segments_splits_and_preserves_values(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "location", "Played on triple J", "Played on Unearthed", "emails", "Notes"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {
                "Artist": "Act A",
                "location": " Melbourne ",
                "Played on triple J": " yes ",
                "Played on Unearthed": "true",
                "emails": "a@gmail.com, b@gmail.com,,a@gmail.com,",
                "Notes": "  keep spaces  ",
            },
            {
                "Artist": "Act B",
                "location": "service area",
                "Played on triple J": "",
                "Played on Unearthed": "1.0",
                "emails": "solo@example.com",
                "Notes": "NA",
            },
            {
                "Artist": "Act C",
                "location": "VIC",
                "Played on triple J": "0",
                "Played on Unearthed": "0.0",
                "emails": "",
                "Notes": "null",
            },
            {
                "Artist": "Act D",
                "location": "",
                "Played on triple J": "false",
                "Played on Unearthed": "y",
                "emails": "None",
                "Notes": "blank location",
            },
        ],
        columns,
    )

    result = module.generate_campaign_csvs(str(input_path), str(output_dir), split_multiple_emails=True)

    assert result == {
        _campaign_result_key("Inside_VIC", "180_plus_days", "Played_TripleJ"): 3,
        _campaign_result_key("Inside_VIC", "180_plus_days", "Neither"): 1,
        _campaign_result_key("Outside_VIC", "180_plus_days", "Played_Unearthed"): 2,
    }
    assert list(result) == [
        _campaign_result_key("Inside_VIC", "180_plus_days", "Played_TripleJ"),
        _campaign_result_key("Inside_VIC", "180_plus_days", "Neither"),
        _campaign_result_key("Outside_VIC", "180_plus_days", "Played_Unearthed"),
    ]

    output_columns, triplej_rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Played_TripleJ"))
    assert output_columns == [*columns, "Recency_Bucket"]
    assert [row["emails"] for row in triplej_rows] == ["a@gmail.com", "b@gmail.com", "a@gmail.com"]
    assert triplej_rows[0]["Notes"] == "  keep spaces  "
    assert {row["Recency_Bucket"] for row in triplej_rows} == {"180_plus_days"}

    _, neither_rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Neither"))
    assert neither_rows[0]["emails"] == ""

    _, outside_rows = _read_csv(_campaign_path(output_dir, "Outside_VIC", "180_plus_days", "Played_Unearthed"))
    assert [row["Artist"] for row in outside_rows] == ["Act B", "Act D"]
    assert outside_rows[0]["location"] == "service area"
    assert outside_rows[0]["Notes"] == "NA"
    assert outside_rows[1]["emails"] == "None"
    assert not _campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Played_Unearthed").exists()
    assert not _campaign_path(output_dir, "Outside_VIC", "180_plus_days", "Played_TripleJ").exists()


def test_generate_campaign_csvs_no_email_column_is_safe_and_missing_columns_are_clear(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [{"Artist": "Act", "Location": "Victoria"}],
        columns,
    )

    result = module.generate_campaign_csvs(str(input_path), str(output_dir), split_multiple_emails=True)

    assert result[_campaign_result_key("Inside_VIC", "180_plus_days", "Neither")] == 1
    assert result.diagnostics["missing_release_date_column"] is True
    assert result.diagnostics["resolved_release_date_column"] is None
    output_columns, rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Neither"))
    assert output_columns == [*columns, "Recency_Bucket"]
    assert rows[0]["Location"] == "Victoria"

    bad_path = tmp_path / "bad.csv"
    _write_csv(bad_path, [{"Artist": "Act"}], ["Artist"])
    with pytest.raises(ValueError) as excinfo:
        module.generate_campaign_csvs(str(bad_path), str(tmp_path / "bad_out"))
    assert "Missing required logical field: location" in str(excinfo.value)
    assert "Detected columns: ['Artist']" in str(excinfo.value)
    assert "Accepted aliases: ['Location', 'location', 'City', 'city', 'State', 'state']" in str(excinfo.value)


def test_generate_campaign_csvs_filters_rows_without_email_after_splitting(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "emails", "Unearthed"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "Act A", "Location": "Melbourne", "emails": "a@test.com, ", "Unearthed": ""},
            {"Artist": "Act B", "Location": "VIC", "emails": " ", "Unearthed": "yes"},
            {"Artist": "Act C", "Location": "Sydney", "emails": "nan", "Unearthed": ""},
            {"Artist": "Act D", "Location": "Adelaide", "emails": "None", "Unearthed": "yes"},
            {"Artist": "Act E", "Location": "Brisbane", "emails": "e@test.com, f@test.com,,", "Unearthed": ""},
        ],
        columns,
    )
    source_bytes = input_path.read_bytes()

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        remove_rows_without_emails=True,
    )

    assert result == {
        _campaign_result_key("Inside_VIC", "180_plus_days", "Neither"): 1,
        _campaign_result_key("Outside_VIC", "180_plus_days", "Neither"): 2,
    }
    _, inside_rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Neither"))
    _, outside_rows = _read_csv(_campaign_path(output_dir, "Outside_VIC", "180_plus_days", "Neither"))
    assert [row["emails"] for row in inside_rows] == ["a@test.com"]
    assert [row["emails"] for row in outside_rows] == ["e@test.com", "f@test.com"]
    assert input_path.read_bytes() == source_bytes
    assert not _campaign_path(output_dir, "Outside_VIC", "180_plus_days", "Played_Unearthed").exists()


def test_generate_campaign_csvs_release_date_alias_priority_reports_ambiguity(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release_Date", "Release Date", "Email"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    reference = module.datetime.datetime(2026, 4, 30, tzinfo=module.datetime.timezone.utc)
    _write_csv(
        input_path,
        [
            {
                "Artist": "Priority",
                "Location": "VIC",
                "Release_Date": "2026-04-10",
                "Release Date": "2025-01-01",
                "Email": "priority@test.com",
            }
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        run_reference_date=reference,
    )

    _, processed_rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert processed_rows[0]["Recency_Bucket"] == "0_30_days"
    assert result.diagnostics["resolved_release_date_column"] == "Release_Date"
    assert result.diagnostics["both_release_date_columns_present"] is True


def test_generate_campaign_csvs_no_email_column_filter_on_writes_empty_primary_files(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Unearthed"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "Act A", "Location": "Victoria", "Unearthed": "yes"},
            {"Artist": "Act B", "Location": "Sydney", "Unearthed": "yes"},
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        remove_rows_without_emails=True,
    )

    assert result == {}
    assert sorted(path.name for path in output_dir.glob("*.csv")) == [
        "master_export_leads.processed.csv",
    ]


def test_generate_campaign_csvs_filter_all_rows_removed_writes_primary_headers_only(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "emails"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "Act A", "Location": "Victoria", "emails": ""},
            {"Artist": "Act B", "Location": "Sydney", "emails": "None"},
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        remove_rows_without_emails=True,
    )

    assert result == {}
    assert sorted(path.name for path in output_dir.glob("*.csv")) == [
        "master_export_leads.processed.csv",
    ]


def test_generate_campaign_csvs_release_date_sort_ascending_invalid_at_bottom_stable(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release Date", "Email"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "Invalid A", "Location": "VIC", "Release Date": "not-a-date", "Email": "a@test.com"},
            {"Artist": "New", "Location": "VIC", "Release Date": "2026-03-10", "Email": "b@test.com"},
            {"Artist": "Old", "Location": "VIC", "Release Date": "2025-01-02", "Email": "c@test.com"},
            {"Artist": "Same 1", "Location": "VIC", "Release Date": "2026-03-10", "Email": "d@test.com"},
            {"Artist": "Missing", "Location": "VIC", "Release Date": "", "Email": "e@test.com"},
            {"Artist": "Invalid B", "Location": "VIC", "Release Date": "32/13/2026", "Email": "f@test.com"},
        ],
        columns,
    )
    source_bytes = input_path.read_bytes()

    module.generate_campaign_csvs(str(input_path), str(output_dir), release_date_sort="ascending")

    _, rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert [row["Artist"] for row in rows] == ["Old", "New", "Same 1", "Invalid A", "Missing", "Invalid B"]
    assert [row["Release Date"] for row in rows[-3:]] == ["not-a-date", "", "32/13/2026"]
    assert input_path.read_bytes() == source_bytes


def test_generate_campaign_csvs_release_date_sort_descending_invalid_at_bottom_stable(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release Date", "Email"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "Middle 1", "Location": "Sydney", "Release Date": "2026-03-10", "Email": "a@test.com"},
            {"Artist": "Old", "Location": "Sydney", "Release Date": "2025-01-02", "Email": "b@test.com"},
            {"Artist": "Invalid A", "Location": "Sydney", "Release Date": "soon", "Email": "c@test.com"},
            {"Artist": "Newest", "Location": "Sydney", "Release Date": "2026-12-01", "Email": "d@test.com"},
            {"Artist": "Middle 2", "Location": "Sydney", "Release Date": "2026-03-10", "Email": "e@test.com"},
            {"Artist": "Missing", "Location": "Sydney", "Release Date": "", "Email": "f@test.com"},
        ],
        columns,
    )

    module.generate_campaign_csvs(str(input_path), str(output_dir), release_date_sort="descending")

    _, rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert [row["Artist"] for row in rows] == ["Newest", "Middle 1", "Middle 2", "Old", "Invalid A", "Missing"]


def test_generate_campaign_csvs_release_date_sort_none_preserves_order_while_parsing_for_recency(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release Date", "Email"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "B", "Location": "VIC", "Release Date": "bad", "Email": "b@test.com"},
            {"Artist": "A", "Location": "VIC", "Release Date": "2025-01-01", "Email": "a@test.com"},
        ],
        columns,
    )

    module.generate_campaign_csvs(str(input_path), str(output_dir))

    _, rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert [row["Artist"] for row in rows] == ["B", "A"]


def test_generate_campaign_csvs_release_date_sort_after_split_and_email_filter(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release Date", "Email"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {"Artist": "Later Split", "Location": "VIC", "Release Date": "2026-05-01", "Email": "later1@test.com, later2@test.com"},
            {"Artist": "No Email", "Location": "VIC", "Release Date": "2024-01-01", "Email": ""},
            {"Artist": "Earlier", "Location": "VIC", "Release Date": "2025-01-01", "Email": "early@test.com"},
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        remove_rows_without_emails=True,
        release_date_sort="ascending",
    )

    assert sum(result.values()) == 3
    _, rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert [(row["Artist"], row["Email"]) for row in rows] == [
        ("Earlier", "early@test.com"),
        ("Later Split", "later1@test.com"),
        ("Later Split", "later2@test.com"),
    ]


def test_generate_campaign_csvs_writes_processed_master_from_prepared_rows(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release Date", "Email", "Email_All", "Custom Source Field"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {
                "Artist": "Later",
                "Location": "Victoria",
                "Release Date": "2026-05-01",
                "Email": "later1@test.com, later2@test.com",
                "Email_All": "later1@test.com, later2@test.com",
                "Custom Source Field": "keep later",
            },
            {
                "Artist": "No Email",
                "Location": "Melbourne",
                "Release Date": "2027-01-01",
                "Email": "",
                "Email_All": "",
                "Custom Source Field": "filtered",
            },
            {
                "Artist": "Earlier",
                "Location": "Sydney",
                "Release Date": "2025-01-01",
                "Email": "earlier@test.com",
                "Email_All": "earlier@test.com",
                "Custom Source Field": "keep earlier",
            },
        ],
        columns,
    )
    source_bytes = input_path.read_bytes()

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        remove_rows_without_emails=True,
        release_date_sort="ascending",
        export_format="woodpecker",
    )

    processed_columns, processed_rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME not in result
    assert processed_columns == [*columns, "Recency_Bucket"]
    assert [(row["Artist"], row["Email"], row["Custom Source Field"]) for row in processed_rows] == [
        ("Earlier", "earlier@test.com", "keep earlier"),
        ("Later", "later1@test.com", "keep later"),
        ("Later", "later2@test.com", "keep later"),
    ]

    assert len(_all_campaign_rows(output_dir, module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)) == len(processed_rows)
    _, campaign_rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "0_30_days", "Neither"))
    assert [row["Email"] for row in campaign_rows] == ["later1@test.com", "later2@test.com"]
    assert input_path.read_bytes() == source_bytes


def test_generate_campaign_csvs_processed_master_uses_sorted_split_release_date_buffer(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release_Date", "Primary_Email"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {
                "Artist": "Older Split",
                "Location": "VIC",
                "Release_Date": "2025-01-01",
                "Primary_Email": "old1@test.com, old2@test.com",
            },
            {
                "Artist": "Newest",
                "Location": "VIC",
                "Release_Date": "2026-06-01",
                "Primary_Email": "new@test.com",
            },
            {
                "Artist": "Invalid",
                "Location": "VIC",
                "Release_Date": "coming soon",
                "Primary_Email": "invalid@test.com",
            },
            {
                "Artist": "Middle",
                "Location": "VIC",
                "Release_Date": "2025-09-15",
                "Primary_Email": "mid@test.com",
            },
        ],
        columns,
    )

    module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        remove_rows_without_emails=True,
        release_date_sort="descending",
    )

    _, processed_rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    campaign_rows = _all_campaign_rows(output_dir, module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    expected = [
        ("Newest", "new@test.com"),
        ("Middle", "mid@test.com"),
        ("Older Split", "old1@test.com"),
        ("Older Split", "old2@test.com"),
        ("Invalid", "invalid@test.com"),
    ]
    assert [(row["Artist"], row["Primary_Email"]) for row in processed_rows] == expected
    assert [(row["Artist"], row["Primary_Email"]) for row in campaign_rows] == expected


def test_generate_campaign_csvs_release_date_sort_does_not_mutate_export_rows(tmp_path):
    module = _load_legacy_module()
    rows = [
        ({"Release Date": "2026-01-01", "Email": "a@test.com"}, {"Release Date": "2026-01-01", "Email": "a@test.com"}),
        ({"Release Date": "2025-01-01", "Email": "b@test.com"}, {"Release Date": "2025-01-01", "Email": "b@test.com"}),
    ]
    original_rows = [({**source}, {**export}) for source, export in rows]

    sorted_rows = module._campaign_prep_sort_buffer_by_release_date(rows, "ascending")

    assert rows == original_rows
    assert [export["Email"] for _source, export in sorted_rows] == ["b@test.com", "a@test.com"]


def test_generate_campaign_csvs_supports_real_master_headers_and_alias_priority(tmp_path):
    module = _load_legacy_module()
    columns = [
        "Artist",
        "Contact_Name",
        "Organization",
        "Location",
        "City",
        "State",
        "Sounds Like",
        "Social Link",
        "Song_Title",
        "Primary_Email",
        "All_Emails",
        "Website",
        "Facebook_URL",
        "Instagram_URL",
        "Played_On_Triple_J",
        "Played_On_Unearthed",
        "Source_URL",
        "Notes",
    ]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {
                "Artist": "Act A",
                "Contact_Name": "Alex",
                "Organization": "Act A Pty",
                "Location": "Melbourne",
                "City": "Sydney",
                "State": "NSW",
                "Sounds Like": "Indie",
                "Social Link": "https://social.example/a",
                "Song_Title": "Song A",
                "Primary_Email": "primary1@example.com, primary2@example.com,",
                "All_Emails": "ignored@example.com",
                "Website": "https://example.com",
                "Facebook_URL": "https://facebook.example/a",
                "Instagram_URL": "https://instagram.example/a",
                "Played_On_Triple_J": "",
                "Played_On_Unearthed": "yes",
                "Source_URL": "https://source.example/a",
                "Notes": "Keep",
            }
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        export_format="woodpecker",
    )

    assert result == {
        _campaign_result_key("Inside_VIC", "180_plus_days", "Played_Unearthed"): 2,
    }
    output_columns, rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Played_Unearthed"))
    assert output_columns == [*module.CAMPAIGN_PREP_WOODPECKER_COLUMNS, "Recency_Bucket"]
    assert [row["Email"] for row in rows] == ["primary1@example.com", "primary2@example.com"]
    assert {row["Email"] for row in rows}.isdisjoint({"ignored@example.com"})
    assert rows[0]["First Name"] == "Alex"
    assert rows[0]["Company"] == "Act A Pty"
    assert rows[0]["Location"] == "Melbourne"
    assert rows[0]["Source URL"] == "https://source.example/a"


def test_generate_campaign_csvs_woodpecker_missing_fields_are_blank_and_duplicates_preserved(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "State", "Primary Email", "TripleJ"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {
                "Artist": "Act B",
                "State": "VIC",
                "Primary Email": "dup@example.com, dup@example.com, nan, None,",
                "TripleJ": "true",
            }
        ],
        columns,
    )

    module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        export_format="woodpecker",
    )

    output_columns, rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Played_TripleJ"))
    assert output_columns == [*module.CAMPAIGN_PREP_WOODPECKER_COLUMNS, "Recency_Bucket"]
    assert [row["Email"] for row in rows] == ["dup@example.com", "dup@example.com"]
    assert rows[0]["First Name"] == "Act B"
    assert rows[0]["Company"] == "Act B"
    assert rows[0]["Website"] == ""
    assert rows[0]["Instagram"] == ""
    assert rows[0]["Facebook"] == ""


def test_generate_campaign_csvs_woodpecker_filter_uses_resolved_email_without_fallback(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Primary_Email", "All_Emails"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    _write_csv(
        input_path,
        [
            {
                "Artist": "Act A",
                "Location": "VIC",
                "Primary_Email": "",
                "All_Emails": "fallback@example.com",
            },
            {
                "Artist": "Act B",
                "Location": "VIC",
                "Primary_Email": "primary@example.com",
                "All_Emails": "other@example.com",
            },
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        export_format="woodpecker",
        remove_rows_without_emails=True,
    )

    assert result == {
        _campaign_result_key("Inside_VIC", "180_plus_days", "Neither"): 1,
    }
    output_columns, rows = _read_csv(_campaign_path(output_dir, "Inside_VIC", "180_plus_days", "Neither"))
    assert output_columns == [*module.CAMPAIGN_PREP_WOODPECKER_COLUMNS, "Recency_Bucket"]
    assert [row["Email"] for row in rows] == ["primary@example.com"]
    assert rows[0]["Artist"] == "Act B"


def test_generate_campaign_csvs_input_headers_and_invalid_format(tmp_path):
    module = _load_legacy_module()
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    columns = ["Artist", "City", "Unearthed", "All Emails"]
    _write_csv(
        input_path,
        [{"Artist": "Act", "City": "Adelaide", "Unearthed": "y", "All Emails": "a@example.com,b@example.com"}],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        export_format="input_headers",
    )
    output_columns, rows = _read_csv(_campaign_path(output_dir, "Outside_VIC", "180_plus_days", "Played_Unearthed"))
    assert result[_campaign_result_key("Outside_VIC", "180_plus_days", "Played_Unearthed")] == 2
    assert output_columns == [*columns, "Recency_Bucket"]
    assert [row["All Emails"] for row in rows] == ["a@example.com", "b@example.com"]

    with pytest.raises(ValueError, match="Invalid export_format"):
        module.generate_campaign_csvs(str(input_path), str(tmp_path / "invalid"), export_format="bad")


def test_generate_campaign_csvs_outputs_are_byte_stable(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "location", "Played on triple J", "Played on Unearthed", "Email_All"]
    input_path = tmp_path / "master.csv"
    _write_csv(
        input_path,
        [
            {"Artist": "A", "location": "vic", "Played on triple J": "1.0", "Played on Unearthed": "y", "Email_All": "a@x.com,b@x.com"},
            {"Artist": "B", "location": "Sydney", "Played on triple J": "", "Played on Unearthed": "", "Email_All": "null"},
        ],
        columns,
    )

    first = module.generate_campaign_csvs(str(input_path), str(tmp_path / "one"), split_multiple_emails=True)
    second = module.generate_campaign_csvs(str(input_path), str(tmp_path / "two"), split_multiple_emails=True)

    assert first == second
    for filename in first:
        assert (tmp_path / "one" / filename).read_bytes() == (tmp_path / "two" / filename).read_bytes()


def test_generate_campaign_csvs_release_date_recency_buckets_partition_and_boundaries(tmp_path):
    module = _load_legacy_module()
    columns = ["Artist", "Location", "Release_Date", "Upload_Date", "Email", "Played_On_Triple_J", "Played_On_Unearthed"]
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    reference = module.datetime.datetime(2026, 4, 30, 10, 30, tzinfo=module.datetime.timezone.utc)
    _write_csv(
        input_path,
        [
            {"Artist": "Exactly 30", "Location": "Melbourne", "Release_Date": "2026-03-31", "Upload_Date": "2026-04-01", "Email": "d30@test.com", "Played_On_Triple_J": "yes", "Played_On_Unearthed": ""},
            {"Artist": "Exactly 31 Split", "Location": "Sydney", "Release_Date": "2026-03-30", "Upload_Date": "2026-03-30", "Email": "d31a@test.com, d31b@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": "yes"},
            {"Artist": "Within 90", "Location": "VIC", "Release_Date": "2026-02-15", "Upload_Date": "2026-02-16", "Email": "w90@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": ""},
            {"Artist": "Exactly 90", "Location": "Brisbane", "Release_Date": "2026-01-30", "Upload_Date": "2026-01-31", "Email": "d90@test.com", "Played_On_Triple_J": "yes", "Played_On_Unearthed": ""},
            {"Artist": "Exactly 91", "Location": "Victoria", "Release_Date": "2026-01-29", "Upload_Date": "2026-01-29", "Email": "d91@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": "yes"},
            {"Artist": "Exactly 180", "Location": "Adelaide", "Release_Date": "2025-11-01", "Upload_Date": "2025-11-02", "Email": "d180@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": ""},
            {"Artist": "Exactly 181", "Location": "Melbourne", "Release_Date": "2025-10-31", "Upload_Date": "2025-10-31", "Email": "d181@test.com", "Played_On_Triple_J": "yes", "Played_On_Unearthed": ""},
            {"Artist": "Blank Date", "Location": "Perth", "Release_Date": "", "Upload_Date": "2026-04-02", "Email": "blank@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": ""},
            {"Artist": "Invalid Date", "Location": "VIC", "Release_Date": "not-a-date", "Upload_Date": "2026-04-03", "Email": "invalid@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": "yes"},
            {"Artist": "Future Date", "Location": "Hobart", "Release_Date": "2026-05-02", "Upload_Date": "2026-05-03", "Email": "future@test.com", "Played_On_Triple_J": "", "Played_On_Unearthed": ""},
        ],
        columns,
    )

    result = module.generate_campaign_csvs(
        str(input_path),
        str(output_dir),
        split_multiple_emails=True,
        export_format="woodpecker",
        release_date_sort="descending",
        run_reference_date=reference,
    )

    processed_columns, processed_rows = _read_csv(output_dir / module.CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME)
    assert processed_columns == [*columns, "Recency_Bucket"]
    assert [(row["Artist"], row["Email"]) for row in processed_rows] == [
        ("Future Date", "future@test.com"),
        ("Exactly 30", "d30@test.com"),
        ("Exactly 31 Split", "d31a@test.com"),
        ("Exactly 31 Split", "d31b@test.com"),
        ("Within 90", "w90@test.com"),
        ("Exactly 90", "d90@test.com"),
        ("Exactly 91", "d91@test.com"),
        ("Exactly 180", "d180@test.com"),
        ("Exactly 181", "d181@test.com"),
        ("Blank Date", "blank@test.com"),
        ("Invalid Date", "invalid@test.com"),
    ]
    assert {row["Recency_Bucket"] for row in processed_rows} <= set(module.CAMPAIGN_PREP_RECENCY_BUCKETS)
    assert {
        (row["Artist"], row["Email"]): row["Recency_Bucket"]
        for row in processed_rows
    } == {
        ("Future Date", "future@test.com"): "0_30_days",
        ("Exactly 30", "d30@test.com"): "0_30_days",
        ("Exactly 31 Split", "d31a@test.com"): "30_90_days",
        ("Exactly 31 Split", "d31b@test.com"): "30_90_days",
        ("Within 90", "w90@test.com"): "30_90_days",
        ("Exactly 90", "d90@test.com"): "30_90_days",
        ("Exactly 91", "d91@test.com"): "90_180_days",
        ("Exactly 180", "d180@test.com"): "90_180_days",
        ("Exactly 181", "d181@test.com"): "180_plus_days",
        ("Blank Date", "blank@test.com"): "180_plus_days",
        ("Invalid Date", "invalid@test.com"): "180_plus_days",
    }
    assert processed_rows[-2]["Artist"] == "Blank Date"
    assert processed_rows[-1]["Artist"] == "Invalid Date"

    campaign_row_count = sum(result.values())
    assert campaign_row_count == len(processed_rows)
    bucket_counts = {
        bucket: sum(1 for row in processed_rows if row["Recency_Bucket"] == bucket)
        for bucket in module.CAMPAIGN_PREP_RECENCY_BUCKETS
    }
    assert sum(bucket_counts.values()) == len(processed_rows)
    assert bucket_counts == {
        "0_30_days": 2,
        "30_90_days": 4,
        "90_180_days": 2,
        "180_plus_days": 3,
    }
    assert result.diagnostics["run_reference_date"] == reference.isoformat()
    assert result.diagnostics["total_processed_rows"] == len(processed_rows)
    assert result.diagnostics["recency_bucket_counts"] == bucket_counts
    assert result.diagnostics["invalid_blank_unparseable_release_date_count"] == 2
    assert result.diagnostics["min_parsed_release_date"] == "2025-10-31"
    assert result.diagnostics["max_parsed_release_date"] == "2026-05-02"
    assert result.diagnostics["sample_invalid_release_date_values"] == ["", "not-a-date"]
    assert result.diagnostics["missing_release_date_column"] is False
    assert result.diagnostics["resolved_release_date_column"] == "Release_Date"
    assert result.diagnostics["both_release_date_columns_present"] is False
    assert sorted(path.name for path in output_dir.iterdir() if path.is_dir()) == [
        "0_30_days",
        "180_plus_days",
        "30_90_days",
        "90_180_days",
    ]
    assert not any(path.name.startswith(("Inside_VIC_", "Outside_VIC_")) for path in output_dir.glob("*.csv"))

    def expected_filename(row: dict) -> str:
        region = "Inside_VIC" if module._campaign_prep_inside_vic(row["Location"]) else "Outside_VIC"
        if module._campaign_prep_truthy(row["Played_On_Triple_J"]):
            radio_bucket = "Played_TripleJ"
        elif module._campaign_prep_truthy(row["Played_On_Unearthed"]):
            radio_bucket = "Played_Unearthed"
        else:
            radio_bucket = "Neither"
        return _campaign_result_key(region, row["Recency_Bucket"], radio_bucket)

    for filename in result:
        _, campaign_rows = _read_csv(output_dir / filename)
        assert "Release Date" in campaign_rows[0]
        assert "Upload Date" in campaign_rows[0]
        assert list(campaign_rows[0])[-1] == "Recency_Bucket"
        expected_rows = [
            row
            for row in processed_rows
            if expected_filename(row) == filename
        ]
        assert [(row["Artist"], row["Email"], row["Recency_Bucket"]) for row in campaign_rows] == [
            (row["Artist"], row["Email"], row["Recency_Bucket"])
            for row in expected_rows
        ]


def test_main_window_contains_campaign_prep_tab(qapp):
    module = _load_legacy_module()
    window = module.MainWindow()

    labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]

    assert "Campaign Prep" in labels
    assert window.campaign_prep_tab.export_format_combo.currentData() == "lead_machine_full"
    assert [
        window.campaign_prep_tab.export_format_combo.itemData(index)
        for index in range(window.campaign_prep_tab.export_format_combo.count())
    ] == ["lead_machine_full", "woodpecker", "input_headers"]
    assert window.campaign_prep_tab.release_date_sort_combo.currentData() == "none"
    assert [
        window.campaign_prep_tab.release_date_sort_combo.itemData(index)
        for index in range(window.campaign_prep_tab.release_date_sort_combo.count())
    ] == ["none", "ascending", "descending"]
    window.close()


def test_campaign_prep_remove_rows_without_emails_checkbox_defaults_and_passes_value(qapp, tmp_path, monkeypatch):
    module = _load_legacy_module()
    input_path = tmp_path / "master.csv"
    output_dir = tmp_path / "campaign"
    output_dir.mkdir()
    _write_csv(input_path, [{"Artist": "Act", "Location": "VIC", "emails": ""}], ["Artist", "Location", "emails"])
    calls = []

    def fake_generate(*args, **kwargs):
        calls.append((args, kwargs))
        return {"Inside_VIC.csv": 0, "Outside_VIC.csv": 0}

    monkeypatch.setattr(module, "generate_campaign_csvs", fake_generate)
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "information", lambda *args, **kwargs: None)

    tab = module.CampaignPrepTab()
    assert tab.remove_rows_without_emails_checkbox.text() == "Remove rows without emails"
    assert tab.remove_rows_without_emails_checkbox.isChecked() is False

    tab.input_csv_edit.setText(str(input_path))
    tab.output_dir_edit.setText(str(output_dir))
    tab.split_emails_checkbox.setChecked(True)
    tab.remove_rows_without_emails_checkbox.setChecked(True)
    tab.release_date_sort_combo.setCurrentIndex(2)
    tab._generate_campaign_csvs()

    assert calls[0][1]["split_multiple_emails"] is True
    assert calls[0][1]["remove_rows_without_emails"] is True
    assert calls[0][1]["release_date_sort"] == "descending"
    tab.close()
