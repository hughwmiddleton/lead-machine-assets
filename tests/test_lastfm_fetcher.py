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


def _live_lookup_call_order(worker, monkeypatch):
    calls = []

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda artist: calls.append("bandcamp") or None)
    monkeypatch.setattr(worker, "_live_search_soundcloud", lambda artist: calls.append("soundcloud") or None)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda artist: calls.append("lastfm") or None)

    worker._live_lookup("Test Artist")
    return calls


def test_live_lookup_bc_lf_warmup_keeps_default_order(monkeypatch):
    worker = _build_worker()
    worker._live_lookup_bclf_adaptive_enabled = True
    worker._reset_live_lookup_bclf_stats()
    worker._live_lookup_bclf_stats["bandcamp"]["attempts"] = cde.LIVE_LOOKUP_BCLF_MIN_ATTEMPTS - 1
    worker._live_lookup_bclf_stats["bandcamp"]["enriched"] = 1
    worker._live_lookup_bclf_stats["lastfm"]["attempts"] = cde.LIVE_LOOKUP_BCLF_MIN_ATTEMPTS + 2
    worker._live_lookup_bclf_stats["lastfm"]["enriched"] = cde.LIVE_LOOKUP_BCLF_MIN_ATTEMPTS + 2

    assert _live_lookup_call_order(worker, monkeypatch) == ["bandcamp", "soundcloud", "lastfm"]


def test_live_lookup_bc_lf_adaptive_order_tracks_stats(monkeypatch):
    worker = _build_worker()
    worker._live_lookup_bclf_adaptive_enabled = True

    worker._reset_live_lookup_bclf_stats()
    worker._live_lookup_bclf_stats["bandcamp"].update({"attempts": 4, "enriched": 1, "cooldown": 0})
    worker._live_lookup_bclf_stats["lastfm"].update({"attempts": 4, "enriched": 4, "cooldown": 0})
    assert _live_lookup_call_order(worker, monkeypatch) == ["lastfm", "soundcloud", "bandcamp"]

    worker._reset_live_lookup_bclf_stats()
    worker._live_lookup_bclf_stats["bandcamp"].update({"attempts": 4, "enriched": 4, "cooldown": 0})
    worker._live_lookup_bclf_stats["lastfm"].update({"attempts": 4, "enriched": 1, "cooldown": 0})
    assert _live_lookup_call_order(worker, monkeypatch) == ["bandcamp", "soundcloud", "lastfm"]


def test_live_lookup_bc_lf_cooldown_penalty_biases_order(monkeypatch):
    worker = _build_worker()
    worker._live_lookup_bclf_adaptive_enabled = True
    worker._reset_live_lookup_bclf_stats()
    worker._live_lookup_bclf_stats["bandcamp"].update({"attempts": 4, "enriched": 2, "cooldown": 0})
    worker._live_lookup_bclf_stats["lastfm"].update({"attempts": 4, "enriched": 4, "cooldown": 4})

    assert _live_lookup_call_order(worker, monkeypatch) == ["bandcamp", "soundcloud", "lastfm"]


def test_live_lookup_success_credit_requires_applied_winner(monkeypatch):
    worker = _build_worker()
    worker._live_lookup_bclf_adaptive_enabled = True
    worker._reset_live_lookup_bclf_stats()
    seed_df = pd.DataFrame(
        [{"Artist Name": "Artist", "Bandcamp_URL": "", "SoundCloud Link": ""}],
        dtype=str,
    ).fillna("")
    ctx = {"artist": "Artist", "spotify_id": ""}
    payload = cde.EnrichmentPayload(
        socials=set(),
        websites={"https://example.com"},
        emails=set(),
        link_hubs=set(),
        source_dir="lastfm",
        source_url="https://www.last.fm/music/artist",
        match_score=0.9,
    )
    applied_results = iter([False, True])

    monkeypatch.setattr(worker, "_live_lookup", lambda *args, **kwargs: payload)
    monkeypatch.setattr(worker, "_mark_sc_blocked_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_apply_payload_guarded", lambda *args, **kwargs: next(applied_results))

    enriched, skip_rest = worker._enrich_row_live_lookup(seed_df, 0, ctx)
    assert enriched is False
    assert skip_rest is False
    assert worker._live_lookup_bclf_stats["lastfm"]["enriched"] == 0

    enriched, skip_rest = worker._enrich_row_live_lookup(seed_df, 0, ctx)
    assert enriched is True
    assert skip_rest is False
    assert worker._live_lookup_bclf_stats["lastfm"]["enriched"] == 1


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


def test_lastfm_gate_skips_weak_row_before_fetch(monkeypatch):
    worker = _build_worker()
    seed_df = pd.DataFrame([{"Artist Name": "DJ"}], dtype=str).fillna("")
    worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        worker,
        "_fetch_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("lastfm fetch should be gated")),
    )

    payload = worker._live_search_lastfm("DJ")

    assert payload is None
    assert worker._row_enrichment_state.get("lastfm") == "skipped"


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


def test_lastfm_primary_406_uses_artist_only_fallback_and_continues(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist X"}
    calls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        calls.append((url, label, max_attempts))
        if label == "Last.fm search":
            worker._last_http_status = 406
            worker._last_fetch_ok = False
            worker._lf_mark_406("search")
            return None
        worker._last_http_status = 200
        worker._last_fetch_ok = True
        worker._lf_mark_success("search")
        return "<a href='/music/artist-x'>Artist X</a>"

    def fake_profile(profile_url, source_dir, confidence=None):
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=profile_url
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist X")

    assert payload is not None
    assert calls == [
        ('https://www.last.fm/search?q=%22Artist+X%22+%22Good+Track%22&type=artist', "Last.fm search", 1),
        ("https://www.last.fm/search?q=Artist+X&type=artist", "Last.fm search (fallback)", 1),
    ]
    assert worker._lf_search_consecutive_406 == 0


def test_lastfm_artist_only_406_single_attempt(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "", "artist": "Artist Y"}
    calls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        calls.append((label, max_attempts))
        worker._last_http_status = 406
        worker._last_fetch_ok = False
        worker._lf_mark_406("search")
        return None

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Y")

    assert payload is None
    assert calls == [("Last.fm search (fallback)", 1)]
    assert worker._lf_search_consecutive_406 == 1


def test_lastfm_primary_406_fallback_is_bounded_to_two_attempts(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist X"}
    calls = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        calls.append((url, label, max_attempts))
        worker._last_http_status = 406
        worker._last_fetch_ok = False
        worker._lf_mark_406("search")
        return None

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist X")

    assert payload is None
    assert calls == [
        ('https://www.last.fm/search?q=%22Artist+X%22+%22Good+Track%22&type=artist', "Last.fm search", 1),
        ("https://www.last.fm/search?q=Artist+X&type=artist", "Last.fm search (fallback)", 1),
    ]


def test_lastfm_primary_406_fallback_can_trigger_cooldown_after_bounded_attempts(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist X"}
    calls = []
    monkeypatch.setattr(cde, "LF_COOLDOWN_CONSEC_406", 2)

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        calls.append((label, max_attempts))
        worker._last_http_status = 406
        worker._last_fetch_ok = False
        worker._lf_mark_406("search")
        return None

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist X")

    assert payload is None
    assert calls == [("Last.fm search", 1), ("Last.fm search (fallback)", 1)]
    assert worker._lf_endpoint_in_cooldown("search")


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


def test_lastfm_low_confidence_no_second_search(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Good Track", "artist": "Artist Q"}
    calls: List[tuple] = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        calls.append((label, max_attempts))
        worker._last_http_status = 200
        worker._last_fetch_ok = True
        # Intentionally mismatched name to keep confidence below threshold.
        return "<a href='/music/not-artist'>Not Artist</a>"

    def fail_profile(profile_url, source_dir, confidence=None):
        raise AssertionError("profile fetch should not be called when confidence is too low")

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fail_profile)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    payload = worker._live_search_lastfm("Artist Q")
    assert payload is None
    assert calls == [("Last.fm search", 1)]


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

    def fake_live_lookup(artist_name, skip_soundcloud=False, skip_bandcamp=False, skip_lastfm=False):
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


def test_lastfm_profile_cache_skips_search(monkeypatch):
    worker = _build_worker()
    artist = "Cached Artist"
    artist_key = cde.normalise_artist_name(artist)
    cached_url = "https://www.last.fm/music/cached-artist"
    worker._lf_profile_url_cache[artist_key] = cached_url

    def fail_increment():
        raise AssertionError("search should not run on cache hit")

    profile_calls = {"count": 0}

    def fake_profile(url, source_dir, confidence=None):
        profile_calls["count"] += 1
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials=set(), websites=set(), emails=set(), link_hubs=set(), source_dir=source_dir, source_url=url
        )

    monkeypatch.setattr(worker, "_increment_live_counter", fail_increment)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload = worker._live_search_lastfm(artist)

    assert payload is not None
    assert profile_calls["count"] == 1


def test_lastfm_search_populates_profile_cache(monkeypatch):
    worker = _build_worker()
    artist = "Search Artist"
    artist_key = cde.normalise_artist_name(artist)
    search_calls = {"count": 0}

    def fake_increment():
        search_calls["count"] += 1
        return True

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        worker._last_http_status = 200
        worker._last_fetch_ok = True
        return "<a href='/music/search-artist'>Search Artist</a>"

    def fake_profile(url, source_dir, confidence=None):
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials={"https://twitter.com/demo"},
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        )

    monkeypatch.setattr(worker, "_increment_live_counter", fake_increment)
    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload = worker._live_search_lastfm(artist)

    assert payload is not None
    assert search_calls["count"] == 1
    assert worker._lf_profile_url_cache[artist_key] == "https://www.last.fm/music/search-artist"
    assert worker._lf_search_result_cache[artist_key] == "https://www.last.fm/music/search-artist"


def test_lastfm_search_path_single_profile_fetch(monkeypatch):
    worker = _build_worker()
    worker._live_context = {"song_title": "Great Song", "artist": "Single Artist"}
    search_calls: List[str] = []
    profile_calls: List[str] = []

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        if endpoint == "search":
            search_calls.append(label)
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            return "<a href='/music/single-artist'>Single Artist</a>"
        raise AssertionError("only search fetch expected")

    def fake_profile(url, source_dir, confidence=None):
        profile_calls.append(url)
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials={"https://twitter.com/demo"},
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        )

    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload = worker._live_search_lastfm("Single Artist")

    assert payload is not None
    assert search_calls == ["Last.fm search"]
    assert len(profile_calls) == 1


def test_lastfm_cache_used_during_search_cooldown(monkeypatch):
    worker = _build_worker()
    artist = "Cooldown Artist"
    artist_key = cde.normalise_artist_name(artist)
    cached_url = "https://www.last.fm/music/cooldown-artist"
    worker._lf_profile_url_cache[artist_key] = cached_url
    worker._lf_search_cooldown_until = worker._lf_now() + 30

    def fail_increment():
        raise AssertionError("search should not run when cache is present")

    profile_calls = {"count": 0}

    def fake_profile(url, source_dir, confidence=None):
        profile_calls["count"] += 1
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials={"https://twitter.com/demo"},
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        )

    monkeypatch.setattr(worker, "_increment_live_counter", fail_increment)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload = worker._live_search_lastfm(artist)

    assert payload is not None
    assert profile_calls["count"] == 1
    assert worker._lf_search_skipped_cooldown == 0


def test_lastfm_profile_first_from_seed_url(monkeypatch):
    worker = _build_worker()
    artist = "Seed Artist"
    seed_url = "https://www.last.fm/music/seed-artist"
    worker._live_context = {"seed_lastfm_urls": {seed_url}, "song_title": "", "artist": artist}
    worker._lf_search_cooldown_until = worker._lf_now() + 30

    def fail_increment():
        raise AssertionError("search should not run when seed URL is present")

    profile_calls = {"count": 0}

    def fake_profile(url, source_dir, confidence=None):
        profile_calls["count"] += 1
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials={"https://twitter.com/demo"},
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        )

    monkeypatch.setattr(worker, "_increment_live_counter", fail_increment)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload = worker._live_search_lastfm(artist)

    assert payload is not None
    assert profile_calls["count"] == 1
    assert worker._lf_search_skipped_cooldown == 0


def test_lastfm_search_result_reuse(monkeypatch):
    worker = _build_worker()
    artist = "Reuse Artist"
    artist_key = cde.normalise_artist_name(artist)
    search_calls = {"count": 0}
    profile_calls = {"count": 0}

    def fake_increment():
        search_calls["count"] += 1
        return True

    def fake_fetch(url, label=None, max_attempts=None, headers=None, endpoint=None):
        if "search" in (label or "").lower():
            worker._last_http_status = 200
            worker._last_fetch_ok = True
            return "<a href='/music/reuse-artist'>Reuse Artist</a>"
        return ""

    def fake_profile(url, source_dir, confidence=None):
        profile_calls["count"] += 1
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials={"https://twitter.com/demo"},
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        )

    monkeypatch.setattr(worker, "_increment_live_counter", fake_increment)
    monkeypatch.setattr(worker, "_fetch_url", fake_fetch)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload1 = worker._live_search_lastfm(artist)
    # Simulate a fresh row (row-scoped state resets between rows in production).
    worker._row_enrichment_state = {"lastfm": "pending"}
    payload2 = worker._live_search_lastfm(artist)

    assert payload1 is not None and payload2 is not None
    assert search_calls["count"] == 1
    assert profile_calls["count"] == 2
    assert worker._lf_search_result_cache[artist_key] == "https://www.last.fm/music/reuse-artist"


def test_lastfm_canonical_cache_reuse(monkeypatch):
    worker = _build_worker()
    artist = "Canonical Artist"
    artist_key = cde.normalise_artist_name(artist)
    observed_url = "https://www.last.fm/music/observed"
    canonical_url = "https://www.last.fm/music/canonical"
    worker._lf_profile_url_cache[artist_key] = observed_url
    worker._lf_canonical_url_cache[observed_url] = canonical_url

    def fail_increment():
        raise AssertionError("search should not run when canonical cache is present")

    profile_calls = {"count": 0}

    def fake_profile(url, source_dir, confidence=None):
        assert url == canonical_url
        profile_calls["count"] += 1
        worker._last_fetch_ok = True
        worker._last_resolved_profile_url = url
        return cde.EnrichmentPayload(
            socials={"https://twitter.com/demo"},
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        )

    monkeypatch.setattr(worker, "_increment_live_counter", fail_increment)
    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_profile)

    payload = worker._live_search_lastfm(artist)

    assert payload is not None
    assert profile_calls["count"] == 1
    assert worker._lf_profile_url_cache[artist_key] == canonical_url
    assert worker._lf_canonical_url_cache[observed_url] == canonical_url
