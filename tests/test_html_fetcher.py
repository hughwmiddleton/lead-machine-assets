import pytest

import html_fetcher


class _DummyResp:
    def __init__(self, status: int, text: str = "", url: str = "https://example.com") -> None:
        self.status_code = status
        self.text = text
        self.url = url


class _DummySession:
    def __init__(self, responder):
        self._responder = responder

    def get(self, url, timeout=None, allow_redirects=True):
        return self._responder(url, timeout, allow_redirects)


@pytest.fixture(autouse=True)
def _reset_browsers():
    html_fetcher._JOB_BROWSERS.clear()
    yield
    html_fetcher._JOB_BROWSERS.clear()


def test_status_403_triggers_playwright(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(403, "blocked", url))
    monkeypatch.setattr(
        html_fetcher,
        "_playwright_fetch",
        lambda url, job_id, timeout_s: {"html": "<html>pw</html>", "final_url": url + "/pw"},
    )
    result = html_fetcher.fetch_html("https://example.com", session=session, directory="test")
    assert result["mode_used"] == "playwright"
    assert result["reason"] == "status_403"
    assert "<html>pw" in result["html"]


def test_soft_block_triggers_playwright(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(200, "Please verify you are human", url))
    monkeypatch.setattr(
        html_fetcher,
        "_playwright_fetch",
        lambda url, job_id, timeout_s: {"html": "<html>ok</html>", "final_url": url},
    )
    result = html_fetcher.fetch_html("https://example.com", session=session, directory="test")
    assert result["mode_used"] == "playwright"
    assert result["reason"] == "soft_block"


def test_missing_selector_triggers_playwright(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(200, "<html><body></body></html>", url))
    monkeypatch.setattr(
        html_fetcher,
        "_playwright_fetch",
        lambda url, job_id, timeout_s: {"html": "<html><div class='hit'></div></html>", "final_url": url},
    )
    result = html_fetcher.fetch_html(
        "https://example.com", session=session, directory="test", required_selectors=[".hit"]
    )
    assert result["mode_used"] == "playwright"
    assert result["reason"] == "missing_selectors"


def test_page_budget_enforced(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(403, "blocked", url))
    monkeypatch.setattr(html_fetcher, "_PW_MAX_PAGES", 1)

    calls = {"n": 0}

    def fake_pw_fetch(url, job_id, timeout_s):
        if calls["n"] >= html_fetcher._PW_MAX_PAGES:
            raise html_fetcher.PlaywrightUnavailable("page_budget_exhausted")
        calls["n"] += 1
        return {"html": "<html>pw</html>", "final_url": url}

    monkeypatch.setattr(html_fetcher, "_playwright_fetch", fake_pw_fetch)

    first = html_fetcher.fetch_html("https://example.com/a", session=session, job_id="job1")
    second = html_fetcher.fetch_html("https://example.com/b", session=session, job_id="job1")

    assert first["mode_used"] == "playwright"
    assert second["reason"] == "playwright_error"
    assert calls["n"] == 1


def test_playwright_disabled(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(403, "blocked", url))
    monkeypatch.setattr(html_fetcher, "_PW_ENABLED", False)
    result = html_fetcher.fetch_html("https://example.com", session=session, directory="test")
    assert result["mode_used"] == "requests"
    assert result["reason"] == "status_403"
