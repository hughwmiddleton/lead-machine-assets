import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import pandas as pd

import night_mode_runner
import night_mode_bandcamp
import pipeline_runner
from night_mode_v2 import phased_runner


class BandcampAggregateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_dir = os.path.join(self.tmpdir.name, "run")
        os.makedirs(self.run_dir, exist_ok=True)

    def _make_job_state(self, job_id, source_directory, bandcamp_rows=None, generic_rows=None, status="completed"):
        job_dir = os.path.join(self.run_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        raw_csv = os.path.join(job_dir, "raw.csv")
        enriched_csv = os.path.join(job_dir, "enriched.csv")

        if generic_rows is not None:
            pd.DataFrame(generic_rows).to_csv(raw_csv, index=False)
            pd.DataFrame(generic_rows).to_csv(enriched_csv, index=False)
        else:
            pd.DataFrame([{"Artist Name": "A"}]).to_csv(raw_csv, index=False)
            pd.DataFrame([{"Artist Name": "A"}]).to_csv(enriched_csv, index=False)

        if bandcamp_rows is not None:
            bandcamp_csv = os.path.join(job_dir, "bandcamp_enriched.csv")
            pd.DataFrame(bandcamp_rows).to_csv(bandcamp_csv, index=False)

        return {
            "job_id": job_id,
            "status": status,
            "source_directory": source_directory,
            "raw_csv": raw_csv,
            "enriched_csv": enriched_csv,
        }

    def test_one_bandcamp_job_unchanged(self):
        rows = [
            {"Artist Name": f"Artist {i}", "Profile URL": f"https://artist{i}.bandcamp.com/"}
            for i in range(10)
        ]
        states = [self._make_job_state("job_bandcamp_1", "bandcamp", bandcamp_rows=rows)]
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, states, logger)
        self.assertTrue(os.path.exists(path))
        df = pd.read_csv(path)
        self.assertEqual(len(df), 10)
        logger.info.assert_any_call("[Master] Bandcamp directory CSVs -> %d files, %d rows", 1, 10)

    def test_three_bandcamp_jobs_all_incorporated(self):
        states = []
        for i in range(3):
            rows = [
                {
                    "Artist Name": f"Job{i} Artist {j}",
                    "Profile URL": f"https://job{i}artist{j}.bandcamp.com/",
                }
                for j in range(10)
            ]
            states.append(self._make_job_state(f"job_bandcamp_{i + 1}", "bandcamp", bandcamp_rows=rows))
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, states, logger)
        df = pd.read_csv(path)
        self.assertEqual(len(df), 30)

    def test_job_id_is_not_source_authority(self):
        rows = [{"Artist Name": "Artist A", "Profile URL": "https://artist.bandcamp.com/"}]
        states = [self._make_job_state("job_spotify_1", "bandcamp", bandcamp_rows=rows)]
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, states, logger)
        df = pd.read_csv(path)
        self.assertEqual(len(df), 1)

    def test_mixed_sources_not_absorbed(self):
        bc_rows = [{"Artist Name": "BC Artist", "Profile URL": "https://bc.bandcamp.com/"}]
        sc_rows = [{"Artist Name": "SC Artist", "Source URL": "https://soundcloud.com/sc"}]
        states = [
            self._make_job_state("job_bandcamp_1", "bandcamp", bandcamp_rows=bc_rows),
            self._make_job_state("job_soundcloud_1", "soundcloud", generic_rows=sc_rows),
            self._make_job_state("job_spotify_1", "spotify", generic_rows=[{"Artist Name": "SP Artist"}]),
        ]
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, states, logger)
        df = pd.read_csv(path)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Artist Name"], "BC Artist")

    def test_duplicates_deduped_by_canonical_url(self):
        rows1 = [{"Artist Name": "Artist A", "Profile URL": "https://artist.bandcamp.com/"}]
        rows2 = [{"Artist Name": "Artist A (variant)", "Profile URL": "https://artist.bandcamp.com/album/x"}]
        states = [
            self._make_job_state("job_bandcamp_1", "bandcamp", bandcamp_rows=rows1),
            self._make_job_state("job_bandcamp_2", "bandcamp", bandcamp_rows=rows2),
        ]
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, states, logger)
        df = pd.read_csv(path)
        self.assertEqual(len(df), 1)

    def test_missing_optional_file_skipped_safely(self):
        rows = [{"Artist Name": "Artist A", "Profile URL": "https://artist.bandcamp.com/"}]
        state1 = self._make_job_state("job_bandcamp_1", "bandcamp", bandcamp_rows=rows)
        state2 = self._make_job_state("job_bandcamp_2", "bandcamp", generic_rows=[{"Artist Name": "Artist B"}])
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, [state1, state2], logger)
        df = pd.read_csv(path)
        # state1 contributes bandcamp_enriched.csv; state2 falls back to generic enriched.csv
        self.assertEqual(len(df), 2)

    def test_empty_file_does_not_break_aggregation(self):
        rows = [{"Artist Name": "Artist A", "Profile URL": "https://artist.bandcamp.com/"}]
        state1 = self._make_job_state("job_bandcamp_1", "bandcamp", bandcamp_rows=rows)
        state2 = self._make_job_state("job_bandcamp_2", "bandcamp", bandcamp_rows=[])
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, [state1, state2], logger)
        df = pd.read_csv(path)
        self.assertEqual(len(df), 1)

    def test_corrupt_file_does_not_break_aggregation(self):
        rows = [{"Artist Name": "Artist A", "Profile URL": "https://artist.bandcamp.com/"}]
        state1 = self._make_job_state("job_bandcamp_1", "bandcamp", bandcamp_rows=rows)
        state2 = self._make_job_state("job_bandcamp_2", "bandcamp")
        corrupt_path = os.path.join(os.path.dirname(state2["raw_csv"]), "bandcamp_enriched.csv")
        with open(corrupt_path, "wb") as f:
            f.write(b"\xff\xfe\x00\x80")
        logger = mock.Mock(spec=["info", "warning"])

        path = night_mode_runner._build_bandcamp_aggregate_csv(self.run_dir, [state1, state2], logger)
        df = pd.read_csv(path)
        self.assertEqual(len(df), 1)
        logger.warning.assert_called_once()

    def test_runner_aliases_the_single_shared_implementation(self):
        self.assertIs(
            night_mode_runner._build_bandcamp_aggregate_csv,
            night_mode_bandcamp.build_bandcamp_aggregate_csv,
        )


class PhasedBandcampAggregateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_dir = os.path.join(self.tmpdir.name, "run")
        os.makedirs(self.run_dir, exist_ok=True)

    def _job(self, job_id, source_directory, rows=None, *, output="bandcamp"):
        job_dir = os.path.join(self.run_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        raw_csv = os.path.join(job_dir, "raw.csv")
        raw_rows = rows or [{"Artist Name": job_id, "Profile URL": ""}]
        pd.DataFrame(raw_rows).to_csv(raw_csv, index=False)
        state = {
            "status": "completed",
            "directory": job_dir,
            "source_directory": source_directory,
            "raw_csv": raw_csv,
            "row_count": len(raw_rows),
            "schema_hash": "",
        }
        if output == "bandcamp":
            pd.DataFrame(rows or []).to_csv(os.path.join(job_dir, "bandcamp_enriched.csv"), index=False)
        elif output == "empty":
            open(os.path.join(job_dir, "bandcamp_enriched.csv"), "w", encoding="utf-8").close()
        elif output == "corrupt":
            with open(os.path.join(job_dir, "bandcamp_enriched.csv"), "wb") as f:
                f.write(b"\xff\xfe\x00\x80")
        return state

    def _run_phase(self, jobs):
        master_raw = os.path.join(self.run_dir, "master_raw.csv")
        pd.DataFrame([{"Artist Name": "Seed Artist", "Email": ""}]).to_csv(master_raw, index=False)
        captured = []

        def fake_master_enrichment(input_csv, output_csv, **kwargs):
            captured.append(kwargs.get("bandcamp_csv_path", ""))
            shutil.copyfile(input_csv, output_csv)
            return output_csv

        def fake_enrichment(input_csv, output_csv, **kwargs):
            shutil.copyfile(input_csv, output_csv)
            return output_csv

        seed_result = {
            "schema_version": "2.0",
            "phases": {"seed": {"status": "completed", "jobs": jobs}},
        }
        with mock.patch.object(night_mode_runner, "_merge_raw_master", return_value=master_raw), mock.patch.object(
            pipeline_runner, "run_master_enrichment", side_effect=fake_master_enrichment
        ), mock.patch.object(pipeline_runner, "run_enrichment", side_effect=fake_enrichment):
            result = phased_runner.run_enrich_phase(
                {"jobs": [], "master_enrichment": {"enable_live_search": False}},
                self.run_dir,
                seed_result,
            )
        self.assertEqual(result["phases"]["enrich"]["status"], "completed")
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_one_bandcamp_job_passes_run_scoped_aggregate(self):
        rows = [{"Artist Name": "Solo", "Profile URL": "https://solo.bandcamp.com/"}]
        aggregate_path = self._run_phase({"anything": self._job("anything", "bandcamp", rows)})

        self.assertEqual(aggregate_path, os.path.join(self.run_dir, "bandcamp_enriched_aggregate.csv"))
        self.assertEqual(pd.read_csv(aggregate_path)["Artist Name"].tolist(), ["Solo"])

    def test_phased_path_aggregates_metadata_selected_jobs_and_skips_bad_outputs(self):
        jobs = {
            "misleading_spotify_id": self._job(
                "misleading_spotify_id",
                "bandcamp",
                [{"Artist Name": "First", "Profile URL": "https://shared.bandcamp.com/"}],
            ),
            "second": self._job(
                "second",
                "bandcamp",
                [
                    {"Artist Name": "Duplicate", "Profile URL": "https://shared.bandcamp.com/album/release"},
                    {"Artist Name": "Second", "Profile URL": "https://second.bandcamp.com/"},
                ],
            ),
            "third": self._job(
                "third",
                "bandcamp",
                [{"Artist Name": "Third", "Profile URL": "https://third.bandcamp.com/"}],
            ),
            "soundcloud_named_bandcamp": self._job(
                "soundcloud_named_bandcamp",
                "soundcloud",
                [{"Artist Name": "Excluded", "Profile URL": "https://excluded.bandcamp.com/"}],
            ),
            "missing": self._job("missing", "bandcamp", output="missing"),
            "empty": self._job("empty", "bandcamp", output="empty"),
            "corrupt": self._job("corrupt", "bandcamp", output="corrupt"),
        }

        aggregate_path = self._run_phase(jobs)
        aggregate = pd.read_csv(aggregate_path, dtype=str, keep_default_na=False)

        self.assertEqual(aggregate["Artist Name"].tolist(), ["First", "Second", "Third"])
        self.assertNotIn("Excluded", aggregate["Artist Name"].tolist())


class BandcampAggregateEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.run_root = os.path.join(self.tmpdir.name, "overnight_runs")
        os.makedirs(self.run_root, exist_ok=True)

    def test_runner_passes_aggregate_to_master_enrichment(self):
        config_path = os.path.join(self.tmpdir.name, "config.json")
        config = {
            "export_mode": "both",
            "master_enrichment": {"enabled": True},
            "jobs": [
                {"job_id": "job_bc_1", "directory": "bandcamp", "target_valid_leads": 2},
                {"job_id": "job_bc_2", "directory": "bandcamp", "target_valid_leads": 2},
            ],
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        def fake_run_directory_job(job_config, raw_output_path, logger=None):
            job_dir = os.path.dirname(raw_output_path)
            rows = [
                {
                    "Artist Name": f"{job_config['job_id']}_artist",
                    "Profile URL": "https://shared.bandcamp.com/",
                    "Email": "",
                }
            ]
            pd.DataFrame(rows).to_csv(raw_output_path, index=False)
            bc_path = os.path.join(job_dir, "bandcamp_enriched.csv")
            pd.DataFrame(rows).to_csv(bc_path, index=False)
            return raw_output_path

        def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None, night_mode=False):
            shutil.copyfile(raw_csv_path, enriched_output_path)
            return enriched_output_path

        captured = []

        def fake_run_master_enrichment(
            input_csv, output_csv, logger=None, enable_live_search=True, max_live_searches=None, night_mode=False, **kwargs
        ):
            captured.append(kwargs.get("bandcamp_csv_path", ""))
            shutil.copyfile(input_csv, output_csv)
            return output_csv

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

        with mock.patch.object(
            night_mode_runner, "run_directory_job", side_effect=fake_run_directory_job
        ), mock.patch.object(night_mode_runner, "run_enrichment", side_effect=fake_run_enrichment), mock.patch.object(
            night_mode_runner, "run_master_enrichment", side_effect=fake_run_master_enrichment
        ), mock.patch.object(
            night_mode_runner, "run_facebook_global_pass_nightmode", side_effect=fake_fb_pass
        ):
            result = night_mode_runner.run_night_mode(config_path, run_root=self.run_root)

        self.assertTrue(captured)
        aggregate_path = captured[0]
        self.assertTrue(aggregate_path.endswith("bandcamp_enriched_aggregate.csv"))
        df = pd.read_csv(aggregate_path)
        self.assertEqual(len(df), 1)


if __name__ == "__main__":
    unittest.main()
