"""Deterministic regression tests for Undiscovered Music discovery source.

All tests use mocked HTML/responses; no live-network dependency.
"""

from __future__ import annotations

import csv
import json
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


def test_hometown_parses_correctly():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["hometown_raw"] == "Austin, TX"


def test_genre_and_subgenres_parse_correctly():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["genre_primary"] == "Blues"
    assert "Soul" in profile["genres"]
    assert "Funk" in profile["genres"]


def test_website_parses_and_enters_enrichment_path():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    assert profile["website_url"] == "https://deltasoulmusic.com"
    row = um._build_row(profile, "2025-01-01")
    assert row["External Links"] == "https://deltasoulmusic.com"


def test_public_booking_email_has_correct_provenance():
    profile = um.parse_artist_profile(STRONG_PROFILE_HTML, "https://undiscovered.music/artists/delta-soul")
    row = um._build_row(profile, "2025-01-01")
    assert row["Email"] == "booking@deltasoulmusic.com"
    assert row["Email_Source_Type"] == "undiscovered_music_profile"
    assert row["Email_Extract_Method"] == "profile_direct"
    assert "undiscovered_music_profile" in row["Email_Provenance_JSON"]
    assert row["Email_Source_URL"] == "https://undiscovered.music/artists/delta-soul"


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

    def _fake_scrape(target_count, params, logger=None):
        calls.append((target_count, params))
        return [
            {
                "Artist Name": "Test Artist",
                "Location": "Nashville",
                "Song Title": "",
                "Primary Genre": "Country",
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
    assert df.at[0, "Lead_Source"] == "Undiscovered Music"
    assert df.at[0, "Source_Directory"] == "undiscovered_music"


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
