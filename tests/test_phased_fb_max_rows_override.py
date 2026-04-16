import json
from pathlib import Path

import pandas as pd

import night_mode_v2.phased_runner as phased_runner


_OMITTED = object()


def _write_master_pre_fb(run_dir: Path) -> None:
    pd.DataFrame(
        [
            {
                "Artist Name": "Artist A",
                "Source Directory": "spotify",
                "Source URL": "https://example.com/a",
                "Email": "",
                "Email_All": "",
            }
        ]
    ).to_csv(run_dir / "master_pre_fb.csv", index=False)


def _run_phased_with_fb_override(monkeypatch, tmp_path, override=_OMITTED):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    config = {"export_profile": "full_dump", "jobs": []}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    captured_max_rows = []

    def fake_ensure_run_dir(resume=False, run_root=None):
        return run_dir.as_posix(), "run"

    def fake_seed_phase(config_path_arg, run_dir_arg, resume=False):
        return {
            "config_hash": phased_runner.config_hash(config),
            "run_dir": run_dir_arg,
            "phases": {"seed": {"status": "completed", "jobs": {}}},
        }

    def fake_enrich_phase(cfg, run_dir_arg, seed_manifest, resume=False, night_fb_run_state=None):
        _write_master_pre_fb(Path(run_dir_arg))
        master_pre_fb = Path(run_dir_arg) / "master_pre_fb.csv"
        return {
            "config_hash": phased_runner.config_hash(cfg),
            "run_dir": run_dir_arg,
            "phases": {
                "seed": seed_manifest["phases"]["seed"],
                "enrich": {
                    "status": "completed",
                    "outputs": {
                        "master_pre_fb": {
                            "path": master_pre_fb.as_posix(),
                            "row_count": 1,
                        }
                    },
                },
            },
        }

    def fake_fb_pass(input_csv, output_csv, state_path=None, **kwargs):
        captured_max_rows.append(kwargs.get("max_rows_per_run", _OMITTED))
        pd.read_csv(input_csv, dtype=str, keep_default_na=False).to_csv(output_csv, index=False)

    def fake_enrichment(input_csv, output_csv, **kwargs):
        pd.read_csv(input_csv, dtype=str, keep_default_na=False).to_csv(output_csv, index=False)
        return output_csv

    def fake_export_master_leads(input_csv, output_csv, **kwargs):
        pd.read_csv(input_csv, dtype=str, keep_default_na=False).to_csv(output_csv, index=False)

    monkeypatch.setattr(phased_runner.night_mode_runner, "_ensure_run_dir", fake_ensure_run_dir)
    monkeypatch.setattr(phased_runner, "run_seed_phase", fake_seed_phase)
    monkeypatch.setattr(phased_runner, "run_enrich_phase", fake_enrich_phase)
    monkeypatch.setattr(phased_runner.pipeline_runner, "run_facebook_global_pass_nightmode", fake_fb_pass)
    monkeypatch.setattr(phased_runner.pipeline_runner, "run_enrichment", fake_enrichment)
    monkeypatch.setattr(phased_runner.pipeline_runner, "export_master_leads", fake_export_master_leads)

    kwargs = {"config_path": config_path.as_posix(), "run_root": (tmp_path / "root").as_posix()}
    if override is not _OMITTED:
        kwargs["fb_max_rows_override"] = override

    phased_runner.run_phased_night_mode(**kwargs)
    return captured_max_rows


def test_phased_fb_max_rows_override_preserves_zero(monkeypatch, tmp_path):
    assert _run_phased_with_fb_override(monkeypatch, tmp_path, override=0) == [0]


def test_phased_fb_max_rows_override_preserves_explicit_cap(monkeypatch, tmp_path):
    assert _run_phased_with_fb_override(monkeypatch, tmp_path, override=15) == [15]


def test_phased_fb_max_rows_without_override_uses_existing_default(monkeypatch, tmp_path):
    assert _run_phased_with_fb_override(monkeypatch, tmp_path) == [_OMITTED]
