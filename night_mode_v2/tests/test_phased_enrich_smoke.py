import pandas as pd
import pytest

from night_mode_v2.phased_runner import _schema_hash_from_df, run_enrich_phase
from night_mode_v2.manifest import config_hash


@pytest.fixture()
def fake_jobs(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    jobs = {}
    for name, email in (("job_a", "a@example.com"), ("job_b", "b@example.com")):
        job_dir = run_dir / name
        job_dir.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(
            [
                {"Artist Name": f"Artist {name[-1]}", "Email": email, "Email_All": email, "Source Directory": name},
            ]
        )
        raw_path = job_dir / "raw.csv"
        df.to_csv(raw_path, index=False)
        jobs[name] = {
            "status": "completed",
            "directory": raw_path.parent.as_posix(),
            "raw_csv": raw_path.as_posix(),
            "row_count": len(df.index),
            "schema_hash": _schema_hash_from_df(df),
        }

    config = {"jobs": [], "master_enrichment": {"enable_live_search": False}}
    seed_manifest = {
        "config_hash": config_hash(config),
        "run_dir": run_dir.as_posix(),
        "phases": {"seed": {"status": "completed", "jobs": jobs}},
    }
    return run_dir, seed_manifest, config


def test_enrich_phase_smoke(monkeypatch, fake_jobs):
    run_dir, seed_manifest, config = fake_jobs

    def fake_master_enrich(seed_csv_path, output_csv_path, **kwargs):
        df = pd.read_csv(seed_csv_path)
        df["master_enriched_marker"] = "y"
        df.to_csv(output_csv_path, index=False)
        return output_csv_path

    def fake_enrich(raw_csv_path, enriched_output_path, **kwargs):
        df = pd.read_csv(raw_csv_path)
        df["validated_marker"] = "y"
        df.to_csv(enriched_output_path, index=False)
        return enriched_output_path

    monkeypatch.setattr("pipeline_runner.run_master_enrichment", fake_master_enrich)
    monkeypatch.setattr("pipeline_runner.run_enrichment", fake_enrich)

    manifest = run_enrich_phase(config, run_dir.as_posix(), seed_manifest, resume=False)

    master_raw = run_dir / "master_raw.csv"
    master_enriched = run_dir / "master_enriched.csv"
    master_pre_fb = run_dir / "master_pre_fb.csv"

    assert master_raw.exists() and pd.read_csv(master_raw).shape[0] > 0
    assert master_enriched.exists() and pd.read_csv(master_enriched).shape[0] > 0
    assert master_pre_fb.exists() and pd.read_csv(master_pre_fb).shape[0] > 0

    assert manifest["phases"]["enrich"]["status"] == "completed"
    outputs = manifest["phases"]["enrich"]["outputs"]
    assert outputs["master_pre_fb"]["row_count"] > 0
