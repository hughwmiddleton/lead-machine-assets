from night_mode_fb import NightModeFacebookEnricher, NightModeFacebookResult, _candidate_url


class _DummyLegacy:
    pass


def _make_enricher():
    # use_shared_session=False to avoid driver/session requirements in tests
    return NightModeFacebookEnricher(_DummyLegacy(), username="", password="", use_shared_session=False)


def _apply_owner(enricher: NightModeFacebookEnricher, artist: str, url: str) -> None:
    row = {"Artist Name": artist, "Email": "", "Email_All": ""}
    night_result = NightModeFacebookResult()
    night_result.email = "owner@test.com"
    night_result.email_all = "owner@test.com"
    night_result.email_type = "fb_night"
    night_result.facebook_url = url
    night_result.candidate_url = url
    # Simulate accepted FB result with emails applied so ownership is recorded.
    enricher._apply_night_fb_result(row, night_result, ["owner@test.com"], url)


def test_owner_guard_blocks_cross_artist_reuse():
    enricher = _make_enricher()
    url = "https://www.facebook.com/djvirginia"

    _apply_owner(enricher, "Artist A", url)

    ranked_items = [
        {"candidate": {"url": url}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist B", ranked_items)

    assert cand is None
    assert selected_by == "no_viable_candidate"
    assert enricher._fb_owner_skip_count == 1


def test_owner_guard_allows_fallback_to_other_candidate():
    enricher = _make_enricher()
    url_used = "https://www.facebook.com/djvirginia"
    url_other = "https://www.facebook.com/goodartist"

    _apply_owner(enricher, "Artist A", url_used)

    ranked_items = [
        {"candidate": {"url": url_used}, "features": {"match_level": "near"}},
        {"candidate": {"url": url_other}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist B", ranked_items)

    assert _candidate_url(cand) == url_other
    assert selected_by == "ranked_sort"


def test_owner_guard_does_not_block_same_artist():
    enricher = _make_enricher()
    url = "https://www.facebook.com/djvirginia"

    _apply_owner(enricher, "Artist A", url)

    ranked_items = [
        {"candidate": {"url": url}, "features": {"match_level": "near"}},
    ]

    cand, selected_by = enricher._choose_ranked_candidate("Artist A", ranked_items)

    assert _candidate_url(cand) == url
    assert selected_by == "ranked_sort"
