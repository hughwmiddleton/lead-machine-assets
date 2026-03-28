import pytest
from bs4 import BeautifulSoup

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde
import night_mode_fb as nmfb


def _build_enricher(logs):  # noqa: ANN001
    enricher = nmfb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    enricher._page_budget_remaining = 2
    return enricher


def test_select_fb_contact_surface_detects_about_link() -> None:
    html = '<html><body><a href="/artist?sk=about">About</a></body></html>'

    selected = cde._select_fb_contact_surface_url("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist?sk=about"


def test_select_fb_contact_surface_detects_contact_page() -> None:
    html = '<html><body><a href="/artist/contact_and_basic_info">Contact</a></body></html>'

    selected = cde._select_fb_contact_surface_url("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/contact_and_basic_info"


def test_select_fb_contact_surface_priority_prefers_about_over_info() -> None:
    html = """
    <html><body>
      <a href="/artist/info">Info</a>
      <a href="/artist/about">About</a>
    </body></html>
    """

    selected = cde._select_fb_contact_surface_url("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about"


def test_select_fb_contact_surface_rejects_external_domains() -> None:
    html = '<html><body><a href="https://example.com/contact">Contact</a></body></html>'

    selected = cde._select_fb_contact_surface_url("https://www.facebook.com/artist", html)

    assert selected is None


def test_select_fb_contact_surface_rejects_javascript_links() -> None:
    html = '<html><body><a href="javascript:void(0)">About</a></body></html>'

    selected = cde._select_fb_contact_surface_url("https://www.facebook.com/artist", html)

    assert selected is None


def test_select_fb_contact_surface_strips_fragments() -> None:
    html = '<html><body><a href="/artist/about#section">About</a></body></html>'

    selected = cde._select_fb_contact_surface_url("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about"


def _pick_from_html(base_url: str, html: str):  # noqa: ANN001
    soup = BeautifulSoup(html, "html.parser")
    return nmfb._pick_fb_contact_link(soup, base_url)


def test_night_mode_pick_fb_contact_link_accepts_about_path() -> None:
    html = '<html><body><a href="/artist/about">About</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about"


def test_night_mode_pick_fb_contact_link_accepts_about_contact_and_basic_info() -> None:
    html = '<html><body><a href="/artist/about_contact_and_basic_info">Contact info</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about_contact_and_basic_info"


def test_night_mode_pick_fb_contact_link_accepts_contact_and_basic_info() -> None:
    html = '<html><body><a href="/artist/contact_and_basic_info">Contact info</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/contact_and_basic_info"


def test_night_mode_pick_fb_contact_link_prefers_about_contact_and_basic_info_over_about() -> None:
    html = """
    <html><body>
      <a href="/artist/about">About</a>
      <a href="/artist/about_contact_and_basic_info">Contact info</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about_contact_and_basic_info"


def test_night_mode_pick_fb_contact_link_prefers_contact_and_basic_info_over_about() -> None:
    html = """
    <html><body>
      <a href="/artist/about">About</a>
      <a href="/artist/contact_and_basic_info">Contact info</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/contact_and_basic_info"


def test_night_mode_pick_fb_contact_link_accepts_contact_path() -> None:
    html = '<html><body><a href="/artist/contact">Contact</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/contact"


def test_night_mode_pick_fb_contact_link_prefers_about_details_over_about() -> None:
    html = """
    <html><body>
      <a href="/artist/about">About</a>
      <a href="/artist/about_details">Details</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about_details"


def test_night_mode_pick_fb_contact_link_falls_back_to_about_over_contact() -> None:
    html = """
    <html><body>
      <a href="/artist/contact">Contact</a>
      <a href="/artist/about">About</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about"


def test_night_mode_pick_fb_contact_link_rejects_events_surface() -> None:
    html = '<html><body><a href="/artist/events">Events</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected is None


def test_night_mode_pick_fb_contact_link_rejects_birthdays_surface() -> None:
    html = '<html><body><a href="/events/birthdays/?foo=1">Birthdays</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected is None


def test_night_mode_pick_fb_contact_link_rejects_text_only_about_on_birthdays_href() -> None:
    html = '<html><body><a href="/events/birthdays/?foo=1">About</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected is None


def test_night_mode_pick_fb_contact_link_skips_invalid_surface_and_selects_valid_about() -> None:
    html = """
    <html><body>
      <a href="/events/birthdays/?foo=1">About</a>
      <a href="/artist/about">About</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist/about"


def test_night_mode_pick_fb_contact_link_returns_none_when_no_valid_links_exist() -> None:
    html = "<html><body><div>No useful anchors</div></body></html>"

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected is None


def test_night_mode_pick_fb_contact_link_accepts_valid_sk_about_surface() -> None:
    html = '<html><body><a href="/artist?sk=about">About</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist?sk=about"


def test_night_mode_pick_fb_contact_link_prefers_sk_about_contact_and_basic_info_over_sk_about() -> None:
    html = """
    <html><body>
      <a href="/artist?sk=about">About</a>
      <a href="/artist?sk=about_contact_and_basic_info">Contact info</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/artist", html)

    assert selected == "https://www.facebook.com/artist?sk=about_contact_and_basic_info"


def test_night_mode_pick_fb_contact_link_accepts_profile_sk_about_details_surface() -> None:
    html = '<html><body><a href="/profile.php?id=123&sk=about_details">Details</a></body></html>'

    selected = _pick_from_html("https://www.facebook.com/profile.php?id=123", html)

    assert selected == "https://www.facebook.com/profile.php?id=123&sk=about_details"


def test_night_mode_pick_fb_contact_link_prefers_profile_sk_about_details_over_about() -> None:
    html = """
    <html><body>
      <a href="/profile.php?id=123&sk=about">About</a>
      <a href="/profile.php?id=123&sk=about_details">Details</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/profile.php?id=123", html)

    assert selected == "https://www.facebook.com/profile.php?id=123&sk=about_details"


def test_night_mode_pick_fb_contact_link_prefers_strongest_unique_profile_surface() -> None:
    html = """
    <html><body>
      <a href="https://m.facebook.com/profile.php?id=123&sk=about#bio">About</a>
      <a href="/profile.php?id=123&sk=about">About again</a>
      <a href="https://touch.facebook.com/profile.php?id=123&sk=about_details#contact">Details</a>
    </body></html>
    """

    selected = _pick_from_html("https://www.facebook.com/profile.php?id=123", html)

    assert selected == "https://www.facebook.com/profile.php?id=123&sk=about_details"


def test_fetch_fb_about_variants_profile_includes_about_details_between_contact_and_about() -> None:
    variants = nmfb._fetch_fb_about_variants("https://www.facebook.com/profile.php?id=123")

    assert variants == [
        "https://www.facebook.com/profile.php?id=123&sk=about_contact_and_basic_info",
        "https://www.facebook.com/profile.php?id=123&sk=about_details",
        "https://www.facebook.com/profile.php?id=123&sk=about",
    ]


def test_fetch_fb_about_variants_slug_prefers_directory_contact_info() -> None:
    variants = nmfb._fetch_fb_about_variants("https://www.facebook.com/artist")

    assert variants == [
        "https://www.facebook.com/artist/directory_contact_info",
        "https://www.facebook.com/artist/about_contact_and_basic_info",
        "https://www.facebook.com/artist/about_details",
        "https://www.facebook.com/artist/about",
    ]


def test_fetch_fb_about_variants_slug_keeps_existing_variants_after_directory_contact_info() -> None:
    variants = nmfb._fetch_fb_about_variants("https://www.facebook.com/artist")

    assert variants[1:] == [
        "https://www.facebook.com/artist/about_contact_and_basic_info",
        "https://www.facebook.com/artist/about_details",
        "https://www.facebook.com/artist/about",
    ]


class _FakeFacebookDriver:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.current_url = ""
        self.page_source = ""

    def get(self, url):  # noqa: ANN001
        self.calls.append(url)
        self.current_url = url
        self.page_source = self.pages.get(url, "")


class _NoRawGetFacebookDriver:
    def __init__(self):
        self.current_url = ""
        self.page_source = ""

    def get(self, url):  # noqa: ANN001
        raise AssertionError(f"raw driver.get should not be used for bounded Night navigation: {url}")


class _FakeBoundedFacebookSession:
    def __init__(self, driver, pages):
        self.driver = driver
        self.pages = pages
        self.calls = []
        self.last_nav_current_url = ""
        self.last_nav_page_source = ""

    def navigate(self, url, logger=None):  # noqa: ANN001
        self.calls.append(url)
        page = self.pages.get(url, {})
        resolved_url = page.get("resolved_url", url)
        html = page.get("html", "")
        self.driver.current_url = resolved_url
        self.driver.page_source = html
        self.last_nav_current_url = resolved_url
        self.last_nav_page_source = html
        return self.driver


def test_extract_fb_emails_bounded_caps_fetches_at_two_with_detected_surface() -> None:
    driver = _FakeFacebookDriver(
        {
            "https://www.facebook.com/artist": '<html><body><a href="/artist?sk=about">About</a></body></html>',
            "https://www.facebook.com/artist?sk=about": '<html><body><a href="mailto:test@artist.com">Email</a></body></html>',
        }
    )

    emails, resolved, reason = cde._extract_fb_emails_bounded(driver, "https://www.facebook.com/artist")

    assert emails == ["test@artist.com"]
    assert resolved == "https://www.facebook.com/artist?sk=about"
    assert reason == ""
    assert driver.calls == [
        "https://www.facebook.com/artist",
        "https://www.facebook.com/artist?sk=about",
    ]
    assert len(driver.calls) == 2


def test_extract_fb_emails_bounded_no_candidate_still_caps_fetches_at_two() -> None:
    driver = _FakeFacebookDriver(
        {
            "https://www.facebook.com/artist": "<html><body><div>No useful anchors</div></body></html>",
            "https://www.facebook.com/artist/about": "<html><body><div>No email here</div></body></html>",
        }
    )

    emails, resolved, reason = cde._extract_fb_emails_bounded(driver, "https://www.facebook.com/artist")

    assert emails == []
    assert resolved == "https://www.facebook.com/artist/about"
    assert reason == ""
    assert driver.calls == [
        "https://www.facebook.com/artist",
        "https://www.facebook.com/artist/about",
    ]
    assert len(driver.calls) == 2


def test_extract_fb_emails_bounded_uses_shared_session_navigation_for_main_and_secondary() -> None:
    driver = _NoRawGetFacebookDriver()
    session = _FakeBoundedFacebookSession(
        driver,
        {
            "https://www.facebook.com/artist": {
                "html": '<html><body><a href="/artist/about">About</a></body></html>',
            },
            "https://www.facebook.com/artist/about": {
                "html": '<html><body><a href="mailto:test@artist.com">Email</a></body></html>',
            },
        },
    )

    emails, resolved, reason = cde._extract_fb_emails_bounded(
        driver,
        "https://www.facebook.com/artist",
        fb_session=session,
    )

    assert emails == ["test@artist.com"]
    assert resolved == "https://www.facebook.com/artist/about"
    assert reason == ""
    assert session.calls == [
        "https://www.facebook.com/artist",
        "https://www.facebook.com/artist/about",
    ]


def test_scrape_single_fb_candidate_direct_fallback_prefers_directory_contact_info_without_extra_fetches(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    fallback_url = "https://www.facebook.com/artist/directory_contact_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
              </body>
            </html>
            """,
            main_url,
        ),
        fallback_url: (
            """
            <html>
              <body>
                <div>No email here either.</div>
              </body>
            </html>
            """,
            fallback_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == []
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "no_email"
    assert calls == [main_url, fallback_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "no_email_on_page"
    assert any(f"trying direct fallback {fallback_url}" in msg for msg in logs)


def test_scrape_single_fb_candidate_uses_first_preferred_fallback_variant(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    fallback_url = "https://www.facebook.com/artist/directory_contact_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
              </body>
            </html>
            """,
            main_url,
        ),
        fallback_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            fallback_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, fallback_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any(f"[FB Email] Visiting {fallback_url}" in msg for msg in logs)
