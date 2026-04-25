from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    resume_mode: str | None = None,
    selected_cursor: str | None = None,
    persistent_cursor: str | None = None,
    night_mode_state: dict | None = None,
    cursor_loader=None,
    cursor_writer=None,
    extra_job_config: dict | None = None,
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
    if cursor_loader is None:
        cursor_loader = lambda: persistent_cursor if persistent_cursor is not None else target_profile_url
    if cursor_writer is None:
        cursor_writer = lambda _profile_url: None
    monkeypatch.setattr(module, "_load_unearthed_persistent_cursor", cursor_loader)
    monkeypatch.setattr(module, "_write_unearthed_persistent_cursor", cursor_writer)

    job_config = {"unearthed_url_index_path": str(tmp_path / "unearthed_artist_url_index.csv")}
    effective_resume_mode = resume_mode if resume_mode is not None else ("cursor" if target_profile_url else None)
    if effective_resume_mode:
        job_config["unearthed_resume_mode"] = effective_resume_mode
    if selected_cursor is not None:
        job_config["unearthed_selected_cursor"] = selected_cursor
    if night_mode_state is not None:
        job_config["_night_mode_state"] = night_mode_state
    if extra_job_config:
        job_config.update(extra_job_config)
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


def test_unearthed_slice_raises_for_missing_cursor() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
    ]

    with pytest.raises(module.UnearthedResumeCursorError):
        module._slice_unearthed_profile_urls(
            ordered_profile_urls,
            "https://www.abc.net.au/triplejunearthed/artist/missing",
            2,
        )


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


def test_scrape_website_stops_at_first_resolved_cursor_during_discovery(monkeypatch, tmp_path) -> None:
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
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]
    assert load_more_requests == 1


def test_scrape_website_auto_resume_uses_fresh_persistent_cursor_over_runtime_state(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [["artist-1", "artist-2", "artist-3", "artist-4", "artist-5"]],
        max_artists=2,
        resume_mode="auto",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-2",
        night_mode_state={
            "unearthed_last_profile_url": "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        },
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]
    assert load_more_requests == 0


def test_scrape_website_cursor_jobs_write_terminal_cursor_for_next_job_without_runtime_state(monkeypatch, tmp_path) -> None:
    cursor_state = {
        "value": "https://www.abc.net.au/triplejunearthed/artist/artist-2",
    }
    cursor_reads: list[str] = []
    cursor_writes: list[str] = []

    def _load_cursor() -> str:
        cursor_reads.append(cursor_state["value"])
        return cursor_state["value"]

    def _write_cursor(profile_url: str) -> None:
        cursor_writes.append(profile_url)
        cursor_state["value"] = profile_url

    pages = [["artist-1", "artist-2", "artist-3", "artist-4", "artist-5", "artist-6"]]

    first_job_urls, first_load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        pages,
        max_artists=2,
        resume_mode="cursor",
        cursor_loader=_load_cursor,
        cursor_writer=_write_cursor,
    )
    second_job_urls, second_load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        pages,
        max_artists=2,
        resume_mode="cursor",
        cursor_loader=_load_cursor,
        cursor_writer=_write_cursor,
    )

    assert first_job_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]
    assert second_job_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]
    assert first_load_more_requests == 0
    assert second_load_more_requests == 0
    assert cursor_reads == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]
    assert cursor_writes == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]
    assert cursor_state["value"] == "https://www.abc.net.au/triplejunearthed/artist/artist-6"


def test_scrape_website_emits_resume_debug_summary_without_changing_slice_flow(monkeypatch, tmp_path, capsys) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [["artist-1", "artist-2", "artist-3", "artist-4"]],
        max_artists=2,
        target_profile_url="https://www.abc.net.au/triplejunearthed/artist/artist-2",
    )

    captured = capsys.readouterr()

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]
    assert load_more_requests == 0
    assert "[UE Resume Debug] resolver" in captured.out
    assert "target_profile_url='https://www.abc.net.au/triplejunearthed/artist/artist-2'" in captured.out
    assert "normalized_target_profile_url='https://www.abc.net.au/triplejunearthed/artist/artist-2'" in captured.out
    assert "target_slug='artist-2'" in captured.out
    assert "exact_match_indices=[1]" in captured.out
    assert "normalized_match_indices=[1]" in captured.out
    assert "slug_match_indices=[1]" in captured.out
    assert "matched_index=1" in captured.out
    assert "resolved_resume_index=2" in captured.out
    assert "[UE Resume Debug] discovery_sample" in captured.out
    assert "[UE Resume Debug] cursor_resolved" in captured.out
    assert "[UE Resume Debug] slice_decision" in captured.out
    assert "remaining_after_resume_index=2" in captured.out
    assert "slice_ready=True" in captured.out
    assert "fallback_to_zero=False" in captured.out


def test_scrape_website_cursor_mode_raises_when_cursor_is_never_found(monkeypatch, tmp_path) -> None:
    module = pipeline_runner._load_legacy_module()

    with pytest.raises(module.UnearthedResumeCursorError):
        _run_fake_unearthed_scrape(
            monkeypatch,
            tmp_path,
            [["artist-1", "artist-2", "artist-3"]],
            max_artists=2,
            resume_mode="cursor",
            persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/missing",
        )


def test_scrape_website_cursor_discovery_scans_beyond_shallow_pages_before_resolving(monkeypatch, tmp_path) -> None:
    pages = [
        [f"artist-{index}" for index in range(1, 51)],
        [f"artist-{index}" for index in range(1, 151)],
        [f"artist-{index}" for index in range(1, 201)],
    ]

    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        pages,
        max_artists=2,
        resume_mode="cursor",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-120",
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-121",
        "https://www.abc.net.au/triplejunearthed/artist/artist-122",
    ]
    assert load_more_requests == 1


def test_scrape_website_cursor_discovery_stops_at_search_limit(monkeypatch, tmp_path, capsys) -> None:
    module = pipeline_runner._load_legacy_module()

    with pytest.raises(module.UnearthedResumeCursorError):
        _run_fake_unearthed_scrape(
            monkeypatch,
            tmp_path,
            [
                [f"artist-{index}" for index in range(1, 51)],
                [f"artist-{index}" for index in range(1, 151)],
                [f"artist-{index}" for index in range(1, 251)],
            ],
            max_artists=2,
            resume_mode="cursor",
            persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-120",
            extra_job_config={"unearthed_cursor_search_limit": 100},
        )

    captured = capsys.readouterr()

    assert 'target_slug="artist-120"' in captured.out
    assert "discovered_count=100" in captured.out
    assert "search_exhausted=True" in captured.out


def test_scrape_website_cursor_discovery_logs_progress_and_early_resolution(monkeypatch, tmp_path, capsys) -> None:
    _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            [f"artist-{index}" for index in range(1, 51)],
            [f"artist-{index}" for index in range(1, 151)],
            [f"artist-{index}" for index in range(1, 201)],
        ],
        max_artists=2,
        resume_mode="cursor",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-120",
    )

    captured = capsys.readouterr()

    assert '[UE Resume Debug] cursor_search_progress target_slug="artist-120" discovered_count=50' in captured.out
    assert '[UE Resume Debug] cursor_search_progress target_slug="artist-120" discovered_count=100' in captured.out
    assert '[UE Resume Debug] cursor_resolved target_slug="artist-120" matched_index=119 resolved_resume_index=120 discovered_count=120' in captured.out


def test_scrape_website_stops_cursor_search_after_resolution(monkeypatch, tmp_path, capsys) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            [f"artist-{index}" for index in range(1, 51)],
            [f"artist-{index}" for index in range(1, 151)],
            [f"artist-{index}" for index in range(1, 2001)],
        ],
        max_artists=2,
        resume_mode="cursor",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-120",
        extra_job_config={"unearthed_cursor_search_limit": 2000},
    )

    captured = capsys.readouterr()

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-121",
        "https://www.abc.net.au/triplejunearthed/artist/artist-122",
    ]
    assert load_more_requests == 1
    assert "Found 122 artist profile URLs so far..." in captured.out
    assert "Found 150 artist profile URLs so far..." not in captured.out
    assert "Found 2000 artist profile URLs so far..." not in captured.out


def test_scrape_website_fills_only_required_post_cursor_slice(monkeypatch, tmp_path, capsys) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            [f"artist-{index}" for index in range(1, 121)],
            [f"artist-{index}" for index in range(1, 181)],
        ],
        max_artists=5,
        resume_mode="cursor",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-120",
        extra_job_config={"unearthed_cursor_search_limit": 2000},
    )

    captured = capsys.readouterr()

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-121",
        "https://www.abc.net.au/triplejunearthed/artist/artist-122",
        "https://www.abc.net.au/triplejunearthed/artist/artist-123",
        "https://www.abc.net.au/triplejunearthed/artist/artist-124",
        "https://www.abc.net.au/triplejunearthed/artist/artist-125",
    ]
    assert load_more_requests == 1
    assert "Found 125 artist profile URLs so far..." in captured.out
    assert "Found 180 artist profile URLs so far..." not in captured.out


def test_scrape_website_cursor_resume_debug_logs_once_after_resolution(monkeypatch, tmp_path, capsys) -> None:
    _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [
            [f"artist-{index}" for index in range(1, 121)],
            [f"artist-{index}" for index in range(1, 181)],
        ],
        max_artists=5,
        resume_mode="cursor",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-120",
    )

    captured = capsys.readouterr()

    assert captured.out.count("[UE Resume Debug] resolver ") == 1
    assert captured.out.count("[UE Resume Debug] cursor_resolved ") == 1
    assert captured.out.count("[UE Resume Debug] slice_decision ") == 1


def test_unearthed_full_pipeline_wrapper_propagates_resume_boundary_errors(tmp_path) -> None:
    class UnearthedResumeCursorError(RuntimeError):
        pass

    def _raise_resume_error(**_kwargs):
        raise UnearthedResumeCursorError("missing cursor")

    def _unexpected_fallback(*_args, **_kwargs):
        raise AssertionError("listing-only fallback should not run after a resume boundary error")

    module = SimpleNamespace(
        UNEARTHED_DEFAULT_URL="https://www.abc.net.au/triplejunearthed",
        run_unearthed_pipeline=_raise_resume_error,
        scrape_website=_unexpected_fallback,
    )

    with pytest.raises(UnearthedResumeCursorError):
        pipeline_runner._run_unearthed_full_pipeline(
            {"directory": "unearthed", "target_valid_leads": 2},
            str(tmp_path / "raw.csv"),
            module,
            logger=None,
        )


def test_scrape_website_selected_cursor_uses_explicit_checkpoint_instead_of_persistent_cursor(monkeypatch, tmp_path) -> None:
    visited_profile_urls, load_more_requests = _run_fake_unearthed_scrape(
        monkeypatch,
        tmp_path,
        [["artist-1", "artist-2", "artist-3", "artist-4", "artist-5"]],
        max_artists=2,
        resume_mode="selected",
        selected_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-2",
        persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/artist-4",
    )

    assert visited_profile_urls == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
    ]
    assert load_more_requests == 0


def test_scrape_website_selected_cursor_raises_when_checkpoint_is_missing(monkeypatch, tmp_path) -> None:
    module = pipeline_runner._load_legacy_module()

    with pytest.raises(module.UnearthedSelectedCursorError):
        _run_fake_unearthed_scrape(
            monkeypatch,
            tmp_path,
            [["artist-1", "artist-2", "artist-3"]],
            max_artists=2,
            resume_mode="selected",
            selected_cursor="https://www.abc.net.au/triplejunearthed/artist/missing",
        )


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
