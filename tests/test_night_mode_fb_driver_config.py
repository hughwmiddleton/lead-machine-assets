import night_mode_fb as nmfb
import pytest


class _DummyDriver:
    def __init__(self) -> None:
        self.cdp_calls = []

    def execute_cdp_cmd(self, command, payload):  # noqa: ANN001
        self.cdp_calls.append((command, payload))
        return {}


def test_create_fb_driver_night_mode_forces_images_on(monkeypatch):
    captured = {}
    sentinel_driver = _DummyDriver()

    monkeypatch.setattr(nmfb, "_resolve_night_fb_profile_dir", lambda logger=None: "/tmp/night_fb_profile")

    def fake_start(chrome_options, logger=None, profile_dir=None, enable_temp_profile_fallback=False):
        captured["options"] = chrome_options
        captured["profile_dir"] = profile_dir
        captured["temp_fallback"] = enable_temp_profile_fallback
        return sentinel_driver

    monkeypatch.setattr(nmfb, "_start_chromedriver_with_retry", fake_start)

    driver = nmfb._create_fb_driver_night_mode(headless=False, logger=None)

    assert driver is sentinel_driver
    prefs = captured["options"]._experimental_options["prefs"]
    assert prefs["profile.managed_default_content_settings.images"] == 1
    assert captured["profile_dir"] == "/tmp/night_fb_profile"
    assert captured["temp_fallback"] is False
    assert nmfb._chrome_images_policy_label(captured["options"]) == "managed_allow"
    assert sentinel_driver.cdp_calls
    assert sentinel_driver.cdp_calls[0][0] == "Page.addScriptToEvaluateOnNewDocument"
    assert "navigator" in sentinel_driver.cdp_calls[0][1]["source"]


def test_create_fb_driver_public_keeps_images_blocked(monkeypatch):
    captured = {}
    sentinel_driver = object()

    def fake_start(chrome_options, logger=None, profile_dir=None, enable_temp_profile_fallback=False):
        captured["options"] = chrome_options
        captured["profile_dir"] = profile_dir
        captured["temp_fallback"] = enable_temp_profile_fallback
        return sentinel_driver

    monkeypatch.setattr(nmfb, "_start_chromedriver_with_retry", fake_start)

    driver = nmfb._create_fb_driver_public(headless=True)

    assert driver is sentinel_driver
    prefs = captured["options"]._experimental_options["prefs"]
    assert prefs["profile.managed_default_content_settings.images"] == 2
    assert captured["profile_dir"] is None
    assert captured["temp_fallback"] is False
    assert nmfb._chrome_images_policy_label(captured["options"]) == "managed_block"


class _LoggedOutDriver:
    def __init__(self) -> None:
        self.current_url = "about:blank"
        self.loaded_urls = []

    def get(self, url):  # noqa: ANN001
        self.loaded_urls.append(url)
        self.current_url = url

    def get_cookie(self, name):  # noqa: ANN001
        return None


def test_persistent_session_warns_when_manual_login_required(monkeypatch):
    logs = []
    driver = _LoggedOutDriver()
    session = nmfb.NightPersistentFacebookSession(
        driver_factory=lambda: driver,
        headless=False,
        logger=lambda msg: logs.append(msg),
        wait_seconds=1.0,
    )

    waited = []
    monkeypatch.setattr(session, "_wait_for_manual_login", lambda drv: waited.append(drv))

    returned = session.ensure_logged_in()

    assert returned is driver
    assert waited == [driver]
    assert driver.loaded_urls == ["https://www.facebook.com/"]
    assert any("manual login is required" in msg.lower() for msg in logs)
    assert any("image loading is enabled" in msg.lower() for msg in logs)


class _HeadlessLoggedOutDriver:
    def __init__(self) -> None:
        self.current_url = "https://www.facebook.com/login/"
        self.loaded_urls = []
        self.page_source = "<html><body>Log in to Facebook</body></html>"

    def get(self, url):  # noqa: ANN001
        self.loaded_urls.append(url)

    def get_cookie(self, name):  # noqa: ANN001
        return None


def test_persistent_session_headless_fails_fast_when_auth_missing():
    logs = []
    driver = _HeadlessLoggedOutDriver()
    session = nmfb.NightPersistentFacebookSession(
        driver_factory=lambda: driver,
        headless=True,
        logger=lambda msg: logs.append(msg),
        wait_seconds=1.0,
    )

    with pytest.raises(nmfb.FacebookDriverError, match="auth is missing \\(reason=redirect_login"):
        session.ensure_logged_in()

    assert session.last_health_ok is False
    assert session.last_health_reason == "redirect_login"
    assert any("[Night FB][auth_probe]" in msg and "reason=redirect_login" in msg for msg in logs)


def test_start_chromedriver_blocks_temp_profile_fallback_for_persistent_profile(monkeypatch):
    logs = []
    options = nmfb.ChromeOptions()
    options.add_argument("--user-data-dir=/tmp/night_fb_profile")
    monkeypatch.setattr(nmfb, "ChromeService", lambda *args, **kwargs: object())

    monkeypatch.setattr(
        nmfb.webdriver,
        "Chrome",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("user data directory is already in use")),
    )

    with pytest.raises(nmfb.FacebookDriverError, match="temp-profile fallback blocked for authenticated Night FB"):
        nmfb._start_chromedriver_with_retry(
            options,
            logger=lambda msg: logs.append(msg),
            profile_dir="/tmp/night_fb_profile",
            enable_temp_profile_fallback=False,
        )

    assert any("[Night FB][profile]" in msg and "temp_fallback=blocked" in msg for msg in logs)
