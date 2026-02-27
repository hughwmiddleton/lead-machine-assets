import final_checker
from night_mode_fb import NightModeFacebookEnricher, NightModeFacebookResult
from pipeline_runner import _fb_status_is_rejected


class _DummyLegacy:
    pass


def _enricher():
    # use_shared_session=False to avoid driver/session requirements in tests
    return NightModeFacebookEnricher(_DummyLegacy(), username="", password="", logger=None, use_shared_session=False)


def _empty_flags():
    return {"name_flag": 0, "dir_conflict_flag": 0, "dup_email_flag": 0, "dup_artist_flag": 0, "genre_outlier_flag": 0}


def test_mismatch_not_treated_as_reject():
    assert _fb_status_is_rejected("reject:name_mismatch") is True
    assert _fb_status_is_rejected("blocked_403") is True
    assert _fb_status_is_rejected("mismatch") is False
    assert _fb_status_is_rejected("mismatch_fallback") is False


def test_low_confidence_warn_keeps_email_and_sets_flags():
    enricher = _enricher()
    row = {"Artist Name": "Low Conf Artist", "Email": "", "Email_All": ""}
    night_result = NightModeFacebookResult(
        email="fb@test.com",
        email_all="fb@test.com",
        email_type="fb_night",
        facebook_url="https://facebook.com/page",
        accepted=True,
    )
    night_result.match_level = "mismatch"
    night_result.selected_by = "mismatch_fallback"
    night_result.review_reason = "fb_low_confidence:mismatch_fallback"

    updated = enricher._apply_night_fb_result(row, night_result, ["fb@test.com"], night_result.facebook_url)

    assert updated.get("Email") == "fb@test.com"
    assert updated.get("FB_Selected_By") == "mismatch_fallback"
    assert updated.get("FB_Match_Level") == "mismatch"
    assert updated.get("FB_Review_Reason") == "fb_low_confidence:mismatch_fallback"

    status = final_checker.compute_final_status(updated, _empty_flags(), match_score=0.9)
    assert status == "WARN"


def test_high_confidence_stays_ok():
    row = {"Artist Name": "Confident Artist", "Email": "ok@test.com", "Email_All": "ok@test.com"}
    row["FB_Match_Level"] = "near"
    row["FB_Selected_By"] = "ranked_sort"

    status = final_checker.compute_final_status(row, _empty_flags(), match_score=0.9)
    assert status == "OK"


def test_existing_block_remains_block():
    row = {"Artist Name": "Blocked Artist", "Email": "", "Email_All": "", "final_status": "BLOCK"}
    row["FB_Match_Level"] = "mismatch"
    status = final_checker.compute_final_status(row, _empty_flags(), match_score=0.9)
    assert status == "BLOCK"
