import os
import types
import importlib.util
import unittest
from unittest import mock

import pandas as pd
import requests

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
        enricher._T007_SC_HELPER = None
        enricher._T007_SC_HELPER_LOADED = False
        enricher._SC_TRACKS_API_FALLBACK_LOGGED = False

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

    def test_fetch_url_respects_max_attempts(self) -> None:
        worker = self._make_worker()
        worker.session = mock.Mock()

        class FakeResp:
            def __init__(self, status_code: int):
                self.status_code = status_code
                self.text = "error"

            def raise_for_status(self):
                raise requests.HTTPError(response=self)

        worker.session.get.side_effect = [FakeResp(500), FakeResp(500)]
        worker._fetch_url("http://example.com", "test", max_attempts=1)
        self.assertEqual(worker.session.get.call_count, 1)

        worker.session.get.reset_mock()
        worker.session.get.side_effect = [FakeResp(500), FakeResp(500)]
        worker._fetch_url("http://example.com", "test")
        self.assertEqual(worker.session.get.call_count, 2)

    def test_t007_helper_cached_once(self) -> None:
        calls = {"spec": 0, "exec": 0}

        def fake_spec(*args, **kwargs):
            calls["spec"] += 1
            spec = mock.Mock()

            def exec_module(mod):
                calls["exec"] += 1
                mod._sc_fetch_contact_payload = lambda handle: {"data": {"emails": [], "external_urls": []}}

            loader = mock.Mock()
            loader.exec_module.side_effect = exec_module
            spec.loader = loader
            return spec

        with mock.patch.object(importlib.util, "spec_from_file_location", side_effect=fake_spec), mock.patch.object(
            importlib.util, "module_from_spec", side_effect=lambda spec: types.SimpleNamespace()
        ):
            helper1 = enricher._get_t007_sc_helper()
            helper2 = enricher._get_t007_sc_helper()

        self.assertEqual(calls["spec"], 1)
        self.assertEqual(calls["exec"], 1)
        self.assertIs(helper1, helper2)

    def test_html_challenges_force_rss_only_without_fallback_block(self) -> None:
        worker = self._make_worker()
        worker.night_mode = True
        worker._live_context = {"song_title": "", "location": "", "track": "", "genre": ""}

        logs: list = []

        class Log:
            @staticmethod
            def emit(msg, *args, **kwargs):
                logs.append(str(msg))

        worker.log_message = Log()

        # Simulate prior html challenges triggering rss_only mode.
        worker._sc_html_challenge_count = 2
        worker._sc_enter_rss_only_mode(reason="consecutive_challenges")

        # Avoid network: no candidates from search.
        worker._night_sc_search_candidates = lambda *args, **kwargs: None

        # RSS payload succeeds; no HTML fallback attempted.
        payload = EnrichmentPayload(
            socials=set(),
            websites={"https://soundcloud.com/test-handle"},
            emails=set(),
            link_hubs=set(),
            source_dir="soundcloud",
            source_url="https://soundcloud.com/test-handle",
            source_detail=_format_source_display("soundcloud_live"),
        )
        worker._sc_build_rss_payload = lambda handle, base_payload, row_idx=None: (payload, True, True, "rss_success")
        worker._apply_payload_guarded = lambda *args, **kwargs: True
        worker._finalize_night_sc = lambda *args, **kwargs: None

        original_flags = enricher._SC_SHARED_ENGINE.get_run_flags
        enricher._SC_SHARED_ENGINE.get_run_flags = lambda: {
            "root_fetch_disabled": 0,
            "tracks_api_blocked": 0,
            "used_rss": 1,
        }

        df = pd.DataFrame([{"SoundCloud Link": "https://soundcloud.com/test-handle"}])
        try:
            result = worker._night_sc_attempt_row(df, 0, "Test Artist")
        finally:
            enricher._SC_SHARED_ENGINE.get_run_flags = original_flags

        self.assertTrue(result)
        self.assertTrue(any("rss_only=1" in msg for msg in logs))
        self.assertFalse(any("fallback_blocked" in msg for msg in logs))

    def test_tracks_api_block_allows_html_fallback_by_default(self) -> None:
        worker = self._make_worker()
        worker.night_mode = True
        worker._live_context = {"song_title": "", "location": "", "track": "", "genre": ""}
        worker._night_sc_cache_lookup = lambda handle, profile_url: None

        # Simulate RSS attempt failing but available.
        worker._sc_build_rss_payload = lambda handle, base_payload, row_idx=None: (None, False, True, "rss_fail")

        fallback_called = {"count": 0}

        def fake_fallback(url, attempt):
            fallback_called["count"] += 1
            return (
                EnrichmentPayload(
                    socials=set(),
                    websites=set(),
                    emails=set(),
                    link_hubs=set(),
                    source_dir="soundcloud",
                    source_url=url,
                    source_detail=_format_source_display("soundcloud_live"),
                ),
                True,
            )

        worker._night_sc_fetch_profile_payload = fake_fallback
        worker._apply_payload_guarded = lambda *args, **kwargs: True
        worker._finalize_night_sc = lambda *args, **kwargs: None

        original_get_flags = enricher._SC_SHARED_ENGINE.get_run_flags
        enricher._SC_SHARED_ENGINE.get_run_flags = lambda: {
            "root_fetch_disabled": 0,
            "tracks_api_blocked": 1,
            "used_rss": 0,
        }

        df = pd.DataFrame([{"SoundCloud Link": "https://soundcloud.com/test-handle"}])
        try:
            with mock.patch.dict(os.environ, {"SC_ALLOW_FALLBACK_ON_TRACKS_401_403": "1"}):
                result = worker._night_sc_attempt_row(df, 0, "Test Artist")
        finally:
            enricher._SC_SHARED_ENGINE.get_run_flags = original_get_flags

        self.assertTrue(result)
        self.assertGreaterEqual(fallback_called["count"], 1)

    def test_night_sc_attempt_uses_promoted_unearthed_seed_url_first(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "Artist Name": "jski",
                    "Source Directory": "Triple J Unearthed",
                    "Social Link": "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P",
                    "External Links": "",
                    "SoundCloud Link": "",
                    "SC_Status": "",
                    "SC_Reason": "",
                }
            ]
        ).fillna("")
        enricher._apply_unearthed_platform_promotion_df(df)

        worker = self._make_worker()
        worker.night_mode = True
        worker._live_context = {"song_title": "", "location": "", "track": "", "genre": ""}

        seen = {}

        def fake_cache_lookup(handle, profile_url):
            seen["handle"] = handle
            seen["profile_url"] = profile_url
            return {"payload": None, "reason": "cache_hit", "http": 200, "fetches": 0, "confidence": 1.0, "match_score": 1.0}

        worker._night_sc_cache_lookup = fake_cache_lookup
        worker._apply_sc_snapshot_to_row = lambda *args, **kwargs: False
        worker._finalize_night_sc = lambda *args, **kwargs: None

        result = worker._night_sc_attempt_row(df, 0, "jski")

        self.assertFalse(result)
        self.assertEqual(df.at[0, "SoundCloud Link"], "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P")
        self.assertEqual(seen["profile_url"], "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P")

    def test_pick_best_soundcloud_candidate_prefers_metadata_aligned_ambiguous_match(self) -> None:
        worker = self._make_worker()
        worker._compute_match_score_for_candidate = lambda *args, **kwargs: 0.0

        candidates = [
            {
                "profile_url": "https://soundcloud.com/nova",
                "handle": "nova",
                "display_name": "Nova",
                "location": "Berlin, Germany",
                "context": "Berlin shoegaze artist. Latest single Crystal Skin.",
            },
            {
                "profile_url": "https://soundcloud.com/nova-official",
                "handle": "nova-official",
                "display_name": "Nova",
                "location": "Los Angeles, United States",
                "context": "Los Angeles hip hop producer.",
            },
        ]

        best = worker._pick_best_soundcloud_candidate(
            "Nova",
            candidates,
            location_hint="Berlin, Germany",
            genre_hint="shoegaze",
            song_title="Crystal Skin",
            track_hint="crystal skin",
        )

        self.assertIsNotNone(best)
        self.assertEqual(best["handle"], "nova")

    def test_pick_best_soundcloud_candidate_title_evidence_can_flip_close_name_match(self) -> None:
        worker = self._make_worker()
        worker._compute_match_score_for_candidate = lambda *args, **kwargs: 0.0

        candidates = [
            {
                "profile_url": "https://soundcloud.com/mooncrewlive",
                "handle": "mooncrewlive",
                "display_name": "Moon Crew Live",
                "location": "",
                "context": "Indie pop artist.",
            },
            {
                "profile_url": "https://soundcloud.com/mooncrewmusic",
                "handle": "mooncrewmusic",
                "display_name": "Moon Crew Music",
                "location": "",
                "context": "Latest track Static Hearts.",
            },
        ]

        without_title = worker._pick_best_soundcloud_candidate("Moon Crew", [dict(c) for c in candidates])
        with_title = worker._pick_best_soundcloud_candidate(
            "Moon Crew",
            [dict(c) for c in candidates],
            song_title="Static Hearts",
            track_hint="static hearts",
        )

        self.assertIsNotNone(without_title)
        self.assertIsNotNone(with_title)
        self.assertEqual(without_title["handle"], "mooncrewlive")
        self.assertEqual(with_title["handle"], "mooncrewmusic")

    def test_pick_best_soundcloud_candidate_preserves_selection_when_metadata_missing(self) -> None:
        worker = self._make_worker()
        worker._compute_match_score_for_candidate = lambda *args, **kwargs: 0.0

        candidates = [
            {
                "profile_url": "https://soundcloud.com/amber",
                "handle": "amber",
                "display_name": "Amber",
                "location": "",
                "context": "",
            },
            {
                "profile_url": "https://soundcloud.com/amber-music",
                "handle": "amber-music",
                "display_name": "Amber Music",
                "location": "",
                "context": "",
            },
        ]

        best = worker._pick_best_soundcloud_candidate("Amber", list(candidates))

        self.assertIsNotNone(best)
        self.assertEqual(best["handle"], "amber")
