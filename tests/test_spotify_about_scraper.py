import requests

import spotify_about_scraper as sas
from bs4 import BeautifulSoup
from pathlib import Path


class _DummySession(requests.Session):
    pass


def test_fetch_artist_html_returns_empty_on_non_200_requests(monkeypatch):
    # Simulate requests path returning non-200; should yield empty string.
    monkeypatch.setattr(
        sas,
        "fetch_html",
        lambda url, **kwargs: {"mode_used": "requests", "status": 500, "html": "<html>blocked</html>"},
    )
    session = _DummySession()
    html, meta = sas._fetch_artist_html(session, "https://spotify.com/artist/123", logger=None)
    assert html == ""
    assert meta.get("reason") == "http_500"


def test_extract_socials_from_nextdata_fixture():
    fixture = Path("tests/fixtures/spotify_about_nextdata.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(fixture, "html.parser")
    next_data = sas._extract_next_data_from_soup(soup)
    profile = sas._extract_profile_blob(next_data)
    socials = sas._extract_socials_from_profile(profile)
    assert socials["website"] == "https://foo.com"
    assert socials["instagram"].startswith("https://instagram.com/")
    assert socials["facebook"].startswith("https://facebook.com/")
    assert socials["twitter"].startswith("https://twitter.com/")


def test_cookie_wall_fixture_returns_cookie_reason(monkeypatch):
    fixture = Path("tests/fixtures/spotify_about_cookie_wall.html").read_text(encoding="utf-8")
    session = _DummySession()

    def fake_fetch(url, **kwargs):
        return {
            "mode_used": "requests",
            "status": 200,
            "html": fixture,
            "reason": "ok",
            "final_url": url,
        }

    monkeypatch.setattr(sas, "fetch_html", fake_fetch)
    html, meta = sas._fetch_artist_html(session, "https://open.spotify.com/artist/xyz", logger=None)
    assert html == fixture

    payload = sas._fetch_about_payload(session, "xyz", logger=None)
    assert payload.get("reason") == "cookie_wall_or_bot"
    assert payload.get("socials") == {"instagram": "", "facebook": "", "twitter": "", "website": ""}


def test_cookie_wall_with_spotify_generic_socials_is_filtered(monkeypatch):
    fixture = Path("tests/fixtures/spotify_about_cookie_wall_socials.html").read_text(encoding="utf-8")
    session = _DummySession()

    def fake_fetch(url, **kwargs):
        return {
            "mode_used": "requests",
            "status": 200,
            "html": fixture,
            "reason": "ok",
            "final_url": url,
        }

    monkeypatch.setattr(sas, "fetch_html", fake_fetch)
    html, meta = sas._fetch_artist_html(session, "https://open.spotify.com/artist/xyz", logger=None)
    assert html == fixture

    payload = sas._fetch_about_payload(session, "xyz", logger=None)
    assert payload.get("reason") in {"cookie_wall_or_bot", "next_data_missing"}
    assert payload.get("socials") == {"instagram": "", "facebook": "", "twitter": "", "website": ""}


def test_cookie_wall_with_spotify_substring_handle_does_not_matter(monkeypatch):
    fixture = Path("tests/fixtures/spotify_about_cookie_wall_spotify_substring.html").read_text(encoding="utf-8")
    session = _DummySession()

    def fake_fetch(url, **kwargs):
        return {
            "mode_used": "requests",
            "status": 200,
            "html": fixture,
            "reason": "ok",
            "final_url": url,
        }

    monkeypatch.setattr(sas, "fetch_html", fake_fetch)
    payload = sas._fetch_about_payload(session, "xyz", logger=None)
    assert payload.get("reason") in {"cookie_wall_or_bot", "next_data_missing"}
    assert payload.get("socials") == {"instagram": "", "facebook": "", "twitter": "", "website": ""}


def test_nextdata_with_spotify_website_is_filtered(monkeypatch):
    fixture = Path("tests/fixtures/spotify_about_nextdata_spotify_site.html").read_text(encoding="utf-8")
    session = _DummySession()

    def fake_fetch(url, **kwargs):
        return {
            "mode_used": "requests",
            "status": 200,
            "html": fixture,
            "reason": "ok",
            "final_url": url,
        }

    monkeypatch.setattr(sas, "fetch_html", fake_fetch)
    payload = sas._fetch_about_payload(session, "xyz", logger=None)
    assert payload.get("socials") == {"instagram": "", "facebook": "", "twitter": "", "website": ""}
    assert payload.get("reason") == "generic_spotify_socials_filtered"
def test_consent_handler_marks_flags_once():
    calls = {"wait": 0, "click": 0}

    class _FakeLocator:
        def wait_for(self, state=None, timeout=None):
            calls["wait"] += 1

        def click(self, timeout=None):
            calls["click"] += 1

    class _FakePage:
        def locator(self, sel):
            return _FakeLocator()

        def wait_for_timeout(self, ms):
            pass

    meta = {}
    sas._spotify_consent_handler(_FakePage(), meta)
    assert meta["consent_found"] is True
    assert meta["consent_clicked"] is True
    assert calls["wait"] == 1
    assert calls["click"] == 1
