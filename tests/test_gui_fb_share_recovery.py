import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets

import night_mode_runner


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy_fb_share_recovery", path)
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


def _capture_launch_command(module, tab, monkeypatch):
    captured = {}

    class FakeNightModeWorker:
        def __init__(self, command, workdir, env=None, secrets=None, parent=None):
            captured["command"] = command
            self.log_signal = SimpleNamespace(connect=lambda fn: None)
            self.finished_signal = SimpleNamespace(connect=lambda fn: None)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(module, "NightModeWorker", FakeNightModeWorker)
    tab.jobs = [{"directory": "spotify", "mode": "playlist", "input": "https://open.spotify.com/playlist/test"}]
    tab._launch_night_mode(headless=True)
    return captured["command"]


def test_fb_share_recovery_toggle_off_passes_no_flags(qapp, monkeypatch):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    tab.fb_share_recovery_checkbox.setChecked(False)

    cmd = _capture_launch_command(module, tab, monkeypatch)

    assert "--enable-fb-share-recovery" not in cmd
    assert "--fb-share-recovery-batch-size" not in cmd
    assert "--fb-share-recovery-in-place" not in cmd
    tab.shutdown()


def test_fb_share_recovery_default_copy_flag_mapping(qapp, monkeypatch):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    tab.fb_share_recovery_checkbox.setChecked(True)
    tab.fb_share_recovery_batch_spin.setValue(40)
    tab.fb_share_recovery_copy_radio.setChecked(True)

    cmd = _capture_launch_command(module, tab, monkeypatch)

    assert "--enable-fb-share-recovery" in cmd
    assert cmd[cmd.index("--fb-share-recovery-batch-size") + 1] == "40"
    assert "--fb-share-recovery-in-place" not in cmd
    tab.shutdown()


def test_fb_share_recovery_in_place_flag_only_when_selected(qapp, monkeypatch):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    tab.fb_share_recovery_checkbox.setChecked(True)
    tab.fb_share_recovery_in_place_radio.setChecked(True)

    cmd = _capture_launch_command(module, tab, monkeypatch)

    assert "--enable-fb-share-recovery" in cmd
    assert "--fb-share-recovery-in-place" in cmd
    tab.shutdown()


def test_fb_share_recovery_invalid_batch_size_falls_back_to_40():
    module = _load_legacy_module()

    assert module._coerce_fb_share_recovery_batch_size("not-an-int") == 40
    assert module._coerce_fb_share_recovery_batch_size(0) == 40
    assert module._coerce_fb_share_recovery_batch_size(-5) == 40
    assert module._coerce_fb_share_recovery_batch_size(12) == 12


def test_fb_share_recovery_checkbox_off_overrides_saved_config(qapp, tmp_path, monkeypatch):
    module = _load_legacy_module()
    config_path = tmp_path / "overnight_jobs.json"
    config_path.write_text(
        json.dumps(
            {
                "export_mode": "both",
                "jobs": [{"directory": "spotify", "mode": "playlist", "input": "playlist"}],
                "fb_share_recovery": {"enabled": True, "batch_size": 7, "output_mode": "in_place"},
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeNightModeWorker:
        def __init__(self, command, workdir, env=None, secrets=None, parent=None):
            captured["command"] = command
            self.log_signal = SimpleNamespace(connect=lambda fn: None)
            self.finished_signal = SimpleNamespace(connect=lambda fn: None)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(module, "NightModeWorker", FakeNightModeWorker)
    tab = module.NightModeTab()
    tab.jobs = []
    tab.config_path_edit.setText(str(config_path))
    tab.fb_share_recovery_checkbox.setChecked(False)

    tab._launch_night_mode(headless=True)

    cmd = captured["command"]
    assert "--enable-fb-share-recovery" not in cmd
    prepared_config = json.loads(Path(cmd[cmd.index("--config") + 1]).read_text(encoding="utf-8"))
    assert prepared_config["fb_share_recovery"]["enabled"] is False
    assert prepared_config["fb_share_recovery"]["output_mode"] == "copy"
    tab.shutdown()


def test_fb_share_recovery_runner_logs_script_summary_exactly(tmp_path, monkeypatch, caplog):
    export = tmp_path / "master_export_leads.csv"
    export.write_text("Artist Name,FB_Status\nA,fb_share_resolution_failed\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "[FB Share Recovery] candidates=2 resolved=1 enriched=1 failed=1 skipped_existing=3 duration_sec=4\n"
                "candidates=2\n"
                "resolved=1\n"
                "enriched=1\n"
                "failed=1\n"
                "skipped_existing=3\n"
                f"output={tmp_path / 'master_export_leads__fb_share_recovered.csv'}\n"
            ),
        )

    monkeypatch.setattr(night_mode_runner.subprocess, "run", fake_run)
    logger = night_mode_runner.logging.getLogger("test_fb_share_recovery")

    with caplog.at_level(night_mode_runner.logging.INFO, logger="test_fb_share_recovery"):
        output = night_mode_runner._run_fb_share_recovery_after_export(
            str(export),
            batch_size="bad",
            in_place=False,
            logger=logger,
        )

    assert calls[0][calls[0].index("--limit") + 1] == "40"
    assert "--in-place" not in calls[0]
    text = caplog.text
    assert "[FB Share Recovery] Starting post-run /share recovery" in text
    assert f"[FB Share Recovery] input={export}" in text
    assert "[FB Share Recovery] batch_size=40" in text
    assert "[FB Share Recovery] output_mode=copy" in text
    assert "[FB Share Recovery] candidates=2 resolved=1 enriched=1 failed=1 skipped_existing=3 duration_sec=4" in text
    assert output.endswith("master_export_leads__fb_share_recovered.csv")


def test_fb_share_recovery_runner_in_place_logs_explicit_mode(tmp_path, monkeypatch, caplog):
    export = tmp_path / "master_export_leads.csv"
    export.write_text("Artist Name,FB_Status\nA,fb_share_resolution_failed\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=f"candidates=0\noutput={export}\n")

    monkeypatch.setattr(night_mode_runner.subprocess, "run", fake_run)
    logger = night_mode_runner.logging.getLogger("test_fb_share_recovery_in_place")

    with caplog.at_level(night_mode_runner.logging.INFO, logger="test_fb_share_recovery_in_place"):
        output = night_mode_runner._run_fb_share_recovery_after_export(
            str(export),
            batch_size=10,
            in_place=True,
            logger=logger,
        )

    assert "--in-place" in calls[0]
    assert calls[0][calls[0].index("--limit") + 1] == "10"
    assert "[FB Share Recovery] Running in-place update" in caplog.text
    assert output == str(export)
