import csv

from lead_vault.schema import get_canonical_master_schema
from lead_vault.stats import summarize_master_dataset


def _write_canonical_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=get_canonical_master_schema())
        writer.writeheader()
        writer.writerows(rows)


def test_summarize_master_dataset_counts_rows_emails_review_and_sources(tmp_path):
    master_path = tmp_path / "master.csv"
    rows = [
        {
            "Artist": "Spotify One",
            "Primary_Email": " one@example.com ",
            "Needs_Review": "Yes",
            "Source_Directory": "spotify",
        },
        {
            "Artist": "Spotify Two",
            "Primary_Email": "   ",
            "Needs_Review": "TRUE",
            "Source_Directory": "spotify",
        },
        {
            "Artist": "Unearthed One",
            "Primary_Email": "two@example.com",
            "Needs_Review": "No",
            "Source_Directory": "unearthed",
        },
        {
            "Artist": "No Source",
            "Primary_Email": "",
            "Needs_Review": "Yes ",
            "Source_Directory": "",
        },
    ]
    _write_canonical_csv(master_path, rows)

    result = summarize_master_dataset(master_path)

    assert result == {
        "total_rows": 4,
        "rows_with_email": 2,
        "needs_review": 1,
        "sources": {
            "spotify": 2,
            "unearthed": 1,
        },
    }


def test_summarize_master_dataset_handles_header_only_csv(tmp_path):
    master_path = tmp_path / "master.csv"
    _write_canonical_csv(master_path, [])

    result = summarize_master_dataset(master_path)

    assert result == {
        "total_rows": 0,
        "rows_with_email": 0,
        "needs_review": 0,
        "sources": {},
    }


def test_summarize_master_dataset_treats_missing_columns_as_blank(tmp_path):
    master_path = tmp_path / "master.csv"
    with open(master_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Artist", "Source_Directory"])
        writer.writeheader()
        writer.writerow({"Artist": "Act One", "Source_Directory": "bandcamp"})
        writer.writerow({"Artist": "Act Two", "Source_Directory": ""})

    result = summarize_master_dataset(master_path)

    assert result == {
        "total_rows": 2,
        "rows_with_email": 0,
        "needs_review": 0,
        "sources": {
            "bandcamp": 1,
        },
    }
