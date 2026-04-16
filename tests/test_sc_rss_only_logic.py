import time
import pandas as pd

from cross_directory_enricher import (
    CrossDirectoryEnricherWorker,
    SC_BREAKER_MIN_ROWS,
    SC_RSS_FAIL_BREAKER_THRESHOLD,
    SC_RSS_ONLY_CONSEC_CHALLENGES,
    SC_RSS_ONLY_CONSEC_403,
    SC_RSS_ONLY_COOLDOWN_SECONDS,
    _NightSCAttempt,
)


def _mk_worker() -> CrossDirectoryEnricherWorker:
    w = CrossDirectoryEnricherWorker(
        seed_csv_path="",
        output_csv_path="",
        bandcamp_csv_path="",
        soundcloud_csv_path="",
        unearthed_csv_path="",
        lastfm_csv_path="",
        enable_live_search=False,
        max_live_searches=1,
    )
    w.night_mode = True
    # Stub logger to avoid Qt signal machinery in tests.
    w.log_message = type("obj", (), {"emit": lambda *args, **kwargs: None})
    return w


def test_challenge_streak_required_for_rss_only():
    w = _mk_worker()
    # One challenge should not trigger RSS-only.
    w._sc_record_html_challenge()
    assert w._night_sc_challenge_streak == 1
    assert w._sc_rss_only_mode is False
    # Hitting the threshold should trigger RSS-only.
    for _ in range(SC_RSS_ONLY_CONSEC_CHALLENGES - 1):
        w._sc_record_html_challenge()
    assert w._sc_rss_only_mode is True


def test_challenge_reset_prevents_rss_only():
    w = _mk_worker()
    w._sc_record_html_challenge()
    # Reset streak as if a non-challenge fetch occurred.
    w._night_sc_challenge_streak = 0
    w._sc_last_challenge_at = 0.0
    w._sc_record_html_challenge()
    assert w._sc_rss_only_mode is False


def test_rss_only_cooldown_exits_mode():
    w = _mk_worker()
    w._sc_enter_rss_only_mode(reason="test", row_idx=5)
    w._sc_rss_only_entered_at = time.time() - (SC_RSS_ONLY_COOLDOWN_SECONDS + 1)
    w._sc_maybe_exit_rss_only(row_idx=6)
    assert w._sc_rss_only_mode is False


def test_consecutive_403_triggers_rss_only_mode():
    w = _mk_worker()
    for i in range(SC_RSS_ONLY_CONSEC_403):
        w._sc_record_403(row_idx=i, source="test")
    assert w._sc_rss_only_mode is True
    assert w._sc_consecutive_403 >= SC_RSS_ONLY_CONSEC_403
    assert w._sc_rss_only_entries_consecutive_403 == 1


def test_consecutive_403_does_not_overcount_entries():
    w = _mk_worker()
    for i in range(SC_RSS_ONLY_CONSEC_403):
        w._sc_record_403(row_idx=i, source="test")
    assert w._sc_rss_only_entries_consecutive_403 == 1
    # Additional 403s while already in rss_only should not increment entries counter.
    for i in range(2):
        w._sc_record_403(row_idx=100 + i, source="test")
    assert w._sc_rss_only_entries_consecutive_403 == 1


def test_rss_only_skips_engine_fetch():
    w = _mk_worker()
    w._sc_enter_rss_only_mode(reason="test")
    # Fake session that would raise if called.
    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("engine fetch should be skipped in rss_only")

    w.session = FakeSession()
    attempt = _NightSCAttempt()
    status, body = w._night_sc_http_get("https://soundcloud.com", "profile_fetch", attempt)
    assert status is None
    assert body == ""
    assert w.session.calls == 0
    assert w._sc_rss_only_engine_fetch_skips >= 1


def test_rss_only_skips_live_search_engine_paths(monkeypatch):
    w = _mk_worker()
    w._sc_enter_rss_only_mode(reason="test")
    called = {"people_search": 0}

    def fake_people_search(query):
        called["people_search"] += 1
        return []

    monkeypatch.setattr(w, "_soundcloud_people_search_candidates", fake_people_search)
    result = w._live_search_soundcloud("Artist")
    assert result is None
    assert called["people_search"] == 0  # guard prevented call
    assert w._sc_rss_only_engine_fetch_skips >= 1


def test_sc_gate_skips_weak_row_without_live_lookup(monkeypatch):
    logs = []
    w = _mk_worker()
    w.night_mode = False
    w.log_message = type("obj", (), {"emit": lambda *args: logs.append(str(args[1] if len(args) > 1 else args[0]))})
    seed_df = pd.DataFrame([{"Artist Name": "DJ", "SoundCloud Link": ""}], dtype=str).fillna("")
    ctx = w._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        w,
        "_live_search_soundcloud",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("soundcloud live lookup should be gated")),
    )

    matched, skip_rest = w._enrich_row_sc_live(seed_df, 0, ctx)

    assert matched is False
    assert skip_rest is False
    assert any("skipping soundcloud heavy enrichment" in msg.lower() for msg in logs)


def test_sc_phase_continues_during_cooldown(monkeypatch):
    w = _mk_worker()
    w.enable_live_search = True
    rows = 5
    seed_df = pd.DataFrame(
        {
            "SoundCloud Link": [""] * rows,
            "Artist Name": [f"Artist {i}" for i in range(rows)],
        }
    )

    # Cooldown flags per row: False, True, True, False, False
    cooldown_flags = [False, True, True, False, False]

    def fake_cooldown(now=None):
        return cooldown_flags.pop(0) if cooldown_flags else False

    sc_calls = []

    def fake_enrich(df, row_idx, ctx):
        sc_calls.append(row_idx)
        return (False, False)

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    w.log_message = Log()
    w._sc_in_live_cooldown = fake_cooldown
    w._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }
    w._enrich_row_sc_live = fake_enrich

    w._phase_soundcloud(seed_df, rows)

    # Enrichment called only on non-cooldown rows (0,3,4).
    assert sc_calls == [0, 3, 4]
    # Summary reflects all rows processed and cooldown skips counted.
    summary_lines = [l for l in logs if "[Enricher][SC Phase] Completed" in l]
    assert summary_lines
    assert "Completed 5 rows" in summary_lines[-1]
    assert "skipped_cooldown=2" in summary_lines[-1]
    # Cooldown skip summary is emitted instead of early stop.
    cooldown_lines = [l for l in logs if "cooldown_skip_summary" in l]
    assert cooldown_lines
    assert "cooldown_s_remaining" in cooldown_lines[-1]
    # No early-stop log on cooldown.
    assert not any("Stopped early: cooldown" in l for l in logs)


def test_sc_phase_continues_after_midphase_breaker_trip(monkeypatch):
    w = _mk_worker()
    w.enable_live_search = True
    rows = 4
    seed_df = pd.DataFrame(
        {
            "SoundCloud Link": [""] * rows,
            "Artist Name": [f"Artist {i}" for i in range(rows)],
        }
    )

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    w.log_message = Log()

    # Real cooldown detector, but we control the timestamp knob.
    w._sc_live_disabled_until = 0.0

    def build_ctx(df, row_idx, position, total):
        return {
            "artist": df.at[row_idx, "Artist Name"],
            "position": position,
            "total": total,
            "spotify_id": "",
        }

    w._build_row_context = build_ctx

    def fake_sc_enrich(df, row_idx, ctx):
        # Trip breaker on row 1, leaving subsequent rows to observe cooldown.
        if row_idx == 1:
            w._sc_live_disabled_until = time.time() + 999
        return (False, False)

    w._enrich_row_sc_live = fake_sc_enrich

    w._phase_soundcloud(seed_df, rows)

    # Completed should still show all rows, with cooldown skips for rows 2 and 3.
    summary_lines = [l for l in logs if "[Enricher][SC Phase] Completed" in l]
    assert summary_lines
    assert "Completed 4 rows" in summary_lines[-1]
    assert "skipped_cooldown=2" in summary_lines[-1]
    # Cooldown skip summary emitted.
    cooldown_lines = [l for l in logs if "cooldown_skip_summary" in l]
    assert cooldown_lines
    # No early-stop cooldown log.
    assert not any("Stopped early: cooldown" in l for l in logs)


def test_sc_retry_after_cooldown(monkeypatch):
    w = _mk_worker()
    w.enable_live_search = True
    rows = 4
    seed_df = pd.DataFrame(
        {
            "SoundCloud Link": [""] * rows,
            "Artist Name": [f"Artist {i}" for i in range(rows)],
            "SC_Status": [""] * rows,
            "SC_Reason": [""] * rows,
        }
    )

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    w.log_message = Log()
    w._phase_directory_matching = lambda *args, **kwargs: None
    w._phase_live_lookup = lambda *args, **kwargs: now.__setitem__(0, now[0] + 20.0)
    w._phase_instagram_email = lambda *args, **kwargs: None
    w._phase_website_email = lambda *args, **kwargs: None
    w._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }

    calls = []
    attempts = {}

    def fake_sc_enrich(df, row_idx, ctx):
        attempts[row_idx] = attempts.get(row_idx, 0) + 1
        calls.append(row_idx)
        if row_idx == 1 and attempts[row_idx] == 1:
            w._sc_live_disabled_until = now[0] + 10.0
        if row_idx in {2, 3} and now[0] >= 1010.0:
            df.at[row_idx, "SC_Status"] = "retried"
        return (False, False)

    w._enrich_row_sc_live = fake_sc_enrich

    w._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=None, total=rows)

    assert calls == [0, 1, 2, 3]
    assert seed_df.at[2, "SC_Status"] == "retried"
    assert seed_df.at[3, "SC_Status"] == "retried"
    assert any("Starting post_website" in line for line in logs)
    assert any("Completed post_website (retried=2" in line for line in logs)


def test_sc_retry_capped_at_two(monkeypatch):
    import cross_directory_enricher as cde

    w = _mk_worker()
    w.enable_live_search = True
    rows = 2
    seed_df = pd.DataFrame(
        {
            "SoundCloud Link": [""] * rows,
            "Artist Name": [f"Artist {i}" for i in range(rows)],
            "SC_Status": [""] * rows,
            "SC_Reason": [""] * rows,
        }
    )

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    w.log_message = Log()
    w._phase_directory_matching = lambda *args, **kwargs: None
    w._phase_live_lookup = lambda *args, **kwargs: now.__setitem__(0, now[0] + 20.0)
    w._phase_instagram_email = lambda *args, **kwargs: None
    w._phase_website_email = lambda *args, **kwargs: None
    w._phase_facebook = lambda *args, **kwargs: now.__setitem__(0, now[0] + 20.0)
    w._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }

    attempts = {}

    def fake_sc_enrich(df, row_idx, ctx):
        attempts[row_idx] = attempts.get(row_idx, 0) + 1
        if row_idx == 0 and attempts[row_idx] == 1:
            w._sc_live_disabled_until = now[0] + 10.0
        elif row_idx == 1:
            w._sc_live_disabled_until = now[0] + 10.0
        return (False, False)

    w._enrich_row_sc_live = fake_sc_enrich

    w._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=object(), total=rows)

    assert attempts[0] == 1
    assert attempts[1] == 2
    assert any("Completed final_window" in line and "exhausted=1" in line for line in logs)


def test_sc_retry_skipped_if_row_enriched_elsewhere(monkeypatch):
    w = _mk_worker()
    w.enable_live_search = True
    rows = 4
    seed_df = pd.DataFrame(
        {
            "SoundCloud Link": [""] * rows,
            "Artist Name": [f"Artist {i}" for i in range(rows)],
            "SC_Status": [""] * rows,
            "SC_Reason": [""] * rows,
        }
    )

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    w.log_message = Log()
    w._phase_directory_matching = lambda *args, **kwargs: None
    w._phase_instagram_email = lambda *args, **kwargs: None
    w._phase_website_email = lambda *args, **kwargs: None
    w._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }

    def fake_phase_live_lookup(df, total):
        now[0] += 20.0
        df.at[2, "SoundCloud Link"] = "https://soundcloud.com/already-filled"

    w._phase_live_lookup = fake_phase_live_lookup

    calls = []
    attempts = {}

    def fake_sc_enrich(df, row_idx, ctx):
        attempts[row_idx] = attempts.get(row_idx, 0) + 1
        calls.append(row_idx)
        if row_idx == 1 and attempts[row_idx] == 1:
            w._sc_live_disabled_until = now[0] + 10.0
        return (False, False)

    w._enrich_row_sc_live = fake_sc_enrich

    w._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=None, total=rows)

    assert calls == [0, 1, 3]
    assert attempts.get(2, 0) == 0
    assert seed_df.at[2, "SoundCloud Link"] == "https://soundcloud.com/already-filled"
    assert any("Completed post_website (retried=1" in line for line in logs)


def test_source_phased_top_level_order_unchanged(monkeypatch):
    import cross_directory_enricher as cde

    w = _mk_worker()
    w.enable_live_search = True
    seed_df = pd.DataFrame({"Artist Name": [], "SoundCloud Link": []})
    calls = []

    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)
    monkeypatch.setattr(
        cde,
        "_apply_fb_promotion_df",
        lambda df, log_fn=None: calls.append("fb_promote") or df,
    )
    w._phase_directory_matching = lambda *args, **kwargs: calls.append("directory")
    w._phase_soundcloud = lambda *args, **kwargs: calls.append("soundcloud") or {}
    w._phase_live_lookup = lambda *args, **kwargs: calls.append("live_lookup")
    w._phase_instagram_email = lambda *args, **kwargs: calls.append("instagram")
    w._phase_website_email = lambda *args, **kwargs: calls.append("website")
    w._phase_facebook = lambda *args, **kwargs: calls.append("facebook")

    w._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=object(), total=0)

    assert calls == [
        "directory",
        "soundcloud",
        "live_lookup",
        "instagram",
        "website",
        "fb_promote",
        "facebook",
    ]


def test_unearthed_fb_first_source_phased_bypasses_shared_enrichers(monkeypatch):
    import cross_directory_enricher as cde

    w = _mk_worker()
    w.enable_live_search = True
    w.max_live_searches = 0
    w.live_search_attempts = 0
    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    w.log_message = Log()
    w._should_short_circuit_after_domain_reuse = lambda *args, **kwargs: False
    w._init_row_enrichment_state = lambda: None
    w._update_progress = lambda *args, **kwargs: None
    w._phase_spotify_discovery = lambda *args, **kwargs: None
    w._retry_deferred_soundcloud_rows = lambda *args, **kwargs: {}
    w._reset_live_lookup_bclf_stats = lambda: None
    w._lf_endpoint_in_cooldown = lambda *args, **kwargs: False
    w._set_platform_state = lambda *args, **kwargs: None
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)
    monkeypatch.setattr(cde, "_apply_fb_promotion_df", lambda df, log_fn=None: df)

    calls = {"dir": [], "sc": [], "lf": [], "ig": [], "website": [], "fb": []}

    def build_ctx(df, row_idx, position, total):
        return {
            "artist": df.at[row_idx, "Artist Name"],
            "position": position,
            "total": total,
            "spotify_id": "",
            "seed_lastfm_urls": [],
        }

    def record_dir(df, row_idx, directory_indexes, priority, ctx):
        calls["dir"].append(row_idx)
        return False

    def record_sc(df, row_idx, ctx):
        calls["sc"].append(row_idx)
        return False, False

    def record_lf(df, row_idx, ctx, skip_lastfm=False):
        calls["lf"].append(row_idx)
        return False, False

    def record_ig(df, row_idx, ctx):
        calls["ig"].append(row_idx)
        return False

    def record_website(df, row_idx, ctx):
        calls["website"].append(row_idx)
        return False

    def record_fb(df, row_idx, fb_driver, ctx):
        calls["fb"].append(row_idx)
        return False

    w._build_row_context = build_ctx
    w._enrich_row_directories = record_dir
    w._enrich_row_sc_live = record_sc
    w._enrich_row_live_lookup = record_lf
    w._enrich_row_instagram_email = record_ig
    w._enrich_row_website_email = record_website
    w._enrich_row_facebook = record_fb

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed FB First",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "https://www.facebook.com/unearthed.fb.first",
                "External Links": "",
                "Facebook_URL": "",
                "SoundCloud Link": "",
            },
            {
                "Artist Name": "Unearthed Fallback",
                "Source Directory": "Unearthed",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "External Links": "",
                "Facebook_URL": "",
                "SoundCloud Link": "",
            },
            {
                "Artist Name": "Spotify Control",
                "Source Directory": "Spotify",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "External Links": "",
                "Facebook_URL": "",
                "SoundCloud Link": "",
            },
        ],
        dtype=str,
    ).fillna("")

    w._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=object(), total=len(seed_df))

    for phase_name in ("dir", "website"):
        assert 0 not in calls[phase_name]
        assert 1 in calls[phase_name]
        assert 2 in calls[phase_name]
    for phase_name in ("ig", "fb"):
        assert 0 in calls[phase_name]
        assert 1 in calls[phase_name]
        assert 2 in calls[phase_name]
    assert 0 not in calls["sc"]
    assert 0 not in calls["lf"]
    assert any("[Unearthed Path] activated artist='Unearthed FB First' row=0" in line for line in logs)
    assert any("[Unearthed Path] no usable FB URL, resuming standard path artist='Unearthed Fallback' row=1" in line for line in logs)


def test_breaker_has_grace_period():
    w = _mk_worker()
    # Failures before grace rows should not trip breaker.
    w._sc_rows_seen = SC_BREAKER_MIN_ROWS - 5
    for _ in range(SC_RSS_FAIL_BREAKER_THRESHOLD):
        w._sc_record_rss_result(False, reason="rss_unavailable", row_idx=1)
    assert w._night_sc_breaker_tripped is False
    assert w._sc_live_enrich_disabled is False
    # After grace rows, consecutive failures should trip breaker.
    w._sc_rows_seen = SC_BREAKER_MIN_ROWS + 1
    w._sc_rss_fail_streak = 0
    for _ in range(SC_RSS_FAIL_BREAKER_THRESHOLD):
        w._sc_record_rss_result(False, reason="blocked_api", row_idx=10)
    assert w._night_sc_breaker_tripped is True
    assert w._sc_live_enrich_disabled is False
    assert w._sc_live_disabled_until > time.time()


def test_breaker_cooldown_expires_and_resets():
    w = _mk_worker()
    w._sc_rows_seen = SC_BREAKER_MIN_ROWS + 1
    for _ in range(SC_RSS_FAIL_BREAKER_THRESHOLD):
        w._sc_record_rss_result(False, reason="blocked_api", row_idx=5)
    assert w._sc_in_live_cooldown() is True
    # Simulate time passing
    w._sc_live_disabled_until = time.time() - 1
    assert w._sc_in_live_cooldown() is False
    assert w._sc_rss_fail_streak == 0


def test_nofeed_does_not_trip_breaker():
    w = _mk_worker()
    w._sc_rows_seen = SC_BREAKER_MIN_ROWS + 5
    for _ in range(SC_RSS_FAIL_BREAKER_THRESHOLD + 2):
        w._sc_record_rss_result(False, reason="rss_unavailable", row_idx=2)
    assert w._night_sc_breaker_tripped is False
    assert w._sc_in_live_cooldown() is False
    assert w._sc_rss_fail_streak_blocked == 0
    assert w._sc_rss_fail_streak_nofeed >= SC_RSS_FAIL_BREAKER_THRESHOLD


def test_blocked_trips_breaker_after_grace_rows():
    w = _mk_worker()
    w._sc_rows_seen = SC_BREAKER_MIN_ROWS + 1
    for _ in range(SC_RSS_FAIL_BREAKER_THRESHOLD):
        w._sc_record_rss_result(False, reason="blocked_api", row_idx=3)
    assert w._night_sc_breaker_tripped is True
    assert w._sc_in_live_cooldown() is True


def test_mixed_reasons_only_trip_on_blocked_streak():
    w = _mk_worker()
    w._sc_rows_seen = SC_BREAKER_MIN_ROWS + 1
    for _ in range(2):
        w._sc_record_rss_result(False, reason="rss_unavailable", row_idx=4)
    for _ in range(SC_RSS_FAIL_BREAKER_THRESHOLD):
        w._sc_record_rss_result(False, reason="blocked_api", row_idx=4)
    assert w._night_sc_breaker_tripped is True
    assert w._sc_in_live_cooldown() is True
