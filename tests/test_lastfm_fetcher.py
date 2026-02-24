import time
from types import SimpleNamespace
from typing import List

import requests

import cross_directory_enricher as cde


class _DummyResp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def _build_worker():
    # Minimal worker instance; we never start the QThread.
    worker = cde.CrossDirectoryEnricherWorker(
        seed_csv_path="",
        output_csv_path="",
        enable_live_search=False,
        max_live_searches=0,
    )
    worker.log_message = SimpleNamespace(emit=lambda msg: None)
    return worker


def test_lastfm_406_retries_then_succeeds(monkeypatch):
    worker = _build_worker()
    statuses: List[int] = [406, 406, 200]

    def fake_get(url, timeout=None, headers=None):
        status = statuses.pop(0)
        return _DummyResp(status, "html-ok")

    sleeps = []

    monkeypatch.setattr(worker.session, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    html = worker._fetch_url("https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=3)

    assert html == "html-ok"
    assert worker._last_http_status == 200
    # Two 406s should trigger two backoffs.
    assert len(sleeps) == 2


def test_lastfm_breaker_trips_after_consecutive_406(monkeypatch):
    worker = _build_worker()
    monkeypatch.setattr(cde, "LF_BREAKER_CONSEC_406", 2)
    monkeypatch.setattr(cde, "LF_BREAKER_COOLDOWN_S", 1)
    statuses: List[int] = [406, 406]

    def fake_get(url, timeout=None, headers=None):
        status = statuses.pop(0)
        return _DummyResp(status)

    sleeps = []
    now = [1000.0]

    def fake_time():
        return now[0]

    def fake_sleep(s):
        sleeps.append(s)
        now[0] += s

    monkeypatch.setattr(worker.session, "get", fake_get)
    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    html = worker._fetch_url("https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=2)
    assert html is None
    assert worker._lf_breaker_until > fake_time()

    # Next call should short-circuit due to breaker without hitting network.
    called = {"count": 0}

    def fail_get(*args, **kwargs):
        called["count"] += 1
        return _DummyResp(200, "should not be called")

    monkeypatch.setattr(worker.session, "get", fail_get)
    html2 = worker._fetch_url("https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=1)
    assert html2 is None
    assert called["count"] == 0

