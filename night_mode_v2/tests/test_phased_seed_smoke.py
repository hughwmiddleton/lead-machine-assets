import json
import os
import tempfile

from night_mode_v2.phased_runner import run_seed_phase


def test_seed_phase_empty_jobs_completes(tmp_path):
    config = {"jobs": []}

    with tempfile.TemporaryDirectory() as td:
        cfg_path = os.path.join(td, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        run_dir = os.path.join(td, "run")
        manifest = run_seed_phase(cfg_path, run_dir, resume=False)

        manifest_path = os.path.join(run_dir, "run_manifest_v2.json")
        assert os.path.exists(manifest_path)
        assert manifest["phases"]["seed"]["status"] == "completed"

