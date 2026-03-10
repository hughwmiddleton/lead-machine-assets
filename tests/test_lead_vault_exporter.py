import csv

import pytest

from lead_vault.exporter import WOODPECKER_EXPORT_PRESET, export_with_preset
from lead_vault.schema import get_canonical_master_schema


@pytest.fixture
def mock_master_csv(tmp_path):
    master_path = tmp_path / "master.csv"
    rows = []
    for artist, email, genre in [
        ("First Act", "first@example.com", "indie"),
        ("Second Act", "   ", "rock"),
        ("Third Act", "", "electronic"),
    ]:
        row = {field: "" for field in get_canonical_master_schema()}
        row.update(
            {
                "Artist": artist,
                "Primary_Email": email,
                "All_Emails": f"{email};other@example.com" if email.strip() else "",
                "Location": "Melbourne",
                "Primary_Genre": genre,
                "Website": f"https://{artist.lower().replace(' ', '')}.example.com",
                "Spotify_URL": f"https://spotify.com/{artist.lower().replace(' ', '')}",
                "SoundCloud_URL": f"https://soundcloud.com/{artist.lower().replace(' ', '')}",
                "External_Links": "https://example.com/link1;https://example.com/link2",
                "Discovery_Source": "spotify",
                "Source_Directory": "spotify",
                "Source_URL": "https://example.com/source",
                "Played_On_Triple_J": "Yes",
                "Played_On_Unearthed": "No",
                "Release_Date": "2026-01-15",
                "Date_Added": "2026-03-01",
                "Final_Status": "ready",
                "Needs_Review": "No",
                "Review_Urls": "https://example.com/review",
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


def test_woodpecker_field_mapping(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["Artist Name"] == "First Act"
    assert row["Primary Email"] == "first@example.com"
    assert row["Primary Genre"] == "indie"
    assert row["Source Directory"] == "spotify"
    assert row["Final_Status"] == "ready"


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


def test_utf8_sig_output(mock_master_csv, tmp_path) -> None:
    output_path = tmp_path / "woodpecker.csv"

    export_with_preset(WOODPECKER_EXPORT_PRESET, mock_master_csv, output_path)

    assert output_path.read_bytes().startswith(b"\xef\xbb\xbf")
