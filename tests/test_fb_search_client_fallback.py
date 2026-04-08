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


@pytest.mark.parametrize(
    ("extra_signal", "expected_query"),
    [
        ("", "Test Artist"),
        ("Melbourne", "Test Artist Melbourne"),
        ("Night Drive", "Test Artist Night Drive"),
        ("Headside In Da Skiez", "Test Artist Headside In Da Skiez"),
        ("TAKE OVER", "Test Artist TAKE OVER"),
    ],
)
def test_find_best_page_url_builds_expected_query(monkeypatch, extra_signal, expected_query) -> None:
    artist_url = "https://www.facebook.com/testartist"
    driver = _FakeDriver(
        {
            artist_url: "<html><body><div>Musician/Band</div><a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>",
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    fetch_calls = []

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        fetch_calls.append((query, search_method))
        return (
            "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/testartist'>Test Artist</a></div></div>",
            "https://www.facebook.com/search/pages/?q=test",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (2.0, 1.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (True, "music_category"))

    result = client.find_best_page_url("Test Artist", extra_signal, require_strong_candidate=True)

    assert result == artist_url
    assert fetch_calls == [(expected_query, "homepage_ui")]


def test_find_best_page_url_uses_homepage_ui_as_single_entry(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/testartist"
    driver = _FakeDriver(
        {
            artist_url: "<html><body><div>Musician/Band</div><a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>",
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    search_methods = []

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        search_methods.append(search_method)
        return (
            "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/testartist'>Test Artist</a></div></div>",
            "https://www.facebook.com/search/top/?q=test",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (2.0, 1.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (True, "music_category"))

    result = client.find_best_page_url("Test Artist", require_strong_candidate=True)

    assert result == artist_url
    assert search_methods == ["homepage_ui"]


def test_find_best_page_url_does_not_rerun_homepage_when_candidates_later_reject(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/rejectedartist"
    driver = _FakeDriver(
        {
            artist_url: "<html><body><div>Musician/Band</div></body></html>",
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    search_methods = []

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        search_methods.append(search_method)
        return (
            "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/rejectedartist'>Rejected Artist</a></div></div>",
            "https://www.facebook.com/search/top/?q=test",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (2.0, 1.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (False, "slug_or_name_only_match"))

    result = client.find_best_page_url("Rejected Artist", require_strong_candidate=True)

    assert result is None
    assert search_methods == ["homepage_ui"]


def test_find_best_page_url_rejects_zero_identity_music_candidate(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/fergie"
    driver = _FakeDriver(
        {
            artist_url: "<html><body><div>Musician/Band</div><a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>",
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)
    search_methods = []

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        search_methods.append(search_method)
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{artist_url}'>Fergie</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=tallulah+argue",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("Tallulah Argue", require_strong_candidate=True)

    assert result is None
    assert driver.visited_urls == []
    assert search_methods == ["homepage_ui"]


def test_find_best_page_url_defers_identity_floor_to_postscrape_for_bounded_discovery(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/fergie"
    driver = _FakeDriver(
        {
            artist_url: (
                "<html><body><div>Musician/Band</div>"
                "<a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>"
            ),
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (2.0, 0.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{artist_url}'>Fergie</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=tallulah+argue",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url(
        "Tallulah Argue",
        require_strong_candidate=True,
        defer_identity_floor_to_postscrape=True,
    )

    assert result == artist_url
    assert driver.visited_urls == [artist_url]


def test_find_best_page_url_tries_next_ranked_candidate_after_prescrape_identity_reject(monkeypatch) -> None:
    logs = []
    rejected_url = "https://www.facebook.com/fergie"
    accepted_url = "https://www.facebook.com/tallulahargue"
    driver = _FakeDriver(
        {
            accepted_url: (
                "<html><body><div>Musician/Band</div>"
                "<a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>"
            ),
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=logs.append)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{rejected_url}'>Fergie</a><div class='subtitle'>Musician/Band</div></div>"
                f"<div class='card'><a href='{accepted_url}'>Tallulah Argue Music</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=tallulah+argue",
            False,
        )

    def _fake_score(artist_name, candidate_name, candidate_url, category):  # noqa: ANN001
        if candidate_url == rejected_url:
            return (3.0, 0.0, 1.0)
        if candidate_url == accepted_url:
            return (2.0, 0.6, 1.0)
        raise AssertionError(f"unexpected candidate: {candidate_url}")

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", _fake_score)
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (True, "music_category"))

    result = client.find_best_page_url("Tallulah Argue", require_strong_candidate=True)

    assert result == accepted_url
    assert driver.visited_urls == [accepted_url]
    assert any("Considering next plausible ranked candidate" in message for message in logs)


def test_find_best_page_url_allows_weak_metadata_candidate_with_lexical_evidence() -> None:
    candidate = cde.FbCandidate(
        name="Tallulah Argue Live",
        url="https://www.facebook.com/tallulah-argue-live",
        category="Public figure",
    )

    allowed, reason = cde._facebook_candidate_has_min_identity_evidence(
        "Tallulah Argue",
        candidate,
        name_score=0.5,
    )

    assert allowed is True
    assert reason == "name_score"


def test_facebook_candidate_identity_allows_compact_normalized_variant() -> None:
    candidate = cde.FbCandidate(
        name="Low Brain Official",
        url="https://www.facebook.com/low-brain-official",
        category="Musician/Band",
    )

    allowed, reason = cde._facebook_candidate_has_min_identity_evidence(
        "Lowbrain",
        candidate,
        name_score=0.0,
    )

    assert allowed is True
    assert reason == "compact_norm"


def test_facebook_candidate_identity_rejects_zero_evidence_profile_junk() -> None:
    candidate = cde.FbCandidate(
        name="Profile Photos",
        url="https://www.facebook.com/profile.php?id=123456789",
        category="",
    )

    allowed, reason = cde._facebook_candidate_has_min_identity_evidence(
        "Lowbrain",
        candidate,
        name_score=0.0,
    )

    assert allowed is False
    assert reason == "identity_floor"


def test_find_best_page_url_allows_compact_normalized_candidate_through_identity_floor(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/low-brain-official"
    driver = _FakeDriver(
        {
            artist_url: (
                "<html><body><div>Musician/Band</div>"
                "<a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>"
            ),
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (1.0, 0.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (True, "music_category"))

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{artist_url}'>Low Brain Official</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=lowbrain",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("Lowbrain", require_strong_candidate=True)

    assert result == artist_url
    assert driver.visited_urls == [artist_url]


def test_find_best_page_url_accepts_plausible_music_candidate_with_identity(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/tallulahargue"
    driver = _FakeDriver(
        {
            artist_url: "<html><body><div>Musician/Band</div><a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>",
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{artist_url}'>Tallulah Argue Music</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=tallulah+argue",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("Tallulah Argue", require_strong_candidate=True)

    assert result == artist_url
    assert driver.visited_urls[-1] == artist_url


def test_find_best_page_url_prescrape_candidate_walking_stays_bounded(monkeypatch) -> None:
    first_url = "https://www.facebook.com/fergie"
    second_url = "https://www.facebook.com/hotstuff"
    third_url = "https://www.facebook.com/tallulahargue"
    driver = _FakeDriver(
        {
            third_url: (
                "<html><body><div>Musician/Band</div>"
                "<a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>"
            ),
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    identity_checks = []

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{first_url}'>Fergie</a><div class='subtitle'>Musician/Band</div></div>"
                f"<div class='card'><a href='{second_url}'>Hot Stuff!!</a><div class='subtitle'>Musician/Band</div></div>"
                f"<div class='card'><a href='{third_url}'>Tallulah Argue Music</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=tallulah+argue",
            False,
        )

    def _fake_score(artist_name, candidate_name, candidate_url, category):  # noqa: ANN001
        scores = {
            first_url: (3.0, 0.0, 1.0),
            second_url: (2.5, 0.0, 1.0),
            third_url: (2.0, 0.8, 1.0),
        }
        return scores[candidate_url]

    def _fake_identity(artist_name, candidate, *, name_score=None):  # noqa: ANN001
        identity_checks.append(candidate.url)
        return (candidate.url == third_url, "name_score" if candidate.url == third_url else "identity_floor")

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", _fake_score)
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "_facebook_candidate_has_min_identity_evidence", _fake_identity)

    result = client.find_best_page_url("Tallulah Argue", require_strong_candidate=True)

    assert result is None
    assert identity_checks == [first_url, second_url]
    assert driver.visited_urls == []


def test_find_best_page_url_rejects_generic_homepage_auth_surface(monkeypatch) -> None:
    logs = []
    driver = _FakeDriver({})
    client = cde.FacebookSearchClient(driver=driver, logger=logs.append)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    search_methods = []
    homepage_html = (
        "<div role='main'>"
        "<a href='https://www.facebook.com/reg/'>Sign up</a>"
        "<a href='https://www.facebook.com/lite/'>Facebook Lite</a>"
        "<a href='https://www.facebook.com/about/'>About</a>"
        "</div>"
    )

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        search_methods.append(search_method)
        return homepage_html, "https://www.facebook.com/", False

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("Qux", require_strong_candidate=True)

    assert result is None
    assert driver.visited_urls == []
    assert search_methods == ["homepage_ui"]
    assert any("search_method=homepage_ui junk_candidates_filtered=3" in message for message in logs)
    assert any("search_method=homepage_ui failure_mode=generic_auth_surface" in message for message in logs)


def test_find_best_page_url_prefers_page_style_url_over_profile_tie(monkeypatch) -> None:
    page_url = "https://www.facebook.com/nightlightau"
    profile_url = "https://www.facebook.com/profile.php?id=123"
    driver = _FakeDriver(
        {
            page_url: "<html><body><div>Musician/Band</div><a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>",
            profile_url: "<html><body><div>Musician/Band</div><a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>",
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<a href='{profile_url}'>Nightlight AU</a>"
                f"<a href='{page_url}'>Nightlight AU</a>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=nightlight+au",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (2.0, 1.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (True, "music_category"))

    result = client.find_best_page_url("Nightlight AU", require_strong_candidate=True)

    assert result == page_url
    assert driver.visited_urls[-1] == page_url


def test_find_best_page_url_does_not_try_next_candidate_after_postscrape_reject(monkeypatch) -> None:
    rejected_url = "https://www.facebook.com/rejectedartist"
    skipped_url = "https://www.facebook.com/tallulahargue"
    driver = _FakeDriver(
        {
            rejected_url: (
                "<html><body><div>Musician/Band</div>"
                "<a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>"
            ),
            skipped_url: (
                "<html><body><div>Musician/Band</div>"
                "<a href='https://open.spotify.com/artist/test'>Spotify</a></body></html>"
            ),
        }
    )
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        return (
            (
                "<div role='main'><div aria-label='Search results'>"
                f"<div class='card'><a href='{rejected_url}'>Rejected Artist</a><div class='subtitle'>Musician/Band</div></div>"
                f"<div class='card'><a href='{skipped_url}'>Tallulah Argue Music</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=rejected+artist",
            False,
        )

    def _fake_score(artist_name, candidate_name, candidate_url, category):  # noqa: ANN001
        if candidate_url == rejected_url:
            return (3.0, 1.0, 1.0)
        if candidate_url == skipped_url:
            return (2.0, 1.0, 1.0)
        raise AssertionError(f"unexpected candidate: {candidate_url}")

    def _fake_strong(
        artist_name,
        candidate,
        page_html,
        page_category_text,
        page_text_blocks,
        outbound_links,
        allow_identity_floor_page_signal_override=False,
        logger=None,
    ):  # noqa: ANN001
        if candidate.url == rejected_url:
            return (False, "slug_or_name_only_match")
        return (True, "music_category")

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", _fake_score)
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", _fake_strong)

    result = client.find_best_page_url("Rejected Artist", require_strong_candidate=True)

    assert result is None
    assert driver.visited_urls == [rejected_url]
