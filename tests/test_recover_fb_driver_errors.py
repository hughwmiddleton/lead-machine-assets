import json

import pandas as pd

from scripts.recover_fb_driver_errors import recover_dataframe, row_qualifies_for_recovery


def _fb_provenance(email="artist@example.com"):
    return json.dumps(
        {
            email: {
                "source_type": "facebook_enrich",
                "source_url": "https://www.facebook.com/artist",
                "extract_method": "regex",
            }
        }
    )


def _base_row(**overrides):
    row = {
        "Artist Name": "Driver Artist",
        "Facebook_URL": "https://www.facebook.com/driverartist",
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
        "Email_Provenance_JSON": "",
        "FB_Status": "driver_error",
        "FB_Opportunity_State": "fb_opportunity_present",
        "FB_Gate_State": "",
        "FB_Attempt_State": "attempted_fb_timeout_or_fetch_error",
        "FB_Write_State": "",
        "FB_Debug_Reason": "driver_error",
        "Unrelated Field": "keep-me",
    }
    row.update(overrides)
    return row


def test_selection_identifies_driver_error_with_fb_opportunity():
    assert row_qualifies_for_recovery(_base_row()) is True


def test_discovery_fallback_without_canonical_url_is_skipped():
    df, summary = recover_dataframe(
        pd.DataFrame(
            [
                _base_row(
                    FB_Opportunity_State="fb_discovery_fallback_eligible",
                    Facebook_URL="",
                )
            ]
        ),
        dry_run=True,
    )

    assert summary.driver_error_rows == 1
    assert summary.candidates_found == 0
    assert summary.excluded_discovery_fallback_only == 1
    assert df.at[0, "retry_attempted"] == ""


def test_no_canonical_facebook_url_is_skipped():
    df, summary = recover_dataframe(pd.DataFrame([_base_row(Facebook_URL="")]), dry_run=True)

    assert summary.driver_error_rows == 1
    assert summary.candidates_found == 0
    assert summary.excluded_no_canonical_fb_url == 1
    assert df.at[0, "retry_attempted"] == ""


def test_share_facebook_url_is_skipped_as_no_canonical_url():
    df, summary = recover_dataframe(
        pd.DataFrame([_base_row(Facebook_URL="https://www.facebook.com/share/abc")]),
        dry_run=True,
    )

    assert summary.driver_error_rows == 1
    assert summary.candidates_found == 0
    assert summary.excluded_no_canonical_fb_url == 1
    assert df.at[0, "retry_attempted"] == ""


def test_social_link_fb_without_canonical_url_is_not_recovered():
    df, summary = recover_dataframe(
        pd.DataFrame(
            [
                _base_row(
                    Facebook_URL="",
                    **{"Social Link": "https://www.facebook.com/exampleband"},
                )
            ]
        ),
        dry_run=True,
    )

    assert summary.candidates_found == 0
    assert summary.excluded_no_canonical_fb_url == 1
    assert df.at[0, "retry_attempted"] == ""


def test_recovery_selector_only_selects_driver_error_with_canonical_facebook_url():
    rows = [
        _base_row(
            FB_Status="driver_error",
            Facebook_URL="https://www.facebook.com/drivercanonical",
            **{"Artist Name": "Driver Canonical"},
        ),
        _base_row(
            FB_Status="no_candidates",
            FB_Debug_Reason="",
            Facebook_URL="https://www.facebook.com/nocandidatescanonical",
            **{"Artist Name": "No Candidates Canonical"},
        ),
        _base_row(
            FB_Status="no_canonical_fb_url",
            FB_Debug_Reason="",
            Facebook_URL="",
            **{"Artist Name": "No Canonical"},
        ),
    ]

    assert [row_qualifies_for_recovery(row) for row in rows] == [True, False, False]


def test_existing_fb_email_is_skipped_for_driver_error_recovery():
    row = _base_row(
        Email="artist@example.com",
        Email_All="artist@example.com",
        Email_Provenance_JSON=_fb_provenance(),
    )
    df, summary = recover_dataframe(pd.DataFrame([row]), dry_run=True)

    assert summary.driver_error_rows == 1
    assert summary.candidates_found == 0
    assert summary.excluded_existing_fb_email == 1
    assert df.at[0, "retry_attempted"] == ""


def test_selection_identifies_final_export_status_driver_error():
    row = _base_row(FB_Status="driver_error", FB_Debug_Reason="")

    assert row_qualifies_for_recovery(row) is True


def test_selection_identifies_debug_reason_driver_error():
    row = _base_row(FB_Status="", FB_Debug_Reason="driver_error")

    assert row_qualifies_for_recovery(row) is True


def test_selection_ignores_non_driver_terminal_status():
    row = _base_row(FB_Status="pass_a_no_email_on_page", FB_Debug_Reason="")

    assert row_qualifies_for_recovery(row) is False


def test_selection_identifies_driver_error_in_terminal_fields():
    for column in (
        "FB_Terminal_Reason",
        "FB_Normalized_Terminal_Reason",
        "FB_Normalized_Terminal_Outcome",
    ):
        row = _base_row(FB_Status="", FB_Debug_Reason="", **{column: "driver_error"})

        assert row_qualifies_for_recovery(row) is True


def test_successful_retry_updates_fb_status_and_email():
    calls = []

    def enricher(row, idx):
        calls.append((idx, row.copy()))
        out = row.copy()
        out["FB_Status"] = "pass_a_found_email"
        out["FB_Attempt_State"] = "attempted_fb_found_email"
        out["FB_Write_State"] = "fb_email_written"
        out["Email"] = "artist@example.com"
        out["Email_All"] = "artist@example.com"
        out["Email_Source_Type"] = "facebook_enrich"
        out["Email_Provenance_JSON"] = _fb_provenance()
        return out

    df, summary = recover_dataframe(pd.DataFrame([_base_row()]), fb_enricher=enricher)

    assert summary.candidates_found == 1
    assert summary.retry_attempted == 1
    assert summary.retry_success == 1
    assert summary.fb_email_found == 1
    assert calls
    assert df.at[0, "FB_Status"] == "pass_a_found_email"
    assert df.at[0, "Email"] == "artist@example.com"
    assert df.at[0, "retry_attempted"] == "true"
    assert df.at[0, "retry_success"] == "true"
    assert df.at[0, "previous_fb_debug_reason"] == "driver_error"
    assert df.at[0, "new_FB_Status"] == "pass_a_found_email"
    assert df.at[0, "new_FB_Attempt_State"] == "attempted_fb_found_email"


def test_retry_no_email_marks_success_without_email():
    def enricher(row, idx):
        out = row.copy()
        out["FB_Status"] = "pass_a_no_email_on_page"
        out["FB_Attempt_State"] = "attempted_fb_no_email_on_page"
        out["FB_Write_State"] = "fb_no_email_written"
        return out

    df, summary = recover_dataframe(pd.DataFrame([_base_row()]), fb_enricher=enricher)

    assert summary.retry_success == 1
    assert summary.fb_email_found == 0
    assert df.at[0, "FB_Attempt_State"] == "attempted_fb_no_email_on_page"
    assert df.at[0, "FB_Write_State"] == "fb_no_email_written"
    assert df.at[0, "Email"] == ""
    assert df.at[0, "retry_success"] == "true"


def test_already_valid_fb_rows_are_skipped():
    row = _base_row(
        FB_Status="fb_enrich_found_email",
        FB_Attempt_State="attempted_fb_found_email",
        Email="artist@example.com",
        Email_All="artist@example.com",
        Email_Provenance_JSON=_fb_provenance(),
    )
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run")),
    )

    assert summary.candidates_found == 0
    assert df.at[0, "Email"] == "artist@example.com"
    assert df.at[0, "retry_attempted"] == ""


def test_provenance_gate_retries_missing_or_non_fb_provenance():
    rows = [
        _base_row(Email="artist@example.com", Email_All="artist@example.com", Email_Provenance_JSON=""),
        _base_row(
            Email="artist2@example.com",
            Email_All="artist2@example.com",
            Email_Provenance_JSON=json.dumps(
                {
                    "artist2@example.com": {
                        "source_type": "instagram_profile",
                        "source_url": "https://www.instagram.com/artist2",
                    }
                }
            ),
        ),
    ]
    calls = []

    def enricher(row, idx):
        calls.append(idx)
        out = row.copy()
        out["FB_Status"] = "pass_a_no_email_on_page"
        out["FB_Attempt_State"] = "attempted_fb_no_email_on_page"
        return out

    df, summary = recover_dataframe(pd.DataFrame(rows), fb_enricher=enricher)

    assert summary.candidates_found == 2
    assert summary.retry_attempted == 2
    assert calls == [0, 1]
    assert df.at[0, "Email"] == "artist@example.com"
    assert df.at[1, "Email"] == "artist2@example.com"


def test_no_duplication_and_non_fb_fields_are_preserved():
    rows = [
        _base_row(**{"Unrelated Field": "candidate"}),
        _base_row(
            **{
                "Artist Name": "Untouched",
                "FB_Debug_Reason": "not_driver_error",
                "Unrelated Field": "untouched",
            }
        ),
    ]

    def enricher(row, idx):
        out = row.copy()
        out["FB_Status"] = "pass_a_no_email_on_page"
        out["FB_Attempt_State"] = "attempted_fb_no_email_on_page"
        out["Unrelated Field"] = "should-not-copy"
        return out

    before = pd.DataFrame(rows)
    after, summary = recover_dataframe(before, fb_enricher=enricher)

    assert len(before.index) == len(after.index)
    assert summary.rows_written == len(before.index)
    assert after.at[0, "Unrelated Field"] == "candidate"
    assert after.at[1, "Unrelated Field"] == "untouched"
