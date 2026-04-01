import pytest
from types import SimpleNamespace

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


class _DummyPage:
    def __init__(self, html: str, url: str = "https://example.com/rendered") -> None:
        self.html = html
        self.url = url
        self.calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url, wait_until, timeout))

    def content(self):
        self.calls.append(("content",))
        return self.html

    def close(self):
        self.calls.append(("close",))


class _DummyContext:
    def __init__(self, page: _DummyPage) -> None:
        self._page = page

    def new_page(self):
        return self._page


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
        lambda url, job_id, timeout_s, browser_ready_wait=None: {
            "html": "<html>pw</html>",
            "final_url": url + "/pw",
        },
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
        lambda url, job_id, timeout_s, browser_ready_wait=None: {"html": "<html>ok</html>", "final_url": url},
    )
    result = html_fetcher.fetch_html("https://example.com", session=session, directory="test")
    assert result["mode_used"] == "playwright"
    assert result["reason"] == "soft_block"


def test_missing_selector_triggers_playwright(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(200, "<html><body></body></html>", url))
    monkeypatch.setattr(
        html_fetcher,
        "_playwright_fetch",
        lambda url, job_id, timeout_s, browser_ready_wait=None: {
            "html": "<html><div class='hit'></div></html>",
            "final_url": url,
        },
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

    def fake_pw_fetch(url, job_id, timeout_s, browser_ready_wait=None):
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


def test_playwright_status_normalized_on_success(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(406, "blocked", url))
    monkeypatch.setattr(
        html_fetcher,
        "_playwright_fetch",
        lambda url, job_id, timeout_s, browser_ready_wait=None: {
            "html": "<html>ok</html>",
            "final_url": url + "/pw",
        },
    )

    result = html_fetcher.fetch_html("https://example.com", session=session, directory="test")

    assert result["mode_used"] == "playwright"
    assert result["html"] == "<html>ok</html>"
    assert result["status"] == 200


def test_playwright_status_preserved_when_empty(monkeypatch):
    session = _DummySession(lambda url, *_: _DummyResp(406, "blocked", url))
    monkeypatch.setattr(
        html_fetcher,
        "_playwright_fetch",
        lambda url, job_id, timeout_s, browser_ready_wait=None: {"html": "", "final_url": url},
    )

    result = html_fetcher.fetch_html("https://example.com", session=session, directory="test")

    assert result["mode_used"] == "playwright"
    assert result["html"] == ""
    assert result["status"] == 406


def test_playwright_fetch_runs_ready_wait_before_content_capture(monkeypatch):
    page = _DummyPage("<html><body>shell</body></html>")
    monkeypatch.setattr(
        html_fetcher,
        "_ensure_context",
        lambda job_id: SimpleNamespace(context=_DummyContext(page), pages_used=0),
    )

    def ready_wait(target_page, timeout_s):
        target_page.calls.append(("ready_wait", timeout_s))
        target_page.html = "<html><head><meta property='og:description' content='Ready'></head></html>"

    result = html_fetcher._playwright_fetch(
        "https://www.instagram.com/artist/",
        job_id=None,
        timeout_s=20,
        browser_ready_wait=ready_wait,
    )

    assert result["html"] == "<html><head><meta property='og:description' content='Ready'></head></html>"
    assert [name for name, *_ in page.calls] == ["goto", "ready_wait", "content", "close"]


def test_playwright_fetch_ready_wait_timeout_returns_best_effort_html(monkeypatch):
    page = _DummyPage("<html><body>shell</body></html>")
    monkeypatch.setattr(
        html_fetcher,
        "_ensure_context",
        lambda job_id: SimpleNamespace(context=_DummyContext(page), pages_used=0),
    )

    def ready_wait(target_page, timeout_s):
        target_page.calls.append(("ready_wait", timeout_s))
        raise TimeoutError("not ready yet")

    result = html_fetcher._playwright_fetch(
        "https://www.instagram.com/artist/",
        job_id=None,
        timeout_s=20,
        browser_ready_wait=ready_wait,
    )

    assert result["html"] == "<html><body>shell</body></html>"
    assert [name for name, *_ in page.calls] == ["goto", "ready_wait", "content", "close"]


def test_playwright_fetch_without_ready_wait_keeps_default_capture_path(monkeypatch):
    page = _DummyPage("<html><body>plain</body></html>")
    monkeypatch.setattr(
        html_fetcher,
        "_ensure_context",
        lambda job_id: SimpleNamespace(context=_DummyContext(page), pages_used=0),
    )

    result = html_fetcher._playwright_fetch("https://example.com", job_id=None, timeout_s=20)

    assert result["html"] == "<html><body>plain</body></html>"
    assert [name for name, *_ in page.calls] == ["goto", "content", "close"]
