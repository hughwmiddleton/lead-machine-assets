import pandas as pd

from scripts.recover_fb_share_rows import recover_dataframe, row_qualifies_for_recovery


def _base_row(**overrides):
    row = {
        "Artist Name": "Share Artist",
        "Social Link": "https://www.instagram.com/shareartist | https://www.facebook.com/share/1F7WrB5Hwt/?mibextid=wwXIfr",
        "External Links": "",
        "Facebook_URL": "",
        "facebook_url": "",
        "Facebook URL": "",
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
        "Email_Provenance_JSON": "",
        "FB_Status": "",
        "FB_Opportunity_State": "no_fb_opportunity",
        "FB_Gate_State": "",
        "FB_Attempt_State": "fb_not_attempted",
        "FB_Write_State": "",
        "FB_Debug_Reason": "",
        "Unrelated Field": "keep-me",
    }
    row.update(overrides)
    return row


def test_recovery_selection_identifies_share_no_opportunity_not_attempted_row():
    assert row_qualifies_for_recovery(_base_row()) is True


def test_successful_resolution_enters_fb_enrichment_seam():
    calls = []

    def resolver(raw):
        return "https://www.facebook.com/resolvedartist"

    def enricher(row, idx):
        calls.append((idx, row.copy()))
        out = row.copy()
        out["FB_Status"] = "pass_a_no_email_on_page"
        return out

    df, summary = recover_dataframe(
        pd.DataFrame([_base_row()]),
        share_resolver=resolver,
        fb_enricher=enricher,
    )

    assert summary.candidates_found == 1
    assert summary.resolution_success == 1
    assert summary.fb_attempted == 1
    assert calls
    assert calls[0][1]["Facebook_URL"] == "https://www.facebook.com/resolvedartist"
    assert calls[0][1]["FB_Opportunity_State"] == "fb_opportunity_present"
    assert calls[0][1]["FB_Attempt_State"] == "attempted_fb"
    assert df.at[0, "resolved_fb_url"] == "https://www.facebook.com/resolvedartist"
    assert df.at[0, "FB_Opportunity_State"] == "fb_opportunity_present"
    assert df.at[0, "FB_Attempt_State"] == "attempted_fb_no_email_on_page"


def test_failed_resolution_records_failure_and_does_not_attempt_fb():
    calls = []

    df, summary = recover_dataframe(
        pd.DataFrame([_base_row()]),
        share_resolver=lambda raw: "",
        fb_enricher=lambda row, idx: calls.append(row) or row,
    )

    assert summary.candidates_found == 1
    assert summary.resolution_failed == 1
    assert summary.fb_attempted == 0
    assert calls == []
    assert df.at[0, "resolution_attempted"] == "true"
    assert df.at[0, "resolution_success"] == "false"
    assert df.at[0, "FB_Gate_State"] == "fb_share_resolution_failed"
    assert "fb_share_resolution_failed" in df.at[0, "FB_Debug_Reason"]
    assert df.at[0, "FB_Opportunity_State"] == "no_fb_opportunity"
    assert df.at[0, "FB_Attempt_State"] == "fb_not_attempted"


def test_already_processed_rows_are_untouched():
    row = _base_row(FB_Attempt_State="attempted_fb_no_email_on_page")
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: "https://www.facebook.com/resolvedartist",
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run")),
    )

    assert summary.candidates_found == 0
    assert df.at[0, "FB_Attempt_State"] == "attempted_fb_no_email_on_page"
    assert df.at[0, "Facebook_URL"] == ""
    assert df.at[0, "Unrelated Field"] == "keep-me"


def test_existing_fb_result_rows_are_untouched():
    row = _base_row(
        Email="artist@example.com",
        Email_All="artist@example.com",
        Email_Source_Type="facebook_enrich",
        FB_Status="fb_enrich_found_email",
    )
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: "https://www.facebook.com/resolvedartist",
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run")),
    )

    assert summary.candidates_found == 0
    assert df.at[0, "Email"] == "artist@example.com"
    assert df.at[0, "Facebook_URL"] == ""


def test_no_duplication_and_non_fb_fields_are_preserved():
    rows = [
        _base_row(**{"Unrelated Field": "candidate"}),
        _base_row(
            **{
                "Artist Name": "Untouched",
                "Social Link": "https://www.instagram.com/untouched",
                "Unrelated Field": "untouched",
            }
        ),
    ]

    def enricher(row, idx):
        out = row.copy()
        out["FB_Status"] = "pass_a_no_email_on_page"
        out["Unrelated Field"] = "should-not-copy"
        return out

    before = pd.DataFrame(rows)
    after, summary = recover_dataframe(
        before,
        share_resolver=lambda raw: "https://www.facebook.com/resolvedartist",
        fb_enricher=enricher,
    )

    assert len(before.index) == len(after.index)
    assert summary.rows_written == len(before.index)
    assert after.at[0, "Unrelated Field"] == "candidate"
    assert after.at[1, "Unrelated Field"] == "untouched"
