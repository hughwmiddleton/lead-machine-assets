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
    spec = importlib.util.spec_from_file_location("lead_machine_manual_fb_recovery", path)
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
        f"Manual Artist,https://www.facebook.com/manualartist,{email},driver_error\n",
        encoding="utf-8",
    )


def _write_scripts(base_dir: Path):
    scripts = base_dir / "scripts"
    scripts.mkdir()
    (scripts / "recover_fb_share_rows.py").write_text("# fixture\n", encoding="utf-8")
    (scripts / "recover_fb_driver_errors.py").write_text("# fixture\n", encoding="utf-8")


def test_manual_fb_recovery_no_csv_selected_validation(qapp, monkeypatch):
    module = _load_legacy_module()
    window = module.MainWindow()
    messages = []
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "warning", lambda *args: messages.append(args))

    window.manual_fb_recovery_csv_edit.clear()
    window.start_manual_fb_recovery(dry_run=True)

    assert "Select a CSV" in window.fb_log.toPlainText()
    assert messages
    window._shutdown_threads()


def test_manual_fb_recovery_dry_run_uses_temp_files_only(tmp_path):
    module = _load_legacy_module()
    _write_scripts(tmp_path)
    export = tmp_path / "manual.csv"
    _write_export(export)
    original = export.read_text(encoding="utf-8")
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text("temp output\n", encoding="utf-8")
        if "recover_fb_share_rows.py" in cmd[1]:
            return SimpleNamespace(returncode=0, stdout="candidates_found=1\nresolution_success=1\nfb_email_found=1\n")
        return SimpleNamespace(
            returncode=0,
            stdout="driver_error_rows=1\ncandidates_found=1\nretry_attempted=0\nretry_success=0\nfb_email_found=0\n",
        )

    result = module._run_manual_fb_recovery(
        str(export),
        dry_run=True,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert result["final_recovered_csv"] == "<dry-run>"
    assert export.read_text(encoding="utf-8") == original
    assert not (tmp_path / "manual.share_recovered.csv").exists()
    assert not list(tmp_path.glob("manual.driver_recovered_batch*.csv"))
    assert "--dry-run" in calls[-1]


def test_manual_fb_recovery_share_only_invokes_only_share(tmp_path):
    module = _load_legacy_module()
    _write_scripts(tmp_path)
    export = tmp_path / "manual.csv"
    _write_export(export)
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        input_path = Path(cmd[cmd.index("--input") + 1])
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="share_candidates_found=2\nshare_resolution_success=1\nshare_fb_email_found=1\n")

    result = module._run_manual_fb_recovery(
        str(export),
        recover_share=True,
        recover_driver=False,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert len(calls) == 1
    assert "recover_fb_share_rows.py" in calls[0][1]
    assert Path(result["final_recovered_csv"]).name == "manual.recovered_final.csv"
    assert export.exists()


def test_manual_fb_recovery_driver_only_invokes_only_driver(tmp_path):
    module = _load_legacy_module()
    _write_scripts(tmp_path)
    export = tmp_path / "manual.csv"
    _write_export(export)
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        input_path = Path(cmd[cmd.index("--input") + 1])
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="candidates_found=0\nretry_attempted=0\nretry_success=0\nfb_email_found=0\n")

    module._run_manual_fb_recovery(
        str(export),
        recover_share=False,
        recover_driver=True,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert len(calls) == 1
    assert "recover_fb_driver_errors.py" in calls[0][1]


def test_manual_fb_recovery_both_runs_share_then_driver_and_chains_batches(tmp_path):
    module = _load_legacy_module()
    _write_scripts(tmp_path)
    export = tmp_path / "manual.csv"
    _write_export(export)
    calls = []
    driver_summaries = [
        "driver_error_rows=2\ncandidates_found=2\nretry_attempted=2\nretry_success=1\nfb_email_found=1\n",
        "driver_error_rows=1\ncandidates_found=0\nretry_attempted=0\nretry_success=0\nfb_email_found=0\n",
    ]

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        input_path = Path(cmd[cmd.index("--input") + 1])
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
        if "recover_fb_share_rows.py" in cmd[1]:
            return SimpleNamespace(returncode=0, stdout="candidates_found=1\nresolution_success=1\nfb_email_found=1\n")
        return SimpleNamespace(returncode=0, stdout=driver_summaries[len(calls) - 2])

    result = module._run_manual_fb_recovery(
        str(export),
        batch_size=40,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert "recover_fb_share_rows.py" in calls[0][1]
    assert "recover_fb_driver_errors.py" in calls[1][1]
    assert calls[1][calls[1].index("--input") + 1].endswith("manual.share_recovered.csv")
    assert calls[2][calls[2].index("--input") + 1].endswith("manual.driver_recovered_batch1.csv")
    assert result["driver_retry_attempted"] == 2
    assert result["driver_fb_email_found"] == 1


def test_manual_fb_recovery_default_preserves_original(tmp_path):
    module = _load_legacy_module()
    _write_scripts(tmp_path)
    export = tmp_path / "manual.csv"
    _write_export(export)
    original = export.read_text(encoding="utf-8")

    def fake_runner(cmd, **kwargs):
        output_path = Path(cmd[cmd.index("--output") + 1])
        _write_export(output_path, email="recovered@example.com")
        return SimpleNamespace(returncode=0, stdout="candidates_found=0\nretry_attempted=0\n")

    result = module._run_manual_fb_recovery(
        str(export),
        recover_share=False,
        recover_driver=True,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert export.read_text(encoding="utf-8") == original
    assert "recovered@example.com" in Path(result["final_recovered_csv"]).read_text(encoding="utf-8")


def test_manual_fb_recovery_in_place_uses_atomic_replace(tmp_path, monkeypatch):
    module = _load_legacy_module()
    _write_scripts(tmp_path)
    export = tmp_path / "manual.csv"
    _write_export(export)
    events = []
    real_replace = os.replace

    def fake_runner(cmd, **kwargs):
        output_path = Path(cmd[cmd.index("--output") + 1])
        _write_export(output_path, email="recovered@example.com")
        return SimpleNamespace(returncode=0, stdout="candidates_found=0\nretry_attempted=0\n")

    def fake_copy(source, dest):
        events.append(("copy", Path(dest).name))
        Path(dest).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")

    def fake_validate(original, recovered):
        events.append(("validate", Path(recovered).name))

    def fake_replace(source, dest):
        events.append(("replace", Path(source).name, Path(dest).name))
        real_replace(source, dest)

    monkeypatch.setattr(module, "_copy_csv_atomic", fake_copy)
    monkeypatch.setattr(module, "_validate_recovered_csv_matches_original", fake_validate)
    monkeypatch.setattr(module.os, "replace", fake_replace)

    module._run_manual_fb_recovery(
        str(export),
        recover_share=False,
        recover_driver=True,
        in_place=True,
        runner=fake_runner,
        python_executable="python",
        base_dir=str(tmp_path),
    )

    assert events == [
        ("copy", "manual.recovered_temp.csv"),
        ("validate", "manual.recovered_temp.csv"),
        ("replace", "manual.recovered_temp.csv", "manual.csv"),
    ]
    assert "recovered@example.com" in export.read_text(encoding="utf-8")


def test_manual_fb_recovery_summary_parsing_share_variants_and_missing_fields():
    module = _load_legacy_module()

    variant_a = module._normalize_manual_fb_share_summary(
        "candidates_found=3\nresolution_success=2\nfb_email_found=1\n"
    )
    variant_b = module._normalize_manual_fb_share_summary(
        "share_candidates_found=4\nshare_resolution_success=3\nshare_fb_email_found=2\n"
    )
    missing = module._normalize_manual_fb_driver_summary("retry_success=1\n")

    assert variant_a == {
        "share_candidates_found": 3,
        "share_resolution_success": 2,
        "share_fb_email_found": 1,
    }
    assert variant_b == {
        "share_candidates_found": 4,
        "share_resolution_success": 3,
        "share_fb_email_found": 2,
    }
    assert missing["driver_candidates_found"] == 0
    assert missing["driver_retry_attempted"] == 0
    assert missing["driver_retry_success"] == 1
