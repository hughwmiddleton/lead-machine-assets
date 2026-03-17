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
    assert fetch_calls == [(expected_query, "direct_route")]


def test_find_best_page_url_falls_back_to_homepage_after_direct_surface_miss(monkeypatch) -> None:
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
        if search_method == "direct_route":
            return "<html><body>direct-miss</body></html>", "https://www.facebook.com/search/pages/?q=test", False
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
    assert search_methods == ["direct_route", "homepage_ui"]


def test_find_best_page_url_does_not_fallback_when_direct_candidates_later_reject(monkeypatch) -> None:
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
        if search_method != "direct_route":
            raise AssertionError("homepage fallback should not run when direct-route produced candidates")
        return (
            "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/rejectedartist'>Rejected Artist</a></div></div>",
            "https://www.facebook.com/search/pages/?q=test",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(cde, "score_fb_candidate", lambda *args, **kwargs: (2.0, 1.0, 1.0))
    monkeypatch.setattr(cde, "is_music_page", lambda *args, **kwargs: True)
    monkeypatch.setattr(cde, "classify_corporate_signals", lambda *args, **kwargs: SimpleNamespace(has_hard=False, has_artist=True))
    monkeypatch.setattr(cde, "_facebook_candidate_is_strong", lambda *args, **kwargs: (False, "slug_or_name_only_match"))

    result = client.find_best_page_url("Rejected Artist", require_strong_candidate=True)

    assert result is None
    assert search_methods == ["direct_route"]


def test_find_best_page_url_rejects_zero_identity_music_candidate(monkeypatch) -> None:
    artist_url = "https://www.facebook.com/fergie"
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
                f"<div class='card'><a href='{artist_url}'>Fergie</a><div class='subtitle'>Musician/Band</div></div>"
                "</div></div>"
            ),
            "https://www.facebook.com/search/pages/?q=tallulah+argue",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("Tallulah Argue", require_strong_candidate=True)

    assert result is None
    assert driver.visited_urls[-1] == artist_url


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


def test_find_best_page_url_rejects_generic_homepage_auth_surface(monkeypatch) -> None:
    logs = []
    driver = _FakeDriver({})
    client = cde.FacebookSearchClient(driver=driver, logger=logs.append)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    search_methods = []
    direct_html = "<html><body>direct-miss</body></html>"
    homepage_html = (
        "<div role='main'>"
        "<a href='https://www.facebook.com/reg/'>Sign up</a>"
        "<a href='https://www.facebook.com/lite/'>Facebook Lite</a>"
        "<a href='https://www.facebook.com/about/'>About</a>"
        "</div>"
    )

    def _fake_fetch(query, *, search_method):  # noqa: ANN001
        search_methods.append(search_method)
        if search_method == "direct_route":
            return direct_html, "https://www.facebook.com/search/pages/?q=qux", False
        return homepage_html, "https://www.facebook.com/", False

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("Qux", require_strong_candidate=True)

    assert result is None
    assert driver.visited_urls == []
    assert search_methods == ["direct_route", "homepage_ui"]
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
