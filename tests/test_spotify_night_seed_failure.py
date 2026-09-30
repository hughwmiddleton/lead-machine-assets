import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import pipeline_runner
import spotify_scraper
from night_mode_v2.phased_runner import run_seed_phase
from spotify_client import SpotifyClient


FRESH_FINDS_CANADA_URL = (
    "https://open.spotify.com/playlist/37i9dQZF1DWVxStm5ni6tl?si=868025fc12ef4a7d"
)
FRESH_FINDS_CANADA_ID = "37i9dQZF1DWVxStm5ni6tl"


def _identity_rows(rows, **_kwargs):
    return rows


def test_extract_playlist_id_accepts_fresh_finds_url_uri_and_bare_id() -> None:
    assert spotify_scraper._extract_playlist_id(FRESH_FINDS_CANADA_URL) == FRESH_FINDS_CANADA_ID
    assert (
        spotify_scraper._extract_playlist_id(f"spotify:playlist:{FRESH_FINDS_CANADA_ID}")
        == FRESH_FINDS_CANADA_ID
    )
    assert spotify_scraper._extract_playlist_id(FRESH_FINDS_CANADA_ID) == FRESH_FINDS_CANADA_ID


def test_revoked_refresh_token_falls_back_to_client_credentials(monkeypatch) -> None:
    messages = []
    client = SpotifyClient(client_id="client", client_secret="secret", logger=messages.append)
    client.refresh_token = "revoked-refresh-token"
    calls = {"refresh": 0, "client": 0}

    def revoked_refresh():
        calls["refresh"] += 1
        raise RuntimeError("Spotify refresh-token request failed: 400 invalid_grant")

    def client_credentials():
        calls["client"] += 1
        return "application-token"

    monkeypatch.setattr(client, "_get_user_access_token_from_refresh", revoked_refresh)
    monkeypatch.setattr(client, "get_access_token", client_credentials)

    assert client._auth_headers()["Authorization"] == "Bearer application-token"
    assert client._auth_headers()["Authorization"] == "Bearer application-token"
    assert calls == {"refresh": 1, "client": 2}
    assert any("falling back to client credentials" in message for message in messages)


class _Api404SpotifyClient:
    API_BASE = "https://api.spotify.test/v1"

    def __init__(self, **_kwargs):
        pass

    def get_playlist_metadata(self, _playlist_id):
        raise RuntimeError("Spotify API error 404: Resource not found")

    def get_playlist_tracks(self, _playlist_id, limit=100, max_items=500):
        raise RuntimeError("Spotify API error 404: Resource not found")

    def get_artists_details(self, artist_ids):
        return {
            artist_id: {
                "id": artist_id,
                "name": f"Artist {index}",
                "external_urls": {"spotify": f"https://open.spotify.com/artist/{artist_id}"},
                "genres": ["indie pop"],
                "followers": {"total": 100 + index},
                "popularity": 40 + index,
            }
            for index, artist_id in enumerate(artist_ids, start=1)
        }


def _html_fixture(_playlist_id, **_kwargs):
    return [
        {
            "artist_name": "Artist One",
            "artist_url": "https://open.spotify.com/artist/artist-1",
            "artist_id": "artist-1",
            "track_name": "Track One",
            "playlist_name": "Fresh Finds Canada",
            "track_position": 1,
            "is_primary": True,
        },
        {
            "artist_name": "Artist Two",
            "artist_url": "https://open.spotify.com/artist/artist-2",
            "artist_id": "artist-2",
            "track_name": "Track Two",
            "playlist_name": "Fresh Finds Canada",
            "track_position": 2,
            "is_primary": True,
        },
    ]


def test_api_404_html_fallback_produces_canonical_spotify_seed_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(spotify_scraper, "SpotifyClient", _Api404SpotifyClient)
    monkeypatch.setattr(spotify_scraper, "scrape_playlist_artists_via_html", _html_fixture)
    monkeypatch.setattr(spotify_scraper, "enrich_spotify_rows_with_about_links", _identity_rows)
    monkeypatch.setattr(spotify_scraper, "enrich_rows_with_website_emails", _identity_rows)

    rows = spotify_scraper.scrape_spotify(2, {"search_term": FRESH_FINDS_CANADA_URL})
    raw_csv = tmp_path / "job_spotify_1" / "raw.csv"
    result = pipeline_runner._write_rows_to_csv(rows, raw_csv.as_posix(), source_directory="spotify")
    frame = pd.read_csv(raw_csv, dtype=str, keep_default_na=False)

    assert result.row_count == 2
    assert list(frame["Artist Name"]) == ["Artist 1", "Artist 2"]
    assert set(frame["Spotify Playlist"]) == {"Fresh Finds Canada"}
    assert set(frame["Spotify_Playlist_URL"]) == {
        f"https://open.spotify.com/playlist/{FRESH_FINDS_CANADA_ID}"
    }
    assert list(frame["Spotify_Seed_Position"]) == ["1", "2"]
    assert set(frame["Spotify_Seed_Type"]) == {"playlist"}
    assert set(frame["Spotify_Seed_Query"]) == {FRESH_FINDS_CANADA_URL}
    assert set(frame["Lead_Source"]) == {"spotify"}
    assert set(frame["Source_Directory"]) == {"spotify"}
    assert set(frame["Source Directory"]) == {"spotify"}


def test_fresh_finds_fixture_reaches_night_raw_csv_with_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(spotify_scraper, "SpotifyClient", _Api404SpotifyClient)
    monkeypatch.setattr(spotify_scraper, "scrape_playlist_artists_via_html", _html_fixture)
    monkeypatch.setattr(spotify_scraper, "enrich_spotify_rows_with_about_links", _identity_rows)
    monkeypatch.setattr(spotify_scraper, "enrich_rows_with_website_emails", _identity_rows)
    monkeypatch.setattr(
        pipeline_runner,
        "_load_legacy_module",
        lambda: SimpleNamespace(scrape_spotify=spotify_scraper.scrape_spotify),
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "job_id": "job_spotify_1",
                        "directory": "spotify",
                        "input_seed_csv": FRESH_FINDS_CANADA_URL,
                        "target_valid_leads": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    manifest = run_seed_phase(config_path.as_posix(), (tmp_path / "run").as_posix())
    raw_csv = tmp_path / "run" / "job_spotify_1" / "raw.csv"
    status = json.loads(
        (tmp_path / "run" / "job_spotify_1" / "job_status.json").read_text(encoding="utf-8")
    )
    frame = pd.read_csv(raw_csv, dtype=str, keep_default_na=False)

    assert status["status"] == "completed"
    assert status["row_count"] == 2
    assert status["error"] == ""
    assert len(frame.index) == 2
    assert set(frame["Spotify_Playlist_URL"]) == {
        f"https://open.spotify.com/playlist/{FRESH_FINDS_CANADA_ID}"
    }
    assert set(frame["Source_Directory"]) == {"spotify"}
    assert manifest["phases"]["seed"]["jobs"]["job_spotify_1"]["row_count"] == 2
    assert manifest["phases"]["seed"]["status"] == "completed"


class _EmptySpotifyClient(_Api404SpotifyClient):
    def get_playlist_metadata(self, _playlist_id):
        return {"id": FRESH_FINDS_CANADA_ID, "name": "Fresh Finds Canada"}

    def get_playlist_tracks(self, _playlist_id, limit=100, max_items=500):
        return []


def test_successful_empty_playlist_remains_a_legitimate_empty_result(monkeypatch) -> None:
    monkeypatch.setattr(spotify_scraper, "SpotifyClient", _EmptySpotifyClient)
    assert spotify_scraper.scrape_spotify(5, {"search_term": FRESH_FINDS_CANADA_URL}) == []


def test_failed_api_and_fallback_raise_operational_error(monkeypatch) -> None:
    monkeypatch.setattr(spotify_scraper, "SpotifyClient", _Api404SpotifyClient)
    monkeypatch.setattr(spotify_scraper, "scrape_playlist_artists_via_html", lambda *_args, **_kwargs: [])

    with pytest.raises(spotify_scraper.SpotifySeedOperationalError) as exc_info:
        spotify_scraper.scrape_spotify(5, {"search_term": FRESH_FINDS_CANADA_URL})

    assert "API fetch failed" in str(exc_info.value)
    assert "HTML fallback returned no artists" in str(exc_info.value)


def test_night_mode_surfaces_spotify_operational_failure(monkeypatch, tmp_path: Path) -> None:
    def failed_spotify_job(_job_config, _raw_output_path, logger=None):
        raise spotify_scraper.SpotifySeedOperationalError("Spotify API and HTML fallback failed")

    monkeypatch.setattr(pipeline_runner, "run_directory_job", failed_spotify_job)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "job_id": "job_spotify_1",
                        "directory": "spotify",
                        "input_seed_csv": FRESH_FINDS_CANADA_URL,
                        "target_valid_leads": 5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    manifest = run_seed_phase(config_path.as_posix(), (tmp_path / "run").as_posix())
    status = json.loads(
        (tmp_path / "run" / "job_spotify_1" / "job_status.json").read_text(encoding="utf-8")
    )
    manifest_job = manifest["phases"]["seed"]["jobs"]["job_spotify_1"]

    assert status["status"] == "failed"
    assert status["row_count"] == 0
    assert status["raw_exists"] is False
    assert "SpotifySeedOperationalError" in status["error"]
    assert manifest_job["status"] == "failed"
    assert manifest_job["error"] == status["error"]
    assert manifest["phases"]["seed"]["status"] == "failed"
