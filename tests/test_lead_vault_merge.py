import csv
import datetime as dt
import os

from lead_vault.alias_map import HEADER_ALIASES
import lead_vault.merge as merge_module
from lead_vault.merge import confirm_csv_merge_preview, merge_csv_into_master, preview_csv_import, preview_csv_merge_counts
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


def test_profile_url_match_updates_existing_row(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="The Echo",
                Source_URL="https://example.com/the-echo/",
                Date_Added="2026-01-01T00:00:00Z",
                Import_Source_File="existing.csv",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Bandcamp URL"],
        [
            {
                "Artist Name": "The Echo",
                "Profile URL": "HTTPS://EXAMPLE.COM/the-echo",
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


def test_artist_location_fallback_updates_when_both_profile_urls_are_blank(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Night Tides",
                Location=" Melbourne ",
                Source_URL="",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Location", "Email"],
        [{"Artist Name": "Night Tides", "Location": "melbourne", "Email": "alt@example.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert rows[0]["Primary_Email"] == "alt@example.com"


def test_artist_location_fallback_does_not_match_rows_with_existing_profile_url(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="City Lines", Location="Sydney", Source_URL="https://example.com/city-lines")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Location", "Email"],
        [
            {
                "Artist Name": "City Lines",
                "Location": "sydney",
                "Email": "hello@citylines.example.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 0
    assert result["rows_added"] == 1
    assert len(rows) == 2


def test_update_strategy_merges_duplicate_fields_without_data_loss(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Signals",
                Location="Melbourne",
                Source_URL="https://example.com/signals/",
                Primary_Email="artist@example.com",
                All_Emails="manager@example.com;artist@example.com",
                Facebook_URL="https://facebook.com/oldsignals",
                Instagram_URL="https://instagram.com/oldsignals",
                Website="https://signals.example.com",
                Contact_Name="Existing Contact",
                Contact_Role="",
                External_Links="https://signals.example.com/links",
                Notes="Keep this note",
            )
        ],
    )
    _write_csv(
        source_path,
        [
            "Artist Name",
            "Profile URL",
            "Email",
            "All Emails",
            "Facebook URL",
            "Instagram URL",
            "Website",
            "Contact Role",
            "Notes",
        ],
        [
            {
                "Artist Name": "Signals",
                "Profile URL": "https://example.com/signals",
                "Email": "booking@example.com",
                "All Emails": "manager@example.com;press@example.com",
                "Facebook URL": "https://facebook.com/newsignals",
                "Instagram URL": "https://instagram.com/newsignals",
                "Website": "https://new-signals.example.com",
                "Contact Role": "Manager",
                "Notes": "",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)
    rows = _read_master_rows(master_path)

    assert result["rows_updated"] == 1
    assert result["rows_duplicates_detected"] == 1
    assert rows[0]["Primary_Email"] == "artist@example.com"
    assert set(rows[0]["All_Emails"].split(";")) == {
        "artist@example.com",
        "manager@example.com",
        "press@example.com",
        "booking@example.com",
    }
    assert len(rows[0]["All_Emails"].split(";")) == 4
    assert rows[0]["Facebook_URL"] == "https://facebook.com/newsignals"
    assert rows[0]["Instagram_URL"] == "https://instagram.com/oldsignals"
    assert rows[0]["Website"] == "https://signals.example.com"
    assert rows[0]["Contact_Name"] == "Existing Contact"
    assert rows[0]["Contact_Role"] == "Manager"
    assert rows[0]["Notes"] == "Keep this note"
    assert set(rows[0]["External_Links"].split(";")) == {
        "https://signals.example.com/links",
        "https://facebook.com/oldsignals",
        "https://facebook.com/newsignals",
        "https://instagram.com/newsignals",
        "https://new-signals.example.com",
    }
    assert len(rows[0]["External_Links"].split(";")) == 5


def test_skip_strategy_leaves_existing_duplicate_unchanged_and_adds_new_rows(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Static Bloom", Source_URL="https://example.com/static-bloom", Primary_Email="hi@staticbloom.com")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Email"],
        [
            {"Artist Name": "Static Bloom", "Profile URL": "https://example.com/static-bloom/", "Email": "new@staticbloom.com"},
            {"Artist Name": "Fresh Act", "Profile URL": "https://example.com/fresh-act", "Email": "hello@fresh.example.com"},
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT, duplicate_strategy="skip")
    rows = _read_master_rows(master_path)

    assert result["rows_duplicates_detected"] == 1
    assert result["rows_skipped_duplicates"] == 1
    assert result["rows_updated"] == 0
    assert result["rows_added"] == 1
    assert len(rows) == 2
    assert rows[0]["Primary_Email"] == "hi@staticbloom.com"
    assert rows[1]["Artist"] == "Fresh Act"


def test_keep_both_strategy_inserts_duplicate_rows(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Echoes", Source_URL="https://example.com/echoes")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Email"],
        [{"Artist Name": "Echoes", "Profile URL": "https://example.com/echoes/", "Email": "echoes@example.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT, duplicate_strategy="keep_both")
    rows = _read_master_rows(master_path)

    assert result["rows_duplicates_detected"] == 1
    assert result["rows_kept_duplicates"] == 1
    assert result["rows_added"] == 1
    assert result["rows_updated"] == 0
    assert len(rows) == 2
    assert rows[0]["Artist"] == "Echoes"
    assert rows[1]["Artist"] == "Echoes"


def test_matched_row_with_no_safe_changes_counts_as_skipped_duplicate(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Static Bloom", Source_URL="https://example.com/static-bloom", Primary_Email="hi@staticbloom.com")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Email"],
        [{"Artist Name": "Static Bloom", "Profile URL": "https://example.com/static-bloom", "Email": "hi@staticbloom.com"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, now=RUN_AT)

    assert result["rows_duplicates_detected"] == 1
    assert result["rows_skipped_duplicates"] == 1
    assert result["rows_updated"] == 0


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
                Source_URL="https://example.com/existing-act",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Bandcamp URL"],
        [
            {
                "Artist Name": "Existing Act",
                "Profile URL": "https://example.com/existing-act/",
                "Bandcamp URL": "https://existingact.bandcamp.com",
            },
            {
                "Artist Name": "New Act",
                "Profile URL": "https://example.com/new-act",
                "Bandcamp URL": "https://newact.bandcamp.com",
            },
        ],
    )

    result = preview_csv_merge_counts(source_path, master_path=master_path, now=RUN_AT)

    assert result["rows_duplicates_detected"] == 1
    assert result["rows_updated"] == 1
    assert result["rows_added"] == 1
    assert result["rows_skipped_duplicates"] == 0


def test_preview_csv_merge_counts_does_not_modify_master_csv(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Static Bloom", Source_URL="https://example.com/static-bloom")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Bandcamp URL"],
        [
            {
                "Artist Name": "Static Bloom",
                "Profile URL": "https://example.com/static-bloom",
                "Bandcamp URL": "https://staticbloom.bandcamp.com",
            }
        ],
    )
    before = master_path.read_text(encoding="utf-8-sig")

    result = preview_csv_merge_counts(source_path, master_path=master_path, now=RUN_AT)
    after = master_path.read_text(encoding="utf-8-sig")

    assert result["rows_updated"] == 1
    assert before == after


def test_merge_consolidate_incoming_email_replaces_empty_existing_and_writes_backup(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Better Email", Source_URL="https://example.com/better")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email"],
        [{"Artist Name": "Better Email", "artist_url": "https://example.com/better", "Email": "new@example.com"}],
    )

    result = merge_csv_into_master(
        source_path,
        master_path=master_path,
        duplicate_strategy="merge_consolidate",
    )
    rows = _read_master_rows(master_path)

    assert result["rows_existing"] == 1
    assert result["rows_incoming"] == 1
    assert result["rows_replaced"] == 1
    assert result["rows_final"] == 1
    assert rows[0]["Primary_Email"] == "new@example.com"
    assert result["backup_path"]
    assert os.path.exists(result["backup_path"])


def test_merge_consolidate_upgrade_restores_existing_spotify_discovery_origin(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    spotify_url = "https://open.spotify.com/artist/origin-lock"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Origin Lock",
                Lead_Source="Spotify",
                Source_Directory="Spotify",
                Source_URL="",
                Spotify_URL=spotify_url,
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Lead Source", "Source Directory", "Email", "Bandcamp URL"],
        [
            {
                "Artist Name": "Origin Lock",
                "Lead Source": "Bandcamp",
                "Source Directory": "bandcamp",
                "Email": "artist@example.com",
                "Bandcamp URL": "https://origin-lock.bandcamp.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    row = _read_master_rows(master_path)[0]

    assert result["rows_replaced"] == 1
    assert row["Lead_Source"] == "Spotify"
    assert row["Source_Directory"] == "Spotify"
    assert row["Source_URL"] == spotify_url
    assert row["Bandcamp_URL"] == "https://origin-lock.bandcamp.com"
    assert row["Primary_Email"] == "artist@example.com"


def test_merge_consolidate_protects_existing_email_from_empty_incoming(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Protected", Source_URL="https://example.com/protected", Primary_Email="keep@example.com")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Facebook URL"],
        [{"Artist Name": "Protected", "artist_url": "https://example.com/protected", "Facebook URL": "https://facebook.com/protected"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert result["rows_kept_existing"] == 1
    assert rows[0]["Primary_Email"] == "keep@example.com"
    assert rows[0]["Facebook_URL"] == ""


def test_merge_consolidate_email_all_unique_count_breaks_email_tie(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Email Tie",
                Source_URL="https://example.com/tie",
                Primary_Email="one@example.com",
                All_Emails="one@example.com",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email", "Email_All"],
        [
            {
                "Artist Name": "Email Tie",
                "artist_url": "https://example.com/tie",
                "Email": "two@example.com",
                "Email_All": "two@example.com, Two@example.com | three@example.com; four@example.com",
            }
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert result["rows_replaced"] == 1
    assert rows[0]["Primary_Email"] == "two@example.com"
    assert rows[0]["All_Emails"] == "two@example.com, Two@example.com | three@example.com; four@example.com"


def test_merge_consolidate_adds_new_artist_and_prevents_duplicate_explosion(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(master_path, get_canonical_master_schema(), [])
    _write_csv(
        source_path,
        ["Artist Name", "Email", "Facebook URL"],
        [
            {"Artist Name": "New Artist", "Email": "", "Facebook URL": ""},
            {"Artist Name": " new artist ", "Email": "", "Facebook URL": "https://facebook.com/newartist"},
            {"Artist Name": "New Artist", "Email": "artist@example.com", "Facebook URL": ""},
        ],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert result["rows_added_new"] == 1
    assert result["rows_final"] == 1
    assert len(rows) == 1
    assert rows[0]["Primary_Email"] == "artist@example.com"


def test_merge_consolidate_no_email_uses_richer_enrichment_fields(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="No Email", Source_URL="https://example.com/no-email")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Facebook URL", "Instagram URL", "External Links"],
        [
            {
                "Artist Name": "No Email",
                "artist_url": "https://example.com/no-email",
                "Facebook URL": "https://facebook.com/noemail",
                "Instagram URL": "https://instagram.com/noemail",
                "External Links": "https://noemail.example.com",
            }
        ],
    )

    merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert rows[0]["Facebook_URL"] == "https://facebook.com/noemail"
    assert rows[0]["Instagram_URL"] == "https://instagram.com/noemail"
    assert rows[0]["External_Links"] == "https://noemail.example.com"


def test_merge_consolidate_stability_bias_keeps_equal_existing_row(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Stable", Source_URL="https://example.com/stable", Notes="existing")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Notes"],
        [{"Artist Name": "Stable", "artist_url": "https://example.com/stable", "Notes": "incoming"}],
    )

    result = merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert result["rows_kept_existing"] == 1
    assert rows[0]["Notes"] == "existing"


def test_merge_consolidate_mixed_key_isolation_retains_url_and_name_rows(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Same Name", Source_URL="https://example.com/same-name")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Email"],
        [{"Artist Name": "same name", "Email": "name-only@example.com"}],
    )

    merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert len(rows) == 2
    assert {row["Source_URL"] for row in rows} == {"https://example.com/same-name", ""}


def test_merge_consolidate_null_key_is_logged_without_crash(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(master_path, get_canonical_master_schema(), [])
    _write_csv(source_path, ["Artist Name", "artist_url", "Email"], [{"Artist Name": "None", "artist_url": "nan", "Email": "x@example.com"}])

    result = merge_csv_into_master(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    rows = _read_master_rows(master_path)

    assert result["rows_errors"] == 1
    assert rows == []


def test_merge_consolidate_preview_classifies_rows_and_does_not_write_or_backup(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(Artist="Upgrade", Source_URL="https://example.com/upgrade"),
            _master_row(
                Artist="Keep",
                Source_URL="https://example.com/keep",
                Primary_Email="keep@example.com",
                All_Emails="keep@example.com",
            ),
            _master_row(
                Artist="Same",
                Source_URL="https://example.com/same",
                Primary_Email="same@example.com",
                All_Emails="same@example.com",
            ),
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email", "Email_All"],
        [
            {"Artist Name": "New", "artist_url": "https://example.com/new", "Email": "new@example.com", "Email_All": ""},
            {"Artist Name": "Upgrade", "artist_url": "https://example.com/upgrade", "Email": "upgrade@example.com", "Email_All": "team@example.com"},
            {"Artist Name": "Keep", "artist_url": "https://example.com/keep", "Email": "", "Email_All": ""},
            {"Artist Name": "Same", "artist_url": "https://example.com/same", "Email": "SAME@example.com ", "Email_All": " same@example.com "},
        ],
    )
    before = master_path.read_text(encoding="utf-8-sig")

    result = preview_csv_merge_counts(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")

    assert master_path.read_text(encoding="utf-8-sig") == before
    assert not list(tmp_path.glob("master_backup_*.csv"))
    assert result["preview_session_id"]
    assert result["dry_run"] is True
    assert result["merge_preview_counts"] == {
        "NEW": 1,
        "UPGRADE": 1,
        "KEEP_EXISTING": 1,
        "UNCHANGED": 1,
    }
    assert sum(result["merge_preview_counts"].values()) == result["row_count"]
    assert result["rows_final"] == 4
    assert len(result["merge_preview_upgrade_rows"]) == 1
    upgrade = result["merge_preview_upgrade_rows"][0]
    assert upgrade["artist_name"] == "Upgrade"
    assert upgrade["existing_email"] == ""
    assert upgrade["incoming_email"] == "upgrade@example.com"
    assert upgrade["incoming_email_all_count"] == 1


def test_confirm_merge_preview_writes_cached_snapshot_and_backup(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [_master_row(Artist="Upgrade", Source_URL="https://example.com/upgrade")],
    )
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email"],
        [{"Artist Name": "Upgrade", "artist_url": "https://example.com/upgrade", "Email": "upgrade@example.com"}],
    )
    preview = preview_csv_merge_counts(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")

    confirmed = confirm_csv_merge_preview(preview, preview_session_id=preview["preview_session_id"])
    rows = _read_master_rows(master_path)

    assert confirmed["merge_preview_counts"] == preview["merge_preview_counts"]
    assert confirmed["rows_final"] == preview["rows_final"]
    assert confirmed["backup_path"]
    assert os.path.exists(confirmed["backup_path"])
    assert rows == preview["merge_result_snapshot"]["final_rows"]
    assert rows[0]["Primary_Email"] == "upgrade@example.com"


def test_confirm_merge_preview_blocks_when_master_changes_after_preview(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(master_path, get_canonical_master_schema(), [_master_row(Artist="Stable", Source_URL="https://example.com/stable")])
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email"],
        [{"Artist Name": "Stable", "artist_url": "https://example.com/stable", "Email": "new@example.com"}],
    )
    preview = preview_csv_merge_counts(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")
    before_snapshot = preview["merge_result_snapshot"]["final_rows"]
    _write_csv(master_path, get_canonical_master_schema(), [_master_row(Artist="Changed", Source_URL="https://example.com/changed")])

    try:
        confirm_csv_merge_preview(preview, preview_session_id=preview["preview_session_id"])
    except ValueError as exc:
        assert "Master CSV changed" in str(exc)
    else:
        raise AssertionError("Expected changed master CSV to block preview confirmation")

    rows = _read_master_rows(master_path)
    assert rows != before_snapshot
    assert rows[0]["Artist"] == "Changed"
    assert not list(tmp_path.glob("master_backup_*.csv"))


def test_confirm_merge_preview_blocks_session_mismatch(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(master_path, get_canonical_master_schema(), [_master_row(Artist="Stable", Source_URL="https://example.com/stable")])
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email"],
        [{"Artist Name": "Stable", "artist_url": "https://example.com/stable", "Email": "new@example.com"}],
    )
    preview = preview_csv_merge_counts(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")

    try:
        confirm_csv_merge_preview(preview, preview_session_id="wrong-session")
    except ValueError as exc:
        assert "session mismatch" in str(exc)
    else:
        raise AssertionError("Expected session mismatch to block preview confirmation")

    assert not list(tmp_path.glob("master_backup_*.csv"))


def test_confirm_merge_preview_does_not_recompute_scoring_or_deduplication(tmp_path, monkeypatch):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "import.csv"
    _write_csv(master_path, get_canonical_master_schema(), [_master_row(Artist="Stable", Source_URL="https://example.com/stable")])
    _write_csv(
        source_path,
        ["Artist Name", "artist_url", "Email"],
        [{"Artist Name": "Stable", "artist_url": "https://example.com/stable", "Email": "new@example.com"}],
    )
    preview = preview_csv_merge_counts(source_path, master_path=master_path, duplicate_strategy="merge_consolidate")

    def fail_if_recomputed(*args, **kwargs):
        raise AssertionError("confirm recomputed merge preview internals")

    monkeypatch.setattr(merge_module, "_score_lead_vault_row", fail_if_recomputed)
    monkeypatch.setattr(merge_module, "_lead_vault_consolidation_key", fail_if_recomputed)

    confirmed = confirm_csv_merge_preview(preview, preview_session_id=preview["preview_session_id"])
    rows = _read_master_rows(master_path)

    assert confirmed["rows_final"] == preview["rows_final"]
    assert rows[0]["Primary_Email"] == "new@example.com"
