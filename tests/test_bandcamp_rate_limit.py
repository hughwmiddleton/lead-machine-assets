import importlib.util
from pathlib import Path

import pytest


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload or {}


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.call_idx = 0
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        resp = self.responses[self.call_idx]
        self.call_idx += 1
        return resp


# ---------------------------------------------------------------------------
#  429 + Retry-After
# ---------------------------------------------------------------------------

def test_429_with_retry_after_sleeps_and_retries():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=3)
    sleeps = []
    pacing.sleep_retry = lambda attempt, retry_after=None: sleeps.append((attempt, retry_after)) or 0.0
    pacing.sleep_cursor = lambda: None
    pacing._ensure_gap = lambda: None

    batch_ok = {
        "results": [{"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"}],
        "cursor": None,
        "batch_result_count": 1,
    }

    responses = [
        _FakeResponse(429, headers={"Retry-After": "30"}),
        _FakeResponse(200, payload=batch_ok),
    ]
    fake_session = _FakeSession(responses)

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        assert len(results) == 1
        assert len(sleeps) == 1
        assert sleeps[0] == (0, 30)
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing


# ---------------------------------------------------------------------------
#  429 without Retry-After -> bounded exponential backoff
# ---------------------------------------------------------------------------

def test_429_without_retry_after_uses_bounded_exponential_backoff():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=3)
    sleeps = []
    pacing.sleep_retry = lambda attempt, retry_after=None: sleeps.append((attempt, retry_after)) or 0.0
    pacing.sleep_cursor = lambda: None
    pacing._ensure_gap = lambda: None

    batch_ok = {
        "results": [{"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"}],
        "cursor": None,
        "batch_result_count": 1,
    }

    responses = [
        _FakeResponse(429, headers={}),
        _FakeResponse(200, payload=batch_ok),
    ]
    fake_session = _FakeSession(responses)

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        assert len(results) == 1
        assert len(sleeps) == 1
        assert sleeps[0][0] == 0
        assert sleeps[0][1] is None
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing


# ---------------------------------------------------------------------------
#  Successful retry after one 429
# ---------------------------------------------------------------------------

def test_successful_retry_after_one_429():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=3)
    pacing.sleep_retry = lambda attempt, retry_after=None: 0.0
    pacing.sleep_cursor = lambda: None
    pacing._ensure_gap = lambda: None

    batch_ok = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
            {"item_id": 2, "band_url": "https://artist2.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": None,
        "batch_result_count": 2,
    }

    responses = [
        _FakeResponse(429, headers={"Retry-After": "5"}),
        _FakeResponse(200, payload=batch_ok),
    ]
    fake_session = _FakeSession(responses)

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        assert len(results) == 2
        assert fake_session.call_idx == 2
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing


# ---------------------------------------------------------------------------
#  Retries exhausted -> safe controlled failure/fallback
# ---------------------------------------------------------------------------

def test_retries_exhausted_returns_already_collected_candidates():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=2)
    pacing.sleep_retry = lambda attempt, retry_after=None: 0.0
    pacing.sleep_cursor = lambda: None
    pacing._ensure_gap = lambda: None

    batch1 = {
        "results": [{"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"}],
        "cursor": "cursor_1",
        "batch_result_count": 1,
    }

    responses = [
        _FakeResponse(200, payload=batch1),
        _FakeResponse(429, headers={}),
        _FakeResponse(429, headers={}),
        _FakeResponse(429, headers={}),
    ]
    fake_session = _FakeSession(responses)

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        # Should preserve candidate from batch 1 and stop cleanly when retries exhausted on batch 2
        assert len(results) == 1
        assert results[0]["url"] == "https://artist1.bandcamp.com/"
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing


# ---------------------------------------------------------------------------
#  Non-429 errors preserve existing behaviour
# ---------------------------------------------------------------------------

def test_non_429_error_breaks_and_returns_collected():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=3)
    pacing.sleep_retry = lambda attempt, retry_after=None: 0.0
    pacing.sleep_cursor = lambda: None
    pacing._ensure_gap = lambda: None

    batch1 = {
        "results": [{"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"}],
        "cursor": "cursor_1",
        "batch_result_count": 1,
    }

    class _ErrorSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json})
            raise Exception("Network timeout")

    fake_session = _ErrorSession()

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        # Non-429 errors are retried up to max_retries, then break and return empty
        assert results == []
        assert len(fake_session.calls) == pacing.max_retries + 1
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing


# ---------------------------------------------------------------------------
#  Sequential Bandcamp jobs share pacing policy without corrupting state
# ---------------------------------------------------------------------------

def test_sequential_jobs_share_pacing_state():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=3)
    call_log = []
    pacing.sleep_retry = lambda attempt, retry_after=None: call_log.append("retry") or 0.0
    pacing.sleep_cursor = lambda: call_log.append("cursor")
    pacing._ensure_gap = lambda: call_log.append("gap")

    batch1 = {
        "results": [{"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"}],
        "cursor": "cursor_1",
        "batch_result_count": 1,
    }
    batch2 = {
        "results": [],
        "cursor": None,
        "batch_result_count": 0,
    }

    responses = [
        _FakeResponse(200, payload=batch1), _FakeResponse(200, payload=batch2),
        _FakeResponse(200, payload=batch1), _FakeResponse(200, payload=batch2),
    ]
    fake_session = _FakeSession(responses)

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        # Run two "jobs" sequentially using the same module-level pacing
        r1 = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        r2 = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        assert len(r1) == 1
        assert len(r2) == 1
        # Both should have used the shared pacing object without corruption
        assert "gap" in call_log
        assert "cursor" in call_log
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing


# ---------------------------------------------------------------------------
#  Pacing sleep is mockable so unit tests remain fast
# ---------------------------------------------------------------------------

def test_pacing_sleep_is_mockable_for_fast_tests():
    lm = _load_legacy_module()

    pacing = lm.BandcampPacingPolicy(cursor_delay_ms=(0, 0), min_gap_ms=0, max_retries=3)
    cursor_sleeps = 0
    retry_sleeps = 0

    def track_cursor():
        nonlocal cursor_sleeps
        cursor_sleeps += 1

    def track_retry(attempt, retry_after=None):
        nonlocal retry_sleeps
        retry_sleeps += 1
        return 0.0

    pacing.sleep_cursor = track_cursor
    pacing.sleep_retry = track_retry
    pacing._ensure_gap = lambda: None

    batch = {
        "results": [{"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"}],
        "cursor": None,
        "batch_result_count": 1,
    }

    responses = [_FakeResponse(200, payload=batch)]
    fake_session = _FakeSession(responses)

    original_session = lm._bandcamp_session
    lm._bandcamp_session = lambda: fake_session
    original_pacing = lm._BANDCAMP_PACING
    lm._BANDCAMP_PACING = pacing

    try:
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "rock",
            "location": "london",
            "location_label": "london",
            "sort": "new",
            "raw_slug": "rock+london",
            "geoname_id": 2643743,
            "api_tags": ["rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=10)
        assert len(results) == 1
        assert cursor_sleeps == 0  # single batch, no cursor sleep needed
        assert retry_sleeps == 0   # no 429, no retry sleep needed
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session
        lm._BANDCAMP_PACING = original_pacing
