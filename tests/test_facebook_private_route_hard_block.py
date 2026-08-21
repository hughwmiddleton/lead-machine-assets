import logging
import urllib.parse
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

import night_mode_fb
from facebook_enrich import (
    FbCandidate,
    _fb_is_candidate_url_allowed,
    is_junk_facebook_candidate,
)
from source_scheduler import canonicalize_facebook_url


# ---------------------------------------------------------------------------
# 1. Blocked route classification via canonicalize_facebook_url
# ---------------------------------------------------------------------------

_BLOCKED_CANONICAL_URLS = [
    "https://www.facebook.com/messages/e2ee/t/123",
    "https://facebook.com/messages/t/123",
    "https://www.facebook.com/notifications",
    "https://www.facebook.com/settings",
    "https://www.facebook.com/groups/example",
    "https://www.facebook.com/marketplace",
    "https://www.facebook.com/messenger",
    "https://www.facebook.com/login",
    "https://www.facebook.com/logout",
    "https://www.facebook.com/recover",
    "https://www.facebook.com/checkpoint",
    "https://www.facebook.com/help",
    "https://www.facebook.com/privacy",
    "https://www.facebook.com/gaming",
    "https://www.facebook.com/friends",
    "https://www.facebook.com/bookmarks",
    "https://www.facebook.com/saved",
    "https://www.facebook.com/watch",
    "https://www.facebook.com/reel",
    "https://www.facebook.com/events",
    "https://www.facebook.com/share",
    "https://www.facebook.com/share/something",
    "https://m.me/example",
    "https://www.m.me/example",
]

_ALLOWED_CANONICAL_URLS = [
    "https://www.facebook.com/exampleartist",
    "https://facebook.com/exampleartist",
    "https://www.facebook.com/profile.php?id=123456",
    "https://www.facebook.com/exampleartist/about",
    "https://www.facebook.com/exampleartist/contact",
    "https://fb.me/exampleartist",
]


@pytest.mark.parametrize("url", _BLOCKED_CANONICAL_URLS)
def test_canonicalize_facebook_url_rejects_blocked_routes(url: str):
    assert canonicalize_facebook_url(url) == "", f"Expected empty for {url}"


@pytest.mark.parametrize("url", _ALLOWED_CANONICAL_URLS)
def test_canonicalize_facebook_url_allows_public_pages(url: str):
    result = canonicalize_facebook_url(url)
    assert result != "", f"Expected non-empty canonical for {url}"
    assert "facebook.com" in result


# ---------------------------------------------------------------------------
# 2. Candidate selection guard via _fb_is_candidate_url_allowed
# ---------------------------------------------------------------------------

_BLOCKED_CANDIDATE_URLS = [
    "https://www.facebook.com/messages/e2ee/t/123",
    "https://www.facebook.com/messenger",
    "https://www.facebook.com/notifications",
    "https://www.facebook.com/settings",
    "https://www.facebook.com/groups/example",
    "https://www.facebook.com/marketplace",
    "https://www.facebook.com/login",
    "https://www.facebook.com/logout",
    "https://www.facebook.com/watch",
    "https://www.facebook.com/events",
]

_ALLOWED_CANDIDATE_URLS = [
    "https://www.facebook.com/exampleartist",
    "https://www.facebook.com/profile.php?id=123456",
    "https://www.facebook.com/profile.php?id=123456&foo=1",
]


@pytest.mark.parametrize("url", _BLOCKED_CANDIDATE_URLS)
def test_fb_candidate_url_allowed_rejects_blocked_routes(url: str):
    assert _fb_is_candidate_url_allowed(url) is False


@pytest.mark.parametrize("url", _ALLOWED_CANDIDATE_URLS)
def test_fb_candidate_url_allowed_allows_public_pages(url: str):
    assert _fb_is_candidate_url_allowed(url) is True


# ---------------------------------------------------------------------------
# 3. Junk candidate guard via is_junk_facebook_candidate
# ---------------------------------------------------------------------------


def test_junk_candidate_rejects_messenger_url():
    candidate = FbCandidate(name="Test", url="https://www.facebook.com/messages/t/123", category="")
    assert is_junk_facebook_candidate(candidate) is True


def test_junk_candidate_rejects_notifications_url():
    candidate = FbCandidate(name="Test", url="https://www.facebook.com/notifications", category="")
    assert is_junk_facebook_candidate(candidate) is True


def test_junk_candidate_allows_public_page():
    candidate = FbCandidate(name="Test Band", url="https://www.facebook.com/testband", category="Band")
    assert is_junk_facebook_candidate(candidate) is False


# ---------------------------------------------------------------------------
# 4. Secondary page selection guard via _pick_fb_contact_link
# ---------------------------------------------------------------------------


def test_pick_fb_contact_link_rejects_messenger_in_html():
    html = """
    <html><body>
        <a href="/messages/t/123">Messages</a>
        <a href="/about">About</a>
    </body></html>
    """
    result = night_mode_fb._pick_fb_contact_link(
        __import__("bs4").BeautifulSoup(html, "html.parser"),
        "https://www.facebook.com/artist",
    )
    assert result is not None
    assert "messages" not in result.lower()
    assert "about" in result.lower()


def test_pick_fb_contact_link_rejects_settings_in_html():
    html = """
    <html><body>
        <a href="/settings">Settings</a>
        <a href="/about_contact_and_basic_info">Contact</a>
    </body></html>
    """
    result = night_mode_fb._pick_fb_contact_link(
        __import__("bs4").BeautifulSoup(html, "html.parser"),
        "https://www.facebook.com/artist",
    )
    assert result is not None
    assert "settings" not in result.lower()
    assert "about_contact_and_basic_info" in result.lower()


def test_pick_fb_contact_link_returns_none_when_only_blocked():
    html = """
    <html><body>
        <a href="/messages/t/123">Messages</a>
        <a href="/notifications">Notifications</a>
    </body></html>
    """
    result = night_mode_fb._pick_fb_contact_link(
        __import__("bs4").BeautifulSoup(html, "html.parser"),
        "https://www.facebook.com/artist",
    )
    assert result is None


# ---------------------------------------------------------------------------
# 5. Pre-navigation guard via _load_fb_page_with_timeout
# ---------------------------------------------------------------------------


def test_load_fb_page_blocks_private_url_before_navigation():
    """A mock driver whose get() should never be called for a blocked URL."""
    driver = MagicMock()
    driver.current_url = "about:blank"
    driver.page_source = ""

    html, current_url, timed_out = night_mode_fb._load_fb_page_with_timeout(
        driver,
        "https://www.facebook.com/messages/e2ee/t/123",
        timeout_s=20.0,
        logger=None,
        unblock_on_ready=False,
    )

    assert html == ""
    assert "messages" in current_url
    assert timed_out is False
    driver.get.assert_not_called()


def test_load_fb_page_blocks_messenger_before_navigation():
    driver = MagicMock()
    driver.current_url = "about:blank"
    driver.page_source = ""

    html, current_url, timed_out = night_mode_fb._load_fb_page_with_timeout(
        driver,
        "https://www.facebook.com/messenger",
        timeout_s=20.0,
        logger=None,
        unblock_on_ready=False,
    )

    assert html == ""
    assert timed_out is False
    driver.get.assert_not_called()


def test_load_fb_page_allows_public_page_navigation():
    driver = MagicMock()
    driver.current_url = "about:blank"
    driver.page_source = "<html><body>public page</body></html>"

    html, current_url, timed_out = night_mode_fb._load_fb_page_with_timeout(
        driver,
        "https://www.facebook.com/exampleartist",
        timeout_s=20.0,
        logger=None,
        unblock_on_ready=False,
    )

    # Navigation should proceed for public pages.
    driver.get.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Post-redirect / final URL guard
# ---------------------------------------------------------------------------


def test_load_fb_page_post_redirect_rejects_messages():
    """Simulate navigation where current_url lands on /messages after redirect."""
    driver = MagicMock()
    # First call is before navigation; second is after.
    driver.current_url = "https://www.facebook.com/messages/t/123"
    driver.page_source = ""

    html, current_url, timed_out = night_mode_fb._load_fb_page_with_timeout(
        driver,
        "https://www.facebook.com/exampleartist",
        timeout_s=20.0,
        logger=None,
        unblock_on_ready=True,
    )

    # Because the handoff URL is invalid (messages), the loop should time out
    # or break without accepting the page.  unblock_on_ready path uses a loop
    # that checks _is_valid_fb_handoff_url; if the current_url is messages,
    # url_valid and usable_handoff_url are both False, so the loop continues
    # until timeout.
    assert timed_out is True
