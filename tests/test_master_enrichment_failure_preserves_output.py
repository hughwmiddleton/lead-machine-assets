from pathlib import Path

import pandas as pd

import cross_directory_enricher as cde
import pipeline_runner


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _run_failing_master_enrichment(
    tmp_path: Path,
    monkeypatch,
    *,
    seed_rows: list[dict[str, str]],
    existing_output: str | None = None,
    write_before_failure: list[dict[str, str]] | None = None,
) -> tuple[Path, Path, list[str]]:
    seed_csv_path = tmp_path / "master_raw.csv"
    output_csv_path = tmp_path / "master_enriched.csv"
    _write_csv(seed_csv_path, seed_rows)
    if existing_output is not None:
        output_csv_path.write_text(existing_output, encoding="utf-8")

    logs: list[str] = []

    def fake_run_cross_directory_enrichment(seed_path, output_path, **kwargs):
        if write_before_failure is not None:
            _write_csv(Path(output_path), write_before_failure)
        raise RuntimeError("runtime canary failed late")

    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)
    monkeypatch.setattr(pipeline_runner.time, "sleep", lambda _: None)

    pipeline_runner.run_master_enrichment(
        seed_csv_path.as_posix(),
        output_csv_path.as_posix(),
        logger=logs.append,
        enable_live_search=False,
        max_live_searches=0,
        night_mode=False,
    )
    return seed_csv_path, output_csv_path, logs


def test_master_enrichment_failure_preserves_valid_enriched_output(tmp_path, monkeypatch):
    _, output_csv_path, logs = _run_failing_master_enrichment(
        tmp_path,
        monkeypatch,
        seed_rows=[{"Artist Name": "Raw Artist", "Email": ""}],
        write_before_failure=[{"Artist Name": "Enriched Artist", "Email": "ig@example.com"}],
    )

    output_text = output_csv_path.read_text(encoding="utf-8")
    assert "Enriched Artist" in output_text
    assert "ig@example.com" in output_text
    assert "Raw Artist" not in output_text
    assert any("preserved_existing_enriched_output" in msg for msg in logs)


def test_master_enrichment_failure_copies_raw_when_enriched_missing(tmp_path, monkeypatch):
    seed_csv_path, output_csv_path, logs = _run_failing_master_enrichment(
        tmp_path,
        monkeypatch,
        seed_rows=[{"Artist Name": "Raw Artist", "Email": ""}],
    )

    assert output_csv_path.read_text(encoding="utf-8") == seed_csv_path.read_text(encoding="utf-8")
    assert any("fallback_to_raw_due_to_missing_or_invalid_enriched_output" in msg for msg in logs)


def test_master_enrichment_failure_copies_raw_when_enriched_empty(tmp_path, monkeypatch):
    seed_csv_path, output_csv_path, logs = _run_failing_master_enrichment(
        tmp_path,
        monkeypatch,
        seed_rows=[{"Artist Name": "Raw Artist", "Email": ""}],
        existing_output="",
    )

    assert output_csv_path.read_text(encoding="utf-8") == seed_csv_path.read_text(encoding="utf-8")
    assert any("fallback_to_raw_due_to_missing_or_invalid_enriched_output" in msg for msg in logs)


def test_master_enrichment_failure_copies_raw_when_enriched_corrupt(tmp_path, monkeypatch):
    seed_csv_path, output_csv_path, logs = _run_failing_master_enrichment(
        tmp_path,
        monkeypatch,
        seed_rows=[{"Artist Name": "Raw Artist", "Email": ""}],
        existing_output='Artist Name,Email\n"unterminated',
    )

    assert output_csv_path.read_text(encoding="utf-8") == seed_csv_path.read_text(encoding="utf-8")
    assert any("fallback_to_raw_due_to_missing_or_invalid_enriched_output" in msg for msg in logs)


def test_master_enrichment_failure_preserves_valid_partial_enriched_output(tmp_path, monkeypatch):
    _, output_csv_path, logs = _run_failing_master_enrichment(
        tmp_path,
        monkeypatch,
        seed_rows=[
            {"Artist Name": "Raw Artist 1", "Email": ""},
            {"Artist Name": "Raw Artist 2", "Email": ""},
            {"Artist Name": "Raw Artist 3", "Email": ""},
        ],
        existing_output="Artist Name,Email\nPartial Artist,partial@example.com\n",
    )

    output_text = output_csv_path.read_text(encoding="utf-8")
    assert "Partial Artist" in output_text
    assert "partial@example.com" in output_text
    assert "Raw Artist 2" not in output_text
    assert any("preserved_existing_enriched_output" in msg for msg in logs)


def test_master_enrichment_failure_logs_fallback_decision(tmp_path, monkeypatch):
    _, _, preserve_logs = _run_failing_master_enrichment(
        tmp_path / "preserve",
        monkeypatch,
        seed_rows=[{"Artist Name": "Raw Artist", "Email": ""}],
        existing_output="Artist Name,Email\nEnriched Artist,ig@example.com\n",
    )
    _, _, fallback_logs = _run_failing_master_enrichment(
        tmp_path / "fallback",
        monkeypatch,
        seed_rows=[{"Artist Name": "Raw Artist", "Email": ""}],
        existing_output="",
    )

    assert any("preserved_existing_enriched_output" in msg for msg in preserve_logs)
    assert any("fallback_to_raw_due_to_missing_or_invalid_enriched_output" in msg for msg in fallback_logs)
