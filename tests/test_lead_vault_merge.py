import csv
import datetime as dt

from lead_vault.alias_map import HEADER_ALIASES
from lead_vault.merge import merge_csv_into_master, preview_csv_import, preview_csv_merge_counts
from lead_vault.schema import get_canonical_master_schema


RUN_AT = dt.datetime(2026, 3, 10, 12, 0, 0, tzinfo=dt.timezone.utc)


def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _read_master_rows(path):
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _master_row(**overrides):
    row = {field: "" for field in get_canonical_master_schema()}
    row.update(overrides)
    return row


def test_primary_email_match_updates_existing_row(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="The Echo",
                Primary_Email="artist@example.com",
                Date_Added="2026-01-01T00:00:00Z",
                Import_Source_File="existing.csv",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Primary Email", "Bandcamp URL"],
        [
            {
                "Artist Name": "The Echo",
                "Primary Email": "artist@example.com",
                "Bandcamp URL": "https://theecho.bandcamp.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert result["rows_added"] == 0
    assert rows[0]["Bandcamp_URL"] == "https://theecho.bandcamp.com"
    assert rows[0]["Import_Source_File"] == "existing.csv"
    assert rows[0]["Date_Added"] == "2026-01-01T00:00:00Z"
    assert rows[0]["Last_Updated"] == "2026-03-10T12:00:00Z"


def test_all_emails_membership_match_updates_missing_primary_email(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Night Tides",
                Website="https://nighttides.example.com",
                All_Emails="manager@example.com;alt@example.com",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Email"],
        [{"Artist Name": "Night Tides", "Email": "alt@example.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert rows[0]["Primary_Email"] == "alt@example.com"


def test_website_match_uses_normalized_url(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="City Lines", Website="https://www.citylines.com/")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Website", "Bandcamp URL"],
        [
            {
                "Artist Name": "City Lines",
                "Website": "citylines.com",
                "Bandcamp URL": "https://citylines.bandcamp.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert rows[0]["Bandcamp_URL"] == "https://citylines.bandcamp.com"


def test_soundcloud_url_match_uses_normalized_url(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Signals", SoundCloud_URL="https://soundcloud.com/signals/")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "SoundCloud URL", "Website"],
        [
            {
                "Artist Name": "Signals",
                "SoundCloud URL": "soundcloud.com/signals?utm_source=test",
                "Website": "https://signals.example.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert rows[0]["Website"] == "https://signals.example.com"


def test_bandcamp_url_match_uses_normalized_url(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Harbor", Bandcamp_URL="https://harbor.bandcamp.com/album/demo")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Bandcamp URL", "Primary Genre"],
        [
            {
                "Artist Name": "Harbor",
                "Bandcamp URL": "harbor.bandcamp.com/album/demo?from=discover",
                "Primary Genre": "indie",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert rows[0]["Primary_Genre"] == "indie"


def test_all_emails_merge_is_additive_and_keeps_existing_primary(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Cascade",
                Website="https://cascade.example.com",
                Primary_Email="artist@example.com",
                All_Emails="manager@example.com",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Website", "Primary Email", "All Emails"],
        [
            {
                "Artist Name": "Cascade",
                "Website": "cascade.example.com",
                "Primary Email": "booking@example.com",
                "All Emails": "press@example.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert rows[0]["Primary_Email"] == "artist@example.com"
    assert rows[0]["All_Emails"] == "manager@example.com;press@example.com;booking@example.com"


def test_matched_row_with_no_safe_changes_counts_as_skipped_duplicate(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Static Bloom", Website="https://staticbloom.com", Primary_Email="hi@staticbloom.com")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Website", "Email"],
        [{"Artist Name": "Static Bloom", "Website": "staticbloom.com", "Email": "hi@staticbloom.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)

    assert result["rows_skipped_duplicates"] == 1
    assert result["rows_updated"] == 0


def test_ambiguous_multi_hit_rows_are_skipped(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(Artist="Act One", Domain_Root="example.com"),
            _master_row(Artist="Act Two", Domain_Root="example.com"),
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Domain Root"],
        [{"Artist Name": "Example Artist", "Domain Root": "example.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_ambiguous"] == 1
    assert result["rows_added"] == 0
    assert len(rows) == 2


def test_artist_fallback_only_runs_when_stronger_incoming_keys_are_absent(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="The Lanterns")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Website"],
        [{"Artist Name": "Lanterns", "Website": "https://new-lanterns.example.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_added"] == 1
    assert result["rows_updated"] == 0
    assert len(rows) == 2


def test_master_auto_create_persists_canonical_header_order(tmp_path):
    master_path = tmp_path / "nested" / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        source_path,
        ["Artist Name", "Email"],
        [{"Artist Name": "First Contact", "Email": "hello@example.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_added"] == 1
    assert rows[0]["Artist"] == "First Contact"
    with open(master_path, "r", newline="", encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == get_canonical_master_schema()


def test_runtime_manual_mapping_and_ignore_do_not_mutate_alias_table(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    alias_snapshot = dict(HEADER_ALIASES)
    _write_csv(
        source_path,
        ["Artist Name", "Booking Email", "Mystery Column"],
        [{"Artist Name": "Mapped Act", "Booking Email": "book@example.com", "Mystery Column": "ignore me"}],
    )

    preview = preview_csv_import(
        source_path,
        header_overrides={"Booking Email": "Primary_Email"},
        ignored_headers=["Mystery Column"],
        master_path=master_path,
    )
    result = merge_csv_into_master(
        source_path,
        header_overrides={"Booking Email": "Primary_Email"},
        ignored_headers=["Mystery Column"],
        master_path=master_path,
        now=RUN_AT,
    )
    rows = _read_master_rows(master_path)

    assert preview["mapped_headers"]["Booking Email"] == "Primary_Email"
    assert preview["ignored_headers"] == ["Mystery Column"]
    assert preview["unmapped_headers"] == []
    assert result["rows_added"] == 1
    assert rows[0]["Primary_Email"] == "book@example.com"
    assert dict(HEADER_ALIASES) == alias_snapshot


def test_preview_csv_merge_counts_returns_expected_added_and_updated_rows(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Existing Act",
                Primary_Email="existing@example.com",
                Website="https://existing.example.com",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Email", "Bandcamp URL"],
        [
            {
                "Artist Name": "Existing Act",
                "Email": "existing@example.com",
                "Bandcamp URL": "https://existingact.bandcamp.com",
            },
            {
                "Artist Name": "New Act",
                "Email": "new@example.com",
                "Bandcamp URL": "https://newact.bandcamp.com",
            },
        ],
    )

    result = preview_csv_merge_counts(source_path, master_path=master_path, now=RUN_AT)

    assert result["rows_updated"] == 1
    assert result["rows_added"] == 1
    assert result["rows_skipped_duplicates"] == 0


def test_preview_csv_merge_counts_does_not_modify_master_csv(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Static Bloom", Primary_Email="hello@staticbloom.com")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Email", "Bandcamp URL"],
        [
            {
                "Artist Name": "Static Bloom",
                "Email": "hello@staticbloom.com",
                "Bandcamp URL": "https://staticbloom.bandcamp.com",
            }
        ],
    )
    before = master_path.read_text(encoding="utf-8-sig")

    result = preview_csv_merge_counts(source_path, master_path=master_path, now=RUN_AT)
    after = master_path.read_text(encoding="utf-8-sig")

    assert result["rows_updated"] == 1
    assert before == after
