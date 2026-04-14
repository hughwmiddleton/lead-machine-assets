from __future__ import annotations

from types import SimpleNamespace

import pipeline_runner


def _build_unearthed_listing_page(slugs: list[str]) -> str:
    anchors = "".join(
        f'<a href="/triplejunearthed/artist/{slug}">{slug}</a>'
        for slug in slugs
    )
    return f"<html><body>{anchors}</body></html>"


class _FakeUnearthedLoadMoreButton:
    def __init__(self, driver) -> None:
        self.driver = driver

    def click(self) -> None:
        self.driver.click_load_more()


class _FakeUnearthedDriver:
    def __init__(self, pages: list[str]) -> None:
        self.pages = list(pages)
        self.page_index = 0
        self.page_source = ""
        self.current_url = ""
        self.load_more_requests = 0

    def get(self, url: str) -> None:
        self.current_url = url
        self.page_index = 0
        self.page_source = self.pages[self.page_index]

    def click_load_more(self) -> None:
        if self.page_index + 1 >= len(self.pages):
            raise RuntimeError("no more pages")
        self.page_index += 1
        self.page_source = self.pages[self.page_index]

    def quit(self) -> None:
        return None


class _FakeUnearthedWait:
    def __init__(self, driver, _timeout) -> None:
        self.driver = driver

    def until(self, condition):
        if condition[0] == "presence":
            return True
        if condition[0] == "clickable":
            self.driver.load_more_requests += 1
            if self.driver.page_index + 1 >= len(self.driver.pages):
                raise RuntimeError("no more pages")
            return _FakeUnearthedLoadMoreButton(self.driver)
        raise AssertionError(f"unexpected condition: {condition!r}")


def _run_fake_unearthed_scrape(
    monkeypatch,
    tmp_path,
    pages: list[list[str]],
    *,
    max_artists: int,
    target_profile_url: str | None = None,
):
    module = pipeline_runner._load_legacy_module()
    driver = _FakeUnearthedDriver([
        _build_unearthed_listing_page(page_slugs)
        for page_slugs in pages
    ])
    visited_profile_urls: list[str] = []

    def _fake_scrape_artist_profile(_driver, profile_url: str, fb_driver=None):
        visited_profile_urls.append(profile_url)
        artist_slug = profile_url.rstrip("/").rsplit("/", 1)[-1]
        return ("", "", "", "", artist_slug, "", "", "", "")

    monkeypatch.setattr(module, "setup_driver", lambda: driver)
    monkeypatch.setattr(module, "WebDriverWait", _FakeUnearthedWait)
    monkeypatch.setattr(
        module,
        "EC",
        SimpleNamespace(
            presence_of_element_located=lambda locator: ("presence", locator),
            element_to_be_clickable=lambda locator: ("clickable", locator),
        ),
    )
    monkeypatch.setattr(module, "scrape_artist_profile", _fake_scrape_artist_profile)
    monkeypatch.setattr(module, "get_drum_status_from_source", lambda _html: "")
    monkeypatch.setattr(module, "save_to_csv", lambda _data, _filename: None)
    monkeypatch.setattr(module, "SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1", False)
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.random, "uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(module, "_load_unearthed_persistent_cursor", lambda: target_profile_url)

    job_config = {"unearthed_resume_mode": "cursor"} if target_profile_url else {}
    module.scrape_website(
        "https://www.abc.net.au/triplejunearthed",
        existing_csv=str(tmp_path / "unearthed.csv"),
        max_artists=max_artists,
        job_config=job_config,
    )
    return visited_profile_urls, driver.load_more_requests


def test_unearthed_profile_batches_chain_forward_in_discovery_order() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls: list[str] = []
    seen_profile_urls: set[str] = set()
    discovered_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]

    for profile_url in discovered_urls:
        module._append_unearthed_profile_url(ordered_profile_urls, seen_profile_urls, profile_url)

    module._append_unearthed_profile_url(
        ordered_profile_urls,
        seen_profile_urls,
        discovered_urls[2],
    )

    first_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, None, 2)
    second_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, first_batch[-1], 2)
    third_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, second_batch[-1], 2)

    assert ordered_profile_urls == discovered_urls
    assert first_batch == discovered_urls[0:2]
    assert second_batch == discovered_urls[2:4]
    assert third_batch == discovered_urls[4:6]


def test_unearthed_remaining_count_uses_latest_cursor_position() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
    ]

    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, None) == 5
    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, ordered_profile_urls[1]) == 3
    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, ordered_profile_urls[3]) == 1
    assert module._count_unearthed_remaining_profile_urls(
        ordered_profile_urls,
        "https://www.abc.net.au/triplejunearthed/artist/missing",
    ) == 0


def test_unearthed_resume_resolution_prefers_last_logical_occurrence() -> None:
    module = pipeline_runner._load_legacy_module()

    target_profile_url = "https://www.abc.net.au/triplejunearthed/artist/artist-2"
    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        target_profile_url,
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        f"{target_profile_url}/",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        target_profile_url,
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
    ]

    assert module._resolve_unearthed_resume_index(ordered_profile_urls, target_profile_url) == 6
    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, target_profile_url) == 1
    assert module._slice_unearthed_profile_urls(
        ordered_profile_urls,
        f"{target_profile_url}/",
        2,
    ) == ordered_profile_urls[6:7]


def test_unearthed_resume_matching_ignores_trailing_slash_differences() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]

    assert module._count_unearthed_remaining_profile_urls(
        ordered_profile_urls,
        "https://www.abc.net.au/triplejunearthed/artist/artist-2/",
    ) == 2
    assert module._count_unearthed_remaining_profile_urls(
        [f"{url}/" for url in ordered_profile_urls],
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
    ) == 2

    assert module._slice_unearthed_profile_urls(
        ordered_profile_urls,
        "https://www.abc.net.au/triplejunearthed/artist/artist-2/",
        2,
    ) == ordered_profile_urls[2:4]
    ordered_profile_urls_with_slashes = [f"{url}/" for url in ordered_profile_urls]
    assert module._slice_unearthed_profile_urls(
        ordered_profile_urls_with_slashes,
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        2,
    ) == ordered_profile_urls_with_slashes[2:4]


def test_unearthed_profile_batches_chain_forward_across_trailing_slash_cursor_mismatch() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]

    first_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, None, 2)
    second_batch = module._slice_unearthed_profile_urls(
        ordered_profile_urls,
        f"{first_batch[-1]}/",
        2,
    )
    third_batch = module._slice_unearthed_profile_urls(
        ordered_profile_urls,
        second_batch[-1],
        2,
    )

    assert first_batch == ordered_profile_urls[0:2]
    assert second_batch == ordered_profile_urls[2:4]
    assert third_batch == ordered_profile_urls[4:6]


def test_unearthed_slice_still_falls_back_to_fresh_start_for_missing_cursor() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
    ]

    assert module._slice_unearthed_profile_urls(
        ordered_profile_urls,
        "https://www.abc.net.au/triplejunearthed/artist/missing",
        2,
    ) == ordered_profile_urls[0:2]


def test_scrape_website_waits_until_cursor_is_rediscovered_before_slicing(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            ["artist-1", "artist-2", "artist-3"],
            ["artist-1", "artist-2", "artist-3", "artist-4", "artist-5", "artist-6"],
        ],
        max_artists=2,
        target_profile_url="https://www.abc.net.au/triplejunearthed/artist/artist-4",
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]
    assert load_more_requests == 1


def test_scrape_website_waits_for_full_post_cursor_window_before_slicing(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            ["artist-1", "artist-2", "artist-3", "artist-4"],
            ["artist-1", "artist-2", "artist-3", "artist-4", "artist-5"],
            ["artist-1", "artist-2", "artist-3", "artist-4", "artist-5", "artist-6"],
        ],
        max_artists=2,
        target_profile_url="https://www.abc.net.au/triplejunearthed/artist/artist-4",
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]
    assert load_more_requests == 2


def test_scrape_website_resumes_from_terminal_logical_cursor_occurrence(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            ["artist-1", "artist-2/", "artist-3"],
            ["artist-1", "artist-2/", "artist-3", "artist-4", "artist-2", "artist-5", "artist-6"],
        ],
        max_artists=2,
        target_profile_url="https://www.abc.net.au/triplejunearthed/artist/artist-2",
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]
    assert load_more_requests == 1


def test_scrape_website_preserves_fresh_start_fallback_when_cursor_is_never_found(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [["artist-1", "artist-2", "artist-3"]],
        max_artists=2,
        target_profile_url="https://www.abc.net.au/triplejunearthed/artist/missing",
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
    ]
    assert load_more_requests == 1


def test_scrape_website_fresh_run_behavior_is_unchanged(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [["artist-1", "artist-2", "artist-3"]],
        max_artists=2,
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
    ]
    assert load_more_requests == 1
