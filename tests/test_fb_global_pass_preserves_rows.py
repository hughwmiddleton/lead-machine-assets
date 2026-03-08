import os
from types import SimpleNamespace

import pandas as pd
import pipeline_runner


class DummyFBHelper:
    def __init__(self, *args, **kwargs):
        self.calls = 0

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
        return {
            "FB_Status": "ok",
            "Email": "fb@example.com",
            "Email_All": "fb@example.com",
            "Email_Type": "fb_night",
            "Facebook_URL": "https://facebook.com/dummy",
        }


def _make_dummy_module():
    return SimpleNamespace(scrape_csv=lambda *args, **kwargs: None)


def test_fb_global_pass_preserves_rows_and_emails(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": "Unearthed Artist",
            "__source_job": "job_unearthed_6",
            "Email": "unearthed@example.com",
            "Email_All": "unearthed@example.com",
            "Social Link": "",
            "Facebook_URL": "",
        },
        {
            "Artist Name": "Has FB URL",
            "__source_job": "job_fb_url",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "Facebook_URL": "https://facebook.com/hasfb",
        },
        {
            "Artist Name": "Social Link FB",
            "__source_job": "job_social_fb",
            "Email": "",
            "Email_All": "",
            "Social Link": "https://www.facebook.com/socialfb",
            "Facebook_URL": "",
        },
        {
            "Artist Name": "Has Email Already",
            "__source_job": "job_email",
            "Email": "sc@example.com",
            "Email_All": "sc@example.com",
            "Social Link": "",
            "Facebook_URL": "",
        },
        {
            "Artist Name": "No Clue",
            "__source_job": "job_none",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "Facebook_URL": "",
        },
    ]

    input_csv = tmp_path / "master_pre_fb.csv"
    pd.DataFrame(rows).to_csv(input_csv, index=False)
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"

    # Env + stubs to avoid real Selenium/Facebook work.
    helper = DummyFBHelper()

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

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")

    # Output retains all rows and preserves order.
    assert len(df_out.index) == len(rows)
    assert df_out["Artist Name"].tolist() == [row["Artist Name"] for row in rows]

    # Unearthed row is present and untouched.
    unearthed_rows = df_out[df_out["__source_job"] == "job_unearthed_6"]
    assert len(unearthed_rows) == 1
    assert unearthed_rows.iloc[0]["Email"] == "unearthed@example.com"
    assert unearthed_rows.iloc[0]["Email_All"] == "unearthed@example.com"

    # Rows with existing email are skipped and not cleared.
    existing_email_rows = df_out[df_out["__source_job"] == "job_email"]
    assert existing_email_rows.iloc[0]["Email"] == "sc@example.com"
    assert existing_email_rows.iloc[0]["Email_All"] == "sc@example.com"

    # Rows attempted for FB enrichment get FB-derived email/status.
    fb_attempted = df_out[df_out["__source_job"].isin(["job_fb_url", "job_social_fb"])]
    assert (fb_attempted["Email"] == "fb@example.com").all()
    assert (fb_attempted["Email_All"].str.contains("fb@example.com")).all()
    assert (fb_attempted["FB_Status"].str.lower() == "ok").all()

    # Rows without canonical Facebook_URL stay unattempted, even without email.
    no_clue_rows = df_out[df_out["__source_job"] == "job_none"]
    assert len(no_clue_rows) == 1
    assert no_clue_rows.iloc[0]["Email"] == ""
    assert no_clue_rows.iloc[0]["Email_All"] == ""
    assert no_clue_rows.iloc[0]["FB_Status"] == ""

    # Only rows without existing emails were attempted.
    assert helper.calls == len(fb_attempted.index)

    # Status object reflects that work happened but total rows equal input.
    assert status.total_rows == len(rows)
