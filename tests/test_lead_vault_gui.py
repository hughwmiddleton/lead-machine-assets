import csv
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


def test_ignore_all_sets_every_mapping_dropdown_to_ignore(qapp, tmp_path):
    module = _load_legacy_module()
    tab = module.LeadVaultTab()
    source_path = tmp_path / "input.csv"

    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["FB_Status", "Throwaway"],
            "mapped_headers": {},
            "unmapped_headers": ["FB_Status", "Throwaway"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab.ignore_all_button.click()

    assert tab.import_button.isEnabled()
    assert [tab.unmapped_table.cellWidget(index, 1).currentData() for index in range(tab.unmapped_table.rowCount())] == [
        tab.IGNORE_OPTION,
        tab.IGNORE_OPTION,
    ]


def test_auto_map_known_reuses_alias_mapping_and_ignores_unknown_headers(qapp, tmp_path):
    module = _load_legacy_module()
    tab = module.LeadVaultTab()
    source_path = tmp_path / "input.csv"

    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["Email", "Artist Name", "FB_Status"],
            "mapped_headers": {},
            "unmapped_headers": ["Email", "Artist Name", "FB_Status"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab.auto_map_known_button.click()

    assert tab.import_button.isEnabled()
    assert [tab.unmapped_table.cellWidget(index, 1).currentData() for index in range(tab.unmapped_table.rowCount())] == [
        "Primary_Email",
        "Artist",
        tab.IGNORE_OPTION,
    ]


def test_manual_override_still_works_after_bulk_action(qapp, tmp_path):
    module = _load_legacy_module()
    tab = module.LeadVaultTab()
    source_path = tmp_path / "input.csv"

    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["Booking Email", "FB_Status"],
            "mapped_headers": {},
            "unmapped_headers": ["Booking Email", "FB_Status"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab.ignore_all_button.click()
    booking_combo = tab.unmapped_table.cellWidget(0, 1)
    booking_combo.setCurrentText("Primary_Email")
    tab._refresh_import_button()

    overrides, ignored_headers, unresolved_headers = tab._collect_manual_mapping_state()

    assert overrides == {"Booking Email": "Primary_Email"}
    assert ignored_headers == ["FB_Status"]
    assert unresolved_headers == []
    assert tab.import_button.isEnabled()


def test_save_mapping_preset_persists_current_table_state_locally(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    info_calls = []

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    monkeypatch.setattr(
        module.QtWidgets.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("unearthed_reviewed", True),
    )
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module.QtWidgets.QMessageBox,
        "information",
        lambda *args, **kwargs: info_calls.append(args[2] if len(args) > 2 else ""),
    )

    tab = module.LeadVaultTab()
    tab._handle_preview_finished(
        {
            "source_path": str(tmp_path / "input.csv"),
            "master_path": str(master_path),
            "row_count": 1,
            "detected_headers": ["Booking Email", "Debug Flag", "Leave Blank"],
            "mapped_headers": {},
            "unmapped_headers": ["Booking Email", "Debug Flag", "Leave Blank"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab.unmapped_table.cellWidget(0, 1).setCurrentText("Primary_Email")
    tab.unmapped_table.cellWidget(1, 1).setCurrentText(tab.IGNORE_OPTION)
    tab.save_mapping_preset_button.click()

    state_path = tmp_path / module.LEAD_VAULT_UI_STATE_FILENAME
    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["mapping_presets"]["unearthed_reviewed"] == {
        "Booking Email": "Primary_Email",
        "Debug Flag": tab.IGNORE_OPTION,
    }
    assert "Leave Blank" not in payload["mapping_presets"]["unearthed_reviewed"]
    assert info_calls
    assert "Saved preset 'unearthed_reviewed'" in info_calls[0]


def test_load_mapping_preset_updates_matching_rows_only_and_manual_override_still_works(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    state_path = tmp_path / module.LEAD_VAULT_UI_STATE_FILENAME
    state_path.write_text(
        json.dumps(
            {
                "mapping_presets": {
                    "unearthed_reviewed": {
                        "Email": "Primary_Email",
                        "Debug Flag": "Ignore column",
                        "Legacy ID": "Artist",
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.QtWidgets.QInputDialog,
        "getItem",
        lambda *args, **kwargs: ("unearthed_reviewed", True),
    )
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "information", lambda *args, **kwargs: None)

    tab = module.LeadVaultTab()
    tab._handle_preview_finished(
        {
            "source_path": str(tmp_path / "input.csv"),
            "master_path": str(master_path),
            "row_count": 1,
            "detected_headers": ["Email", "Debug Flag", "Fresh Debug"],
            "mapped_headers": {},
            "unmapped_headers": ["Email", "Debug Flag", "Fresh Debug"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    fresh_combo = tab.unmapped_table.cellWidget(2, 1)
    fresh_combo.setCurrentText("Artist")

    tab.load_mapping_preset_button.click()

    assert tab.unmapped_table.cellWidget(0, 1).currentData() == "Primary_Email"
    assert tab.unmapped_table.cellWidget(1, 1).currentData() == tab.IGNORE_OPTION
    assert fresh_combo.currentData() == "Artist"

    tab.unmapped_table.cellWidget(0, 1).setCurrentText("Contact_Name")
    overrides, ignored_headers, unresolved_headers = tab._collect_manual_mapping_state()

    assert overrides == {
        "Email": "Contact_Name",
        "Fresh Debug": "Artist",
    }
    assert ignored_headers == ["Debug Flag"]
    assert unresolved_headers == []


def test_bulk_actions_still_work_after_mapping_preset_load(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    (tmp_path / module.LEAD_VAULT_UI_STATE_FILENAME).write_text(
        json.dumps(
            {
                "mapping_presets": {
                    "repeat_source": {
                        "Email": "Contact_Name",
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.QtWidgets.QInputDialog,
        "getItem",
        lambda *args, **kwargs: ("repeat_source", True),
    )
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "information", lambda *args, **kwargs: None)

    tab = module.LeadVaultTab()
    tab._handle_preview_finished(
        {
            "source_path": str(tmp_path / "input.csv"),
            "master_path": str(master_path),
            "row_count": 1,
            "detected_headers": ["Email", "Artist Name", "Mystery Header"],
            "mapped_headers": {},
            "unmapped_headers": ["Email", "Artist Name", "Mystery Header"],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab.load_mapping_preset_button.click()
    assert tab.unmapped_table.cellWidget(0, 1).currentData() == "Contact_Name"

    tab.ignore_all_button.click()
    assert [tab.unmapped_table.cellWidget(index, 1).currentData() for index in range(tab.unmapped_table.rowCount())] == [
        tab.IGNORE_OPTION,
        tab.IGNORE_OPTION,
        tab.IGNORE_OPTION,
    ]

    tab.auto_map_known_button.click()
    assert [tab.unmapped_table.cellWidget(index, 1).currentData() for index in range(tab.unmapped_table.rowCount())] == [
        "Primary_Email",
        "Artist",
        tab.IGNORE_OPTION,
    ]


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
    assert tab.master_selector.currentData() == master_path.name


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
        def __init__(
            self,
            mode,
            source_path,
            header_overrides=None,
            ignored_headers=None,
            master_path=None,
            duplicate_strategy="update",
            parent=None,
        ):
            self.mode = mode
            self.source_path = source_path
            self.header_overrides = dict(header_overrides or {})
            self.ignored_headers = list(ignored_headers or [])
            self.master_path = master_path
            self.duplicate_strategy = duplicate_strategy
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
    monkeypatch.setattr(module, "preview_csv_merge_counts", lambda *args, **kwargs: {"rows_duplicates_detected": 0})
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
    assert calls[-1].duplicate_strategy == "update"
    assert "New contacts added: 1" in tab.summary_view.toPlainText()


def test_duplicate_prompt_passes_skip_strategy_to_import_worker(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    source_path = tmp_path / "input.csv"
    source_path.write_text("Artist Name,Profile URL\nAct,https://example.com/act\n", encoding="utf-8")

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
        def __init__(
            self,
            mode,
            source_path,
            header_overrides=None,
            ignored_headers=None,
            master_path=None,
            duplicate_strategy="update",
            parent=None,
        ):
            self.mode = mode
            self.source_path = source_path
            self.header_overrides = dict(header_overrides or {})
            self.ignored_headers = list(ignored_headers or [])
            self.master_path = master_path
            self.duplicate_strategy = duplicate_strategy
            self.finished_signal = _Signal()
            self.error_signal = _Signal()
            calls.append(self)

        def start(self):
            self.finished_signal.emit(
                {
                    "source_path": self.source_path,
                    "master_path": self.master_path,
                    "row_count": 1,
                    "rows_added": 0,
                    "rows_updated": 0,
                    "rows_duplicates_detected": 1,
                    "rows_skipped_duplicates": 1,
                    "rows_kept_duplicates": 0,
                    "rows_unresolved_mapping": 0,
                    "rows_ambiguous": 0,
                    "rows_errors": 0,
                    "detected_headers": ["Artist Name", "Profile URL"],
                    "mapped_headers": {"Artist Name": "Artist", "Profile URL": "Source_URL"},
                    "ignored_headers": [],
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

    class FakeMessageBox:
        Question = object()
        AcceptRole = object()
        ActionRole = object()
        Cancel = object()

        def __init__(self, *args, **kwargs):
            self._buttons = []
            self._clicked = None

        def setIcon(self, *_args, **_kwargs):
            return None

        def setWindowTitle(self, *_args, **_kwargs):
            return None

        def setText(self, *_args, **_kwargs):
            return None

        def setInformativeText(self, *_args, **_kwargs):
            return None

        def addButton(self, label_or_button, *_args, **_kwargs):
            button = object()
            self._buttons.append((label_or_button, button))
            return button

        def setDefaultButton(self, *_args, **_kwargs):
            return None

        def exec_(self):
            for label, button in self._buttons:
                if label == "Skip duplicates":
                    self._clicked = button
                    return

        def clickedButton(self):
            return self._clicked

    monkeypatch.setattr(module, "LeadVaultWorker", FakeWorker)
    monkeypatch.setattr(module, "preview_csv_merge_counts", lambda *args, **kwargs: {"rows_duplicates_detected": 1})
    monkeypatch.setattr(module.QtWidgets, "QMessageBox", FakeMessageBox)

    tab = module.LeadVaultTab()
    tab.source_edit.setText(str(source_path))
    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["Artist Name", "Profile URL"],
            "mapped_headers": {"Artist Name": "Artist", "Profile URL": "Source_URL"},
            "unmapped_headers": [],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab._start_import()

    assert calls
    assert calls[-1].duplicate_strategy == "skip"
    assert "Duplicates skipped: 1" in tab.summary_view.toPlainText()


def test_no_duplicate_import_does_not_show_prompt(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    source_path = tmp_path / "input.csv"
    source_path.write_text("Artist Name,Profile URL\nAct,https://example.com/act\n", encoding="utf-8")
    calls = []

    class FakeWorker:
        def __init__(
            self,
            mode,
            source_path,
            header_overrides=None,
            ignored_headers=None,
            master_path=None,
            duplicate_strategy="update",
            parent=None,
        ):
            self.mode = mode
            self.source_path = source_path
            self.header_overrides = dict(header_overrides or {})
            self.ignored_headers = list(ignored_headers or [])
            self.master_path = master_path
            self.duplicate_strategy = duplicate_strategy
            self.finished_signal = type("Signal", (), {"connect": staticmethod(lambda slot: None)})()
            self.error_signal = type("Signal", (), {"connect": staticmethod(lambda slot: None)})()
            calls.append(self)

        def start(self):
            return None

        def isRunning(self):
            return False

        def wait(self, timeout=None):
            return True

        def terminate(self):
            return None

    def _unexpected_message_box(*_args, **_kwargs):
        raise AssertionError("Duplicate prompt should not be shown when there are no duplicates.")

    monkeypatch.setattr(module, "LeadVaultWorker", FakeWorker)
    monkeypatch.setattr(module, "preview_csv_merge_counts", lambda *args, **kwargs: {"rows_duplicates_detected": 0})
    monkeypatch.setattr(module.QtWidgets, "QMessageBox", _unexpected_message_box)

    tab = module.LeadVaultTab()
    tab.source_edit.setText(str(source_path))
    tab._handle_preview_finished(
        {
            "source_path": str(source_path),
            "master_path": str(tmp_path / "master.csv"),
            "row_count": 1,
            "detected_headers": ["Artist Name", "Profile URL"],
            "mapped_headers": {"Artist Name": "Artist", "Profile URL": "Source_URL"},
            "unmapped_headers": [],
            "ignored_headers": [],
            "warnings": [],
        }
    )

    tab._start_import()

    assert calls
    assert calls[-1].duplicate_strategy == "update"


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


def test_create_master_csv_initializes_schema_and_selects_new_file(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    info_calls = []

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    monkeypatch.setattr(
        module.QtWidgets.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("unearthed master", True),
    )
    monkeypatch.setattr(module.QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module.QtWidgets.QMessageBox,
        "information",
        lambda *args, **kwargs: info_calls.append(args[2] if len(args) > 2 else ""),
    )

    tab = module.LeadVaultTab()
    tab.create_master_button.click()

    new_master_path = tmp_path / "unearthed_master.csv"

    assert new_master_path.exists()
    with open(new_master_path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        assert next(reader) == module.get_canonical_master_schema()
    assert tab.master_selector.currentData() == "unearthed_master.csv"
    assert tab.master_path_label.text() == str(new_master_path)
    assert not info_calls


def test_unmapped_dropdown_exposes_new_canonical_outreach_headers(qapp):
    module = _load_legacy_module()
    tab = module.LeadVaultTab()
    tab._populate_unmapped_table(["Mystery Header"])

    combo = tab.unmapped_table.cellWidget(0, 1)
    options = [combo.itemText(index) for index in range(combo.count())]

    assert "Sounds Like" in options
    assert "Social Link" in options
    assert "Unearthed_Genre_Raw" in options
    assert "Email_Type" in options
    assert "FB_Debug_Reason" not in options


def test_master_selector_persists_selected_master_csv(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    alt_master_path = tmp_path / "unearthed_master.csv"
    alt_master_path.write_text("Artist\n", encoding="utf-8-sig")

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)

    first_tab = module.LeadVaultTab()
    first_tab.master_selector.setCurrentIndex(first_tab.master_selector.findData(alt_master_path.name))

    second_tab = module.LeadVaultTab()

    assert second_tab.master_selector.currentData() == alt_master_path.name
    assert second_tab.master_path_label.text() == str(alt_master_path)


def test_start_worker_uses_selected_master_csv(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    source_path = tmp_path / "input.csv"
    source_path.write_text("Artist Name\nAct\n", encoding="utf-8")
    master_path = tmp_path / "master.csv"
    alt_master_path = tmp_path / "unearthed_master.csv"
    alt_master_path.write_text("Artist\n", encoding="utf-8-sig")
    calls = []

    class FakeWorker:
        def __init__(
            self,
            mode,
            source_path,
            header_overrides=None,
            ignored_headers=None,
            master_path=None,
            duplicate_strategy="update",
            parent=None,
        ):
            self.mode = mode
            self.source_path = source_path
            self.header_overrides = dict(header_overrides or {})
            self.ignored_headers = list(ignored_headers or [])
            self.master_path = master_path
            self.duplicate_strategy = duplicate_strategy
            self.finished_signal = type("Signal", (), {"connect": staticmethod(lambda slot: None)})()
            self.error_signal = type("Signal", (), {"connect": staticmethod(lambda slot: None)})()
            calls.append(self)

        def start(self):
            return None

        def isRunning(self):
            return False

        def wait(self, timeout=None):
            return True

        def terminate(self):
            return None

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    monkeypatch.setattr(module, "LeadVaultWorker", FakeWorker)

    tab = module.LeadVaultTab()
    tab.source_edit.setText(str(source_path))
    tab.master_selector.setCurrentIndex(tab.master_selector.findData(alt_master_path.name))
    tab._start_worker("preview", str(source_path), {}, [])

    assert calls
    assert calls[0].master_path == str(alt_master_path)
    assert tab.master_path_label.text() == str(alt_master_path)


def test_create_master_csv_rejects_folder_paths(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    master_path = tmp_path / "master.csv"
    warning_calls = []

    monkeypatch.setattr(module, "get_default_master_csv_path", lambda: master_path)
    monkeypatch.setattr(
        module.QtWidgets.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("../escape", True),
    )
    monkeypatch.setattr(
        module.QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: warning_calls.append(args[2] if len(args) > 2 else ""),
    )

    tab = module.LeadVaultTab()
    tab.create_master_button.click()

    assert warning_calls == ["Enter a filename only, not a folder path."]
    assert tab.master_selector.currentData() == master_path.name
    assert tab.master_path_label.text() == str(master_path)


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


def test_night_mode_selected_cursor_round_trips_into_saved_config(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    tab.jobs = [
        {"job_id": "job_unearthed_1", "directory": "unearthed", "target_valid_leads": 15},
        {"job_id": "job_spotify_1", "directory": "spotify", "target_valid_leads": 10},
    ]
    tab._set_unearthed_resume_mode("selected")
    checkpoint = "https://www.abc.net.au/triplejunearthed/artist/artist-42"
    tab._set_unearthed_selected_cursor(checkpoint)

    output_path = tmp_path / "overnight_jobs.json"
    monkeypatch.setattr(
        module.QtWidgets.QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(output_path), "JSON Files (*.json)"),
    )

    tab._save_config_to_file()

    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert tab.unearthed_selected_cursor_edit.isEnabled()
    assert payload["unearthed_resume_mode"] == "selected"
    assert payload["unearthed_selected_cursor"] == checkpoint
    assert payload["jobs"][0]["unearthed_resume_mode"] == "selected"
    assert payload["jobs"][0]["unearthed_selected_cursor"] == checkpoint
    assert "unearthed_selected_cursor" not in payload["jobs"][1]


def test_night_mode_unearthed_source_mode_writes_explicit_default_flag(qapp):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    tab.jobs = [
        {"job_id": "job_unearthed_1", "directory": "unearthed", "target_valid_leads": 15},
        {"job_id": "job_spotify_1", "directory": "spotify", "target_valid_leads": 10},
    ]

    jobs = tab._night_mode_jobs_for_config()

    assert tab._current_unearthed_use_url_index() is False
    assert jobs[0]["use_unearthed_url_index"] is False
    assert "use_unearthed_url_index" not in jobs[1]


def test_night_mode_unearthed_source_mode_writes_index_flag(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    index_path.write_text("artist_url\nhttps://example.test/a\nhttps://example.test/b\n", encoding="utf-8")
    tab._set_unearthed_url_index_path(str(index_path))
    tab.jobs = [{"job_id": "job_unearthed_1", "directory": "unearthed"}]

    tab._set_unearthed_source_mode(True)
    tab._sync_unearthed_source_mode_controls()
    jobs = tab._night_mode_jobs_for_config()

    assert jobs[0]["use_unearthed_url_index"] is True
    assert jobs[0]["unearthed_url_index_path"] == str(index_path)
    assert "ACTIVE INDEX: unearthed_artist_url_index.csv" in tab.unearthed_index_status_label.text()
    assert "Index contains: 2 artist URLs" in tab.unearthed_index_status_label.text()


def test_night_mode_unearthed_source_mode_reports_missing_index(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    missing_path = tmp_path / "missing.csv"
    tab._set_unearthed_url_index_path(str(missing_path))

    tab._set_unearthed_source_mode(True)
    tab._sync_unearthed_source_mode_controls()

    assert "Index not found" in tab.unearthed_index_status_label.text()


def test_night_mode_unearthed_source_mode_reports_empty_index(qapp, monkeypatch, tmp_path):
    module = _load_legacy_module()
    tab = module.NightModeTab()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    index_path.write_text("artist_url\n", encoding="utf-8")
    tab._set_unearthed_url_index_path(str(index_path))

    tab._set_unearthed_source_mode(True)
    tab._sync_unearthed_source_mode_controls()

    assert "Index contains: 0 artist URLs" in tab.unearthed_index_status_label.text()
