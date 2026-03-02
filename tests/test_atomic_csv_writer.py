import os
import csv
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


def _write_stub_csv(path: str, rows: list[list[str]]) -> None:
    """Helper to emit a simple CSV for directory job fakes."""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Artist Name", "Email"])
        writer.writerows(rows)


def test_directory_job_bandcamp_finalizes(monkeypatch, tmp_path: Path) -> None:
    class FakeModule:
        def scrape_bandcamp(
            self,
            seed,
            pages_per_tag=None,
            existing_csv="",
            max_artists=None,
            progress_path=None,
            mode=None,
            max_pages=None,
            max_items=None,
            search_domain=None,
            search_location_filter=None,
        ):
            _write_stub_csv(existing_csv, [["Bandcamp Artist", "bc@example.com"]])

    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: FakeModule())

    raw_path = tmp_path / "raw.csv"
    result_path = pipeline_runner.run_directory_job(
        {"directory": "bandcamp", "job_id": "job_bandcamp_1"},
        raw_path.as_posix(),
        logger=None,
    )

    assert Path(result_path).exists()
    assert raw_path.exists()
    assert not (tmp_path / "raw.tmp.csv").exists()


def test_directory_job_soundcloud_finalizes(monkeypatch, tmp_path: Path) -> None:
    class FakeModule:
        def scrape_soundcloud(
            self,
            url,
            seed_tags=None,
            pages_per_tag=None,
            existing_csv="",
            max_artists=None,
            max_handles=None,
            min_yield=None,
            dry_run=False,
        ):
            _write_stub_csv(existing_csv, [["SC Artist", "sc@example.com"]])

    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: FakeModule())

    raw_path = tmp_path / "raw.csv"
    result_path = pipeline_runner.run_directory_job(
        {"directory": "soundcloud", "job_id": "job_soundcloud_1", "soundcloud_url": "https://soundcloud.com/foo"},
        raw_path.as_posix(),
        logger=None,
    )

    assert Path(result_path).exists()
    assert raw_path.exists()
    assert not (tmp_path / "raw.tmp.csv").exists()


def test_directory_job_unearthed_finalizes(monkeypatch, tmp_path: Path) -> None:
    def fake_run_unearthed(job_config, output_path, module, logger=None):
        _write_stub_csv(output_path, [["Unearthed Artist", "ue@example.com"]])
        return output_path

    class FakeModule:
        pass

    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: FakeModule())
    monkeypatch.setattr(pipeline_runner, "_run_unearthed_full_pipeline", fake_run_unearthed)

    raw_path = tmp_path / "raw.csv"
    result_path = pipeline_runner.run_directory_job(
        {"directory": "unearthed", "job_id": "job_unearthed_1", "search_term": "test"},
        raw_path.as_posix(),
        logger=None,
    )

    assert Path(result_path).exists()
    assert raw_path.exists()
    assert not (tmp_path / "raw.tmp.csv").exists()
