import csv

import pandas as pd
import pytest

import pipeline_runner
from lead_vault.exporter import WOODPECKER_EXPORT_PRESET, export_with_preset
from lead_vault.merge import merge_csv_into_master
from lead_vault.origin import (
    OriginIntegrityError,
    OriginLockedRow,
    merge_origin_fields,
    preserve_origin_fields,
    repair_origin_fields,
    repair_origin_integrity_df,
    safe_row_update,
    validate_origin_integrity_rows,
)
from lead_vault.schema import get_canonical_master_schema


def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _master_row(**overrides):
    row = {field: "" for field in get_canonical_master_schema()}
    row.update(overrides)
    return row


def test_origin_persists_when_email_source_changes_to_soundcloud():
    row = {
        "Lead_Source": "unearthed",
        "Source_Directory": "unearthed",
        "Source Directory": "unearthed",
        "Email_Source_Type": "",
    }

    safe_row_update(
        row,
        {
            "Email_Source_Type": "soundcloud",
            "Email_Source_URL": "https://soundcloud.com/example",
            "Source_Directory": "soundcloud",
            "Lead_Source": "soundcloud",
        },
    )
    repair_origin_fields(row)

    assert row["Lead_Source"] == "unearthed"
    assert row["Source_Directory"] == "unearthed"
    assert row["Source Directory"] == "unearthed"
    assert row["Email_Source_Type"] == "soundcloud"


def test_guarded_mutation_blocks_origin_overwrite():
    row = {
        "Lead_Source": "unearthed",
        "Source_Directory": "unearthed",
        "Source Directory": "unearthed",
        "Source URL": "https://www.abc.net.au/triplejunearthed/artist/origin-lock",
        "Source_URL": "https://www.abc.net.au/triplejunearthed/artist/origin-lock",
    }

    safe_row_update(
        row,
        {
            "Source_Directory": "soundcloud",
            "Source Directory": "soundcloud",
            "Source URL": "https://soundcloud.com/origin-lock",
            "Source_URL": "https://soundcloud.com/origin-lock",
        },
    )

    assert row["Source_Directory"] == "unearthed"
    assert row["Source Directory"] == "unearthed"
    assert row["Source URL"].startswith("https://www.abc.net.au/")
    assert row["Source_URL"].startswith("https://www.abc.net.au/")


def test_guarded_mutation_allows_blank_origin_initialization():
    row = {"Lead_Source": "", "Source_Directory": "", "Source URL": ""}

    safe_row_update(
        row,
        {"Lead_Source": "Bandcamp", "Source_Directory": "bandcamp", "Source URL": "https://act.bandcamp.com"},
    )

    assert row == {
        "Lead_Source": "Bandcamp",
        "Source_Directory": "bandcamp",
        "Source URL": "https://act.bandcamp.com",
    }


def test_merge_origin_fields_preserves_existing_values_and_backfills_blanks():
    row = {"Lead_Source": "Bandcamp", "Source_Directory": "bandcamp", "Source_URL": ""}

    merge_origin_fields(
        row,
        {"Lead_Source": "SoundCloud", "Source_Directory": "soundcloud", "Source_URL": "https://act.bandcamp.com"},
    )

    assert row == {
        "Lead_Source": "Bandcamp",
        "Source_Directory": "bandcamp",
        "Source_URL": "https://act.bandcamp.com",
    }


def test_replacement_restores_spotify_origin_from_existing_row():
    replacement = {
        "Lead_Source": "Bandcamp",
        "Source_Directory": "bandcamp",
        "Source Directory": "Bandcamp",
        "Source URL": "https://act.bandcamp.com",
        "Source_URL": "https://act.bandcamp.com",
    }
    original = {
        "Lead_Source": "Spotify",
        "Source_Directory": "Spotify",
        "Source Directory": "Spotify",
        "Source URL": "https://open.spotify.com/artist/abc",
        "Source_URL": "https://open.spotify.com/artist/abc",
        "Spotify_URL": "https://open.spotify.com/artist/abc",
    }

    preserve_origin_fields(replacement, original)

    assert replacement == {
        "Lead_Source": "Spotify",
        "Source_Directory": "Spotify",
        "Source Directory": "Spotify",
        "Source URL": "https://open.spotify.com/artist/abc",
        "Source_URL": "https://open.spotify.com/artist/abc",
    }


def test_origin_locked_row_update_blocks_origin_overwrite():
    row = OriginLockedRow({"Lead_Source": "unearthed", "Source_Directory": "unearthed"})

    row.update({"Source_Directory": "soundcloud", "Lead_Source": "soundcloud"})

    assert row["Lead_Source"] == "unearthed"
    assert row["Source_Directory"] == "unearthed"


def test_blank_source_directory_repairs_from_lead_source():
    row = {"Lead_Source": "unearthed", "Source_Directory": ""}

    repair_origin_fields(row)

    assert row["Source_Directory"] == "unearthed"


def test_merge_conflict_keeps_existing_lead_source(tmp_path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "incoming.csv"
    _write_csv(
        master_path,
        get_canonical_master_schema(),
        [
            _master_row(
                Artist="Origin Lock",
                Source_URL="https://example.com/origin-lock",
                Lead_Source="unearthed",
                Source_Directory="unearthed",
            )
        ],
    )
    _write_csv(
        source_path,
        ["Artist Name", "Profile URL", "Lead Source", "Source Directory", "Email"],
        [
            {
                "Artist Name": "Origin Lock",
                "Profile URL": "https://example.com/origin-lock",
                "Lead Source": "soundcloud",
                "Source Directory": "soundcloud",
                "Email": "artist@example.com",
            }
        ],
    )

    merge_csv_into_master(source_path, master_path=master_path)

    with open(master_path, "r", newline="", encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["Lead_Source"] == "unearthed"
    assert row["Source_Directory"] == "unearthed"
    assert row["Primary_Email"] == "artist@example.com"


def test_export_integrity_repair_and_validation(tmp_path):
    valid = {"Lead_Source": "unearthed", "Source_Directory": "unearthed"}
    validate_origin_integrity_rows([valid])
    validate_origin_integrity_rows(
        [
            {
                "Lead_Source": "Triple J Unearthed",
                "Source_Directory": "unearthed",
                "Source Directory": "Triple J Unearthed",
            }
        ]
    )

    with pytest.raises(OriginIntegrityError):
        validate_origin_integrity_rows([{"Lead_Source": "unearthed", "Source_Directory": "soundcloud"}])


def test_undiscovered_music_origin_pair_and_canonical_url_pass_export_validation():
    validate_origin_integrity_rows(
        [
            {
                "Lead_Source": "Undiscovered Music",
                "Source_Directory": "undiscovered_music",
                "Source Directory": "Undiscovered Music",
                "Source URL": "https://undiscovered.music/artists/example-artist",
            }
        ]
    )


@pytest.mark.parametrize(
    ("lead_source", "source_directory"),
    [
        ("Undiscovered Music", "soundcloud"),
        ("Spotify", "undiscovered_music"),
    ],
)
def test_undiscovered_music_origin_cross_source_pairings_still_fail(lead_source, source_directory):
    with pytest.raises(OriginIntegrityError):
        validate_origin_integrity_rows(
            [{"Lead_Source": lead_source, "Source_Directory": source_directory}]
        )


@pytest.mark.parametrize(
    ("lead_source", "source_directory"),
    [
        ("AMRAP", "amrap"),
        ("Jamendo", "jamendo"),
        ("Spotify", "spotify"),
        ("Triple J Unearthed", "unearthed"),
    ],
)
def test_existing_origin_pairings_remain_accepted(lead_source, source_directory):
    validate_origin_integrity_rows(
        [{"Lead_Source": lead_source, "Source_Directory": source_directory}]
    )


def test_woodpecker_export_fails_on_blank_origin(tmp_path):
    master_path = tmp_path / "master.csv"
    output_path = tmp_path / "woodpecker.csv"
    row = _master_row(
        Artist="Blank Origin",
        Primary_Email="blank@example.com",
        All_Emails="blank@example.com",
        Lead_Source="",
        Source_Directory="",
    )
    _write_csv(master_path, get_canonical_master_schema(), [row])

    with pytest.raises(OriginIntegrityError):
        export_with_preset(WOODPECKER_EXPORT_PRESET, master_path, output_path)


def test_dataframe_blank_repair_keeps_source_directory_mirroring_lead_source():
    df = pd.DataFrame([{"Lead_Source": "unearthed", "Source_Directory": "", "Source Directory": ""}])

    repaired = repair_origin_integrity_df(df)

    assert repaired.at[0, "Lead_Source"] == "unearthed"
    assert repaired.at[0, "Source_Directory"] == "unearthed"
    assert repaired.at[0, "Source Directory"] == "unearthed"


def test_unearthed_source_row_creation_sets_canonical_origin_fields(tmp_path):
    output_path = tmp_path / "raw.csv"

    pipeline_runner._write_rows_to_csv(
        [{"Artist Name": "Unearthed Act", "Email": "act@example.com"}],
        output_path.as_posix(),
        source_directory="unearthed",
    )

    df = pd.read_csv(output_path, dtype=str, keep_default_na=False).fillna("")
    row = df.iloc[0].to_dict()
    assert row["Lead_Source"] == "Triple J Unearthed"
    assert row["Source_Directory"] == "unearthed"
    assert row["Source Directory"] == "Triple J Unearthed"


def test_final_export_projection_preserves_origin_contract_fields():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed Act",
                "Email": "act@example.com",
                "Email_All": "act@example.com",
                "Email_Source_URL": "https://example.com/contact",
                "Email_Source_Type": "website_enrich",
                "final_status": "OK",
                "Lead_Source": "Triple J Unearthed",
                "Source_Directory": "unearthed",
                "Source Directory": "Triple J Unearthed",
            }
        ]
    )

    export_df = pipeline_runner._build_final_export_frame(df)

    assert export_df.at[0, "Lead_Source"] == "Triple J Unearthed"
    assert export_df.at[0, "Source_Directory"] == "unearthed"
    assert export_df.at[0, "Source Directory"] == "Triple J Unearthed"


def test_export_master_leads_keeps_fail_closed_on_blank_origin(tmp_path):
    input_path = tmp_path / "master.csv"
    output_path = tmp_path / "master_export_leads.csv"
    pd.DataFrame(
        [
            {
                "Artist Name": "Malformed",
                "Email": "bad@example.com",
                "Email_All": "bad@example.com",
                "final_status": "OK",
                "Lead_Source": "",
                "Source_Directory": "",
            }
        ]
    ).to_csv(input_path, index=False)

    pipeline_runner.export_master_leads(input_path.as_posix(), output_path.as_posix(), export_profile="full_dump")

    assert not output_path.exists()
