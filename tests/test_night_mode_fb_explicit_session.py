import pytest

import night_mode_fb as nmfb
from selenium.common.exceptions import TimeoutException


class _DummyLegacy:
    def setup_facebook_driver(self):
        return object()


@pytest.fixture
def enricher(monkeypatch):
    helper = nmfb.NightModeFacebookEnricher(
        _DummyLegacy(), username="user", password="pass", logger=None, use_shared_session=False
    )
    # Avoid real network/driver work.
    monkeypatch.setattr(helper, "_ensure_driver_alive", lambda session: session)
    return helper


def test_explicit_fb_url_prefers_authenticated_session(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["allow_anon"] = allow_anon
        observed["fb_url"] = fb_url
        return (
            nmfb.NightModeFacebookResult(
                email="fb@example.com",
                email_all="fb@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["fb@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    def _fail_anon(self):
        raise AssertionError("anon not expected")

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_get_anon_driver", _fail_anon)

    row = {
        "Artist Name": "Explicit FB",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/explicit.session.test",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed.get("allow_anon") is False, "Should not fall back to anonymous when session is available"
    assert "Using explicit FB URLs with authenticated session" in " ".join(logs)
    assert any('mode="session"' in msg for msg in logs), "PASS A log should report session mode"
    assert result.get("FB_Status", "").startswith("pass_a")
    assert "fb@example.com" in result.get("Email_All", "")


def test_explicit_fb_url_falls_back_to_legacy_anon(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["allow_anon"] = allow_anon
        return None  # force fallback handling

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    # Make anon driver available so legacy probe path stays intact if reached elsewhere.
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())

    row = {
        "Artist Name": "Explicit FB",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/explicit.anon.test",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed.get("allow_anon") is True, "Legacy anon probe should remain enabled when session missing"
    assert "Falling back to legacy anon probe for explicit FB URLs" in " ".join(logs)
    assert any('mode="legacy_anon_probe"' in msg for msg in logs), "PASS A log should report legacy anon mode"
    assert result.get("FB_Status", "")  # status still set/returned without crashing


def test_rows_without_explicit_fb_urls_unchanged(monkeypatch, enricher):
    # Ensure explicit-URL branch is untouched when no FB URLs exist.
    def _fail_session():
        raise AssertionError("should not be called")

    monkeypatch.setattr(enricher, "_has_authenticated_session", _fail_session)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)

    def _fail_scrape(self, *args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _fail_scrape)

    row = {"Artist Name": "No FB", "Email": "", "Email_All": "", "Social Link": ""}
    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status", "").startswith("pass_a_skipped") or result.get("FB_Status", "") == "ok" or not result.get("FB_Status", "")


class _TimeoutDriver:
    def __init__(self, html: str = ""):
        self._html = html
        self.set_timeout_called = None
        self.stop_called = False
        self.get_calls = 0

    def set_page_load_timeout(self, seconds):
        self.set_timeout_called = seconds

    def get(self, url):  # noqa: ANN001
        self.get_calls += 1
        raise TimeoutException("timed out")

    def execute_script(self, script):
        if script == "window.stop();":
            self.stop_called = True

    @property
    def page_source(self):
        return self._html

    @property
    def current_url(self):
        return "https://www.facebook.com/timeout"


def test_explicit_fb_timeout_without_html(monkeypatch, enricher):
    driver = _TimeoutDriver(html="")
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: driver)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)

    row = {
        "Artist Name": "Timeout Artist",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/timeout.artist",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") == "pass_a_timeout"
    assert result.get("FB_Reason") == "timeout"
    assert driver.set_timeout_called == 20
    assert driver.stop_called is True


def test_explicit_fb_timeout_with_salvage_html(monkeypatch, enricher):
    html = '<html><body><a href="mailto:artist@test.com">email</a></body></html>'
    driver = _TimeoutDriver(html=html)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: driver)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    # Ensure email override allows keeping emails even if music signals are weak in this synthetic page.
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    row = {
        "Artist Name": "Timeout Artist",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/timeout.artist",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") != "pass_a_timeout"
    assert driver.stop_called is True
    # Salvaged HTML should still be parsed and produce the email.
    assert "artist@test.com" in (result.get("Email_All") or "") or result.get("Email") == "artist@test.com"
