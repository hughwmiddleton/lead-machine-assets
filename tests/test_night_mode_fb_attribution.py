import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import night_mode_runner
import pipeline_runner
from fb_attribution import (
    FB_ATTEMPT_STATE_COL,
    FB_GATE_STATE_COL,
    FB_OPPORTUNITY_STATE_COL,
    FB_WRITE_STATE_COL,
)


class StaticFBHelper:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_session_failure(self):
        return False, ""

    def get_pass_a_counts(self):
        return {}

    def enrich_row_with_facebook_night(self, row, row_index=0):
        self.calls += 1
        self.rows.append({"row": dict(row or {}), "row_index": row_index})
        if callable(self.payload):
            return dict(self.payload(row, row_index) or {})
        return dict(self.payload or {})


def _make_dummy_module():
    return SimpleNamespace(scrape_csv=lambda *args, **kwargs: None)


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


def _run_night_fb_pass(monkeypatch, tmp_path, rows, helper):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(rows).to_csv(input_csv, index=False)

    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _make_dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    status = pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=True,
    )
    return _read_csv(output_csv), status


def test_upstream_identity_anchor_enters_discovery_fallback_path(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "No FB Artist",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "https://soundcloud.com/no-fb-artist/tracks",
                "Social Link": "",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_discovery_fallback_eligible"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_no_facebook_url_without_identity_anchor_skips_night_discovery(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Weak FB Artist",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 0
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "no_fb_opportunity"
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_no_identity_anchor"
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == ""
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_existing_email_sets_skip_gate_attribution(monkeypatch, tmp_path):
    helper = StaticFBHelper({})
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Has Email",
                "Email": "seed@example.com",
                "Email_All": "seed@example.com",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/hasemail",
            }
        ],
        helper,
    )

    assert helper.calls == 0
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_existing_usable_email"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_attempted_without_email_records_attempt_outcome(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
            "Facebook_URL": "https://facebook.com/noemail",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "No Email On Page",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/noemail",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_attempted_with_accepted_email_records_write(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_found_email",
            FB_ATTEMPT_STATE_COL: "attempted_fb_found_email",
            "Email": "fb@example.com",
            "Email_All": "fb@example.com",
            "Email_Type": "fb_night",
            "Email_Source_URL": "https://facebook.com/found/about",
            "Email_Source_Type": "facebook_enrich",
            "Email_Extract_Method": "regex",
            "Facebook_URL": "https://facebook.com/found",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "FB Found",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/found",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, "Email"] == "fb@example.com"
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_found_email"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_wrote_email"


def test_attempted_with_rejected_email_records_not_applied(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "rejected_candidate",
            "FB_Reason": "reject:name_mismatch",
            FB_ATTEMPT_STATE_COL: "attempted_fb_rejected_by_acceptance_guard",
            "Facebook_URL": "https://facebook.com/rejected",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "FB Rejected",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/rejected",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, "Email"] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_rejected_by_acceptance_guard"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_found_email_not_applied"


def test_night_mode_outputs_preserve_fb_attribution(monkeypatch, tmp_path):
    config = {
        "export_mode": "both",
        "export_profile": "full_dump",
        "master_enrichment": {"enabled": True},
        "jobs": [
            {
                "job_id": "job_one",
                "directory": "spotify",
                "target_valid_leads": 1,
            }
        ],
    }
    config_path = tmp_path / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    def fake_run_directory_job(job_config, raw_output_path, logger=None):
        pd.DataFrame(
            [
                {
                    "Artist Name": "Persist Artist",
                    "Email": "",
                    "Email_All": "",
                    "Social Link": "",
                    "Facebook_URL": "https://facebook.com/persist",
                }
            ]
        ).to_csv(raw_output_path, index=False)
        return raw_output_path

    def fake_run_master_enrichment(input_csv, output_csv, logger=None, enable_live_search=True, max_live_searches=None, night_mode=False):
        df = _read_csv(Path(input_csv))
        df.to_csv(output_csv, index=False)
        return output_csv

    def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None, night_mode=False):
        df = _read_csv(Path(raw_csv_path))
        if "final_status" not in df.columns:
            df["final_status"] = df["Email"].apply(lambda v: "OK" if str(v).strip() else "REVIEW")
        if "Needs_Review" not in df.columns:
            df["Needs_Review"] = ""
        df.to_csv(enriched_output_path, index=False)
        return enriched_output_path

    def fake_fb_pass(input_csv, output_csv, state_path, max_rows_per_run=100, **kwargs):
        df = _read_csv(Path(input_csv))
        df[FB_OPPORTUNITY_STATE_COL] = "fb_opportunity_present"
        df[FB_GATE_STATE_COL] = ""
        df[FB_ATTEMPT_STATE_COL] = "attempted_fb_found_email"
        df[FB_WRITE_STATE_COL] = "fb_wrote_email"
        df["FB_Status"] = "pass_a_found_email"
        df["Email"] = "persist@example.com"
        df["Email_All"] = "persist@example.com"
        df["Email_Source_URL"] = "https://facebook.com/persist/about"
        df["Email_Source_Type"] = "facebook_enrich"
        df["Email_Extract_Method"] = "regex"
        df["final_status"] = "OK"
        df.to_csv(output_csv, index=False)
        return pipeline_runner.FacebookGlobalPassStatus(
            processed_rows=len(df.index),
            total_rows=len(df.index),
            completed=True,
            hit_captcha=False,
            limit_reached=False,
            attempted_total=len(df.index),
        )

    monkeypatch.setattr(night_mode_runner, "run_directory_job", fake_run_directory_job)
    monkeypatch.setattr(night_mode_runner, "run_master_enrichment", fake_run_master_enrichment)
    monkeypatch.setattr(night_mode_runner, "run_enrichment", fake_run_enrichment)
    monkeypatch.setattr(night_mode_runner, "run_facebook_global_pass_nightmode", fake_fb_pass)

    result = night_mode_runner.run_night_mode(config_path.as_posix(), run_root=tmp_path.as_posix())
    run_dir = Path(result["run_dir"])

    expected_attempt_files = {
        "master_post_fb.csv",
        "master_enriched_deduped.csv",
        "master_export_leads.csv",
    }
    for filename in (
        "master_pre_fb.csv",
        "master_post_fb.csv",
        "master_enriched_deduped.csv",
        "master_export_leads.csv",
    ):
        df = _read_csv(run_dir / filename)
        assert FB_OPPORTUNITY_STATE_COL in df.columns
        assert FB_GATE_STATE_COL in df.columns
        assert FB_ATTEMPT_STATE_COL in df.columns
        assert FB_WRITE_STATE_COL in df.columns
        assert df.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
        if filename in expected_attempt_files:
            assert df.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_found_email"
            assert df.loc[0, FB_WRITE_STATE_COL] == "fb_wrote_email"
