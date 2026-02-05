import os
import unittest
from unittest import mock

import pandas as pd

import cross_directory_enricher as enricher
from cross_directory_enricher import (
    CrossDirectoryEnricherWorker,
    EnrichmentPayload,
    _format_source_display,
    _sc_handle_from_profile_url,
)


class SoundCloudEngineSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset once-per-run flag so tests remain isolated.
        enricher._SC_HEALTHCHECK_LOGGED = False

    def _make_worker(self) -> CrossDirectoryEnricherWorker:
        worker = CrossDirectoryEnricherWorker("seed.csv", "output.csv")
        worker.log_message = type("Log", (), {"emit": staticmethod(lambda *args, **kwargs: None)})()
        worker._init_row_enrichment_state()
        return worker

    def test_handle_parse(self) -> None:
        self.assertEqual(_sc_handle_from_profile_url("https://soundcloud.com/FooBar/"), "foobar")
        self.assertEqual(_sc_handle_from_profile_url("https://soundcloud.com/foo-bar?utm=1"), "foo-bar")
        self.assertEqual(_sc_handle_from_profile_url("https://soundcloud.com/foo/bar?utm=1"), "foo")
        self.assertIsNone(_sc_handle_from_profile_url("https://example.com/foo"))

    def test_blocked_marks_status(self) -> None:
        worker = self._make_worker()
        df = pd.DataFrame([{"SC_Status": "", "final_status": ""}])
        worker._flag_sc_blocked(status_code=403, html="Access Denied")
        marked = worker._mark_sc_blocked_row(df, 0)
        self.assertTrue(marked)
        self.assertEqual(df.at[0, "SC_Status"], "blocked_403")
        self.assertEqual(worker._row_enrichment_state.get("soundcloud"), "skipped")

    def test_engine_switch_does_not_run_in_legacy_mode(self) -> None:
        worker = self._make_worker()
        worker.night_mode = False
        worker._live_context = {"artist": "Legacy Tester", "song_title": ""}
        candidate = {
            "profile_url": "https://soundcloud.com/legacy-test",
            "score": 1.0,
            "rank_score": 1.0,
            "display_name": "Legacy Test",
            "handle": "legacy-test",
        }
        worker._soundcloud_people_search_candidates = lambda query: [candidate]
        worker._pick_best_soundcloud_candidate = lambda artist_name, candidates, location_hint, genre_hint: candidates[0]
        worker._soundcloud_universal_search_candidates = lambda query: []
        fetch_calls = {"count": 0}

        def fake_fetch(url, source_dir):
            fetch_calls["count"] += 1
            return EnrichmentPayload(
                socials=set(),
                websites=set(),
                emails=set(),
                link_hubs=set(),
                source_dir=source_dir,
                source_url=url,
                source_detail=_format_source_display("soundcloud_live"),
            )

        worker._fetch_profile_and_build = fake_fetch
        with mock.patch.dict(os.environ, {"NIGHTMODE_SC_ENGINE": "t007"}):
            payload = worker._live_search_soundcloud("Legacy Tester")

        self.assertIsNotNone(payload)
        self.assertEqual(fetch_calls["count"], 1)
        self.assertEqual(payload.source_url, candidate["profile_url"])
