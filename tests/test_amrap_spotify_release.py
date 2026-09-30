"""Deterministic AMRAP integration tests for shared Spotify release fallback."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

import amrap_scraper as amrap
import spotify_latest_release as slr


ARTIST_ID = "0123456789ABCDEFGHIJKL"
ARTIST_URL = f"https://open.spotify.com/artist/{ARTIST_ID}"
AMRAP_URL = "https://amrap.org.au/artist/safe-artist"


def _row(**updates: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "Artist Name": "Safe Artist",
        "Location": "QLD, Brisbane",
        "Song Title": "",
        "Release Date": "",
        "Social Link": ARTIST_URL,
        "Date Added": "2026-09-02",
        "Lead_Source": "AMRAP",
        "Source_Directory": "amrap",
        "Source Directory": "AMRAP",
        "Source URL": AMRAP_URL,
        "Source_URL": AMRAP_URL,
    }
    row.update(updates)
    return row


class FakeSpotifyClient:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.release_calls = 0

    def get_artist_releases(self, artist_id: str) -> List[Dict[str, Any]]:
        assert artist_id == ARTIST_ID
        self.release_calls += 1
        if self.fail:
            raise RuntimeError("Spotify unavailable")
        return [
            {
                "id": "release-1",
                "name": "Latest Single",
                "album_type": "single",
                "album_group": "single",
                "release_date": "2026-08-30",
                "release_date_precision": "day",
                "artists": [{"id": ARTIST_ID}],
            }
        ]

    def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        assert album_id == "release-1"
        return [
            {
                "id": "track-1",
                "name": "Spotify Song",
                "artists": [{"id": ARTIST_ID}],
                "disc_number": 1,
                "track_number": 1,
            }
        ]


def test_native_amrap_release_wins_without_spotify_lookup() -> None:
    client = FakeSpotifyClient()
    row = _row(**{"Song Title": "Native Song", "Release Date": "2026-08-31"})
    amrap.enrich_amrap_spotify_releases([row], spotify_client=client)
    assert (row["Song Title"], row["Release Date"]) == ("Native Song", "2026-08-31")
    assert client.release_calls == 0


def test_missing_release_with_direct_artist_url_uses_shared_enricher(monkeypatch) -> None:
    client = FakeSpotifyClient()
    calls: List[str] = []
    original_lookup = slr.SpotifyLatestReleaseEnricher.lookup

    def _lookup(self, artist_id: str):
        calls.append(artist_id)
        return original_lookup(self, artist_id)

    monkeypatch.setattr(slr.SpotifyLatestReleaseEnricher, "lookup", _lookup)
    row = _row(
        **{
            "Social Link": (
                "https://safeartist.example; "
                f"{ARTIST_URL}?si=amrap; "
                "https://instagram.com/safeartist"
            )
        }
    )
    amrap.enrich_amrap_spotify_releases([row], spotify_client=client)
    assert calls == [ARTIST_ID]
    assert client.release_calls == 1
    assert (row["Song Title"], row["Release Date"]) == ("Spotify Song", "2026-08-30")


def test_missing_or_non_artist_spotify_identity_makes_no_lookup() -> None:
    client = FakeSpotifyClient()
    rows = [
        _row(**{"Social Link": "https://instagram.com/safeartist"}),
        _row(**{"Social Link": f"https://open.spotify.com/track/{ARTIST_ID}"}),
        _row(**{"Social Link": f"https://open.spotify.com/album/{ARTIST_ID}"}),
        _row(**{"Social Link": f"https://open.spotify.com/playlist/{ARTIST_ID}"}),
    ]
    amrap.enrich_amrap_spotify_releases(rows, spotify_client=client)
    assert client.release_calls == 0
    assert all((row["Song Title"], row["Release Date"]) == ("", "") for row in rows)


def test_malformed_artist_identity_fails_closed_and_is_logged() -> None:
    logs: List[str] = []
    client = FakeSpotifyClient()
    row = _row(**{"Social Link": "https://open.spotify.com/artist/too-short"})
    amrap.enrich_amrap_spotify_releases([row], spotify_client=client, logger=logs.append)
    assert client.release_calls == 0
    assert (row["Song Title"], row["Release Date"]) == ("", "")
    assert any("malformed_identities=1" in message for message in logs)


def test_lookup_failure_retains_row_and_amrap_provenance() -> None:
    row = _row()
    amrap.enrich_amrap_spotify_releases([row], spotify_client=FakeSpotifyClient(fail=True))
    assert (row["Song Title"], row["Release Date"]) == ("", "")
    assert row["Lead_Source"] == "AMRAP"
    assert row["Source_Directory"] == "amrap"
    assert row["Source Directory"] == "AMRAP"
    assert row["Source URL"] == AMRAP_URL
    assert row["Source_URL"] == AMRAP_URL


def test_partial_native_release_is_not_mixed_with_spotify_pair() -> None:
    client = FakeSpotifyClient()
    row = _row(**{"Song Title": "Native Song", "Release Date": ""})
    amrap.enrich_amrap_spotify_releases([row], spotify_client=client)
    assert (row["Song Title"], row["Release Date"]) == ("Native Song", "")
    assert client.release_calls == 0


def test_successful_fallback_preserves_provenance_and_csv_alignment(tmp_path: Path) -> None:
    row = _row()
    amrap.enrich_amrap_spotify_releases([row], spotify_client=FakeSpotifyClient())
    output = tmp_path / "amrap.csv"
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=amrap.AMRAP_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(row)

    with output.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        emitted = next(reader)
        assert reader.fieldnames == amrap.AMRAP_CSV_FIELDS

    for field in (
        "Song Title",
        "Release Date",
        "Social Link",
        "Date Added",
        "Lead_Source",
        "Source_Directory",
        "Source Directory",
        "Source URL",
        "Source_URL",
    ):
        assert emitted[field] == row[field]
    assert emitted["Lead_Source"] == "AMRAP"
    assert emitted["Source_Directory"] == "amrap"
    assert emitted["Source Directory"] == "AMRAP"
    assert emitted["Source URL"] == AMRAP_URL
    assert emitted["Source_URL"] == AMRAP_URL
