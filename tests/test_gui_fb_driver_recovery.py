import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy_fb_recovery", path)
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


def _write_export(path: Path, email: str = ""):
    path.write_text(
        "Artist Name,Facebook_URL,Email,FB_Status\n"
        f"Driver Artist,https://www.facebook.com/driverartist,{email},driver_error\n",
        encoding="utf-8",
    )


def test_fb_driver_recovery_summary_parsing_extracts_stats():
    module = _load_legacy_module()

    parsed = module._parse_fb_driver_recovery_summary(
        "noise\ncandidates_found=3\nretry_attempted=2\nretry_success=1\nfb_email_found=1\noutput=/tmp/out.csv\n"
    )

    assert parsed["candidates_found"] == "3"
    assert parsed["retry_attempted"] == "2"
    assert parsed["retry_success"] == "1"
    assert parsed["fb_email_found"] == "1"


def test_fb_driver_recovery_batch_chaining_and_default_preserves_original(tmp_path):
    module = _load_legacy_module()
    script = tmp_path / "scripts" / "recover_fb_driver_errors.py"
    script.parent.mkdir()
    script.write_text("# fixture\n", encoding="utf-8")
    export = tmp_path / "master_export_leads.csv"
    _write_export(export)
    original = export.read_text(encoding="utf-8")
    calls = []
    summaries = [
        "candidates_found=2\nretry_attempted=2\nretry_success=1\nfb_email_found=1\n",
        "candidates_found=0\nretry_attempted=0\nretry_success=0\nfb_email_found=0\n",
    ]

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        input_path = Path(cmd[cmd.index("--input") + 1])
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=summaries[len(calls) - 1])

    result = module._run_fb_driver_recovery_chain(
        str(export),
        batch_size=40,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert calls[0][calls[0].index("--input") + 1] == str(export)
    assert calls[1][calls[1].index("--input") + 1].endswith("master_export_leads.fb_driver_recovered_batch1.csv")
    assert Path(result["final_recovered_csv"]).name == "master_export_leads.recovered_final.csv"
    assert Path(result["final_recovered_csv"]).exists()
    assert (tmp_path / "master_export_leads.fb_driver_recovered_batch1.csv").exists()
    assert (tmp_path / "master_export_leads.fb_driver_recovered_batch2.csv").exists()
    assert export.read_text(encoding="utf-8") == original


def test_fb_driver_recovery_stops_when_retry_attempted_zero(tmp_path):
    module = _load_legacy_module()
    script = tmp_path / "scripts" / "recover_fb_driver_errors.py"
    script.parent.mkdir()
    script.write_text("# fixture\n", encoding="utf-8")
    export = tmp_path / "master_export_leads.csv"
    _write_export(export)
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        input_path = Path(cmd[cmd.index("--input") + 1])
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="candidates_found=4\nretry_attempted=0\nretry_success=0\nfb_email_found=0\n")

    result = module._run_fb_driver_recovery_chain(
        str(export),
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert len(calls) == 1
    assert result["stopped_reason"] == "retry_attempted=0"


def test_fb_driver_recovery_in_place_uses_temp_validation_before_replace(tmp_path, monkeypatch):
    module = _load_legacy_module()
    script = tmp_path / "scripts" / "recover_fb_driver_errors.py"
    script.parent.mkdir()
    script.write_text("# fixture\n", encoding="utf-8")
    export = tmp_path / "master_export_leads.csv"
    _write_export(export)
    events = []
    real_replace = os.replace
    real_validate = module._validate_recovered_csv_matches_original

    def fake_runner(cmd, **kwargs):
        output_path = Path(cmd[cmd.index("--output") + 1])
        _write_export(output_path, email="recovered@example.com")
        return SimpleNamespace(returncode=0, stdout="candidates_found=0\nretry_attempted=0\nretry_success=0\nfb_email_found=0\n")

    def fake_copy(source, dest):
        events.append(("copy", Path(dest).name))
        Path(dest).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")

    def fake_validate(original, recovered):
        events.append(("validate", Path(recovered).name))
        real_validate(original, recovered)

    def fake_replace(source, dest):
        events.append(("replace", Path(source).name, Path(dest).name))
        real_replace(source, dest)

    monkeypatch.setattr(module, "_copy_csv_atomic", fake_copy)
    monkeypatch.setattr(module, "_validate_recovered_csv_matches_original", fake_validate)
    monkeypatch.setattr(module.os, "replace", fake_replace)

    module._run_fb_driver_recovery_chain(
        str(export),
        in_place=True,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert events == [
        ("copy", "master_export_leads.recovered_temp.csv"),
        ("validate", "master_export_leads.recovered_temp.csv"),
        ("replace", "master_export_leads.recovered_temp.csv", "master_export_leads.csv"),
    ]
    assert "recovered@example.com" in export.read_text(encoding="utf-8")


def test_night_mode_toggle_off_does_not_start_recovery(qapp, monkeypatch):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    called = []
    monkeypatch.setattr(tab, "_start_fb_driver_recovery", lambda: called.append(True) or True)

    tab.fb_driver_recovery_checkbox.setChecked(False)
    tab._on_finished(0)

    assert called == []
    tab.shutdown()


def test_night_mode_toggle_on_starts_recovery_after_export_exists(qapp, tmp_path, monkeypatch):
    module = _load_legacy_module()
    run_dir = tmp_path / "2026-04-29_120000"
    run_dir.mkdir()
    export = run_dir / "master_export_leads.csv"
    _write_export(export)
    started = []

    class FakeRecoveryWorker:
        def __init__(self, export_csv, batch_size, in_place, parent=None):
            started.append((export_csv, batch_size, in_place))
            self.log_signal = SimpleNamespace(connect=lambda fn: None)
            self.finished_signal = SimpleNamespace(connect=lambda fn: None)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(module, "FbDriverRecoveryWorker", FakeRecoveryWorker)
    tab = module.NightModeTab()
    tab.run_root_edit.setText(str(tmp_path))
    tab.fb_driver_recovery_checkbox.setChecked(True)

    tab._on_finished(0)

    assert started == [(str(export), 40, False)]
    tab.shutdown()
