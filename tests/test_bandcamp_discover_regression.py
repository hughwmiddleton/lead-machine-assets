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
#  Discover URL parsing
# ---------------------------------------------------------------------------

def test_discover_url_indie_rock_london_uk():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/indie-rock+london-uk?s=new"
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == "indie-rock"
    assert filters["location"] == "london uk"
    assert filters["location_label"] == "london uk"
    assert filters["sort"] == "new"


def test_discover_url_rock_london():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/rock+london?s=new"
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == "rock"
    assert filters["location"] == "london"
    assert filters["location_label"] == "london"


def test_discover_url_genre_only_no_false_location():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/indie-rock?s=new"
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == "indie-rock"
    assert filters["location"] == ""
    assert filters["location_label"] == ""


def test_discover_url_location_only():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/london?s=new"
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == ""
    assert filters["location"] == "london"


def test_discover_url_manchester_uk():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/rock+manchester-uk?s=new"
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == "rock"
    assert filters["location"] == "manchester uk"


def test_discover_url_bristol_uk():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/electronic+bristol-uk?s=new"
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == "electronic"
    assert filters["location"] == "bristol uk"


def test_discover_url_explicit_loc_param():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/rock?s=new&loc=2643743"
    # We can't resolve the numeric loc without fetching, but the function should
    # at least preserve it gracefully.
    filters = lm._bandcamp_parse_discover_filters(url)
    assert filters["genre"] == "rock"


def test_location_label_from_url_uses_slug_classification():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/indie-rock+london-uk?s=new"
    meta = lm._bandcamp_location_label_from_url(url)
    assert meta["display_label"] == "london uk"
    assert meta["hint"] == "london uk"


def test_location_label_from_url_genre_only_returns_empty():
    lm = _load_legacy_module()
    url = "https://bandcamp.com/discover/indie-rock?s=new"
    meta = lm._bandcamp_location_label_from_url(url)
    assert meta["display_label"] == ""
    assert meta["hint"] == ""


# ---------------------------------------------------------------------------
#  Location filter sanitization
# ---------------------------------------------------------------------------

def test_sanitize_location_filter_rejects_rock():
    lm = _load_legacy_module()
    assert lm._bc_sanitize_location_filter("rock") == ""


def test_sanitize_location_filter_rejects_indie_rock():
    lm = _load_legacy_module()
    assert lm._bc_sanitize_location_filter("indie-rock") == ""


def test_sanitize_location_filter_accepts_london():
    lm = _load_legacy_module()
    assert lm._bc_sanitize_location_filter("london") == "london"


def test_sanitize_location_filter_accepts_london_uk():
    lm = _load_legacy_module()
    assert lm._bc_sanitize_location_filter("london uk") == "london uk"


# ---------------------------------------------------------------------------
#  Location matching
# ---------------------------------------------------------------------------

def test_london_profile_passes_london_target():
    lm = _load_legacy_module()
    assert lm._bandcamp_location_match_("London, Uk", "", "london", "london")
    assert lm._bandcamp_location_match_("London, Uk", "", "london uk", "london uk")


def test_dartford_profile_fails_london_target():
    lm = _load_legacy_module()
    # Dartford is near London but not London; city-level intent must not collapse to UK-wide
    assert not lm._bandcamp_location_match_("Dartford, Uk", "", "london", "london")
    assert not lm._bandcamp_location_match_("Dartford, Uk", "", "london uk", "london uk")


def test_melbourne_profile_fails_london_target():
    lm = _load_legacy_module()
    assert not lm._bandcamp_location_match_("Melbourne, Australia", "", "london", "london")


def test_lima_profile_fails_london_target():
    lm = _load_legacy_module()
    assert not lm._bandcamp_location_match_("Lima, Peru", "", "london uk", "london uk")


def test_london_united_kingdom_same_city_semantics():
    lm = _load_legacy_module()
    assert lm._bandcamp_location_match_("London, United Kingdom", "", "london united kingdom", "london united kingdom")
    assert not lm._bandcamp_location_match_("Dartford, United Kingdom", "", "london united kingdom", "london united kingdom")
    assert not lm._bandcamp_location_match_("Manchester, United Kingdom", "", "london united kingdom", "london united kingdom")


def test_manchester_uk_target_is_city_level():
    lm = _load_legacy_module()
    assert lm._bandcamp_location_match_("Manchester, Uk", "", "manchester uk", "manchester uk")
    assert not lm._bandcamp_location_match_("London, Uk", "", "manchester uk", "manchester uk")
    assert not lm._bandcamp_location_match_("Bristol, Uk", "", "manchester uk", "manchester uk")


def test_bristol_uk_target_is_city_level():
    lm = _load_legacy_module()
    assert lm._bandcamp_location_match_("Bristol, Uk", "", "bristol uk", "bristol uk")
    assert not lm._bandcamp_location_match_("London, Uk", "", "bristol uk", "bristol uk")
    assert not lm._bandcamp_location_match_("Manchester, Uk", "", "bristol uk", "bristol uk")


def test_uk_country_level_target_matches_any_uk():
    lm = _load_legacy_module()
    assert lm._bandcamp_location_match_("London, Uk", "", "uk", "uk")
    assert lm._bandcamp_location_match_("Manchester, Uk", "", "uk", "uk")
    assert lm._bandcamp_location_match_("Bristol, Uk", "", "uk", "uk")
    assert lm._bandcamp_location_match_("Dartford, Uk", "", "uk", "uk")
    assert lm._bandcamp_location_match_("England, Uk", "", "uk", "uk")


def test_united_kingdom_country_level_target_matches_any_uk():
    lm = _load_legacy_module()
    assert lm._bandcamp_location_match_("London, Uk", "", "united kingdom", "united kingdom")
    assert lm._bandcamp_location_match_("Glasgow, Uk", "", "united kingdom", "united kingdom")


# ---------------------------------------------------------------------------
#  Pagination regression
# ---------------------------------------------------------------------------

def test_discover_pagination_stops_on_duplicate_page():
    lm = _load_legacy_module()

    class FakeDriver:
        def __init__(self, pages):
            self.pages = pages
            self.call_idx = -1

        def get(self, url):
            self.call_idx += 1

        def execute_script(self, script):
            pass

        def find_elements(self, by, selector):
            return []

        @property
        def page_source(self):
            if self.call_idx < len(self.pages):
                return self.pages[self.call_idx]
            return ""

    # Build two identical pages of HTML with 3 tiles each
    tile_html = (
        '<div class="discover-item">'
        '<a href="https://artist1.bandcamp.com/">A1</a>'
        '</div>'
        '<div class="discover-item">'
        '<a href="https://artist2.bandcamp.com/">A2</a>'
        '</div>'
        '<div class="discover-item">'
        '<a href="https://artist3.bandcamp.com/">A3</a>'
        '</div>'
    )
    pages = [tile_html, tile_html]
    driver = FakeDriver(pages)

    # Monkey-patch the wait function to return tile counts without real selenium
    original_wait = lm._bandcamp_wait_for_discover_tiles
    lm._bandcamp_wait_for_discover_tiles = lambda driver, selectors, timeout=15: 3

    try:
        results = lm._bandcamp_collect_discover_dom(driver, "https://bandcamp.com/discover/rock+london?s=new", max_pages=2)
        # Should stop after page 1 because page 2 is a duplicate
        assert len(results) == 3
    finally:
        lm._bandcamp_wait_for_discover_tiles = original_wait


def test_discover_pagination_continues_when_pages_differ():
    lm = _load_legacy_module()

    class FakeDriver:
        def __init__(self, pages):
            self.pages = pages
            self.call_idx = -1

        def get(self, url):
            self.call_idx += 1

        def execute_script(self, script):
            pass

        def find_elements(self, by, selector):
            return []

        @property
        def page_source(self):
            if self.call_idx < len(self.pages):
                return self.pages[self.call_idx]
            return ""

    page1 = (
        '<div class="discover-item">'
        '<a href="https://artist1.bandcamp.com/">A1</a>'
        '</div>'
    )
    page2 = (
        '<div class="discover-item">'
        '<a href="https://artist2.bandcamp.com/">A2</a>'
        '</div>'
    )
    pages = [page1, page2]
    driver = FakeDriver(pages)

    original_wait = lm._bandcamp_wait_for_discover_tiles
    lm._bandcamp_wait_for_discover_tiles = lambda driver, selectors, timeout=15: 1

    try:
        results = lm._bandcamp_collect_discover_dom(driver, "https://bandcamp.com/discover/rock+london?s=new", max_pages=2)
        assert len(results) == 2
    finally:
        lm._bandcamp_wait_for_discover_tiles = original_wait


# ---------------------------------------------------------------------------
#  Cursor API pagination regression
# ---------------------------------------------------------------------------

def test_discover_api_first_batch_returns_cursor():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    batch1 = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
            {"item_id": 2, "band_url": "https://artist2.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": "cursor_1",
        "batch_result_count": 2,
    }

    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        return FakeResponse(batch1)

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

    try:
        # Monkey-patch parse function to avoid live fetch
        original_parse = lm._bandcamp_parse_discover_filters
        lm._bandcamp_parse_discover_filters = lambda url: {
            "genre": "indie-rock",
            "location": "london uk",
            "location_label": "london uk",
            "sort": "new",
            "raw_slug": "indie-rock+london-uk",
            "geoname_id": 2643743,
            "api_tags": ["indie-rock"],
        }
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/indie-rock+london-uk?s=new", max_candidates=10)
        assert len(results) == 2
        assert calls[0]["cursor"] is None
        assert calls[0]["geoname_id"] == 2643743
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


def test_discover_api_second_request_uses_returned_cursor():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    batch1 = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": "cursor_1",
        "batch_result_count": 1,
    }
    batch2 = {
        "results": [
            {"item_id": 2, "band_url": "https://artist2.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": None,
        "batch_result_count": 1,
    }

    call_idx = [0]
    calls = []
    batches = [batch1, batch2]

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        resp = batches[call_idx[0]]
        call_idx[0] += 1
        return FakeResponse(resp)

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

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
        assert len(calls) == 2
        assert calls[0]["cursor"] is None
        assert calls[1]["cursor"] == "cursor_1"
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


def test_discover_api_duplicate_ids_are_deduped():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    batch1 = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": "cursor_1",
        "batch_result_count": 1,
    }
    batch2 = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": None,
        "batch_result_count": 1,
    }

    call_idx = [0]
    batches = [batch1, batch2]

    def fake_post(url, json, headers, timeout):
        resp = batches[call_idx[0]]
        call_idx[0] += 1
        return FakeResponse(resp)

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

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
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


def test_discover_api_no_cursor_stops_cleanly():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    batch = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": None,
        "batch_result_count": 1,
    }

    def fake_post(url, json, headers, timeout):
        return FakeResponse(batch)

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

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
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


def test_discover_api_zero_new_candidates_stops():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    batch1 = {
        "results": [
            {"item_id": 1, "band_url": "https://artist1.bandcamp.com?from=discover_page", "band_location": "London, UK"},
        ],
        "cursor": "cursor_1",
        "batch_result_count": 1,
    }
    batch2 = {
        "results": [],
        "cursor": "cursor_2",
        "batch_result_count": 0,
    }

    call_idx = [0]
    batches = [batch1, batch2]

    def fake_post(url, json, headers, timeout):
        resp = batches[call_idx[0]]
        call_idx[0] += 1
        return FakeResponse(resp)

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

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
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


def test_discover_api_candidate_cap_prevents_extra_calls():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def make_batch(start_id):
        return {
            "results": [
                {"item_id": start_id + i, "band_url": f"https://artist{start_id + i}.bandcamp.com?from=discover_page", "band_location": "London, UK"}
                for i in range(20)
            ],
            "cursor": f"cursor_{start_id}",
            "batch_result_count": 20,
        }

    call_count = [0]

    def fake_post(url, json, headers, timeout):
        call_count[0] += 1
        return FakeResponse(make_batch(call_count[0] * 100))

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

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
        results = lm._bandcamp_collect_discover_via_api("https://bandcamp.com/discover/rock+london?s=new", max_candidates=25)
        assert len(results) == 25
        assert call_count[0] == 2  # 20 in batch 1, 5 in batch 2, then cap stops
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


def test_discover_api_failure_falls_back_safely():
    lm = _load_legacy_module()

    class FakeResponse:
        status_code = 500

        def raise_for_status(self):
            raise Exception("500 Internal Server Error")

    def fake_post(url, json, headers, timeout):
        return FakeResponse()

    original_session = lm._bandcamp_session

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return fake_post(url, json, headers, timeout)

    lm._bandcamp_session = FakeSession

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
        assert results == []
    finally:
        lm._bandcamp_parse_discover_filters = original_parse
        lm._bandcamp_session = original_session


# ---------------------------------------------------------------------------
#  Discover page URL generation
# ---------------------------------------------------------------------------

def test_discover_page_url_generation_is_deterministic():
    lm = _load_legacy_module()
    base = "https://bandcamp.com/discover/rock+london?s=new"
    urls = lm._bandcamp_build_discover_page_urls(base, 3)
    assert urls == [
        "https://bandcamp.com/discover/rock+london?s=new&p=0",
        "https://bandcamp.com/discover/rock+london?s=new&p=1",
        "https://bandcamp.com/discover/rock+london?s=new&p=2",
    ]
