import pytest

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


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

