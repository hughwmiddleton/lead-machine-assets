import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import night_mode_runner


class NightModeRunnerDummyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_root = os.path.join(self.tmpdir.name, "overnight_runs")
        os.makedirs(self.run_root, exist_ok=True)

    def _write_config(self, path: str) -> dict:
        config = {
            "export_mode": "both",
            "jobs": [
                {
                    "job_id": "job_one",
                    "directory": "spotify",
                    "target_valid_leads": 3,
                },
                {
                    "job_id": "job_two",
                    "directory": "bandcamp",
                    "target_valid_leads": 2,
                },
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return config

    def test_dummy_run_and_resume(self) -> None:
        config_path = os.path.join(self.tmpdir.name, "config.json")
        config = self._write_config(config_path)

        def fake_run_directory_job(job_config, raw_output_path, logger=None):
            rows = [
                {"Artist Name": f"{job_config['job_id']}_artist_a", "Email": f"{job_config['job_id']}@example.com"},
                {"Artist Name": f"{job_config['job_id']}_artist_b", "Email": ""},
            ]
            pd.DataFrame(rows).to_csv(raw_output_path, index=False)
            return raw_output_path

        def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None):
            df = pd.read_csv(raw_csv_path)
            df["enriched"] = True
            df.to_csv(enriched_output_path, index=False)
            return enriched_output_path

        with mock.patch.object(night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job), mock.patch.object(
            night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment
        ):
            result = night_mode_runner.run_night_mode(config_path, run_root=self.run_root)

        run_dir = result["run_dir"]
        for job in config["jobs"]:
            job_dir = os.path.join(run_dir, job["job_id"])
            state_path = os.path.join(job_dir, "state.json")
            enriched_path = os.path.join(job_dir, "enriched.csv")
            log_path = os.path.join(job_dir, "log.txt")
            self.assertTrue(os.path.exists(state_path))
            self.assertTrue(os.path.exists(enriched_path))
            self.assertTrue(os.path.exists(log_path))
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
                self.assertEqual(state.get("status"), "completed")

        master_path = os.path.join(run_dir, "master_enriched_deduped.csv")
        self.assertTrue(os.path.exists(master_path))
        master_df = pd.read_csv(master_path)
        # Two jobs, two rows each, dedupe drops only empty email duplicates.
        self.assertGreaterEqual(len(master_df.index), 2)

        # Simulate an interrupted job_two and ensure resume picks it up.
        job_two_dir = os.path.join(run_dir, "job_two")
        job_two_state_path = os.path.join(job_two_dir, "state.json")
        with open(job_two_state_path, "r", encoding="utf-8") as f:
            job_two_state = json.load(f)
        job_two_state["status"] = "running"
        with open(job_two_state_path, "w", encoding="utf-8") as f:
            json.dump(job_two_state, f, indent=2)
        os.remove(os.path.join(job_two_dir, "enriched.csv"))

        with mock.patch.object(night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job), mock.patch.object(
            night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment
        ):
            result_resume = night_mode_runner.run_night_mode(config_path, resume=True, run_root=self.run_root)

        self.assertEqual(result_resume["run_dir"], run_dir)
        with open(job_two_state_path, "r", encoding="utf-8") as f:
            resumed_state = json.load(f)
            self.assertEqual(resumed_state.get("status"), "completed")
        self.assertTrue(os.path.exists(os.path.join(job_two_dir, "enriched.csv")))


if __name__ == "__main__":
    unittest.main()
