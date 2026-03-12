import night_mode_fb as nmfb


def test_create_fb_driver_night_mode_forces_images_on(monkeypatch):
    captured = {}
    sentinel_driver = object()

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
    assert captured["temp_fallback"] is True
    assert nmfb._chrome_images_policy_label(captured["options"]) == "managed_allow"


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
