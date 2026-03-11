import logging
import shutil

import pandas as pd

import cross_directory_enricher
import final_checker
import night_mode_runner
import origin_validator
import pipeline_runner
import spotify_scraper


class _FakeSpotifyClient:
    API_BASE = "https://api.spotify.com/v1"

    def __init__(self, client_id=None, client_secret=None, logger=None):
        self.logger = logger

    def get_playlist_metadata(self, playlist_id):
        return {"id": playlist_id, "name": "Fresh Finds Test"}

    def get_playlist_tracks(self, playlist_id, limit=100, max_items=500):
        return [
            {
                "track": {
                    "id": "track-1",
                    "name": "Song One",
                    "artists": [
                        {
                            "id": "artist-1",
                            "name": "Artist One",
                            "external_urls": {"spotify": "https://open.spotify.com/artist/artist-1"},
                        }
                    ],
                }
            }
        ]

    def get_artists_details(self, artist_ids):
        assert artist_ids == ["artist-1"]
        return {
            "artist-1": {
                "id": "artist-1",
                "name": "Artist One",
                "external_urls": {"spotify": "https://open.spotify.com/artist/artist-1"},
                "genres": ["indie pop", "dream pop"],
                "followers": {"total": 4321},
                "popularity": 57,
            }
        }


class _MissingMetadataSpotifyClient(_FakeSpotifyClient):
    def get_artists_details(self, artist_ids):
        assert artist_ids == ["artist-1"]
        return {
            "artist-1": {
                "id": "artist-1",
                "name": "Artist One",
                "external_urls": {"spotify": "https://open.spotify.com/artist/artist-1"},
            }
        }


def _identity_rows(rows, **kwargs):
    return rows


def _scrape_rows(monkeypatch, client_cls, search_term="https://open.spotify.com/playlist/pl123?si=test"):
    monkeypatch.setattr(spotify_scraper, "SpotifyClient", client_cls)
    monkeypatch.setattr(spotify_scraper, "enrich_spotify_rows_with_about_links", _identity_rows)
    monkeypatch.setattr(spotify_scraper, "enrich_rows_with_website_emails", _identity_rows)
    return spotify_scraper.scrape_spotify(
        1,
        {
            "search_term": search_term,
            "spotify_client_id": "client-id",
            "spotify_client_secret": "client-secret",
            "directory_base": ".",
        },
        logger=None,
    )


def test_scrape_spotify_preserves_playlist_seed_metadata(monkeypatch):
    rows = _scrape_rows(monkeypatch, _FakeSpotifyClient)

    assert len(rows) == 1
    row = rows[0]
    assert row["Spotify Playlist"] == "Fresh Finds Test"
    assert row["Spotify_Playlist_URL"] == "https://open.spotify.com/playlist/pl123"
    assert row["Spotify_Seed_Position"] == "1"
    assert row["Spotify_Seed_Type"] == "playlist"
    assert row["Spotify_Seed_Query"] == "https://open.spotify.com/playlist/pl123?si=test"
    assert row["Spotify_URL"] == "https://open.spotify.com/artist/artist-1"
    assert row["Spotify_Artist_ID"] == "artist-1"


def test_scrape_spotify_preserves_artist_context(monkeypatch):
    rows = _scrape_rows(monkeypatch, _FakeSpotifyClient)

    assert len(rows) == 1
    row = rows[0]
    assert row["Spotify_Genres"] == "indie pop, dream pop"
    assert row["Spotify_Followers"] == "4321"
    assert row["Spotify_Popularity"] == "57"
    assert row["Primary Genre"] == "indie pop, dream pop"


def test_scrape_spotify_leaves_optional_context_blank_when_missing(monkeypatch):
    monkeypatch.setattr(spotify_scraper, "_fetch_additional_genres", lambda **kwargs: "")
    monkeypatch.setattr(spotify_scraper, "_fetch_genres_from_top_tracks", lambda **kwargs: "")
    monkeypatch.setattr(spotify_scraper, "_fetch_genres_from_lastfm", lambda **kwargs: "")

    rows = _scrape_rows(monkeypatch, _MissingMetadataSpotifyClient)

    assert len(rows) == 1
    row = rows[0]
    assert row["Spotify_Genres"] == ""
    assert row["Spotify_Followers"] == ""
    assert row["Spotify_Popularity"] == ""
    assert row["Spotify_Playlist_URL"] == "https://open.spotify.com/playlist/pl123"
    assert row["Spotify_Seed_Position"] == "1"


def test_spotify_seed_context_survives_raw_master_and_enrichment(monkeypatch, tmp_path):
    rows = _scrape_rows(monkeypatch, _FakeSpotifyClient)

    raw_csv = tmp_path / "job_spotify_1" / "raw.csv"
    pipeline_runner._write_rows_to_csv(rows, raw_csv.as_posix(), source_directory="spotify")

    logger = logging.getLogger("spotify-seed-context-test")
    master_raw = night_mode_runner._merge_raw_master(
        tmp_path.as_posix(),
        [{"job_id": "job_spotify_1", "raw_csv": raw_csv.as_posix()}],
        logger,
    )

    def fake_run_cross_directory_enrichment(seed_csv_path, output_csv_path, **kwargs):
        df = pd.read_csv(seed_csv_path, dtype=str, keep_default_na=False).fillna("")
        df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")

    def fake_run_auto_validate(csv_path, output_path=None, **kwargs):
        target = output_path or csv_path
        shutil.copyfile(csv_path, target)
        return target

    monkeypatch.setattr(cross_directory_enricher, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)
    monkeypatch.setattr(origin_validator, "run_auto_validate", fake_run_auto_validate)
    monkeypatch.setattr(final_checker, "run_final_checker", lambda path: path)

    master_enriched = tmp_path / "master_enriched.csv"
    pipeline_runner.run_master_enrichment(
        master_raw,
        master_enriched.as_posix(),
        logger=None,
        enable_live_search=False,
        night_mode=True,
    )

    final_csv = tmp_path / "master_final.csv"
    pipeline_runner.run_enrichment(
        master_enriched.as_posix(),
        final_csv.as_posix(),
        logger=None,
        night_mode=True,
    )

    master_raw_df = pd.read_csv(master_raw, dtype=str, keep_default_na=False).fillna("")
    master_enriched_df = pd.read_csv(master_enriched, dtype=str, keep_default_na=False).fillna("")
    final_df = pd.read_csv(final_csv, dtype=str, keep_default_na=False).fillna("")

    for df in (master_raw_df, master_enriched_df, final_df):
        row = df.iloc[0].to_dict()
        assert row["Spotify_URL"] == "https://open.spotify.com/artist/artist-1"
        assert row["Spotify_Artist_ID"] == "artist-1"
        assert row["Spotify_Playlist_URL"] == "https://open.spotify.com/playlist/pl123"
        assert row["Spotify_Seed_Position"] == "1"
        assert row["Spotify_Genres"] == "indie pop, dream pop"
        assert row["Spotify_Followers"] == "4321"
        assert row["Spotify_Popularity"] == "57"
        assert row["Spotify_Seed_Type"] == "playlist"
        assert row["Spotify_Seed_Query"] == "https://open.spotify.com/playlist/pl123?si=test"
