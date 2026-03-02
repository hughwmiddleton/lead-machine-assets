from types import SimpleNamespace

import night_mode_fb


def _cand(name: str, url: str, category: str = "", aria: str = "", secondary: str = ""):
    return SimpleNamespace(name=name, url=url, category=category, aria_label=aria, secondary_text=secondary)


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

