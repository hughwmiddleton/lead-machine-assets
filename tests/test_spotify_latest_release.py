"""Deterministic Spotify latest-release and Undiscovered integration tests."""

from __future__ import annotations

import csv
from typing import Any, Dict, List

import pytest

import pipeline_runner
import spotify_latest_release as slr
import undiscovered_music as um


ARTIST_ID = "0123456789ABCDEFGHIJKL"
OTHER_ID = "ZYXWVUTSRQPONMLKJIHGFE"
ARTIST_URL = f"https://open.spotify.com/artist/{ARTIST_ID}"


def _release(
    release_id: str,
    date: str,
    precision: str = "day",
    release_type: str = "single",
    name: str = "Release",
    artist_id: str = ARTIST_ID,
) -> Dict[str, Any]:
    return {
        "id": release_id,
        "name": name,
        "album_type": release_type,
        "album_group": release_type,
        "release_date": date,
        "release_date_precision": precision,
        "artists": [{"id": artist_id, "name": "Target"}],
    }


def _track(
    title: str,
    *,
    artist_id: str = ARTIST_ID,
    track_number: int = 1,
    disc_number: int = 1,
) -> Dict[str, Any]:
    return {
        "id": f"track-{disc_number}-{track_number}-{title}",
        "name": title,
        "artists": [{"id": artist_id, "name": "Target"}],
        "track_number": track_number,
        "disc_number": disc_number,
    }


class FakeSpotifyClient:
    def __init__(self, releases: List[Dict[str, Any]], tracks: Dict[str, List[Dict[str, Any]]]):
        self.releases = releases
        self.tracks = tracks
        self.release_calls = 0
        self.track_calls: List[str] = []

    def get_artist_releases(self, artist_id: str):
        assert artist_id == ARTIST_ID
        self.release_calls += 1
        return self.releases

    def get_album_tracks(self, album_id: str):
        self.track_calls.append(album_id)
        return self.tracks.get(album_id, [])


@pytest.mark.parametrize(
    "value",
    [ARTIST_URL, f"{ARTIST_URL}?si=abc", f"spotify:artist:{ARTIST_ID}"],
)
def test_valid_spotify_artist_identity_extracts_canonical_id(value):
    assert slr.extract_spotify_artist_identity(value) == (ARTIST_ID, ARTIST_URL)


@pytest.mark.parametrize(
    "value",
    [
        "https://open.spotify.com/artist/accounts./en/status",
        "https://open.spotify.com/track/0123456789ABCDEFGHIJKL",
        "https://open.spotify.com/artist/too-short",
        f"https://evil.example/artist/{ARTIST_ID}",
        f"https://open.spotify.com/artist/{ARTIST_ID}/extra",
        f"spotify:album:{ARTIST_ID}",
    ],
)
def test_malformed_or_non_artist_spotify_identity_is_rejected(value):
    assert slr.extract_spotify_artist_identity(value) == ("", "")


def test_release_order_uses_dates_not_api_response_order_and_populates_fields():
    client = FakeSpotifyClient(
        [_release("old", "2024-12-31"), _release("new", "2025-02-01")],
        {"old": [_track("Old Song")], "new": [_track("Newest Song")]},
    )
    result = slr.select_latest_artist_release(client, ARTIST_ID)
    assert result == slr.LatestReleaseResult("Newest Song", "2025-02-01")
    assert client.track_calls == ["new"]


def test_newest_single_is_preferred_over_equal_date_album():
    client = FakeSpotifyClient(
        [
            _release("album", "2025-03-04", release_type="album"),
            _release("single", "2025-03-04", release_type="single"),
        ],
        {"album": [_track("Album Opener")], "single": [_track("Newest Single")]},
    )
    assert slr.select_latest_artist_release(client, ARTIST_ID) == slr.LatestReleaseResult(
        "Newest Single", "2025-03-04"
    )


def test_date_precision_is_deterministic_and_respected():
    client = FakeSpotifyClient(
        [
            _release("year", "2025", precision="year"),
            _release("month", "2025-06", precision="month"),
            _release("day", "2025-06-15", precision="day"),
        ],
        {
            "year": [_track("Year Song")],
            "month": [_track("Month Song")],
            "day": [_track("Day Song")],
        },
    )
    assert slr.select_latest_artist_release(client, ARTIST_ID) == slr.LatestReleaseResult(
        "Day Song", "2025-06-15"
    )


def test_album_fallback_chooses_first_artist_track_in_disc_track_order():
    client = FakeSpotifyClient(
        [_release("album", "2025-08", precision="month", release_type="album")],
        {
            "album": [
                _track("Unrelated", artist_id=OTHER_ID, track_number=1),
                _track("Second", track_number=2),
                _track("First Eligible", track_number=1),
            ]
        },
    )
    assert slr.select_latest_artist_release(client, ARTIST_ID) == slr.LatestReleaseResult(
        "First Eligible", "2025-08"
    )


def test_false_artist_matches_compilations_and_tribute_releases_are_excluded():
    compilation = _release("comp", "2026-01-01", name="Compilation")
    compilation["album_type"] = compilation["album_group"] = "compilation"
    client = FakeSpotifyClient(
        [
            _release("false", "2027-01-01", artist_id=OTHER_ID),
            compilation,
            _release("tribute", "2026-02-01", name="A Tribute to Someone"),
            _release("valid", "2025-01-01"),
        ],
        {"valid": [_track("Real Latest Song")]},
    )
    assert slr.select_latest_artist_release(client, ARTIST_ID).song_title == "Real Latest Song"
    assert client.track_calls == ["valid"]


def test_enricher_caches_duplicate_artist_ids_and_fails_gracefully():
    client = FakeSpotifyClient([_release("single", "2025-01-02")], {"single": [_track("Cached Song")]})
    enricher = slr.SpotifyLatestReleaseEnricher(client)
    assert enricher.lookup(ARTIST_ID).song_title == "Cached Song"
    assert enricher.lookup(ARTIST_ID).song_title == "Cached Song"
    assert client.release_calls == 1

    class FailingClient:
        def get_artist_releases(self, artist_id):
            raise RuntimeError("transient Spotify failure")

    failed = slr.SpotifyLatestReleaseEnricher(FailingClient())
    assert failed.lookup(ARTIST_ID) == slr.LatestReleaseResult()
    assert failed.lookup_failures == 1


def _profile_html(spotify_url: str = "", name: str = "Accepted Artist") -> str:
    spotify = f'<a href="{spotify_url}">Spotify</a>' if spotify_url else ""
    return f"""
    <html><head><meta property="og:title" content="{name}"></head><body>
      <h1>{name}</h1><p><strong>Genres:</strong> Indie</p>
      <div id="social-links">{spotify}<a href="https://soundcloud.com/accepted">SoundCloud</a></div>
      <div class="card"><div class="card-header">Booking Contact</div>
      <div class="card-body"><div>Agent Name</div><a href="mailto:agent@accepted.net">Email</a></div></div>
    </body></html>
    """


def test_accepted_undiscovered_row_is_enriched_without_changing_origin_or_contacts(
    monkeypatch, tmp_path
):
    url = "https://undiscovered.music/artists/accepted"
    fake = FakeSpotifyClient([_release("single", "2025-09-01")], {"single": [_track("Accepted Song")]})
    monkeypatch.setattr(um, "discover_artist_urls", lambda **kwargs: [url])
    monkeypatch.setattr(
        um,
        "fetch_html",
        lambda *args, **kwargs: {"html": _profile_html(ARTIST_URL), "status": 200},
    )
    monkeypatch.setattr(um, "SpotifyClient", lambda **kwargs: fake)

    row = um.scrape_undiscovered_music(target_count=1, params={})[0]
    assert row["Song Title"] == "Accepted Song"
    assert row["Release Date"] == "2025-09-01"
    assert "Spotify_Artist_ID" not in row
    assert "Spotify_URL" not in row
    assert ARTIST_URL in row["Social Link"].split(" | ")
    assert row["Lead_Source"] == "Undiscovered Music"
    assert row["Source_Directory"] == "undiscovered_music"
    assert row["Source Directory"] == "Undiscovered Music"
    assert row["SoundCloud Link"] == "https://soundcloud.com/accepted"
    assert row["Booking_Contact_Name"] == "Agent Name"
    assert row["Email"] == "agent@accepted.net"

    output_path = tmp_path / "undiscovered_operator.csv"
    pipeline_runner._write_rows_to_csv([row], str(output_path), source_directory="undiscovered_music")
    with output_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        emitted = next(reader)
        headers = reader.fieldnames or []
    assert "Spotify_Artist_ID" not in headers
    assert "Spotify_URL" not in headers
    assert emitted["Song Title"] == "Accepted Song"
    assert emitted["Release Date"] == "2025-09-01"


def test_missing_or_malformed_identity_stays_blank_and_does_not_call_spotify(monkeypatch):
    urls = [
        "https://undiscovered.music/artists/missing",
        "https://undiscovered.music/artists/malformed",
    ]
    html = {
        urls[0]: _profile_html(name="Missing Artist"),
        urls[1]: _profile_html(
            "https://open.spotify.com/artist/accounts./en/status", name="Malformed Artist"
        ),
    }
    monkeypatch.setattr(um, "discover_artist_urls", lambda **kwargs: urls)
    monkeypatch.setattr(um, "fetch_html", lambda url, **kwargs: {"html": html[url], "status": 200})
    monkeypatch.setattr(
        um,
        "SpotifyClient",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Spotify must not be constructed")),
    )
    rows = um.scrape_undiscovered_music(target_count=2, params={})
    assert [(row["Song Title"], row["Release Date"]) for row in rows] == [("", ""), ("", "")]


def test_rejected_undiscovered_candidate_never_calls_spotify(monkeypatch):
    url = "https://undiscovered.music/artists/rejected"
    rejected_html = f"""
    <html><head><meta property="og:title" content="123 Main Street"></head><body>
      <h1>123 Main Street</h1><div id="social-links"><a href="{ARTIST_URL}">Spotify</a></div>
    </body></html>
    """
    monkeypatch.setattr(um, "discover_artist_urls", lambda **kwargs: [url])
    monkeypatch.setattr(um, "fetch_html", lambda *args, **kwargs: {"html": rejected_html, "status": 200})
    monkeypatch.setattr(
        um,
        "SpotifyClient",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("rejected row called Spotify")),
    )
    assert um.scrape_undiscovered_music(target_count=1, params={}) == []


def test_spotify_api_failure_keeps_accepted_undiscovered_row_usable(monkeypatch):
    url = "https://undiscovered.music/artists/accepted"

    class FailingClient:
        def get_artist_releases(self, artist_id):
            raise RuntimeError("429 cooldown")

    monkeypatch.setattr(um, "discover_artist_urls", lambda **kwargs: [url])
    monkeypatch.setattr(
        um, "fetch_html", lambda *args, **kwargs: {"html": _profile_html(ARTIST_URL), "status": 200}
    )
    monkeypatch.setattr(um, "SpotifyClient", lambda **kwargs: FailingClient())
    row = um.scrape_undiscovered_music(target_count=1, params={})[0]
    assert row["Artist Name"] == "Accepted Artist"
    assert row["Song Title"] == ""
    assert row["Release Date"] == ""
    assert row["Lead_Source"] == "Undiscovered Music"
