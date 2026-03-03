import json
from pathlib import Path

import pytest

from night_mode_v2.phased_runner import run_seed_phase
import pipeline_runner


def _write_zero_row_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Artist Name,Email\n", encoding="utf-8")


def test_seed_job_zero_rows_marked_completed(monkeypatch, tmp_path: Path) -> None:
    """A successful scrape that returns zero rows should be classified as completed."""

    def fake_run_directory_job(job_config, raw_output_path, logger=None):  # type: ignore[override]
        _write_zero_row_csv(Path(raw_output_path))
        return raw_output_path

    monkeypatch.setattr(pipeline_runner, "run_directory_job", fake_run_directory_job)

    config = {
        "jobs": [
            {
                "job_id": "job_zero",
                "directory": "spotify",
                "search_term": "artist",
            }
        ]
    }

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    run_dir = tmp_path / "run"

    run_seed_phase(config_path.as_posix(), run_dir.as_posix())

    status_path = run_dir / "job_zero" / "job_status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))

    assert payload["status"] == "completed"
    assert payload["row_count"] == 0
    assert payload["raw_exists"] is True
    assert payload.get("error") is None
    assert payload.get("note") == "zero rows"


def test_seed_job_exception_marks_failed(monkeypatch, tmp_path: Path) -> None:
    """Real exceptions must still surface as failed jobs."""

    def boom(job_config, raw_output_path, logger=None):  # type: ignore[override]
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_runner, "run_directory_job", boom)

    config = {"jobs": [{"job_id": "job_boom", "directory": "spotify"}]}

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    run_dir = tmp_path / "run"

    run_seed_phase(config_path.as_posix(), run_dir.as_posix())

    status_path = run_dir / "job_boom" / "job_status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))

    assert payload["status"] == "failed"
    assert payload["row_count"] == 0
    assert payload["raw_exists"] is False
    assert "RuntimeError" in payload.get("error", "")
