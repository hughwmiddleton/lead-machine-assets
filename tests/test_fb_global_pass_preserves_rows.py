import os
from types import SimpleNamespace

import night_mode_fb as nmfb
import pandas as pd
import pipeline_runner
from fb_attribution import FB_GATE_STATE_COL


class DummyFBHelper:
    def __init__(self, *args, **kwargs):
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
            "Artist Name": "Anchored Only",
            "__source_job": "job_anchor",
            "Email": "",
            "Email_All": "",
            "SoundCloud Link": "https://soundcloud.com/anchored-only/tracks",
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
    monkeypatch.setattr(
        pipeline_runner,
        "_build_night_fb_share_promotion_resolver",
        lambda **kwargs: (lambda url: ""),
    )

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

    # Rows attempted for FB enrichment get FB-derived email/status, including blank-FB fallback discovery.
    fb_attempted = df_out[df_out["__source_job"].isin(["job_fb_url", "job_social_fb", "job_anchor"])]
    assert (fb_attempted["Email"] == "fb@example.com").all()
    assert (fb_attempted["Email_All"].str.contains("fb@example.com")).all()
    assert (fb_attempted["FB_Status"].str.lower() == "ok").all()

    # Rows without canonical Facebook_URL can still enter the bounded Night FB discovery path
    # when they carry a usable upstream identity anchor.
    anchored_rows = df_out[df_out["__source_job"] == "job_anchor"]
    assert len(anchored_rows) == 1
    assert anchored_rows.iloc[0]["Email"] == "fb@example.com"
    assert "fb@example.com" in anchored_rows.iloc[0]["Email_All"]
    assert anchored_rows.iloc[0]["FB_Status"].lower() == "ok"

    # Weak rows without a Facebook URL or upstream identity anchor are preserved but skipped.
    no_clue_rows = df_out[df_out["__source_job"] == "job_none"]
    assert len(no_clue_rows) == 1
    assert no_clue_rows.iloc[0]["Email"] == ""
    assert no_clue_rows.iloc[0]["Email_All"] == ""
    assert no_clue_rows.iloc[0]["FB_Status"] == ""
    assert no_clue_rows.iloc[0][FB_GATE_STATE_COL] == "skipped_no_identity_anchor"

    # Only rows without existing emails were attempted.
    assert helper.calls == len(fb_attempted.index)
    assert {entry["row"]["Artist Name"] for entry in helper.rows} == {
        "Has FB URL",
        "Social Link FB",
        "Anchored Only",
    }

    # Status object reflects that work happened but total rows equal input.
    assert status.total_rows == len(rows)


def test_fb_global_pass_runtime_share_fallback_admits_row_once_without_canonical_mutation(monkeypatch, tmp_path):
    share_url = "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    input_csv = tmp_path / "master_pre_fb_runtime_share.csv"
    output_csv = tmp_path / "master_post_fb_runtime_share.csv"
    state_path = tmp_path / "fb_state_runtime_share.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Runtime Share",
                "Source Directory": "unearthed",
                "__source_job": "job_unearthed_runtime",
                "Email": "",
                "Email_All": "",
                "Social Link": share_url,
                "Facebook_URL": "",
                nmfb.FB_SHARE_RUNTIME_FALLBACK_URL_COL: share_url,
                nmfb.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL: "Social Link",
            }
        ]
    ).to_csv(input_csv, index=False)

    logs = []

    class RuntimeShareHelper(DummyFBHelper):
        def enrich_row_with_facebook_night(self, row, row_index=0):
            self.calls += 1
            self.rows.append({"row": dict(row or {}), "row_index": row_index})
            return {
                "FB_Status": "pass_a_no_email_on_page",
                "FB_Reason": "session_fetch_ok_no_email",
                "Email": "",
                "Email_All": "",
                "Facebook_URL": "",
            }

    helper = RuntimeShareHelper()

    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _make_dummy_module)
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
    row_gate_logs = [msg for msg in logs if "[Night FB][Row Gate]" in msg]

    assert helper.calls == 1
    assert any(
        "artist='Runtime Share'" in msg
        and "fb_url_present=False" in msg
        and "fb_entrypoint_present=True" in msg
        and "share_runtime_fallback=True" in msg
        and "eligible_for_fb=True" in msg
        for msg in row_gate_logs
    )
    assert df_out.loc[0, "Facebook_URL"] == ""
    assert df_out.loc[0, "FB_Status"] == "pass_a_no_email_on_page"


def test_nightmode_fb_pass_allows_discovery_fallback_without_canonical_fb_url_even_if_attempted(
    monkeypatch, tmp_path
):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Anchored Only",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "SoundCloud Link": "https://soundcloud.com/anchored-only/tracks",
                "Facebook_URL": "",
                pipeline_runner.FB_DISCOVERY_ATTEMPT_FLAG_COL: "1",
            }
        ]
    ).to_csv(input_csv, index=False)

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

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert df_out.loc[0, "Email"] == "fb@example.com"
    assert df_out.loc[0, "FB_Status"] == "ok"
    assert df_out.loc[0, FB_GATE_STATE_COL] != "skipped_duplicate_fb_discovery"
    assert status.total_rows == 1


def test_nightmode_fb_pass_allows_unearthed_no_url_even_when_discovery_attempted(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed Missing FB",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "",
                pipeline_runner.FB_DISCOVERY_ATTEMPT_FLAG_COL: "1",
            }
        ]
    ).to_csv(input_csv, index=False)

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

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert helper.rows[0]["row"]["Source Directory"] == "Unearthed"
    assert df_out.loc[0, "Email"] == "fb@example.com"
    assert df_out.loc[0, "FB_Status"] == "ok"
    assert df_out.loc[0, FB_GATE_STATE_COL] != "skipped_duplicate_fb_discovery"
    assert status.total_rows == 1


def test_nightmode_fb_pass_allows_explicit_url_even_when_discovery_attempted(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Explicit URL",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/expliciturl",
                pipeline_runner.FB_DISCOVERY_ATTEMPT_FLAG_COL: "1",
            }
        ]
    ).to_csv(input_csv, index=False)

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

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == "https://www.facebook.com/expliciturl"
    assert df_out.loc[0, "Email"] == "fb@example.com"
    assert df_out.loc[0, "FB_Status"] == "ok"
    assert df_out.loc[0, FB_GATE_STATE_COL] != "skipped_duplicate_fb_discovery"
    assert status.total_rows == 1


def test_nightmode_fb_pass_allows_unearthed_explicit_url_even_when_discovery_attempted(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed Explicit URL",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/unearthedexplicit",
                pipeline_runner.FB_DISCOVERY_ATTEMPT_FLAG_COL: "1",
            }
        ]
    ).to_csv(input_csv, index=False)

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

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == "https://www.facebook.com/unearthedexplicit"
    assert df_out.loc[0, "Email"] == "fb@example.com"
    assert df_out.loc[0, "FB_Status"] == "ok"
    assert df_out.loc[0, FB_GATE_STATE_COL] != "skipped_duplicate_fb_discovery"
    assert status.total_rows == 1


def test_nightmode_fb_pass_allows_profile_backed_session_without_credentials(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    profile_dir = tmp_path / "night_fb_profile"
    (profile_dir / "Default").mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "Artist Name": "Has FB URL",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/hasfb",
            }
        ]
    ).to_csv(input_csv, index=False)

    helper = DummyFBHelper()

    monkeypatch.delenv("FB_USERNAME", raising=False)
    monkeypatch.delenv("FB_PASSWORD", raising=False)
    monkeypatch.setenv("NIGHT_FB_PROFILE_DIR", profile_dir.as_posix())
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _make_dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    status = pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=False,
    )

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")

    assert helper.calls == 1
    assert df_out.loc[0, "FB_Status"] == "ok"
    assert status.total_rows == 1


def test_nightmode_fb_pass_uses_inferred_profile_source_for_explicit_unearthed_url(
    monkeypatch, tmp_path
):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    profile_dir = tmp_path / "night_fb_profile"
    (profile_dir / "Default").mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed Explicit URL",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/unearthedexplicit",
            }
        ]
    ).to_csv(input_csv, index=False)

    helper = DummyFBHelper()
    helper_kwargs = {}
    logs = []

    def fake_helper(*args, **kwargs):
        helper_kwargs.update(kwargs)
        return helper

    monkeypatch.delenv("FB_USERNAME", raising=False)
    monkeypatch.delenv("FB_PASSWORD", raising=False)
    monkeypatch.delenv("NIGHT_FB_PROFILE_DIR", raising=False)
    monkeypatch.setattr(nmfb, "_infer_night_fb_profile_dir", lambda: profile_dir.as_posix())
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", fake_helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _make_dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    status = pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=False,
        logger=logs.append,
    )

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
    session_gate_logs = [msg for msg in logs if "[Night FB][Session Gate]" in msg]

    assert session_gate_logs
    assert any("source=profile" in msg and "decision=probe_pending" in msg for msg in session_gate_logs)
    assert all("reason=missing_session_source" not in msg for msg in session_gate_logs)
    assert helper.calls == 1
    assert helper_kwargs["run_state"] is not None
    assert helper.rows[0]["row"]["Facebook_URL"] == "https://www.facebook.com/unearthedexplicit"
    assert df_out.loc[0, "FB_Status"] == "ok"
    assert status.total_rows == 1


def test_nightmode_fb_pass_still_disables_when_session_source_is_truly_missing(
    monkeypatch, tmp_path
):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    missing_profile_dir = tmp_path / "missing_night_fb_profile"
    pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed Explicit URL",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/unearthedexplicit",
            }
        ]
    ).to_csv(input_csv, index=False)

    helper_calls = []
    logs = []

    monkeypatch.delenv("FB_USERNAME", raising=False)
    monkeypatch.delenv("FB_PASSWORD", raising=False)
    monkeypatch.delenv("NIGHT_FB_PROFILE_DIR", raising=False)
    monkeypatch.setattr(nmfb, "_infer_night_fb_profile_dir", lambda: missing_profile_dir.as_posix())
    monkeypatch.setattr(
        pipeline_runner,
        "NightModeFacebookEnricher",
        lambda *args, **kwargs: helper_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    status = pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=False,
        logger=logs.append,
    )

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
    session_gate_logs = [msg for msg in logs if "[Night FB][Session Gate]" in msg]

    assert session_gate_logs
    assert any(
        "source=none" in msg
        and "decision=disabled_for_run" in msg
        and "reason=missing_session_source" in msg
        for msg in session_gate_logs
    )
    assert helper_calls == []
    assert df_out.loc[0, "FB_Status"] == ""
    assert status.completed is True
    assert status.total_rows == 1


def test_nightmode_fb_pass_respects_prior_run_disable(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Has FB URL",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/hasfb",
            }
        ]
    ).to_csv(input_csv, index=False)

    run_state = nmfb.create_night_fb_run_state("user", "pass")
    nmfb.disable_night_fb_run_state(
        run_state,
        "session_invalid",
        session_invalid=True,
    )
    helper_calls = []

    monkeypatch.setattr(
        pipeline_runner,
        "NightModeFacebookEnricher",
        lambda *args, **kwargs: helper_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    status = pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=False,
        night_fb_run_state=run_state,
    )

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")

    assert helper_calls == []
    assert df_out.loc[0, "Email"] == ""
    assert df_out.loc[0, "FB_Status"] == ""
    assert status.completed is True
    assert status.total_rows == 1


def test_nightmode_fb_pass_session_gate_logs_true_disable_reason(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Has FB URL",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/hasfb",
            }
        ]
    ).to_csv(input_csv, index=False)

    run_state = nmfb.create_night_fb_run_state("profile_session", "profile_session")
    nmfb.disable_night_fb_run_state(run_state, "captcha")
    logs = []

    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=False,
        night_fb_run_state=run_state,
        logger=logs.append,
    )

    session_gate_logs = [msg for msg in logs if "[Night FB][Session Gate]" in msg]

    assert session_gate_logs
    assert any("reason=captcha" in msg for msg in session_gate_logs)
    assert all("reason=profile_session" not in msg for msg in session_gate_logs)


def test_nightmode_fb_pass_logs_outer_row_gate(monkeypatch, tmp_path):
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(
        [
            {
                "Artist Name": "Skip Me",
                "Email": "skip@example.com",
                "Email_All": "skip@example.com",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/skipme",
            },
            {
                "Artist Name": "Try Me",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "https://soundcloud.com/try-me/tracks",
                "Social Link": "",
                "Facebook_URL": "",
            },
        ]
    ).to_csv(input_csv, index=False)

    helper = DummyFBHelper()
    logs = []

    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _make_dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        skip_rows_with_email=True,
        logger=logs.append,
    )

    row_gate_logs = [msg for msg in logs if "[Night FB][Row Gate]" in msg]

    assert any("row=0" in msg and "artist='Skip Me'" in msg and "email_present=True" in msg and "eligible_for_fb=False" in msg for msg in row_gate_logs)
    assert any("row=1" in msg and "artist='Try Me'" in msg and "fb_url_present=False" in msg and "eligible_for_fb=True" in msg for msg in row_gate_logs)


def test_finalize_fb_row_attribution_classifies_authoritative_outcomes():
    df = pd.DataFrame(
        [
            {pipeline_runner.FB_OPPORTUNITY_STATE_COL: "no_fb_opportunity"},
            {
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
                pipeline_runner.FB_GATE_STATE_COL: "skipped_existing_usable_email",
            },
            {
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
                pipeline_runner.FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
            },
            {
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
                pipeline_runner.FB_ATTEMPT_STATE_COL: "attempted_fb_found_email",
                pipeline_runner.FB_WRITE_STATE_COL: "fb_found_email_not_applied",
            },
            {
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
                pipeline_runner.FB_ATTEMPT_STATE_COL: "attempted_fb_found_email",
                pipeline_runner.FB_WRITE_STATE_COL: "fb_wrote_email",
            },
            {
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
                pipeline_runner.FB_ATTEMPT_STATE_COL: "attempted_fb_login_wall_or_checkpoint",
            },
            {},
        ],
        dtype=str,
    ).fillna("")

    for idx in df.index:
        pipeline_runner.finalize_fb_row_attribution(df, idx)

    assert df.loc[0, pipeline_runner.FB_TERMINAL_REASON_COL] == "no_fb_opportunity"
    assert df.loc[0, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_extract_not_applicable"

    assert df.loc[1, pipeline_runner.FB_ATTEMPT_STATE_COL] == "fb_not_attempted"
    assert df.loc[1, pipeline_runner.FB_TERMINAL_REASON_COL] == "fb_opportunity_not_attempted_existing_email_gate"
    assert df.loc[1, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_extract_not_attempted"

    assert df.loc[2, pipeline_runner.FB_TERMINAL_REASON_COL] == "fb_no_email_found"
    assert df.loc[2, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_no_usable_email_found"

    assert df.loc[3, pipeline_runner.FB_TERMINAL_REASON_COL] == "fb_found_email_not_written"
    assert df.loc[3, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_found_usable_email"

    assert df.loc[4, pipeline_runner.FB_TERMINAL_REASON_COL] == "fb_email_written"
    assert df.loc[4, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_found_usable_email"

    assert df.loc[5, pipeline_runner.FB_TERMINAL_REASON_COL] == "fb_login_required_or_blocked"
    assert df.loc[5, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_extract_blocked_or_unavailable"

    assert df.loc[6, pipeline_runner.FB_ATTEMPT_STATE_COL] == "fb_not_attempted"
    assert df.loc[6, pipeline_runner.FB_TERMINAL_REASON_COL] == "fb_indeterminate"
    assert df.loc[6, pipeline_runner.FB_EXTRACT_STATE_COL] == "fb_extract_indeterminate"
