import os
from pathlib import Path

import pandas as pd
import pytest

import pipeline_runner


def test_safe_atomic_write_creates_header_only(tmp_path: Path) -> None:
    final_path = tmp_path / "raw.csv"
    result = pipeline_runner._safe_atomic_write_csv(
        pd.DataFrame(columns=["Artist Name", "Email"]),
        final_path.as_posix(),
        ["Artist Name", "Email"],
        reason="unit-test",
    )

    assert final_path.exists()
    assert not (tmp_path / "raw.tmp.csv").exists()
    assert result.raw_bytes > 0
    assert result.row_count == 0

    with open(final_path, "r", encoding="utf-8-sig") as handle:
        header = handle.readline().strip()
    assert header == "Artist Name,Email"


def test_safe_atomic_write_cleans_tmp_on_failure(monkeypatch, tmp_path: Path) -> None:
    final_path = tmp_path / "raw.csv"
    tmp_file = tmp_path / "raw.tmp.csv"

    def boom(src, dst):  # type: ignore[override]
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        pipeline_runner._safe_atomic_write_csv(pd.DataFrame({"Email": ["a@example.com"]}), final_path.as_posix(), ["Email"])

    assert not tmp_file.exists()


def test_safe_atomic_write_respects_keep_tmp(monkeypatch, tmp_path: Path) -> None:
    final_path = tmp_path / "raw.csv"
    tmp_file = tmp_path / "raw.tmp.csv"

    def boom(src, dst):  # type: ignore[override]
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setenv("KEEP_TMP_ON_FAILURE", "1")

    with pytest.raises(OSError):
        pipeline_runner._safe_atomic_write_csv(pd.DataFrame({"Email": ["b@example.com"]}), final_path.as_posix(), ["Email"])

    assert tmp_file.exists()


def test_run_directory_job_empty_rows_atomic(monkeypatch, tmp_path: Path) -> None:
    class FakeModule:
        def scrape_spotify(self, target_count, params, logger=None):
            return []  # no rows

    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: FakeModule())

    raw_path = tmp_path / "raw.csv"
    result_path = pipeline_runner.run_directory_job({"directory": "spotify", "job_id": "job_spotify_1"}, raw_path.as_posix())

    assert Path(result_path).exists()
    assert raw_path.stat().st_size > 0  # header-only still non-zero bytes
    # Ensure no temporary CSV artifacts remain
    tmp_patterns = ["*.tmp.csv", "*.csv.tmp", "raw*.tmp*.csv"]
    for pattern in tmp_patterns:
        assert not list(tmp_path.glob(pattern)), f"Found leftover tmp files for pattern {pattern}"

    header = raw_path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "Artist Name" in header
    assert "Source Directory" in header
