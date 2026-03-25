import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from email_provenance import EMAIL_PROVENANCE_JSON_COL
import night_mode_runner
import pipeline_runner
from fb_attribution import (
    FB_ATTEMPT_STATE_COL,
    FB_DEBUG_REASON_COL,
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


def _run_night_fb_pass(monkeypatch, tmp_path, rows, helper, logger=None):
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
        logger=logger,
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


def test_spotify_identity_anchor_enters_discovery_fallback_path(monkeypatch, tmp_path):
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
                "Artist Name": "Spotify Seed Artist",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "",
                "Spotify_URL": "https://open.spotify.com/artist/spotify-seed-artist",
                "Spotify_Artist_ID": "spotify-seed-artist",
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


def test_unearthed_no_facebook_url_enters_bounded_discovery_path(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "ok_unearthed_blind",
            FB_ATTEMPT_STATE_COL: "attempted_fb_found_email",
            "Email": "unearthed@example.com",
            "Email_All": "unearthed@example.com",
            "Email_Type": "fb_night",
            "Email_Source_URL": "https://facebook.com/unearthed-found/about",
            "Email_Source_Type": "facebook_enrich",
            "Email_Extract_Method": "regex",
            "Facebook_URL": "https://facebook.com/unearthed-found",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Unearthed Missing FB",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert df_out.loc[0, "Email"] == "unearthed@example.com"
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_discovery_fallback_eligible"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_found_email"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_wrote_email"


def test_unearthed_no_facebook_url_discovery_miss_stays_safe(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "unearthed_no_candidates",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Unearthed Missing FB",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, "Email"] == ""
    assert df_out.loc[0, "FB_Status"] == "unearthed_no_candidates"
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_discovery_fallback_eligible"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_unearthed_no_facebook_url_with_discovery_flag_still_uses_helper_states(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "unearthed_no_candidates",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
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
        ],
        helper,
    )

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert df_out.loc[0, "FB_Status"] == "unearthed_no_candidates"
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_discovery_fallback_eligible"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_canonical_detectable_explicit_facebook_link_still_runs_without_identity_anchor(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    monkeypatch.setattr(pipeline_runner, "_promote_fb_urls_df", lambda df, logger=None: df)
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Explicit FB Artist",
                "Email": "",
                "Email_All": "",
                "Social Link": "https://fb.com/explicitfbartist",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert helper.rows[0]["row"]["Social Link"] == "https://fb.com/explicitfbartist"
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_web_host_explicit_facebook_link_still_runs_without_identity_anchor(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    monkeypatch.setattr(pipeline_runner, "_promote_fb_urls_df", lambda df, logger=None: df)
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Explicit Web FB Artist",
                "Email": "",
                "Email_All": "",
                "Social Link": "https://web.facebook.com/explicitwebfbartist",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Social Link"] == "https://web.facebook.com/explicitwebfbartist"
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_share_entrypoint_runs_without_identity_anchor(monkeypatch, tmp_path):
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
                "Artist Name": "Explicit Share FB Artist",
                "Email": "",
                "Email_All": "",
                "Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert helper.rows[0]["row"]["Facebook_URL"] == ""
    assert helper.rows[0]["row"]["Social Link"] == "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr"
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
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


def test_unearthed_existing_email_with_explicit_fb_still_runs(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
            "Facebook_URL": "https://facebook.com/unearthed-hasemail",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Unearthed Has Email",
                "Source Directory": "Unearthed",
                "Email": "seed@example.com",
                "Email_All": "seed@example.com",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/unearthed-hasemail",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_OPPORTUNITY_STATE_COL] in {"", "fb_opportunity_present"}
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_no_email_on_page"
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"


def test_placeholder_email_does_not_set_skip_gate_attribution(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_found_email",
            FB_ATTEMPT_STATE_COL: "attempted_fb_found_email",
            "Email": "fb@example.com",
            "Email_All": "fb@example.com",
            "Email_Type": "fb_night",
            "Email_Source_URL": "https://facebook.com/hasemail/about",
            "Email_Source_Type": "facebook_enrich",
            "Email_Extract_Method": "regex",
            "Facebook_URL": "https://facebook.com/hasemail",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Has Placeholder Email",
                "Email": "user@domain.com",
                "Email_All": "user@domain.com",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/hasemail",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, "Email"] == "fb@example.com"
    assert df_out.loc[0, FB_GATE_STATE_COL] == ""
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_wrote_email"


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


def test_content_unavailable_status_preserves_distinct_attempt_outcome(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_content_unavailable",
            "FB_Reason": "content_unavailable",
            "Facebook_URL": "https://facebook.com/unavailable",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "FB Unavailable",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/unavailable",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_content_unavailable"
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
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "email_written"
    provenance = json.loads(df_out.loc[0, EMAIL_PROVENANCE_JSON_COL])
    assert provenance["fb@example.com"]["surface"] == "facebook_about"
    assert provenance["fb@example.com"]["source_type"] == "facebook_enrich"


def test_attempted_main_page_mailto_maps_debug_reason(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_found_email",
            FB_ATTEMPT_STATE_COL: "attempted_fb_found_email",
            "Email": "mailto@example.com",
            "Email_All": "mailto@example.com",
            "Email_Type": "fb_night",
            "Email_Source_URL": "https://facebook.com/mailto",
            "Email_Source_Type": "facebook_enrich",
            "Email_Extract_Method": "mailto",
            "Facebook_URL": "https://facebook.com/mailto",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "FB Mailto",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/mailto",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_wrote_email"
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "email_written"


def test_attempted_about_page_no_email_maps_debug_reason(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
            "FB_About_Attempted": "yes",
            "FB_About_Result": "no_email",
            "Facebook_URL": "https://facebook.com/about-miss",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "FB About Miss",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/about-miss",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_WRITE_STATE_COL] == "fb_no_email_written"
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "no_email_visible"


def test_attempted_no_contact_surface_maps_debug_reason(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
            "FB_About_Attempted": "yes",
            "FB_About_Result": "no_contact_link",
            "Facebook_URL": "https://facebook.com/no-contact",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "FB No Contact",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "https://facebook.com/no-contact",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "no_email_visible"


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
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "email_found_not_applied"


def test_no_safe_candidate_maps_pre_scrape_reject_debug_reason(monkeypatch, tmp_path):
    helper = StaticFBHelper(
        {
            "FB_Status": "no_candidates",
            "FB_Reason": "non_music_category",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
        }
    )
    df_out, _ = _run_night_fb_pass(
        monkeypatch,
        tmp_path,
        [
            {
                "Artist Name": "Rejected Candidate",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Spotify_URL": "https://open.spotify.com/artist/rejected-candidate",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 1
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "no_email_visible"


def test_skipped_no_identity_anchor_maps_no_fb_candidate(monkeypatch, tmp_path):
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
                "Artist Name": "No Anchor Artist",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "Facebook_URL": "",
            }
        ],
        helper,
    )

    assert helper.calls == 0
    assert df_out.loc[0, FB_GATE_STATE_COL] == "skipped_no_identity_anchor"
    assert df_out.loc[0, FB_DEBUG_REASON_COL] == "no_fb_candidate"


def test_night_fb_logs_debug_summary_in_fixed_order():
    logs = []
    df = pd.DataFrame(
        {
            FB_DEBUG_REASON_COL: [
                "email_written",
                "email_found_not_applied",
                "no_email_visible",
                "no_email_visible",
                "login_required_or_blocked",
                "content_unavailable",
                "timeout_or_fetch_error",
                "no_fb_candidate",
                "",
                None,
                "unexpected_value",
            ]
        }
    )
    pipeline_runner._log_fb_debug_summary(df, logger=logs.append)

    assert logs == [
        "[FB Debug Summary]",
        "email_written=1",
        "email_found_not_applied=1",
        "no_email_visible=2",
        "login_required_or_blocked=1",
        "content_unavailable=1",
        "timeout_or_fetch_error=1",
        "no_fb_candidate=1",
    ]


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
        df[FB_DEBUG_REASON_COL] = "email_written"
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
        assert FB_DEBUG_REASON_COL in df.columns
        assert df.loc[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
        if filename in expected_attempt_files:
            assert df.loc[0, FB_ATTEMPT_STATE_COL] == "attempted_fb_found_email"
            assert df.loc[0, FB_WRITE_STATE_COL] == "fb_wrote_email"
