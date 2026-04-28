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
