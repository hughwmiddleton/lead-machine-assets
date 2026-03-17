import logging
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("PyQt5")

import night_mode_runner
import night_mode_fb
import pipeline_runner
from cross_directory_enricher import CrossDirectoryEnricherWorker, EnrichmentPayload
from email_provenance import _set_email_with_provenance


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


def test_facebook_apply_preserves_regex_provenance_for_obfuscated_rendered_email() -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(legacy_module=None, username="", password="", logger=None)
    row = {
        "Artist Name": "FB Artist",
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
    }
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body><div>No visible email</div></body></html>",
        rendered_text="Bookings name @ artist dot com",
    )
    assert emails == ["name@artist.com"]
    assert used_mailto is False

    fb_result = night_mode_fb.NightModeFacebookResult(
        email="name@artist.com",
        email_all="name@artist.com",
        email_type="fb_night",
        facebook_url="https://www.facebook.com/testartist",
        email_source="main",
        email_source_url="https://www.facebook.com/testartist",
        email_extract_method="regex",
    )
    updated = enricher._apply_night_fb_result(row, fb_result, emails, "https://www.facebook.com/testartist")

    assert updated["Email"] == "name@artist.com"
    assert updated["Email_Source_URL"] == "https://www.facebook.com/testartist"
    assert updated["Email_Source_Type"] == "facebook_enrich"
    assert updated["Email_Extract_Method"] == "regex"


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


def test_apply_payload_email_mutation_increments_summary() -> None:
    pipeline_runner.reset_email_summary_counts()
    df = pd.DataFrame(
        [
            {
                "Social Link": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
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

    assert df.at[0, "Email"] == "sc@example.com"
    assert pipeline_runner.get_email_summary_counts()["emails_found"] == 1


def test_apply_payload_metadata_only_does_not_increment_summary() -> None:
    pipeline_runner.reset_email_summary_counts()
    df = pd.DataFrame(
        [
            {
                "Social Link": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials={"https://instagram.com/testartist"},
        websites={"https://testartist.example.com"},
        emails=set(),
        link_hubs=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/testartist",
        source_detail="soundcloud_live",
    )

    CrossDirectoryEnricherWorker._apply_payload(None, df, 0, payload)

    assert df.at[0, "Email"] == ""
    assert pipeline_runner.get_email_summary_counts()["emails_found"] == 0


def test_apply_payload_same_row_same_email_does_not_double_count() -> None:
    pipeline_runner.reset_email_summary_counts()
    df = pd.DataFrame(
        [
            {
                "Social Link": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"repeat@example.com"},
        link_hubs=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/testartist",
        source_detail="soundcloud_live",
    )

    CrossDirectoryEnricherWorker._apply_payload(None, df, 0, payload)
    CrossDirectoryEnricherWorker._apply_payload(None, df, 0, payload)

    assert df.at[0, "Email"] == "repeat@example.com"
    assert pipeline_runner.get_email_summary_counts()["emails_found"] == 1


def test_set_email_with_provenance_ignores_telemetry_only_email() -> None:
    df = pd.DataFrame(
        [
            {
                "Email": "",
                "Email_All": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
            }
        ]
    )

    _set_email_with_provenance(
        (df, 0),
        "abc@o363271.ingest.us.sentry.io",
        "https://artist.example/contact",
        "website_enrich",
        "regex",
    )

    assert df.at[0, "Email"] == ""
    assert df.at[0, "Email_All"] == ""


def test_apply_payload_ignores_telemetry_only_email() -> None:
    pipeline_runner.reset_email_summary_counts()
    df = pd.DataFrame(
        [
            {
                "Social Link": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
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
        emails={"abc@o363271.ingest.us.sentry.io"},
        link_hubs=set(),
        source_dir="bandcamp",
        source_url="https://artist.bandcamp.com",
        source_detail="bandcamp_live",
    )

    CrossDirectoryEnricherWorker._apply_payload(None, df, 0, payload)

    assert df.at[0, "Email"] == ""
    assert df.at[0, "Email_All"] == ""
    assert pipeline_runner.get_email_summary_counts()["emails_found"] == 0


def test_fill_email_provenance_fallback_about_url() -> None:
    df = pd.DataFrame(
        [
            {
                "Email": "fb@example.com",
                "Facebook_URL": "https://www.facebook.com/testartist",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
            }
        ]
    )
    pipeline_runner._fill_email_provenance_fields(df, 0, source=None, fb_url_hint=df.at[0, "Facebook_URL"])
    assert df.at[0, "Email_Source_URL"] == "https://www.facebook.com/testartist/about"
    assert df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert df.at[0, "Email_Extract_Method"] == "regex"


def test_infer_email_source_uses_website_provenance() -> None:
    row = pd.Series(
        {
            "Email": "bookings@example.com",
            "Email_Type": "website_enrich",
            "Email_Source_Type": "website_enrich",
            "Source Directory": "spotify",
            "Source URL": "https://open.spotify.com/artist/abc",
        }
    )

    assert pipeline_runner.infer_email_source(row) == "Website"
