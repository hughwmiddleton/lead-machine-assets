import pytest
from selenium.common.exceptions import WebDriverException

from night_mode_fb import FacebookDriverError, NightModeFacebookEnricher


class _FakeDriver:
    def __init__(self):
        self._current_url = "https://www.facebook.com/"

    @property
    def current_url(self):
        return self._current_url

    def get_cookie(self, name):
        if name == "c_user":
            return {"value": "1"}
        return None

    def get(self, url):  # pragma: no cover - defensive
        raise WebDriverException("no such window")


class _AlwaysDeadSession:
    def __init__(self):
        self.refreshed = False
        self.refresh_calls = 0
        self.driver = _FakeDriver()

    def navigate(self, url):
        raise WebDriverException("no such window: target window already closed")

    def ensure_logged_in(self):
        return self.driver

    def refresh_session(self):
        self.refresh_calls += 1
        self.refreshed = True
        self.driver = _FakeDriver()
        return self.driver

    def close(self):
        pass


def _build_enricher(monkeypatch, session, logs):
    enricher = NightModeFacebookEnricher(
        legacy_module=None,
        username="user",
        password="pass",
        logger=logs.append,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: False)
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: None)
    return enricher


def test_dead_driver_triggers_single_restart(monkeypatch):
    logs = []
    session = _AlwaysDeadSession()
    enricher = _build_enricher(monkeypatch, session, logs)

    row = {"Artist Name": "Dead Driver", "Facebook_URL": "https://facebook.com/dead"}

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") == "driver_error"
    assert session.refreshed is True
    assert session.refresh_calls == 1
    assert enricher._driver_restart_count == 1
    log_text = " ".join(logs)
    assert "[Night FB] Detected dead FB driver" in log_text
    assert "Restart limit reached" not in log_text


def test_dead_driver_respects_restart_limit(monkeypatch):
    logs = []
    session = _AlwaysDeadSession()
    enricher = _build_enricher(monkeypatch, session, logs)
    enricher._driver_restart_limit = 2

    row = {"Artist Name": "Dead Driver", "Facebook_URL": "https://facebook.com/dead"}

    # First two attempts restart successfully but fail the row.
    for _ in range(2):
        result = enricher.enrich_row_with_facebook_night(row)
        assert result.get("FB_Status") == "driver_error"

    assert enricher._driver_restart_count == 2
    assert session.refresh_calls == 2

    # Third attempt should hit the restart ceiling and mark the session failed.
    result_limit = enricher.enrich_row_with_facebook_night(row)
    assert result_limit.get("FB_Status") == "driver_error"

    assert enricher._session_failed is True
    assert enricher._skip_fb_due_to_session_failure is True
    log_text = " ".join(logs)
    assert "Restart limit reached. Skipping remaining FB rows." in log_text

    # Subsequent rows short-circuit without raising.
    result = enricher.enrich_row_with_facebook_night(row)
    assert result.get("FB_Status") == "driver_error"
    assert result.get("FB_Reason") == "driver_restart_limit"
