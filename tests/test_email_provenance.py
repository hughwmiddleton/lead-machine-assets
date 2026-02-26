import logging
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("PyQt5")

import night_mode_runner
import night_mode_fb
import pipeline_runner
from cross_directory_enricher import CrossDirectoryEnricherWorker, EnrichmentPayload


def test_merge_raw_master_counts_missing_provenance(tmp_path: Path) -> None:
    run_dir = tmp_path
    job_csv = run_dir / "job_a_raw.csv"
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Test Artist",
                "Email": "a@example.com",
                "Email_Source_URL": "",
                "Source Directory": "spotify",
                "Source URL": "https://open.spotify.com/artist/abc",
            }
        ]
    )
    df.to_csv(job_csv, index=False)
    stats = night_mode_runner.SmokeStats()
    logger = logging.getLogger("test_provenance")
    result = night_mode_runner._merge_raw_master(
        run_dir.as_posix(), [{"job_id": "job_a", "raw_csv": job_csv.as_posix()}], logger, stats=stats
    )

    assert result is not None
    assert stats.emails_total == 1
    assert stats.emails_missing_source_url == 1


def test_export_columns_include_provenance() -> None:
    for cols in (
        pipeline_runner.DEFAULT_EXPORT_COLUMNS,
        pipeline_runner.FINAL_EXPORT_COLUMNS,
        pipeline_runner.WOODPECKER_EXPORT_COLUMNS,
    ):
        assert "Email_Source_URL" in cols
        assert "Email_Source_Type" in cols
        assert "Email_Extract_Method" in cols


def test_facebook_apply_sets_provenance() -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(legacy_module=None, username="", password="", logger=None)
    row = {
        "Artist Name": "FB Artist",
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
    }
    fb_result = night_mode_fb.NightModeFacebookResult(
        email="fb@example.com",
        email_all="fb@example.com",
        email_type="fb_night",
        facebook_url="https://www.facebook.com/testartist",
        email_source="main",
        email_source_url="https://www.facebook.com/testartist/about",
        email_extract_method="mailto",
    )
    updated = enricher._apply_night_fb_result(row, fb_result, ["fb@example.com"], "https://www.facebook.com/testartist")
    assert updated["Email_Source_URL"] == "https://www.facebook.com/testartist"
    assert updated["Email_Source_Type"] == "facebook_enrich"
    assert updated["Email_Extract_Method"] == "mailto"


def test_soundcloud_apply_sets_provenance() -> None:
    df = pd.DataFrame(
        [
            {
                "Social Link": "",
                "External Links": "",
                "Email": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "SoundCloud Link": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"sc@example.com"},
        link_hubs=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/testartist",
        source_detail="soundcloud_live",
    )
    CrossDirectoryEnricherWorker._apply_payload(None, df, 0, payload)
    row = df.loc[0]
    assert row["Email"] == "sc@example.com"
    assert row["Email_Source_URL"] == "https://soundcloud.com/testartist"
    assert row["Email_Source_Type"] == "soundcloud"
    assert row["Email_Extract_Method"] == "regex"
