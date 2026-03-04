import io
import contextlib
import unittest
from unittest import mock

import requests

import soundcloud_engine as sc


class FakeResp:
    def __init__(self, url: str, status_code: int = 200, text: str = ""):
        self.url = url
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.headers = {}
        self.calls = {}

    def get(self, url, timeout=None, headers=None):
        self.calls[url] = self.calls.get(url, 0) + 1
        return self.mapping.get(url, FakeResp(url, 404, ""))


class SoundCloudAggregatorTests(unittest.TestCase):
    def setUp(self):
        # Avoid touching cache on disk and speed up sleeps.
        self.cache_set_patcher = mock.patch.object(sc.SC_ABOUT_CACHE, "set", lambda *a, **k: None)
        self.cache_get_patcher = mock.patch.object(sc.SC_ABOUT_CACHE, "get", lambda *a, **k: None)
        self.sleep_patcher = mock.patch.object(sc, "polite_sleep", lambda *a, **k: None)
        self.cache_set_patcher.start()
        self.cache_get_patcher.start()
        self.sleep_patcher.start()

    def tearDown(self):
        self.cache_set_patcher.stop()
        self.cache_get_patcher.stop()
        self.sleep_patcher.stop()
        sc._AGGREGATOR_BUDGET_CHECK = None

    def _build_mapping(self, handle: str, about_html: str, aggregator_url: str = None, aggregator_html: str = "", aggregator_status: int = 200):
        root_url = f"https://soundcloud.com/{handle}"
        about_url = f"{root_url}/about"
        mapping = {
            about_url: FakeResp(about_url, 200, about_html),
            root_url: FakeResp(root_url, 200, ""),
        }
        if aggregator_url:
            mapping[aggregator_url] = FakeResp(aggregator_url, aggregator_status, aggregator_html)
        return mapping

    def test_allowlisted_aggregator_email_is_extracted(self):
        handle = "daykat"
        aggregator_url = "https://linktr.ee/daykat"
        about_html = f'<a href="{aggregator_url}">links</a>'
        agg_html = "Book us at booking@daykatmusic.com"
        session = FakeSession(self._build_mapping(handle, about_html, aggregator_url, agg_html))

        payload = sc.extract_sc_links(session, handle)

        self.assertIn("booking@daykatmusic.com", payload.get("emails", []))

    def test_non_allowlisted_domain_is_not_fetched(self):
        handle = "noagg"
        about_html = '<a href="https://example.com/card">card</a>'
        session = FakeSession(self._build_mapping(handle, about_html))

        payload = sc.extract_sc_links(session, handle)

        self.assertEqual(session.calls.get("https://example.com/card"), None)
        self.assertFalse(payload.get("emails"))

    def test_multiple_aggregators_fetches_only_first(self):
        handle = "multi"
        first = "https://linktr.ee/multi"
        second = "https://beacons.ai/multi"
        about_html = f'<a href="{first}">one</a><a href="{second}">two</a>'
        mapping = self._build_mapping(handle, about_html, first, "a@b.com")
        session = FakeSession(mapping)

        sc.extract_sc_links(session, handle)

        total_fetches = (session.calls.get(first, 0) + session.calls.get(second, 0))
        self.assertEqual(total_fetches, 1)

    def test_preference_order_beats_lexical(self):
        handle = "pref"
        preferred = "https://linktr.ee/pref"
        secondary = "https://beacons.ai/pref"
        about_html = f'<a href="{secondary}">b</a><a href="{preferred}">l</a>'
        mapping = self._build_mapping(handle, about_html, preferred, "contact@preferred.com")
        session = FakeSession(mapping)

        payload = sc.extract_sc_links(session, handle)

        self.assertIn("contact@preferred.com", payload.get("emails", []))
        self.assertEqual(session.calls.get(preferred, 0), 1)
        self.assertIsNone(session.calls.get(secondary))

    def test_fetch_failure_is_swallowed(self):
        handle = "fail"
        aggregator_url = "https://linktr.ee/fail"
        about_html = f'<a href="{aggregator_url}">links</a>'
        mapping = self._build_mapping(handle, about_html, aggregator_url, "", aggregator_status=500)
        session = FakeSession(mapping)

        payload = sc.extract_sc_links(session, handle)

        self.assertEqual(payload.get("emails"), [])
        # Still actionable because external link exists.
        self.assertEqual(payload.get("status"), "actionable")

    def test_budget_skip_logs_and_does_not_fetch(self):
        handle = "budget"
        aggregator_url = "https://linktr.ee/budget"
        about_html = f'<a href="{aggregator_url}">links</a>'
        session = FakeSession(self._build_mapping(handle, about_html, aggregator_url, "contact@x.com"))

        sc._AGGREGATOR_BUDGET_CHECK = lambda: (False, "max_live")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc.extract_sc_links(session, handle)
        output = buf.getvalue()

        self.assertIn("[Aggregator] skipped: max_live", output)
        self.assertEqual(session.calls.get(aggregator_url, 0), 0)

    def test_allowlisted_aggregator_not_double_fetched(self):
        handle = "nodouble"
        aggregator_url = "https://linktr.ee/nodouble"
        about_html = f'<a href="{aggregator_url}">links</a>'
        mapping = self._build_mapping(handle, about_html, aggregator_url, "hello@artist.com")
        session = FakeSession(mapping)
        with mock.patch.object(sc, "expand_for_email", wraps=sc.expand_for_email) as expand_mock:
            payload = sc.extract_sc_links(session, handle)

        self.assertIn("hello@artist.com", payload.get("emails", []))
        self.assertEqual(session.calls.get(aggregator_url, 0), 1)
        self.assertEqual(expand_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main()
