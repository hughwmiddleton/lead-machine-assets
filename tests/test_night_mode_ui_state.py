"""Focused regression tests for Night Mode GUI progress/log-streaming fixes.

These tests verify that:
- stale global progress cannot override active Night Mode state
- job_status.json drives current-job identity truthfully
- Bandcamp heartbeat counts are accurate
- missing/malformed JSON fails safely
- the phased runner emits the run-dir handshake
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# Import the GUI module under a short alias so helpers are accessible.
spec = __import__("importlib.util").util.spec_from_file_location(
    "lm_gui", "Lead Machine (Final Update 5).py"
)
lm_gui = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(lm_gui)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# 1. Stale global progress must not infect active Night Mode UI
# ---------------------------------------------------------------------------


def test_stale_global_progress_ignored_when_worker_active():
    """A stale global run_complete must not appear beneath an active run.

    We verify at the helper level: when no job status is available yet,
    the display is indeterminate and never fabricates old row counts,
    percentages, or ETAs.
    """
    with tempfile.TemporaryDirectory() as run_dir:
        jobs = [{"job_id": "j1", "directory": "bandcamp", "target_valid_leads": 5}]
        result = lm_gui._build_night_mode_progress_display(
            jobs, run_dir, [{}], bandcamp_progress=None
        )
        # Must be indeterminate; must not contain fabricated completion data.
        assert result["indeterminate"] is True
        assert result["status_text"] == "processing"
        assert result["row_count"] is None
        assert result["heartbeat"] is None


# ---------------------------------------------------------------------------
# 2. job_status.json with status=running resolves correct source + Job N of M
# ---------------------------------------------------------------------------


def test_job_status_running_resolves_source_and_job_number():
    with tempfile.TemporaryDirectory() as run_dir:
        jobs = [
            {"job_id": "job_bandcamp_1", "directory": "bandcamp", "target_valid_leads": 5},
            {"job_id": "job_soundcloud_1", "directory": "soundcloud", "target_valid_leads": 10},
        ]
        _write_json(
            os.path.join(run_dir, "job_bandcamp_1", "job_status.json"),
            {"job_id": "job_bandcamp_1", "status": "running", "row_count": 0},
        )
        _write_json(
            os.path.join(run_dir, "job_soundcloud_1", "job_status.json"),
            {"job_id": "job_soundcloud_1", "status": "pending", "row_count": 0},
        )

        result = lm_gui._build_night_mode_progress_display(
            jobs, run_dir, [
                {"status": "running", "row_count": 0},
                {"status": "pending", "row_count": 0},
            ]
        )

        assert result["source"] == "bandcamp"
        assert result["job_index"] == 1
        assert result["total_jobs"] == 2
        assert result["status_text"] == "processing"


# ---------------------------------------------------------------------------
# 3. target_valid_leads shown without fabricating current valid-lead count
# ---------------------------------------------------------------------------


def test_target_valid_leads_exposed_without_fabricating_count():
    with tempfile.TemporaryDirectory() as run_dir:
        jobs = [
            {"job_id": "j1", "directory": "bandcamp", "target_valid_leads": 7},
        ]
        _write_json(
            os.path.join(run_dir, "j1", "job_status.json"),
            {"job_id": "j1", "status": "running", "row_count": 0},
        )

        result = lm_gui._build_night_mode_progress_display(
            jobs, run_dir, [{"status": "running", "row_count": 0}]
        )

        assert result["target_valid_leads"] == 7
        # row_count is real; we must not invent a valid-lead count.
        assert result["row_count"] == 0


# ---------------------------------------------------------------------------
# 4. Bandcamp unique URL counting across checkpoint aliases
# ---------------------------------------------------------------------------


def test_bandcamp_unique_urls_no_double_count():
    progress = {
        "discover:rock": {
            "scraped_artist_urls": [
                "https://a.bandcamp.com",
                "https://b.bandcamp.com",
            ]
        },
        "rock": {
            "scraped_artist_urls": [
                "https://a.bandcamp.com",  # duplicate alias
                "https://c.bandcamp.com",
            ]
        },
    }
    assert lm_gui._count_unique_bandcamp_urls(progress) == 3


def test_bandcamp_progress_empty_returns_none():
    assert lm_gui._count_unique_bandcamp_urls({}) is None
    assert lm_gui._count_unique_bandcamp_urls(None) is None


def test_bandcamp_heartbeat_in_display():
    with tempfile.TemporaryDirectory() as run_dir:
        jobs = [{"job_id": "j1", "directory": "bandcamp"}]
        _write_json(
            os.path.join(run_dir, "j1", "job_status.json"),
            {"status": "running"},
        )
        _write_json(
            os.path.join(run_dir, "j1", "bandcamp_progress.json"),
            {
                "k1": {"scraped_artist_urls": ["https://a.bc", "https://b.bc"]},
                "k2": {"scraped_artist_urls": ["https://a.bc", "https://c.bc"]},
            },
        )

        bc = lm_gui._read_json_safe(os.path.join(run_dir, "j1", "bandcamp_progress.json"))
        result = lm_gui._build_night_mode_progress_display(
            jobs, run_dir, [{"status": "running"}], bandcamp_progress=bc
        )

        assert result["heartbeat"] == "Profiles checked: 3"


# ---------------------------------------------------------------------------
# 5. Missing / malformed JSON fails safely and leaves UI indeterminate
# ---------------------------------------------------------------------------


def test_missing_job_status_indeterminate():
    with tempfile.TemporaryDirectory() as run_dir:
        jobs = [{"job_id": "j1", "directory": "bandcamp"}]
        result = lm_gui._build_night_mode_progress_display(
            jobs, run_dir, [{}], bandcamp_progress=None
        )
        assert result["indeterminate"] is True
        assert result["status_text"] == "processing"


def test_malformed_json_returns_none_safely():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert lm_gui._read_json_safe(path) is None


def test_malformed_bandcamp_progress_ignored():
    with tempfile.TemporaryDirectory() as run_dir:
        jobs = [{"job_id": "j1", "directory": "bandcamp"}]
        _write_json(
            os.path.join(run_dir, "j1", "job_status.json"),
            {"status": "running"},
        )
        bad_path = os.path.join(run_dir, "j1", "bandcamp_progress.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("garbage")

        bc = lm_gui._read_json_safe(bad_path)
        result = lm_gui._build_night_mode_progress_display(
            jobs, run_dir, [{"status": "running"}], bandcamp_progress=bc
        )
        assert result["heartbeat"] is None
        assert result["indeterminate"] is True


# ---------------------------------------------------------------------------
# 6. Phased runner emits run-dir handshake
# ---------------------------------------------------------------------------


def test_phased_runner_emits_run_dir_handshake(capsys):
    import night_mode_v2.phased_runner as runner

    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"jobs": []}, f)

        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir, exist_ok=True)

        with patch.object(runner.night_mode_runner, "_ensure_run_dir", return_value=(run_dir, None)):
            with patch.object(runner, "run_seed_phase", return_value={}):
                with patch.object(runner, "run_enrich_phase", return_value={}):
                    with patch.object(runner, "run_contact_phase", return_value={}):
                        runner.run_phased_night_mode(config_path, run_root=tmp)

        captured = capsys.readouterr()
        expected = f"[Night Mode][Runtime] run_dir={os.path.abspath(run_dir)}"
        assert expected in captured.out


# ---------------------------------------------------------------------------
# Handshake parser
# ---------------------------------------------------------------------------


def test_parse_handshake_line():
    assert lm_gui._parse_night_mode_run_dir_handshake(
        "[Night Mode][Runtime] run_dir=/tmp/overnight_runs/2026-01-01_120000"
    ) == "/tmp/overnight_runs/2026-01-01_120000"


def test_parse_handshake_line_no_match():
    assert lm_gui._parse_night_mode_run_dir_handshake("some other log line") is None
