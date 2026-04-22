import pandas as pd

from fb_attribution import (
    FB_ATTEMPT_STATE_COL,
    FB_EXTRACT_STATE_COL,
    FB_GATE_STATE_COL,
    FB_NORMALIZED_TERMINAL_OUTCOME_COL,
    FB_NORMALIZED_TERMINAL_REASON_COL,
    FB_OPPORTUNITY_STATE_COL,
    FB_TERMINAL_REASON_COL,
    FB_WRITE_STATE_COL,
    IG_ATTEMPT_STATE_COL,
    IG_EXTRACT_STATE_COL,
    IG_NORMALIZED_TERMINAL_OUTCOME_COL,
    IG_NORMALIZED_TERMINAL_REASON_COL,
    IG_OPPORTUNITY_STATE_COL,
    IG_SURFACE_REASON_COL,
    IG_TERMINAL_REASON_COL,
    IG_WRITE_STATE_COL,
    PLATFORM_BLOCKED_OUTCOME,
    SUCCESS_OUTCOME,
    GENUINE_NO_EMAIL_OUTCOME,
    UNKNOWN_OUTCOME,
    classify_fb_normalized_terminal,
    classify_ig_normalized_terminal,
    finalize_fb_normalized_terminal,
    finalize_ig_normalized_terminal,
)


def test_fb_normalized_terminal_genuine_no_email():
    normalized = classify_fb_normalized_terminal(
        {
            FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
            FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
            FB_EXTRACT_STATE_COL: "fb_no_usable_email_found",
            FB_WRITE_STATE_COL: "fb_no_email_written",
            FB_TERMINAL_REASON_COL: "fb_no_email_found",
        }
    )

    assert normalized == {
        "outcome": GENUINE_NO_EMAIL_OUTCOME,
        "reason": "fb_no_email_found",
    }


def test_fb_normalized_terminal_login_wall_maps_to_platform_blocked():
    normalized = classify_fb_normalized_terminal(
        {
            FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
            FB_ATTEMPT_STATE_COL: "attempted_fb_login_wall_or_checkpoint",
            FB_EXTRACT_STATE_COL: "fb_extract_blocked_or_unavailable",
            FB_WRITE_STATE_COL: "fb_no_email_written",
            FB_TERMINAL_REASON_COL: "fb_login_required_or_blocked",
        }
    )

    assert normalized == {
        "outcome": PLATFORM_BLOCKED_OUTCOME,
        "reason": "fb_login_required_or_blocked",
    }


def test_fb_normalized_terminal_success_has_precedence_over_blocked_markers():
    normalized = classify_fb_normalized_terminal(
        {
            FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
            FB_ATTEMPT_STATE_COL: "attempted_fb_login_wall_or_checkpoint",
            FB_EXTRACT_STATE_COL: "fb_found_usable_email",
            FB_WRITE_STATE_COL: "fb_wrote_email",
            FB_TERMINAL_REASON_COL: "fb_email_written",
        }
    )

    assert normalized == {
        "outcome": SUCCESS_OUTCOME,
        "reason": "fb_email_written",
    }


def test_fb_normalized_terminal_unknown_when_evidence_is_incomplete():
    normalized = classify_fb_normalized_terminal(
        {
            FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
            FB_ATTEMPT_STATE_COL: "attempted_fb",
            FB_EXTRACT_STATE_COL: "fb_no_usable_email_found",
            FB_WRITE_STATE_COL: "fb_no_email_written",
            FB_TERMINAL_REASON_COL: "fb_no_email_found",
            FB_GATE_STATE_COL: "",
        }
    )

    assert normalized == {
        "outcome": UNKNOWN_OUTCOME,
        "reason": "fb_no_email_found",
    }


def test_ig_normalized_terminal_genuine_no_email():
    normalized = classify_ig_normalized_terminal(
        {
            IG_OPPORTUNITY_STATE_COL: "ig_opportunity_present",
            IG_ATTEMPT_STATE_COL: "attempted_ig_no_email_found",
            IG_EXTRACT_STATE_COL: "ig_no_usable_email_found",
            IG_WRITE_STATE_COL: "ig_no_email_written",
            IG_TERMINAL_REASON_COL: "ig_no_email_found",
            IG_SURFACE_REASON_COL: "",
        }
    )

    assert normalized == {
        "outcome": GENUINE_NO_EMAIL_OUTCOME,
        "reason": "ig_no_email_found",
    }


def test_ig_normalized_terminal_bridge_failure_maps_to_platform_blocked():
    normalized = classify_ig_normalized_terminal(
        {
            IG_OPPORTUNITY_STATE_COL: "ig_opportunity_present",
            IG_ATTEMPT_STATE_COL: "attempted_ig_no_email_found",
            IG_EXTRACT_STATE_COL: "ig_no_usable_email_found",
            IG_WRITE_STATE_COL: "ig_no_email_written",
            IG_TERMINAL_REASON_COL: "ig_no_email_found",
            IG_SURFACE_REASON_COL: "bridge_not_profile_surface_or_unavailable",
        }
    )

    assert normalized == {
        "outcome": PLATFORM_BLOCKED_OUTCOME,
        "reason": "bridge_not_profile_surface_or_unavailable",
    }


def test_ig_normalized_terminal_success_has_precedence_over_surface_failure():
    normalized = classify_ig_normalized_terminal(
        {
            IG_OPPORTUNITY_STATE_COL: "ig_opportunity_present",
            IG_ATTEMPT_STATE_COL: "attempted_ig_found_email",
            IG_EXTRACT_STATE_COL: "ig_found_usable_email",
            IG_WRITE_STATE_COL: "ig_wrote_email",
            IG_TERMINAL_REASON_COL: "ig_email_written",
            IG_SURFACE_REASON_COL: "bridge_not_profile_surface_or_unavailable",
        }
    )

    assert normalized == {
        "outcome": SUCCESS_OUTCOME,
        "reason": "ig_email_written",
    }


def test_ig_finalizer_clears_stale_surface_reason_without_attempt():
    df = pd.DataFrame(
        [
            {
                IG_OPPORTUNITY_STATE_COL: "ig_opportunity_present",
                IG_ATTEMPT_STATE_COL: "ig_not_attempted",
                IG_EXTRACT_STATE_COL: "ig_extract_not_attempted",
                IG_WRITE_STATE_COL: "ig_no_email_written",
                IG_TERMINAL_REASON_COL: "ig_opportunity_not_attempted_existing_email_gate",
                IG_SURFACE_REASON_COL: "profile_fetch_http_403",
            }
        ],
        dtype=str,
    ).fillna("")

    finalize_ig_normalized_terminal(df, 0)

    assert df.at[0, IG_SURFACE_REASON_COL] == ""
    assert df.at[0, IG_NORMALIZED_TERMINAL_OUTCOME_COL] == UNKNOWN_OUTCOME
    assert (
        df.at[0, IG_NORMALIZED_TERMINAL_REASON_COL]
        == "ig_opportunity_not_attempted_existing_email_gate"
    )


def test_platform_finalizers_are_observational_only():
    df = pd.DataFrame(
        [
            {
                FB_OPPORTUNITY_STATE_COL: "fb_opportunity_present",
                FB_ATTEMPT_STATE_COL: "attempted_fb_no_email_on_page",
                FB_EXTRACT_STATE_COL: "fb_no_usable_email_found",
                FB_WRITE_STATE_COL: "fb_no_email_written",
                FB_TERMINAL_REASON_COL: "fb_no_email_found",
                IG_OPPORTUNITY_STATE_COL: "ig_opportunity_present",
                IG_ATTEMPT_STATE_COL: "attempted_ig_no_email_found",
                IG_EXTRACT_STATE_COL: "ig_no_usable_email_found",
                IG_WRITE_STATE_COL: "ig_no_email_written",
                IG_TERMINAL_REASON_COL: "ig_no_email_found",
                IG_SURFACE_REASON_COL: "",
            }
        ],
        dtype=str,
    ).fillna("")
    before = df.loc[0].to_dict()

    finalize_fb_normalized_terminal(df, 0)
    finalize_ig_normalized_terminal(df, 0)

    for column, value in before.items():
        assert df.at[0, column] == value
    assert df.at[0, FB_NORMALIZED_TERMINAL_OUTCOME_COL] == GENUINE_NO_EMAIL_OUTCOME
    assert df.at[0, FB_NORMALIZED_TERMINAL_REASON_COL] == "fb_no_email_found"
    assert df.at[0, IG_NORMALIZED_TERMINAL_OUTCOME_COL] == GENUINE_NO_EMAIL_OUTCOME
    assert df.at[0, IG_NORMALIZED_TERMINAL_REASON_COL] == "ig_no_email_found"
