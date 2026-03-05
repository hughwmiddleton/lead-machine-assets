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
