import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from typing import List

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
            "master_enrichment": {"enabled": True},
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

        def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None, night_mode=False):
            df = pd.read_csv(raw_csv_path)
            df["enriched"] = True
            df.to_csv(enriched_output_path, index=False)
            return enriched_output_path

        def fake_run_master_enrichment(
            input_csv, output_csv, logger=None, enable_live_search=True, max_live_searches=None, night_mode=False
        ):
            df = pd.read_csv(input_csv)
            df["master_enriched"] = True
            df.to_csv(output_csv, index=False)
            return output_csv

        fb_calls = []

        def fake_fb_pass(input_csv, output_csv, state_path, max_rows_per_run=100, **kwargs):
            fb_calls.append((input_csv, output_csv, state_path, max_rows_per_run))
            shutil.copyfile(input_csv, output_csv)
            return pipeline_runner.FacebookGlobalPassStatus(
                processed_rows=4,
                total_rows=4,
                completed=True,
                hit_captcha=False,
                limit_reached=False,
                attempted_total=4,
            )

        with mock.patch.object(night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job), mock.patch.object(
            night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment
        ), mock.patch.object(night_mode_runner, "run_master_enrichment", side_effect=fake_run_master_enrichment), mock.patch.object(
            night_mode_runner, "run_facebook_global_pass_nightmode", side_effect=fake_fb_pass
        ):
            result = night_mode_runner.run_night_mode(config_path, run_root=self.run_root)

        run_dir = result["run_dir"]
        for job in config["jobs"]:
            job_dir = os.path.join(run_dir, job["job_id"])
            state_path = os.path.join(job_dir, "state.json")
            log_path = os.path.join(job_dir, "log.txt")
            self.assertTrue(os.path.exists(state_path))
            self.assertTrue(os.path.exists(log_path))
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
                self.assertEqual(state.get("status"), "completed")

        master_raw = os.path.join(run_dir, "master_raw.csv")
        master_enriched = os.path.join(run_dir, "master_enriched.csv")
        master_pre = os.path.join(run_dir, "master_pre_fb.csv")
        master_post = os.path.join(run_dir, "master_post_fb.csv")
        master_path = os.path.join(run_dir, "master_enriched_deduped.csv")
        self.assertTrue(os.path.exists(master_raw))
        self.assertTrue(os.path.exists(master_enriched))
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
        # raw exists; no per-job enrichment file expected in master-enrich mode.

        with mock.patch.object(night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job), mock.patch.object(
            night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment
        ), mock.patch.object(night_mode_runner, "run_master_enrichment", side_effect=fake_run_master_enrichment):
            result_resume = night_mode_runner.run_night_mode(config_path, resume=True, run_root=self.run_root)

        self.assertEqual(result_resume["run_dir"], run_dir)
        with open(job_two_state_path, "r", encoding="utf-8") as f:
            resumed_state = json.load(f)
            self.assertEqual(resumed_state.get("status"), "completed")
        self.assertTrue(os.path.exists(os.path.join(job_two_dir, "raw.csv")))

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



class FacebookNightModeWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_path = os.path.join(self.tmpdir.name, "fb_state.json")
        self.input_csv = os.path.join(self.tmpdir.name, "fb_input.csv")
        self.output_csv = os.path.join(self.tmpdir.name, "fb_output.csv")

    def _write_input(self) -> None:
        df = pd.DataFrame(
            [
                {"Artist Name": "HasEmail", "Email": "a@example.com", "Social Link": "https://facebook.com/hasemail"},
                {"Artist Name": "NeedsFb1", "Email": "", "Social Link": "https://facebook.com/page1"},
                {"Artist Name": "", "Email": "", "Social Link": ""},
                {"Artist Name": "NeedsFb2", "Email": "", "Social Link": "https://facebook.com/page2"},
            ]
        )
        df.to_csv(self.input_csv, index=False)

    def test_nightmode_wrapper_resume_and_limit(self) -> None:
        self._write_input()

        class FakeModule:
            def __init__(self):
                self.calls = []

            def scrape_csv(self, in_csv, out_csv, fb_user, fb_pass, max_emails=None):
                df_local = pd.read_csv(in_csv)
                self.calls.append(df_local.copy())
                df_local["Email"] = df_local["Artist Name"].apply(lambda v: f"fb_{v}")
                df_local.to_csv(out_csv, index=False)

        class FakeHelper:
            def __init__(self, *_args, **_kwargs):
                self.calls: List[dict] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def enrich_row_with_facebook_night(self, row, row_index=None):
                self.calls.append(dict(row))
                enriched = dict(row)
                if not enriched.get("Email"):
                    enriched["Email"] = f"fb_{enriched.get('Artist Name', '')}"
                enriched["Email_All"] = enriched.get("Email_All", "")
                enriched["Email_Type"] = "fb_night"
                return enriched

        fake_module = FakeModule()
        with mock.patch.object(pipeline_runner, "_load_legacy_module", return_value=fake_module), mock.patch.dict(
            os.environ, {"FB_USERNAME": "u", "FB_PASSWORD": "p"}
        ), mock.patch("pipeline_runner.time.sleep", return_value=None), mock.patch.object(
            pipeline_runner, "NightModeFacebookEnricher", side_effect=lambda *args, **kwargs: FakeHelper()
        ):
            status_first = pipeline_runner.run_facebook_global_pass_nightmode(
                self.input_csv,
                self.output_csv,
                self.state_path,
                max_rows_per_run=1,
                per_row_delay_range=(0, 0),
                short_break_range=(0, 0),
                long_break_range=(0, 0),
            )
        self.assertFalse(status_first.completed)
        self.assertFalse(status_first.hit_captcha)

        with open(self.state_path, "r", encoding="utf-8") as f:
            first_state = json.load(f)
        self.assertTrue(first_state.get("fb_limit_reached"))
        self.assertFalse(first_state.get("fb_run_completed"))
        self.assertEqual(first_state.get("fb_attempted_total"), 1)
        df_first = pd.read_csv(self.output_csv)
        self.assertEqual(df_first.loc[df_first["Artist Name"] == "NeedsFb1", "Email"].iloc[0], "fb_NeedsFb1")
        email_two = df_first.loc[df_first["Artist Name"] == "NeedsFb2", "Email"].iloc[0]
        self.assertTrue(pd.isna(email_two) or email_two == "")

        # Resume should continue from the stored state and finish remaining eligible rows.
        with mock.patch.object(pipeline_runner, "_load_legacy_module", return_value=fake_module), mock.patch.dict(
            os.environ, {"FB_USERNAME": "u", "FB_PASSWORD": "p"}
        ), mock.patch("pipeline_runner.time.sleep", return_value=None), mock.patch.object(
            pipeline_runner, "NightModeFacebookEnricher", side_effect=lambda *args, **kwargs: FakeHelper()
        ):
            status_second = pipeline_runner.run_facebook_global_pass_nightmode(
                self.input_csv,
                self.output_csv,
                self.state_path,
                max_rows_per_run=5,
                per_row_delay_range=(0, 0),
                short_break_range=(0, 0),
                long_break_range=(0, 0),
            )
        self.assertTrue(status_second.completed)
        self.assertFalse(status_second.hit_captcha)

        with open(self.state_path, "r", encoding="utf-8") as f:
            final_state = json.load(f)
        self.assertTrue(final_state.get("fb_run_completed"))
        self.assertFalse(final_state.get("fb_captcha_flag"))
        self.assertEqual(final_state.get("fb_attempted_total"), 2)
        df_final = pd.read_csv(self.output_csv)
        self.assertEqual(df_final.loc[df_final["Artist Name"] == "NeedsFb2", "Email"].iloc[0], "fb_NeedsFb2")

    def test_nightmode_wrapper_handles_captcha_signal(self) -> None:
        self._write_input()

        class FakeCaptcha(Exception):
            pass

        class FakeModule:
            def scrape_csv(self, in_csv, out_csv, fb_user, fb_pass, max_emails=None):
                raise FakeCaptcha("captcha required")

        class FakeHelper:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def enrich_row_with_facebook_night(self, row, row_index=None):
                raise FakeCaptcha("captcha required")

        with mock.patch.object(pipeline_runner, "_load_legacy_module", return_value=FakeModule()), mock.patch.dict(
            os.environ, {"FB_USERNAME": "u", "FB_PASSWORD": "p"}
        ), mock.patch("pipeline_runner.time.sleep", return_value=None), mock.patch.object(
            pipeline_runner, "NightModeFacebookEnricher", side_effect=lambda *args, **kwargs: FakeHelper()
        ):
            status = pipeline_runner.run_facebook_global_pass_nightmode(
                self.input_csv,
                self.output_csv,
                self.state_path,
                max_rows_per_run=5,
                per_row_delay_range=(0, 0),
                short_break_range=(0, 0),
                long_break_range=(0, 0),
            )
        self.assertTrue(status.hit_captcha)
        self.assertFalse(status.completed)

        with open(self.state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertTrue(state.get("fb_captcha_flag"))
        self.assertFalse(state.get("fb_run_completed"))
        df = pd.read_csv(self.output_csv)
        email_two = df.loc[df["Artist Name"] == "NeedsFb1", "Email"].iloc[0]
        self.assertTrue(pd.isna(email_two) or email_two == "")


class NightModeFacebookAutoResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_root = os.path.join(self.tmpdir.name, "overnight_runs")
        os.makedirs(self.run_root, exist_ok=True)

    def _write_config(self, path: str) -> dict:
        config = {
            "export_mode": "both",
            "facebook": {
                "auto_resume_after_captcha": False,
                "cooldown_seconds": 0,
                "max_auto_resume_attempts": 2,
                "max_rows_per_run": 100,
            },
            "jobs": [
                {
                    "job_id": "job_one",
                    "directory": "spotify",
                    "target_valid_leads": 1,
                }
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return config

    def test_auto_resume_after_captcha(self) -> None:
        config_path = os.path.join(self.tmpdir.name, "config.json")
        self._write_config(config_path)

        def fake_run_directory_job(job_config, raw_output_path, logger=None):
            rows = [
                {"Artist Name": f"{job_config['job_id']}_artist_a", "Email": ""},
                {"Artist Name": f"{job_config['job_id']}_artist_b", "Email": ""},
            ]
            pd.DataFrame(rows).to_csv(raw_output_path, index=False)
            return raw_output_path

        def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None, night_mode=False):
            shutil.copyfile(raw_csv_path, enriched_output_path)
            return enriched_output_path

        fb_statuses = [
            pipeline_runner.FacebookGlobalPassStatus(
                processed_rows=1,
                total_rows=2,
                completed=False,
                hit_captcha=True,
                limit_reached=False,
                attempted_total=1,
            ),
            pipeline_runner.FacebookGlobalPassStatus(
                processed_rows=2,
                total_rows=2,
                completed=True,
                hit_captcha=False,
                limit_reached=False,
                attempted_total=2,
            ),
        ]
        fb_calls = []

        def fake_fb_pass(input_csv, output_csv, state_path, **kwargs):
            fb_calls.append((input_csv, output_csv, state_path))
            shutil.copyfile(input_csv, output_csv)
            return fb_statuses.pop(0)

        with mock.patch.object(night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job), mock.patch.object(
            night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment
        ), mock.patch.object(night_mode_runner, "run_facebook_global_pass_nightmode", side_effect=fake_fb_pass), mock.patch(
            "night_mode_runner.time.sleep", return_value=None
        ):
            result = night_mode_runner.run_night_mode(
                config_path,
                run_root=self.run_root,
                fb_auto_resume_override=True,
                fb_cooldown_override=0,
                fb_max_attempts_override=2,
            )

        self.assertGreaterEqual(len(fb_calls), 2)
        run_dir = result["run_dir"]
        master_post = os.path.join(run_dir, "master_post_fb.csv")
        master_final = os.path.join(run_dir, "master_enriched_deduped.csv")
        self.assertTrue(os.path.exists(master_post))
        self.assertTrue(os.path.exists(master_final))

if __name__ == "__main__":
    unittest.main()
