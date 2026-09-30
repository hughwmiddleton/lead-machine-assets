import csv
from pathlib import Path

import pytest

import pipeline_runner


PLAYLIST_ID = "37i9dQZF1DX7AqyNZFu97s"


class _CapturingSpotifyModule:
    def __init__(self):
        self.calls = []

    def scrape_spotify(self, target_count, params, logger=None):
        self.calls.append((target_count, params))
        return []


def _run_spotify_job(monkeypatch, tmp_path: Path, job_config):
    module = _CapturingSpotifyModule()
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: module)
    output_path = tmp_path / "raw.csv"
    pipeline_runner.run_directory_job(
        {"directory": "spotify", "job_id": "job_spotify_1", **job_config},
        output_path.as_posix(),
    )
    assert output_path.exists()
    assert len(module.calls) == 1
    return module.calls[0]


@pytest.mark.parametrize(
    "playlist_input",
    [
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=69614537e2ef4c60",
        f"spotify:playlist:{PLAYLIST_ID}",
    ],
)
def test_spotify_playlist_inputs_reach_playlist_ids_only(monkeypatch, tmp_path, playlist_input):
    target_count, params = _run_spotify_job(
        monkeypatch,
        tmp_path,
        {"input_seed_csv": playlist_input, "target_valid_leads": 10},
    )

    assert target_count == 10
    assert params["playlist_ids"] == [PLAYLIST_ID]
    assert params["search_term"] == ""


def test_spotify_explicit_playlist_ids_remain_authoritative(monkeypatch, tmp_path):
    explicit_ids = ["explicitPlaylist123456"]
    target_count, params = _run_spotify_job(
        monkeypatch,
        tmp_path,
        {
            "playlist_ids": explicit_ids,
            "input_seed_csv": f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=ignored",
            "target_valid_leads": 5,
        },
    )

    assert target_count == 5
    assert params["playlist_ids"] is explicit_ids
    assert params["search_term"] == ""


def test_spotify_free_text_input_remains_search_term(monkeypatch, tmp_path):
    target_count, params = _run_spotify_job(
        monkeypatch,
        tmp_path,
        {"input_seed_csv": "melbourne indie pop", "target_valid_leads": 7},
    )

    assert target_count == 7
    assert params["playlist_ids"] is None
    assert params["search_term"] == "melbourne indie pop"


def test_non_spotify_directory_dispatch_is_unchanged(monkeypatch, tmp_path):
    calls = []

    class _SoundCloudModule:
        def scrape_soundcloud(self, url, **kwargs):
            calls.append((url, kwargs))
            with open(kwargs["existing_csv"], "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Artist Name", "Email"])
                writer.writerow(["Control Artist", "control@example.com"])

    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: _SoundCloudModule())
    output_path = tmp_path / "soundcloud.csv"
    pipeline_runner.run_directory_job(
        {
            "directory": "soundcloud",
            "job_id": "job_soundcloud_control",
            "input_seed_csv": "https://soundcloud.com/control",
            "target_valid_leads": 3,
        },
        output_path.as_posix(),
    )

    assert calls[0][0] == "https://soundcloud.com/control"
    assert calls[0][1]["max_artists"] == 3
    assert output_path.exists()


def test_spotify_night_mode_field_describes_supported_inputs():
    gui_source = (Path(__file__).parents[1] / "Lead Machine (Final Update 5).py").read_text(encoding="utf-8")

    assert 'self.input_label.setText("Playlist URL/URI or search term:")' in gui_source
    assert 'self.input_edit.setPlaceholderText("e.g. https://open.spotify.com/playlist/...")' in gui_source
