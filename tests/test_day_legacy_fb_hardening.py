import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import final_checker


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy_day_fb", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeWait:
    def __init__(self, _driver, _timeout):
        pass

    def until(self, _condition):
        return True


class _FakeBody:
    def __init__(self, text: str):
        self.text = text


class _FakeDriver:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.current_window_handle = "main"
        self.current_url = ""
        self.page_source = ""
        self.body_text = ""

    def load(self, url: str):
        page = self.pages[url]
        self.current_url = page.get("current_url", url)
        self.page_source = page.get("page_source", "")
        self.body_text = page.get("body_text", "")

    def get(self, url: str):
        self.load(url)

    def find_element(self, _by, value):
        if value == "body":
            return _FakeBody(self.body_text)
        raise LookupError(value)

    def execute_script(self, script, *_args):
        if "document.body" in str(script or ""):
            return self.body_text
        return None

    def quit(self):
        return None


class _FakeSessionManager:
    pages = {}

    def __init__(self, _username, _password, _driver_factory, logger=None):
        self.logger = logger
        self.driver = _FakeDriver(self.pages)

    def ensure_logged_in(self):
        return self.driver

    def navigate(self, url: str):
        self.driver.load(url)
        return self.driver

    def refresh_session(self):
        return None

    def close_extra_windows(self):
        return None

    def close(self):
        return None


def test_resolve_legacy_target_prefers_canonical_field_and_rejects_wrappers():
    lm = _load_legacy_module()

    raw, canonical, status = lm._resolve_legacy_facebook_target(
        {
            "Facebook_URL": "http://m.facebook.com/artist/",
            "Social Link": "https://www.facebook.com/share.php?u=test",
        }
    )

    assert raw == "http://m.facebook.com/artist/"
    assert canonical == "https://www.facebook.com/artist"
    assert status == "resolved"

    share_raw, share_canonical, share_status = lm._resolve_legacy_facebook_target(
        {"Social Link": "https://www.facebook.com/share/r/test"}
    )
    assert share_raw == "https://www.facebook.com/share/r/test"
    assert share_canonical == ""
    assert share_status == "unsafe_or_weak"

    about_raw, about_canonical, about_status = lm._resolve_legacy_facebook_target(
        {"Facebook_URL": "https://www.facebook.com/artist/about"}
    )
    assert about_raw == "https://www.facebook.com/artist/about"
    assert about_canonical == ""
    assert about_status == "unsafe_or_weak"

    profile_raw, profile_canonical, profile_status = lm._resolve_legacy_facebook_target(
        {"Facebook_URL": "https://www.facebook.com/profile.php?id=123&ref=share"}
    )
    assert profile_raw == "https://www.facebook.com/profile.php?id=123&ref=share"
    assert profile_canonical == "https://www.facebook.com/profile.php?id=123"
    assert profile_status == "resolved"


def test_scrape_csv_uses_main_page_email_without_about_visit(monkeypatch, tmp_path, capsys):
    lm = _load_legacy_module()
    input_csv = tmp_path / "input.csv"
    output_csv = tmp_path / "output.csv"
    main_url = "https://www.facebook.com/artist"
    pd.DataFrame([{"Artist Name": "Artist", "Social Link": "http://m.facebook.com/artist", "Email": ""}]).to_csv(input_csv, index=False)

    _FakeSessionManager.pages = {
        main_url: {
            "current_url": main_url,
            "page_source": '<html><body><div>Bookings: bookings@artist.com</div><a href="/artist/about">About</a></body></html>',
            "body_text": "Bookings: bookings@artist.com",
        }
    }
    about_calls = []

    monkeypatch.setattr(lm, "FacebookSessionManager", _FakeSessionManager)
    monkeypatch.setattr(lm, "WebDriverWait", _FakeWait)
    monkeypatch.setattr(lm.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lm.random, "uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(lm, "_prompt_origin_auto_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(final_checker, "run_final_checker", lambda path: str(path))
    monkeypatch.setattr(
        lm,
        "_goto_facebook_about",
        lambda *_args, **_kwargs: about_calls.append("called") or False,
    )

    lm.scrape_csv(str(input_csv), str(output_csv), "user", "pass")

    output_df = pd.read_csv(output_csv)
    assert output_df.at[0, "Email"] == "bookings@artist.com"
    assert about_calls == []
    stdout = capsys.readouterr().out
    assert "raw=http://m.facebook.com/artist" in stdout
    assert "canonical=https://www.facebook.com/artist" in stdout
    assert "main_email=yes" in stdout
    assert "about=no" in stdout
    assert "status=found_email_main" in stdout


def test_scrape_csv_uses_existing_about_visit_when_main_page_has_no_email(monkeypatch, tmp_path, capsys):
    lm = _load_legacy_module()
    input_csv = tmp_path / "input.csv"
    output_csv = tmp_path / "output.csv"
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"
    pd.DataFrame([{"Artist Name": "Artist", "Facebook_URL": main_url, "Email": ""}]).to_csv(input_csv, index=False)

    _FakeSessionManager.pages = {
        main_url: {
            "current_url": main_url,
            "page_source": '<html><body><div>No email on main.</div><a href="/artist/about">About</a></body></html>',
            "body_text": "No email on main.",
        },
        about_url: {
            "current_url": about_url,
            "page_source": "<html><body><div>Bookings: bookings@artist.com</div></body></html>",
            "body_text": "Bookings: bookings@artist.com",
        },
    }
    about_calls = []

    def _goto_about(driver, _url, timeout=5.0):
        about_calls.append(timeout)
        driver.load(about_url)
        return True

    monkeypatch.setattr(lm, "FacebookSessionManager", _FakeSessionManager)
    monkeypatch.setattr(lm, "WebDriverWait", _FakeWait)
    monkeypatch.setattr(lm.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lm.random, "uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(lm, "_prompt_origin_auto_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(final_checker, "run_final_checker", lambda path: str(path))
    monkeypatch.setattr(lm, "_goto_facebook_about", _goto_about)

    lm.scrape_csv(str(input_csv), str(output_csv), "user", "pass")

    output_df = pd.read_csv(output_csv)
    assert output_df.at[0, "Email"] == "bookings@artist.com"
    assert len(about_calls) == 1
    stdout = capsys.readouterr().out
    assert "final=https://www.facebook.com/artist/about" in stdout
    assert "main_email=no" in stdout
    assert "about=yes" in stdout
    assert "status=found_email_about" in stdout


def test_scrape_csv_preserves_no_email_outcome(monkeypatch, tmp_path, capsys):
    lm = _load_legacy_module()
    input_csv = tmp_path / "input.csv"
    output_csv = tmp_path / "output.csv"
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"
    pd.DataFrame([{"Artist Name": "Artist", "Facebook_URL": main_url, "Email": ""}]).to_csv(input_csv, index=False)

    _FakeSessionManager.pages = {
        main_url: {
            "current_url": main_url,
            "page_source": '<html><body><div>No email on main.</div><a href="/artist/about">About</a></body></html>',
            "body_text": "No email on main.",
        },
        about_url: {
            "current_url": about_url,
            "page_source": "<html><body><div>No email on About either.</div></body></html>",
            "body_text": "No email on About either.",
        },
    }

    monkeypatch.setattr(lm, "FacebookSessionManager", _FakeSessionManager)
    monkeypatch.setattr(lm, "WebDriverWait", _FakeWait)
    monkeypatch.setattr(lm.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lm.random, "uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(lm, "_prompt_origin_auto_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(final_checker, "run_final_checker", lambda path: str(path))
    monkeypatch.setattr(lm, "_goto_facebook_about", lambda driver, _url, timeout=5.0: driver.load(about_url) or True)

    lm.scrape_csv(str(input_csv), str(output_csv), "user", "pass")

    output_df = pd.read_csv(output_csv)
    assert output_df.empty
    stdout = capsys.readouterr().out
    assert "main_email=no" in stdout
    assert "about=yes" in stdout
    assert "status=no_email" in stdout


def test_automated_login_disabled_log_is_emitted_once_per_session(monkeypatch):
    lm = _load_legacy_module()
    logs = []
    fake_driver = SimpleNamespace(current_window_handle="main")
    manager = lm.FacebookSessionManager("user", "pass", lambda: fake_driver, logger=logs.append)

    monkeypatch.delenv("FB_ALLOW_AUTOMATED_LOGIN", raising=False)

    assert manager.ensure_logged_in() is fake_driver
    assert manager.ensure_logged_in() is fake_driver
    assert manager.ensure_logged_in() is fake_driver
    assert sum("Automated login disabled" in msg for msg in logs) == 1
