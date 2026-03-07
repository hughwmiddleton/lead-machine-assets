import json
import shutil
from pathlib import Path

import pandas as pd

import night_mode_runner
import pipeline_runner


def _write_seed_csv(path: str, rows: list[dict]) -> None:
    columns = ["Artist Name", "Email", "Source Directory"]
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def _default_jobs() -> list[dict]:
    return [
        {"job_id": "job_spotify", "directory": "spotify", "target_valid_leads": 3},
        {"job_id": "job_unearthed", "directory": "unearthed", "target_valid_leads": 3},
        {"job_id": "job_bandcamp", "directory": "bandcamp", "target_valid_leads": 3},
    ]


def _write_config(path: Path, *, master_enrichment_enabled: bool = True, jobs: list[dict] | None = None) -> None:
    config = {
        "export_mode": "both",
        "master_enrichment": {"enabled": master_enrichment_enabled},
        "jobs": jobs or _default_jobs(),
    }
    path.write_text(json.dumps(config), encoding="utf-8")


def _install_common_stubs(monkeypatch, directory_rows: dict[str, list[dict]], calls: list[str]) -> None:
    def fake_run_directory_job(job_config, raw_output_path, logger=None):
        directory = (job_config.get("directory") or "").strip().lower()
        calls.append(directory)
        _write_seed_csv(raw_output_path, directory_rows.get(directory, []))
        return raw_output_path

    def fake_run_master_enrichment(input_csv, output_csv, logger=None, enable_live_search=True, max_live_searches=None, night_mode=False):
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


def test_spotify_zero_row_fallback(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path)

    calls: list[str] = []
    _install_common_stubs(
        monkeypatch,
        {
            "spotify": [],
            "unearthed": [{"Artist Name": "Fallback Unearthed", "Email": "", "Source Directory": "unearthed"}],
            "bandcamp": [{"Artist Name": "Should Not Run", "Email": "", "Source Directory": "bandcamp"}],
        },
        calls,
    )
    monkeypatch.setenv("SPOTIFY_ZERO_ROW_FALLBACK", "1")
    monkeypatch.setenv("SPOTIFY_SEED_FALLBACK_ORDER", "unearthed,bandcamp")

    result = night_mode_runner.run_night_mode(config_path.as_posix(), run_root=(tmp_path / "runs").as_posix())

    assert calls == ["spotify", "unearthed"]

    master_log = (Path(result["run_dir"]) / "master_log.txt").read_text(encoding="utf-8")
    assert "[Spotify][Seed] 0 artists discovered from playlist." in master_log
    assert "Reason: playlist empty / inaccessible / extraction returned no artists." in master_log
    assert "Continuing pipeline." in master_log
    assert "[Spotify][Seed] 0 rows detected; triggering fallback chain: unearthed,bandcamp" in master_log
    assert "[Seed Fallback] attempting configured fallback source: unearthed" in master_log
    assert "[Seed Fallback] unearthed produced 1 artists" in master_log
    assert "[Seed Fallback] attempting configured fallback source: bandcamp" not in master_log

    master_raw = pd.read_csv(result["master_raw"], dtype=str, keep_default_na=False)
    assert "Fallback Unearthed" in set(master_raw["Artist Name"])
    assert Path(result["master_csv"]).exists()
    assert result["smoke_stats"]["jobs_merged"] == 1

    skipped_bandcamp_state = json.loads((Path(result["run_dir"]) / "job_bandcamp" / "state.json").read_text(encoding="utf-8"))
    assert skipped_bandcamp_state["row_count"] == 0
    assert skipped_bandcamp_state["skipped_reason"] == "spotify_zero_row_fallback_short_circuit"


def test_spotify_zero_row_fallback_invalid_tokens(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path)

    calls: list[str] = []
    _install_common_stubs(
        monkeypatch,
        {
            "spotify": [],
            "unearthed": [],
            "bandcamp": [{"Artist Name": "Fallback Bandcamp", "Email": "", "Source Directory": "bandcamp"}],
        },
        calls,
    )
    monkeypatch.setenv("SPOTIFY_ZERO_ROW_FALLBACK", "1")
    monkeypatch.setenv("SPOTIFY_SEED_FALLBACK_ORDER", "unearthed,invalid,soundcloud,bandcamp")

    result = night_mode_runner.run_night_mode(config_path.as_posix(), run_root=(tmp_path / "runs").as_posix())

    assert calls == ["spotify", "unearthed", "bandcamp"]

    master_log = (Path(result["run_dir"]) / "master_log.txt").read_text(encoding="utf-8")
    assert "[Seed Fallback] ignoring invalid fallback sources: invalid" in master_log
    assert "[Seed Fallback] attempting configured fallback source: unearthed" in master_log
    assert "[Seed Fallback] unearthed produced 0 artists" in master_log
    assert "[Seed Fallback] attempting configured fallback source: soundcloud" in master_log
    assert "[Seed Fallback] no configured pending job found for source: soundcloud" in master_log
    assert "[Seed Fallback] attempting configured fallback source: bandcamp" in master_log
    assert "[Seed Fallback] bandcamp produced 1 artists" in master_log

    master_raw = pd.read_csv(result["master_raw"], dtype=str, keep_default_na=False)
    assert "Fallback Bandcamp" in set(master_raw["Artist Name"])


def test_spotify_zero_row_no_fallback_when_disabled(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path)

    calls: list[str] = []
    _install_common_stubs(
        monkeypatch,
        {
            "spotify": [],
            "unearthed": [{"Artist Name": "Normal Unearthed", "Email": "", "Source Directory": "unearthed"}],
            "bandcamp": [{"Artist Name": "Normal Bandcamp", "Email": "", "Source Directory": "bandcamp"}],
        },
        calls,
    )
    monkeypatch.setenv("SPOTIFY_ZERO_ROW_FALLBACK", "0")
    monkeypatch.delenv("SPOTIFY_SEED_FALLBACK_ORDER", raising=False)

    result = night_mode_runner.run_night_mode(config_path.as_posix(), run_root=(tmp_path / "runs").as_posix())

    assert calls == ["spotify", "unearthed", "bandcamp"]

    master_log = (Path(result["run_dir"]) / "master_log.txt").read_text(encoding="utf-8")
    assert "[Spotify][Seed] 0 artists discovered from playlist." in master_log
    assert "Reason: playlist empty / inaccessible / extraction returned no artists." in master_log
    assert "Continuing pipeline." in master_log
    assert "[Spotify][Seed] 0 rows detected; triggering fallback chain:" not in master_log
    assert "[Seed Fallback]" not in master_log

    master_raw = pd.read_csv(result["master_raw"], dtype=str, keep_default_na=False)
    assert set(master_raw["Artist Name"]) == {"Normal Unearthed", "Normal Bandcamp"}


def test_spotify_zero_row_fallback_invalid_only_order_does_nothing(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path)

    calls: list[str] = []
    _install_common_stubs(
        monkeypatch,
        {
            "spotify": [],
            "unearthed": [{"Artist Name": "Normal Unearthed", "Email": "", "Source Directory": "unearthed"}],
            "bandcamp": [{"Artist Name": "Normal Bandcamp", "Email": "", "Source Directory": "bandcamp"}],
        },
        calls,
    )
    monkeypatch.setenv("SPOTIFY_ZERO_ROW_FALLBACK", "1")
    monkeypatch.setenv("SPOTIFY_SEED_FALLBACK_ORDER", "invalid,also_bad")

    result = night_mode_runner.run_night_mode(config_path.as_posix(), run_root=(tmp_path / "runs").as_posix())

    assert calls == ["spotify", "unearthed", "bandcamp"]

    master_log = (Path(result["run_dir"]) / "master_log.txt").read_text(encoding="utf-8")
    assert "[Seed Fallback] ignoring invalid fallback sources: invalid, also_bad" in master_log
    assert "[Seed Fallback] fallback enabled but no valid order remains after validation; doing nothing" in master_log
    assert "[Seed Fallback] attempting configured fallback source:" not in master_log

    master_raw = pd.read_csv(result["master_raw"], dtype=str, keep_default_na=False)
    assert set(master_raw["Artist Name"]) == {"Normal Unearthed", "Normal Bandcamp"}


def test_spotify_zero_row_fallback_rows_still_dedupe_in_master_merge(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        master_enrichment_enabled=False,
        jobs=[
            {"job_id": "job_spotify", "directory": "spotify", "target_valid_leads": 3},
            {"job_id": "job_unearthed", "directory": "unearthed", "target_valid_leads": 3},
            {"job_id": "job_bandcamp", "directory": "bandcamp", "target_valid_leads": 3},
        ],
    )

    calls: list[str] = []
    _install_common_stubs(
        monkeypatch,
        {
            "spotify": [],
            "unearthed": [{"Artist Name": "Duplicate Artist", "Email": "dup@example.com", "Source Directory": "unearthed"}],
            "bandcamp": [{"Artist Name": "Duplicate Artist", "Email": "dup@example.com", "Source Directory": "bandcamp"}],
        },
        calls,
    )
    monkeypatch.setenv("SPOTIFY_ZERO_ROW_FALLBACK", "1")
    monkeypatch.setenv("SPOTIFY_SEED_FALLBACK_ORDER", "unearthed")

    result = night_mode_runner.run_night_mode(config_path.as_posix(), run_root=(tmp_path / "runs").as_posix())

    assert calls == ["spotify", "unearthed", "bandcamp"]

    master_csv = pd.read_csv(result["master_csv"], dtype=str, keep_default_na=False)
    dup_rows = master_csv[master_csv["Email"] == "dup@example.com"]
    assert len(dup_rows.index) == 1
