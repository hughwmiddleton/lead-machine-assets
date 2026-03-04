import requests
import json

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
    next_data = sas._extract_next_data(fixture)
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


def test_document_html_preferred_over_page_content(monkeypatch):
    doc_html = Path("tests/fixtures/spotify_about_document_has_nextdata.html").read_text(encoding="utf-8")
    shell_html = Path("tests/fixtures/spotify_about_document_no_nextdata_shell.html").read_text(encoding="utf-8")
    session = _DummySession()

    def fake_fetch_artist(session_arg, url, logger=None):
        return shell_html, {
            "mode": "playwright",
            "reason": "status_403",
            "final_url": url,
            "document_html": doc_html,
            "consent_clicked": True,
            "consent_found": True,
            "next_data_found": False,
            "next_data_waited": True,
            "next_data_retry": True,
        }

    monkeypatch.setattr(sas, "_fetch_artist_html", fake_fetch_artist)
    payload = sas._fetch_about_payload(session, "artist123", logger=None)
    socials = payload.get("socials") or {}
    assert socials["website"] == "https://artist.com"
    assert socials["instagram"].endswith("/artist")


def test_fallback_evaluate_json_used(monkeypatch):
    eval_blob = {
        "props": {"pageProps": {"artistProfile": {"externalLinks": {"items": [{"name": "website", "url": "https://eval.com"}]}}}}
    }
    session = _DummySession()

    def fake_fetch_artist(session_arg, url, logger=None):
        return "", {
            "mode": "playwright",
            "reason": "status_403",
            "final_url": url,
            "document_html": "",
            "evaluated_next_data_json": json.dumps(eval_blob),
            "consent_clicked": True,
            "consent_found": True,
            "next_data_found": False,
            "next_data_waited": True,
            "next_data_retry": True,
        }

    monkeypatch.setattr(sas, "_fetch_artist_html", fake_fetch_artist)
    payload = sas._fetch_about_payload(session, "artist123", logger=None)
    assert payload.get("socials", {}).get("website") == "https://eval.com"


def test_reason_next_data_missing_after_consent(monkeypatch):
    shell_html = Path("tests/fixtures/spotify_about_document_no_nextdata_shell.html").read_text(encoding="utf-8")
    session = _DummySession()

    def fake_fetch_artist(session_arg, url, logger=None):
        return shell_html, {
            "mode": "playwright",
            "reason": "status_403",
            "final_url": url,
            "document_html": shell_html,
            "consent_clicked": True,
            "consent_found": True,
            "next_data_found": False,
            "next_data_waited": True,
            "next_data_retry": True,
        }

    monkeypatch.setattr(sas, "_fetch_artist_html", fake_fetch_artist)
    payload = sas._fetch_about_payload(session, "artist123", logger=None)
    assert payload.get("reason") == "next_data_missing_after_consent"
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


def test_consent_handler_checks_iframes(monkeypatch):
    class _FakeLocator:
        def __init__(self, calls):
            self.calls = calls

        def wait_for(self, state=None, timeout=None):
            self.calls["wait"] += 1

        def click(self, timeout=None):
            self.calls["click"] += 1

    class _FailPage:
        def locator(self, sel):
            raise Exception("page locator fails to force iframe path")

        @property
        def frames(self):
            return [self._frame]

        def __init__(self, frame):
            self._frame = frame

    calls = {"wait": 0, "click": 0, "selectors": []}

    class _Frame:
        def locator(self, sel):
            calls["selectors"].append(sel)
            return _FakeLocator(calls)

    meta = {}
    sas._spotify_consent_handler(_FailPage(_Frame()), meta)
    assert meta["consent_found"] is True
    assert meta["consent_clicked"] is True
    assert calls["wait"] >= 1
    assert calls["click"] == 1


def test_spotify_page_handler_waits_and_retries(monkeypatch):
    calls = {"wait": 0, "reload": 0, "screenshot": 0}

    def fake_wait(page, meta, timeout_ms):
        calls["wait"] += 1
        return False

    def fake_screenshot(page, artist_id):
        calls["screenshot"] += 1

    class _Page:
        def reload(self, wait_until=None):
            calls["reload"] += 1

    meta = {"consent_found": False, "consent_clicked": False}
    monkeypatch.setattr(sas, "_spotify_wait_for_next_data", fake_wait)
    monkeypatch.setattr(sas, "_spotify_maybe_screenshot", fake_screenshot)

    handler = sas._spotify_page_handler("artist123", meta)
    handler(_Page())

    assert calls["wait"] == 2  # initial + retry
    assert calls["reload"] == 1
    assert calls["screenshot"] == 1
    assert meta.get("next_data_retry") is True


def test_spotify_page_handler_stops_after_success(monkeypatch):
    calls = {"wait": 0, "reload": 0, "screenshot": 0}

    def fake_wait(page, meta, timeout_ms):
        calls["wait"] += 1
        return True

    def fake_screenshot(page, artist_id):
        calls["screenshot"] += 1

    class _Page:
        def reload(self, wait_until=None):
            calls["reload"] += 1

    meta = {"consent_found": False, "consent_clicked": False}
    monkeypatch.setattr(sas, "_spotify_wait_for_next_data", fake_wait)
    monkeypatch.setattr(sas, "_spotify_maybe_screenshot", fake_screenshot)

    handler = sas._spotify_page_handler("artist123", meta)
    handler(_Page())

    assert calls["wait"] == 1
    assert calls["reload"] == 0
    assert calls["screenshot"] == 0
