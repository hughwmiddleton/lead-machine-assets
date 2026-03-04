import pytest

from source_scheduler import SourceDiversityScheduler, SourceResult, SourceSpec


def test_scheduler_interleaves_sources():
    calls = []
    rows = [0, 1, 2]

    def make_spec(name):
        return SourceSpec(
            name=name,
            rows=rows,
            run_row=lambda idx: (calls.append(f"{name}{idx}") or SourceResult(attempted=True)),
            is_available=lambda: (True, None),
        )

    scheduler = SourceDiversityScheduler(
        [make_spec("SC"), make_spec("LF"), make_spec("FB")],
        row_label=str,
    )
    scheduler.run()

    # Expect non-bursty order: first three calls touch all sources.
    assert calls[:3] == ["SC0", "LF0", "FB0"]
    # Ensure sequence alternates by source, not SC-only burst.
    assert calls[:6] == ["SC0", "LF0", "FB0", "SC1", "LF1", "FB1"]


def test_scheduler_skips_on_cooldown_but_continues_other_sources():
    rows = [0, 1, 2, 3]
    sc_calls = []
    lf_calls = []
    sc_checks = {"count": 0}

    def sc_is_available():
        sc_checks["count"] += 1
        # After first two rows, SC enters cooldown.
        return (sc_checks["count"] <= 2, "cooldown" if sc_checks["count"] > 2 else None)

    sc_spec = SourceSpec(
        name="SC",
        rows=rows,
        run_row=lambda idx: (sc_calls.append(idx) or SourceResult(attempted=True)),
        is_available=sc_is_available,
    )
    lf_spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: (lf_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
    )

    summary = SourceDiversityScheduler([sc_spec, lf_spec], row_label=str).run()

    assert sc_calls == [0, 1]
    assert summary["SC"]["skipped_cooldown"] == len(rows) - len(sc_calls)
    # LF should still process all rows.
    assert lf_calls == rows


def test_scheduler_attempt_counts_match_rows():
    rows = [5, 6, 7, 8]
    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: SourceResult(attempted=True, enriched=(idx % 2 == 0)),
        is_available=lambda: (True, None),
    )
    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert summary["LF"]["attempted"] == len(rows)
    assert summary["LF"]["enriched"] == 2  # rows 6 and 8 (even idx) considered enriched
    assert summary["LF"]["skipped_cooldown"] == 0


def test_scheduler_mode_skips_legacy_phases(monkeypatch):
    # Build a worker instance without running QThread.__init__
    from cross_directory_enricher import CrossDirectoryEnricherWorker
    monkeypatch.setattr(CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    worker = CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()

    calls = {"dir": 0, "sc": 0, "lf": 0, "fb": 0, "sched": 0}

    monkeypatch.setattr(worker, "_phase_directory_matching", lambda *a, **k: calls.__setitem__("dir", calls["dir"] + 1))
    monkeypatch.setattr(worker, "_phase_soundcloud", lambda *a, **k: calls.__setitem__("sc", calls["sc"] + 1))
    monkeypatch.setattr(worker, "_phase_live_lookup", lambda *a, **k: calls.__setitem__("lf", calls["lf"] + 1))
    monkeypatch.setattr(worker, "_phase_facebook", lambda *a, **k: calls.__setitem__("fb", calls["fb"] + 1))
    monkeypatch.setattr(worker, "_run_interleaved_sources", lambda *a, **k: calls.__setitem__("sched", calls["sched"] + 1))

    monkeypatch.setenv("SOURCE_DIVERSITY_SCHEDULER", "1")

    # Minimal inputs: empty dataframe and stubs for unused params.
    import pandas as pd
    seed_df = pd.DataFrame()
    worker._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=None, total=0)

    assert calls["dir"] == 1
    assert calls["sched"] == 1
    assert calls["sc"] == 0
    assert calls["lf"] == 0
    assert calls["fb"] == 0


def test_adaptive_priority_prefers_successful_sources(monkeypatch):
    # Deterministic jitter for reproducibility.
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)

    calls = []
    rows = [0, 1, 2, 3]

    def sc_run(idx):
        calls.append(f"SC{idx}")
        return SourceResult(attempted=True, enriched=True)

    def lf_run(idx):
        calls.append(f"LF{idx}")
        return SourceResult(attempted=True, enriched=False)

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None)),
            SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lambda: (True, None)),
        ],
        row_label=str,
    )
    scheduler.run()

    # SC should be scheduled before LF once success history is available (after first row).
    assert calls[:2] == ["SC0", "LF0"]
    assert calls[2:4] == ["SC1", "LF1"]
    assert calls[4:6] == ["SC2", "LF2"]


def test_cooldown_sources_are_deprioritised(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)

    rows = [0, 1, 2, 3]
    calls = []
    lf_checks = {"count": 0}

    def sc_run(idx):
        calls.append(f"SC{idx}")
        return SourceResult(attempted=True, enriched=True)

    def lf_is_available():
        lf_checks["count"] += 1
        # LF in cooldown for first two checks, then becomes available.
        return (lf_checks["count"] > 2, "cooldown" if lf_checks["count"] <= 2 else None)

    def lf_run(idx):
        calls.append(f"LF{idx}")
        return SourceResult(attempted=True, enriched=False)

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None)),
            SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lf_is_available),
        ],
        row_label=str,
    )
    scheduler.run()

    # LF rows 0 and 1 are skipped for cooldown; first LF run should occur after SC has progressed.
    first_lf_idx = calls.index("LF2")
    assert first_lf_idx > calls.index("SC2")
    # Healthy SC keeps leading even after LF becomes available.
    assert calls.index("SC3") < calls.index("LF3")
