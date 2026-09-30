"""
Regression tests for Ticket 6: Fix Facebook fallback candidate score inflation.

Root cause: fallback_candidates and generic_candidates buckets in
_discover_facebook_url_bounded artificially raised weak candidate scores to 1.0
via max(final_score, 1.0), allowing clearly incompatible pages (e.g.
The Betoota Advocate for The Pretty Things) to bypass MIN_FINAL_SCORE.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


class _FakeDriver:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.current_url = "about:blank"
        self.page_source = ""
        self.visited_urls = []

    def get(self, url):
        self.visited_urls.append(url)
        self.current_url = url
        self.page_source = self.pages.get(url, self.page_source)

    def find_element(self, by=None, value=None):  # noqa: ANN001
        if value == "body":
            return object()
        raise Exception(f"unexpected locator: {by} {value}")


def _make_client(driver_pages):
    driver = _FakeDriver(driver_pages)
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    return client


def _betoota_html():
    """Simulated Betoota Advocate page with music-y tokens in category."""
    return (
        "<html><body>"
        "<div>The Betoota Advocate</div>"
        "<div>Musician/Band</div>"
        "<a href='https://open.spotify.com/artist/betoota'>Spotify</a>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# 1. The Pretty Things → The Betoota Advocate must reject
# ---------------------------------------------------------------------------

def test_pretty_things_betoota_advocate_rejected(monkeypatch) -> None:
    """
    A candidate with name_score≈0.20 and final_score<1.0 must be rejected
    even if the page carries music category signals.
    """
    betoota_url = "https://www.facebook.com/betootaadvocate"
    client = _make_client({betoota_url: _betoota_html()})
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<a href='{betoota_url}'>The Betoota Advocate</a>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/top/?q=pretty",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    # Betoota Advocate scores: overlap=1/5=0.20, no exact bonus, cat_boost=0.0
    monkeypatch.setattr(
        cde, "score_fb_candidate",
        lambda artist, name, url, cat: (0.20, 0.20, 0.0),
    )
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cde, "classify_corporate_signals",
        lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True),
    )
    monkeypatch.setattr(
        cde, "_facebook_candidate_is_strong",
        lambda *args, **kwargs: (True, "music_category"),
    )

    result = client.find_best_page_url("The Pretty Things", require_strong_candidate=True)
    assert result is None


# ---------------------------------------------------------------------------
# 2. Weak candidate cannot be accepted solely due to page_music=True
# ---------------------------------------------------------------------------

def test_weak_candidate_rejected_despite_music_signals(monkeypatch) -> None:
    """
    name_score≈0.20 and final_score<1.0 must stay rejected regardless of
    music/page category bonuses.
    """
    weak_url = "https://www.facebook.com/weakpage"
    client = _make_client({weak_url: _betoota_html()})
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<a href='{weak_url}'>Weak Page</a>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/top/?q=weak",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    # Music category gives a 0.5 bonus, but base identity is still 0.20
    monkeypatch.setattr(
        cde, "score_fb_candidate",
        lambda artist, name, url, cat: (0.20, 0.20, 0.0),
    )
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cde, "classify_corporate_signals",
        lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True),
    )
    monkeypatch.setattr(
        cde, "_facebook_candidate_is_strong",
        lambda *args, **kwargs: (True, "music_category"),
    )

    result = client.find_best_page_url("Some Artist", require_strong_candidate=True)
    assert result is None


# ---------------------------------------------------------------------------
# 3. Strong normalized fallback candidate still accepts
# ---------------------------------------------------------------------------

def test_strong_fallback_candidate_still_accepted(monkeypatch) -> None:
    """
    A close slug/name variant with a genuinely high identity score must
    continue to be accepted after the inflation removal.
    """
    strong_url = "https://www.facebook.com/theprettythingsmusic"
    client = _make_client(
        {strong_url: "<html><body><div>Musician/Band</div></body></html>"}
    )
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<a href='{strong_url}'>The Pretty Things Music</a>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/top/?q=pretty",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    # Exact match bonus pushes name_score to 1.5, final_score well above 1.0
    monkeypatch.setattr(
        cde, "score_fb_candidate",
        lambda artist, name, url, cat: (1.5, 1.5, 0.0),
    )
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cde, "classify_corporate_signals",
        lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True),
    )
    monkeypatch.setattr(
        cde, "_facebook_candidate_is_strong",
        lambda *args, **kwargs: (True, "music_category"),
    )

    result = client.find_best_page_url("The Pretty Things", require_strong_candidate=True)
    assert result == strong_url


# ---------------------------------------------------------------------------
# 4. Fallback score is never inflated above the computed score
# ---------------------------------------------------------------------------

def test_fallback_score_never_exceeds_computed_score(monkeypatch) -> None:
    """
    A candidate that lands in the fallback bucket must retain its true
    final_score; the bucket must not force it to 1.0.
    """
    marginal_url = "https://www.facebook.com/marginalartist"
    client = _make_client(
        {marginal_url: "<html><body><div>Musician/Band</div></body></html>"}
    )
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<a href='{marginal_url}'>Marginal Artist</a>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/top/?q=marginal",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    # final_score=0.8 is below the 1.0 threshold and must be rejected
    monkeypatch.setattr(
        cde, "score_fb_candidate",
        lambda artist, name, url, cat: (0.8, 0.8, 0.0),
    )
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cde, "classify_corporate_signals",
        lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True),
    )
    monkeypatch.setattr(
        cde, "_facebook_candidate_is_strong",
        lambda *args, **kwargs: (True, "music_category"),
    )

    result = client.find_best_page_url("Marginal Artist", require_strong_candidate=True)
    assert result is None
