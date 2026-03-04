import time
from types import SimpleNamespace
from typing import List

import pandas as pd

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


def test_lastfm_406_no_retry(monkeypatch):
    worker = _build_worker()
    statuses: List[int] = [406, 200]

    def fake_get(url, timeout=None, headers=None):
        status = statuses.pop(0)
        return _DummyResp(status, "html-ok")

    sleeps: List[float] = []

    monkeypatch.setattr(worker.session, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    html = worker._fetch_url(
        "https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=3, endpoint="search"
    )

    assert html is None  # first 406 should short-circuit
    assert worker._last_http_status == 406
    assert len(statuses) == 1  # second status unused (no retry)
    assert sleeps == []  # no backoff when 406


def test_lastfm_soft_cooldown_after_consecutive_406(monkeypatch):
    worker = _build_worker()
    monkeypatch.setattr(cde, "LF_COOLDOWN_CONSEC_406", 4)
    monkeypatch.setattr(cde, "LF_COOLDOWN_MIN_S", 10)
    monkeypatch.setattr(cde, "LF_COOLDOWN_MAX_S", 60)
    statuses: List[int] = [406, 406, 406, 406]

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

    # First three 406s should not enter search cooldown yet.
    for _ in range(3):
        assert (
            worker._fetch_url(
                "https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=1, endpoint="search"
            )
            is None
        )
    assert worker._lf_search_cooldown_until == 0.0

    # Fourth 406 crosses the threshold.
    assert (
        worker._fetch_url(
            "https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=1, endpoint="search"
        )
        is None
    )

    assert worker._lf_search_cooldown_until > fake_mono()
    assert any("Entering soft cooldown (search)" in msg for msg in logs)

    called = {"count": 0}

    def fail_get(*args, **kwargs):
        called["count"] += 1
        return _DummyResp(200, "should not be called")

    monkeypatch.setattr(worker.session, "get", fail_get)
    html2 = worker._fetch_url(
        "https://www.last.fm/search?q=test&type=artist", "Last.fm search", max_attempts=1, endpoint="search"
    )
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
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


def test_lastfm_phase_skips_rows_without_stopping(monkeypatch):
    worker = _build_worker()
    worker._lf_search_cooldown_until = worker._lf_now() + 30
    logs: List[str] = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))

    df = pd.DataFrame(
        {
            "Artist Name": [f"Artist {i}" for i in range(10)],
            "SoundCloud Link": ["" for _ in range(10)],
            "Bandcamp_URL": ["" for _ in range(10)],
        }
    )

    worker._phase_live_lookup(df, total=len(df.index))

    assert not any("Stopped early: cooldown" in msg for msg in logs)
    # Should log search cooldown and summary with search_skipped=10
    skipped_logs = [msg for msg in logs if "search cooldown active" in msg]
    assert len(skipped_logs) >= 1
    assert any("search_skipped=10" in msg for msg in logs)


def test_lastfm_search_cooldown_does_not_block_profile_fetch(monkeypatch):
    worker = _build_worker()
    logs: List[str] = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))

    # Pretend search cooldown is already active.
    worker._lf_search_cooldown_until = worker._lf_now() + 30

    # Search attempt should be skipped and logged.
    payload = worker._live_search_lastfm("Artist With Cooldown")
    assert payload is None
    assert any("search cooldown active" in msg for msg in logs)
    assert not any("profile cooldown active" in msg for msg in logs)

    # Profile fetch should still run (different endpoint).
    monkeypatch.setattr(
        cde,
        "_extract_links_from_profile",
        lambda html, source_dir, url: ({"https://twitter.com/demo"}, set(), set(), set()),
    )
    calls: List[str] = []

    def fake_get(url, timeout=None, headers=None):
        calls.append(url)
        return _DummyResp(200, "<html/>")

    monkeypatch.setattr(worker.session, "get", fake_get)
    profile_payload = worker._fetch_profile_and_build("https://www.last.fm/music/demo", "lastfm")
    assert profile_payload is not None
    assert profile_payload.source_url == "https://www.last.fm/music/demo"
    assert calls  # profile fetch issued despite search cooldown
    # Profile cooldown should not have been set by the skipped search.
    assert not worker._lf_endpoint_in_cooldown("profile")


def test_lastfm_endpoint_cooldowns_are_independent(monkeypatch):
    worker = _build_worker()
    # Trigger search cooldown.
    for _ in range(cde.LF_COOLDOWN_CONSEC_406):
        worker._lf_mark_406("search")
    assert worker._lf_endpoint_in_cooldown("search")
    assert not worker._lf_endpoint_in_cooldown("profile")
    assert worker._lf_profile_cooldown_until == 0.0

    # Trigger profile cooldown separately.
    for _ in range(cde.LF_COOLDOWN_CONSEC_406):
        worker._lf_mark_406("profile")
    assert worker._lf_endpoint_in_cooldown("profile")
    # Search cooldown remains set (independent timers).
    assert worker._lf_endpoint_in_cooldown("search")


def test_lastfm_cooldown_logs_include_endpoint_labels(monkeypatch):
    worker = _build_worker()
    logs: List[str] = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))

    # Enter search cooldown via helper.
    worker._lf_set_endpoint_cooldown("search", consec=cde.LF_COOLDOWN_CONSEC_406)
    worker._lf_set_endpoint_cooldown("profile", consec=cde.LF_COOLDOWN_CONSEC_406)

    assert any("search" in msg.lower() for msg in logs)
    assert any("profile" in msg.lower() for msg in logs)


def test_lastfm_phase_mixed_rows_continues_under_search_cooldown(monkeypatch):
    worker = _build_worker()
    logs: List[str] = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))

    df = pd.DataFrame(
        {
            "Artist Name": ["Artist A", "Artist B"],
            "SoundCloud Link": ["", ""],
            "Bandcamp_URL": ["", ""],
        }
    )

    # Make profile fetch return actionable payload.
    monkeypatch.setattr(
        cde,
        "_extract_links_from_profile",
        lambda html, source_dir, url: ({"https://twitter.com/demo"}, set(), set(), set()),
    )
    monkeypatch.setattr(worker.session, "get", lambda url, timeout=None, headers=None: _DummyResp(200, "<html/>"))

    profile_called = {"called": False}

    def fake_live_lookup(artist_name, skip_soundcloud=False, skip_bandcamp=False):
        if artist_name == "Artist A":
            for _ in range(cde.LF_COOLDOWN_CONSEC_406):
                worker._lf_mark_406("search")
            worker._lf_search_skipped_cooldown += 1
            worker.log_message.emit("[Enricher][LF] search cooldown active; skipping row 'Artist A' expires_in=30s")
            return None
        if artist_name == "Artist B":
            profile_called["called"] = True
            return worker._fetch_profile_and_build("https://www.last.fm/music/artistb", "lastfm")
        return None

    monkeypatch.setattr(worker, "_live_lookup", fake_live_lookup)

    worker._phase_live_lookup(df, total=len(df.index))

    # Search attempts were skipped for first row; profile still fetched for second row.
    assert any("search cooldown active" in msg for msg in logs)
    assert profile_called["called"]
    # Phase summary should show at least one search skip and no early stop.
    assert any("search_skipped" in msg for msg in logs)
    assert not any("Stopped early: cooldown" in msg for msg in logs)
