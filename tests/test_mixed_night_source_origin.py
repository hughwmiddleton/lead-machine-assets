import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import cross_directory_enricher
import final_checker
import night_mode_runner
import origin_validator
import pipeline_runner


def _write_seed(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row]).to_csv(path, index=False)


def test_bandcamp_directory_job_backfills_origin_at_seed_boundary(monkeypatch, tmp_path):
    def fake_scrape_bandcamp(_seed, *, existing_csv, **_kwargs):
        _write_seed(
            Path(existing_csv),
            {
                "Artist Name": "Bandcamp Act",
                "Profile URL": "https://bandcamp-act.bandcamp.com",
                "Lead_Source": "",
                "Source_Directory": "",
                "Source Directory": "",
            },
        )

    monkeypatch.setattr(
        pipeline_runner,
        "_load_legacy_module",
        lambda: SimpleNamespace(
            scrape_bandcamp=fake_scrape_bandcamp,
            BANDCAMP_PAGES_PER_TAG=5,
            BANDCAMP_TARGET_ROWS=200,
        ),
    )

    output_path = tmp_path / "job_bandcamp_1" / "raw.csv"
    pipeline_runner.run_directory_job(
        {"job_id": "job_bandcamp_1", "directory": "bandcamp", "target_valid_leads": 1},
        output_path.as_posix(),
    )

    row = pd.read_csv(output_path, dtype=str, keep_default_na=False).iloc[0]
    assert row["Lead_Source"] == "bandcamp"
    assert row["Source_Directory"] == "bandcamp"
    assert row["Source Directory"] == "bandcamp"


def test_seed_origin_backfill_preserves_existing_valid_values(tmp_path):
    output_path = tmp_path / "raw.csv"
    _write_seed(
        output_path,
        {
            "Artist Name": "Already Canonical",
            "Lead_Source": "Bandcamp",
            "Source_Directory": "Bandcamp",
            "Source Directory": "Bandcamp",
        },
    )

    pipeline_runner.ensure_seed_origin_fields(output_path, "bandcamp")

    row = pd.read_csv(output_path, dtype=str, keep_default_na=False).iloc[0]
    assert row["Lead_Source"] == "Bandcamp"
    assert row["Source_Directory"] == "Bandcamp"
    assert row["Source Directory"] == "Bandcamp"


def test_mixed_bandcamp_unearthed_origin_survives_to_client_export(monkeypatch, tmp_path):
    bandcamp_raw = tmp_path / "job_bandcamp_1" / "raw.csv"
    unearthed_raw = tmp_path / "job_unearthed_1" / "raw.csv"
    _write_seed(
        bandcamp_raw,
        {
            "Artist Name": "Bandcamp Act",
            "Email": "bandcamp@example.test",
            "Email_All": "bandcamp@example.test",
            "Email_Source_URL": "https://bandcamp-act.bandcamp.com/contact",
            "final_status": "OK",
            "Lead_Source": "",
            "Source_Directory": "",
            "Source Directory": "",
        },
    )
    _write_seed(
        unearthed_raw,
        {
            "Artist Name": "Unearthed Act",
            "Email": "unearthed@example.test",
            "Email_All": "unearthed@example.test",
            "Email_Source_URL": "https://www.abc.net.au/triplejunearthed/artist/example",
            "final_status": "OK",
            "Lead_Source": "Triple J Unearthed",
            "Source_Directory": "unearthed",
            "Source Directory": "Triple J Unearthed",
        },
    )
    pipeline_runner.ensure_seed_origin_fields(bandcamp_raw, "bandcamp")
    pipeline_runner.ensure_seed_origin_fields(unearthed_raw, "unearthed")

    master_raw = night_mode_runner._merge_raw_master(
        tmp_path.as_posix(),
        [
            {"job_id": "job_bandcamp_1", "raw_csv": bandcamp_raw.as_posix()},
            {"job_id": "job_unearthed_1", "raw_csv": unearthed_raw.as_posix()},
        ],
        logging.getLogger("mixed-night-source-origin"),
    )

    def fake_cross_directory_enrichment(seed_csv_path, output_csv_path, **_kwargs):
        df = pd.read_csv(seed_csv_path, dtype=str, keep_default_na=False).fillna("")
        bandcamp_mask = df["Artist Name"].eq("Bandcamp Act")
        df.loc[bandcamp_mask, "Instagram_URL"] = "https://instagram.com/bandcamp-act"
        df.loc[bandcamp_mask, "SoundCloud Link"] = "https://soundcloud.com/bandcamp-act"
        df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")

    def fake_auto_validate(csv_path, output_path=None, **_kwargs):
        target = output_path or csv_path
        if target != csv_path:
            shutil.copyfile(csv_path, target)
        return target

    monkeypatch.setattr(
        cross_directory_enricher,
        "run_cross_directory_enrichment",
        fake_cross_directory_enrichment,
    )
    monkeypatch.setattr(origin_validator, "run_auto_validate", fake_auto_validate)
    monkeypatch.setattr(final_checker, "run_final_checker", lambda path: path)

    master_enriched = tmp_path / "master_enriched.csv"
    pipeline_runner.run_master_enrichment(
        master_raw,
        master_enriched.as_posix(),
        enable_live_search=False,
        night_mode=True,
    )
    master_pre_fb = tmp_path / "master_pre_fb.csv"
    pipeline_runner.run_enrichment(
        master_enriched.as_posix(), master_pre_fb.as_posix(), night_mode=True
    )

    post_fb = pd.read_csv(master_pre_fb, dtype=str, keep_default_na=False).fillna("")
    bandcamp_mask = post_fb["Artist Name"].eq("Bandcamp Act")
    post_fb.loc[bandcamp_mask, "Facebook_URL"] = "https://facebook.com/bandcamp-act"
    post_fb.loc[bandcamp_mask, "Email_Source_Type"] = "facebook_enrich"
    master_post_fb = tmp_path / "master_post_fb.csv"
    post_fb.to_csv(master_post_fb, index=False)

    master_final = tmp_path / "master_enriched_deduped.csv"
    pipeline_runner.run_enrichment(
        master_post_fb.as_posix(), master_final.as_posix(), night_mode=True
    )
    export_path = tmp_path / "master_export_leads.csv"
    pipeline_runner.export_master_leads(
        master_final.as_posix(), export_path.as_posix(), export_profile="full_dump"
    )

    exported = pd.read_csv(export_path, dtype=str, keep_default_na=False).set_index("Artist Name")
    assert exported.loc["Bandcamp Act", "Lead_Source"] == "bandcamp"
    assert exported.loc["Bandcamp Act", "Source_Directory"] == "bandcamp"
    assert exported.loc["Unearthed Act", "Lead_Source"] == "Triple J Unearthed"
    assert exported.loc["Unearthed Act", "Source_Directory"] == "unearthed"
