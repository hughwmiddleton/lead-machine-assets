from types import SimpleNamespace

import pandas as pd

import night_mode_fb as nmfb
import pipeline_runner
from fb_attribution import FB_GATE_STATE_COL


class RecordingFBHelper:
    def __init__(self):
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
        return {
            "FB_Status": "pass_a_found_email",
            "Email": "fb@example.com",
            "Email_All": "fb@example.com",
            "Email_Type": "fb_night",
            "Facebook_URL": row.get("Facebook_URL") or "https://facebook.com/found",
        }


def _dummy_module():
    return SimpleNamespace(scrape_csv=lambda *args, **kwargs: None)


def _run_night_fb_pass(monkeypatch, tmp_path, rows):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(rows).to_csv(input_csv, index=False)

    helper = RecordingFBHelper()
    logs = []

    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pipeline_runner,
        "_build_night_fb_share_promotion_resolver",
        lambda **kwargs: (lambda url: ""),
    )

    pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=True,
        logger=logs.append,
    )

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
    return helper, logs, df_out


def test_missing_email_with_valid_fb_url_runs(monkeypatch, tmp_path):
    helper, logs, df_out = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Missing Email",
                "Email": "",
                "Email_All": "",
                "Facebook_URL": "https://facebook.com/missingemail",
            }
        ],
    )

    assert helper.calls == 1
    assert helper.rows[0]["row_index"] == 0
    assert df_out.loc[0, "FB_Status"] == "pass_a_found_email"
    assert any("eligible_for_fb=True" in msg for msg in logs if "[Night FB][Row Gate]" in msg)


def test_unearthed_existing_email_with_valid_fb_url_does_not_run(monkeypatch, tmp_path):
    helper, logs, df_out = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Has Email",
                "Source Directory": "unearthed",
                "__source_job": "job_unearthed",
                "Email": "known@example.com",
                "Email_All": "known@example.com",
                "Facebook_URL": "https://facebook.com/hasemail",
            }
        ],
    )

    assert helper.calls == 0
    assert df_out.loc[0, "Artist Name"] == "Has Email"
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_existing_usable_email"
    assert any("email_already_present" in msg for msg in logs)
    assert any("email_present=True" in msg and "eligible_for_fb=False" in msg for msg in logs)


def test_existing_email_blocks_unearthed_fb_first_and_share_fallback(monkeypatch, tmp_path):
    share_url = "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    helper, logs, df_out = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Share Has Email",
                "Source Directory": "unearthed",
                "__source_job": "job_unearthed_runtime",
                "Email": "known@example.com",
                "Email_All": "known@example.com",
                "Social Link": share_url,
                "Facebook_URL": "",
                nmfb.FB_SHARE_RUNTIME_FALLBACK_URL_COL: share_url,
                nmfb.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL: "Social Link",
            }
        ],
    )

    assert helper.calls == 0
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_existing_usable_email"
    assert any("share_runtime_fallback=True" in msg and "eligible_for_fb=False" in msg for msg in logs)


def test_missing_email_with_share_runtime_fallback_runs(monkeypatch, tmp_path):
    share_url = "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    helper, logs, _df_out = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Share Missing Email",
                "Source Directory": "unearthed",
                "__source_job": "job_unearthed_runtime",
                "Email": "",
                "Email_All": "",
                "Social Link": share_url,
                "Facebook_URL": "",
                nmfb.FB_SHARE_RUNTIME_FALLBACK_URL_COL: share_url,
                nmfb.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL: "Social Link",
            }
        ],
    )

    assert helper.calls == 1
    assert any("share_runtime_fallback=True" in msg and "eligible_for_fb=True" in msg for msg in logs)


def test_missing_email_without_identity_path_does_not_run(monkeypatch, tmp_path):
    helper, logs, df_out = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "No Identity",
                "Email": "",
                "Email_All": "",
                "Facebook_URL": "",
                "Social Link": "",
            }
        ],
    )

    assert helper.calls == 0
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_no_identity_anchor"
    assert any("eligible_for_fb=False" in msg for msg in logs if "[Night FB][Row Gate]" in msg)


def test_terminal_fb_status_blocks_execution(monkeypatch, tmp_path):
    helper, _logs, df_out = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Terminal",
                "Email": "",
                "Email_All": "",
                "Facebook_URL": "https://facebook.com/terminal",
                "FB_Status": "no_candidates",
            }
        ],
    )

    assert helper.calls == 0
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_terminal_fb_status"
    assert df_out.loc[0, "FB_Status"] == "no_candidates"


def test_email_skip_preserves_row_count_and_order(monkeypatch, tmp_path):
    rows = [
        {"Artist Name": "First", "Email": "first@example.com", "Email_All": "first@example.com", "Facebook_URL": "https://facebook.com/first"},
        {"Artist Name": "Second", "Email": "", "Email_All": "", "Facebook_URL": "https://facebook.com/second"},
        {"Artist Name": "Third", "Email": "third@example.com", "Email_All": "third@example.com", "Facebook_URL": "https://facebook.com/third"},
    ]

    helper, _logs, df_out = _run_night_fb_pass(monkeypatch, tmp_path, rows)

    assert helper.calls == 1
    assert list(df_out["Artist Name"]) == ["First", "Second", "Third"]
    assert len(df_out) == 3
