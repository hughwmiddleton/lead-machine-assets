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
        "Inside_VIC.csv": 4,
        "Outside_VIC.csv": 2,
        "Inside_VIC_Played_TripleJ.csv": 3,
        "Inside_VIC_Neither.csv": 1,
        "Outside_VIC_Played_Unearthed.csv": 2,
    }
    assert list(result) == [
        "Inside_VIC.csv",
        "Outside_VIC.csv",
        "Inside_VIC_Played_TripleJ.csv",
        "Inside_VIC_Neither.csv",
        "Outside_VIC_Played_Unearthed.csv",
    ]

    output_columns, inside_rows = _read_csv(output_dir / "Inside_VIC.csv")
    assert output_columns == columns
    assert [row["emails"] for row in inside_rows[:3]] == ["a@gmail.com", "b@gmail.com", "a@gmail.com"]
    assert inside_rows[0]["Notes"] == "  keep spaces  "
    assert inside_rows[3]["emails"] == ""

    _, outside_rows = _read_csv(output_dir / "Outside_VIC.csv")
    assert [row["Artist"] for row in outside_rows] == ["Act B", "Act D"]
    assert outside_rows[0]["location"] == "service area"
    assert outside_rows[0]["Notes"] == "NA"
    assert outside_rows[1]["emails"] == "None"
    assert not (output_dir / "Inside_VIC_Played_Unearthed.csv").exists()
    assert not (output_dir / "Outside_VIC_Played_TripleJ.csv").exists()


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

    assert result["Inside_VIC.csv"] == 1
    assert result["Inside_VIC_Neither.csv"] == 1
    output_columns, rows = _read_csv(output_dir / "Inside_VIC.csv")
    assert output_columns == columns
    assert rows[0]["Location"] == "Victoria"

    bad_path = tmp_path / "bad.csv"
    _write_csv(bad_path, [{"Artist": "Act"}], ["Artist"])
    with pytest.raises(ValueError) as excinfo:
        module.generate_campaign_csvs(str(bad_path), str(tmp_path / "bad_out"))
    assert "Missing required logical field: location" in str(excinfo.value)
    assert "Detected columns: ['Artist']" in str(excinfo.value)
    assert "Accepted aliases: ['Location', 'location', 'City', 'city', 'State', 'state']" in str(excinfo.value)


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
        "Inside_VIC.csv": 2,
        "Outside_VIC.csv": 0,
        "Inside_VIC_Played_Unearthed.csv": 2,
    }
    output_columns, rows = _read_csv(output_dir / "Inside_VIC.csv")
    assert output_columns == [
        "Email",
        "First Name",
        "Company",
        "Artist",
        "Location",
        "Song Title",
        "Sounds Like",
        "Website",
        "Instagram",
        "Facebook",
        "Source URL",
        "Notes",
    ]
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

    output_columns, rows = _read_csv(output_dir / "Inside_VIC_Played_TripleJ.csv")
    assert output_columns == module.CAMPAIGN_PREP_WOODPECKER_COLUMNS
    assert [row["Email"] for row in rows] == ["dup@example.com", "dup@example.com"]
    assert rows[0]["First Name"] == "Act B"
    assert rows[0]["Company"] == "Act B"
    assert rows[0]["Website"] == ""
    assert rows[0]["Instagram"] == ""
    assert rows[0]["Facebook"] == ""


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
    output_columns, rows = _read_csv(output_dir / "Outside_VIC.csv")
    assert result["Outside_VIC.csv"] == 2
    assert output_columns == columns
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
    window.close()
