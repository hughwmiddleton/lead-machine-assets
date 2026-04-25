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
            if self.driver.page_index + 1 >= len(self.driver.pages):
                raise RuntimeError("no more pages")
            return _FakeUnearthedLoadMoreButton(self.driver)
        raise AssertionError(f"unexpected condition: {condition!r}")


def _run_fake_resume_scrape(
    monkeypatch,
    tmp_path,
    *,
    pages: list[list[str]],
    max_artists: int,
    resume_mode: str | None = None,
    persistent_cursor: str | None = None,
    selected_cursor: str | None = None,
    include_state: bool = False,
    side_effects=None,
    extra_job_config: dict | None = None,
):
    module = pipeline_runner._load_legacy_module()
    driver = _FakeUnearthedDriver([
        _build_unearthed_listing_page(page_slugs)
        for page_slugs in pages
    ])
    side_effects = side_effects or {
        "scrape_calls": [],
        "save_calls": [],
        "cursor_writes": [],
        "persist_calls": [],
    }

    def _fake_scrape_artist_profile(_driver, profile_url: str, fb_driver=None):
        side_effects["scrape_calls"].append(profile_url)
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
    monkeypatch.setattr(
        module,
        "save_to_csv",
        lambda data, filename: side_effects["save_calls"].append((list(data), filename)),
    )
    monkeypatch.setattr(
        module,
        "_write_unearthed_persistent_cursor",
        lambda profile_url: side_effects["cursor_writes"].append(profile_url),
    )
    monkeypatch.setattr(
        module,
        "_load_unearthed_persistent_cursor",
        lambda: persistent_cursor,
    )
    monkeypatch.setattr(module, "SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1", False)
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.random, "uniform", lambda *_args, **_kwargs: 0.0)

    state = {} if include_state else None

    def _persist_state() -> None:
        side_effects["persist_calls"].append("persisted")

    job_config = {}
    if resume_mode is not None:
        job_config["unearthed_resume_mode"] = resume_mode
    if selected_cursor is not None:
        job_config["unearthed_selected_cursor"] = selected_cursor
    if include_state:
        job_config["_night_mode_state"] = state
        job_config["_night_mode_state_persist"] = _persist_state
    if extra_job_config:
        job_config.update(extra_job_config)

    module.scrape_website(
        "https://www.abc.net.au/triplejunearthed",
        existing_csv=str(tmp_path / "unearthed.csv"),
        max_artists=max_artists,
        job_config=job_config,
    )
    return side_effects


def test_normalize_unearthed_cursor_accepts_url_forms_and_bare_slug() -> None:
    module = pipeline_runner._load_legacy_module()

    expected = "tara-formosa"
    assert module.normalize_unearthed_cursor(
        "https://www.abc.net.au/triplejunearthed/artist/tara-formosa"
    ) == expected
    assert module.normalize_unearthed_cursor(
        "https://www.abc.net.au/triplejunearthed/artist/tara-formosa/"
    ) == expected
    assert module.normalize_unearthed_cursor(
        "https://www.abc.net.au/triplejunearthed/artist/tara-formosa?x=1#bio"
    ) == expected
    assert module.normalize_unearthed_cursor("tara-formosa") == expected


def test_resume_index_resolves_by_slug_across_url_formats() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/tara-formosa/",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
    ]

    assert module._resolve_unearthed_resume_index(
        ordered_profile_urls,
        "https://www.abc.net.au/triplejunearthed/artist/tara-formosa?x=1",
    ) == 1


def test_unearthed_cursor_search_limit_default_and_clamps() -> None:
    module = pipeline_runner._load_legacy_module()

    assert module._default_unearthed_cursor_search_limit(10) == 2000
    assert module._default_unearthed_cursor_search_limit(500) == 2500
    assert module._default_unearthed_cursor_search_limit(5000) == 10000
    assert module._resolve_unearthed_cursor_search_limit({}, 10) == 2000
    assert module._resolve_unearthed_cursor_search_limit(
        {"unearthed_cursor_search_limit": -1},
        10,
    ) == 2000
    assert module._resolve_unearthed_cursor_search_limit(
        {"unearthed_cursor_search_limit": 25000},
        10,
    ) == 10000


def test_unresolved_cursor_fails_safely_without_side_effects(monkeypatch, tmp_path, capsys) -> None:
    module = pipeline_runner._load_legacy_module()

    with pytest.raises(module.UnearthedResumeCursorError):
        _run_fake_resume_scrape(
            monkeypatch,
            tmp_path,
            pages=[["artist-1", "artist-2", "artist-3"]],
            max_artists=2,
            resume_mode="cursor",
            persistent_cursor="https://www.abc.net.au/triplejunearthed/artist/missing-artist",
            include_state=True,
            extra_job_config={"unearthed_cursor_search_limit": 100},
        )

    captured = capsys.readouterr()

    assert '[UE Resume Error] cursor_unresolved target_profile_url="https://www.abc.net.au/triplejunearthed/artist/missing-artist"' in captured.out
    assert 'target_slug="missing-artist"' in captured.out
    assert "discovered_count=3" in captured.out
    assert 'first_url="https://www.abc.net.au/triplejunearthed/artist/artist-1"' in captured.out
    assert 'last_url="https://www.abc.net.au/triplejunearthed/artist/artist-3"' in captured.out
    assert "search_exhausted=True" in captured.out


def test_unresolved_cursor_has_zero_scrape_write_or_persist_side_effects(monkeypatch, tmp_path) -> None:
    module = pipeline_runner._load_legacy_module()
    side_effects = {
        "scrape_calls": [],
        "save_calls": [],
        "cursor_writes": [],
        "persist_calls": [],
    }

    with pytest.raises(module.UnearthedResumeCursorError):
        _run_fake_resume_scrape(
            monkeypatch,
            tmp_path,
            pages=[["artist-1", "artist-2"]],
            max_artists=1,
            resume_mode="cursor",
            persistent_cursor="missing-artist",
            include_state=True,
            side_effects=side_effects,
        )

    assert side_effects["scrape_calls"] == []
    assert side_effects["save_calls"] == []
    assert side_effects["cursor_writes"] == []
    assert side_effects["persist_calls"] == []
    assert not (tmp_path / "unearthed.csv").exists()


def test_bare_slug_cursor_resumes_from_next_artist(monkeypatch, tmp_path) -> None:
    side_effects = _run_fake_resume_scrape(
        monkeypatch,
        tmp_path,
        pages=[["artist-1", "tara-formosa", "artist-3"]],
        max_artists=1,
        resume_mode="cursor",
        persistent_cursor="tara-formosa",
    )

    assert side_effects["scrape_calls"] == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
    ]
    assert len(side_effects["save_calls"]) == 1


def test_no_cursor_preserves_fresh_run_behavior(monkeypatch, tmp_path) -> None:
    side_effects = _run_fake_resume_scrape(
        monkeypatch,
        tmp_path,
        pages=[["artist-1", "artist-2", "artist-3"]],
        max_artists=2,
    )

    assert side_effects["scrape_calls"] == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
    ]
    assert len(side_effects["save_calls"]) == 1


def test_run_directory_job_propagates_resume_errors_without_fallback_or_outputs(monkeypatch, tmp_path) -> None:
    class UnearthedResumeCursorError(RuntimeError):
        pass

    fallback_calls: list[str] = []
    dedupe_calls: list[str] = []

    def _raise_resume_error(**_kwargs):
        raise UnearthedResumeCursorError("missing cursor")

    def _unexpected_fallback(*_args, **_kwargs):
        fallback_calls.append("scrape_website")

    fake_module = SimpleNamespace(
        UNEARTHED_DEFAULT_URL="https://www.abc.net.au/triplejunearthed",
        run_unearthed_pipeline=_raise_resume_error,
        scrape_website=_unexpected_fallback,
    )

    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: fake_module)
    monkeypatch.setattr(
        pipeline_runner,
        "_dedupe_unearthed_csv",
        lambda path, logger=None: dedupe_calls.append(path),
    )

    raw_output_path = tmp_path / "job_unearthed_1" / "raw.csv"

    with pytest.raises(UnearthedResumeCursorError):
        pipeline_runner.run_directory_job(
            {"directory": "unearthed", "target_valid_leads": 2},
            raw_output_path.as_posix(),
        )

    assert fallback_calls == []
    assert dedupe_calls == []
    assert not raw_output_path.exists()
    assert not raw_output_path.with_name("raw.tmp.csv").exists()
