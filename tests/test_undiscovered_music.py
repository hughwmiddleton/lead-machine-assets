"""Deterministic regression tests for Undiscovered Music discovery source.

All tests use mocked HTML/responses; no live-network dependency.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pandas as pd
import pytest

import night_mode_runner
import origin_validator
import pipeline_runner
from lead_vault.merge import merge_csv_into_master
from lead_vault.schema import get_canonical_master_schema

import undiscovered_music as um


def _load_gui_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_undiscovered_gui", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
        app.setQuitOnLastWindowClosed(False)
    return app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIRECTORY_HTML = """
<!DOCTYPE html>
<html>
<body>
  <h1>Artists</h1>
  <a href="/artists/alpha-wave">Alpha Wave</a>
  <a href="https://undiscovered.music/artists/beta-pulse">Beta Pulse</a>
  <a href="/artists/gamma-beats">Gamma Beats</a>
  <a href="/artists/alpha-wave">Alpha Wave (duplicate)</a>
  <a href="/">Home</a>
  <a href="/artists">Artists</a>
  <a href="/events">Events</a>
  <a href="/artists/search?q=rock">Search</a>
  <a href="/venues/club-nova">Club Nova</a>
  <a href="/artists/123-main-st">123 Main St</a>
</body>
</html>
"""

STRONG_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Delta Soul | Undiscovered Music"></head>
<body>
  <h1>Delta Soul</h1>
  <p><strong>Hometown:</strong> Austin, TX</p>
  <p><strong>Genres:</strong> Blues, Soul, Funk</p>
  <p><strong>Website:</strong> <a href="https://deltasoulmusic.com">deltasoulmusic.com</a></p>
  <p><strong>Booking:</strong> Sarah Jones — booking@deltasoulmusic.com</p>
  <section>
    <h2>Upcoming Shows</h2>
    <ul>
      <li><span class="date">Sep 15, 2025</span> Antone's Nightclub</li>
      <li><span class="date">Oct 02, 2025</span> Continental Club</li>
    </ul>
  </section>
  <meta property="og:description" content="Delta Soul blends blues and soul.">
</body>
</html>
"""

NATIVE_LINKS_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Native Links Artist | Undiscovered Music"></head>
<body>
  <h1>Native Links Artist</h1>
  <p><strong>Genres:</strong> Indie</p>
  <p><strong>Website:</strong> <a href="https://nativelinks.example">nativelinks.example</a></p>
  <div id="social-links">
    <a href="https://instagram.com/native.links"></a>
    <a href="https://facebook.com/nativelinksartist"></a>
    <a href="https://soundcloud.com/native-links"></a>
    <a href="https://youtube.com/@nativelinks"></a>
    <a href="https://tiktok.com/@nativelinks"></a>
    <a href="https://open.spotify.com/artist/0123456789ABCDEFGHIJKL"></a>
    <a href="https://instagram.com/native.links"></a>
    <a href="/artists/search"></a>
    <a href="https://undiscovered.music/login"></a>
    <a href="https://facebook.com/sharer/sharer.php?u=https://nativelinks.example"></a>
    <a href="mailto:contact"></a>
  </div>
  <div id="subbrand">
    <a href="https://facebook.com/UndiscoveredMusicNetwork"></a>
  </div>
  <a class="share-button" href="https://twitter.com/intent/tweet?url=https://nativelinks.example">Share</a>
</body>
</html>
"""

NO_EMAIL_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Echo Drift"></head>
<body>
  <h1>Echo Drift</h1>
  <p><strong>Location:</strong> Berlin</p>
  <p><strong>Genre:</strong> Electronic</p>
  <a href="https://echodrift.bandcamp.com">Website</a>
  <p>Berlin-based electronic producer.</p>
</body>
</html>
"""

NO_WEBSITE_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Frost Byte"></head>
<body>
  <h1>Frost Byte</h1>
  <p><strong>From:</strong> Reykjavik</p>
  <p><strong>Genres:</strong> Ambient / Experimental</p>
  <p>Frost Byte creates icy soundscapes.</p>
  <section>
    <h2>Upcoming Shows</h2>
    <p>No upcoming shows</p>
  </section>
</body>
</html>
"""

NO_SHOWS_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Ghost Note"></head>
<body>
  <h1>Ghost Note</h1>
  <p><strong>Hometown:</strong> New Orleans</p>
  <p><strong>Genre:</strong> Jazz</p>
  <a href="https://ghostnotejazz.com">Official Site</a>
  <p>Traditional jazz quartet.</p>
</body>
</html>
"""

JUNK_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<body>
  <h1>123 Main Street Suite 400</h1>
  <p>Booking: info@venue.com</p>
</body>
</html>
"""

VENUE_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<body>
  <h1>The Blue Room LLC</h1>
  <p>Live music venue and bar & grill</p>
  <p>Contact: manager@blueroom.com</p>
</body>
</html>
"""

UNUSUAL_NAME_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="XÆA-12"></head>
<body>
  <h1>XÆA-12</h1>
  <p><strong>Genre:</strong> Hyperpop</p>
  <p>Experimental project.</p>
</body>
</html>
"""

EMPTY_PROFILE_HTML = """
<!DOCTYPE html>
<html>
<body></body>
</html>
"""


def _cloudflare_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(char) ^ key:02x}" for char in email)


BOOKING_EMAIL = "bookings@nativeartist.net"
CHROME_EMAIL = "support@undiscovered.music"
CLOUDFLARE_BOOKING_CARD_HTML = f"""
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Native Booking Artist"></head>
<body>
  <h1>Native Booking Artist</h1>
  <p><strong>Genres:</strong> Indie</p>
  <div class="card mb-5">
    <div class="card-header where-card">Booking Contact</div>
    <div class="card-body">
      <div>Native Booking Agent</div>
      <div>
        <a href="/cdn-cgi/l/email-protection#{_cloudflare_encode(BOOKING_EMAIL)}">
          <span class="__cf_email__" data-cfemail="{_cloudflare_encode(BOOKING_EMAIL)}">[email protected]</span>
        </a>
      </div>
    </div>
  </div>
  <footer>
    <a href="/cdn-cgi/l/email-protection#{_cloudflare_encode(CHROME_EMAIL)}">
      <span class="__cf_email__" data-cfemail="{_cloudflare_encode(CHROME_EMAIL)}">[email protected]</span>
    </a>
    <a href="mailto:hello@undiscovered.music">Site contact</a>
  </footer>
</body>
</html>
"""

MAILTO_BOOKING_CARD_HTML = """
<!DOCTYPE html>
<html>
<head><meta property="og:title" content="Mailto Booking Artist"></head>
<body>
  <h1>Mailto Booking Artist</h1>
  <p><strong>Genres:</strong> Folk</p>
  <div class="card mb-5">
    <div class="card-header where-card">Booking Contact</div>
    <div class="card-body">
      <div>Mailto Booking Agent</div>
      <div><a href="mailto:agent@mailtoartist.net?subject=Booking">Email agent</a></div>
    </div>
  </div>
</body>
</html>
"""


@pytest.fixture
def mock_fetch_html(monkeypatch):
    """Monkeypatch html_fetcher.fetch_html to return controlled responses."""
    _registry: Dict[str, Dict[str, Any]] = {}

    def _register(url: str, html: str = "", status: int = 200, reason: str = "ok") -> None:
        _registry[url] = {"html": html, "status": status, "reason": reason, "final_url": url, "mode_used": "requests", "elapsed_ms": 100, "domain": "undiscovered.music"}

    def _fetch(url, **kwargs):
        return _registry.get(url, {"html": "", "status": 404, "reason": "not_found", "final_url": url, "mode_used": "requests", "elapsed_ms": 100, "domain": "undiscovered.music"})

    monkeypatch.setattr("undiscovered_music.fetch_html", _fetch)
    monkeypatch.setattr("html_fetcher.fetch_html", _fetch)
    return _register


# ---------------------------------------------------------------------------
# 1-4. Discovery & URL hygiene
# ---------------------------------------------------------------------------

def test_discover_extracts_profile_links(mock_fetch_html):
    mock_fetch_html("https://undiscovered.music/artists", html=SAMPLE_DIRECTORY_HTML)
    urls = um.discover_artist_urls(max_results=50)
    assert len(urls) == 4
    assert "https://undiscovered.music/artists/alpha-wave" in urls
    assert "https://undiscovered.music/artists/beta-pulse" in urls
    assert "https://undiscovered.music/artists/gamma-beats" in urls


def test_profile_url_normalization():
    assert um.canonicalize_undiscovered_profile_url("/artists/slug") == ""
    assert um.canonicalize_undiscovered_profile_url("https://undiscovered.music/artists/slug") == "https://undiscovered.music/artists/slug"
    assert um.canonicalize_undiscovered_profile_url("https://undiscovered.music/artists/slug/") == "https://undiscovered.music/artists/slug"
    assert um.canonicalize_undiscovered_profile_url("https://undiscovered.music/artists/slug?utm=x") == ""
    assert um.canonicalize_undiscovered_profile_url("https://undiscovered.music/artists/slug#frag") == ""
    assert um.canonicalize_undiscovered_profile_url("https://undiscovered.music/") == ""
    assert um.canonicalize_undiscovered_profile_url("https://undiscovered.music/events") == ""


def test_duplicate_urls_removed(mock_fetch_html):
    mock_fetch_html("https://undiscovered.music/artists", html=SAMPLE_DIRECTORY_HTML)
    urls = um.discover_artist_urls(max_results=50)
    assert urls.count("https://undiscovered.music/artists/alpha-wave") == 1


def test_navigation_non_artist_urls_ignored(mock_fetch_html):
    mock_fetch_html("https://undiscovered.music/artists", html=SAMPLE_DIRECTORY_HTML)
    urls = um.discover_artist_urls(max_results=50)
    assert "https://undiscovered.music/" not in urls
    assert "https://undiscovered.music/events" not in urls
    assert "https://undiscovered.music/venues/club-nova" not in urls
    assert "https://undiscovered.music/artists/search" not in urls


def test_canonical_directory_hrefs_preserve_quotes_and_url_encoding():
    html = """
    <a href='/artists/"poor"_howard_stith'>"Poor" Howard Stith</a>
    <a href="/artists/rock%27n%27roll%20collective">Rock'n'Roll Collective</a>
    """

    assert um._extract_profile_links(html) == [
        'https://undiscovered.music/artists/"poor"_howard_stith',
        "https://undiscovered.music/artists/rock%27n%27roll%20collective",
    ]


# ---------------------------------------------------------------------------
# 5-13. Profile parsing
# ---------------------------------------------------------------------------

def test_strong_profile_parses_correctly():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["artist_name"] == "Delta Soul"
    assert profile["hometown_raw"] == "Austin, TX"
    assert profile["genre_primary"] == "Blues"
    assert "Blues" in profile["genres"]
    assert "Soul" in profile["genres"]
    assert "Funk" in profile["genres"]
    assert profile["website_url"] == "https://deltasoulmusic.com"
    assert profile["booking_contact_name"] == "Sarah Jones"
    assert profile["booking_email"] == "booking@deltasoulmusic.com"
    assert profile["upcoming_show_count"] == 2
    assert profile["next_upcoming_show_date"] == "Sep 15, 2025"
    assert "blues and soul" in profile["bio_text"].lower()


def test_native_booking_card_extracts_contact_name_and_cloudflare_email():
    profile_url = "https://undiscovered.music/artists/native-booking-artist"
    profile = um.parse_artist_profile(CLOUDFLARE_BOOKING_CARD_HTML, profile_url)
    row = um._build_row(profile, "2025-01-01")

    assert profile["booking_contact_name"] == "Native Booking Agent"
    assert profile["booking_email"] == BOOKING_EMAIL
    assert row["Booking_Contact_Name"] == "Native Booking Agent"
    assert row["Email"] == BOOKING_EMAIL
    assert row["Email_All"] == BOOKING_EMAIL
    assert row["Email_Source_URL"] == profile_url
    assert row["Email_Source_Type"] == "undiscovered_music_profile"
    assert row["Email_Extract_Method"] == "profile_direct"
    assert json.loads(row["Email_Provenance_JSON"]) == {
        BOOKING_EMAIL: {
            "extract_method": "profile_direct",
            "source_type": "undiscovered_music_profile",
            "source_url": profile_url,
            "surface": "undiscovered_music_profile",
        }
    }


def test_normal_mailto_booking_email_still_works_inside_booking_card():
    profile = um.parse_artist_profile(
        MAILTO_BOOKING_CARD_HTML,
        "https://undiscovered.music/artists/mailto-booking-artist",
    )

    assert profile["booking_contact_name"] == "Mailto Booking Agent"
    assert profile["booking_email"] == "agent@mailtoartist.net"


def test_multiple_native_booking_emails_use_existing_email_all_convention():
    html = """
    <html><head><meta property="og:title" content="Two Agents"></head><body>
      <p><strong>Genres:</strong> Rock</p>
      <div class="card">
        <div class="card-header">Booking Contact</div>
        <div class="card-body">
          <div>Two Agent Team</div>
          <div><a href="mailto:first@twoagents.net">First</a></div>
          <div><a href="mailto:second@twoagents.net">Second</a></div>
        </div>
      </div>
    </body></html>
    """
    profile_url = "https://undiscovered.music/artists/two-agents"
    profile = um.parse_artist_profile(html, profile_url)
    row = um._build_row(profile, "2025-01-01")

    assert row["Email"] == "first@twoagents.net"
    assert row["Email_All"] == "first@twoagents.net;second@twoagents.net"
    assert set(json.loads(row["Email_Provenance_JSON"])) == {
        "first@twoagents.net",
        "second@twoagents.net",
    }


def test_cloudflare_decoder_rejects_malformed_or_non_email_payloads():
    assert um._decode_cloudflare_email(_cloudflare_encode(BOOKING_EMAIL)) == BOOKING_EMAIL
    assert um._decode_cloudflare_email("not-hex") == ""
    assert um._decode_cloudflare_email("42") == ""
    assert um._decode_cloudflare_email(_cloudflare_encode("not an email")) == ""


def test_cloudflare_and_site_contacts_outside_booking_card_are_ignored():
    profile = um.parse_artist_profile(
        CLOUDFLARE_BOOKING_CARD_HTML,
        "https://undiscovered.music/artists/native-booking-artist",
    )

    assert profile["booking_emails"] == [BOOKING_EMAIL]
    assert CHROME_EMAIL not in profile["booking_emails"]
    assert "hello@undiscovered.music" not in profile["booking_emails"]


def test_page_with_only_sitewide_contacts_leaves_booking_fields_blank():
    html = f"""
    <html><head><meta property="og:title" content="No Booking Artist"></head><body>
      <p><strong>Genres:</strong> Ambient</p>
      <footer>
        <a href="/cdn-cgi/l/email-protection#{_cloudflare_encode(CHROME_EMAIL)}">
          <span class="__cf_email__" data-cfemail="{_cloudflare_encode(CHROME_EMAIL)}">[email protected]</span>
        </a>
        <a href="mailto:hello@undiscovered.music">Site contact</a>
      </footer>
    </body></html>
    """
    profile = um.parse_artist_profile(html, "https://undiscovered.music/artists/no-booking")
    row = um._build_row(profile, "2025-01-01")

    assert profile["booking_contact_name"] == ""
    assert profile["booking_email"] == ""
    assert row["Booking_Contact_Name"] == ""
    assert row["Email"] == ""
    assert row["Email_Provenance_JSON"] == ""


def test_hometown_parses_correctly():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["hometown_raw"] == "Austin, TX"


def test_genre_and_subgenres_parse_correctly():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["genre_primary"] == "Blues"
    assert "Soul" in profile["genres"]
    assert "Funk" in profile["genres"]


@pytest.mark.parametrize(
    ("raw_genres", "expected_primary"),
    [
        ("Singer/Songwriter", "Singer/Songwriter"),
        (
            "(Singer/Songwriter, Traditional Country, Rockabilly, Old Time Country, Roots Rock, Alt Country)",
            "Singer/Songwriter",
        ),
        ("Pop (Singer/Songwriter)", "Pop"),
        ("(Bluegrass, Western Swing)", "Bluegrass"),
        ("Folk", "Folk"),
        ("Folk/Americana (Singer/Songwriter, Americana, Folk Rock)", "Folk"),
        ("Celtic (Celtic)", "Celtic (Celtic)"),
        ("Alt-Country (Alt Country)", "Alt-Country (Alt Country)"),
        ("", ""),
        ("Pop (Singer/Songwriter", ""),
    ],
)
def test_primary_genre_normalization_preserves_valid_labels(
    raw_genres: str,
    expected_primary: str,
):
    assert um._derive_primary_genre(raw_genres) == expected_primary


@pytest.mark.parametrize(
    "raw_genres",
    [
        "(Singer/Songwriter, Traditional Country, Rockabilly)",
        "(Bluegrass, Western Swing)",
        "Pop (Singer/Songwriter)",
        "Celtic (Celtic)",
    ],
)
def test_genre_raw_remains_source_faithful(raw_genres: str):
    html = f"<p><strong>Genres:</strong> {raw_genres}</p>"
    profile = um.parse_artist_profile(html, "https://undiscovered.music/artists/example")
    row = um._build_row(profile, "2025-01-01")

    assert profile["genres"] == raw_genres
    assert row["Undiscovered_Genre_Raw"] == raw_genres
    assert row["Primary Genre"] == profile["genre_primary"]


def test_website_parses_and_enters_enrichment_path():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["website_url"] == "https://deltasoulmusic.com"
    row = um._build_row(profile, "2025-01-01")
    assert row["External Links"] == "https://deltasoulmusic.com"


def test_native_instagram_and_facebook_urls_are_retained():
    profile = um.parse_artist_profile(
        NATIVE_LINKS_PROFILE_HTML,
        "https://undiscovered.music/artists/native-links",
    )
    row = um._build_row(profile, "2025-01-01")

    assert "https://instagram.com/native.links" in row["Social Link"].split(" | ")
    assert "https://www.facebook.com/nativelinksartist" in row["Social Link"].split(" | ")


def test_native_soundcloud_url_populates_existing_soundcloud_field():
    profile = um.parse_artist_profile(
        NATIVE_LINKS_PROFILE_HTML,
        "https://undiscovered.music/artists/native-links",
    )
    row = um._build_row(profile, "2025-01-01")

    assert row["SoundCloud Link"] == "https://soundcloud.com/native-links"


def test_multiple_native_links_preserve_website_and_all_supported_socials():
    profile = um.parse_artist_profile(
        NATIVE_LINKS_PROFILE_HTML,
        "https://undiscovered.music/artists/native-links",
    )
    row = um._build_row(profile, "2025-01-01")

    assert row["External Links"] == "https://nativelinks.example"
    assert row["Social Link"].split(" | ") == [
        "https://instagram.com/native.links",
        "https://www.facebook.com/nativelinksartist",
        "https://soundcloud.com/native-links",
        "https://youtube.com/@nativelinks",
        "https://tiktok.com/@nativelinks",
        "https://open.spotify.com/artist/0123456789ABCDEFGHIJKL",
    ]


def test_native_social_platform_urls_are_normalized_and_malformed_values_rejected():
    html = """
    <html><head><meta property="og:title" content="Social Hygiene Artist"></head><body>
      <h1>Social Hygiene Artist</h1><p><strong>Genres:</strong> Indie</p>
      <div id="social-links">
        <a href="https://www.facebook.com/profile.php?id=123"></a>
        <a href="https://facebook.com/https://www.facebook.com/profile.php?id=456"></a>
        <a href="https://facebook.com/aformerfriendandfriends, a4merfriend"></a>
        <a href="https://instagram.com//aaronburdett"></a>
        <a href="https://instagram.com/valid.artist"></a>
        <a href="https://open.spotify.com/artist/accounts./en/status"></a>
        <a href="https://open.spotify.com/artist/0123456789ABCDEFGHIJKL"></a>
        <a href="https://soundcloud.com/m./davevansise"></a>
        <a href="https://soundcloud.com/valid-artist"></a>
        <a href="https://facebook.com/sharer/sharer.php?u=https://example.com"></a>
        <a href="https://instagram.com/explore"></a>
        <a href="https://facebook.com/UndiscoveredMusicNetwork"></a>
      </div>
    </body></html>
    """
    profile = um.parse_artist_profile(html, "https://undiscovered.music/artists/social-hygiene")
    row = um._build_row(profile, "2025-01-01")
    links = row["Social Link"].split(" | ")

    assert links == [
        "https://www.facebook.com/profile.php?id=123",
        "https://www.facebook.com/profile.php?id=456",
        "https://instagram.com/aaronburdett",
        "https://instagram.com/valid.artist",
        "https://open.spotify.com/artist/0123456789ABCDEFGHIJKL",
        "https://soundcloud.com/valid-artist",
    ]
    assert not any("facebook.com/https" in link for link in links)
    assert not any("," in link for link in links)
    assert "accounts./en/status" not in row["Social Link"]
    assert "m./davevansise" not in row["Social Link"]
    assert row["SoundCloud Link"] == "https://soundcloud.com/valid-artist"
    assert profile["spotify_artist_id"] == "0123456789ABCDEFGHIJKL"


def test_native_social_normalization_preserves_website_booking_and_admission_fields():
    html = STRONG_PROFILE_HTML.replace(
        "</body>",
        '<div id="social-links"><a href="https://instagram.com//delta.soul"></a></div></body>',
    )
    profile = um.parse_artist_profile(html, "https://undiscovered.music/artists/delta-soul")
    row = um._build_row(profile, "2025-01-01")

    assert um.qualify_artist_profile(profile) is True
    assert row["Social Link"] == "https://instagram.com/delta.soul"
    assert row["External Links"] == "https://deltasoulmusic.com"
    assert row["Booking_Contact_Name"] == "Sarah Jones"
    assert row["Email"] == "booking@deltasoulmusic.com"
    assert row["Email_All"] == "booking@deltasoulmusic.com"
    assert row["Email_Source_URL"] == "https://undiscovered.music/artists/delta-soul"
    assert row["Email_Source_Type"] == "undiscovered_music_profile"
    assert row["Email_Extract_Method"] == "profile_direct"
    assert row["Email_Provenance_JSON"]
    assert row["Lead_Source"] == "Undiscovered Music"
    assert row["Source_Directory"] == "undiscovered_music"


def test_internal_navigation_site_chrome_and_share_links_are_ignored():
    profile = um.parse_artist_profile(
        NATIVE_LINKS_PROFILE_HTML,
        "https://undiscovered.music/artists/native-links",
    )
    row = um._build_row(profile, "2025-01-01")
    emitted = row["Social Link"]

    assert "undiscovered.music" not in emitted
    assert "UndiscoveredMusicNetwork" not in emitted
    assert "/sharer/" not in emitted
    assert "/intent/" not in emitted
    assert "mailto:" not in emitted


def test_native_links_do_not_change_source_url_or_provenance_fields():
    profile_url = "https://undiscovered.music/artists/native-links"
    profile = um.parse_artist_profile(NATIVE_LINKS_PROFILE_HTML, profile_url)
    row = um._build_row(profile, "2025-01-01")

    assert row["Source URL"] == profile_url
    assert row["Source_URL"] == profile_url
    assert row["Lead_Source"] == "Undiscovered Music"
    assert row["Source_Directory"] == "undiscovered_music"
    assert row["Source Directory"] == "Undiscovered Music"
    assert row["Email_Source_URL"] == ""
    assert row["Email_Source_Type"] == ""
    assert row["Email_Extract_Method"] == ""
    assert row["Email_Provenance_JSON"] == ""


def test_public_booking_email_has_correct_provenance():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    row = um._build_row(profile, "2025-01-01")
    assert row["Email"] == "booking@deltasoulmusic.com"
    assert row["Email_Source_Type"] == "undiscovered_music_profile"
    assert row["Email_Extract_Method"] == "profile_direct"
    assert row["Email_Source_URL"] == "https://undiscovered.music/artists/delta-soul"
    assert json.loads(row["Email_Provenance_JSON"])["booking@deltasoulmusic.com"] == {
        "extract_method": "profile_direct",
        "source_type": "undiscovered_music_profile",
        "source_url": "https://undiscovered.music/artists/delta-soul",
        "surface": "undiscovered_music_profile",
    }


def test_profile_without_booking_email_remains_valid():
    profile = um.parse_artist_profile(NO_EMAIL_PROFILE_HTML, "https://undiscovered.music/artists/echo-drift")
    assert profile["booking_email"] == ""
    assert um.qualify_artist_profile(profile) is True


def test_profile_without_website_remains_valid_when_credible():
    profile = um.parse_artist_profile(NO_WEBSITE_PROFILE_HTML, "https://undiscovered.music/artists/frost-byte")
    assert profile["website_url"] == ""
    assert profile["hometown_raw"] == "Reykjavik"
    assert profile["genre_primary"] == "Ambient"
    assert um.qualify_artist_profile(profile) is True


def test_profile_without_upcoming_shows_remains_eligible():
    profile = um.parse_artist_profile(NO_SHOWS_PROFILE_HTML, "https://undiscovered.music/artists/ghost-note")
    assert profile["upcoming_show_count"] == 0
    assert um.qualify_artist_profile(profile) is True


def test_upcoming_show_activity_recorded_when_present():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["upcoming_show_count"] == 2
    assert profile["next_upcoming_show_date"] == "Sep 15, 2025"


# ---------------------------------------------------------------------------
# 14-16. Qualification / junk rejection
# ---------------------------------------------------------------------------

def test_malformed_non_artist_profile_rejected():
    profile = um.parse_artist_profile(EMPTY_PROFILE_HTML, "https://undiscovered.music/artists/empty")
    assert um.qualify_artist_profile(profile) is False


def test_address_venue_like_junk_rejected():
    profile = um.parse_artist_profile(JUNK_PROFILE_HTML, "https://undiscovered.music/artists/123-main-st")
    assert um.qualify_artist_profile(profile) is False

    profile2 = um.parse_artist_profile(VENUE_PROFILE_HTML, "https://undiscovered.music/artists/blue-room")
    assert um.qualify_artist_profile(profile2) is False


def test_unusual_but_valid_artist_name_retained():
    profile = um.parse_artist_profile(UNUSUAL_NAME_PROFILE_HTML, "https://undiscovered.music/artists/xaea-12")
    assert profile["artist_name"] == "XÆA-12"
    assert um.qualify_artist_profile(profile) is True


# ---------------------------------------------------------------------------
# 17-18. Failure tolerance
# ---------------------------------------------------------------------------

def test_one_failed_profile_does_not_abort_run(mock_fetch_html, monkeypatch, tmp_path):
    mock_fetch_html("https://undiscovered.music/artists", html=SAMPLE_DIRECTORY_HTML)
    # Register only one profile successfully; others 404
    mock_fetch_html("https://undiscovered.music/artists/alpha-wave", html=STRONG_PROFILE_HTML)
    # beta-pulse and gamma-beats unregistered → 404

    rows = um.scrape_undiscovered_music(target_count=10, params={})
    # Should get the one valid profile, not crash
    assert len(rows) >= 1
    assert any(r["Artist Name"] == "Delta Soul" for r in rows)


def test_scrape_counts_only_accepted_rows_toward_target_and_stops(monkeypatch):
    urls = [
        "https://undiscovered.music/artists/16_swan_street",
        'https://undiscovered.music/artists/"poor"_howard_stith',
        "https://undiscovered.music/artists/blue-room",
        "https://undiscovered.music/artists/echo-drift",
        "https://undiscovered.music/artists/frost-byte",
        "https://undiscovered.music/artists/must-not-be-fetched",
    ]
    html_by_url = {
        urls[0]: EMPTY_PROFILE_HTML,
        urls[1]: STRONG_PROFILE_HTML.replace("Delta Soul", '"Poor" Howard Stith'),
        urls[2]: VENUE_PROFILE_HTML,
        urls[3]: NO_EMAIL_PROFILE_HTML,
        urls[4]: NO_WEBSITE_PROFILE_HTML,
        urls[5]: STRONG_PROFILE_HTML,
    }
    discovery_limits = []
    fetched = []
    logs = []

    def _discover(max_results, logger_fn=None):
        discovery_limits.append(max_results)
        return urls

    def _fetch(url, **kwargs):
        fetched.append(url)
        return {"html": html_by_url[url], "status": 200, "reason": "ok"}

    monkeypatch.setattr(um, "discover_artist_urls", _discover)
    monkeypatch.setattr(um, "fetch_html", _fetch)

    rows = um.scrape_undiscovered_music(target_count=3, params={}, logger_fn=logs.append)

    assert discovery_limits == [0]
    assert len(rows) == 3
    assert fetched == urls[:5]
    assert rows[0]["Artist Name"] == '"Poor" Howard Stith'
    assert rows[0]["Source URL"] == urls[1]
    assert rows[0]["Source_URL"] == urls[1]
    assert all(row["Lead_Source"] == "Undiscovered Music" for row in rows)
    assert all(row["Source_Directory"] == "undiscovered_music" for row in rows)
    assert any("Rejected" in message and "16_swan_street" in message for message in logs)
    assert any("attempted=5, accepted=3, rejected=2, failed=0" in message for message in logs)


def test_scrape_returns_available_rows_when_candidate_pool_is_exhausted(monkeypatch):
    urls = [
        "https://undiscovered.music/artists/empty",
        "https://undiscovered.music/artists/echo-drift",
        "https://undiscovered.music/artists/unavailable",
        "https://undiscovered.music/artists/frost-byte",
    ]
    responses = {
        urls[0]: {"html": EMPTY_PROFILE_HTML, "status": 200},
        urls[1]: {"html": NO_EMAIL_PROFILE_HTML, "status": 200},
        urls[2]: {"html": "", "status": 503},
        urls[3]: {"html": NO_WEBSITE_PROFILE_HTML, "status": 200},
    }
    fetched = []
    logs = []

    monkeypatch.setattr(um, "discover_artist_urls", lambda max_results, logger_fn=None: urls)

    def _fetch(url, **kwargs):
        fetched.append(url)
        return responses[url]

    monkeypatch.setattr(um, "fetch_html", _fetch)

    rows = um.scrape_undiscovered_music(target_count=3, params={}, logger_fn=logs.append)

    assert len(rows) == 2
    assert fetched == urls
    assert any("attempted=4, accepted=2, rejected=1, failed=1" in message for message in logs)


def test_timeout_network_failure_is_neutral(mock_fetch_html):
    mock_fetch_html("https://undiscovered.music/artists", html="", status=503, reason="status_503")
    urls = um.discover_artist_urls(max_results=50)
    assert urls == []


# ---------------------------------------------------------------------------
# 19. Origin locking after downstream enrichment
# ---------------------------------------------------------------------------

def test_origin_remains_undiscovered_music_after_enrichment_simulation(tmp_path: Path):
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    row = um._build_row(profile, "2025-01-01")

    # Simulate what downstream enrichment might try to do
    from lead_vault.origin import safe_row_update, repair_origin_integrity_df

    safe_row_update(
        row,
        {
            "Lead_Source": "spotify",
            "Source_Directory": "spotify",
            "Source Directory": "spotify",
            "Email_Source_Type": "website_enrich",
        },
    )
    df = pd.DataFrame([row])
    repair_origin_integrity_df(df)

    assert df.at[0, "Lead_Source"] == "Undiscovered Music"
    assert df.at[0, "Source_Directory"] == "undiscovered_music"
    assert df.at[0, "Source Directory"] == "Undiscovered Music"
    assert df.at[0, "Email_Source_Type"] == "website_enrich"  # contact provenance independent


# ---------------------------------------------------------------------------
# 20. Cross-source dedupe
# ---------------------------------------------------------------------------

def test_duplicate_artist_does_not_create_second_canonical_lead(tmp_path: Path):
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "incoming.csv"

    master_row = {field: "" for field in get_canonical_master_schema()}
    master_row.update(
        {
            "Artist": "Delta Soul",
            "Location": "Austin, TX",
            "Source_URL": "https://undiscovered.music/artists/delta-soul",
            "Lead_Source": "Undiscovered Music",
            "Source_Directory": "undiscovered_music",
        }
    )
    with master_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=get_canonical_master_schema())
        writer.writeheader()
        writer.writerow(master_row)

    incoming = {
        "Artist Name": "Delta Soul",
        "Location": "Austin, TX",
        "Source_URL": "https://undiscovered.music/artists/delta-soul",
        "Lead_Source": "Undiscovered Music",
        "Source_Directory": "undiscovered_music",
    }
    with source_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(incoming.keys()))
        writer.writeheader()
        writer.writerow(incoming)

    result = merge_csv_into_master(source_path, master_path=master_path)
    with master_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert result["rows_added"] == 0
    assert len(rows) == 1
    assert rows[0]["Artist"] == "Delta Soul"
    assert rows[0]["Source_URL"] == "https://undiscovered.music/artists/delta-soul"


# ---------------------------------------------------------------------------
# 21. Source registration / Night Mode wiring
# ---------------------------------------------------------------------------

def test_undiscovered_music_job_populates_canonical_source_fields(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_dir = run_dir / "job_undiscovered_music_1"
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = job_dir / "raw.csv"

    with raw_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Artist Name", "Location", "Song Title", "Email"])
        writer.writeheader()
        writer.writerow({"Artist Name": "Delta Soul", "Location": "Austin, TX", "Song Title": "", "Email": ""})

    logger = MagicMock()
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [{"job_id": "job_undiscovered_music_1", "raw_csv": raw_csv.as_posix(), "source_directory": "undiscovered_music"}],
        logger,
    )

    assert master_path is not None
    df = pd.read_csv(master_path, dtype=str, keep_default_na=False).fillna("")
    assert len(df) == 1
    assert df.at[0, "Lead_Source"] == "Undiscovered Music"
    assert df.at[0, "Source_Directory"] == "undiscovered_music"
    assert df.at[0, "Source Directory"] == "Undiscovered Music"
    assert df.at[0, "__source_job"] == "job_undiscovered_music_1"


def test_run_directory_job_dispatches_undiscovered_music(monkeypatch, tmp_path: Path):
    raw_output = tmp_path / "raw.csv"
    calls = []

    def _fake_scrape(target_count, params, logger_fn=None):
        calls.append((target_count, params))
        return [
            {
                "Artist Name": "Test Artist",
                "Location": "Nashville",
                "Song Title": "",
                "Primary Genre": "Country",
                "Social Link": "https://instagram.com/testartist | https://facebook.com/testartist",
                "SoundCloud Link": "https://soundcloud.com/testartist",
                "External Links": "https://testartist.example",
                "Email": "test@test.com",
                "Source URL": "https://undiscovered.music/artists/test-artist",
            }
        ]

    monkeypatch.setattr("undiscovered_music.scrape_undiscovered_music", _fake_scrape)

    result_path = pipeline_runner.run_directory_job(
        {"directory": "undiscovered_music", "target_valid_leads": 5, "job_id": "job_um_1"},
        str(raw_output),
    )

    assert result_path is not None
    assert raw_output.exists()
    df = pd.read_csv(result_path, dtype=str, keep_default_na=False).fillna("")
    assert len(df) == 1
    assert df.at[0, "Artist Name"] == "Test Artist"
    assert df.at[0, "Social Link"] == "https://instagram.com/testartist | https://facebook.com/testartist"
    assert df.at[0, "SoundCloud Link"] == "https://soundcloud.com/testartist"
    assert df.at[0, "External Links"] == "https://testartist.example"
    assert df.at[0, "Lead_Source"] == "Undiscovered Music"
    assert df.at[0, "Source_Directory"] == "undiscovered_music"
    assert calls == [(5, {"max_results": 5, "url": ""})]


def test_main_artist_source_combo_includes_undiscovered_and_existing_sources():
    gui_source = (Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py").read_text(encoding="utf-8")
    expected = (
        'self.source_combo.addItems(["Unearthed", "Bandcamp", "SoundCloud", '
        '"Last.fm Similar", "Spotify", "Undiscovered Music", "AMRAP"])'
    )
    assert expected in gui_source


def test_undiscovered_main_gui_hides_irrelevant_source_inputs(qapp):
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    module = _load_gui_module()
    controls = SimpleNamespace(
        url_label=QtWidgets.QLabel("Website URL:"),
        url_edit=QtWidgets.QLineEdit(module.UNEARTHED_DEFAULT_URL),
        pages_per_tag_label=QtWidgets.QLabel("Pages per Tag:"),
        pages_per_tag_edit=QtWidgets.QLineEdit(str(module.BANDCAMP_PAGES_PER_TAG)),
        sc_meta_checkbox=QtWidgets.QCheckBox(),
    )

    module.MainWindow.on_source_changed(controls, "Undiscovered Music")

    assert controls.url_label.isHidden()
    assert controls.url_edit.isHidden()
    assert controls.pages_per_tag_label.isHidden()
    assert controls.pages_per_tag_edit.isHidden()
    assert not controls.pages_per_tag_edit.isEnabled()
    assert controls.sc_meta_checkbox.isHidden()


def test_artist_scraper_thread_dispatches_undiscovered_target_to_existing_backend(qapp, monkeypatch, tmp_path: Path):
    module = _load_gui_module()
    calls = []

    def _fake_run_directory_job(job_config, output_csv, logger=None):
        calls.append((job_config, output_csv, logger))
        return output_csv

    monkeypatch.setattr(module.pipeline_runner, "run_directory_job", _fake_run_directory_job)
    output_csv = tmp_path / "undiscovered.csv"
    thread = module.ArtistScraperThread("", 17, str(output_csv), source="Undiscovered Music")

    thread.run()

    assert len(calls) == 1
    job_config, dispatched_output, logger = calls[0]
    assert job_config == {"directory": "undiscovered_music", "target_valid_leads": 17}
    assert dispatched_output == str(output_csv)
    assert callable(logger)


def test_start_artist_scraping_builds_undiscovered_thread(qapp, monkeypatch, tmp_path: Path):
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    module = _load_gui_module()
    created = []

    class _Signal:
        def connect(self, callback):
            self.callback = callback

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            created.append((args, kwargs, self))
            self.log_signal = _Signal()
            self.finished_signal = _Signal()
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(module, "ArtistScraperThread", _FakeThread)
    source_combo = QtWidgets.QComboBox()
    source_combo.addItem("Undiscovered Music")
    controls = SimpleNamespace(
        source_combo=source_combo,
        url_edit=QtWidgets.QLineEdit(""),
        max_artists_edit=QtWidgets.QLineEdit("31"),
        pages_per_tag_edit=QtWidgets.QLineEdit("1"),
        artist_output_csv_edit=QtWidgets.QLineEdit(str(tmp_path / "artists.csv")),
        artist_start_button=QtWidgets.QPushButton(),
        artist_progress_bar=QtWidgets.QProgressBar(),
        artist_log=QtWidgets.QTextEdit(),
        current_artist_source="",
        artist_thread=None,
        update_artist_log=lambda message: None,
        artist_scraping_finished=lambda: None,
    )

    module.MainWindow.start_artist_scraping(controls)

    assert len(created) == 1
    args, kwargs, thread = created[0]
    assert args[:3] == ("", 31, str(tmp_path / "artists.csv"))
    assert kwargs["source"] == "Undiscovered Music"
    assert controls.current_artist_source == "Undiscovered Music"
    assert thread.started


def test_night_mode_dialog_round_trips_undiscovered_job(qapp):
    module = _load_gui_module()
    original = {
        "job_id": "job_undiscovered_gui",
        "directory": "undiscovered_music",
        "target_valid_leads": 23,
        "max_hours": 1.5,
        "notes": "directory-wide discovery",
    }

    dialog = module.NightModeJobDialog(original)
    assert dialog.directory_combo.currentText() == "undiscovered_music"
    assert dialog.target_spin.value() == 23
    assert dialog.input_label.isHidden()
    assert dialog.input_edit.isHidden()
    assert not dialog.mode_combo.isEnabled()
    assert dialog.amrap_genre_edit.isHidden()

    saved = dialog.get_job()
    reloaded = module.NightModeJobDialog(saved)
    assert saved["directory"] == "undiscovered_music"
    assert saved["target_valid_leads"] == 23
    assert reloaded.directory_combo.currentText() == "undiscovered_music"
    assert reloaded.target_spin.value() == 23
    dialog.close()
    reloaded.close()


def test_night_mode_runner_normalizes_undiscovered_music_source():
    assert night_mode_runner._normalise_seed_source_name("undiscovered_music") == "undiscovered_music"
    assert night_mode_runner._normalise_seed_source_name("Undiscovered Music") == "undiscovered_music"


# ---------------------------------------------------------------------------
# 22. Adjacent source/provenance regressions remain green (run externally)
# ---------------------------------------------------------------------------


def test_infer_discovery_source_labels_undiscovered_music():
    row = pd.Series({"Source Directory": "undiscovered_music", "Spotify Playlist": ""})
    assert pipeline_runner.infer_discovery_source(row) == "Undiscovered Music"


def test_infer_email_source_labels_undiscovered_music():
    row = pd.Series(
        {
            "Email": "booking@test.com",
            "Email_Type": "",
            "Email_Source_Type": "undiscovered_music_profile",
            "FB_Status": "",
            "Source Directory": "undiscovered_music",
            "Source URL": "https://undiscovered.music/artists/test",
        }
    )
    assert pipeline_runner.infer_email_source(row) == "Undiscovered Music profile"


def test_origin_validator_infers_undiscovered_music_directory():
    assert origin_validator._infer_directory_from_url("https://undiscovered.music/artists/test") == "undiscovered_music"
    assert origin_validator._normalise_source_directory("Undiscovered Music") == "undiscovered_music"
    assert origin_validator._choose_checker("undiscovered_music") is not None
