import pytest

from night_mode_fb import (
    NightModeFacebookEnricher,
    NightModeFacebookResult,
    _candidate_url,
)


class _DummyLegacy:
    pass


def _make_enricher():
    # use_shared_session=False to avoid session enforcement in tests
    return NightModeFacebookEnricher(_DummyLegacy(), username="", password="", use_shared_session=False)


def test_fb_reject_does_not_persist_emails():
    enricher = _make_enricher()
    row = {"Artist Name": "DJ Virginia", "Email": "", "Email_All": ""}
    night_result = NightModeFacebookResult(
        email="r@studiodemeyer.com",
        email_all="r@studiodemeyer.com",
        email_type="fb_night",
        facebook_url="https://www.facebook.com/djvirginia",
        email_source="main",
        about_attempted="yes",
        about_result="emails_found",
        accepted=False,
        reject_reason="email_override_reject:name_mismatch",
        candidate_url="https://www.facebook.com/djvirginia",
    )
    emails = ["r@studiodemeyer.com"]

    updated = enricher._apply_night_fb_result(dict(row), night_result, emails, night_result.facebook_url)

    assert updated.get("Email", "") == ""
    assert updated.get("Email_All", "") == ""
    assert "__fb_emails_applied" not in updated
    assert updated.get("FB_Status") == "rejected"
    assert updated.get("FB_Reason") == "email_override_reject:name_mismatch"


def test_fb_reject_cache_prevents_reselect():
    enricher = _make_enricher()
    artist = "DJ Virginia"
    rejected_url = "https://www.facebook.com/djvirginia"
    other_url = "https://www.facebook.com/goodartist"

    enricher._fb_mark_rejected(artist, rejected_url, "no_music_signals")

    ranked_items = [
        {"candidate": {"url": rejected_url}, "features": {"match_level": "near"}},
        {"candidate": {"url": other_url}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate(artist, ranked_items)

    assert _candidate_url(cand) == other_url
    assert selected_by == "ranked_sort"


def test_mismatch_fallback_is_blocked_when_rejected():
    enricher = _make_enricher()
    artist = "Some Artist"
    rejected_url = "https://www.facebook.com/rejectedpage"

    # Mark globally rejected
    enricher._fb_mark_rejected(artist, rejected_url, "no_music_signals")

    ranked_items = [
        {"candidate": {"url": rejected_url}, "features": {"match_level": "mismatch"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate(artist, ranked_items)

    assert cand is None
    assert selected_by == "no_viable_candidate"


def test_global_reject_blocks_other_artist():
    enricher = _make_enricher()
    url = "https://www.facebook.com/crossartist"

    enricher._fb_mark_rejected("Artist A", url, "no_music_signals")

    ranked_items = [
        {"candidate": {"url": url}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist B", ranked_items)

    assert cand is None
    assert selected_by == "no_viable_candidate"


def test_url_normalization_blocks_variants():
    enricher = _make_enricher()
    rejected_variant = "https://m.facebook.com/PageName/?fbclid=XYZ"
    candidate_variant = "https://www.facebook.com/pagename"

    enricher._fb_mark_rejected("Artist A", rejected_variant, "no_music_signals")

    ranked_items = [
        {"candidate": {"url": candidate_variant}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist B", ranked_items)

    assert cand is None
    assert selected_by == "no_viable_candidate"


def test_pg_normalization_blocks_variants():
    enricher = _make_enricher()
    rejected_variant = "https://www.facebook.com/pg/PageName/about?fbclid=XYZ"
    candidate_variant = "https://www.facebook.com/pagename"

    enricher._fb_mark_rejected("Artist A", rejected_variant, "no_music_signals")

    ranked_items = [
        {"candidate": {"url": candidate_variant}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist B", ranked_items)

    assert cand is None
    assert selected_by == "no_viable_candidate"


def test_bad_url_reject_does_not_block_valid_candidate():
    enricher = _make_enricher()
    bad_url = "not a url"
    good_url = "https://www.facebook.com/goodartist"

    enricher._fb_mark_rejected("Artist A", bad_url, "no_music_signals")

    ranked_items = [
        {"candidate": {"url": good_url}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist B", ranked_items)

    assert _candidate_url(cand) == good_url
    assert selected_by == "ranked_sort"
