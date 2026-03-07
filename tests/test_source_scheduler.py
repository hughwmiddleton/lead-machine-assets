import pytest

from source_scheduler import SourceDiversityScheduler, SourceResult, SourceSpec, promote_facebook_url


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


def test_row_source_lock_prevents_repeat_attempts():
    rows = [0, 0, 1]
    calls = []

    def run_row(idx):
        calls.append(idx)
        if idx == 0:
            return SourceResult(attempted=True, enriched=False, retry_later=False)
        return SourceResult(attempted=True, enriched=True)

    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=run_row,
        is_available=lambda: (True, None),
    )

    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert calls == [0, 1]
    assert summary["LF"]["attempted"] == 2
    assert summary["LF"]["enriched"] == 1


def test_row_source_lock_does_not_apply_to_retry_later():
    rows = [0, 0, 1]
    calls = []
    row_zero_attempts = {"count": 0}

    def run_row(idx):
        calls.append(idx)
        if idx == 0:
            row_zero_attempts["count"] += 1
            return SourceResult(
                attempted=True,
                enriched=(row_zero_attempts["count"] > 1),
                retry_later=(row_zero_attempts["count"] == 1),
            )
        return SourceResult(attempted=True, enriched=True)

    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=run_row,
        is_available=lambda: (True, None),
    )

    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert calls == [0, 0, 1]
    assert summary["LF"]["attempted"] == 3
    assert summary["LF"]["enriched"] == 2


def test_scheduler_mode_skips_legacy_phases(monkeypatch):
    # Build a worker instance without running QThread.__init__
    from cross_directory_enricher import CrossDirectoryEnricherWorker
    monkeypatch.setattr(CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    worker = CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()

    calls = {"dir": 0, "ig": 0, "sc": 0, "lf": 0, "fb": 0, "sched": 0}

    monkeypatch.setattr(worker, "_phase_directory_matching", lambda *a, **k: calls.__setitem__("dir", calls["dir"] + 1))
    monkeypatch.setattr(worker, "_phase_instagram_email", lambda *a, **k: calls.__setitem__("ig", calls["ig"] + 1))
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
    assert calls["ig"] == 1
    assert calls["sched"] == 1
    assert calls["sc"] == 0
    assert calls["lf"] == 0
    assert calls["fb"] == 0


def test_interleaved_fb_run_maps_login_wall_to_retry_later(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    captured = {}

    class FakeScheduler:
        def __init__(self, sources, row_label=None, log_fn=None, short_circuit_fn=None):
            captured["sources"] = list(sources)

        def run(self):
            return {}

    monkeypatch.setattr(cde, "SourceDiversityScheduler", FakeScheduler)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = False
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker._fb_discovery_attempted_rows = set()
    worker._domain_email_reuse_rows = set()
    worker._build_row_context = lambda *args, **kwargs: {"artist": "Artist X", "position": 1, "total": 1}
    worker._maybe_apply_domain_email_reuse = lambda *args, **kwargs: False
    worker._init_row_enrichment_state = lambda: None

    def fake_enrich_row_facebook(seed_df, row_idx, fb_driver, ctx):
        seed_df.at[row_idx, "FB_Status"] = "login_wall"
        return False

    worker._enrich_row_facebook = fake_enrich_row_facebook

    seed_df = pd.DataFrame([{"Artist Name": "Artist X", "FB_Status": ""}], index=[0])
    worker._run_interleaved_sources(seed_df, fb_driver=object(), total=1)

    fb_source = next(spec for spec in captured["sources"] if spec.name == "FB")
    result = fb_source.run_row(0)

    assert result.attempted is True
    assert result.enriched is False
    assert result.retry_later is True


def test_email_summary_resets_per_cross_directory_run(tmp_path, monkeypatch):
    import cross_directory_enricher as cde
    import pipeline_runner

    seed_csv = tmp_path / "missing.csv"
    output_one = tmp_path / "out_one.csv"
    output_two = tmp_path / "out_two.csv"

    def _make_worker(output_path):
        worker = cde.CrossDirectoryEnricherWorker(seed_csv.as_posix(), output_path.as_posix(), enable_live_search=False)
        worker.log_message = type("obj", (), {"emit": lambda *args, **kwargs: None})
        worker.progress = type("obj", (), {"emit": lambda *args, **kwargs: None})
        worker.finished = type("obj", (), {"emit": lambda *args, **kwargs: None})
        return worker

    pipeline_runner.increment_pattern_emails(7)
    pipeline_runner.record_email_summary_row_change(
        {"Email": "", "Email_All": ""},
        {"Email": "stale@example.com", "Email_All": "stale@example.com"},
    )

    worker_one = _make_worker(output_one)
    worker_one._run_impl()
    assert pipeline_runner.get_email_summary_counts() == {"emails_found": 0, "pattern_emails": 0}

    pipeline_runner.increment_pattern_emails(4)
    pipeline_runner.record_email_summary_row_change(
        {"Email": "", "Email_All": ""},
        {"Email": "carry@example.com", "Email_All": "carry@example.com"},
    )
    worker_two = _make_worker(output_two)
    worker_two._run_impl()
    assert pipeline_runner.get_email_summary_counts() == {"emails_found": 0, "pattern_emails": 0}


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


def test_opportunity_skips_soundcloud_when_url_present():
    rows = [0]
    row_data = {0: {"Artist Name": "Test Artist", "soundcloud_url": "https://soundcloud.com/test"}}
    sc_calls = []

    sc_spec = SourceSpec(
        name="SC",
        rows=rows,
        run_row=lambda idx: (sc_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([sc_spec], row_label=str).run()

    assert not sc_calls
    assert summary["SC"]["attempted"] == 0
    assert summary["SC"]["skipped_opportunity"] == 1


def test_opportunity_allows_lastfm_when_missing_url():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "lastfm_url": ""}}
    calls = []

    lf_spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: (calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([lf_spec], row_label=str).run()

    assert calls == [0]
    assert summary["LF"]["attempted"] == 1
    assert summary["LF"]["skipped_opportunity"] == 0


def test_opportunity_runs_facebook_when_url_missing_but_artist_present():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": ""}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_true_when_social_link_contains_facebook():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "https://www.facebook.com/dizzydaysband", "Email": ""}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_after_promotion_from_social_link():
    rows = [0]
    row = {"Artist Name": "Artist X", "facebook_url": "", "Social Link": "https://www.facebook.com/dizzydaysband"}
    promote_facebook_url(row)
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row,
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert row.get("facebook_url") == "https://www.facebook.com/dizzydaysband"
    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_true_with_artist_name_even_without_explicit_facebook_url():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "Social Link": "https://www.instagram.com/xxx"}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_false_when_email_present():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "https://facebook.com/artist", "Email": "x@test.com"}}
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert not fb_calls
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1


def test_fb_opportunity_false_without_artist_or_explicit_facebook_url():
    rows = [0]
    row_data = {0: {"Artist Name": "", "Social Link": "https://www.instagram.com/xxx", "Email": ""}}
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert not fb_calls
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1


def test_fb_opportunity_true_when_external_links_contains_facebook():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "http://m.facebook.com/dizzydays", "Email_All": ""}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_false_when_discovery_already_attempted_and_no_url():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "", "__fb_discovery_attempted_this_run": "1"}}
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert not fb_calls
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1


def test_fb_opportunity_true_with_explicit_url_even_if_discovery_attempted():
    rows = [0]
    row_data = {
        0: {
            "Artist Name": "Artist X",
            "facebook_url": "https://www.facebook.com/artistx",
            "__fb_discovery_attempted_this_run": "1",
        }
    }
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_failed_fb_discovery_does_not_starve_other_sources_or_change_interleaving(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    rows = [0]
    row_data = {
        0: {
            "Artist Name": "Artist A",
            "facebook_url": "",
            "__fb_discovery_attempted_this_run": "1",
            "soundcloud_url": "",
            "lastfm_url": "",
            "Email": "",
            "Email_All": "",
        }
    }
    calls: list[str] = []

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(
                name="SC",
                rows=rows,
                run_row=lambda idx: (calls.append(f"SC{idx}") or SourceResult(attempted=True)),
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
            SourceSpec(
                name="LF",
                rows=rows,
                run_row=lambda idx: (calls.append(f"LF{idx}") or SourceResult(attempted=True)),
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
            SourceSpec(
                name="FB",
                rows=rows,
                run_row=lambda idx: (calls.append(f"FB{idx}") or SourceResult(attempted=True)),
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
        ],
        row_label=str,
    )
    summary = scheduler.run()

    assert "FB0" not in calls
    assert set(calls) == {"SC0", "LF0"}
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1
    assert summary["SC"]["attempted"] == 1
    assert summary["LF"]["attempted"] == 1


def test_opportunity_weight_prioritises_sources(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    rows = [0]
    row_data = {
        0: {
            "Artist Name": "Artist A",
            "soundcloud_url": "https://soundcloud.com/test",  # no SC opportunity
            "facebook_url": "https://facebook.com/test",  # FB opportunity
        }
    }
    order: list[str] = []

    def sc_run(idx):
        order.append("SC")
        return SourceResult(attempted=True, enriched=False)

    def fb_run(idx):
        order.append("FB")
        return SourceResult(attempted=True, enriched=False)

    row_getter = lambda idx: row_data[idx]
    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None), row_getter=row_getter),
            SourceSpec(name="FB", rows=rows, run_row=fb_run, is_available=lambda: (True, None), row_getter=row_getter),
        ],
        row_label=str,
    )
    summary = scheduler.run()

    assert order[0] == "FB"
    assert summary["SC"]["skipped_opportunity"] == 1


def test_short_circuit_stops_remaining_sources(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    rows = [0]
    row_data = {0: {"Artist Name": "Artist A", "Email": "", "Email_All": ""}}
    calls = {"SC": 0, "LF": 0}

    def sc_run(idx):
        calls["SC"] += 1
        row_data[idx]["Email"] = "artist@example.com"
        return SourceResult(attempted=True, enriched=True)

    def lf_run(idx):
        calls["LF"] += 1
        return SourceResult(attempted=True, enriched=True)

    row_getter = lambda idx: row_data[idx]

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None), row_getter=row_getter),
            SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lambda: (True, None), row_getter=row_getter),
        ],
        row_label=str,
        short_circuit_fn=lambda row: __import__("pipeline_runner").has_contact_email_for_short_circuit(row),
    )
    scheduler.run()

    assert calls["SC"] == 1
    assert calls["LF"] == 0


def test_scheduler_opportunity_fallback_on_row_error():
    rows = [0]
    calls = []

    def row_getter(_):
        raise RuntimeError("row failure")

    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: (calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=row_getter,
    )

    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert calls == [0]
    assert summary["LF"]["attempted"] == 1
