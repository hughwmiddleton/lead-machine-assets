"""Deterministic coverage for the AMRAP release identity cascade."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

import amrap_scraper as amrap
import bandcamp_profile_engine as bpe
import cross_directory_enricher as cde


ARTIST_ID = "0123456789ABCDEFGHIJKL"
SPOTIFY_URL = f"https://open.spotify.com/artist/{ARTIST_ID}"
AMRAP_URL = "https://amrap.org.au/artist/safe-artist"
BANDCAMP_URL = "https://safeartist.bandcamp.com/album/example-ep"


def _row(**updates: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "Artist Name": "Safe Artist",
        "Location": "VIC, Melbourne",
        "Song Title": "",
        "Release Date": "",
        "Social Link": "",
        "External Links": "",
        "Date Added": "2026-09-03",
        "Lead_Source": "AMRAP",
        "Source_Directory": "amrap",
        "Source Directory": "AMRAP",
        "Source URL": AMRAP_URL,
        "Source_URL": AMRAP_URL,
    }
    row.update(updates)
    return row


class FakeSpotifyClient:
    def __init__(self) -> None:
        self.release_calls = 0

    def get_artist_releases(self, artist_id: str) -> List[Dict[str, Any]]:
        assert artist_id == ARTIST_ID
        self.release_calls += 1
        return [{
            "id": "release-1",
            "name": "Release Name",
            "album_type": "single",
            "release_date": "2026-09-01",
            "release_date_precision": "day",
            "artists": [{"id": ARTIST_ID}],
        }]

    def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        assert album_id == "release-1"
        return [{
            "id": "track-1",
            "name": "Spotify Track",
            "artists": [{"id": ARTIST_ID}],
            "disc_number": 1,
            "track_number": 1,
        }]


def _bandcamp_result(
    *,
    page_artist: str = "Safe Artist",
    track_title: str = "Actual Song",
    track_date: str = "2026-08-31",
    browser_used: bool = False,
) -> bpe.BandcampProfileResult:
    return bpe.BandcampProfileResult(
        bpe.PROFILE_ACCEPTED,
        "https://safeartist.bandcamp.com/",
        profile={
            "artist_name": page_artist,
            "latest_release_title": "Example EP",
            "latest_release_date": track_date,
            "latest_track_title": track_title,
            "latest_track_date": track_date,
        },
        browser_used=browser_used,
        identity_evidence={"page_artist": page_artist},
    )


def _website_result(url: str, html: str) -> cde.WebsiteFetchResult:
    return cde.WebsiteFetchResult(url, url, 200, "text/html", html, True)


def test_native_pair_short_circuits_every_lookup(monkeypatch) -> None:
    client = FakeSpotifyClient()
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Bandcamp called")))
    row = _row(**{"Song Title": "Native Track", "Release Date": "2026-09-02", "Social Link": f"{SPOTIFY_URL}; {BANDCAMP_URL}; https://safeartist.example"})
    amrap.enrich_amrap_release_cascade([row], spotify_client=client)
    assert (row["Song Title"], row["Release Date"]) == ("Native Track", "2026-09-02")
    assert client.release_calls == 0


def test_direct_spotify_wins_before_bandcamp(monkeypatch) -> None:
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Bandcamp called")))
    client = FakeSpotifyClient()
    row = _row(**{"Social Link": f"{SPOTIFY_URL}; {BANDCAMP_URL}"})
    amrap.enrich_amrap_release_cascade([row], spotify_client=client)
    assert (row["Song Title"], row["Release Date"]) == ("Spotify Track", "2026-09-01")
    assert client.release_calls == 1


def test_direct_bandcamp_requires_and_populates_actual_track(monkeypatch) -> None:
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: _bandcamp_result())
    row = _row(**{"Social Link": BANDCAMP_URL})
    amrap.enrich_amrap_release_cascade([row])
    assert (row["Song Title"], row["Release Date"]) == ("Actual Song", "2026-08-31")


def test_bandcamp_ep_title_without_track_is_rejected_and_counted(monkeypatch) -> None:
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: _bandcamp_result(track_title=""))
    logs: List[str] = []
    row = _row(**{"Social Link": BANDCAMP_URL})
    amrap.enrich_amrap_release_cascade([row], logger=logs.append)
    assert (row["Song Title"], row["Release Date"]) == ("", "")
    assert any("track_semantic_rejections=1" in message for message in logs)


def test_bandcamp_identity_failure_retains_amrap_row(monkeypatch) -> None:
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: _bandcamp_result(page_artist="Different Artist"))
    row = _row(**{"Social Link": BANDCAMP_URL})
    amrap.enrich_amrap_release_cascade([row])
    assert (row["Song Title"], row["Release Date"]) == ("", "")
    assert row["Source URL"] == AMRAP_URL


def test_bandcamp_challenge_path_uses_supplied_bounded_browser(monkeypatch) -> None:
    browser_calls: List[str] = []

    def fake_fetch(url, **kwargs):  # noqa: ANN001
        callback = kwargs["browser_fetcher"]
        assert callback(url) == "<html>recovered</html>"
        return _bandcamp_result(browser_used=True)

    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", fake_fetch)
    row = _row(**{"Social Link": BANDCAMP_URL})
    amrap.enrich_amrap_release_cascade(
        [row],
        browser_fetcher=lambda url: browser_calls.append(url) or "<html>recovered</html>",
    )
    assert browser_calls == [BANDCAMP_URL]
    assert row["Song Title"] == "Actual Song"


def test_official_website_to_spotify_is_one_bounded_hop(monkeypatch) -> None:
    fetches: List[str] = []
    website = "https://safeartist.example/"
    html = f'<a href="{SPOTIFY_URL}">Spotify</a><a href="https://second.example/">More</a>'
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", lambda _session, url: fetches.append(url) or _website_result(url, html))
    client = FakeSpotifyClient()
    row = _row(**{"Social Link": website})
    amrap.enrich_amrap_release_cascade([row], spotify_client=client, website_session=object())
    assert fetches == [website]
    assert (row["Song Title"], row["Release Date"]) == ("Spotify Track", "2026-09-01")


def test_linktree_to_spotify_uses_same_strict_identity_path(monkeypatch) -> None:
    hub = "https://linktr.ee/safeartist"
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", lambda _session, url: _website_result(url, f'<a href="{SPOTIFY_URL}">Spotify</a>'))
    row = _row(**{"Social Link": hub})
    amrap.enrich_amrap_release_cascade([row], spotify_client=FakeSpotifyClient(), website_session=object())
    assert (row["Song Title"], row["Release Date"]) == ("Spotify Track", "2026-09-01")


def test_owned_page_rejects_non_artist_spotify_without_lookup(monkeypatch) -> None:
    website = "https://safeartist.example/"
    malformed = f"https://open.spotify.com/track/{ARTIST_ID}"
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", lambda _session, url: _website_result(url, f'<a href="{malformed}">Track</a>'))
    client = FakeSpotifyClient()
    row = _row(**{"Social Link": website})
    amrap.enrich_amrap_release_cascade([row], spotify_client=client, website_session=object())
    assert client.release_calls == 0
    assert (row["Song Title"], row["Release Date"]) == ("", "")


def test_owned_page_to_bandcamp_uses_track_semantics(monkeypatch) -> None:
    website = "https://safeartist.example/"
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", lambda _session, url: _website_result(url, f'<a href="{BANDCAMP_URL}">Bandcamp</a>'))
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: _bandcamp_result())
    row = _row(**{"Social Link": website})
    amrap.enrich_amrap_release_cascade([row], website_session=object())
    assert (row["Song Title"], row["Release Date"]) == ("Actual Song", "2026-08-31")


def test_partial_native_pair_is_never_mixed() -> None:
    client = FakeSpotifyClient()
    row = _row(**{"Song Title": "Native Only", "Social Link": f"{SPOTIFY_URL}; {BANDCAMP_URL}"})
    amrap.enrich_amrap_release_cascade([row], spotify_client=client)
    assert (row["Song Title"], row["Release Date"]) == ("Native Only", "")
    assert client.release_calls == 0


def test_success_preserves_provenance_and_legacy_csv_alignment(monkeypatch, tmp_path: Path) -> None:
    row = _row(**{"Social Link": BANDCAMP_URL})
    monkeypatch.setattr(amrap, "fetch_bandcamp_profile", lambda *args, **kwargs: _bandcamp_result())
    amrap.enrich_amrap_release_cascade([row])
    monkeypatch.setattr(amrap, "scrape_amrap", lambda **kwargs: [row])
    output = tmp_path / "legacy.csv"
    amrap.scrape_amrap_to_csv(1, output.as_posix())
    with output.open(encoding="utf-8-sig", newline="") as handle:
        emitted = next(csv.DictReader(handle))
    for field in (
        "Artist Name", "Song Title", "Release Date", "Social Link", "Date Added",
        "Lead_Source", "Source_Directory", "Source Directory", "Source URL", "Source_URL",
    ):
        assert emitted[field] == row[field]
    assert emitted["Lead_Source"] == "AMRAP"
    assert emitted["Source_Directory"] == "amrap"
    assert emitted["Source URL"] == AMRAP_URL
