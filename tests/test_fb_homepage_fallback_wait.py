from __future__ import annotations

import night_mode_fb


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _MissingSearchInputDriver:
    def __init__(self) -> None:
        self.calls = []

    def find_elements(self, by, selector):
        self.calls.append((by, selector))
        return []


class _FakeSearchInput:
    def __init__(self, driver) -> None:
        self.driver = driver
        self.actions = []

    def click(self) -> None:
        self.actions.append("click")

    def send_keys(self, *keys) -> None:
        self.actions.append(keys)
        if keys and keys[-1] == night_mode_fb.Keys.ENTER:
            self.driver.current_url = "https://www.facebook.com/search/top/?q=test"
            self.driver.page_source = (
                "<div role='main'><div aria-label='Search results'>"
                "<a href='https://www.facebook.com/testartist'>Test Artist</a>"
                "</div></div>"
            )


class _HomepageDriver:
    def __init__(self) -> None:
        self.current_url = "https://www.facebook.com/"
        self.page_source = "<div>home</div>"
        self.search_input = _FakeSearchInput(self)


class _ImmediateWait:
    def __init__(self, driver, *_args, **_kwargs) -> None:
        self.driver = driver

    def until(self, condition):
        return condition(self.driver)


def test_find_fb_home_search_input_uses_single_total_timeout_budget(monkeypatch) -> None:
    clock = _FakeClock()
    driver = _MissingSearchInputDriver()
    monkeypatch.setattr(night_mode_fb.time, "time", clock.time)
    monkeypatch.setattr(night_mode_fb.time, "sleep", clock.sleep)

    found = night_mode_fb._find_fb_home_search_input(driver, timeout=1.0, poll_seconds=0.25)

    assert found is None
    assert clock.sleeps == [0.25, 0.25, 0.25, 0.25]
    assert len(driver.calls) == len(night_mode_fb._FB_HOME_SEARCH_INPUT_SELECTORS) * 5


def test_run_fb_homepage_search_returns_to_harvest_path_after_fast_ready(monkeypatch) -> None:
    driver = _HomepageDriver()
    monkeypatch.setattr(
        night_mode_fb,
        "_load_fb_page_with_timeout",
        lambda *_args, **_kwargs: ("<div>home</div>", "https://www.facebook.com/", False),
    )
    monkeypatch.setattr(night_mode_fb, "_find_fb_home_search_input", lambda *_args, **_kwargs: driver.search_input)
    monkeypatch.setattr(night_mode_fb, "WebDriverWait", _ImmediateWait)
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda *_args, **_kwargs: None)

    html, current_url, timed_out = night_mode_fb._run_fb_homepage_search(driver, "Test Artist")

    assert timed_out is False
    assert current_url == "https://www.facebook.com/search/top/?q=test"
    assert "https://www.facebook.com/testartist" in html
