import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy_lead_vault", path)
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


def test_main_window_contains_lead_vault_tab(qapp):
    module = _load_legacy_module()
    window = module.MainWindow()

    labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]

    assert "Lead Vault" in labels
    assert "Final Export View" not in labels
    window.close()


def test_preview_populates_unmapped_controls_and_blocks_import_until_resolved(qapp, tmp_path):
    module = _load_legacy_module()
    tab = module.LeadVaultTab()
    source_path = tmp_path / "input.csv"
    source_path.write_text("Artist Name,Booking Email\nAct,book@example.com\n", encoding="utf-8")
    tab.source_edit.setText(str(source_path))

    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["Artist Name", "Booking Email"],
            "mapped_headers": {"Artist Name": "Artist"},
            "unmapped_headers": ["Booking Email"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    assert tab.unmapped_table.rowCount() == 1
    assert isinstance(tab.detected_headers_view, QtWidgets.QListWidget)
    assert isinstance(tab.mapped_headers_view, QtWidgets.QListWidget)
    assert [tab.detected_headers_view.item(index).text() for index in range(tab.detected_headers_view.count())] == [
        "Artist Name",
        "Booking Email",
    ]
    assert [tab.mapped_headers_view.item(index).text() for index in range(tab.mapped_headers_view.count())] == [
        "Artist Name -> Artist"
    ]
    assert not tab.import_button.isEnabled()

    combo = tab.unmapped_table.cellWidget(0, 1)
    combo.setCurrentText("Primary_Email")
    tab._refresh_import_button()

    assert tab.import_button.isEnabled()


def test_lead_vault_layout_sets_minimum_heights_and_path_tooltips(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)

    tab = module.LeadVaultTab()
    source_path = tmp_path / "incoming.csv"
    export_path = tmp_path / "custom_export.csv"
    tab.source_edit.setText(str(source_path))
    tab.export_output_path.setText(str(export_path))

    assert tab.master_summary_view.minimumHeight() >= 100
    assert isinstance(tab.detected_headers_view, QtWidgets.QListWidget)
    assert isinstance(tab.mapped_headers_view, QtWidgets.QListWidget)
    assert tab.detected_headers_view.minimumHeight() >= 80
    assert tab.mapped_headers_view.minimumHeight() >= 80
    assert tab.detected_headers_view.maximumHeight() <= 160
    assert tab.mapped_headers_view.maximumHeight() <= 160
    assert tab.detected_headers_view.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Maximum
    assert tab.mapped_headers_view.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Maximum
    assert tab.summary_view.minimumHeight() >= 100
    assert tab.unmapped_table.minimumHeight() > tab.detected_headers_view.minimumHeight()

    assert tab.source_edit.toolTip() == str(source_path)
    assert tab.master_path_label.toolTip() == str(master_path)
    assert tab.export_output_path.toolTip() == str(export_path)
    assert tab.master_path_label.cursorPosition() == 0


def test_manual_mapping_table_uses_conservative_column_sizing(qapp):
    module = _load_legacy_module()
    tab = module.LeadVaultTab()
    header = tab.unmapped_table.horizontalHeader()

    assert header.sectionResizeMode(0) == QtWidgets.QHeaderView.Interactive
    assert header.sectionResizeMode(1) == QtWidgets.QHeaderView.Stretch
    assert tab.unmapped_table.columnWidth(0) >= 250


def test_manual_mapping_is_passed_to_backend_worker_and_summary_updates(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    source_path = tmp_path / "input.csv"
    source_path.write_text("Artist Name,Booking Email,Throwaway\nAct,book@example.com,skip\n", encoding="utf-8")

    class _Signal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, payload):
            for slot in list(self._slots):
                slot(payload)

    calls = []

    class FakeWorker:
        def __init__(self, mode, source_path, header_overrides=None, ignored_headers=None, master_path=None, parent=None):
            self.mode = mode
            self.source_path = source_path
            self.header_overrides = dict(header_overrides or {})
            self.ignored_headers = list(ignored_headers or [])
            self.master_path = master_path
            self.finished_signal = _Signal()
            self.error_signal = _Signal()
            calls.append(self)

        def start(self):
            self.finished_signal.emit(
                {
                    "source_path": self.source_path,
                    "master_path": self.master_path,
                    "row_count": 1,
                    "rows_added": 1,
                    "rows_updated": 0,
                    "rows_skipped_duplicates": 0,
                    "rows_unresolved_mapping": 0,
                    "rows_ambiguous": 0,
                    "rows_errors": 0,
                    "detected_headers": ["Artist Name", "Booking Email", "Throwaway"],
                    "mapped_headers": {"Artist Name": "Artist", **self.header_overrides},
                    "ignored_headers": list(self.ignored_headers),
                    "unmapped_headers": [],
                    "warnings": [],
                }
            )

        def isRunning(self):
            return False

        def wait(self, timeout=None):
            return True

        def terminate(self):
            return None

    monkeypatch.setattr(module, "LeadVaultWorker", FakeWorker)
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)

    tab = module.LeadVaultTab()
    tab.source_edit.setText(str(source_path))
    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["Artist Name", "Booking Email", "Throwaway"],
            "mapped_headers": {"Artist Name": "Artist"},
            "unmapped_headers": ["Booking Email", "Throwaway"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    booking_combo = tab.unmapped_table.cellWidget(0, 1)
    throwaway_combo = tab.unmapped_table.cellWidget(1, 1)
    booking_combo.setCurrentText("Primary_Email")
    throwaway_combo.setCurrentText(tab.IGNORE_OPTION)
    tab._refresh_import_button()
    tab._start_import()

    assert calls[-1].mode == "import"
    assert calls[-1].header_overrides == {"Booking Email": "Primary_Email"}
    assert calls[-1].ignored_headers == ["Throwaway"]
    assert "Rows added: 1" in tab.summary_view.toPlainText()


def test_generate_export_uses_master_csv_and_shows_summary(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    master_path.write_text("Artist,Primary_Email\nAct,act@example.com\n", encoding="utf-8-sig")
    output_path = tmp_path / "woodpecker_export.csv"
    calls = []
    info_calls = []

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)

    def fake_export_with_preset(preset, master_csv_path, target_output_path):
        calls.append((preset, master_csv_path, target_output_path))
        return {
            "preset": "woodpecker",
            "rows_read": 1,
            "rows_exported": 1,
            "rows_skipped": 0,
            "output_file": str(target_output_path),
        }

    monkeypatch.setattr(module, "export_with_preset", fake_export_with_preset)
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module.QtWidgets.QMessageBox,
        "information",
        lambda *args, **kwargs: info_calls.append(args[2] if len(args) > 2 else ""),
    )

    tab = module.LeadVaultTab()
    tab.export_output_path.setText(str(output_path))
    tab.generate_export_button.click()

    assert calls
    assert calls[0][0]["name"] == "woodpecker"
    assert calls[0][1] == master_path
    assert calls[0][2] == output_path
    assert info_calls
    assert "Export complete" in info_calls[0]
    assert "Rows exported: 1" in info_calls[0]


def test_refresh_summary_updates_read_only_panel(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    monkeypatch.setattr(
        module,
        "summarize_master_dataset",
        lambda path: {
            "total_rows": 4,
            "rows_with_email": 2,
            "needs_review": 1,
            "sources": {
                "bandcamp": 1,
                "spotify": 3,
            },
        },
    )

    tab = module.LeadVaultTab()
    assert tab.master_summary_view.isReadOnly()

    tab.refresh_summary_button.click()

    assert tab.master_summary_view.toPlainText() == (
        "Total leads: 4\n"
        "Leads with email: 2\n"
        "Needs review: 1\n"
        "\n"
        "Sources\n"
        "bandcamp: 1\n"
        "spotify: 3"
    )


def test_refresh_summary_failure_updates_panel_text(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "missing.csv"

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)

    def _raise_missing(_path):
        raise FileNotFoundError(f"No such file: {master_path}")

    monkeypatch.setattr(module, "summarize_master_dataset", _raise_missing)

    tab = module.LeadVaultTab()
    tab.refresh_summary_button.click()

    assert "Lead Vault summary unavailable:" in tab.master_summary_view.toPlainText()
    assert str(master_path) in tab.master_summary_view.toPlainText()


def test_export_preset_selector_exposes_both_presets_and_updates_default_path(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)

    tab = module.LeadVaultTab()

    preset_names = [tab.preset_selector.itemText(index) for index in range(tab.preset_selector.count())]

    assert preset_names == ["woodpecker", "final_export"]
    assert tab.export_output_path.text().endswith("woodpecker_export.csv")

    tab.preset_selector.setCurrentText("final_export")

    assert tab._selected_export_preset()["name"] == "final_export"
    assert tab.export_output_path.text().endswith("final_export.csv")


def test_generate_export_uses_selected_preset(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    master_path.write_text("Artist,Primary_Email\nAct,act@example.com\n", encoding="utf-8-sig")
    calls = []

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)

    def fake_export_with_preset(preset, master_csv_path, target_output_path):
        calls.append((preset, master_csv_path, target_output_path))
        return {
            "preset": str(preset["name"]),
            "rows_read": 1,
            "rows_exported": 1,
            "rows_skipped": 0,
            "output_file": str(target_output_path),
        }

    monkeypatch.setattr(module, "export_with_preset", fake_export_with_preset)
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "information", lambda *args, **kwargs: None)

    tab = module.LeadVaultTab()
    tab.preset_selector.setCurrentText("final_export")
    tab.generate_export_button.click()

    assert calls
    assert calls[0][0]["name"] == "final_export"
    assert calls[0][1] == master_path
    assert calls[0][2].name == "final_export.csv"


def test_night_mode_run_summary_panel_shows_placeholder_when_summary_is_missing(qapp, tmp_path):
    module = _load_legacy_module()
    run_root = tmp_path / "overnight_runs"
    (run_root / "2026-03-10_115959").mkdir(parents=True)
    tab = module.NightModeTab()
    tab.run_root_edit.setText(str(run_root))
    tab._refresh_run_summary()

    assert tab.run_summary_view.isReadOnly()
    assert tab.run_summary_view.toPlainText() == module.NIGHT_MODE_RUN_SUMMARY_PLACEHOLDER


def test_night_mode_run_summary_panel_renders_latest_summary(qapp, tmp_path):
    module = _load_legacy_module()
    run_root = tmp_path / "overnight_runs"
    latest_run = run_root / "2026-03-10_120000"
    latest_run.mkdir(parents=True)
    (latest_run / module.NIGHT_MODE_RUN_SUMMARY_FILENAME).write_text(
        json.dumps(
            {
                "run_timestamp": "2026-03-10_120000",
                "seeds_processed": 200,
                "artists_processed": 180,
                "domains_discovered": 86,
                "emails_discovered": 52,
                "orgs_created": 18,
                "vault_rows_added": 47,
                "vault_rows_updated": 11,
            }
        ),
        encoding="utf-8",
    )

    tab = module.NightModeTab()
    tab.run_root_edit.setText(str(run_root))
    tab._refresh_run_summary()

    assert tab.run_summary_view.toPlainText() == (
        "Seeds processed: 200\n"
        "Artists processed: 180\n"
        "Domains discovered: 86\n"
        "Emails discovered: 52\n"
        "Reusable orgs created: 18\n"
        "\n"
        "Lead Vault\n"
        "Rows added: 47\n"
        "Rows updated: 11"
    )
