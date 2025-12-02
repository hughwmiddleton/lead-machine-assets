import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import pandas as pd

import night_mode_runner
import pipeline_runner


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

        fb_calls = []

        def fake_fb_pass(input_csv, output_csv, skip_rows_with_email=True, skip_rows_with_no_facebook_clue=True):
            fb_calls.append((input_csv, output_csv))
            shutil.copyfile(input_csv, output_csv)

        with mock.patch.object(night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job), mock.patch.object(
            night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment
        ), mock.patch.object(night_mode_runner, "run_facebook_global_pass", side_effect=fake_fb_pass):
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

        master_pre = os.path.join(run_dir, "master_pre_fb.csv")
        master_post = os.path.join(run_dir, "master_post_fb.csv")
        master_path = os.path.join(run_dir, "master_enriched_deduped.csv")
        self.assertTrue(os.path.exists(master_pre))
        self.assertTrue(os.path.exists(master_post))
        self.assertTrue(os.path.exists(master_path))
        master_df = pd.read_csv(master_path)
        self.assertGreaterEqual(len(master_df.index), 2)
        self.assertTrue(fb_calls)

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

    def test_facebook_global_pass_skip_rules(self) -> None:
        # Build a small CSV with various rows.
        tmpdir = self.tmpdir.name
        input_csv = os.path.join(tmpdir, "fb_input.csv")
        output_csv = os.path.join(tmpdir, "fb_output.csv")
        df = pd.DataFrame(
            [
                {"Artist Name": "HasEmail", "Email": "a@example.com", "Social Link": "https://facebook.com/hasemail"},
                {"Artist Name": "HasFacebook", "Email": "", "Social Link": "https://facebook.com/page1"},
                {"Artist Name": "NoClue", "Email": "", "Social Link": "https://instagram.com/only"},
            ]
        )
        df.to_csv(input_csv, index=False)

        calls = []

        class FakeModule:
            def scrape_csv(self, in_csv, out_csv, fb_user, fb_pass, max_emails=None):
                calls.append((in_csv, out_csv, fb_user, fb_pass))
                df_local = pd.read_csv(in_csv)
                df_local.loc[df_local["Artist Name"] == "HasFacebook", "Email"] = "fb@example.com"
                df_local.to_csv(out_csv, index=False)

        with mock.patch.object(pipeline_runner, "_load_legacy_module", return_value=FakeModule()):
            with mock.patch.dict(os.environ, {"FB_USERNAME": "u", "FB_PASSWORD": "p"}):
                pipeline_runner.run_facebook_global_pass(
                    input_csv, output_csv, skip_rows_with_email=True, skip_rows_with_no_facebook_clue=True
                )

        out_df = pd.read_csv(output_csv)
        # Email with existing should remain untouched
        self.assertEqual(out_df.loc[out_df["Artist Name"] == "HasEmail", "Email"].iloc[0], "a@example.com")
        # Facebook row should be enriched
        self.assertEqual(out_df.loc[out_df["Artist Name"] == "HasFacebook", "Email"].iloc[0], "fb@example.com")
        # No clue row should stay blank
        self.assertTrue(
            pd.isna(out_df.loc[out_df["Artist Name"] == "NoClue", "Email"].iloc[0])
            or out_df.loc[out_df["Artist Name"] == "NoClue", "Email"].iloc[0] == ""
        )
        # Scraper should have been called once
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
