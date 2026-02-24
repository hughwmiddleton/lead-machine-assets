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


def test_lastfm_soft_cooldown_after_consecutive_406(monkeypatch):
    worker = _build_worker()
    monkeypatch.setattr(cde, "LF_COOLDOWN_CONSEC_406", 2)
    monkeypatch.setattr(cde, "LF_COOLDOWN_MIN_S", 10)
    monkeypatch.setattr(cde, "LF_COOLDOWN_MAX_S", 60)
    statuses: List[int] = [406, 406]

    def fake_get(url, timeout=None, headers=None):
        status = statuses.pop(0)
        return _DummyResp(status)

    logs: List[str] = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))

    now = [1000.0]

    def fake_mono():
        return now[0]

    def fake_time():
        return now[0]

    def fake_sleep(s):
        now[0] += s

    monkeypatch.setattr(worker.session, "get", fake_get)
    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "monotonic", fake_mono)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    html = worker._fetch_url("https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=2)
    assert html is None
    assert worker._lf_cooldown_until > fake_mono()
    assert any("Entering soft cooldown" in msg for msg in logs)
    assert all("cooling down for 600s" not in msg for msg in logs)

    called = {"count": 0}

    def fail_get(*args, **kwargs):
        called["count"] += 1
        return _DummyResp(200, "should not be called")

    monkeypatch.setattr(worker.session, "get", fail_get)
    html2 = worker._fetch_url("https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=1)
    assert html2 is None
    assert called["count"] == 0


def test_lastfm_track_title_sanitization():
    dirty = " ...And the Dog Followed Me | Barbershop Renaissance / Night—Shift (Deluxe) [Live]"
    cleaned = cde._sanitize_lastfm_track_title(dirty)
    assert cleaned == "And the Dog Followed Me"
    assert len(cleaned) <= 60


def test_lastfm_sanitized_empty_uses_artist_only(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "...", "artist": "Sample Artist"}
    urls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        urls.append((url, label, max_attempts))
        # Return a simple search result for the fallback query.
        worker._last_http_status = 200
        worker._last_fetch_ok = True
        return "<a href='/music/sample-artist'>Sample Artist</a>"

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Sample Artist")

    assert payload is not None
    # Only artist-only fallback should be called (no track query).
    assert len(urls) == 1
    assert "search?q=Sample+Artist&type=artist" in urls[0][0]
    assert urls[0][1] == "Last.fm search (fallback)"
    assert urls[0][2] == 1


def test_lastfm_primary_406_fast_fallback(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist X"}
    calls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        calls.append((label, max_attempts))
        if label == "Last.fm search":
            worker._last_http_status = 406
            worker._last_fetch_ok = False
            return None
        if label == "Last.fm search (fallback)":
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            return "<a href='/music/artist-x'>Artist X</a>"
        return None

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist X")

    assert payload is not None
    # Primary called once with max_attempts=1, fallback invoked next.
    assert calls[0] == ("Last.fm search", 1)
    assert calls[1] == ("Last.fm search (fallback)", 1)
    assert len(calls) == 2


def test_lastfm_fallback_406_does_not_retry(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Track", "artist": "Artist Y"}
    calls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        calls.append((label, max_attempts))
        if label == "Last.fm search":
            worker._last_http_status = 406
            worker._last_fetch_ok = False
            return None
        if label == "Last.fm search (fallback)":
            worker._last_http_status = 406
            worker._last_fetch_ok = False
            return None
        return None

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Y")

    assert payload is None
    assert calls == [("Last.fm search", 1), ("Last.fm search (fallback)", 1)]


def test_lastfm_sanitizer_numeric_heavy_skips_track(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "2026/01/30", "artist": "Artist Z"}
    calls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        calls.append((url, label, max_attempts))
        worker._last_http_status = 200
        worker._last_fetch_ok = True
        return "<a href='/music/artist-z'>Artist Z</a>"

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Z")

    assert payload is not None
    assert len(calls) == 1
    assert calls[0][1] == "Last.fm search (fallback)"
    assert calls[0][2] == 1


def test_lastfm_sanitizer_empty_skips_primary_and_goes_fallback_only(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "12345", "artist": "Artist Z"}
    calls = []
    logs: List[str] = []

    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        calls.append((label, max_attempts))
        worker._last_http_status = 200
        worker._last_fetch_ok = True
        return "<a href='/music/artist-z'>Artist Z</a>"

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Z")

    assert payload is not None
    assert calls == [("Last.fm search (fallback)", 1)]
    assert any("Skipping track query (sanitized empty); using artist-only." in msg for msg in logs)


def test_lastfm_no_quotes_variant_on_low_confidence(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist Q"}
    calls: List[tuple] = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        calls.append((label, max_attempts))
        if label == "Last.fm search":
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            # Mismatch name to keep confidence low (< MIN_LF_CONFIDENCE)
            return "<a href='/music/not-artist'>Not Artist</a>"
        if label == "Last.fm search (no-quotes)":
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            return "<a href='/music/artist-q'>Artist Q</a>"
        if label == "Last.fm search (fallback)":
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            return "<a href='/music/artist-q'>Artist Q</a>"
        return None

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Q")
    assert payload is not None
    assert ("Last.fm search", 1) in calls
    assert ("Last.fm search (no-quotes)", 1) in calls
    # Fallback should not be necessary because no-quotes provided a match.
    assert ("Last.fm search (fallback)", 1) not in calls


def test_lastfm_no_quotes_not_triggered_on_406(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist Q"}
    calls: List[tuple] = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None):
        calls.append((label, max_attempts))
        if label == "Last.fm search":
            worker._last_http_status = 406
            worker._last_fetch_ok = False
            return None
        if label == "Last.fm search (fallback)":
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            return "<a href='/music/artist-q'>Artist Q</a>"
        return None

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Q")
    assert payload is not None
    # Should not attempt no-quotes variant when primary was 406.
    assert ("Last.fm search (no-quotes)", 1) not in calls
    assert ("Last.fm search (fallback)", 1) in calls
