import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


def _make_worker():
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: None)
    return worker


def _ctx_for(worker, row):
    df = pd.DataFrame([row], dtype=str).fillna("")
    return worker._build_row_context(df, 0, 1, 1), df


def test_row_confidence_blocks_artist_name_only_row():
    worker = _make_worker()
    ctx, df = _ctx_for(worker, {"Artist Name": "DJ"})

    decision = worker._row_allows_heavy_enricher(df.loc[0], ctx, "facebook")

    assert decision.allowed is False
    assert decision.score < decision.threshold


def test_row_confidence_allows_explicit_website_clue():
    worker = _make_worker()
    ctx, df = _ctx_for(
        worker,
        {
            "Artist Name": "XY",
            "Spotify_Website_URL": "https://artist.test",
        },
    )

    decision = worker._row_allows_heavy_enricher(df.loc[0], ctx, "website")

    assert decision.allowed is True
    assert "website_url" in decision.reasons


def test_row_confidence_allows_explicit_facebook_url():
    worker = _make_worker()
    ctx, df = _ctx_for(
        worker,
        {
            "Artist Name": "Sun",
            "facebook_url": "https://www.facebook.com/sunmusic",
            "Facebook_URL": "https://www.facebook.com/sunmusic",
        },
    )

    decision = worker._row_allows_heavy_enricher(df.loc[0], ctx, "facebook")

    assert decision.allowed is True
    assert "explicit_facebook_url" in decision.reasons


def test_row_confidence_allows_match_score_with_multiple_links():
    worker = _make_worker()
    ctx, df = _ctx_for(
        worker,
        {
            "Artist Name": "Match Artist",
            "Match_Score": "0.72",
            "Social Link": "https://instagram.com/matchartist",
            "External Links": "https://artist.test",
            "Source URL": "https://soundcloud.com/matchartist",
        },
    )

    decision = worker._row_allows_heavy_enricher(df.loc[0], ctx, "lastfm")

    assert decision.allowed is True
    assert "strong_match_score" in decision.reasons
    assert "multiple_link_clues" in decision.reasons


def test_row_confidence_allows_borderline_some_link_clues_at_threshold():
    worker = _make_worker()
    ctx = {
        "artist": "Conan Gray",
        "signal_snapshot": {
            "spotify_domain": "",
            "seed_links_by_source": {},
            "website_candidates": (),
            "soundcloud_link": "",
            "canonical_fb_url": "",
            "source_url": "",
            "source_url_source": "",
            "match_score": 0.0,
            "signal_sources": ("external_field", "social_field"),
        },
    }
    df = pd.DataFrame([{"Artist Name": "Conan Gray"}], dtype=str).fillna("")

    decision = worker._row_allows_heavy_enricher(df.loc[0], ctx, "facebook")

    assert decision.allowed is True
    assert decision.score == 0.30
    assert decision.threshold == 0.30
    assert "some_link_clues" in decision.reasons
