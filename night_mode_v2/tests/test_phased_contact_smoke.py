import json
from pathlib import Path

import pandas as pd

from night_mode_v2.manifest import config_hash
from night_mode_v2.phased_runner import run_contact_phase


def _write_csv(path: Path, rows) -> None:
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)


def test_contact_phase_smoke(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    master_pre_fb = run_dir / "master_pre_fb.csv"
    _write_csv(
        master_pre_fb,
        [
            {"Artist Name": "A", "Source Directory": "dir", "Source URL": "u1", "Email": "a@example.com", "Email_All": "a@example.com"},
            {"Artist Name": "B", "Source Directory": "dir", "Source URL": "u2", "Email": "b@example.com", "Email_All": "b@example.com"},
        ],
    )

    # Minimal manifest state carried from enrich phase.
    cfg = {"export_profile": "full_dump"}
    enrich_manifest = {
        "config_hash": config_hash(cfg),
        "run_dir": run_dir.as_posix(),
        "phases": {"enrich": {"status": "completed", "outputs": {"master_pre_fb": {"path": master_pre_fb.as_posix(), "row_count": 2}}}},
    }

    def fake_fb_pass(input_csv, output_csv, state_path=None, **kwargs):
        df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        df["fb_marker"] = "y"
        df.to_csv(output_csv, index=False)
        # Simulate status object minimally.
        return type("Status", (), {"completed": True, "hit_captcha": False, "limit_reached": False})

    def fake_enrich(input_csv, output_csv, **kwargs):
        df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        df["final_marker"] = "y"
        df.to_csv(output_csv, index=False)
        return output_csv

    def fake_export_master_leads(input_csv, output_csv, final_export_csv=None, woodpecker_export_csv=None, **kwargs):
        df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        df.to_csv(output_csv, index=False)
        Path(final_export_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(final_export_csv, index=False)
        Path(woodpecker_export_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(woodpecker_export_csv, index=False)

    monkeypatch.setattr("pipeline_runner.run_facebook_global_pass_nightmode", fake_fb_pass)
    monkeypatch.setattr("pipeline_runner.run_enrichment", fake_enrich)
    monkeypatch.setattr("pipeline_runner.export_master_leads", fake_export_master_leads)

    manifest = run_contact_phase(cfg, run_dir.as_posix(), enrich_manifest, resume=False)

    master_post_fb = run_dir / "master_post_fb.csv"
    master_final = run_dir / "master_final.csv"
    master_export = run_dir / "master_export_leads.csv"
    final_export = run_dir / "final_export.csv"
    woodpecker_export = run_dir / "woodpecker_export.csv"

    for path in (master_post_fb, master_final, master_export, final_export, woodpecker_export):
        assert path.exists()
        assert pd.read_csv(path).shape[0] > 0

    contact_phase = manifest["phases"]["contact"]
    assert contact_phase["status"] == "completed"
    outputs = contact_phase["outputs"]
    assert outputs["master_post_fb"]["row_count"] > 0
    assert outputs["master_final"]["row_count"] > 0
