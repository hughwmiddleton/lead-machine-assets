import csv
import logging
from pathlib import Path

import pandas as pd
import pytest

import night_mode_runner
import pipeline_runner
from lead_vault.exporter import _build_legacy_final_export_bridge_frame
from lead_vault.origin import repair_origin_integrity_df, safe_row_update, validate_origin_integrity_rows


def _write_csv(path: Path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


def test_bandcamp_job_populates_canonical_source_fields(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_bandcamp_1"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    _write_csv(
        raw_csv,
        ["Artist Name", "Location", "Song Title", "Email"],
        [
            {"Artist Name": "Bandcamp Act", "Location": "Berlin", "Song Title": "Track 1", "Email": ""},
        ],
    )

    logger = logging.getLogger("test_bandcamp_source")
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_bandcamp_1", "raw_csv": raw_csv.as_posix(), "source_directory": "bandcamp"}],
        logger,
    )

    assert master_path is not None
    df = _load_csv(Path(master_path))
    assert len(df) == 1
    assert df.at[0, "Lead_Source"] == "bandcamp"
    assert df.at[0, "Source_Directory"] == "bandcamp"
    assert df.at[0, "Source Directory"] == "bandcamp"
    assert df.at[0, "__source_job"] == "job_bandcamp_1"


def test_soundcloud_job_populates_canonical_source_fields(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_soundcloud_2"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    _write_csv(
        raw_csv,
        ["Artist Name", "Location", "Song Title", "Email"],
        [
            {"Artist Name": "SC Act", "Location": "London", "Song Title": "Track 2", "Email": ""},
        ],
    )

    logger = logging.getLogger("test_sc_source")
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_soundcloud_2", "raw_csv": raw_csv.as_posix(), "source_directory": "soundcloud"}],
        logger,
    )

    assert master_path is not None
    df = _load_csv(Path(master_path))
    assert len(df) == 1
    assert df.at[0, "Lead_Source"] == "soundcloud"
    assert df.at[0, "Source_Directory"] == "soundcloud"
    assert df.at[0, "Source Directory"] == "soundcloud"
    assert df.at[0, "__source_job"] == "job_soundcloud_2"


def test_unearthed_job_normalizes_canonical_source_fields(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_unearthed_1"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    _write_csv(
        raw_csv,
        ["Artist Name", "Location", "Song Title", "Email"],
        [
            {"Artist Name": "Unearthed Act", "Location": "Sydney", "Song Title": "Track 3", "Email": ""},
        ],
    )

    logger = logging.getLogger("test_ue_source")
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_unearthed_1", "raw_csv": raw_csv.as_posix(), "source_directory": "unearthed"}],
        logger,
    )

    assert master_path is not None
    df = _load_csv(Path(master_path))
    assert len(df) == 1
    assert df.at[0, "Lead_Source"] == "Triple J Unearthed"
    assert df.at[0, "Source_Directory"] == "unearthed"
    assert df.at[0, "Source Directory"] == "Triple J Unearthed"


def test_existing_source_fields_not_overwritten_by_merge(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_spotify_1"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    _write_csv(
        raw_csv,
        ["Artist Name", "Lead_Source", "Source_Directory", "Source Directory", "Email"],
        [
            {
                "Artist Name": "Spotify Act",
                "Lead_Source": "spotify",
                "Source_Directory": "spotify",
                "Source Directory": "spotify",
                "Email": "",
            },
        ],
    )

    logger = logging.getLogger("test_spotify_preserve")
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_spotify_1", "raw_csv": raw_csv.as_posix(), "source_directory": "spotify"}],
        logger,
    )

    assert master_path is not None
    df = _load_csv(Path(master_path))
    assert df.at[0, "Lead_Source"] == "spotify"
    assert df.at[0, "Source_Directory"] == "spotify"
    assert df.at[0, "Source Directory"] == "spotify"


def test_cross_directory_enrichment_preserves_original_origin():
    row = {
        "Lead_Source": "bandcamp",
        "Source_Directory": "bandcamp",
        "Source Directory": "bandcamp",
        "Email_Source_Type": "",
        "SoundCloud_URL": "",
    }

    safe_row_update(
        row,
        {
            "Email_Source_Type": "soundcloud",
            "Email_Source_URL": "https://soundcloud.com/example",
            "SoundCloud_URL": "https://soundcloud.com/example",
            "Source_Directory": "soundcloud",
            "Lead_Source": "soundcloud",
        },
    )
    repair_origin_integrity_df(pd.DataFrame([row]))

    assert row["Lead_Source"] == "bandcamp"
    assert row["Source_Directory"] == "bandcamp"
    assert row["Source Directory"] == "bandcamp"
    assert row["Email_Source_Type"] == "soundcloud"
    assert row["SoundCloud_URL"] == "https://soundcloud.com/example"


def test_pipeline_survival_from_raw_to_final_export(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_bandcamp_1"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    _write_csv(
        raw_csv,
        ["Artist Name", "Location", "Song Title", "Email", "Email_Source_URL", "final_status"],
        [
            {
                "Artist Name": "Bandcamp Act",
                "Location": "Berlin",
                "Song Title": "Track 1",
                "Email": "act@example.com",
                "Email_Source_URL": "https://bandcamp.com/act",
                "final_status": "OK",
            },
        ],
    )

    logger = logging.getLogger("test_pipeline_survival")
    master_raw = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_bandcamp_1", "raw_csv": raw_csv.as_posix(), "source_directory": "bandcamp"}],
        logger,
    )

    df = _load_csv(Path(master_raw))
    df = repair_origin_integrity_df(df)
    validate_origin_integrity_rows(df.to_dict(orient="records"))

    bridge_df = _build_legacy_final_export_bridge_frame(df)
    export_df = pipeline_runner._build_final_export_frame(bridge_df)

    assert export_df.at[0, "Lead_Source"] == "bandcamp"
    assert export_df.at[0, "Source_Directory"] == "bandcamp"
    assert export_df.at[0, "Source Directory"] == "bandcamp"


def test_fallback_derives_source_from_job_id_when_source_directory_missing(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_lastfm_1"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    _write_csv(
        raw_csv,
        ["Artist Name", "Location", "Song Title", "Email"],
        [{"Artist Name": "Last.fm Act", "Location": "Tokyo", "Song Title": "Track 4", "Email": ""}],
    )

    logger = logging.getLogger("test_fallback")
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_lastfm_1", "raw_csv": raw_csv.as_posix()}],
        logger,
    )

    df = _load_csv(Path(master_path))
    assert df.at[0, "Lead_Source"] == "lastfm"
    assert df.at[0, "Source_Directory"] == "lastfm"
    assert df.at[0, "Source Directory"] == "lastfm"
