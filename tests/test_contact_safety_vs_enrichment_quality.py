"""Contact safety must be decided separately from enrichment quality.

A valid, safely sourced email must not be classified BLOCK (or dropped from the
customer-facing export) merely because unrelated enrichment fields are weak.
Hard contact-safety conditions must stay strict.
"""

import json

import pandas as pd
import pytest

import final_checker
from email_provenance import EMAIL_PROVENANCE_JSON_COL
from pipeline_runner import recompute_final_status_post_enrichment


def _flags(**overrides) -> dict:
    flags = {
        "name_flag": 0,
        "dir_conflict_flag": 0,
        "dup_email_flag": 0,
        "dup_artist_flag": 0,
        "genre_outlier_flag": 0,
    }
    flags.update(overrides)
    return flags


def _provenance(email: str, source_type: str, source_url: str, surface: str) -> str:
    return json.dumps(
        {
            email: {
                "source_type": source_type,
                "source_url": source_url,
                "surface": surface,
                "extract_method": "regex",
            }
        }
    )


def _trusted_fb_row(**overrides) -> dict:
    """An artist whose email came off their own Facebook About surface."""
    email = overrides.pop("email", "booking@vividmoss.com")
    row = {
        "Artist Name": "Vivid Moss",
        "Song Title": "Slow Bloom",
        "Email": email,
        "Email_All": email,
        "Email_Source_Type": "facebook_enrich",
        "Email_Source_URL": "https://www.facebook.com/vividmoss/about",
        "Email_Extract_Method": "regex",
        EMAIL_PROVENANCE_JSON_COL: _provenance(
            email, "facebook_enrich", "https://www.facebook.com/vividmoss/about", "facebook_about"
        ),
        "FB_Status": "found_email",
        "FB_Selected_By": "",
        "FB_Match_Level": "",
        "FB_Name_Consistency_Flag": "",
        "FB_Review_Reason": "",
        "Review_Urls": "",
        "Source Directory": "bandcamp",
        "Source URL": "https://vividmoss.bandcamp.com/",
    }
    row.update(overrides)
    return row


def _trusted_soundcloud_row(**overrides) -> dict:
    """An artist whose email came off their own SoundCloud profile."""
    email = overrides.pop("email", "vividmoss@riseup.net")
    row = {
        "Artist Name": "Vivid Moss",
        "Song Title": "Slow Bloom",
        "Email": email,
        "Email_All": email,
        "Email_Source_Type": "soundcloud",
        "Email_Source_URL": "https://soundcloud.com/vm-alt-handle",
        "Email_Extract_Method": "regex",
        EMAIL_PROVENANCE_JSON_COL: _provenance(
            email, "soundcloud", "https://soundcloud.com/vm-alt-handle", "soundcloud_profile"
        ),
        "FB_Status": "",
        "FB_Review_Reason": "",
        "Review_Urls": "",
        "Source Directory": "soundcloud",
        "Source URL": "https://soundcloud.com/vm-alt-handle",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# A. Enrichment-quality failures must not BLOCK a safely sourced email
# --------------------------------------------------------------------------


def test_title_not_found_alone_does_not_block_trusted_email() -> None:
    row = _trusted_fb_row(
        origin_match_flag="0",
        origin_match_reason="title_not_found",
        origin_artist_score="0.95",
        origin_title_score="0.18",
    )

    status = final_checker.compute_final_status(row, _flags(), match_score=0.95)

    assert status != "BLOCK"
    assert status == "OK"


def test_failed_facebook_discovery_does_not_block_independent_trusted_email() -> None:
    row = _trusted_soundcloud_row(FB_Status="no_candidates")

    status = final_checker.compute_final_status(row, _flags(), match_score=0.95)

    assert status != "BLOCK"


def test_no_facebook_candidate_does_not_block_independent_trusted_email() -> None:
    row = _trusted_soundcloud_row(FB_Status="pass_a_no_email_on_page")

    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) != "BLOCK"


def test_soundcloud_enrichment_failure_does_not_block_trusted_fb_email() -> None:
    row = _trusted_fb_row(SC_Status="error", **{"SoundCloud Link": ""})

    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "OK"


def test_weak_secondary_directory_match_warns_but_never_blocks() -> None:
    """B: a directory slug disagreement is enrichment quality, not safety."""
    row = _trusted_fb_row()

    status = final_checker.compute_final_status(row, _flags(dir_conflict_flag=1), match_score=0.75)

    assert status == "WARN"


def test_genre_outlier_alone_cannot_demote_a_contact_safe_email() -> None:
    """A: genre rarity is measured within a run and is not a safety signal."""
    row = _trusted_fb_row()

    without_flag = final_checker.compute_final_status(row, _flags(), match_score=0.95)
    with_flag = final_checker.compute_final_status(row, _flags(genre_outlier_flag=1), match_score=0.95)

    assert without_flag == "OK"
    assert with_flag == "OK"


def test_genre_outlier_does_not_demote_on_the_non_strong_identity_path() -> None:
    row = _trusted_fb_row()

    status = final_checker.compute_final_status(
        row, _flags(genre_outlier_flag=1, dup_email_flag=0, name_flag=1), match_score=0.95
    )

    assert status == "OK"


# --------------------------------------------------------------------------
# C. Third-party organisational inbox on a trusted surface -> review
# --------------------------------------------------------------------------


def test_third_party_company_domain_on_trusted_page_is_warn_not_block_or_ok() -> None:
    row = _trusted_fb_row(email="info@tremoloprod.com")

    status = final_checker.compute_final_status(row, _flags(), match_score=0.95)

    assert status == "WARN"
    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_THIRD_PARTY


def test_role_inbox_on_the_entity_own_domain_stays_trusted() -> None:
    row = _trusted_fb_row(email="info@vividmoss.com")

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_TRUSTED
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "OK"


# --------------------------------------------------------------------------
# D/E/F/G. Hard contact-safety conditions stay strict
# --------------------------------------------------------------------------


def test_non_fb_trusted_email_survives_rejected_fb_candidate() -> None:
    """D: a rejected FB discovery must not condemn an independent email."""
    row = _trusted_soundcloud_row(FB_Status="candidate_rejected")

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_TRUSTED
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) != "BLOCK"


def test_fb_derived_email_from_rejected_surface_remains_unusable() -> None:
    """E/F: an email lifted from a rejected FB surface stays BLOCK."""
    row = _trusted_fb_row(FB_Status="candidate_rejected")
    row["__fb_emails_applied"] = row["Email"]

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_UNSAFE
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "BLOCK"


def test_rejected_fb_row_without_provenance_fails_closed() -> None:
    row = _trusted_fb_row(FB_Status="reject:name_mismatch")
    row[EMAIL_PROVENANCE_JSON_COL] = ""
    row["Email_Source_Type"] = ""
    row["Email_Source_URL"] = ""

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_UNSAFE
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "BLOCK"


@pytest.mark.parametrize(
    "private_url",
    [
        "https://www.facebook.com/messenger",
        "https://m.me/vividmoss",
        "https://www.facebook.com/messages/t/1234567890",
        "https://www.facebook.com/share/p/abc123",
    ],
)
def test_private_route_provenance_is_never_a_usable_contact(private_url: str) -> None:
    """G: private/Messenger-derived data stays blocked."""
    email = "booking@vividmoss.com"
    row = _trusted_fb_row(
        Email_Source_URL=private_url,
        **{EMAIL_PROVENANCE_JSON_COL: _provenance(email, "facebook_enrich", private_url, "facebook_main")},
    )

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_UNSAFE
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "BLOCK"


def test_stale_facebook_surface_label_on_non_facebook_url_is_not_unsafe() -> None:
    """A mislabelled surface must not turn a SoundCloud email into a hard BLOCK.

    Real rows carry source_type=soundcloud with a stale surface=facebook_main;
    the source URL is authoritative for the private-route check.
    """
    email = "vividmoss@riseup.net"
    row = _trusted_soundcloud_row(
        **{
            EMAIL_PROVENANCE_JSON_COL: _provenance(
                email, "soundcloud", "https://soundcloud.com/vm-alt-handle", "facebook_main"
            )
        }
    )

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_TRUSTED
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "OK"


def test_facebook_family_hosts_are_all_route_checked() -> None:
    assert final_checker._is_facebook_host("https://m.me/x")
    assert final_checker._is_facebook_host("https://www.facebook.com/x")
    assert final_checker._is_facebook_host("https://web.facebook.com/x")
    assert final_checker._is_facebook_host("https://fb.me/x")
    assert not final_checker._is_facebook_host("https://soundcloud.com/x")
    assert not final_checker._is_facebook_host("https://notfacebook.com.evil.example/x")


@pytest.mark.parametrize(
    "platform_email",
    ["support@bandcamp.com", "noreply@soundcloud.com", "help@facebook.com"],
)
def test_platform_support_email_still_blocks(platform_email: str) -> None:
    """E: platform/support/footer emails remain unusable."""
    row = _trusted_fb_row(email=platform_email)

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_UNSAFE
    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "BLOCK"


def test_identity_contradiction_still_blocks() -> None:
    row = _trusted_fb_row(**{"Artist Name": "Vivid Moss"})

    status = final_checker.compute_final_status(row, _flags(name_flag=1), match_score=0.4)

    assert status == "BLOCK"


def test_labelish_entity_still_blocks_even_with_trusted_email() -> None:
    row = _trusted_fb_row(**{"Artist Name": "Vivid Moss Recordings"})

    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "BLOCK"


def test_malformed_email_is_not_treated_as_a_usable_contact() -> None:
    row = _trusted_fb_row(email="not-an-email")

    assert final_checker.classify_contact_attribution(row) == final_checker.ATTRIBUTION_NONE


def test_directory_conflict_without_attributable_contact_still_blocks() -> None:
    row = _trusted_fb_row()
    row[EMAIL_PROVENANCE_JSON_COL] = ""
    row["Email_Source_Type"] = ""
    row["Email_Source_URL"] = ""

    status = final_checker.compute_final_status(row, _flags(dir_conflict_flag=1), match_score=0.75)

    assert status == "BLOCK"


# --------------------------------------------------------------------------
# F. name_consistency_flag polarity is consistent writer -> reader
# --------------------------------------------------------------------------


def test_name_consistency_flag_writer_uses_one_means_consistent(tmp_path) -> None:
    rows = [
        {
            "Artist Name": "Vivid Moss",
            "Song Title": "Slow Bloom",
            "Email": "booking@vividmoss.com",
            "Source URL": "https://vividmoss.bandcamp.com/",
            "Facebook_URL": "https://www.facebook.com/vividmoss",
        },
        {
            "Artist Name": "Vivid Moss",
            "Song Title": "Slow Bloom",
            "Email": "hi@example.com",
            "Source URL": "https://totallyunrelatedname.bandcamp.com/",
            "Facebook_URL": "https://www.facebook.com/totallyunrelatedname",
        },
    ]
    csv_path = tmp_path / "input.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    checked = pd.read_csv(final_checker.run_final_checker(str(csv_path)))

    assert int(checked.iloc[0]["name_consistency_flag"]) == 1, "matching name must read as consistent"
    assert int(checked.iloc[1]["name_consistency_flag"]) == 0, "mismatching name must read as inconsistent"


def test_post_enrichment_repair_downgrades_directory_conflict_block_with_trusted_email() -> None:
    """The writer/reader polarity agreement makes the late BLOCK repair reachable."""
    row = _trusted_fb_row()
    row.update(
        {
            "final_status": "BLOCK",
            "origin_match_flag": "0",
            "origin_match_reason": "title_not_found",
            "directory_conflict_flag": "1",
            "duplicate_email_flag": "0",
            "duplicate_artist_flag": "0",
            "name_consistency_flag": "1",
            "match_score_overall": "0.75",
        }
    )

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "WARN"
    assert str(result.iloc[0]["Needs_Review"]).upper() == "TRUE"


def test_post_enrichment_repair_keeps_block_when_name_is_inconsistent() -> None:
    row = _trusted_fb_row()
    row.update(
        {
            "final_status": "BLOCK",
            "origin_match_flag": "0",
            "directory_conflict_flag": "1",
            "duplicate_email_flag": "0",
            "duplicate_artist_flag": "0",
            "name_consistency_flag": "0",
            "match_score_overall": "0.5",
        }
    )

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "BLOCK"


def test_post_enrichment_repair_does_not_rescue_rejected_fb_derived_email() -> None:
    row = _trusted_fb_row(FB_Status="candidate_rejected")
    row["__fb_emails_applied"] = row["Email"]
    row.update(
        {
            "final_status": "BLOCK",
            "origin_match_flag": "0",
            "directory_conflict_flag": "1",
            "duplicate_email_flag": "0",
            "duplicate_artist_flag": "0",
            "name_consistency_flag": "1",
            "match_score_overall": "0.75",
        }
    )

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "BLOCK"


def test_post_enrichment_repair_rescues_non_fb_email_despite_fb_rejection() -> None:
    """D, at the repair layer."""
    row = _trusted_soundcloud_row(FB_Status="candidate_rejected")
    row.update(
        {
            "final_status": "BLOCK",
            "origin_match_flag": "0",
            "directory_conflict_flag": "1",
            "duplicate_email_flag": "0",
            "duplicate_artist_flag": "0",
            "name_consistency_flag": "1",
            "match_score_overall": "0.75",
        }
    )

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "WARN"


# --------------------------------------------------------------------------
# G. Blank/NaN Facebook review fields must not fabricate a warning
# --------------------------------------------------------------------------


def test_nan_fb_review_reason_does_not_create_fake_low_confidence() -> None:
    row = _trusted_fb_row()
    row["FB_Review_Reason"] = float("nan")
    row["FB_Name_Consistency_Flag"] = float("nan")
    row["FB_Selected_By"] = float("nan")
    row["FB_Match_Level"] = float("nan")

    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "OK"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (float("nan"), None),
        ("", None),
        (None, None),
        (0, 0),
        (0.0, 0),
        ("0", 0),
        ("0.0", 0),
        (1, 1),
        (1.0, 1),
        ("1.0", 1),
        ("not-a-number", None),
    ],
)
def test_optional_int_flag_is_nan_safe_without_dropping_zero(raw, expected) -> None:
    """A numeric 0 must survive: it is a real FB name-inconsistency signal."""
    assert final_checker._optional_int_flag(raw) == expected


def test_fb_name_inconsistency_flag_still_warns_when_float_formatted() -> None:
    for raw in (0, 0.0, "0", "0.0"):
        row = _trusted_fb_row(FB_Name_Consistency_Flag=raw)
        assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "WARN", raw


def test_real_fb_review_reason_still_warns() -> None:
    row = _trusted_fb_row(FB_Review_Reason="fb_low_confidence:mismatch_fallback")

    assert final_checker.compute_final_status(row, _flags(), match_score=0.95) == "WARN"


def test_run_final_checker_can_still_emit_ok_after_the_facebook_pass(tmp_path) -> None:
    """Blank post-FB columns previously forced every row to WARN."""
    row = _trusted_fb_row()
    row.update({"FB_Review_Reason": "", "FB_Name_Consistency_Flag": "", "Review_Urls": ""})
    csv_path = tmp_path / "post_fb.csv"
    pd.DataFrame([row]).to_csv(csv_path, index=False)

    checked = pd.read_csv(final_checker.run_final_checker(str(csv_path)))

    assert checked.iloc[0]["final_status"] == "OK"


# --------------------------------------------------------------------------
# H/I. Export profile gating
# --------------------------------------------------------------------------


def test_studio_safe_excludes_hard_risk_rows_and_studio_plus_excludes_block() -> None:
    rows = [
        {"Artist Name": "Ok Lead", "final_status": "OK", "Email": "ok@oklead.com"},
        {"Artist Name": "Review Lead", "final_status": "WARN", "Email": "warn@reviewlead.com"},
        {"Artist Name": "Unsafe Lead", "final_status": "BLOCK", "Email": "block@unsafelead.com"},
    ]

    safe = final_checker.filter_rows_for_export("studio_safe", rows)
    plus = final_checker.filter_rows_for_export("studio_plus", rows)

    assert [r["Artist Name"] for r in safe] == ["Ok Lead"]
    assert [r["Artist Name"] for r in plus] == ["Ok Lead", "Review Lead"]
    assert all(r["final_status"] != "BLOCK" for r in plus)


def test_studio_safe_never_contains_a_block_row_for_any_input() -> None:
    rows = [
        {"Artist Name": f"Row {idx}", "final_status": status, "Email": f"row{idx}@example.com"}
        for idx, status in enumerate(["OK", "WARN", "BLOCK", "", "REVIEW", "BLOCKED_BY_ORIGIN"])
    ]

    for profile in ("studio_safe", "studio_plus"):
        exported = final_checker.filter_rows_for_export(profile, rows)
        assert all(str(r.get("final_status", "")).upper() != "BLOCK" for r in exported)
