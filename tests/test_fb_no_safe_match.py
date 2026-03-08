from types import SimpleNamespace

import night_mode_fb


def _cand(
    name: str,
    url: str,
    category: str = "",
    aria: str = "",
    secondary: str = "",
    descriptor: str = "",
    category_tokens=None,
):
    return SimpleNamespace(
        name=name,
        url=url,
        category=category,
        aria_label=aria,
        secondary_text=secondary,
        descriptor=descriptor,
        category_tokens=list(category_tokens or []),
    )


def _make_enricher():
    class _DummyLegacy:
        pass

    # use_shared_session=False to avoid session enforcement in tests
    return night_mode_fb.NightModeFacebookEnricher(_DummyLegacy(), username="", password="", use_shared_session=False)


def _rank_item(artist: str, cand):
    score, breakdown, features = night_mode_fb._score_fb_candidate_night(artist, cand)
    return {"candidate": cand, "score": score, "breakdown": breakdown, "features": features}


def test_non_music_dental_mismatch_is_rejected():
    enricher = _make_enricher()
    artist = "The Enhancer"
    bad = _cand("ADI - American Dental Institute", "https://www.facebook.com/adi.dental", "Dental clinic")
    ranked = [_rank_item(artist, bad)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked, min_accept_score=-50)

    assert chosen is None
    assert selected_by == "no_safe_match"
    assert enricher._last_search_reject_reason == "non_music_category"


def test_rank_threshold_blocks_low_score():
    enricher = _make_enricher()
    artist = "Low Score Artist"
    weak = _cand("Low Score Artist", "https://www.facebook.com/lowscore", "Product/service")
    ranked = [_rank_item(artist, weak)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked, min_accept_score=10)

    assert chosen is None
    assert selected_by == "no_safe_match"
    assert enricher._last_search_reject_reason == "rank_below_threshold"


def test_mismatch_with_music_signal_can_pass():
    enricher = _make_enricher()
    artist = "The Enhancer"
    music_mismatch = _cand("Moonrise Music Collective", "https://www.facebook.com/moonrisemusic", "Musician/Band")
    ranked = [_rank_item(artist, music_mismatch)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked)

    assert chosen is music_mismatch
    assert selected_by in {"ranked_sort", "mismatch_fallback"}


def test_low_similarity_corporate_candidate_is_rejected():
    enricher = _make_enricher()
    artist = "Midnight Birds"
    bad = _cand(
        "Midtown Construction Company",
        "https://www.facebook.com/midtownconstructioncompany",
        "Construction company",
    )
    ranked = [_rank_item(artist, bad)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked, min_accept_score=-50)

    assert chosen is None
    assert selected_by == "no_safe_match"
    assert enricher._last_search_reject_reason == "corporate_no_music_signal"


def test_profile_like_candidate_without_music_metadata_is_rejected():
    enricher = _make_enricher()
    artist = "Aurora Beam"
    bad = _cand(
        "Aurora Personal Updates",
        "https://www.facebook.com/profile.php?id=123456789",
        "",
    )
    ranked = [_rank_item(artist, bad)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked, min_accept_score=-50)

    assert chosen is None
    assert selected_by == "no_safe_match"
    assert enricher._last_search_reject_reason == "profile_no_music_signal"


def test_profile_like_candidate_with_music_metadata_can_pass():
    enricher = _make_enricher()
    artist = "Aurora Beam"
    good = _cand(
        "Aurora Beam",
        "https://www.facebook.com/profile.php?id=123456789",
        "Public figure",
        secondary="DJ / Producer",
    )
    ranked = [_rank_item(artist, good)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked, min_accept_score=-50)

    assert chosen is good
    assert selected_by in {"ranked_sort", "mismatch_fallback"}


def test_exact_name_with_weak_metadata_still_allowed():
    enricher = _make_enricher()
    artist = "Exact Artist"
    good = _cand("Exact Artist", "https://www.facebook.com/exactartist", "Public figure")
    ranked = [_rank_item(artist, good)]

    chosen, selected_by = enricher._choose_ranked_candidate(artist, ranked, min_accept_score=-50)

    assert chosen is good
    assert selected_by in {"ranked_sort", "mismatch_fallback"}
