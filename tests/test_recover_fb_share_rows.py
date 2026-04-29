import pandas as pd

from scripts.recover_fb_share_rows import (
    MAX_CANONICAL_DISCOVERY_CANDIDATES,
    SHARE_DISCOVERY_FAILED_REASON,
    SHARE_DISCOVERY_SUCCESS_REASON,
    discover_canonical_fb_candidates,
    recover_dataframe,
    row_qualifies_for_recovery,
)


def _base_row(**overrides):
    row = {
        "Artist Name": "Share Artist",
        "Social Link": "https://www.instagram.com/safeartist | https://www.facebook.com/share/1F7WrB5Hwt/?mibextid=wwXIfr",
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
        "FB_Status": "fb_share_resolution_failed",
        "FB_Opportunity_State": "no_fb_opportunity",
        "FB_Gate_State": "fb_share_resolution_failed",
        "FB_Attempt_State": "fb_not_attempted",
        "FB_Extract_State": "fb_extract_not_attempted",
        "FB_Write_State": "fb_no_email_written",
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

    assert summary.candidates == 1
    assert summary.resolved == 1
    assert summary.enriched == 1
    assert calls
    assert calls[0][1]["Facebook_URL"] == "https://www.facebook.com/resolvedartist"
    assert calls[0][1]["FB_Opportunity_State"] == "fb_opportunity_present"
    assert calls[0][1]["FB_Attempt_State"] == "attempted_fb"
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/resolvedartist"
    assert df.at[0, "FB_Status"] == "recovered_share_url"
    assert df.at[0, "FB_Opportunity_State"] == "fb_opportunity_present"
    assert df.at[0, "FB_Attempt_State"] == "attempted_fb_no_email_on_page"


def test_failed_resolution_uses_canonical_discovery_and_writes_email():
    calls = []

    def enricher(row, idx):
        calls.append(row["Facebook_URL"])
        assert "/share/" not in row["Facebook_URL"].lower()
        out = row.copy()
        out["Email"] = "zedena@example.com"
        out["Email_All"] = "zedena@example.com"
        out["Email_Source_URL"] = row["Facebook_URL"]
        out["Email_Source_Type"] = "facebook_enrich"
        out["Email_Extract_Method"] = "regex"
        out["FB_Attempt_State"] = "attempted_fb_found_email"
        return out

    row = _base_row(
        **{
            "Artist Name": "zedena",
            "Social Link": (
                "https://www.facebook.com/share/1BwSqEJRTk?mibextid=abc | "
                "instagram.com/zedenamusic | "
                "tiktok.com/@zedenamusic | "
                "youtube.com/@zedena4604"
            ),
        }
    )
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: "",
        fb_enricher=enricher,
    )

    assert calls == ["https://www.facebook.com/zedenamusic"]
    assert summary.candidates == 1
    assert summary.resolved == 1
    assert summary.enriched == 1
    assert summary.fb_email_found == 1
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/zedenamusic"
    assert df.at[0, "Email"] == "zedena@example.com"
    assert "facebook.com/share/" in df.at[0, "Social Link"]
    assert SHARE_DISCOVERY_SUCCESS_REASON in df.at[0, "FB_Debug_Reason"]


def test_failed_resolution_without_valid_candidates_does_not_attempt_fb():
    calls = []
    before = pd.DataFrame(
        [
            _base_row(
                **{
                    "Artist Name": "!!!",
                    "Social Link": "https://www.facebook.com/share/1F7WrB5Hwt/?mibextid=wwXIfr",
                }
            )
        ]
    )

    df, summary = recover_dataframe(
        before,
        share_resolver=lambda raw: "",
        fb_enricher=lambda row, idx: calls.append(row) or row,
    )

    assert summary.candidates == 1
    assert summary.failed == 1
    assert summary.enriched == 0
    assert calls == []
    assert df.at[0, "FB_Status"] == "fb_share_resolution_failed"
    assert df.at[0, "FB_Attempt_State"] == "fb_not_attempted"
    assert df.at[0, "FB_Extract_State"] == "fb_extract_not_attempted"
    assert SHARE_DISCOVERY_FAILED_REASON in df.at[0, "FB_Debug_Reason"]


def test_already_processed_rows_are_untouched():
    row = _base_row(FB_Attempt_State="attempted_fb_no_email_on_page")
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: "https://www.facebook.com/resolvedartist",
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run")),
    )

    assert summary.candidates == 0
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

    assert summary.candidates == 0
    assert df.at[0, "Email"] == "artist@example.com"
    assert df.at[0, "Facebook_URL"] == ""


def test_existing_usable_non_fb_email_is_skipped():
    row = _base_row(
        Email="artist@example.com",
        Email_All="artist@example.com",
        Email_Source_Type="website",
    )
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: (_ for _ in ()).throw(AssertionError("resolver should not run")),
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run")),
    )

    assert summary.candidates == 0
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
    assert after["Artist Name"].tolist() == ["Share Artist", "Untouched"]


def test_valid_facebook_url_is_skipped_and_counted():
    row = _base_row(Facebook_URL="https://www.facebook.com/alreadycanonical")
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: (_ for _ in ()).throw(AssertionError("resolver should not run")),
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run")),
    )

    assert summary.candidates == 0
    assert summary.skipped_existing == 1
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/alreadycanonical"


def test_state_column_can_select_share_failure():
    row = _base_row(FB_Status="", FB_Gate_State="", state="fb_share_resolution_failed")
    df, summary = recover_dataframe(
        pd.DataFrame([row]),
        share_resolver=lambda raw: "https://www.facebook.com/fromstate",
        fb_enricher=lambda row, idx: row,
    )

    assert summary.candidates == 1
    assert summary.resolved == 1
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/fromstate"


def test_idempotency_second_run_does_not_reenrich():
    calls = []

    def enricher(row, idx):
        calls.append(idx)
        return row

    first, first_summary = recover_dataframe(
        pd.DataFrame([_base_row()]),
        share_resolver=lambda raw: "https://www.facebook.com/resolvedartist",
        fb_enricher=enricher,
    )
    second, second_summary = recover_dataframe(
        first,
        share_resolver=lambda raw: "https://www.facebook.com/other",
        fb_enricher=lambda row, idx: (_ for _ in ()).throw(AssertionError("FB should not run twice")),
    )

    assert first_summary.enriched == 1
    assert second_summary.candidates == 0
    assert calls == [0]
    pd.testing.assert_frame_equal(first, second)


def test_successful_share_resolution_does_not_use_canonical_discovery_marker():
    def resolver(raw):
        return "https://www.facebook.com/resolvedartist"

    df, summary = recover_dataframe(
        pd.DataFrame([_base_row()]),
        share_resolver=resolver,
        fb_enricher=lambda row, idx: row,
    )

    assert summary.resolved == 1
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/resolvedartist"
    assert SHARE_DISCOVERY_SUCCESS_REASON not in df.at[0, "FB_Debug_Reason"]


def test_candidate_cap_enforced_and_invalid_facebook_shapes_excluded():
    links = " | ".join(f"https://www.instagram.com/artist{i}" for i in range(20))
    row = _base_row(Social_Link="")
    row["Social Link"] = f"https://www.facebook.com/share/1F7WrB5Hwt/?mibextid=wwXIfr | {links}"

    candidates = discover_canonical_fb_candidates(row)

    assert len(candidates) == MAX_CANONICAL_DISCOVERY_CANDIDATES
    assert candidates[0] == "https://www.facebook.com/artist0"
    assert all("/share/" not in candidate.lower() for candidate in candidates)


def test_no_direct_share_scrape_when_discovery_candidate_exists():
    calls = []

    def enricher(row, idx):
        calls.append(row["Facebook_URL"])
        return row

    df, summary = recover_dataframe(
        pd.DataFrame([_base_row()]),
        share_resolver=lambda raw: "",
        fb_enricher=enricher,
    )

    assert summary.enriched >= 1
    assert calls
    assert all("/share/" not in url.lower() for url in calls)
    assert "facebook.com/share/" in df.at[0, "Social Link"]
