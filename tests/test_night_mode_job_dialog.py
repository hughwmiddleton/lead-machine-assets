import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy_nmd", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
        app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def module(qapp):
    return _load_legacy_module()


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

def test_soundcloud_exposes_only_people_mode(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("soundcloud")
    modes = [dialog.mode_combo.itemText(i) for i in range(dialog.mode_combo.count())]
    assert modes == ["people"]
    dialog.close()


def test_soundcloud_people_mode_sets_label_and_placeholder(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("soundcloud")
    assert dialog.input_label.text() == "People search URL:"
    assert "search/people" in dialog.input_edit.placeholderText().lower()
    assert "soundcloud.com/search/people" in dialog.input_hint.text()
    dialog.close()


def test_soundcloud_people_mode_accepts_valid_url(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("soundcloud")
    dialog.input_edit.setText("https://soundcloud.com/search/people?q=indie&filter.place=berlin")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_not_called()
    dialog.close()


def test_soundcloud_people_mode_rejects_plain_text(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("soundcloud")
    dialog.input_edit.setText("berlin")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_called_once()
    dialog.close()


def test_soundcloud_people_mode_rejects_profile_url(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("soundcloud")
    dialog.input_edit.setText("https://soundcloud.com/someartist")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_called_once()
    dialog.close()


def test_soundcloud_job_serialization(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("soundcloud")
    dialog.input_edit.setText("https://soundcloud.com/search/people?q=indie&filter.place=berlin")
    dialog.target_spin.setValue(50)
    job = dialog.get_job()
    assert job["directory"] == "soundcloud"
    assert job["mode"] == "people"
    assert job["input_seed_csv"] == "https://soundcloud.com/search/people?q=indie&filter.place=berlin"
    assert job["target_valid_leads"] == 50
    dialog.close()


# ---------------------------------------------------------------------------
# Bandcamp
# ---------------------------------------------------------------------------

def test_bandcamp_exposes_only_valid_modes(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    modes = [dialog.mode_combo.itemText(i) for i in range(dialog.mode_combo.count())]
    assert modes == ["discover", "tag", "search"]
    assert "people" not in modes
    assert "tracks" not in modes
    assert "playlist" not in modes
    dialog.close()


def test_bandcamp_discover_accepts_valid_url(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("discover")
    dialog.input_edit.setText("https://bandcamp.com/discover/manchester+rock?s=new")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_not_called()
    dialog.close()


def test_bandcamp_discover_rejects_unrelated_text(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("discover")
    dialog.input_edit.setText("indie rock")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_called_once()
    dialog.close()


def test_bandcamp_tag_accepts_slug(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("tag")
    dialog.input_edit.setText("indie-rock")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_not_called()
    dialog.close()


def test_bandcamp_tag_accepts_tag_url(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("tag")
    dialog.input_edit.setText("https://bandcamp.com/tag/indie-rock")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_not_called()
    dialog.close()


def test_bandcamp_tag_rejects_invalid_url(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("tag")
    dialog.input_edit.setText("https://google.com")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_called_once()
    dialog.close()


def test_bandcamp_search_accepts_plain_keywords(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("search")
    dialog.input_edit.setText("indie rock london")
    with mock.patch.object(QtWidgets.QMessageBox, "warning") as mock_warn:
        dialog.accept()
    mock_warn.assert_not_called()
    dialog.close()


def test_bandcamp_search_exposes_controls(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.show()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("search")
    assert dialog.bandcamp_search_widget.isVisible()
    dialog.close()


def test_bandcamp_search_serializes_controls(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("search")
    dialog.input_edit.setText("indie rock")
    dialog.search_location_edit.setText("London")
    idx = dialog.search_domain_combo.findText("artists")
    dialog.search_domain_combo.setCurrentIndex(idx)
    job = dialog.get_job()
    assert job["input_seed_csv"] == "indie rock"
    assert job["search_location"] == "London"
    assert job["search_domain"] == "artists"
    dialog.close()


def test_bandcamp_search_hides_controls_when_switching_mode(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.show()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("search")
    assert dialog.bandcamp_search_widget.isVisible()
    dialog.mode_combo.setCurrentText("discover")
    assert not dialog.bandcamp_search_widget.isVisible()
    dialog.close()


def test_bandcamp_search_preserves_config_when_switching_away(module, qapp):
    dialog = module.NightModeJobDialog()
    dialog.directory_combo.setCurrentText("bandcamp")
    dialog.mode_combo.setCurrentText("search")
    dialog.search_location_edit.setText("London")
    dialog.search_domain_combo.setCurrentText("tracks")
    dialog.mode_combo.setCurrentText("discover")
    # get_job should still preserve the old search values from self.job
    dialog.job = {"search_location": "London", "search_domain": "tracks"}
    job = dialog.get_job()
    assert job.get("search_location") == "London"
    assert job.get("search_domain") == "tracks"
    dialog.close()


# ---------------------------------------------------------------------------
# Regression — Spotify / Unearthed
# ---------------------------------------------------------------------------

def test_spotify_job_loads_and_saves(module, qapp):
    dialog = module.NightModeJobDialog(
        job={
            "directory": "spotify",
            "mode": "playlist",
            "input_seed_csv": "https://open.spotify.com/playlist/abc",
            "target_valid_leads": 200,
        }
    )
    assert dialog.directory_combo.currentText() == "spotify"
    assert dialog.mode_combo.currentText() == "playlist"
    assert dialog.input_edit.text() == "https://open.spotify.com/playlist/abc"
    job = dialog.get_job()
    assert job["directory"] == "spotify"
    assert job["mode"] == "playlist"
    assert job["input_seed_csv"] == "https://open.spotify.com/playlist/abc"
    dialog.close()


def test_unearthed_job_loads_and_saves(module, qapp):
    dialog = module.NightModeJobDialog(
        job={
            "directory": "unearthed",
            "mode": "discover",
            "input_seed_csv": "https://www.abc.net.au/triplejunearthed/music/",
            "target_valid_leads": 150,
        }
    )
    assert dialog.directory_combo.currentText() == "unearthed"
    assert dialog.mode_combo.currentText() == "discover"
    job = dialog.get_job()
    assert job["directory"] == "unearthed"
    assert job["mode"] == "discover"
    dialog.close()


def test_legacy_blank_mode_loads_for_spotify(module, qapp):
    dialog = module.NightModeJobDialog(
        job={
            "directory": "spotify",
            "mode": "",
            "input_seed_csv": "",
        }
    )
    assert dialog.directory_combo.currentText() == "spotify"
    assert dialog.mode_combo.currentText() == ""
    job = dialog.get_job()
    assert job["mode"] == ""
    dialog.close()


def test_legacy_invalid_mode_gracefully_loaded_for_bandcamp(module, qapp):
    dialog = module.NightModeJobDialog(
        job={
            "directory": "bandcamp",
            "mode": "people",
            "input_seed_csv": "some_value",
        }
    )
    assert dialog.directory_combo.currentText() == "bandcamp"
    # Legacy mode "people" is not in DIRECTORY_MODES for bandcamp, but should
    # have been added temporarily so the dialog doesn't crash.
    assert dialog.mode_combo.currentText() == "people"
    job = dialog.get_job()
    assert job["mode"] == "people"
    dialog.close()
