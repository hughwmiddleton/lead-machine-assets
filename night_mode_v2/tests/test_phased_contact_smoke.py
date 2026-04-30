import json
from pathlib import Path

import pandas as pd

from night_mode_v2.manifest import config_hash
from night_mode_v2.phased_runner import run_contact_phase


def _write_csv(path: Path, rows) -> None:
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)


def _patch_contact_pipeline(monkeypatch):
    def fake_fb_pass(input_csv, output_csv, state_path=None, **kwargs):
        df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        df["fb_marker"] = "y"
        df.to_csv(output_csv, index=False)
        return type("Status", (), {"completed": True, "hit_captcha": False, "limit_reached": False})

    def fake_enrich(input_csv, output_csv, **kwargs):
        df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        df["final_marker"] = "y"
        df.to_csv(output_csv, index=False)
        return output_csv

    def fake_export_master_leads(input_csv, output_csv, **kwargs):
        df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        df.to_csv(output_csv, index=False)

    monkeypatch.setattr("pipeline_runner.run_facebook_global_pass_nightmode", fake_fb_pass)
    monkeypatch.setattr("pipeline_runner.run_enrichment", fake_enrich)
    monkeypatch.setattr("pipeline_runner.export_master_leads", fake_export_master_leads)


def _contact_fixture(tmp_path):
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
    cfg = {"export_profile": "full_dump"}
    enrich_manifest = {
        "config_hash": config_hash(cfg),
        "run_dir": run_dir.as_posix(),
        "phases": {"enrich": {"status": "completed", "outputs": {"master_pre_fb": {"path": master_pre_fb.as_posix(), "row_count": 2}}}},
    }
    return run_dir, cfg, enrich_manifest


def test_contact_phase_smoke(monkeypatch, tmp_path):
    run_dir, cfg, enrich_manifest = _contact_fixture(tmp_path)
    _patch_contact_pipeline(monkeypatch)

    manifest = run_contact_phase(cfg, run_dir.as_posix(), enrich_manifest, resume=False)

    master_post_fb = run_dir / "master_post_fb.csv"
    master_final = run_dir / "master_final.csv"
    master_export = run_dir / "master_export_leads.csv"
    for path in (master_post_fb, master_final, master_export):
        assert path.exists()
        assert pd.read_csv(path).shape[0] > 0

    contact_phase = manifest["phases"]["contact"]
    assert contact_phase["status"] == "completed"
    outputs = contact_phase["outputs"]
    assert outputs["master_post_fb"]["row_count"] > 0
    assert outputs["master_final"]["row_count"] > 0
    assert outputs["master_export_leads"]["row_count"] > 0
    assert "final_export" not in outputs
    assert "woodpecker_export" not in outputs


def test_contact_phase_auto_share_recovery_on_invokes_manual_script_entrypoint(monkeypatch, tmp_path):
    run_dir, cfg, enrich_manifest = _contact_fixture(tmp_path)
    cfg["fb_share_recovery"] = {"enabled": True, "batch_size": 40}
    _patch_contact_pipeline(monkeypatch)
    calls = []

    def fake_share_recovery(export_csv, *, batch_size=None, logger=None):
        calls.append((export_csv, batch_size))
        recovered = Path(export_csv).with_name("master_export_leads.fb_share_recovered.csv")
        recovered.write_text(Path(export_csv).read_text(encoding="utf-8"), encoding="utf-8")
        return {
            "input_csv": export_csv,
            "output_csv": recovered.as_posix(),
            "canonical_export_csv": recovered.as_posix(),
            "rows_recovered": 1,
            "summary": {"candidates_found": "1", "resolved": "1", "enriched": "1", "failed": "0"},
        }

    monkeypatch.setattr("night_mode_runner._run_fb_share_recovery_after_export", fake_share_recovery)

    manifest = run_contact_phase(cfg, run_dir.as_posix(), enrich_manifest, resume=False)

    final_export = run_dir / "master_export_leads.csv"
    assert calls == [(final_export.as_posix(), 40)]
    outputs = manifest["phases"]["contact"]["outputs"]
    assert outputs["master_export_leads"]["path"] == final_export.as_posix()
    assert outputs["canonical_export_leads"]["path"].endswith("master_export_leads.fb_share_recovered.csv")


def test_contact_phase_auto_share_recovery_off_skips_entrypoint(monkeypatch, tmp_path):
    run_dir, cfg, enrich_manifest = _contact_fixture(tmp_path)
    cfg["fb_share_recovery"] = {"enabled": False}
    _patch_contact_pipeline(monkeypatch)

    def fail_share_recovery(*args, **kwargs):
        raise AssertionError("share recovery should not run")

    monkeypatch.setattr("night_mode_runner._run_fb_share_recovery_after_export", fail_share_recovery)

    manifest = run_contact_phase(cfg, run_dir.as_posix(), enrich_manifest, resume=False)

    outputs = manifest["phases"]["contact"]["outputs"]
    assert outputs["canonical_export_leads"]["path"].endswith("master_export_leads.csv")
