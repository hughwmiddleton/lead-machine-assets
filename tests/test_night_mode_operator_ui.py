"""Focused operator-workflow regressions for the Night Mode GUI."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets


def _load_gui():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_operator_ui", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def test_pre_run_summary_is_derived_from_effective_config():
    module = _load_gui()
    summary = module._build_night_mode_pre_run_summary(
        {
            "jobs": [
                {"directory": "bandcamp", "target_valid_leads": 10},
                {"directory": "spotify", "target_valid_leads": 5},
            ],
            "export_mode": "both",
            "facebook": {"auto_resume_after_captcha": True, "max_rows_per_run": 4},
            "master_enrichment": {"enabled": True, "enable_live_search": True, "max_live_searches": 0},
        }
    )

    assert summary["headline"] == "2 jobs · 15 target leads"
    assert summary["features"] == "Full enrichment ON · Facebook ON · Auto-resume ON"
    assert summary["export"] == "Export: both"
    assert summary["warnings"] == ["Facebook limited to 4 rows"]
    assert summary["ready"] is True


def test_job_rows_are_readable_and_duplicate_without_mutating_original(qapp):
    module = _load_gui()
    tab = module.NightModeTab()
    tab.jobs = [
        {
            "job_id": "bandcamp_uk",
            "directory": "bandcamp",
            "mode": "discover",
            "input_seed_csv": "https://bandcamp.com/discover/united-kingdom+alternative?s=new",
            "target_valid_leads": 10,
        }
    ]
    tab._refresh_jobs_table()

    assert tab.jobs_table.item(0, 0).text() == "Bandcamp"
    assert tab.jobs_table.item(0, 1).text() == "United Kingdom · Alternative"
    assert tab.jobs_table.item(0, 2).text() == "10"
    action_texts = {button.text() for button in tab.jobs_table.cellWidget(0, 3).findChildren(QtWidgets.QPushButton)}
    assert action_texts == {"Edit", "Duplicate", "Remove"}

    tab._duplicate_job_at(0)
    assert [job["job_id"] for job in tab.jobs] == ["bandcamp_uk", "bandcamp_uk_copy"]
    assert tab.jobs[0] is not tab.jobs[1]
    tab.shutdown()


def test_facebook_degradation_presentation_is_calm_and_specific():
    module = _load_gui()
    display = module._format_facebook_availability(
        {
            "facebook_degradation": {
                "state": "unavailable",
                "reason": "two_factor_login_challenge",
                "later_opportunities_skipped": 4,
                "run_completed": True,
            }
        }
    )

    assert display["state"] == "unavailable"
    assert display["text"] == (
        "Facebook unavailable · two factor login challenge\n"
        "4 later Facebook opportunities skipped"
    )


def test_completion_summary_uses_authoritative_export_statuses(tmp_path):
    module = _load_gui()
    export = tmp_path / "master_export_leads.csv"
    export.write_text(
        "Artist Name,Email,Email_All,final_status\n"
        "A,a@example.com,,OK\n"
        "B,,,WARN\n"
        "C,,c@example.com,BLOCK\n",
        encoding="utf-8",
    )

    result = module._build_night_mode_completion_summary(
        {"artists_processed": 99, "emails_discovered": 99}, str(export)
    )

    assert result["headline"] == "3 leads · 2 emails · 67% yield"
    assert result["status_counts"] == {"OK": 1, "WARN": 1, "BLOCK": 1}


def test_stage_display_uses_manifest_and_existing_contact_artifacts(tmp_path):
    module = _load_gui()
    manifest = {
        "phases": {
            "seed": {"status": "completed"},
            "enrich": {"status": "completed"},
            "contact": {"status": "running"},
        }
    }
    assert module._build_night_mode_stage_display(manifest, str(tmp_path))["stage"] == "Social / Contact"
    (tmp_path / "master_post_fb.csv").write_text("Artist Name\nA\n", encoding="utf-8")
    assert module._build_night_mode_stage_display(manifest, str(tmp_path))["stage"] == "Validation"
    manifest["phases"]["contact"]["status"] = "completed"
    assert module._build_night_mode_stage_display(manifest, str(tmp_path))["state"] == "completed"


def test_main_window_opens_on_night_mode_with_lead_machine_title(qapp):
    module = _load_gui()
    window = module.MainWindow()

    assert window.windowTitle() == "Lead Machine"
    assert window.tabs.currentWidget() is window.night_mode_tab
    assert window.tabs.tabText(0) == "Night Mode"
    assert "Legacy" in window.tabs.tabText(1)
    window.close()


def test_launch_config_matches_operator_controls(qapp, monkeypatch):
    module = _load_gui()
    captured = {}

    class FakeWorker:
        def __init__(self, command, workdir, env=None, secrets=None, parent=None):
            captured["command"] = command
            self.log_signal = SimpleNamespace(connect=lambda _fn: None)
            self.finished_signal = SimpleNamespace(connect=lambda _fn: None)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(module, "NightModeWorker", FakeWorker)
    tab = module.NightModeTab()
    tab.jobs = [{"job_id": "job_1", "directory": "spotify", "target_valid_leads": 5}]
    tab.master_enrich_checkbox.setChecked(False)
    tab.fb_auto_resume_checkbox.setChecked(True)
    tab.fb_max_rows_spin.setValue(9)
    tab.export_mode_combo.setCurrentText("combined")

    expected = tab._effective_config_snapshot()
    tab._launch_night_mode(headless=True)
    command = captured["command"]
    payload = json.loads(Path(command[command.index("--config") + 1]).read_text(encoding="utf-8"))

    assert payload["jobs"] == expected["jobs"]
    assert payload["export_mode"] == expected["export_mode"]
    assert payload["facebook"] == expected["facebook"]
    assert payload["master_enrichment"] == expected["master_enrichment"]
    tab.shutdown()
