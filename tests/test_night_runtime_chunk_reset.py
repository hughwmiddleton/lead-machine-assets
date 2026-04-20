import json
import shutil
from types import SimpleNamespace

import pandas as pd

import cross_directory_enricher as cde
import night_mode_fb as nmfb
import night_mode_runner
import pipeline_runner


class _DummyClosable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DummyPlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _make_worker(monkeypatch):
    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *args, **kwargs: None)
    worker = cde.CrossDirectoryEnricherWorker(None, None)
    logs = []
    worker.night_mode = True
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(str(msg)))
    return worker, logs


def test_runtime_chunk_boundary_resets_after_completed_rows_without_reprocessing(monkeypatch):
    worker, logs = _make_worker(monkeypatch)
    worker.night_runtime_reset_interval_rows = 2

    seed_df = pd.DataFrame(
        [{"Artist Name": f"Artist {idx}", "marker": ""} for idx in range(5)],
        dtype=str,
    ).fillna("")

    attempts = {}
    chunk_calls = []
    reset_calls = []

    def fake_run_row_linear(df, directory_indexes, priority, fb_driver, total, row_ids=None):
        active_rows = list(row_ids or [])
        chunk_calls.append((active_rows, fb_driver))
        for row_idx in active_rows:
            attempts[row_idx] = attempts.get(row_idx, 0) + 1
            assert attempts[row_idx] == 1
            df.at[row_idx, "marker"] = f"done-{row_idx}"

    def fake_reset_chunk(*, interval_rows, completed_rows, next_row_id, next_row_index):
        reset_calls.append(
            {
                "interval_rows": interval_rows,
                "completed_rows": completed_rows,
                "next_row_id": next_row_id,
                "next_row_index": next_row_index,
            }
        )
        return f"driver_{len(reset_calls)}"

    worker._run_row_linear = fake_run_row_linear
    worker._reset_night_runtime_chunk = fake_reset_chunk

    worker._run_with_night_runtime_chunks(
        seed_df,
        directory_indexes={},
        priority=[],
        fb_driver="driver_0",
        total=len(seed_df.index),
        enrichment_mode="row_linear",
    )

    assert chunk_calls == [
        ([0, 1], "driver_0"),
        ([2, 3], "driver_1"),
        ([4], "driver_2"),
    ]
    assert reset_calls == [
        {
            "interval_rows": 2,
            "completed_rows": 2,
            "next_row_id": 2,
            "next_row_index": 3,
        },
        {
            "interval_rows": 2,
            "completed_rows": 4,
            "next_row_id": 4,
            "next_row_index": 5,
        },
    ]
    assert seed_df["marker"].tolist() == ["done-0", "done-1", "done-2", "done-3", "done-4"]
    assert any("completed_rows=2 boundary_reached=1" in line for line in logs)
    assert any("completed_rows=4 boundary_reached=1" in line for line in logs)
    assert any("completed_rows=5 boundary_reached=0" in line for line in logs)


def test_runtime_reset_interval_normalization_defaults_and_invalid_values(monkeypatch):
    worker, _logs = _make_worker(monkeypatch)

    worker.night_runtime_reset_interval_rows = None
    assert worker._resolve_night_runtime_reset_interval_rows() == 50

    worker.night_runtime_reset_interval_rows = ""
    assert worker._resolve_night_runtime_reset_interval_rows() == 0

    worker.night_runtime_reset_interval_rows = "invalid"
    assert worker._resolve_night_runtime_reset_interval_rows() == 0

    worker.night_runtime_reset_interval_rows = -5
    assert worker._resolve_night_runtime_reset_interval_rows() == 0


def test_runtime_chunk_reset_disabled_preserves_single_pass(monkeypatch):
    worker, logs = _make_worker(monkeypatch)
    worker.night_runtime_reset_interval_rows = 0

    seed_df = pd.DataFrame(
        [{"Artist Name": f"Artist {idx}"} for idx in range(3)],
        dtype=str,
    ).fillna("")

    chunk_calls = []

    def fake_run_source_phased(df, directory_indexes, priority, fb_driver, total, row_ids=None):
        chunk_calls.append(list(row_ids or []))

    worker._run_source_phased = fake_run_source_phased
    worker._reset_night_runtime_chunk = lambda **kwargs: (_ for _ in ()).throw(AssertionError("reset should be disabled"))

    worker._run_with_night_runtime_chunks(
        seed_df,
        directory_indexes={},
        priority=[],
        fb_driver="driver_0",
        total=len(seed_df.index),
        enrichment_mode="source_phased",
    )

    assert chunk_calls == [[0, 1, 2]]
    assert any("interval_rows=0" in line and "enabled=0" in line for line in logs)


def test_runtime_chunk_reset_tears_down_fb_and_ig_runtime_state(monkeypatch):
    worker, logs = _make_worker(monkeypatch)
    worker.night_runtime_reset_interval_rows = 2
    worker._fb_discovery_attempted_rows = {0, 1}
    worker._fb_discovery_disabled = True
    worker._fb_discovery_disabled_reason = "checkpoint"
    worker._fb_discovery_disable_logged = True
    worker._fb_session_auth_checked = True
    worker._fb_session_authenticated = True
    worker._fb_session_warmup_complete = True
    worker._initial_fb_session_warmup_complete = True
    worker._fb_session_auth_reason = "checkpoint"
    worker._fb_session_invalid = True
    worker._instagram_hidden_contact_attempt_keys = {(123, 0)}

    run_state = nmfb.create_night_fb_run_state()
    fb_session = _DummyClosable()
    run_state.session = fb_session
    run_state.disabled_for_run = True
    run_state.disable_reason = "checkpoint"
    run_state.checkpointed = True
    run_state.session_unhealthy = True
    run_state.session_invalid = True
    run_state.authenticated = True
    run_state.reusable = True
    run_state.session_owner = "cross_directory_enricher"
    run_state.session_warmup_complete = True
    run_state.trust_score = -4
    run_state.search_disabled_for_run = True
    run_state.search_disable_reason = "checkpoint"
    worker.night_fb_run_state = run_state

    ig_context = _DummyClosable()
    ig_browser = _DummyClosable()
    ig_playwright = _DummyPlaywright()
    monkeypatch.setitem(
        cde.html_fetcher._JOB_BROWSERS,
        "global",
        SimpleNamespace(
            context=ig_context,
            browser=ig_browser,
            playwright=ig_playwright,
            pages_used=3,
        ),
    )

    cleanup_calls = []
    monkeypatch.setattr(cde, "_cleanup_enricher_facebook_driver", lambda: cleanup_calls.append("cleanup"))
    worker._recreate_night_runtime_for_chunk = lambda: "driver_fresh"

    result = worker._reset_night_runtime_chunk(
        interval_rows=2,
        completed_rows=2,
        next_row_id=2,
        next_row_index=3,
    )

    assert result == "driver_fresh"
    assert cleanup_calls == ["cleanup"]
    assert fb_session.closed is True
    assert run_state.session is None
    assert run_state.disabled_for_run is False
    assert run_state.disable_reason == ""
    assert run_state.checkpointed is False
    assert run_state.session_unhealthy is False
    assert run_state.session_invalid is False
    assert run_state.session_warmup_complete is False
    assert run_state.trust_score == 0
    assert run_state.search_disabled_for_run is False
    assert run_state.search_disable_reason == ""
    assert worker._fb_discovery_attempted_rows == set()
    assert worker._fb_discovery_disabled is False
    assert worker._fb_session_auth_checked is False
    assert worker._fb_session_authenticated is False
    assert worker._fb_session_warmup_complete is False
    assert worker._initial_fb_session_warmup_complete is False
    assert worker._fb_session_auth_reason == ""
    assert worker._fb_session_invalid is False
    assert worker._instagram_hidden_contact_attempt_keys == set()
    assert "global" not in cde.html_fetcher._JOB_BROWSERS
    assert ig_context.closed is True
    assert ig_browser.closed is True
    assert ig_playwright.stopped is True
    assert any("teardown_start=1" in line for line in logs)
    assert any("teardown_complete=1" in line for line in logs)
    assert any("recreate_start=1" in line for line in logs)
    assert any("recreate_complete=1" in line for line in logs)


def test_night_mode_runner_passes_runtime_reset_interval_to_master_enrichment(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    run_root = tmp_path / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    config = {
        "export_mode": "both",
        "master_enrichment": {
            "enabled": True,
            "night_runtime_reset_interval_rows": 7,
        },
        "jobs": [
            {
                "job_id": "job_one",
                "directory": "spotify",
                "target_valid_leads": 1,
            }
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    captured = {}

    def fake_run_directory_job(job_config, raw_output_path, logger=None):
        pd.DataFrame(
            [{"Artist Name": "Artist One", "Email": "artist@example.com"}],
            dtype=str,
        ).to_csv(raw_output_path, index=False)
        return raw_output_path

    def fake_run_master_enrichment(
        input_csv,
        output_csv,
        logger=None,
        enable_live_search=True,
        max_live_searches=None,
        night_mode=False,
        night_runtime_reset_interval_rows=None,
        **kwargs,
    ):
        captured["interval"] = night_runtime_reset_interval_rows
        shutil.copyfile(input_csv, output_csv)
        return output_csv

    def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None, night_mode=False):
        shutil.copyfile(raw_csv_path, enriched_output_path)
        return enriched_output_path

    def fake_fb_pass(input_csv, output_csv, state_path, max_rows_per_run=100, **kwargs):
        shutil.copyfile(input_csv, output_csv)
        return pipeline_runner.FacebookGlobalPassStatus(
            processed_rows=1,
            total_rows=1,
            completed=True,
            hit_captcha=False,
            limit_reached=False,
            attempted_total=1,
        )

    monkeypatch.setattr(night_mode_runner, "run_directory_job", fake_run_directory_job)
    monkeypatch.setattr(night_mode_runner, "run_master_enrichment", fake_run_master_enrichment)
    monkeypatch.setattr(night_mode_runner, "run_enrichment", fake_run_enrichment)
    monkeypatch.setattr(night_mode_runner, "run_facebook_global_pass_nightmode", fake_fb_pass)
    monkeypatch.setattr(
        night_mode_runner.pipeline_runner,
        "export_master_leads",
        lambda input_csv, output_csv, logger=None, export_profile=None: shutil.copyfile(input_csv, output_csv),
    )
    monkeypatch.setattr(night_mode_runner, "preview_csv_merge_counts", lambda *args, **kwargs: {"rows_added": 0, "rows_updated": 0})

    night_mode_runner.run_night_mode(config_path.as_posix(), run_root=run_root.as_posix())

    assert captured["interval"] == 7


def _chunk_lines(logs, prefix):
    return [line for line in logs if str(line).startswith(prefix)]


def test_chunk_yield_summary_emits_boundary_and_partial_final_chunk(monkeypatch):
    worker, logs = _make_worker(monkeypatch)
    worker.night_runtime_reset_interval_rows = 2

    seed_df = pd.DataFrame(
        [{"Artist Name": f"Artist {idx}"} for idx in range(5)],
        dtype=str,
    ).fillna("")

    def fake_run_row_linear(df, directory_indexes, priority, fb_driver, total, row_ids=None):
        active_rows = list(row_ids or [])
        for row_idx in active_rows:
            worker._record_chunk_source_opportunity("facebook", row_idx)
            worker._record_chunk_source_attempt("facebook", row_idx)
            if row_idx in {0, 2, 4}:
                worker._record_chunk_source_found("facebook", row_idx, [f"fb{row_idx}@example.com"])
                worker._record_chunk_source_written(
                    "facebook",
                    row_idx,
                    before_row={"Email": "", "Email_All": ""},
                    after_row={
                        "Email": "",
                        "Email_All": f"fb{row_idx}@example.com",
                        "__fb_emails_applied": f"fb{row_idx}@example.com",
                    },
                    found_emails=[f"fb{row_idx}@example.com"],
                )
            if row_idx in {1, 4}:
                worker._record_chunk_source_opportunity("instagram", row_idx)
                worker._record_chunk_source_attempt("instagram", row_idx)
                worker._record_chunk_source_found("instagram", row_idx, [f"ig{row_idx}@example.com"])
                worker._record_chunk_source_written(
                    "instagram",
                    row_idx,
                    before_row={"Email": "", "Email_All": ""},
                    after_row={"Email": "", "Email_All": f"ig{row_idx}@example.com"},
                    found_emails=[f"ig{row_idx}@example.com"],
                )

    worker._run_row_linear = fake_run_row_linear
    worker._reset_night_runtime_chunk = lambda **kwargs: "driver_reset"

    worker._run_with_night_runtime_chunks(
        seed_df,
        directory_indexes={},
        priority=[],
        fb_driver="driver_0",
        total=len(seed_df.index),
        enrichment_mode="row_linear",
    )

    summary_lines = _chunk_lines(logs, "[Chunk Yield] ")
    assert summary_lines == [
        "[Chunk Yield] chunk_index=1 row_start_index=0 row_end_index=1 rows_in_chunk=2 configured_interval=2 chunk_end_reason=reset_boundary",
        "[Chunk Yield] chunk_index=2 row_start_index=2 row_end_index=3 rows_in_chunk=2 configured_interval=2 chunk_end_reason=reset_boundary",
        "[Chunk Yield] chunk_index=3 row_start_index=4 row_end_index=4 rows_in_chunk=1 configured_interval=2 chunk_end_reason=end_of_run",
    ]
    assert _chunk_lines(logs, "[Chunk Yield][FB] ") == [
        "[Chunk Yield][FB] chunk_index=1 fb_opportunity_rows=2 fb_opportunity_present=1 fb_attempted_rows=2 fb_email_found_rows=1 fb_email_written_rows=1",
        "[Chunk Yield][FB] chunk_index=2 fb_opportunity_rows=2 fb_opportunity_present=1 fb_attempted_rows=2 fb_email_found_rows=1 fb_email_written_rows=1",
        "[Chunk Yield][FB] chunk_index=3 fb_opportunity_rows=1 fb_opportunity_present=1 fb_attempted_rows=1 fb_email_found_rows=1 fb_email_written_rows=1",
    ]
    assert _chunk_lines(logs, "[Chunk Yield][IG] ") == [
        "[Chunk Yield][IG] chunk_index=1 ig_opportunity_rows=1 ig_opportunity_present=1 ig_attempted_rows=1 ig_email_found_rows=1 ig_email_written_rows=1",
        "[Chunk Yield][IG] chunk_index=2 ig_opportunity_rows=0 ig_opportunity_present=0 ig_attempted_rows=0 ig_email_found_rows=0 ig_email_written_rows=0",
        "[Chunk Yield][IG] chunk_index=3 ig_opportunity_rows=1 ig_opportunity_present=1 ig_attempted_rows=1 ig_email_found_rows=1 ig_email_written_rows=1",
    ]


def test_chunk_yield_fb_metrics_use_applied_marker_for_written_rows(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0, 1, 2], configured_interval=3)

    worker._record_chunk_source_opportunity("facebook", 0)
    worker._record_chunk_source_attempt("facebook", 0)
    worker._record_chunk_source_found("facebook", 0, ["found0@example.com"])
    worker._record_chunk_source_written(
        "facebook",
        0,
        before_row={"Email": "", "Email_All": ""},
        after_row={
            "Email": "",
            "Email_All": "found0@example.com",
            "__fb_emails_applied": "found0@example.com",
        },
        found_emails=["found0@example.com"],
    )

    worker._record_chunk_source_opportunity("facebook", 1)
    worker._record_chunk_source_attempt("facebook", 1)
    worker._record_chunk_source_found("facebook", 1, ["found1@example.com"])
    worker._record_chunk_source_written(
        "facebook",
        1,
        before_row={"Email": "", "Email_All": ""},
        after_row={
            "Email": "",
            "Email_All": "found1@example.com",
            "__fb_emails_applied": "",
        },
        found_emails=["found1@example.com"],
    )

    worker._record_chunk_source_opportunity("facebook", 2)
    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield][FB] ") == [
        "[Chunk Yield][FB] chunk_index=1 fb_opportunity_rows=3 fb_opportunity_present=1 fb_attempted_rows=2 fb_email_found_rows=2 fb_email_written_rows=1"
    ]


def test_chunk_yield_fb_attempted_rows_require_execution_seam(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0, 1], configured_interval=2)

    worker._record_chunk_source_opportunity("facebook", 0)
    worker._record_chunk_source_opportunity("facebook", 1)
    worker._record_chunk_source_attempt("facebook", 1, seam="page_fetch_execution")
    worker._record_chunk_source_found("facebook", 1, ["fb1@example.com"])

    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield][FB] ") == [
        "[Chunk Yield][FB] chunk_index=1 fb_opportunity_rows=2 fb_opportunity_present=1 fb_attempted_rows=1 fb_email_found_rows=1 fb_email_written_rows=0"
    ]


def test_chunk_yield_fb_attempted_rows_dedupe_multiple_execution_seams(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0], configured_interval=1)

    worker._record_chunk_source_opportunity("facebook", 0)
    worker._record_chunk_source_attempt("facebook", 0, seam="discovery_execution")
    worker._record_chunk_source_attempt("facebook", 0, seam="page_fetch_execution")
    worker._record_chunk_source_found("facebook", 0, ["fb0@example.com"])

    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield][FB] ") == [
        "[Chunk Yield][FB] chunk_index=1 fb_opportunity_rows=1 fb_opportunity_present=1 fb_attempted_rows=1 fb_email_found_rows=1 fb_email_written_rows=0"
    ]


def test_chunk_yield_ig_metrics_require_committed_delta_intersection(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0, 1, 2], configured_interval=3)

    worker._record_chunk_source_opportunity("instagram", 0)
    worker._record_chunk_source_attempt("instagram", 0, seam="profile_fetch")
    worker._record_chunk_source_found("instagram", 0, ["ig0@example.com"])
    worker._record_chunk_source_written(
        "instagram",
        0,
        before_row={"Email": "", "Email_All": ""},
        after_row={"Email": "", "Email_All": "ig0@example.com"},
        found_emails=["ig0@example.com"],
    )

    worker._record_chunk_source_opportunity("instagram", 1)
    worker._record_chunk_source_attempt("instagram", 1, seam="profile_fetch")
    worker._record_chunk_source_found("instagram", 1, ["ig1@example.com"])
    worker._record_chunk_source_written(
        "instagram",
        1,
        before_row={"Email": "", "Email_All": ""},
        after_row={"Email": "", "Email_All": "other-source@example.com"},
        found_emails=["ig1@example.com"],
    )

    worker._record_chunk_source_opportunity("instagram", 2)
    worker._record_chunk_source_attempt("instagram", 2, seam="profile_fetch")
    worker._record_chunk_source_found("instagram", 2, ["ig2@example.com"])
    worker._record_chunk_source_written(
        "instagram",
        2,
        before_row={"Email": "", "Email_All": "other-source@example.com"},
        after_row={"Email": "", "Email_All": "other-source@example.com;ig2@example.com"},
        found_emails=["ig2@example.com"],
    )

    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield][IG] ") == [
        "[Chunk Yield][IG] chunk_index=1 ig_opportunity_rows=3 ig_opportunity_present=1 ig_attempted_rows=3 ig_email_found_rows=3 ig_email_written_rows=2"
    ]


def test_chunk_yield_ig_written_rows_ignore_non_intersecting_deltas(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0], configured_interval=1)

    worker._record_chunk_source_opportunity("instagram", 0)
    worker._record_chunk_source_attempt("instagram", 0, seam="profile_fetch")
    worker._record_chunk_source_found("instagram", 0, ["ig0@example.com"])
    worker._record_chunk_source_written(
        "instagram",
        0,
        before_row={"Email": "", "Email_All": ""},
        after_row={"Email": "other@example.com", "Email_All": "other@example.com"},
        found_emails=["ig0@example.com"],
    )

    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield][IG] ") == [
        "[Chunk Yield][IG] chunk_index=1 ig_opportunity_rows=1 ig_opportunity_present=1 ig_attempted_rows=1 ig_email_found_rows=1 ig_email_written_rows=0"
    ]


def test_chunk_yield_ig_written_rows_ignore_noop_delta(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0], configured_interval=1)

    worker._record_chunk_source_opportunity("instagram", 0)
    worker._record_chunk_source_attempt("instagram", 0, seam="profile_fetch")
    worker._record_chunk_source_found("instagram", 0, ["ig0@example.com"])
    worker._record_chunk_source_written(
        "instagram",
        0,
        before_row={"Email": "ig0@example.com", "Email_All": "ig0@example.com"},
        after_row={"Email": "ig0@example.com", "Email_All": "ig0@example.com"},
        found_emails=["ig0@example.com"],
    )

    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield][IG] ") == [
        "[Chunk Yield][IG] chunk_index=1 ig_opportunity_rows=1 ig_opportunity_present=1 ig_attempted_rows=1 ig_email_found_rows=1 ig_email_written_rows=0"
    ]


def test_chunk_yield_zero_opportunity_chunk_emits_presence_flags(monkeypatch):
    worker, logs = _make_worker(monkeypatch)

    worker._start_chunk_yield_window(chunk_index=1, active_row_ids=[0, 1], configured_interval=2)
    worker._emit_chunk_yield_summary(chunk_end_reason="end_of_run")

    assert _chunk_lines(logs, "[Chunk Yield] ") == [
        "[Chunk Yield] chunk_index=1 row_start_index=0 row_end_index=1 rows_in_chunk=2 configured_interval=2 chunk_end_reason=end_of_run"
    ]
    assert _chunk_lines(logs, "[Chunk Yield][FB] ") == [
        "[Chunk Yield][FB] chunk_index=1 fb_opportunity_rows=0 fb_opportunity_present=0 fb_attempted_rows=0 fb_email_found_rows=0 fb_email_written_rows=0"
    ]
    assert _chunk_lines(logs, "[Chunk Yield][IG] ") == [
        "[Chunk Yield][IG] chunk_index=1 ig_opportunity_rows=0 ig_opportunity_present=0 ig_attempted_rows=0 ig_email_found_rows=0 ig_email_written_rows=0"
    ]


def test_chunk_yield_email_normalization_handles_multi_value_fields():
    assert cde._normalized_email_set_from_values("a@x.com;b@y.com") == {
        "a@x.com",
        "b@y.com",
    }
    assert cde._normalized_email_set_from_values("A@X.COM ; b@y.com") == {
        "a@x.com",
        "b@y.com",
    }

    committed_delta, delta_intersection = cde._committed_row_email_delta_intersection(
        before_row={"Email": "", "Email_All": "a@x.com;b@y.com"},
        after_row={"Email": "", "Email_All": "A@X.COM ; b@y.com ; c@z.com"},
        found_emails=[" C@Z.COM ", "", "c@z.com"],
    )

    assert committed_delta == {"c@z.com"}
    assert delta_intersection == {"c@z.com"}
