#!/usr/bin/env python3
"""
Lead Machine 11

This script scrapes artist data (Page 1) and Facebook pages (Page 2) and exports the results to CSV files.
It detects whether a song has been played on triple j or triple j unearthed by examining the drum logo on
the artist's page. The artist scraping output CSV includes two columns:
"Played on triple J" and "Played on Unearthed" (with "yes" if detected, or blank otherwise).
When scraping Facebook pages (Page 2), these columns are carried over from the input CSV.

Before running this script, please ensure you have installed the following packages:

    pip install pandas tqdm selenium beautifulsoup4 webdriver_manager PyQt5

Usage:
    python lead_machine11.py
"""

from __future__ import annotations

import sys
import subprocess
import platform
import traceback
import tempfile
import shutil
import logging
import copy
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from fb_email_override import should_accept_email_override
from lead_engine import campaign_prep_sidecar
# ---------------------------
# Dependency Check and Installation
# ---------------------------
required_packages = {
    "pandas": "pandas",
    "tqdm": "tqdm",
    "selenium": "selenium",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "webdriver_manager": "webdriver_manager",
    "PyQt5": "PyQt5",
    "requests": "requests"
}

logger = logging.getLogger(__name__)

def install_package(package_name):
    """Install a package using pip."""
    print(f"Installing {package_name}...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])

def check_and_install_dependencies():
    missing_packages = []
    for import_name, package_name in required_packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_packages.append((import_name, package_name))
    if missing_packages:
        print("The following packages are missing:")
        for imp, pkg in missing_packages:
            print(f" - {pkg}")
        ans = input("Would you like to install them now? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            for imp, pkg in missing_packages:
                try:
                    install_package(pkg)
                except Exception as e:
                    print(f"Failed to install {pkg}: {e}")
                    sys.exit(1)
        else:
            print("Dependencies are missing. Exiting.")
            sys.exit(1)

# ---------------------------
# Prevent Sleep on macOS using caffeinate
# ---------------------------
caffeinate_proc = None
if __name__ == "__main__":
    check_and_install_dependencies()
    if platform.system() == "Darwin":
        print("Detected macOS – starting caffeinate to prevent sleep.")
        caffeinate_proc = subprocess.Popen(['caffeinate'])

# ---------------------------
# Now import the dependencies
# ---------------------------
import os
from soundcloud_engine import SoundCloudEngine
import atexit
import html
import time
import random
import re
import csv
import pandas as pd
import datetime
import json
import argparse
import select
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup as BS
try:
    from bs4 import FeatureNotFound
except Exception:
    class FeatureNotFound(Exception):
        pass
BeautifulSoup = BS
from urllib.parse import (
    urlparse,
    urljoin,
    parse_qs,
    parse_qsl,
    urlunparse,
    urlencode,
    unquote,
    quote,
    quote_plus,
)
from unearthed_common import extract_unearthed_genre_text, parse_unearthed_genre
import urllib.parse as _urlparse
from PyQt5 import QtWidgets, QtCore, QtGui
from dateutil import parser as dparser
from dateutil.relativedelta import relativedelta
import unicodedata
from spotify_scraper import scrape_spotify
from origin_validator import _derive_origin_output_path, run_auto_validate
from soundcloud_metadata_enricher import enrich_soundcloud_metadata
import pipeline_runner
from source_scheduler import canonicalize_facebook_url
from lead_vault import EXPORT_PRESETS, WOODPECKER_EXPORT_PRESET, ensure_master_csv_exists, export_with_preset
from lead_vault.alias_map import map_headers_to_canonical
from lead_vault.merge import confirm_csv_merge_preview, merge_csv_into_master, preview_csv_import, preview_csv_merge_counts
from lead_vault.schema import get_canonical_master_schema, get_default_master_csv_path
from lead_vault.stats import summarize_master_dataset
from progress_state import init_progress, read_progress, update_progress

NIGHT_MODE_RUN_SUMMARY_FILENAME = "run_summary.json"
NIGHT_MODE_RUN_SUMMARY_PLACEHOLDER = "No runs detected yet.\nRun Lead Machine to generate results."
LEAD_VAULT_UI_STATE_FILENAME = "lead_vault_ui_state.json"

LM_SPACING_ROW = 12
LM_SPACING_SECTION = 20
LM_PADDING_CONTAINER = 16
LM_PADDING_SECTION = 14
LM_LABEL_COLUMN_WIDTH = 210
LM_CONTROL_MIN_HEIGHT = 34
LM_BUTTON_MIN_HEIGHT = 36
LM_PROGRESS_MIN_HEIGHT = 22
LM_LOG_MIN_HEIGHT = 180
LM_TABLE_MIN_HEIGHT = 180


def _lm_apply_control_sizing(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
    if isinstance(widget, QtWidgets.QPushButton):
        widget.setMinimumHeight(LM_BUTTON_MIN_HEIGHT)
    elif isinstance(
        widget,
        (
            QtWidgets.QLineEdit,
            QtWidgets.QComboBox,
            QtWidgets.QSpinBox,
            QtWidgets.QDoubleSpinBox,
        ),
    ):
        widget.setMinimumHeight(LM_CONTROL_MIN_HEIGHT)
    elif isinstance(widget, QtWidgets.QCheckBox):
        widget.setMinimumHeight(LM_CONTROL_MIN_HEIGHT)
    elif isinstance(widget, QtWidgets.QProgressBar):
        widget.setMinimumHeight(LM_PROGRESS_MIN_HEIGHT)
    return widget


def _lm_row(label_text: str, *widgets: QtWidgets.QWidget, add_stretch: bool = False) -> QtWidgets.QHBoxLayout:
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(LM_SPACING_ROW)
    label = QtWidgets.QLabel(label_text)
    label.setFixedWidth(LM_LABEL_COLUMN_WIDTH)
    label.setWordWrap(True)
    row.addWidget(label)
    for index, widget in enumerate(widgets):
        _lm_apply_control_sizing(widget)
        stretch = 1 if index == 0 and isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QComboBox, QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit, QtWidgets.QTableWidget)) else 0
        row.addWidget(widget, stretch)
    if add_stretch:
        row.addStretch()
    return row


def _lm_row_with_label_widget(label: QtWidgets.QLabel, *widgets: QtWidgets.QWidget, add_stretch: bool = False) -> QtWidgets.QHBoxLayout:
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(LM_SPACING_ROW)
    label.setFixedWidth(LM_LABEL_COLUMN_WIDTH)
    label.setWordWrap(True)
    row.addWidget(label)
    for index, widget in enumerate(widgets):
        _lm_apply_control_sizing(widget)
        stretch = 1 if index == 0 and isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QComboBox, QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit, QtWidgets.QTableWidget)) else 0
        row.addWidget(widget, stretch)
    if add_stretch:
        row.addStretch()
    return row


def _lm_control_row(*widgets: QtWidgets.QWidget, add_stretch: bool = True) -> QtWidgets.QHBoxLayout:
    return _lm_row("", *widgets, add_stretch=add_stretch)


def _lm_section(title: str) -> tuple[QtWidgets.QGroupBox, QtWidgets.QVBoxLayout]:
    group = QtWidgets.QGroupBox(title)
    layout = QtWidgets.QVBoxLayout()
    layout.setContentsMargins(
        LM_PADDING_SECTION,
        LM_PADDING_SECTION,
        LM_PADDING_SECTION,
        LM_PADDING_SECTION,
    )
    layout.setSpacing(LM_SPACING_ROW)
    group.setLayout(layout)
    return group, layout


def _lm_collapsible_section(title: str, *, collapsed: bool = True) -> tuple[QtWidgets.QWidget, QtWidgets.QToolButton, QtWidgets.QWidget, QtWidgets.QVBoxLayout]:
    container = QtWidgets.QWidget()
    outer_layout = QtWidgets.QVBoxLayout()
    outer_layout.setContentsMargins(0, 0, 0, 0)
    outer_layout.setSpacing(LM_SPACING_ROW)
    container.setLayout(outer_layout)

    toggle = QtWidgets.QToolButton()
    toggle.setCheckable(True)
    toggle.setChecked(not collapsed)
    toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
    toggle.setArrowType(QtCore.Qt.DownArrow if toggle.isChecked() else QtCore.Qt.RightArrow)
    toggle.setText(title)
    outer_layout.addWidget(toggle)

    content = QtWidgets.QWidget()
    content_layout = QtWidgets.QVBoxLayout()
    content_layout.setContentsMargins(
        LM_PADDING_SECTION,
        0,
        LM_PADDING_SECTION,
        LM_PADDING_SECTION,
    )
    content_layout.setSpacing(LM_SPACING_ROW)
    content.setLayout(content_layout)
    content.setVisible(toggle.isChecked())
    outer_layout.addWidget(content)

    def _sync_collapsible(checked: bool) -> None:
        toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        content.setVisible(checked)

    toggle.toggled.connect(_sync_collapsible)
    return container, toggle, content, content_layout


def _lm_scrolled_tab(tab: QtWidgets.QWidget, content_layout: QtWidgets.QVBoxLayout) -> None:
    content_layout.setContentsMargins(
        LM_PADDING_CONTAINER,
        LM_PADDING_CONTAINER,
        LM_PADDING_CONTAINER,
        LM_PADDING_CONTAINER,
    )
    content_layout.setSpacing(LM_SPACING_SECTION)
    content_layout.addStretch()
    content = QtWidgets.QWidget()
    content.setLayout(content_layout)
    scroll_area = QtWidgets.QScrollArea()
    scroll_area.setWidgetResizable(True)
    scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
    scroll_area.setWidget(content)
    outer_layout = QtWidgets.QVBoxLayout()
    outer_layout.setContentsMargins(0, 0, 0, 0)
    outer_layout.addWidget(scroll_area)
    tab.setLayout(outer_layout)


def _discover_latest_night_mode_run_dir(root: str) -> Optional[Path]:
    root_path = Path(root).expanduser()
    if not root_path.exists():
        return None
    candidates = [path for path in root_path.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _load_latest_night_mode_run_summary(run_root: str) -> Optional[dict]:
    latest_run_dir = _discover_latest_night_mode_run_dir(run_root)
    if latest_run_dir is None:
        return None
    summary_path = latest_run_dir / NIGHT_MODE_RUN_SUMMARY_FILENAME
    if not summary_path.exists():
        return None
    try:
        with open(summary_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


FB_DRIVER_RECOVERY_MAX_BATCHES = 10
FB_DRIVER_RECOVERY_SUMMARY_KEYS = (
    "candidates_found",
    "retry_attempted",
    "retry_success",
    "fb_email_found",
)
FB_DRIVER_MANUAL_SUMMARY_KEYS = (
    "driver_error_rows",
    "driver_candidates_found",
    "driver_retry_attempted",
    "driver_retry_success",
    "driver_fb_email_found",
)
FB_SHARE_MANUAL_SUMMARY_KEYS = (
    "share_candidates_found",
    "share_resolution_success",
    "share_fb_email_found",
)
FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE = 40
MANUAL_FB_RECOVERY_MAX_BATCHES = 10
MANUAL_FB_SHARE_RECOVERY_SUMMARY_KEYS = (
    "rows_scanned",
    "candidates_found",
    "rows_recovered",
    "rows_skipped",
    "rows_failed",
)


def _coerce_fb_share_recovery_batch_size(value) -> int:
    try:
        batch_size = int(value)
    except Exception:
        return FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE
    return batch_size if batch_size > 0 else FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE


def _parse_fb_driver_recovery_summary(stdout: str) -> Dict[str, str]:
    summary: Dict[str, str] = {}
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        summary[key.strip()] = value.strip()
    return summary


def _parse_recovery_stdout(stdout: str) -> Dict[str, str]:
    summary: Dict[str, str] = {}
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        summary[key.strip()] = value.strip()
    return summary


def _parse_manual_fb_share_recovery_summary(stdout: str) -> Dict[str, str]:
    raw = _parse_recovery_stdout(stdout)
    return {
        key: str(raw[key]).strip()
        for key in MANUAL_FB_SHARE_RECOVERY_SUMMARY_KEYS
        if key in raw
    }


def _fb_driver_recovery_int(summary: Dict[str, str], key: str) -> int:
    try:
        return int(str(summary.get(key, "0") or "0").strip())
    except Exception:
        return 0


def _summary_int(summary: Mapping[str, object], key: str) -> int:
    try:
        return int(str(summary.get(key, "0") or "0").strip())
    except Exception:
        return 0


def _first_summary_int(summary: Mapping[str, object], *keys: str) -> int:
    for key in keys:
        if key in summary:
            return _summary_int(summary, key)
    return 0


def _normalize_manual_fb_share_summary(stdout: str) -> Dict[str, int]:
    raw = _parse_recovery_stdout(stdout)
    return {
        "share_candidates_found": _first_summary_int(raw, "share_candidates_found", "candidates_found", "candidates"),
        "share_resolution_success": _first_summary_int(raw, "share_resolution_success", "resolution_success", "resolved"),
        "share_fb_email_found": _first_summary_int(raw, "share_fb_email_found", "fb_email_found", "enriched"),
    }


def _normalize_manual_fb_driver_summary(stdout: str) -> Dict[str, int]:
    raw = _parse_recovery_stdout(stdout)
    return {
        "driver_error_rows": _summary_int(raw, "driver_error_rows"),
        "driver_candidates_found": _first_summary_int(raw, "driver_candidates_found", "candidates_found"),
        "driver_retry_attempted": _first_summary_int(raw, "driver_retry_attempted", "retry_attempted"),
        "driver_retry_success": _first_summary_int(raw, "driver_retry_success", "retry_success"),
        "driver_fb_email_found": _first_summary_int(raw, "driver_fb_email_found", "fb_email_found"),
    }


def _manual_fb_recovery_final_path(input_csv: str, *, in_place: bool = False) -> str:
    path = Path(input_csv)
    suffix = path.suffix or ".csv"
    name = f"{path.stem}.recovered_temp{suffix}" if in_place else f"{path.stem}.recovered_final{suffix}"
    return str(path.with_name(name))


def _manual_fb_share_recovery_output_path(input_csv: str, *, in_place: bool = False) -> str:
    path = Path(input_csv)
    if in_place:
        return str(path)
    return str(path.with_name(f"{path.stem}.fb_share_recovered{path.suffix or '.csv'}"))


def _validate_manual_fb_share_recovery_csv(input_csv: str) -> Path:
    input_path = Path(str(input_csv or "").strip())
    if not str(input_path):
        raise ValueError("Select a CSV before running FB /share recovery.")
    if input_path.suffix.lower() != ".csv":
        raise ValueError("Input file must be a .csv file.")
    if not input_path.exists():
        raise FileNotFoundError(f"CSV not found: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"Input path is not a file: {input_path}")
    if not os.access(input_path, os.R_OK):
        raise PermissionError(f"CSV is not readable: {input_path}")
    return input_path


def _manual_fb_share_recovery_command(
    input_csv: str,
    *,
    batch_size: int = FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE,
    in_place: bool = False,
    python_executable: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> tuple[list[str], str, Path]:
    input_path = _validate_manual_fb_share_recovery_csv(input_csv)
    batch_size = _coerce_fb_share_recovery_batch_size(batch_size)
    python_executable = python_executable or sys.executable
    base_path = Path(base_dir or os.path.dirname(os.path.abspath(__file__)))
    script_path = base_path / "scripts" / "recover_fb_share_rows.py"
    if not script_path.exists():
        raise FileNotFoundError(f"FB /share recovery script not found: {script_path}")

    output_path = _manual_fb_share_recovery_output_path(str(input_path), in_place=in_place)
    if not in_place and Path(output_path).exists():
        raise FileExistsError(f"Output already exists: {output_path}")
    cmd = [
        python_executable,
        str(script_path),
        "--input",
        str(input_path),
        "--output",
        output_path,
        "--batch-size",
        str(batch_size),
    ]
    return cmd, output_path, base_path


def _run_manual_fb_share_recovery(
    input_csv: str,
    *,
    batch_size: int = FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE,
    in_place: bool = False,
    logger_fn=None,
    runner=None,
    popen_factory=None,
    python_executable: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Dict[str, object]:
    def _log(message: str) -> None:
        if callable(logger_fn):
            logger_fn(message)

    cmd, output_path, base_path = _manual_fb_share_recovery_command(
        input_csv,
        batch_size=batch_size,
        in_place=in_place,
        python_executable=python_executable,
        base_dir=base_dir,
    )
    _log("[FB /share Manual Recovery] started")
    _log(f"input={input_csv}")
    _log(f"output={output_path}")
    _log(f"batch_size={_coerce_fb_share_recovery_batch_size(batch_size)}")
    _log(f"output_mode={'in_place' if in_place else 'copy'}")

    stdout_lines: list[str] = []
    returncode = 0
    if popen_factory is not None:
        process = popen_factory(
            cmd,
            cwd=str(base_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        stream = getattr(process, "stdout", None)
        while stream:
            ready, _, _ = select.select([stream], [], [], 0.5)
            if ready:
                line = stream.readline()
                if line:
                    clean = line.rstrip("\n")
                    stdout_lines.append(clean)
                    if clean.strip():
                        _log(clean)
                    continue
            if process.poll() is not None:
                for line in stream:
                    clean = line.rstrip("\n")
                    stdout_lines.append(clean)
                    if clean.strip():
                        _log(clean)
                break
        returncode = int(process.wait())
    else:
        runner = runner or subprocess.run
        completed = runner(
            cmd,
            cwd=str(base_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        stdout = getattr(completed, "stdout", "") or ""
        stdout_lines = stdout.splitlines()
        for line in stdout_lines:
            if line.strip():
                _log(line.strip())
        returncode = int(getattr(completed, "returncode", 0) or 0)

    stdout = "\n".join(stdout_lines)
    summary = _parse_manual_fb_share_recovery_summary(stdout)
    if returncode != 0:
        raise RuntimeError(f"FB /share recovery failed with code {returncode}")
    for key in MANUAL_FB_SHARE_RECOVERY_SUMMARY_KEYS:
        if key in summary:
            _log(f"{key}={summary[key]}")
    _log("[FB /share Manual Recovery] complete")
    return {
        "output_csv": output_path,
        "summary": summary,
        "stdout": stdout,
        "command": cmd,
    }


def _run_recovery_subprocess(cmd: list[str], *, base_path: Path, runner) -> tuple[str, int]:
    completed = runner(
        cmd,
        cwd=str(base_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return getattr(completed, "stdout", "") or "", int(getattr(completed, "returncode", 0) or 0)


def _run_manual_fb_recovery(
    input_csv: str,
    *,
    recover_share: bool = True,
    recover_driver: bool = True,
    batch_size: int = 40,
    in_place: bool = False,
    dry_run: bool = False,
    logger_fn=None,
    runner=None,
    python_executable: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Dict[str, object]:
    def _log(message: str) -> None:
        if callable(logger_fn):
            logger_fn(message)

    input_path = Path(input_csv)
    if not input_path.exists():
        raise FileNotFoundError(f"CSV not found: {input_csv}")
    if not recover_share and not recover_driver:
        raise ValueError("Select at least one recovery mode.")

    batch_size = max(int(batch_size or FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE), 1)
    runner = runner or subprocess.run
    python_executable = python_executable or sys.executable
    base_path = Path(base_dir or os.path.dirname(os.path.abspath(__file__)))
    share_script = base_path / "scripts" / "recover_fb_share_rows.py"
    driver_script = base_path / "scripts" / "recover_fb_driver_errors.py"
    if recover_share and not share_script.exists():
        raise FileNotFoundError(f"FB /share recovery script not found: {share_script}")
    if recover_driver and not driver_script.exists():
        raise FileNotFoundError(f"FB driver recovery script not found: {driver_script}")

    summary: Dict[str, object] = {
        **{key: 0 for key in FB_SHARE_MANUAL_SUMMARY_KEYS},
        **{key: 0 for key in FB_DRIVER_MANUAL_SUMMARY_KEYS},
        "final_recovered_csv": "<dry-run>" if dry_run else "",
        "batches_run": 0,
        "stopped_reason": "",
        "dry_run": bool(dry_run),
    }
    _log("Manual FB Recovery started")
    _log(f"input_csv={input_path}")

    with tempfile.TemporaryDirectory(prefix="manual_fb_recovery_") as temp_dir:
        temp_path = Path(temp_dir)
        run_input = input_path
        if dry_run:
            run_input = temp_path / input_path.name
            shutil.copy2(input_path, run_input)

        current_input = str(run_input)
        latest_output = current_input

        if recover_share:
            share_output = (
                temp_path / f"{input_path.stem}.share_recovered{input_path.suffix or '.csv'}"
                if dry_run
                else input_path.with_name(f"{input_path.stem}.share_recovered{input_path.suffix or '.csv'}")
            )
            cmd = [
                python_executable,
                str(share_script),
                "--input",
                current_input,
                "--output",
                str(share_output),
                "--limit",
                str(batch_size),
                "--force",
            ]
            stdout, returncode = _run_recovery_subprocess(cmd, base_path=base_path, runner=runner)
            for line in stdout.splitlines():
                if line.strip():
                    _log(f"[Manual FB Share Recovery] {line.strip()}")
            if returncode != 0:
                raise RuntimeError(f"FB /share recovery failed with code {returncode}")
            share_summary = _normalize_manual_fb_share_summary(stdout)
            summary.update(share_summary)
            latest_output = str(share_output)
            current_input = latest_output

        if recover_driver:
            for batch_index in range(1, MANUAL_FB_RECOVERY_MAX_BATCHES + 1):
                driver_output = temp_path / f"{input_path.stem}.driver_recovered_batch{batch_index}{input_path.suffix or '.csv'}"
                cmd = [
                    python_executable,
                    str(driver_script),
                    "--input",
                    current_input,
                    "--output",
                    str(driver_output),
                    "--limit",
                    str(batch_size),
                    "--batch-size",
                    str(batch_size),
                ]
                if dry_run:
                    cmd.append("--dry-run")
                stdout, returncode = _run_recovery_subprocess(cmd, base_path=base_path, runner=runner)
                for line in stdout.splitlines():
                    if line.strip():
                        _log(f"[Manual FB Driver Recovery] {line.strip()}")
                if returncode != 0:
                    raise RuntimeError(f"FB driver recovery batch {batch_index} failed with code {returncode}")
                driver_summary = _normalize_manual_fb_driver_summary(stdout)
                for key in FB_DRIVER_MANUAL_SUMMARY_KEYS:
                    summary[key] = int(summary.get(key, 0) or 0) + int(driver_summary.get(key, 0) or 0)
                summary["batches_run"] = batch_index

                candidates_found = int(driver_summary.get("driver_candidates_found", 0) or 0)
                retry_attempted = int(driver_summary.get("driver_retry_attempted", 0) or 0)
                latest_output = current_input if dry_run else str(driver_output)
                if candidates_found == 0:
                    summary["stopped_reason"] = "candidates_found=0"
                    break
                if retry_attempted == 0:
                    summary["stopped_reason"] = "retry_attempted=0"
                    break
                current_input = str(driver_output)
            else:
                summary["stopped_reason"] = "max_batches"

        if dry_run:
            final_output = "<dry-run>"
        else:
            final_output = _manual_fb_recovery_final_path(str(input_path), in_place=in_place)
            _copy_csv_atomic(latest_output, final_output)
            _validate_recovered_csv_matches_original(str(input_path), final_output)
            if in_place:
                os.replace(final_output, str(input_path))
                final_output = str(input_path)
        summary["final_recovered_csv"] = final_output

    for key in FB_SHARE_MANUAL_SUMMARY_KEYS:
        _log(f"{key}={int(summary.get(key, 0) or 0)}")
    for key in FB_DRIVER_MANUAL_SUMMARY_KEYS:
        _log(f"{key}={int(summary.get(key, 0) or 0)}")
    _log(f"final_recovered_csv={summary['final_recovered_csv']}")
    _log("Manual FB Recovery complete")
    return summary


def _validate_recovered_csv_matches_original(original_csv: str, recovered_csv: str) -> None:
    original = pd.read_csv(original_csv, dtype=str, keep_default_na=False)
    recovered = pd.read_csv(recovered_csv, dtype=str, keep_default_na=False)
    if list(original.columns) != list(recovered.columns):
        raise ValueError("Recovered CSV schema differs from original export.")
    if len(original.index) != len(recovered.index):
        raise ValueError("Recovered CSV row count differs from original export.")


def _copy_csv_atomic(source_csv: str, dest_csv: str) -> None:
    dest_path = Path(dest_csv)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(dest_path.parent), prefix=f".{dest_path.name}.", suffix=".tmp") as tmp:
        tmp_path = tmp.name
        with open(source_csv, "rb") as source:
            shutil.copyfileobj(source, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp_path, dest_csv)


def _run_fb_driver_recovery_chain(
    export_csv: str,
    *,
    batch_size: int = 40,
    in_place: bool = False,
    logger_fn=None,
    runner=None,
    python_executable: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Dict[str, object]:
    def _log(message: str) -> None:
        if callable(logger_fn):
            logger_fn(message)

    export_path = Path(export_csv)
    if not export_path.exists():
        raise FileNotFoundError(f"Final export not found: {export_csv}")
    batch_size = max(int(batch_size or 40), 1)
    runner = runner or subprocess.run
    python_executable = python_executable or sys.executable
    base_path = Path(base_dir or os.path.dirname(os.path.abspath(__file__)))
    script_path = base_path / "scripts" / "recover_fb_driver_errors.py"
    if not script_path.exists():
        raise FileNotFoundError(f"FB recovery script not found: {script_path}")

    _log("FB Driver Recovery started")
    current_input = str(export_path)
    latest_valid_output = str(export_path)
    aggregate = {key: 0 for key in FB_DRIVER_RECOVERY_SUMMARY_KEYS}
    driver_error_rows = 0
    batches_run = 0
    stopped_reason = "max_batches"
    attempted_recovery = False

    for batch_index in range(1, FB_DRIVER_RECOVERY_MAX_BATCHES + 1):
        batch_output = str(export_path.with_name(f"{export_path.stem}.fb_driver_recovered_batch{batch_index}{export_path.suffix or '.csv'}"))
        cmd = [
            python_executable,
            str(script_path),
            "--input",
            current_input,
            "--output",
            batch_output,
            "--limit",
            str(batch_size),
            "--batch-size",
            str(batch_size),
        ]
        completed = runner(
            cmd,
            cwd=str(base_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        stdout = getattr(completed, "stdout", "") or ""
        returncode = int(getattr(completed, "returncode", 0) or 0)
        for line in stdout.splitlines():
            if line.strip():
                _log(f"[FB Driver Recovery] {line.strip()}")
        if returncode != 0:
            raise RuntimeError(f"FB driver recovery batch {batch_index} failed with code {returncode}")

        summary = _parse_fb_driver_recovery_summary(stdout)
        for key in FB_DRIVER_RECOVERY_SUMMARY_KEYS:
            aggregate[key] += _fb_driver_recovery_int(summary, key)
        driver_error_rows += _fb_driver_recovery_int(summary, "driver_error_rows")
        batches_run = batch_index

        candidates_found = _fb_driver_recovery_int(summary, "candidates_found")
        retry_attempted = _fb_driver_recovery_int(summary, "retry_attempted")
        if candidates_found == 0:
            stopped_reason = "candidates_found=0"
            break
        if retry_attempted == 0:
            stopped_reason = "retry_attempted=0"
            break
        _validate_recovered_csv_matches_original(str(export_path), batch_output)
        attempted_recovery = True
        latest_valid_output = batch_output
        current_input = batch_output
    else:
        _log(f"[FB Driver Recovery] Warning: max_batches reached ({FB_DRIVER_RECOVERY_MAX_BATCHES}); keeping latest output.")

    if not attempted_recovery:
        final_output = str(export_path)
        _log("No FB driver recovery needed")
    elif stopped_reason in {"candidates_found=0", "retry_attempted=0"}:
        final_output = latest_valid_output
        _log("No FB driver recovery needed")
    elif in_place:
        final_output = str(export_path.with_name(f"{export_path.stem}.recovered_temp{export_path.suffix or '.csv'}"))
        _copy_csv_atomic(latest_valid_output, final_output)
        _validate_recovered_csv_matches_original(str(export_path), final_output)
        os.replace(final_output, str(export_path))
        final_output = str(export_path)
    else:
        final_output = str(export_path.with_name(f"{export_path.stem}.recovered_final{export_path.suffix or '.csv'}"))
        _copy_csv_atomic(latest_valid_output, final_output)
        _validate_recovered_csv_matches_original(str(export_path), final_output)

    _log(f"driver_error_rows={driver_error_rows}")
    for key in FB_DRIVER_RECOVERY_SUMMARY_KEYS:
        _log(f"driver_{key}={aggregate[key]}")
    _log(f"final_recovered_csv={final_output}")
    _log("FB Driver Recovery complete")
    return {
        "final_recovered_csv": final_output,
        "batches_run": batches_run,
        "stopped_reason": stopped_reason,
        "driver_error_rows": driver_error_rows,
        **{f"driver_{key}": aggregate[key] for key in FB_DRIVER_RECOVERY_SUMMARY_KEYS},
    }


cross_directory_enricher = None
try:
    import cross_directory_enricher
except ImportError:
    cross_directory_enricher = None

# ---------------------------
# Bandcamp Configuration
# ---------------------------
BANDCAMP_SEED_TAGS = ["united-kingdom", "london", "manchester", "brighton", "leeds", "bristol", "glasgow"]
BANDCAMP_PAGES_PER_TAG = 5
BANDCAMP_MIN_CONTACT_REQUIREMENT = True
BANDCAMP_DEFAULT_TAG_URL = "https://bandcamp.com/tag/united-kingdom"
BANDCAMP_LOCATION_FALLBACK = False
BANDCAMP_TARGET_ROWS = 40
BANDCAMP_MAX_CANDIDATES = 120
BANDCAMP_DISCOVER_PAGES_DEFAULT = 5
UNEARTHED_DEFAULT_URL = "https://www.abc.net.au/triplejunearthed/music/"

# ---------------------------
# SoundCloud Configuration
# ---------------------------
SOUNDCLOUD_SEED_TAGS = ["indie", "rock", "electronic", "hip-hop", "pop", "alternative", "singer-songwriter", "punk", "garage", "ambient"]
SOUNDCLOUD_PAGES_PER_TAG = 5
SOUNDCLOUD_MIN_CONTACT_REQUIREMENT = True
SOUNDCLOUD_DEFAULT_TAG_URL = "https://soundcloud.com/tags/indie"

# ---------------------------
# SoundCloud FAST mode (fb/email focus)
# ---------------------------
SOUNDCLOUD_FAST_FACEBOOK_EMAIL_ONLY = True
SOUNDCLOUD_FAST_TIMEOUT_SEC = 10
SOUNDCLOUD_FAST_MAX_CANDIDATES = 600
SC_HANDLE_RE = re.compile(r"^https?://soundcloud\.com/([a-z0-9][a-z0-9._-]{1,49})/?$", re.IGNORECASE)
SC_HANDLE_BAN = {
    "feed", "upload", "terms-of-use", "imprint", "transparency-reports", "pages",
    "you", "stream", "discover", "explore", "popular"
}
SC_SOCIAL_SELECTORS = [
    'a[href^="mailto:"]',
    'a[href*="instagram.com"]',
    'a[href*="facebook.com"]',
    'a[href*="linktr.ee"]',
    'a[href*="bandcamp.com"]',
    'a[href*="youtube.com"]',
    'a[href*="tiktok.com"]',
    'a[href*="twitter.com"]',
    'a[href*="x.com"]',
    'a[href*="beacons.ai"]',
    'a[href*="carrd.co"]',
    'a[href*="flow.page"]',
    'a[href*="solo.to"]',
    'a[href*="hypeddit.com"]',
    'a[href*="toneden.io"]',
]
SC_AGGREGATOR_ALLOWLIST = ("linktr.ee", "beacons.ai", "solo.to", "hypeddit.com", "toneden.io")
SC_AGGREGATOR_HOSTS = SC_AGGREGATOR_ALLOWLIST
SC_AGGREGATOR_PREFERENCE = (
    "linktr.ee",
    "beacons.ai",
    "solo.to",
    "hypeddit.com",
    "toneden.io",
    "bandcamp.com",
    "carrd.co",
    "flow.page",
)
SC_REQUEST_TIMEOUT = (5, 10)
SC_SEARCH_USERS_API = "https://api-v2.soundcloud.com/search/users"
SC_MAX_WORKERS = 8
SC_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soundcloud_about_cache.json")
SC_CACHE_MAX_AGE_DAYS = 7
SC_DEBUG_LATEST = bool(os.getenv("SC_DEBUG_LATEST"))
SC_ADAPTIVE_ABOUT_DISABLE = bool(os.getenv("SC_ADAPTIVE_ABOUT_DISABLE"))
SC_ABOUT_CHALLENGE_WINDOW = int(os.getenv("SC_ABOUT_CHALLENGE_WINDOW", "5"))
SC_ABOUT_CHALLENGE_THRESHOLD = float(os.getenv("SC_ABOUT_CHALLENGE_THRESHOLD", "0.60"))

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
]

SC_HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


# ---------------------------
# Last.fm Configuration
# ---------------------------
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()
LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
LASTFM_MAX_SIMILAR_PER_SEED = 200  # soft ceiling per seed
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_ACCESS_TOKEN = ""
_SPOTIFY_ACCESS_TOKEN_EXPIRY = 0.0
_SPOTIFY_TOKEN_LOCK = threading.Lock()
SPOTIFY_ARTIST_GENRE_CACHE: dict[str, list[str]] = {}
_SPOTIFY_CACHE_LOCK = threading.Lock()


def _rand_headers():
    headers = dict(SC_HEADERS_BASE)
    headers["User-Agent"] = random.choice(UAS)
    return headers


def build_hardened_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=False,
    )
    adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(_rand_headers())
    return session


def polite_sleep(min_ms=120, max_ms=240):
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


def _sc_stat_inc(key: str, n: int = 1):
    global _SC_RUN_STATS
    if _SC_RUN_STATS is None:
        return
    with _SC_RUN_LOCK:
        _SC_RUN_STATS[key] = int(_SC_RUN_STATS.get(key, 0)) + int(n)

SC_LINK_BATCH_SIZE = 25
SYMBOL_CAT = {"So", "Cs"}
_SC_GENRE_DENY = {"melbourne", "naarm", "australia"}
CONFIG = {}
SC_ABOUT_FIRST = CONFIG.get("SC_ABOUT_FIRST", True)
SC_EXPAND_1HOP = CONFIG.get("SC_EXPAND_1HOP", True)
_SC_ROOT_FORBIDDEN = False
_SC_ROOT_FORBIDDEN_LOGGED = False
_SC_RSS_DEBUG_LOGGED = False
_SC_CLIENT_ID_LOCK = threading.Lock()
_SC_CLIENT_ID = None
_SC_HANDLE_UID_MAP = {}
_SC_HANDLE_USEROBJ_MAP = {}
_SC_RUN_LOCK = threading.Lock()
_SC_RUN_STATS = None
_SC_ABOUT_DISABLED = False
_SC_ABOUT_DISABLE_LOGGED = False
_SC_ENGINE = SoundCloudEngine()
SC_CLIENT_ID_CANDIDATES = ["MaZ7bR62GvbulJgV8EUjQnHfbZGDEKaI"]
SOCIAL_HOSTS = (
    "linktr.ee", "beacons.ai", "bandcamp.com", "carrd.co", "flow.page",
    "instagram.com", "facebook.com", "x.com", "twitter.com", "youtube.com", "tiktok.com",
    "soundcloud.com"
)
SOCIAL_HOSTS_PATTERN = r"(?:linktr\.ee|beacons\.ai|solo\.to|hypeddit\.com|toneden\.io|bandcamp\.com|carrd\.co|flow\.page|instagram\.com|facebook\.com|x\.com|twitter\.com|youtube\.com|tiktok\.com)"
URL_RE = re.compile(rf"https?://{SOCIAL_HOSTS_PATTERN}[^\s\"'<)]+", re.IGNORECASE)
_SOCIAL_TEXT_RE = re.compile(
    rf"((?:https?://|www\.)?{SOCIAL_HOSTS_PATTERN}[^\s\"'<)]+)",
    re.IGNORECASE,
)
_BANDCAMP_HANDLE_HINTS = (
    (
        re.compile(r"(?:instagram|ig)\s*[:\-]?\s*@?([a-z0-9._]{3,})", re.IGNORECASE),
        "https://instagram.com/{handle}",
    ),
    (
        re.compile(r"(?:twitter|x)\s*[:\-]?\s*@?([a-z0-9_]{3,})", re.IGNORECASE),
        "https://twitter.com/{handle}",
    ),
    (
        re.compile(r"(?:tiktok)\s*[:\-]?\s*@?([a-z0-9._]{3,})", re.IGNORECASE),
        "https://www.tiktok.com/@{handle}",
    ),
)
HANDLE_RE = re.compile(r"^/[a-z0-9][a-z0-9._-]{1,49}$", re.IGNORECASE)
SC_DISCOVERY_BAN = {
    "feed", "upload", "terms-of-use", "imprint", "transparency-reports", "pages",
    "you", "stream", "discover", "explore", "popular"
}
COUNTRY_CODE_OVERRIDES = {
    "au": "Australia",
    "us": "United States",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "ca": "Canada",
    "nz": "New Zealand",
    "de": "Germany",
    "fr": "France",
    "es": "Spain",
    "it": "Italy",
    "ie": "Ireland",
    "se": "Sweden",
    "no": "Norway",
    "fi": "Finland",
    "dk": "Denmark",
    "nl": "Netherlands",
    "be": "Belgium",
    "br": "Brazil",
    "mx": "Mexico",
    "jp": "Japan"
}

# -----------------------------------------------------------------------------
# Helper: URL Normalization
# -----------------------------------------------------------------------------
def normalize_url(url):
    """Normalize a URL by stripping trailing slashes and converting to lowercase."""
    return url.rstrip('/').lower()


_PARSER_USED = None


def get_soup(html: str):
    """Prefer lxml; fallback to html.parser if lxml is unavailable."""
    global _PARSER_USED
    try:
        soup = BS(html or "", "lxml")
        if _PARSER_USED is None:
            _PARSER_USED = "lxml"
            print("[init] BeautifulSoup parser=lxml")
        return soup
    except FeatureNotFound:
        soup = BS(html or "", "html.parser")
        if _PARSER_USED is None:
            _PARSER_USED = "html.parser"
            print("[init] BeautifulSoup parser=html.parser (fallback)")
        return soup


def _strip_tracking(u: str) -> str:
    u = re.sub(r"[?&](?:utm_[^=&]+|fbclid|gclid|mc_cid|mc_eid)=[^&]+", "", u, flags=re.IGNORECASE)
    u = re.sub(r"[?&]$", "", u)
    return u


def normalize_external_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    try:
        parsed = urlparse(u)
        host = (parsed.hostname or "").lower()
        if host.endswith("l.soundcloud.com"):
            qs = parse_qs(parsed.query or "")
            target = qs.get("url") or qs.get("q") or []
            if target:
                candidate = unquote(target[0])
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                u = candidate
    except Exception:
        pass
    return _strip_tracking(u)


SC_ASSET_JS_PATTERN = re.compile(r"https://a-v2\.sndcdn\.com/assets/\d+-[a-z0-9]+\.js", re.IGNORECASE)
SC_CLIENT_ID_PATTERN = re.compile(r'client_id:"([a-zA-Z0-9]+)"')


def _sc_test_client_id(session, candidate: str) -> bool:
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": "https://soundcloud.com/soundcloud", "client_id": candidate},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        return resp.status_code == 200
    except Exception:
        return False


def _sc_scrape_client_id(session) -> str:
    sources = [
        "https://soundcloud.com",
        "https://soundcloud.com/discover",
    ]
    for source in sources:
        try:
            resp = session.get(source, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
            resp.raise_for_status()
        except Exception:
            continue
        assets = SC_ASSET_JS_PATTERN.findall(resp.text or "")
        for asset_url in assets[:20]:
            try:
                js_resp = session.get(asset_url, timeout=SC_REQUEST_TIMEOUT)
                js_resp.raise_for_status()
                match = SC_CLIENT_ID_PATTERN.search(js_resp.text or "")
                if match:
                    return match.group(1)
            except Exception:
                continue
    return ""

def _dedupe_song_title_value(value: str) -> str:
    """
    Normalise a Song Title cell that may contain multiple entries (comma/pipe/semicolon separated).
    Removes duplicates while preserving order.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    parts = re.split(r"[|;,]+", text)
    seen = set()
    cleaned = []
    for part in parts:
        token = part.strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned)


def _sc_get_client_id(session) -> str:
    global _SC_CLIENT_ID
    with _SC_CLIENT_ID_LOCK:
        if _SC_CLIENT_ID:
            return _SC_CLIENT_ID
        for candidate in SC_CLIENT_ID_CANDIDATES:
            if _sc_test_client_id(session, candidate):
                _SC_CLIENT_ID = candidate
                print(f"[init] SoundCloud client_id={candidate[:6]}… (predefined)")
                return _SC_CLIENT_ID
        scraped = _sc_scrape_client_id(session)
        if scraped:
            _SC_CLIENT_ID = scraped
            print(f"[init] SoundCloud client_id={scraped[:6]}… (scraped)")
            return _SC_CLIENT_ID
    print("[warn] Unable to acquire SoundCloud client_id.")
    return ""


def _safe_bs(html: str, parser: str = "lxml"):
    if parser == "lxml":
        return get_soup(html)
    try:
        return BS(html, parser)
    except Exception:
        return get_soup(html)


def _extract_handles_generic(html: str):
    doc = get_soup(html)
    handles = []
    for anchor in doc.select('a[href^="/"]'):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if not HANDLE_RE.match(href):
            continue
        slug = href.strip("/").lower()
        if slug in SC_DISCOVERY_BAN:
            continue
        handles.append(slug)
    seen = set()
    ordered = []
    for handle in handles:
        if handle not in seen:
            seen.add(handle)
            ordered.append(handle)
    return ordered


def scrape_handles_from_people_search(session, url: str, limit=None):
    query, place = sc_parse_people_search_url(url)
    max_results = limit if isinstance(limit, int) and limit > 0 else 50
    handles = _SC_ENGINE.people_search(query=query, place=place, max_results=max_results)
    if limit and isinstance(limit, int) and limit > 0:
        handles = handles[:limit]
    return handles


def _sc_fetch_people_search_api(session, url: str, limit=None, place_filter: str = "") -> list:
    if not url:
        return []
    client_id = _sc_get_client_id(session)
    if not client_id:
        return []
    parsed = urlparse(url)
    query_params = parse_qs((parsed.query or ""))
    raw_query = (query_params.get("q", [""])[0] or "").strip()
    query_limit = None
    if query_params.get("limit"):
        try:
            query_limit = int(query_params["limit"][0])
        except (ValueError, TypeError):
            query_limit = None
    target_cap = limit if isinstance(limit, int) and limit > 0 else query_limit
    if not target_cap or target_cap <= 0:
        target_cap = 50
    target_cap = max(1, min(target_cap, 500))
    offset = 0
    if query_params.get("offset"):
        try:
            offset = int(query_params["offset"][0])
        except (ValueError, TypeError):
            offset = 0
    passthrough = {}
    for key, values in query_params.items():
        if not values:
            continue
        value = values[0]
        if key in {"q", "limit", "offset"}:
            continue
        passthrough[key] = value
    handles = []
    place_filter = (place_filter or "").strip()
    while len(handles) < target_cap:
        batch_limit = min(50, target_cap - len(handles))
        params = {
            "client_id": client_id,
            "linked_partitioning": 1,
            "limit": batch_limit,
            "offset": offset,
        }
        if raw_query:
            params["q"] = raw_query
        params.update(passthrough)
        resp = session.get(
            SC_SEARCH_USERS_API,
            params=params,
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        collection = data.get("collection") or []
        for item in collection:
            handle = (item.get("permalink") or "").strip().lower()
            if _sc_handle_ok(handle):
                if place_filter:
                    city = (item.get("city") or "").strip()
                    country = (item.get("country") or item.get("country_code") or item.get("country_name") or "").strip()
                    location_text = " ".join(part for part in (city, country) if part)
                    if not _sc_location_matches_filter(location_text, place_filter):
                        continue
                handles.append(handle)
                if len(handles) >= target_cap:
                    break
        if len(handles) >= target_cap:
            break
        next_href = data.get("next_href")
        if not next_href or not collection:
            break
        try:
            parsed_next = urlparse(next_href)
            query_next = parse_qs(parsed_next.query or "")
            offset = int(query_next.get("offset", [offset + batch_limit])[0])
        except Exception:
            offset += batch_limit
    return handles


def _sc_handles_from_people_page(driver, url: str, limit=None) -> list:
    """
    Load a SoundCloud people search page with Selenium and extract handles
    in the order they appear.
    """
    handles = []
    if not url:
        return handles
    print(f"[dbg] fetching people search via browser: {url}")
    try:
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        polite_sleep()
        html = driver.page_source
        handles = _extract_handles_generic(html)
        if limit and isinstance(limit, int) and limit > 0:
            handles = handles[:limit]
        print(f"SoundCloud: people search (browser) -> {len(handles)} handles")
    except Exception as exc:
        print(f"SoundCloud: browser people search fetch failed ({exc})")
    return handles
def sc_fetch_people_handles_v2(query: Optional[str], place: Optional[str], client_id: str, session, logger=None,
                               max_results: int = 50) -> list:
    """
    Fetch handles from SoundCloud v2 people search API, honoring filter.place when provided.
    """
    if logger is None:
        class _PrintLogger:
            def info(self, msg, *args, **kwargs):
                try:
                    print(msg % args if args else msg)
                except Exception:
                    print(msg)
            def warning(self, msg, *args, **kwargs):
                try:
                    print(msg % args if args else msg)
                except Exception:
                    print(msg)
            def error(self, msg, *args, **kwargs):
                try:
                    print(msg % args if args else msg)
                except Exception:
                    print(msg)
        logger = _PrintLogger()
    handles: list = []
    if not query:
        logger.warning("SoundCloud: v2 people search called without query; returning empty handle list")
        return handles
    base_url = "https://api-v2.soundcloud.com/search/users"
    offset = 0
    page_size = min(50, max_results if max_results > 0 else 50)
    params_base = {
        "q": query,
        "client_id": client_id,
        "limit": page_size,
        "linked_partitioning": 1,
    }
    if place:
        params_base["filter.place"] = place
        params_base["facet"] = "place"
    logger.info(
        "SoundCloud: v2 people search API -> query='%s' place='%s' max_results=%d",
        query, place, max_results,
    )
    while True:
        params = dict(params_base)
        params["offset"] = offset
        try:
            resp = session.get(base_url, params=params, timeout=(6, 12), headers=_rand_headers())
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("SoundCloud: v2 people search API error: %s", exc, exc_info=True)
            break
        collection = data.get("collection") or []
        for item in collection:
            if not isinstance(item, dict):
                continue
            handle = (item.get("permalink") or item.get("username") or "").strip()
            uid_raw = item.get("id") or item.get("urn") or ""
            if not _sc_handle_ok(handle):
                continue
            if uid_raw:
                _SC_HANDLE_UID_MAP[handle] = uid_raw
            if isinstance(item, dict):
                _SC_HANDLE_USEROBJ_MAP[handle] = item
                if SC_DEBUG_LATEST:
                    print(f"[scdbg] userobj cached handle={handle} keys={len(item.keys())}")
            if place:
                place_clean = place.strip().lower()
                city = (item.get("city") or "").strip()
                country = (item.get("country") or item.get("country_code") or item.get("country_name") or "").strip()
                location_text = " ".join(part for part in (city, country) if part).lower()
                if place_clean and place_clean not in location_text:
                    continue
            handles.append(handle)
            if max_results and len(handles) >= max_results:
                break
        logger.info(
            "SoundCloud: v2 people search page -> %d new handles (total=%d)",
            len(collection), len(handles),
        )
        if max_results and len(handles) >= max_results:
            break
        next_href = data.get("next_href")
        if not next_href:
            break
        try:
            parsed_next = urlparse(next_href)
            qs_next = parse_qs(parsed_next.query or "")
            offset = int(qs_next.get("offset", [offset + page_size])[0])
        except Exception:
            offset += page_size
        polite_sleep()
    # Deduplicate preserving order
    deduped = []
    seen = set()
    for h in handles:
        if h in seen:
            continue
        seen.add(h)
        deduped.append(h)
    logger.info(
        "SoundCloud: v2 people search API -> %d unique handles (query='%s' place='%s')",
        len(deduped), query, place,
    )
    return deduped


def scrape_handles_from_tag_page(session, url: str):
    resp = session.get(url, timeout=(6, 12), headers=_rand_headers())
    resp.raise_for_status()
    polite_sleep()
    return _extract_handles_generic(resp.text)


def discover_handles(session, source_url: str, limit=None):
    if not source_url:
        return []
    lowered = source_url.lower()
    if "/search/people" in lowered:
        query, place = sc_parse_people_search_url(source_url)
        print(f"SoundCloud: people search detected -> query='{query}' place='{place}' (using v2 API)")
        handles = _SC_ENGINE.people_search(query=query, place=place, max_results=limit or 50)
        print(f"SoundCloud: people search -> {len(handles)} handles (query='{query}' place='{place}')")
        return handles
    if "/tags/" in lowered:
        return scrape_handles_from_tag_page(session, source_url)
    match = re.match(r"^https?://soundcloud\.com/([a-z0-9][a-z0-9._-]{1,49})/?$", source_url, re.IGNORECASE)
    if match:
        return [match.group(1).lower()]
    return []

def _ensure_parent_dir(path: str):
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
    except Exception:
        pass


def _atomic_write_dataframe(df: pd.DataFrame, path: str):
    """Write DataFrame atomically to path via temp file + fsync."""
    target = Path(path)
    _ensure_parent_dir(str(target))
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8-sig") as handle:
            df.to_csv(handle, index=False)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp_path, target)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise


def _safe_atomic_write_csv(df, path: str, fallback_columns: list[str], reason: str = ""):
    """
    Pandas writes a lone newline when a DataFrame has zero columns; guard with fallback headers.
    Always writes via atomic temp+replace to avoid partial files.
    """
    if df is None:
        df = pd.DataFrame(columns=fallback_columns)
    elif not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)
    # Avoid truthiness on Index (bool(Index) raises ValueError); check length explicitly.
    cols = getattr(df, "columns", None)
    if cols is None or len(cols) == 0:
        df = pd.DataFrame(columns=fallback_columns)
    print(f"[CSV WRITE]{' ' + reason if reason else ''} rows={len(df)} cols={len(df.columns)} path={path}")
    _atomic_write_dataframe(df, path)

def _write_empty_csv_with_headers(path: str):
    _ensure_parent_dir(path)
    headers = [
        'Artist Name', 'Location', 'Song Title', 'Sounds Like', 'Social Link', 'SoundCloud Link',
        'Played on triple J', 'Played on Unearthed', 'Release Date', 'Primary Genre', 'Date Added', 'Email',
        'Lead_Source', 'Source_Directory', 'Source Directory'
    ]
    pd.DataFrame(columns=headers).to_csv(path, index=False, encoding="utf-8-sig")


# -----------------------------------------------------------------------------
# Last.fm Helpers
# -----------------------------------------------------------------------------
_lastfm_session = None


def _lastfm_get_session():
    global _lastfm_session
    if _lastfm_session is None:
        _lastfm_session = requests.Session()
        _lastfm_session.headers.update({
            "User-Agent": "LeadMachine/1.0 (+https://outwiththein.com)"
        })
        _lastfm_session.timeout = 15
    return _lastfm_session


def _lastfm_api_get(method, params):
    """
    Call Last.fm API and return parsed JSON or {} on error.
    """
    if not LASTFM_API_KEY:
        return {}
    session = _lastfm_get_session()
    payload = dict(params or {})
    payload["method"] = method
    payload["api_key"] = LASTFM_API_KEY
    payload["format"] = "json"
    try:
        resp = session.get(LASTFM_API_BASE, params=payload, timeout=15)
        resp.raise_for_status()
        return resp.json() or {}
    except Exception as e:
        print(f"Last.fm: API call failed for {method}: {e}")
        return {}


_LASTFM_EXCLUDED_HOSTS = {
    "www.last.fm", "last.fm", "www.lastfm.com", "lastfm.com",
    "support.last.fm", "help.last.fm", "forum.last.fm"
}
_LASTFM_PLACEHOLDER_TOKENS = ("lastfm", "last_fm", "last-fm", "last.fm")


def _lastfm_is_placeholder_social(parsed):
    target = f"{parsed.netloc}{parsed.path or ''}".lower()
    return any(token in target for token in _LASTFM_PLACEHOLDER_TOKENS)


def _lastfm_extract_socials_and_website(html, profile_url):
    """
    Parse a Last.fm artist HTML page and extract:
      - website (first non-social external link)
      - socials dict: instagram, facebook, twitter, youtube, linktree, spotify, bandsintown, songkick
      - best_guess_location (if any obvious location text is found)
    """
    soup = BeautifulSoup(html, "html.parser")
    socials = {
        "instagram": "",
        "facebook": "",
        "twitter": "",
        "youtube": "",
        "linktree": "",
        "spotify": "",
        "bandsintown": "",
        "songkick": ""
    }
    website = ""
    location = ""

    possible_loc = soup.find(class_=re.compile("location", re.I)) or soup.find("p", class_=re.compile("header-metadata", re.I))
    if possible_loc:
        txt = possible_loc.get_text(" ", strip=True)
        if txt and len(txt) < 80:
            location = txt

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(profile_url, href)

        parsed = urlparse(href)
        scheme = (parsed.scheme or "").lower()
        if not scheme.startswith("http"):
            continue
        host = (parsed.netloc or "").lower()
        if host in _LASTFM_EXCLUDED_HOSTS:
            continue

        if _lastfm_is_placeholder_social(parsed):
            continue

        if "instagram.com" in host:
            socials["instagram"] = socials["instagram"] or href
        elif "facebook.com" in host or "fb.me" in host:
            socials["facebook"] = socials["facebook"] or href
        elif "twitter.com" in host or "x.com" in host:
            socials["twitter"] = socials["twitter"] or href
        elif "youtube.com" in host or "youtu.be" in host:
            socials["youtube"] = socials["youtube"] or href
        elif any(t in host for t in ["linktr.ee", "linktree", "withkoji.com", "beacons.ai"]):
            socials["linktree"] = socials["linktree"] or href
        elif "spotify.com" in host:
            socials["spotify"] = socials["spotify"] or href
        elif "bandsintown.com" in host:
            socials["bandsintown"] = socials["bandsintown"] or href
        elif "songkick.com" in host:
            socials["songkick"] = socials["songkick"] or href
        else:
            if not website:
                website = href

    return website, socials, location


# -----------------------------------------------------------------------------
# ChromeDriver bootstrap with cache self-healing
# -----------------------------------------------------------------------------
def _purge_wdm_cache(driver_path: str) -> None:
    """
    Remove the webdriver_manager cache folder for a given driver path.
    Intended for rare cases where the cached binary is corrupted or unsigned.
    """
    try:
        path = Path(driver_path).resolve()
        cache_dir = path.parent.parent
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:
        pass


_ACTIVE_DRIVERS = weakref.WeakSet()


def _register_driver_cleanup(driver) -> None:
    try:
        _ACTIVE_DRIVERS.add(driver)
    except Exception:
        pass


def _shutdown_all_drivers() -> None:
    for drv in list(_ACTIVE_DRIVERS):
        try:
            drv.quit()
        except Exception:
            pass
    _ACTIVE_DRIVERS.clear()


atexit.register(_shutdown_all_drivers)


def _start_chromedriver_with_retry(chrome_options):
    """
    Start ChromeDriver with one automatic cache purge + reinstall retry if startup fails.
    """
    last_exc = None
    for _ in range(2):
        try:
            driver = webdriver.Chrome(options=chrome_options)
            _register_driver_cleanup(driver)
            return driver
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("Failed to start ChromeDriver.")


# -----------------------------------------------------------------------------
# Helper: Drum Status Detection from Page Source using BeautifulSoup
# -----------------------------------------------------------------------------
def get_drum_status_from_source(page_source):
    """
    Parses the page source to determine drum status for the most recent song release.

    Current live Unearthed profiles expose the release status under a visible "Played on:"
    label. Anchor to that local block, then inspect the adjacent list / accessible text for
    the first release card only.
    """
    soup = BeautifulSoup(page_source, 'html.parser')

    def _direct_child_tags(tag):
        if not tag or not getattr(tag, "children", None):
            return []
        return [child for child in tag.children if getattr(child, "name", None)]

    def _next_tag_sibling(tag):
        if not tag:
            return None
        sibling = tag.next_sibling
        while sibling is not None and not getattr(sibling, "name", None):
            sibling = sibling.next_sibling
        return sibling

    def _first_direct_list(container):
        if not container or not getattr(container, "name", None):
            return None
        if getattr(container, "name", "") in {"ul", "ol"} and container.find("li", recursive=False):
            return container
        for child in _direct_child_tags(container):
            if child.name in {"ul", "ol"} and child.find("li", recursive=False):
                return child
            for grandchild in _direct_child_tags(child):
                if grandchild.name in {"ul", "ol"} and grandchild.find("li", recursive=False):
                    return grandchild
        return None

    played_on_label = soup.find(
        lambda t: getattr(t, "name", "") in {"div", "p", "span", "strong", "h3", "h4"}
        and re.sub(r"\s+", " ", t.get_text(" ", strip=True)).strip().rstrip(":").lower() == "played on"
    )
    if played_on_label:
        label_parent = played_on_label.parent if getattr(played_on_label, "parent", None) else None
        played_on_list = None
        for candidate in (
            _next_tag_sibling(played_on_label),
            _first_direct_list(label_parent),
            _next_tag_sibling(label_parent),
        ):
            played_on_list = _first_direct_list(candidate)
            if played_on_list:
                break

        if played_on_list:
            li = played_on_list.find("li", attrs={"data-component": "ListItem"}) or played_on_list.find("li")
            if li:
                sr_span = li.find("span", attrs={"data-component": "ScreenReaderOnly"})
                if sr_span:
                    text = sr_span.get_text().strip().lower()
                    if "unearthed" in text:
                        return "triple j unearthed"
                    if "triple j" in text:
                        return "triple j"
                li_text = li.get_text(" ", strip=True).lower()
                if "unearthed" in li_text:
                    return "triple j unearthed"
                if "triple j" in li_text:
                    return "triple j"
                drum_svg = li.find("svg", attrs={"data-component": "TripleJDrum"})
                if drum_svg:
                    return "triple j"
    return ""

# -----------------------------------------------------------------------------
# General Driver Setup for Artist Scraping (Headless)
# -----------------------------------------------------------------------------
def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1440,900")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--lang=en-US,en")
    chrome_options.page_load_strategy = 'eager'
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    driver = _start_chromedriver_with_retry(chrome_options)
    try:
        driver.set_page_load_timeout(35)
        driver.set_script_timeout(35)
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        })
    except Exception:
        pass
    return driver

# -----------------------------------------------------------------------------
# Facebook Driver Setup (Visible / Head Mode with Optimizations)
# -----------------------------------------------------------------------------
def setup_facebook_driver():
    chrome_options = Options()
    # Run in visible mode for Facebook scraping.
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.page_load_strategy = 'eager'
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    driver = _start_chromedriver_with_retry(chrome_options)
    return driver


# -----------------------------------------------------------------------------
# Unearthed Page 1 configuration
# -----------------------------------------------------------------------------
# Unearthed Page 1 should only collect socials; email scraping happens in later passes.
SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1 = False
UNEARTHED_LOAD_MORE_MAX_ATTEMPTS = 3
UNEARTHED_LOAD_MORE_XPATH = (
    "//button[contains(translate(normalize-space(.), "
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'load more')]"
)


# =============================================================================
# Scraping Functions for Artist Data (Page 1)
# =============================================================================
def _unearthed_cursor_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "overnight_runs",
        "unearthed_cursor.json",
    )


UNEARTHED_ARTIST_URL_INDEX_COLUMNS = [
    "artist_url",
    "artist_slug",
    "first_seen_at",
    "last_seen_at",
    "source",
]

UNEARTHED_CUSTOM_INDEX_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "indexes",
    "unearthed",
)


def _unearthed_artist_url_index_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data",
        "unearthed_artist_url_index.csv",
    )


def _resolve_unearthed_url_index_path(job_config: dict | None = None) -> str:
    configured = ""
    if isinstance(job_config, dict):
        configured = str(job_config.get("unearthed_url_index_path") or "").strip()
    return configured or _unearthed_artist_url_index_path()


def _ue_startup_log(message: str) -> None:
    try:
        print(f"[UE Startup] {message}")
    except Exception:
        pass


def _ue_startup_warn(message: str) -> None:
    try:
        print(f"[UE Startup][WARN] {message}")
    except Exception:
        pass


def _ue_startup_elapsed(started_at: float) -> float:
    try:
        return max(time.time() - float(started_at), 0.0)
    except Exception:
        return 0.0


def _ue_startup_warn_if_slow(step: str, elapsed_sec: float, threshold_sec: float) -> None:
    try:
        if float(elapsed_sec) > float(threshold_sec):
            _ue_startup_warn(f"slow_step step={step} elapsed_sec={float(elapsed_sec):.3f}")
    except Exception:
        pass


def _ensure_unearthed_custom_index_dir() -> str:
    os.makedirs(UNEARTHED_CUSTOM_INDEX_DIR, exist_ok=True)
    return UNEARTHED_CUSTOM_INDEX_DIR


def _validate_unearthed_index_access(index_path: str, *, require_existing: bool = True) -> None:
    path = str(index_path or "").strip()
    if not path:
        raise ValueError("Unearthed URL index path is empty.")
    if not path.lower().endswith(".csv"):
        raise ValueError(f"Unearthed URL index must be a .csv file: {path}")
    if require_existing and not os.path.exists(path):
        raise FileNotFoundError(f"Unearthed URL index file not found: {path}")
    if os.path.exists(path):
        if not os.path.isfile(path):
            raise OSError(f"Unearthed URL index path is not a file: {path}")
        with open(path, "r", encoding="utf-8-sig", newline=""):
            pass
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"Unearthed URL index directory not found: {parent}")
    probe_path = os.path.join(parent, f".{os.path.basename(path)}.write_test")
    try:
        with open(probe_path, "w", encoding="utf-8") as handle:
            handle.write("")
    finally:
        if os.path.exists(probe_path):
            os.remove(probe_path)


def _write_empty_unearthed_artist_url_index(index_path: str) -> None:
    _write_unearthed_artist_url_index([], index_path=index_path, require_existing=False)


def normalize_unearthed_artist_url(value: str | None) -> tuple[str, str]:
    raw_value = value.strip() if isinstance(value, str) else ""
    if not raw_value:
        return "", ""
    parsed = urlparse(raw_value)
    if not (parsed.scheme or parsed.netloc or raw_value.startswith("/")):
        slug = raw_value.strip("/").split("?", 1)[0].split("#", 1)[0].strip().lower()
        slug = re.sub(r"\s+", "-", slug)
        if not slug:
            return "", ""
        return f"https://www.abc.net.au/triplejunearthed/artist/{slug}", slug
    path = (parsed.path or "").rstrip("/")
    slug_match = re.search(r"/triplejunearthed/artist/([^/]+)$", path, flags=re.IGNORECASE)
    if not slug_match:
        slug_match = re.search(r"/artist/([^/]+)$", path, flags=re.IGNORECASE)
    if not slug_match:
        return "", ""
    slug = unquote(slug_match.group(1)).strip().lower()
    slug = re.sub(r"\s+", "-", slug)
    if not slug:
        return "", ""
    return f"https://www.abc.net.au/triplejunearthed/artist/{slug}", slug


def _load_unearthed_artist_url_index(index_path: str | None = None, *, require_existing: bool = False) -> list[dict]:
    path = index_path or _unearthed_artist_url_index_path()
    if require_existing:
        _validate_unearthed_index_access(path, require_existing=True)
    rows: list[dict] = []
    if not os.path.exists(path):
        print("[UE Index] loaded existing index rows=0")
        return rows
    seen_urls: set[str] = set()
    seen_slugs: set[str] = set()
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                normalized_url, slug = normalize_unearthed_artist_url(row.get("artist_url") or row.get("artist_slug"))
                if not normalized_url or not slug:
                    continue
                if normalized_url in seen_urls or slug in seen_slugs:
                    continue
                seen_urls.add(normalized_url)
                seen_slugs.add(slug)
                rows.append(
                    {
                        "artist_url": normalized_url,
                        "artist_slug": slug,
                        "first_seen_at": (row.get("first_seen_at") or "").strip(),
                        "last_seen_at": (row.get("last_seen_at") or "").strip(),
                        "source": (row.get("source") or "").strip(),
                    }
                )
    except Exception as exc:
        print(f"[UE Index] failed loading index path={path!r}: {exc}")
        raise
    print(f"[UE Index] loaded existing index rows={len(rows)}")
    return rows


def _write_unearthed_artist_url_index(
    rows: list[dict],
    index_path: str | None = None,
    *,
    require_existing: bool = False,
) -> None:
    path = index_path or _unearthed_artist_url_index_path()
    _validate_unearthed_index_access(path, require_existing=require_existing)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNEARTHED_ARTIST_URL_INDEX_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in UNEARTHED_ARTIST_URL_INDEX_COLUMNS})
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(tmp_path, path)


def upsert_unearthed_artist_url_index(
    artist_urls,
    source: str = "discovery",
    index_path: str | None = None,
    require_existing: bool = False,
) -> dict:
    if isinstance(artist_urls, str):
        artist_urls = [artist_urls]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    existing_rows = _load_unearthed_artist_url_index(index_path, require_existing=require_existing)
    ordered_rows: list[dict] = []
    by_url: dict[str, dict] = {}
    by_slug: dict[str, dict] = {}
    for row in existing_rows:
        normalized_url, slug = normalize_unearthed_artist_url(row.get("artist_url") or row.get("artist_slug"))
        if not normalized_url or not slug:
            continue
        if normalized_url in by_url or slug in by_slug:
            continue
        normalized_row = {
            "artist_url": normalized_url,
            "artist_slug": slug,
            "first_seen_at": row.get("first_seen_at") or now,
            "last_seen_at": row.get("last_seen_at") or row.get("first_seen_at") or now,
            "source": row.get("source") or source,
        }
        ordered_rows.append(normalized_row)
        by_url[normalized_url] = normalized_row
        by_slug[slug] = normalized_row

    new_urls = 0
    updated_existing = 0
    for artist_url in artist_urls or []:
        normalized_url, slug = normalize_unearthed_artist_url(artist_url)
        if not normalized_url or not slug:
            continue
        print(f'[UE Index] discovered artist_url="{normalized_url}"')
        existing = by_url.get(normalized_url) or by_slug.get(slug)
        if existing:
            existing["artist_url"] = normalized_url
            existing["artist_slug"] = slug
            existing["last_seen_at"] = now
            if not existing.get("first_seen_at"):
                existing["first_seen_at"] = now
            if not existing.get("source"):
                existing["source"] = source
            by_url[normalized_url] = existing
            by_slug[slug] = existing
            updated_existing += 1
            continue
        row = {
            "artist_url": normalized_url,
            "artist_slug": slug,
            "first_seen_at": now,
            "last_seen_at": now,
            "source": source,
        }
        ordered_rows.append(row)
        by_url[normalized_url] = row
        by_slug[slug] = row
        new_urls += 1

    _write_unearthed_artist_url_index(ordered_rows, index_path, require_existing=require_existing)
    print(f"[UE Index] added new_urls={new_urls} updated_existing={updated_existing} total={len(ordered_rows)}")
    return {"new_urls": new_urls, "updated_existing": updated_existing, "total": len(ordered_rows)}


def load_unearthed_indexed_artist_urls(index_path: str | None = None, *, require_existing: bool = False) -> list[str]:
    rows = _load_unearthed_artist_url_index(index_path, require_existing=require_existing)
    urls = [row["artist_url"] for row in rows if row.get("artist_url")]
    print(f"[UE Index] using indexed URLs rows={len(urls)}")
    return urls


def _unearthed_profile_urls_match(left: str | None, right: str | None) -> bool:
    left_url, left_slug = normalize_unearthed_artist_url(left)
    right_url, right_slug = normalize_unearthed_artist_url(right)
    if left_url and right_url and left_url == right_url:
        return True
    return bool(left_slug and right_slug and left_slug == right_slug)


def _unearthed_index_tail_url(index_path: str | None = None, *, require_existing: bool = False) -> str | None:
    rows = _load_unearthed_artist_url_index(index_path, require_existing=require_existing)
    if not rows:
        return None
    tail_url = str(rows[-1].get("artist_url") or "").strip()
    return tail_url or None


def backfill_unearthed_artist_url_index(source_paths, index_path: str | None = None) -> dict:
    urls: list[str] = []
    for source_path in source_paths or []:
        path = str(source_path or "").strip()
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    candidates = [
                        row.get("artist_url"),
                        row.get("Artist URL"),
                        row.get("Profile URL"),
                        row.get("Source URL"),
                    ]
                    if not any(candidates):
                        candidates.append(row.get("artist_slug") or row.get("Artist Slug") or row.get("Artist Name"))
                    for candidate in candidates:
                        normalized_url, _slug = normalize_unearthed_artist_url(candidate)
                        if normalized_url:
                            urls.append(normalized_url)
                            break
        except Exception as exc:
            print(f"[UE Index] backfill skipped source={path!r}: {exc}")
    return upsert_unearthed_artist_url_index(urls, source="backfill", index_path=index_path)


def _load_unearthed_persistent_cursor() -> str | None:
    try:
        with open(_unearthed_cursor_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    cursor_value = payload.get("unearthed_persistent_cursor")
    if cursor_value is None:
        return None
    if not isinstance(cursor_value, str):
        return None
    cursor_value = cursor_value.strip()
    return cursor_value or None


def _canonical_unearthed_index_identity(index_path: str | None = None) -> str:
    return os.path.abspath(str(index_path or _unearthed_artist_url_index_path()).strip())


def _load_unearthed_index_cursor(index_path: str | None = None) -> tuple[dict | None, str]:
    expected_index_path = _canonical_unearthed_index_identity(index_path)
    try:
        with open(_unearthed_cursor_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None, "missing_file"
    except json.JSONDecodeError:
        return None, "corrupted_state"
    except Exception:
        return None, "unreadable_state"
    if not isinstance(payload, dict):
        return None, "corrupted_state"
    cursor_index_path = str(payload.get("index_file_path") or "").strip()
    if not cursor_index_path:
        return None, "missing_index_file_path"
    if os.path.abspath(cursor_index_path) != expected_index_path:
        return None, "index_path_mismatch"
    try:
        last_position = int(payload.get("last_position"))
    except Exception:
        return None, "invalid_position"
    if last_position < 0:
        return None, "invalid_position"
    last_url = str(payload.get("last_url") or "").strip()
    if not last_url:
        return None, "missing_last_url"
    return {
        "index_file_path": expected_index_path,
        "last_position": last_position,
        "last_url": last_url,
        "timestamp": str(payload.get("timestamp") or "").strip(),
        "batch_size": payload.get("batch_size"),
    }, ""


def _write_unearthed_index_cursor(
    index_path: str | None,
    last_position: int,
    last_url: str | None,
    batch_size: int,
) -> None:
    cursor_value = last_url.strip() if isinstance(last_url, str) else ""
    if not cursor_value:
        return
    payload = {
        "unearthed_persistent_cursor": cursor_value,
        "index_file_path": _canonical_unearthed_index_identity(index_path),
        "last_position": int(last_position),
        "last_url": cursor_value,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "batch_size": int(batch_size),
    }
    try:
        cursor_path = _unearthed_cursor_path()
        os.makedirs(os.path.dirname(cursor_path), exist_ok=True)
        tmp_path = f"{cursor_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cursor_path)
    except Exception as exc:
        print(f"[UE Index Cursor] persist failed reason={exc.__class__.__name__}: {exc}")


def _select_unearthed_index_profile_urls(
    indexed_urls: list[str],
    index_path: str | None,
    max_artists: int,
    resume_mode: str,
    manual_start_position=None,
) -> tuple[list[str], int, int, int]:
    index_rows = len(indexed_urls)
    batch_size = max(int(max_artists or 0), 0)
    normalized_mode = str(resume_mode or "auto").strip().lower()
    if normalized_mode not in {"auto", "cursor", "fresh", "selected"}:
        normalized_mode = "auto"

    start = 0
    manual_start_raw = "" if manual_start_position is None else str(manual_start_position).strip()
    manual_start_applied = False
    if manual_start_raw:
        try:
            manual_start = int(manual_start_raw)
        except Exception as exc:
            raise ValueError(f"Invalid Unearthed start index position: {manual_start_raw!r}") from exc
        if manual_start < 0:
            raise ValueError(f"Invalid Unearthed start index position: {manual_start}")
        start = min(manual_start, index_rows)
        manual_start_applied = True
    elif normalized_mode == "cursor":
        cursor, reason = _load_unearthed_index_cursor(index_path)
        if cursor is None:
            print("[UE Index Cursor] no valid cursor found → starting at 0")
            print(f"[UE Index Cursor] ignored cursor reason={reason}")
        else:
            last_position = int(cursor["last_position"])
            last_url = str(cursor["last_url"] or "").strip()
            if last_position >= index_rows:
                print("[UE Index Cursor] no valid cursor found → starting at 0")
                print("[UE Index Cursor] ignored cursor reason=position_out_of_bounds")
            elif not _unearthed_profile_urls_match(indexed_urls[last_position], last_url):
                print("[UE Index Cursor] no valid cursor found → starting at 0")
                print("[UE Index Cursor] ignored cursor reason=last_url_mismatch")
            else:
                start = last_position + 1

    end_exclusive = min(start + batch_size, index_rows) if batch_size > 0 else start
    selected_urls = indexed_urls[start:end_exclusive]
    end_inclusive = end_exclusive - 1
    print(
        f"[UE Index Cursor] mode={normalized_mode} index_rows={index_rows} "
        f"start={start} end={end_inclusive} count={len(selected_urls)}"
        f" manual_start={'yes' if manual_start_applied else 'no'}"
    )
    if selected_urls:
        _write_unearthed_index_cursor(index_path, end_inclusive, selected_urls[-1], len(selected_urls))
        print(
            f"[UE Index Cursor] persisted last_position={end_inclusive} "
            f"last_url={selected_urls[-1]}"
        )
    return selected_urls, start, end_inclusive, len(selected_urls)


def _write_unearthed_persistent_cursor(profile_url: str | None) -> None:
    cursor_value = profile_url.strip() if isinstance(profile_url, str) else None
    if not cursor_value:
        cursor_value = None
    try:
        cursor_path = _unearthed_cursor_path()
    except Exception as exc:
        print(
            "Persistent cursor write failed "
            "[seam=helper internal write step=_unearthed_cursor_path] "
            f"{exc.__class__.__name__}: {exc}; cursor_value={cursor_value!r}"
        )
        return
    try:
        os.makedirs(os.path.dirname(cursor_path), exist_ok=True)
    except Exception as exc:
        print(
            "Persistent cursor write failed "
            "[seam=helper internal write step=os.makedirs] "
            f"{exc.__class__.__name__}: {exc}; target_path={cursor_path!r}; "
            f"cursor_value={cursor_value!r}"
        )
        return
    try:
        with open(cursor_path, "w", encoding="utf-8") as handle:
            try:
                json.dump({"unearthed_persistent_cursor": cursor_value}, handle, indent=2)
            except Exception as exc:
                print(
                    "Persistent cursor write failed "
                    "[seam=helper internal write step=json.dump] "
                    f"{exc.__class__.__name__}: {exc}; target_path={cursor_path!r}; "
                    f"cursor_value={cursor_value!r}"
                )
    except Exception as exc:
        print(
            "Persistent cursor write failed "
            "[seam=helper internal write step=file open] "
            f"{exc.__class__.__name__}: {exc}; target_path={cursor_path!r}; "
            f"cursor_value={cursor_value!r}"
        )


def _append_unearthed_profile_url(
    ordered_profile_urls: list[str],
    seen_profile_urls: set[str],
    profile_url: str,
) -> bool:
    normalized_profile_url = profile_url.strip() if isinstance(profile_url, str) else ""
    if not normalized_profile_url or normalized_profile_url in seen_profile_urls:
        return False
    seen_profile_urls.add(normalized_profile_url)
    ordered_profile_urls.append(normalized_profile_url)
    return True


def normalize_unearthed_cursor(value: str | None) -> str:
    normalized_value = value.strip() if isinstance(value, str) else ""
    if not normalized_value:
        return ""
    parsed = urlparse(normalized_value)
    normalized_path = (parsed.path or "").rstrip("/")
    if parsed.scheme or parsed.netloc or normalized_value.startswith("/"):
        slug_match = re.search(r"/artist/([^/]+)$", normalized_path, flags=re.IGNORECASE)
        if slug_match:
            return slug_match.group(1).strip().lower()
        fallback_slug = normalized_path.strip("/").rsplit("/", 1)[-1].strip()
        return fallback_slug.lower()
    return normalized_path.strip().lower()


def _normalize_unearthed_profile_url_for_match(profile_url: str | None) -> str:
    normalized_profile_url = profile_url.strip() if isinstance(profile_url, str) else ""
    if not normalized_profile_url:
        return ""
    parsed = urlparse(normalized_profile_url)
    if not (parsed.scheme or parsed.netloc or normalized_profile_url.startswith("/")):
        return ""
    normalized_path = (parsed.path or "").rstrip("/")
    if not normalized_path:
        return ""
    return urlunparse(
        parsed._replace(
            scheme=(parsed.scheme or "https").lower(),
            netloc=(parsed.netloc or "").lower(),
            path=normalized_path.lower(),
            params="",
            query="",
            fragment="",
        )
    )


class UnearthedSelectedCursorError(RuntimeError):
    """Raised when an explicit Unearthed selected cursor cannot be used safely."""


class UnearthedResumeCursorError(RuntimeError):
    """Raised when an Unearthed auto/cursor resume target cannot be used safely."""


def _build_unearthed_resume_debug_details(
    ordered_profile_urls: list[str],
    target_profile_url: str | None,
) -> dict:
    normalized_target_profile_url = _normalize_unearthed_profile_url_for_match(target_profile_url)
    target_slug = normalize_unearthed_cursor(target_profile_url)
    exact_match_indices = []
    normalized_match_indices = []
    slug_match_indices = []
    if target_profile_url:
        exact_match_indices = [
            profile_index
            for profile_index, profile_url in enumerate(ordered_profile_urls)
            if profile_url == target_profile_url
        ]
    if normalized_target_profile_url:
        normalized_match_indices = [
            profile_index
            for profile_index, profile_url in enumerate(ordered_profile_urls)
            if _normalize_unearthed_profile_url_for_match(profile_url) == normalized_target_profile_url
        ]
    if target_slug:
        slug_match_indices = [
            profile_index
            for profile_index, profile_url in enumerate(ordered_profile_urls)
            if normalize_unearthed_cursor(profile_url) == target_slug
        ]
    matched_index = None
    if normalized_target_profile_url or target_slug:
        for profile_index in range(len(ordered_profile_urls) - 1, -1, -1):
            profile_url = ordered_profile_urls[profile_index]
            normalized_profile_url = _normalize_unearthed_profile_url_for_match(profile_url)
            profile_slug = normalize_unearthed_cursor(profile_url)
            if normalized_target_profile_url and normalized_profile_url == normalized_target_profile_url:
                matched_index = profile_index
                break
            if target_slug and profile_slug == target_slug:
                matched_index = profile_index
                break
    resolved_resume_index = matched_index + 1 if matched_index is not None else None
    return {
        "target_profile_url": target_profile_url,
        "normalized_target_profile_url": normalized_target_profile_url,
        "target_slug": target_slug,
        "ordered_profile_urls_count": len(ordered_profile_urls),
        "exact_match_indices": exact_match_indices,
        "normalized_match_indices": normalized_match_indices,
        "slug_match_indices": slug_match_indices,
        "matched_index": matched_index,
        "resolved_resume_index": resolved_resume_index,
    }


def _log_unearthed_resume_debug(message: str) -> None:
    print(f"[UE Resume Debug] {message}")


def _default_unearthed_cursor_search_limit(max_artists: int) -> int:
    safe_max_artists = 0
    try:
        safe_max_artists = int(max_artists or 0)
    except Exception:
        safe_max_artists = 0
    return min(max(safe_max_artists * 5, 2000), 10000)


def _resolve_unearthed_cursor_search_limit(job_config: dict | None, max_artists: int) -> int:
    default_limit = _default_unearthed_cursor_search_limit(max_artists)
    configured_limit = None
    if isinstance(job_config, dict):
        configured_limit = job_config.get("unearthed_cursor_search_limit")
    try:
        resolved_limit = int(configured_limit)
    except Exception:
        resolved_limit = default_limit
    if resolved_limit <= 0:
        resolved_limit = default_limit
    return min(resolved_limit, 10000)


def _resolve_unearthed_resume_index(
    ordered_profile_urls: list[str],
    target_profile_url: str | None,
) -> int | None:
    if not target_profile_url:
        return None
    debug_details = _build_unearthed_resume_debug_details(ordered_profile_urls, target_profile_url)
    _log_unearthed_resume_debug(
        "resolver "
        f"target_profile_url={debug_details['target_profile_url']!r} "
        f"normalized_target_profile_url={debug_details['normalized_target_profile_url']!r} "
        f"target_slug={debug_details['target_slug']!r} "
        f"ordered_profile_urls_count={debug_details['ordered_profile_urls_count']} "
        f"exact_match_indices={debug_details['exact_match_indices']} "
        f"normalized_match_indices={debug_details['normalized_match_indices']} "
        f"slug_match_indices={debug_details['slug_match_indices']} "
        f"matched_index={debug_details['matched_index']} "
        f"resolved_resume_index={debug_details['resolved_resume_index']}"
    )
    return debug_details["resolved_resume_index"]


def _count_unearthed_remaining_profile_urls(
    ordered_profile_urls: list[str],
    target_profile_url: str | None,
) -> int:
    if not target_profile_url:
        return len(ordered_profile_urls)
    resume_index = _resolve_unearthed_resume_index(ordered_profile_urls, target_profile_url)
    if resume_index is None:
        return 0
    return len(ordered_profile_urls[resume_index:])


def _has_unearthed_reached_target_profile_url(
    ordered_profile_urls: list[str],
    target_profile_url: str | None,
) -> bool:
    if not target_profile_url:
        return False
    return _resolve_unearthed_resume_index(ordered_profile_urls, target_profile_url) is not None


def _is_unearthed_slice_ready(
    ordered_profile_urls: list[str],
    target_profile_url: str | None,
    max_artists: int,
) -> bool:
    if max_artists <= 0:
        return True
    if not target_profile_url:
        return len(ordered_profile_urls) >= max_artists
    if not _has_unearthed_reached_target_profile_url(ordered_profile_urls, target_profile_url):
        return False
    return _count_unearthed_remaining_profile_urls(ordered_profile_urls, target_profile_url) >= max_artists


def _slice_unearthed_profile_urls(
    ordered_profile_urls: list[str],
    target_profile_url: str | None,
    max_artists: int,
) -> list[str]:
    resume_index = 0
    if target_profile_url:
        resolved_resume_index = _resolve_unearthed_resume_index(ordered_profile_urls, target_profile_url)
        if resolved_resume_index is None:
            raise UnearthedResumeCursorError(
                "Unearthed resume target was not found in the discovered profile stream: "
                f"{target_profile_url}"
            )
        resume_index = resolved_resume_index
    return ordered_profile_urls[resume_index:resume_index + max_artists]


def scrape_website(url, existing_csv="artist_social_links.csv", max_artists=200, fb_session=None, job_config=None):
    job_config = job_config or {}
    use_url_index = bool(job_config.get("use_unearthed_url_index")) if isinstance(job_config, dict) else False
    resume_mode_for_log = str(job_config.get("unearthed_resume_mode", "auto") or "auto").strip() if isinstance(job_config, dict) else ""
    manual_start_for_log = job_config.get("unearthed_start_index_position") if isinstance(job_config, dict) else None
    source_mode_for_log = "index" if use_url_index else "listing"
    output_dir_for_log = os.path.dirname(os.path.abspath(str(existing_csv or ""))) if existing_csv else ""
    _ue_startup_log(
        "job_entry "
        f"source_mode={source_mode_for_log} "
        f"resume_mode={resume_mode_for_log} "
        f"manual_start={manual_start_for_log if manual_start_for_log is not None else ''} "
        f"scrape_count={max_artists} "
        f"output_dir={output_dir_for_log}"
    )
    index_path = _resolve_unearthed_url_index_path(job_config)
    _ue_startup_log(f"index_resolve_start path={index_path}")
    index_path_explicit = bool(str(job_config.get("unearthed_url_index_path") or "").strip()) if isinstance(job_config, dict) else False
    if index_path_explicit:
        _validate_unearthed_index_access(index_path, require_existing=True)
    browser_init_started_at = time.time()
    _ue_startup_log("browser_init_start")
    try:
        driver = setup_driver()
    finally:
        browser_init_elapsed = _ue_startup_elapsed(browser_init_started_at)
        _ue_startup_log(f"browser_init_done elapsed_sec={browser_init_elapsed:.3f}")
        _ue_startup_warn_if_slow("browser_init", browser_init_elapsed, 15.0)
    fb_driver = None
    artist_data = []
    profile_urls = []
    selected_cursor_strict = False
    resume_cursor_strict = False
    resume_mode = ""
    resume_cursor_source = ""
    resume_continue_active = False
    resume_new_urls_added = 0
    resume_duplicates_seen = 0
    resume_total_index = 0
    terminal_profile_url = None
    scrape_completed = False
    last_discovery_progress_at = 0.0
    try:
        if not use_url_index:
            driver.get(url)
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/triplejunearthed/artist/"]'))
            )
        # Load existing CSV data if available
        existing_data = pd.DataFrame()
        if os.path.exists(existing_csv):
            existing_data = pd.read_csv(existing_csv)
        ordered_profile_urls = []
        seen_profile_urls = set()
        listing_metadata_by_url = {}
        state = job_config.get("_night_mode_state") if isinstance(job_config, dict) else None
        persist_state = job_config.get("_night_mode_state_persist") if isinstance(job_config, dict) else None
        resume_enabled = isinstance(job_config, dict) and (
            "_night_mode_state" in job_config or "unearthed_resume_mode" in job_config
        )
        target_profile_url = None
        cursor_search_limit = 0
        normalized_target_profile_url = ""
        target_slug = ""
        matched_index = None
        resolved_resume_index = None
        cursor_resolved_discovered_count = None
        cursor_search_progress_every = 50
        next_cursor_progress_log_count = cursor_search_progress_every
        search_exhausted = False
        if use_url_index:
            index_load_started_at = time.time()
            indexed_urls = load_unearthed_indexed_artist_urls(index_path, require_existing=index_path_explicit)
            index_load_elapsed = _ue_startup_elapsed(index_load_started_at)
            _ue_startup_log(
                f"index_loaded rows={len(indexed_urls)} path={index_path} elapsed_sec={index_load_elapsed:.3f}"
            )
            _ue_startup_warn_if_slow("index_load", index_load_elapsed, 5.0)
            total_index_rows = len(indexed_urls)
            resume_mode = str(job_config.get("unearthed_resume_mode", "auto") or "auto").strip().lower()
            if resume_mode not in {"auto", "cursor", "fresh", "selected"}:
                resume_mode = "auto"
            _ue_startup_log(
                f"index_slice_start start={job_config.get('unearthed_start_index_position') if job_config.get('unearthed_start_index_position') is not None else ''} "
                f"requested_count={max_artists} total_rows={total_index_rows}"
            )
            index_slice_started_at = time.time()
            indexed_urls, _index_start, _index_end, _index_count = _select_unearthed_index_profile_urls(
                indexed_urls,
                index_path,
                max_artists,
                resume_mode,
                job_config.get("unearthed_start_index_position"),
            )
            index_slice_elapsed = _ue_startup_elapsed(index_slice_started_at)
            first_index_url = indexed_urls[0] if indexed_urls else ""
            last_index_url = indexed_urls[-1] if indexed_urls else ""
            _ue_startup_log(
                f"index_slice_done start={_index_start} end={_index_end} count={_index_count} "
                f"first_url={first_index_url} last_url={last_index_url} elapsed_sec={index_slice_elapsed:.3f}"
            )
            _ue_startup_warn_if_slow("index_slice", index_slice_elapsed, 5.0)
            if not indexed_urls:
                _ue_startup_warn(
                    f"empty_slice start={_index_start} requested_count={max_artists} total_rows={total_index_rows}"
                )
            for indexed_url in indexed_urls:
                _append_unearthed_profile_url(ordered_profile_urls, seen_profile_urls, indexed_url)
            target_profile_url = None
            resume_enabled = False
        if resume_enabled:
            resume_mode = str(job_config.get("unearthed_resume_mode", "auto") or "auto").strip().lower()
            if resume_mode not in {"auto", "cursor", "fresh", "selected"}:
                resume_mode = "auto"
            persistent_cursor = None if resume_mode == "fresh" else _load_unearthed_persistent_cursor()
            run_root_cursor = ""
            if isinstance(state, dict):
                run_root_cursor = str(state.get("unearthed_last_profile_url") or "").strip()
            index_tail_url = None
            if resume_mode in {"auto", "cursor"}:
                index_tail_url = _unearthed_index_tail_url(index_path, require_existing=index_path_explicit)
            if resume_mode == "selected":
                selected_cursor_strict = True
                target_profile_url = str(job_config.get("unearthed_selected_cursor") or "").strip()
                resume_cursor_source = "job_config"
                if not target_profile_url:
                    raise UnearthedSelectedCursorError(
                        "Selected cursor entry point mode requires a non-empty Unearthed checkpoint URL."
                    )
            elif resume_mode in {"auto", "cursor"}:
                if index_tail_url:
                    target_profile_url = index_tail_url
                    resume_cursor_source = "index_tail"
                    for stale_source, stale_cursor in (
                        ("run_root", run_root_cursor),
                        ("global", persistent_cursor),
                    ):
                        stale_cursor = str(stale_cursor or "").strip()
                        if stale_cursor and not _unearthed_profile_urls_match(stale_cursor, index_tail_url):
                            print(
                                "[UE Resume] stale_cursor_ignored "
                                f'cursor_source="{stale_source}" '
                                f'cursor_url="{stale_cursor}" '
                                f'index_tail_url="{index_tail_url}"'
                            )
                elif persistent_cursor:
                    target_profile_url = persistent_cursor
                    resume_cursor_source = "global"
                elif run_root_cursor:
                    target_profile_url = run_root_cursor
                    resume_cursor_source = "run_root"
                resume_cursor_strict = bool(target_profile_url)
        if target_profile_url:
            cursor_search_limit = _resolve_unearthed_cursor_search_limit(job_config, max_artists)
            normalized_target_profile_url = _normalize_unearthed_profile_url_for_match(target_profile_url)
            target_slug = normalize_unearthed_cursor(target_profile_url)
            resume_continue_active = resume_mode in {"auto", "cursor"} and bool(target_profile_url)
            if resume_continue_active:
                print(
                    "[UE Resume] "
                    f'mode="continue" cursor_source="{resume_cursor_source or "unknown"}" '
                    f'target_url="{target_profile_url}"'
                )

        def _resolve_listing_card(link_tag):
            link_text = " ".join(link_tag.stripped_strings)
            for ancestor in link_tag.parents:
                ancestor_name = getattr(ancestor, "name", "")
                if ancestor_name in {"article", "li"}:
                    return ancestor
                if ancestor_name in {"div", "section"}:
                    if ancestor.select_one('a[href^="/triplejunearthed/artist/"]') != link_tag:
                        continue
                    ancestor_text = " ".join(ancestor.stripped_strings)
                    if ancestor_text and ancestor_text != link_text:
                        return ancestor
            return link_tag

        def _extract_listing_metadata(listing_card):
            text_fragments = list(listing_card.stripped_strings)
            attr_fragments = []
            for tag in listing_card.find_all(True):
                for attr_name in ("aria-label", "title", "alt"):
                    attr_value = (tag.get(attr_name) or "").strip()
                    if attr_value:
                        attr_fragments.append(attr_value)
            listing_text = " ".join(text_fragments + attr_fragments)
            listing_text_lower = listing_text.lower()
            location_value = ""

            for text_fragment in text_fragments:
                location_match = re.match(r"Location\s*:?\s*(.+)", text_fragment, flags=re.IGNORECASE)
                if location_match:
                    location_value = location_match.group(1).strip(" :-")
                    break

            if not location_value:
                location_match = re.search(
                    r"Location\s*:?\s*(.+?)(?=\s*(?:Played on triple J|Played on Unearthed|$))",
                    listing_text,
                    flags=re.IGNORECASE,
                )
                if location_match:
                    location_value = location_match.group(1).strip(" :-")

            return {
                "location": location_value,
                "played_on_triplej": "yes" if "played on triple j" in listing_text_lower else "",
                "played_on_unearthed": "yes"
                if ("played on unearthed" in listing_text_lower or "played on triple j unearthed" in listing_text_lower)
                else "",
            }

        def _discover_current_listing_page(
            stop_on_target: bool = False,
            stop_after_count: int | None = None,
        ) -> int | None:
            nonlocal next_cursor_progress_log_count, resume_duplicates_seen, resume_new_urls_added, resume_total_index, last_discovery_progress_at
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            artist_links = soup.select('a.HU3iy.p1_Ju.mqDRk.FQED6.O_grP[href^="/triplejunearthed/artist/"]')
            if not artist_links:
                artist_links = soup.select('a[href^="/triplejunearthed/artist/"]')
            listing_cards = [_resolve_listing_card(artist_link) for artist_link in artist_links]
            for listing_card in listing_cards:
                link = listing_card if getattr(listing_card, "name", "") == "a" else listing_card.select_one('a[href^="/triplejunearthed/artist/"]')
                if not link:
                    continue
                href = link.get('href', '')
                if href.startswith('/triplejunearthed/artist/'):
                    profile_url, _profile_slug = normalize_unearthed_artist_url("https://www.abc.net.au" + href)
                    if not profile_url:
                        continue
                    if not _append_unearthed_profile_url(ordered_profile_urls, seen_profile_urls, profile_url):
                        if resume_continue_active:
                            resume_duplicates_seen += 1
                        continue
                    listing_metadata_by_url.setdefault(profile_url, _extract_listing_metadata(listing_card))
                    while stop_on_target and len(ordered_profile_urls) >= next_cursor_progress_log_count:
                        _log_unearthed_resume_debug(
                            "cursor_search_progress "
                            f'target_slug="{target_slug}" '
                            f"discovered_count={len(ordered_profile_urls)} "
                            f'last_url="{profile_url}"'
                        )
                        next_cursor_progress_log_count += cursor_search_progress_every
                    normalized_profile_url = _normalize_unearthed_profile_url_for_match(profile_url)
                    profile_slug = normalize_unearthed_cursor(profile_url)
                    if stop_on_target and (
                        (normalized_target_profile_url and normalized_profile_url == normalized_target_profile_url)
                        or (target_slug and profile_slug == target_slug)
                    ):
                        return len(ordered_profile_urls) - 1
                    should_persist_discovered_url = not resume_continue_active
                    if resume_continue_active and resolved_resume_index is not None:
                        should_persist_discovered_url = len(ordered_profile_urls) - 1 >= resolved_resume_index
                    if should_persist_discovered_url:
                        index_result = upsert_unearthed_artist_url_index(
                            [profile_url],
                            source="discovery",
                            index_path=index_path,
                            require_existing=index_path_explicit,
                        )
                        if resume_continue_active:
                            resume_new_urls_added += int(index_result.get("new_urls", 0) or 0)
                            resume_duplicates_seen += int(index_result.get("updated_existing", 0) or 0)
                            resume_total_index = int(index_result.get("total", resume_total_index) or 0)
                    progress_now = time.time()
                    if progress_now - last_discovery_progress_at >= 0.25:
                        last_discovery_progress_at = progress_now
                        try:
                            update_progress(
                                0,
                                meta={
                                    "phase": "discovery",
                                    "discovered_urls": len(ordered_profile_urls),
                                    "current_source": "unearthed",
                                },
                            )
                        except Exception:
                            pass
                    if stop_after_count is not None and len(ordered_profile_urls) >= stop_after_count:
                        return None
                    if stop_on_target and len(ordered_profile_urls) >= cursor_search_limit:
                        return None
            return None

        def _click_unearthed_load_more(
            urls_before: int,
            discover_after_click,
            max_attempts: int = UNEARTHED_LOAD_MORE_MAX_ATTEMPTS,
            wait_seconds: int = 12,
        ) -> bool:
            last_exception = None
            terminal_reason = "load_more_unavailable"

            def _log_terminal(reason: str, exception: Exception | None = None) -> None:
                try:
                    buttons = driver.find_elements(By.TAG_NAME, "button")
                except Exception as button_error:
                    buttons = []
                    exception = exception or button_error
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                    body_has_load_more = "load more" in body_text.lower()
                except Exception:
                    body_has_load_more = False
                candidate_texts = []
                for button in buttons:
                    try:
                        text = " ".join((button.text or "").split())
                    except Exception:
                        text = ""
                    if text and "load more" in text.lower():
                        candidate_texts.append(text)
                if not candidate_texts:
                    for button in buttons[:10]:
                        try:
                            text = " ".join((button.text or "").split())
                        except Exception:
                            text = ""
                        if text:
                            candidate_texts.append(text)
                candidate_texts = candidate_texts[:10]
                exception_text = ""
                if exception is not None:
                    exception_text = f"{type(exception).__name__}: {exception}"
                print(
                    f'[UE LoadMore] terminal reason="{reason}" '
                    f"urls_before={urls_before} buttons_found={len(buttons)} "
                    f"body_has_load_more={body_has_load_more}"
                )
                print(f"[UE LoadMore] candidate_texts={candidate_texts}")
                print(f'[UE LoadMore] exception="{exception_text}"')

            for _attempt in range(max_attempts):
                try:
                    load_more_button = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, UNEARTHED_LOAD_MORE_XPATH))
                    )
                    if hasattr(driver, "execute_script"):
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                            load_more_button,
                        )
                    time.sleep(random.uniform(0.2, 0.6))
                    terminal_reason = "button_not_usable"
                    is_displayed = load_more_button.is_displayed() if hasattr(load_more_button, "is_displayed") else True
                    is_enabled = load_more_button.is_enabled() if hasattr(load_more_button, "is_enabled") else True
                    if not (is_displayed and is_enabled):
                        continue
                    terminal_reason = "click_or_wait_failed"
                    try:
                        load_more_button.click()
                    except Exception as click_error:
                        last_exception = click_error
                        if hasattr(driver, "execute_script"):
                            driver.execute_script("arguments[0].click();", load_more_button)
                        else:
                            raise

                    discover_after_click()
                    if len(ordered_profile_urls) > urls_before:
                        return True

                    def _count_increased(_driver) -> bool:
                        discover_after_click()
                        return len(ordered_profile_urls) > urls_before

                    terminal_reason = "count_not_increased"
                    WebDriverWait(driver, wait_seconds, poll_frequency=0.5).until(_count_increased)
                    return True
                except Exception as error:
                    last_exception = error
                    if "no more pages" in str(error).lower():
                        break
                    time.sleep(random.uniform(0.8, 1.4))

            _log_terminal(terminal_reason, last_exception)
            return False

        if use_url_index:
            pass
        elif target_profile_url:
            while resolved_resume_index is None and len(ordered_profile_urls) < cursor_search_limit:
                matched_index = _discover_current_listing_page(stop_on_target=True)
                print(f"Found {len(ordered_profile_urls)} artist profile URLs so far...")
                if matched_index is not None:
                    resolved_resume_index = matched_index + 1
                    cursor_resolved_discovered_count = len(ordered_profile_urls)
                    _log_unearthed_resume_debug(
                        "cursor_resolved "
                        f'target_slug="{target_slug}" '
                        f"matched_index={matched_index} "
                        f"resolved_resume_index={resolved_resume_index} "
                        f"discovered_count={cursor_resolved_discovered_count}"
                    )
                    break
                if len(ordered_profile_urls) >= cursor_search_limit:
                    search_exhausted = True
                    break
                def _discover_cursor_after_load_more():
                    nonlocal resolved_resume_index, cursor_resolved_discovered_count
                    loaded_matched_index = _discover_current_listing_page(stop_on_target=True)
                    if loaded_matched_index is not None:
                        resolved_resume_index = loaded_matched_index + 1
                        cursor_resolved_discovered_count = len(ordered_profile_urls)
                        _log_unearthed_resume_debug(
                            "cursor_resolved "
                            f'target_slug="{target_slug}" '
                            f"matched_index={loaded_matched_index} "
                            f"resolved_resume_index={resolved_resume_index} "
                            f"discovered_count={cursor_resolved_discovered_count}"
                        )

                if not _click_unearthed_load_more(
                    len(ordered_profile_urls),
                    _discover_cursor_after_load_more,
                ):
                    search_exhausted = True
                    break
                if resolved_resume_index is None and len(ordered_profile_urls) >= cursor_search_limit:
                    search_exhausted = True

            if resolved_resume_index is not None:
                def _resolved_slice_ready() -> bool:
                    return max_artists <= 0 or len(ordered_profile_urls) >= resolved_resume_index + max_artists

                required_discovered_count = resolved_resume_index + max_artists
                while not _resolved_slice_ready():
                    _discover_current_listing_page(stop_after_count=required_discovered_count)
                    print(f"Found {len(ordered_profile_urls)} artist profile URLs so far...")
                    if _resolved_slice_ready():
                        break
                    if not _click_unearthed_load_more(
                        len(ordered_profile_urls),
                        lambda: _discover_current_listing_page(stop_after_count=required_discovered_count),
                    ):
                        break
                    print(f"Found {len(ordered_profile_urls)} artist profile URLs so far...")
        elif not use_url_index:
            while _count_unearthed_remaining_profile_urls(ordered_profile_urls, target_profile_url) < max_artists:
                _discover_current_listing_page()
                print(f"Found {len(ordered_profile_urls)} artist profile URLs so far...")
                if not _click_unearthed_load_more(
                    len(ordered_profile_urls),
                    _discover_current_listing_page,
                ):
                    break
        if target_profile_url:
            debug_details = _build_unearthed_resume_debug_details(ordered_profile_urls, target_profile_url)
            target_slug = debug_details["target_slug"]
            matched_index = debug_details["matched_index"]
            resolved_resume_index = debug_details["resolved_resume_index"]
            remaining_after_resume_index = 0
            if resolved_resume_index is not None:
                remaining_after_resume_index = len(ordered_profile_urls[resolved_resume_index:])
            slice_ready = (
                max_artists <= 0
                or (
                    resolved_resume_index is not None
                    and len(ordered_profile_urls) >= resolved_resume_index + max_artists
                )
            )
            _log_unearthed_resume_debug(
                "discovery_sample "
                f"first10={ordered_profile_urls[:10]} "
                f"last10={ordered_profile_urls[-10:]}"
            )
            if resolved_resume_index is None:
                first_url = ordered_profile_urls[0] if ordered_profile_urls else ""
                last_url = ordered_profile_urls[-1] if ordered_profile_urls else ""
                if resume_continue_active:
                    print(
                        "[UE Resume Error] cursor_not_found "
                        f'target_url="{target_profile_url}" '
                        f"discovered_count={len(ordered_profile_urls)}"
                    )
                print(
                    "[UE Resume Error] cursor_unresolved "
                    f'target_profile_url="{target_profile_url}" '
                    f'target_slug="{target_slug}" '
                    f"discovered_count={len(ordered_profile_urls)} "
                    f'first_url="{first_url}" '
                    f'last_url="{last_url}" '
                    f"search_exhausted={search_exhausted}"
                )
            else:
                if cursor_resolved_discovered_count is None:
                    _log_unearthed_resume_debug(
                        "cursor_resolved "
                        f'target_slug="{target_slug}" '
                        f"matched_index={matched_index} "
                        f"resolved_resume_index={resolved_resume_index} "
                        f"discovered_count={len(ordered_profile_urls)}"
                    )
                    cursor_resolved_discovered_count = len(ordered_profile_urls)
                if resume_continue_active:
                    print(
                        "[UE Resume] cursor_found "
                        f"discovered_count={cursor_resolved_discovered_count} "
                        f'target_slug="{target_slug}"'
                    )
                    print(f'[UE Resume] collecting_after_cursor target_slug="{target_slug}"')
                _log_unearthed_resume_debug(
                    "slice_decision "
                    f"resolved_resume_index={resolved_resume_index} "
                    f"remaining_after_resume_index={remaining_after_resume_index} "
                    f"max_artists={max_artists} "
                    f"slice_ready={slice_ready} "
                    "fallback_to_zero=False"
                )
            if selected_cursor_strict and resolved_resume_index is None:
                raise UnearthedSelectedCursorError(
                    "Selected Unearthed cursor entry point was not found in the discovered profile stream: "
                    f"{target_profile_url}"
                )
            if resume_cursor_strict and resolved_resume_index is None:
                raise UnearthedResumeCursorError(
                    "Unearthed auto/cursor resume target was not found in the discovered profile stream; "
                    "refusing to restart from the beginning: "
                    f"{target_profile_url}"
                )
        profile_urls = _slice_unearthed_profile_urls(ordered_profile_urls, target_profile_url, max_artists)
        run_id = ""
        if isinstance(job_config, dict):
            run_id = str(job_config.get("job_id") or job_config.get("run_id") or "").strip()
        run_id = run_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            update_progress(
                0,
                meta={
                    "phase": "discovery",
                    "discovered_urls": len(profile_urls),
                    "current_source": "unearthed",
                },
            )
            init_progress(
                total_rows=len(profile_urls),
                run_id=run_id,
                meta={"phase": "processing", "current_source": "unearthed"},
            )
        except Exception:
            pass
        if resume_continue_active and resolved_resume_index is not None:
            if resume_total_index <= 0:
                resume_total_index = len(load_unearthed_indexed_artist_urls(index_path, require_existing=index_path_explicit))
            if resume_new_urls_added > 0:
                print(
                    "[UE Resume] "
                    f"new_urls_added={resume_new_urls_added} "
                    f"duplicates_seen={resume_duplicates_seen} "
                    f"total_index={resume_total_index}"
                )
            elif not profile_urls:
                print(
                    "[UE Resume] "
                    f"no_new_urls_after_cursor duplicates_seen={resume_duplicates_seen} "
                    f"total_index={resume_total_index}"
                )
            else:
                print(
                    "[UE Resume] "
                    f"new_urls_added=0 duplicates_seen={resume_duplicates_seen} "
                    f"total_index={resume_total_index}"
                )
        print(f"Total artist profile URLs to scrape: {len(profile_urls)}")
        if not profile_urls:
            print("No artist profile URLs found. Please check the website structure or selectors.")
        first_fetch_logged = False
        if profile_urls:
            first_profile_url = profile_urls[0]
            _ue_startup_log(f"first_profile_prepare row_offset=0 url={first_profile_url}")
        for row_offset, profile_url in enumerate(profile_urls):
            # Lazily initialize FB driver only if we encounter a Facebook link later.
            if fb_driver is None and SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1:
                try:
                    if fb_session is not None and hasattr(fb_session, "navigate"):
                        fb_driver = fb_session.navigate("about:blank")
                    else:
                        fb_driver = setup_facebook_driver()
                except Exception:
                    fb_driver = None
            first_fetch_started_at = None
            if not first_fetch_logged:
                first_fetch_started_at = time.time()
                _ue_startup_log(f"first_profile_fetch_start row_offset={row_offset} url={profile_url}")
            first_fetch_success = 0
            try:
                (
                    social_links,
                    location,
                    song_title,
                    sounds_like,
                    artist_name,
                    release_date,
                    primary_genre_value,
                    unearthed_genre_raw,
                    email_value,
                ) = scrape_artist_profile(
                    driver, profile_url, fb_driver=fb_driver
                )
                first_fetch_success = 1
            finally:
                if not first_fetch_logged and first_fetch_started_at is not None:
                    first_fetch_elapsed = _ue_startup_elapsed(first_fetch_started_at)
                    _ue_startup_log(
                        f"first_profile_fetch_done row_offset={row_offset} url={profile_url} "
                        f"success={first_fetch_success} elapsed_sec={first_fetch_elapsed:.3f}"
                    )
                    _ue_startup_warn_if_slow("first_profile_fetch", first_fetch_elapsed, 15.0)
                    first_fetch_logged = True
            listing_metadata = listing_metadata_by_url.get(profile_url, {})
            location = (listing_metadata.get("location") or location or "").strip()
            # Determine drum status from the full page source.
            drum_status_raw = get_drum_status_from_source(driver.page_source)
            played_on_triplej = "yes" if drum_status_raw == "triple j" else ""
            played_on_unearthed = "yes" if drum_status_raw == "triple j unearthed" else ""
            played_on_triplej = listing_metadata.get("played_on_triplej") or played_on_triplej
            played_on_unearthed = listing_metadata.get("played_on_unearthed") or played_on_unearthed
            primary_genre_value = primary_genre_value or ""
            genre_raw_value = unearthed_genre_raw or ""
            artist_data.append(
                (
                    artist_name,
                    location,
                    song_title,
                    sounds_like,
                    social_links,
                    "",
                    played_on_triplej,
                    played_on_unearthed,
                    release_date,
                    primary_genre_value,
                    genre_raw_value,
                    "",
                    "",
                    email_value,
                    "Triple J Unearthed",
                    "unearthed",
                    "Triple J Unearthed",
                )
            )
            if isinstance(state, dict):
                state["unearthed_last_profile_url"] = profile_url
                if callable(persist_state):
                    try:
                        persist_state()
                    except Exception:
                        pass
            terminal_profile_url = profile_url
        scrape_completed = True
    except Exception as e:
        print(f"Error during website scraping: {e}")
        if isinstance(e, (UnearthedSelectedCursorError, UnearthedResumeCursorError)):
            raise
    finally:
        driver.quit()
        if fb_driver:
            try:
                fb_driver.quit()
            except Exception:
                pass
    raw_write_started_at = time.time()
    _ue_startup_log(f"raw_write_start rows={len(artist_data)} path={existing_csv}")
    save_to_csv(artist_data, existing_csv)
    raw_write_elapsed = _ue_startup_elapsed(raw_write_started_at)
    _ue_startup_log(f"raw_write_done rows={len(artist_data)} path={existing_csv} elapsed_sec={raw_write_elapsed:.3f}")
    try:
        update_progress(
            len(profile_urls),
            meta={
                "phase": "processing",
                "emails_found": sum(1 for row in artist_data if len(row) > 13 and str(row[13] or "").strip()),
                "current_source": "unearthed",
                "current_status": "row_write_complete",
            },
        )
    except Exception:
        pass
    if scrape_completed and terminal_profile_url and resume_enabled:
        if isinstance(state, dict):
            state["unearthed_last_profile_url"] = terminal_profile_url
        _write_unearthed_persistent_cursor(terminal_profile_url)
        if callable(persist_state):
            try:
                persist_state()
            except Exception:
                pass
    _ue_startup_log(f"handoff_to_master rows={len(artist_data)} raw_csv={existing_csv}")

# ---------------------------
# Unearthed: release date extraction (robust)
# ---------------------------
_UNEARTHED_DATE_PATTERNS = [
    r"\breleased\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
    r"\breleased\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+([A-Za-z]+)\s+(\d{4})",
    r"\b(released|release\s+date|published)\b[:\s]+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
    r"\b(released|release\s+date|published)\b[:\s]+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    r"\b(20\d{2}|19\d{2})\b"
]

def unearthed_extract_release_date(html: str) -> str:
    """
    Best-effort extraction of a release date from an Unearthed artist/track page.
    Strategy:
      1) Look for <time datetime="..."> or time-like elements
      2) Scan meta/aria/accessible-description blocks
      3) Regex search for 'released ...' phrases and common date shapes
    Returns a normalized string if found (prefer YYYY-MM-DD when datetime attr present),
    else returns "".
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) <time datetime="YYYY-MM-DD"> if present (most reliable)
    for t in soup.find_all("time"):
        dtattr = (t.get("datetime") or "").strip()
        if dtattr and re.match(r"^\d{4}-\d{2}-\d{2}$", dtattr):
            return dtattr
        txt = t.get_text(" ", strip=True)
        if txt and re.search(r"\d{4}", txt):
            return txt

    # 2) meta description sometimes contains 'released ...'
    ogd = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if ogd and ogd.get("content"):
        content = ogd["content"]
        if "release" in content.lower() or "released" in content.lower():
            return content

    # 3) Visible text search in likely containers
    blocks = []
    blocks += [b.get_text(" ", strip=True) for b in soup.select("[data-component], .card, .content, .section, .divwU, .fRXHI, main")]
    text = " ".join(blocks) or soup.get_text(" ", strip=True)

    for pat in _UNEARTHED_DATE_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = " ".join(g for g in m.groups() if g) if m.groups() else m.group(0)
        candidate = candidate.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
            return candidate
        return candidate

    return ""


_unearthed_extract_genre_text = extract_unearthed_genre_text

def scrape_artist_profile(driver, profile_url, fb_driver=None):
    social_links = []
    location = ""
    song_title = ""
    sounds_like = ""
    artist_name = profile_url.split('/')[-1]
    release_date = ""
    email_value = ""
    primary_genre_value = ""
    unearthed_genre_raw = ""
    exclude_social_urls = {
        "https://www.facebook.com/triplejunearthed",
        "https://www.instagram.com/triple_j_unearthed",
        "https://twitter.com/triplejunearthd",
        "https://www.facebook.com/abc",
        "https://www.instagram.com/abcaustralia",
        "https://twitter.com/abcaustralia",
        "https://soundcloud.com/triplejunearthed",
        "https://www.soundcloud.com/triplejunearthed",
        "https://tiktok.com/@triplejradio",
        "https://www.tiktok.com/@triplejradio",
        "https://youtube.com/abcaustralia",
        "https://www.youtube.com/abcaustralia"
    }
    try:
        driver.get(profile_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )
        page_source = driver.page_source
        if SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1:
            try:
                email_matches = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", page_source or "")
                if email_matches:
                    email_value = email_matches[0]
            except Exception:
                email_value = ""
        soup = BeautifulSoup(page_source, 'html.parser')
        release_date = unearthed_extract_release_date(page_source) or ""
        genre_text_raw = _unearthed_extract_genre_text(soup)
        parsed_primary_genre, parsed_genre_raw = parse_unearthed_genre(genre_text_raw)
        if parsed_primary_genre:
            primary_genre_value = primary_genre_value or parsed_primary_genre
        if parsed_genre_raw:
            unearthed_genre_raw = parsed_genre_raw
        def _norm_text(value):
            return re.sub(r"\s+", " ", value or "").strip()

        def _direct_child_tags(tag):
            if not tag or not getattr(tag, "children", None):
                return []
            return [child for child in tag.children if getattr(child, "name", None)]

        def _label_text(tag):
            return _norm_text(tag.get_text(" ", strip=True)).rstrip(":").lower()

        def _heading_with_text(text_value):
            return soup.find(
                lambda t: getattr(t, "name", "") in {"h2", "h3", "h4", "h5", "h6"}
                and _label_text(t) == text_value
            )

        def _next_tag_sibling(tag):
            if not tag:
                return None
            sibling = tag.next_sibling
            while sibling is not None and not getattr(sibling, "name", None):
                sibling = sibling.next_sibling
            return sibling

        def _first_direct_list(container):
            if not container or not getattr(container, "name", None):
                return None
            if getattr(container, "name", "") in {"ul", "ol"} and container.find("li", recursive=False):
                return container
            for child in _direct_child_tags(container):
                if getattr(child, "name", "") in {"h2", "h3", "h4", "h5", "h6"} and _label_text(child) == "tracks":
                    break
                if child.name in {"ul", "ol"} and child.find("li", recursive=False):
                    return child
                for grandchild in _direct_child_tags(child):
                    if grandchild.name in {"ul", "ol"} and grandchild.find("li", recursive=False):
                        return grandchild
            return None

        def _tracks_section_container(anchor):
            current = anchor if getattr(anchor, "name", None) else None
            while current and getattr(current, "name", None):
                if current.find("div", attrs={"data-component": "TrackItem"}):
                    return current
                current = current.parent if getattr(current, "parent", None) else None
            return None

        links = soup.find_all('a', href=True)
        for link in links:
            href = link['href']
            if any(domain in href for domain in ['facebook.com', 'm.facebook.com', 'fb.com', 'fb.me', 'instagram.com', 'twitter.com', 'spotify.com', 'soundcloud.com', 'tiktok.com', 'youtube.com', 'youtu.be']):
                if normalize_url(href) in exclude_social_urls:
                    continue
                social_links.append(href)
        # Explicitly capture icon bar socials (often rendered as SVGs with aria-labels).
        for icon in soup.select("a[aria-label], a[rel='noopener']"):
            href = icon.get("href") or ""
            aria = (icon.get("aria-label") or "").lower()
            if not href:
                continue
            if any(token in aria for token in ("facebook", "instagram", "tiktok", "youtube", "spotify", "soundcloud")) or any(
                domain in href for domain in ("facebook.com", "m.facebook.com", "fb.com", "fb.me", "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "spotify.com", "soundcloud.com")
            ):
                if normalize_url(href) in exclude_social_urls:
                    continue
                social_links.append(href)
        # Also scrape raw HTML for embedded social URLs that may not be visible anchors.
        try:
            social_patterns = [
                r"https?://[\\w.-]*(?:facebook\\.com|m\\.facebook\\.com|fb\\.com|fb\\.me)[^\\s\"'>]+",
                r"https?://[\\w.-]*instagram\\.com[^\\s\"'>]+",
                r"https?://[\\w.-]*tiktok\\.com[^\\s\"'>]+",
                r"https?://[\\w.-]*soundcloud\\.com[^\\s\"'>]+",
                r"https?://[\\w.-]*youtube\\.com[^\\s\"'>]+",
                r"https?://[\\w.-]*youtu\\.be[^\\s\"'>]+",
                r"https?://[\\w.-]*spotify\\.com[^\\s\"'>]+",
            ]
            for pat in social_patterns:
                for candidate in re.findall(pat, page_source or "", flags=re.IGNORECASE):
                    if normalize_url(candidate) in exclude_social_urls:
                        continue
                    social_links.append(candidate)
        except Exception:
            pass
        # Deduplicate socials while preserving order.
        seen_links = set()
        clean_socials = []
        for href in social_links:
            norm = normalize_url(href)
            if norm in seen_links:
                continue
            seen_links.add(norm)
            clean_socials.append(href)
        social_links = clean_socials
        tracks_heading = _heading_with_text("tracks")
        genre_label = soup.find(
            lambda t: getattr(t, "name", "") in {"div", "p", "span", "strong", "h2", "h3", "h4"}
            and _label_text(t) == "genres"
        )
        artist_heading = soup.find("h1")
        artist_meta_container = artist_heading.parent if artist_heading and getattr(artist_heading, "parent", None) else None
        genre_container = None
        if genre_label:
            label_parent = genre_label.parent if getattr(genre_label, "parent", None) else None
            for candidate in (
                _next_tag_sibling(genre_label),
                _first_direct_list(label_parent),
                _next_tag_sibling(label_parent),
            ):
                genre_container = _first_direct_list(candidate)
                if genre_container:
                    break

        if not genre_container and artist_meta_container:
            for child in _direct_child_tags(artist_meta_container):
                if child is artist_heading:
                    break
                genre_container = _first_direct_list(child)
                if genre_container:
                    break

        if artist_meta_container and artist_heading:
            direct_children = _direct_child_tags(artist_meta_container)
            location_node = None
            for child in direct_children:
                if child is artist_heading:
                    continue
                if genre_container and (child is genre_container or genre_container in child.descendants):
                    continue
                if child.name in {"h1", "h2", "h3", "h4", "h5", "h6", "section"}:
                    continue
                child_text_raw = child.get_text(" ", strip=True)
                child_text = _norm_text(child_text_raw)
                if (
                    not child_text
                    or child_text == artist_name
                    or child_text.lower() == "artist"
                    or child.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None
                    or len(child_text) > 80
                    or "\n" in child_text_raw
                ):
                    continue
                location_node = child
                break
            if location_node:
                location = _norm_text(location_node.get_text(" ", strip=True))

        if tracks_heading:
            tracks_section = _tracks_section_container(tracks_heading)
            if tracks_section:
                first_track_item = tracks_section.find("div", attrs={"data-component": "TrackItem"})
                title_node = (
                    first_track_item.find(
                        "span",
                        class_=lambda value: value and "TrackItem_trackTitleText" in value,
                    )
                    if first_track_item else None
                )
                if title_node:
                    song_title = _norm_text(title_node.get_text(" ", strip=True))
        sounds_like_element = soup.find('h2', string="Sounds Like")
        if sounds_like_element:
            sounds_like_list = sounds_like_element.find_next('p')
            if sounds_like_list:
                sounds_like = sounds_like_list.get_text(strip=True)

        # If we have a Facebook link and a driver is available, attempt to scrape contact email from the FB page.
        if fb_driver and SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1 and not email_value:
            for link in list(social_links):
                href_lc = (link or "").lower()
                if not any(tok in href_lc for tok in ("facebook.com", "m.facebook.com", "fb.com", "fb.me")):
                    continue
                try:
                    fb_emails = fb_scrape_emails_from_page(fb_driver, link, log_fn=print, log_prefix="[Unearthed FB]")
                    if fb_emails:
                        email_value = fb_emails[0]
                        break
                except Exception:
                    continue
    except Exception as e:
        print(f"Error scraping profile {profile_url}: {e}")
    return social_links, location, song_title, sounds_like, artist_name, release_date, primary_genre_value, unearthed_genre_raw, email_value

def save_to_csv(data, filename):
    headers = [
        'Artist Name', 'Location', 'Song Title', 'Sounds Like', 'Social Link', 'SoundCloud Link',
        'Played on triple J', 'Played on Unearthed', 'Release Date', 'Primary Genre', 'Unearthed_Genre_Raw', 'Bandcamp_Source_Mode', 'Bandcamp_Search_Domain', 'Date Added', 'Email',
        'Lead_Source', 'Source_Directory', 'Source Directory'
    ]
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    if not data:
        _atomic_write_dataframe(pd.DataFrame(columns=headers), filename)
        print(f"Created empty CSV with headers at {filename}")
        return

    existing_data = pd.DataFrame()
    if os.path.exists(filename):
        try:
            existing_data = pd.read_csv(filename)
        except Exception:
            existing_data = pd.DataFrame()
        for col in headers:
            if col not in existing_data.columns:
                existing_data[col] = ""

    new_data = []
    data_columns = [col for col in headers if col != 'Date Added']
    expected_fields = len(data_columns)
    for entry in data:
        entry_list = list(entry)
        while len(entry_list) < expected_fields:
            entry_list.append("")
        (
            artist_name,
            location,
            song_title,
            sounds_like,
            social_links,
            soundcloud_link,
            played_on_triplej,
            played_on_unearthed,
            release_date,
            primary_genre,
            unearthed_genre_raw,
            bandcamp_source_mode,
            bandcamp_search_domain,
            email_value,
            lead_source,
            source_directory,
            legacy_source_directory,
        ) = entry_list[:expected_fields]
        lead_source = str(lead_source or "").strip()
        source_directory = str(source_directory or "").strip()
        legacy_source_directory = str(legacy_source_directory or "").strip()
        if isinstance(social_links, (str, bytes)):
            links_iterable = [social_links] if social_links else []
        else:
            links_iterable = list(social_links or [])
        if not links_iterable:
            links_iterable = [""]
        for link in links_iterable:
            new_data.append({
                'Artist Name': artist_name,
                'Location': location,
                'Song Title': _dedupe_song_title_value(song_title),
                'Sounds Like': sounds_like,
                'Social Link': link,
                'SoundCloud Link': soundcloud_link,
                'Played on triple J': played_on_triplej,
                'Played on Unearthed': played_on_unearthed,
                'Release Date': release_date,
                'Primary Genre': primary_genre,
                'Unearthed_Genre_Raw': unearthed_genre_raw,
                'Bandcamp_Source_Mode': bandcamp_source_mode,
                'Bandcamp_Search_Domain': bandcamp_search_domain,
                'Date Added': current_date,
                'Email': email_value,
                'Lead_Source': lead_source,
                'Source_Directory': source_directory,
                'Source Directory': legacy_source_directory,
            })

    combined = pd.concat([existing_data, pd.DataFrame(new_data)], ignore_index=True)
    if "Song Title" in combined.columns:
        combined["Song Title"] = combined["Song Title"].apply(_dedupe_song_title_value)
    if not combined.empty:
        combined = combined.drop_duplicates(subset=['Artist Name', 'Social Link'])
    _atomic_write_dataframe(combined, filename)
    print(f"Data saved to {filename}")


# =========================== Last.fm Scraper ===========================
def scrape_lastfm_similar(seed_artists, existing_csv="artist_social_links.csv", max_artists=200, log_fn=None):
    """
    Given a list of seed artist names (strings), use the Last.fm API to:
      - fetch similar artists (artist.getSimilar)
      - fetch each similar artist's info (artist.getInfo)
      - optionally scrape the artist's Last.fm page HTML for socials/website
      - write results to the standard CSV schema via save_to_csv()
    """
    def _log(msg):
        if not msg:
            return
        print(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    if not LASTFM_API_KEY:
        _log("Last.fm: LASTFM_API_KEY not set – skipping Last.fm scraping.")
        return

    if not seed_artists:
        _log("Last.fm: no seed artists provided.")
        return

    session = _lastfm_get_session()
    candidates = {}
    valid_seed_found = False
    for seed in seed_artists:
        seed_name = (seed or "").strip()
        if not seed_name:
            continue
        valid_seed_found = True
        _log(f"Last.fm: fetching similar artists for seed '{seed_name}'")
        data = _lastfm_api_get("artist.getSimilar", {
            "artist": seed_name,
            "limit": LASTFM_MAX_SIMILAR_PER_SEED,
            "autocorrect": 1
        }) or {}
        similar_block = data.get("similarartists", {}).get("artist", [])
        if isinstance(similar_block, dict):
            similar_block = [similar_block]
        for artist in similar_block or []:
            name = (artist.get("name") or "").strip()
            url = (artist.get("url") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key not in candidates:
                candidates[key] = {
                    "name": name,
                    "url": url,
                    "seed": seed_name
                }

    if not valid_seed_found:
        _log("Last.fm: no seed artists provided.")
        return

    if not candidates:
        _log("Last.fm: no similar artists returned.")
        return

    _log(f"Last.fm: {len(candidates)} unique similar artists collected before limit.")
    rows = []
    processed = 0

    for _, info in candidates.items():
        if processed >= max_artists:
            break
        artist_name = info.get("name") or ""
        profile_url = info.get("url") or ""
        seed_name = info.get("seed") or ""

        info_json = _lastfm_api_get("artist.getInfo", {
            "artist": artist_name,
            "autocorrect": 1
        }) or {}
        artist_obj = info_json.get("artist") or {}
        tags = artist_obj.get("tags", {}).get("tag", []) or []
        if isinstance(tags, dict):
            tags = [tags]
        tag_names = [t.get("name", "").strip() for t in tags if t.get("name")]
        primary_genre = tag_names[0].title() if tag_names else ""

        top_data = _lastfm_api_get("artist.getTopTracks", {
            "artist": artist_name,
            "limit": 1,
            "autocorrect": 1
        }) or {}
        top_tracks = top_data.get("toptracks", {}).get("track", []) or []
        if isinstance(top_tracks, dict):
            top_tracks = [top_tracks]
        song_title = ""
        if top_tracks:
            song_title = (top_tracks[0].get("name") or "").strip()

        website = ""
        socials = {}
        location = ""
        if profile_url:
            try:
                resp = session.get(profile_url, timeout=15)
                if resp.ok:
                    website, socials, location = _lastfm_extract_socials_and_website(resp.text, profile_url)
            except Exception as exc:
                _log(f"Last.fm: failed to fetch HTML for {profile_url}: {exc}")

        contact_links = []
        if website:
            contact_links.append(website)
        for link in (socials or {}).values():
            if link:
                contact_links.append(link)
        seen = set()
        clean_links = []
        for link in contact_links:
            if link not in seen:
                seen.add(link)
                clean_links.append(link)
        if not clean_links:
            if profile_url:
                clean_links.append(profile_url)
            else:
                clean_links.append("")

        sounds_like = f"Similar to {seed_name}" if seed_name else ""
        rows.append((
            artist_name,
            location,
            song_title,
            sounds_like,
            clean_links,
            "",
            "",
            "",
            "not present",
            primary_genre,
            "",
            "",
            "",
            ""
        ))
        processed += 1

    if not rows:
        _log("Last.fm: no rows to write.")
        return

    save_to_csv(rows, existing_csv)
    _log(f"Last.fm: total artists written {processed}")


def save_soundcloud_csv(rows, filename):
    _ensure_parent_dir(filename)
    headers = [
        'Artist Name', 'Location', 'Song Title', 'Sounds Like', 'Social Link', 'SoundCloud Link',
        'Played on triple J', 'Played on Unearthed', 'Release Date', 'Primary Genre', 'Date Added',
        'Email'
    ]
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    if not rows:
        pd.DataFrame(columns=headers).to_csv(filename, index=False, encoding="utf-8-sig")
        print(f"SoundCloud: created empty CSV with headers at {filename}")
        return

    existing_data = pd.DataFrame()
    if os.path.exists(filename):
        try:
            existing_data = pd.read_csv(filename)
        except Exception:
            existing_data = pd.DataFrame()
    for col in headers:
        if col not in existing_data.columns:
            existing_data[col] = ""

    new_df = pd.DataFrame(rows)
    for col in headers:
        if col not in new_df.columns:
            new_df[col] = ""
    new_df["Date Added"] = current_date
    for col in ["Social Link", "SoundCloud Link", "Location", "Artist Name", "Primary Genre", "Email"]:
        if col in new_df.columns:
            new_df[col] = new_df[col].fillna("").astype(str)

    combined = pd.concat([existing_data, new_df], ignore_index=True, sort=False)
    combined = combined[headers]
    combined = combined.drop_duplicates(subset=["SoundCloud Link", "Social Link", "Email"])
    combined.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"SoundCloud: data saved to {filename}")

# =========================== Bandcamp Scraper ===========================

# ---------------------------
# Bandcamp release date extraction (robust)
# ---------------------------
_BC_RELEASE_PATTERNS = [
    r"\breleased\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
    r"\breleased\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+(\d{4})"
]

_BC_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)

def _parse_any_date_to_iso(text: str):
    """
    Try to parse any human date into ISO YYYY-MM-DD.
    Returns (date_iso, precision) where precision is 'day'|'month'|'year'.
    """
    if not text:
        return None, None
    text_clean = " ".join(text.split())
    try:
        dt = dparser.parse(
            text_clean,
            fuzzy=True,
            dayfirst=False,
            default=datetime.datetime(1900, 1, 1)
        )
        year = dt.year
        now_year = datetime.datetime.now().year
        if 2000 <= year <= now_year + 1:
            return dt.strftime("%Y-%m-%d"), "day"
    except Exception:
        pass
    month_match = re.search(r"\b([A-Za-z]+)\s+(\d{4})\b", text_clean)
    if month_match:
        try:
            dt = dparser.parse(
                f"01 {month_match.group(1)} {month_match.group(2)}",
                fuzzy=True,
                dayfirst=True
            )
            return dt.strftime("%Y-%m-%d"), "month"
        except Exception:
            pass
    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", text_clean)
    if year_match:
        year = int(year_match.group(1))
        now_year = datetime.datetime.now().year
        if 2000 <= year <= now_year + 1:
            return f"{year:04d}-01-01", "year"
    return None, None

def _extract_from_json_ld(soup) -> tuple:
    """Scan all JSON-LD blocks for datePublished/uploadDate/dateCreated."""
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            for key in ("datePublished", "uploadDate", "dateCreated"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    date_iso, prec = _parse_any_date_to_iso(val)
                    if date_iso:
                        return date_iso, prec, val
    return None, None, None

def _extract_from_meta(soup) -> tuple:
    metas = []
    metas += soup.select('meta[itemprop="datePublished"]')
    metas += soup.select('meta[itemprop="dateCreated"]')
    metas += soup.select('meta[name="date"]')
    metas += soup.select('meta[property="music:release_date"]')
    for meta in metas:
        val = (meta.get("content") or meta.get("value") or "").strip()
        if val:
            date_iso, prec = _parse_any_date_to_iso(val)
            if date_iso:
                return date_iso, prec, val
    og_meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if og_meta:
        desc = (og_meta.get("content") or "").strip()
        if "released" in desc.lower():
            date_iso, prec = _parse_any_date_to_iso(desc)
            if date_iso:
                return date_iso, prec, desc
    return None, None, None

def _extract_from_tralbum_attr(soup) -> tuple:
    """Look for data-tralbum attributes embedded on the page."""
    for node in soup.find_all(attrs={"data-tralbum": True}):
        blob = node.get("data-tralbum")
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        blocks = []
        if isinstance(data, dict):
            blocks.append(data)
            current = data.get("current")
            if isinstance(current, dict):
                blocks.append(current)
            trackinfo = data.get("trackinfo")
            if isinstance(trackinfo, list):
                blocks.extend([ti for ti in trackinfo if isinstance(ti, dict)])
        for block in blocks:
            for key in ("release_date", "publish_date", "album_release_date", "date"):
                val = block.get(key)
                if isinstance(val, str) and val.strip():
                    date_iso, prec = _parse_any_date_to_iso(val)
                    if date_iso:
                        return date_iso, prec, val
    return None, None, None

def _extract_from_tralbum_data(soup) -> tuple:
    """Parse the inline TralbumData blob for release dates."""
    pattern = re.compile(r"var\s+TralbumData\s*=", re.IGNORECASE)
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or "TralbumData" not in text:
            continue
        match = pattern.search(text)
        if not match:
            continue
        remainder = text[match.end():].strip()
        brace_index = remainder.find("{")
        if brace_index == -1:
            continue
        json_text = remainder[brace_index:]
        brace_count = 0
        end_index = None
        for idx, ch in enumerate(json_text):
            if ch == "{":
                brace_count += 1
            elif ch == "}":
                brace_count -= 1
                if brace_count == 0:
                    end_index = idx + 1
                    break
        if end_index is None:
            continue
        payload = json_text[:end_index]
        try:
            data = json.loads(payload)
        except Exception:
            continue
        candidates = []
        blocks = []
        if isinstance(data, dict):
            blocks.append(data)
            current = data.get("current")
            if isinstance(current, dict):
                blocks.append(current)
            trackinfo = data.get("trackinfo")
            if isinstance(trackinfo, list):
                blocks.extend([ti for ti in trackinfo if isinstance(ti, dict)])
        for block in blocks:
            for key in ("release_date", "publish_date", "date", "album_release_date"):
                val = block.get(key)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())
        for val in candidates:
            date_iso, prec = _parse_any_date_to_iso(val)
            if date_iso:
                return date_iso, prec, val
    return None, None, None

def _extract_from_time_tag(soup) -> tuple:
    for time_el in soup.find_all("time"):
        dt_attr = (time_el.get("datetime") or "").strip()
        if dt_attr:
            date_iso, prec = _parse_any_date_to_iso(dt_attr)
            if date_iso:
                return date_iso, prec, dt_attr
        text = time_el.get_text(" ", strip=True)
        if text:
            date_iso, prec = _parse_any_date_to_iso(text)
            if date_iso:
                return date_iso, prec, text
    return None, None, None

def _extract_from_text_released(soup) -> tuple:
    containers = []
    containers += soup.select(".tralbum-credits")
    containers += soup.select(".tralbumData")
    containers += soup.select("#trackInfoInner, #bio-container")
    collected_text = " ".join([c.get_text(" ", strip=True) for c in containers]) or soup.get_text(" ", strip=True)
    for pattern in _BC_RELEASE_PATTERNS:
        match = re.search(pattern, collected_text, flags=re.IGNORECASE)
        if match:
            raw = match.group(0)
            date_iso, prec = _parse_any_date_to_iso(raw)
            if date_iso:
                return date_iso, prec, raw
    return None, None, None

def bandcamp_extract_release_date(html: str) -> dict:
    """
    Robust extractor. Order: JSON-LD -> meta -> <time> -> free-text 'released ...'
    Returns dict with keys date_iso, precision, raw.
    """
    soup = BeautifulSoup(html, "html.parser")
    extractors = (
        _extract_from_json_ld,
        _extract_from_tralbum_attr,
        _extract_from_tralbum_data,
        _extract_from_meta,
        _extract_from_time_tag,
        _extract_from_text_released,
    )
    for extractor in extractors:
        try:
            date_iso, precision, raw = extractor(soup)
            if date_iso:
                return {"date_iso": date_iso, "precision": precision, "raw": raw}
        except Exception:
            continue
    return {"date_iso": None, "precision": None, "raw": None}

# ---------------------------
# Bandcamp genres (tags) + sounds-like extraction
# ---------------------------
_BC_SOUNDS_PATTERNS = [
    r"\bffo\b[:\-–]\s*([^.;\n]+)",
    r"\briyl\b[:\-–]\s*([^.;\n]+)",
    r"\bfor\s+fans\s+of\b[:\-–]?\s*([^.;\n]+)",
    r"\bsounds\s+like\b[:\-–]?\s*([^.;\n]+)",
    r"\binfluences?\b[:\-–]?\s*([^.;\n]+)",
    r"\binspired\s+by\b[:\-–]?\s*([^.;\n]+)",
]

def _norm_tokens(line: str) -> list:
    """Split a comma/pipe/slash separated line into clean tokens."""
    if not line:
        return []
    parts = re.split(r"[,/|•]+|\band\b|\&", line, flags=re.IGNORECASE)
    cleaned = []
    for part in parts:
        token = re.sub(r"\s+", " ", part).strip(" .;:()[]{}\"\u2013\u2014").strip()
        if token:
            cleaned.append(token)
    seen = set()
    unique = []
    for token in cleaned:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    return unique

def bandcamp_extract_genres(soup) -> list:
    """Collect Bandcamp tags/genres from artist or album pages."""
    tags = set()
    for anchor in soup.select(".tralbum-tags a, a.tag, #tags a"):
        txt = anchor.get_text(" ", strip=True)
        if txt:
            tags.add(txt.lower())
    meta_keywords = soup.select_one('meta[name="keywords"]')
    if meta_keywords and meta_keywords.get("content"):
        for token in _norm_tokens(meta_keywords["content"]):
            if token:
                tags.add(token.lower())
    return list(tags)

def bandcamp_extract_sounds_like(soup) -> str:
    """Pull FFO/RIYL/sounds-like phrases from descriptive text."""
    blocks = []
    blocks += [b.get_text(" ", strip=True) for b in soup.select("#bio-container, .tralbum-credits, .tralbumData, #trackInfoInner")]
    desc_meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if desc_meta and desc_meta.get("content"):
        blocks.append(desc_meta["content"])
    text = " \n".join(filter(None, blocks))
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _BC_SOUNDS_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.group(1):
            tokens = _norm_tokens(match.group(1))
            if tokens:
                return ", ".join(t.title() for t in tokens[:5])
    fallback = re.search(r"\b(ffo|riyl)\b[:\-–]\s*([^.;\n]+)", text, flags=re.IGNORECASE)
    if fallback and fallback.group(2):
        tokens = _norm_tokens(fallback.group(2))
        if tokens:
            return ", ".join(t.title() for t in tokens[:5])
    return ""

# ---------------------------
# Bandcamp: card-level genre extraction (uses <p class="genre">)
# ---------------------------
def bandcamp_extract_primary_genre_from_card(card) -> str:
    """
    Extracts the visible genre displayed directly beneath the album title/artist
    on Bandcamp discover/tag grids (inside <p class="genre"> ... </p>).
    Falls back to text in the .meta container if the class is absent.
    """
    def _clean_text(raw: str) -> str:
        if not raw:
            return ""
        return re.sub(r"\s+", " ", raw).strip()

    def _extract(selector: str) -> str:
        el = card.select_one(selector)
        if not el:
            return ""
        txt = _clean_text(el.get_text(" ", strip=True))
        return txt.lower() if txt else ""

    genre_value = _extract("p.genre")
    if genre_value:
        return genre_value

    targeted_selectors = [
        ".meta .item-tag",
        ".meta p.subtext span.item-tag",
        ".meta [class*='tag']",
        ".meta [class*='genre']",
        ".result-info .item-tag",
        ".result-info [class*='tag']",
        ".result-info [class*='genre']",
        ".discover-item-info .item-tag"
    ]
    for selector in targeted_selectors:
        genre_value = _extract(selector)
        if genre_value:
            return genre_value

    meta_el = card.select_one(".meta, .result-info")
    if meta_el:
        lines = meta_el.get_text("\n", strip=True).split("\n")
        for line in reversed(lines):
            cleaned = _clean_text(line)
            if cleaned and "by " not in cleaned.lower() and len(cleaned) < 40:
                return cleaned.lower()
    alt = card.select_one("[class*='genre']")
    if alt:
        txt = _clean_text(alt.get_text(" ", strip=True))
        if txt:
            return txt.lower()
    return ""

def _bandcamp_card_candidates_with_genre(soup, base_url) -> list:
    """
    Returns list of dicts with candidate URLs and primary genres from Bandcamp grids.
    """
    selectors = ["li.results-grid-item", ".discover-results .item", ".music-grid .item"]
    out = []
    seen = set()
    excluded_hosts = {
        "bandcamp.com",
        "store.bandcamp.com",
        "daily.bandcamp.com",
        "blog.bandcamp.com",
        "community.bandcamp.com",
        "supporters.bandcamp.com"
    }
    for selector in selectors:
        for card in soup.select(selector):
            href = None
            for anchor in card.select("a[href]"):
                raw_href = (anchor.get("href") or "").strip()
                if not raw_href:
                    continue
                if raw_href.startswith("//"):
                    candidate = f"https:{raw_href}"
                elif raw_href.startswith("http"):
                    candidate = raw_href
                elif raw_href.startswith("/"):
                    candidate = urljoin(base_url, raw_href)
                elif "bandcamp.com" in raw_href:
                    candidate = f"https://{raw_href.lstrip('/')}"
                else:
                    continue
                lowered = candidate.lower()
                if any(token in lowered for token in ["/album", "/track", ".bandcamp.com"]):
                    href = candidate
                    break
            if not href or href in seen:
                continue
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if not host.endswith("bandcamp.com") or host in excluded_hosts:
                continue
            seen.add(href)
            out.append({
                "url": href,
                "primary_genre": bandcamp_extract_primary_genre_from_card(card)
            })
    return out

_BANDCAMP_EXCLUDED_HOSTS = {
    "bandcamp.com",
    "store.bandcamp.com",
    "daily.bandcamp.com",
    "blog.bandcamp.com",
    "community.bandcamp.com",
    "supporters.bandcamp.com"
}

def _bandcamp_extract_tag_from_url(url: str) -> str | None:
    if not url:
        return None
    match = re.search(r"/tag/([^/?#]+)", url)
    if match:
        return match.group(1).lower()
    return None

# Bandcamp resume-from-checkpoint helpers
def load_bandcamp_progress(progress_path: str) -> dict:
    if not progress_path:
        return {}
    try:
        with open(progress_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(f"Bandcamp: progress file {progress_path} is malformed, recreating.")
    except Exception as exc:
        print(f"Bandcamp: could not read progress file {progress_path}: {exc}")
    return {}


def save_bandcamp_progress(progress: dict, progress_path: str) -> None:
    if not progress_path:
        return
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2)
    except Exception as exc:
        print(f"Bandcamp: failed to write progress file {progress_path}: {exc}")

def _bandcamp_checkpoint_key(mode: str, base_key: str) -> str:
    normalized_mode = (mode or "discover").strip().lower()
    if not base_key:
        return ""
    return f"{normalized_mode}:{base_key}"

def _bandcamp_mode_output_csv_path(existing_csv: str, mode: str, search_domain: str | None = None, search_location: str | None = None) -> str:
    """
    For search runs, redirect to a distinct CSV so discover outputs remain untouched.
    Optionally append the search domain to avoid collisions between artists vs tracks searches.
    """
    normalized_mode = (mode or "discover").strip().lower()
    path = existing_csv or "bandcamp_output.csv"
    root, ext = os.path.splitext(path)
    ext = ext or ".csv"

    def _strip_search_suffix(base_root: str) -> str:
        lowered = base_root.lower()
        # Remove trailing _search, _search_domain, or _search_domain_loc_xxx variants.
        match = re.match(r"^(.*)_search(?:_[^_]+)?(?:_loc_.+)?$", lowered)
        if match and match.group(1):
            # Preserve original casing for the kept portion.
            keep_len = len(match.group(1))
            return base_root[:keep_len]
        return base_root

    def _sanitize_slug(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug[:40]

    if normalized_mode != "search":
        # If the user previously ran a search and left the _search suffix in the filename, strip it so
        # discover runs go back to the canonical path and never overwrite search outputs.
        root = _strip_search_suffix(root)
        return root + ext
    domain = (search_domain or "").strip().lower()
    loc_slug = _sanitize_slug(search_location) if search_location else ""
    # Strip any previous search suffix before constructing the new one.
    base_root = _strip_search_suffix(root)
    parts = [base_root, "_search"]
    if domain:
        parts.append(f"_{domain}")
    if loc_slug:
        parts.append(f"_loc_{loc_slug}")
    return "".join(parts) + ext

def _bandcamp_is_discover_url(url: str) -> bool:
    if not url:
        return False
    normalized = url.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized.lstrip('/')}"
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    host = parsed.netloc.lower()
    return "bandcamp.com" in host and "/discover" in parsed.path


def is_discover_page(url: str) -> bool:
    """Explicit discover-page detector for Bandcamp."""
    return _bandcamp_is_discover_url(url)


def _bandcamp_wait_for_discover_tiles(driver, selectors: list[str], timeout: int = 15) -> int:
    """Wait for discover tiles to render and trigger lazy JS by scrolling."""
    sel = ", ".join(selectors)
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
    except Exception:
        return 0
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass
    time.sleep(random.uniform(0.5, 1.0))
    try:
        driver.execute_script("window.scrollTo(0, Math.max(document.body.scrollHeight * 0.1, 0));")
    except Exception:
        pass
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, sel))
    except Exception:
        return 0

def _bandcamp_replace_query_param(url: str, key: str, value) -> str:
    if not url:
        return ""
    normalized = url.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized.lstrip('/')}"
    parsed = urlparse(normalized)
    pairs = []
    replaced = False
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k == key:
            if not replaced:
                pairs.append((k, str(value)))
                replaced = True
            continue
        pairs.append((k, v))
    if not replaced:
        pairs.append((key, str(value)))
    new_query = urlencode(pairs)
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

def _bandcamp_build_paged_url(base_url: str, page: int) -> str:
    try:
        page_number = int(page)
    except Exception:
        page_number = 1
    if page_number < 1:
        page_number = 1
    return _bandcamp_replace_query_param(base_url, "page", page_number)

def _bandcamp_build_discover_page_urls(base_url: str, pages: int) -> list[str]:
    if pages <= 0:
        pages = 1
    urls = []
    for index in range(pages):
        urls.append(_bandcamp_replace_query_param(base_url, "p", index))
    return urls

def _bandcamp_label_from_discover_url(url: str) -> str:
    if not url:
        return "discover:custom"
    try:
        parsed = urlparse(url)
    except Exception:
        return "discover:custom"
    params = parse_qs(parsed.query)
    parts = []
    loc = (params.get("loc") or [""])[0]
    if loc:
        parts.append(f"loc={loc}")
    genre = (params.get("g") or [""])[0]
    if genre:
        parts.append(f"genre={genre}")
    sort_mode = (params.get("s") or [""])[0]
    if sort_mode:
        parts.append(f"sort={sort_mode}")
    if not parts:
        return "discover:custom"
    return "discover:" + ",".join(parts)

def _normalize_location_text(value: str) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.strip().lower()

def _norm_text_(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    return re.sub(r"[\s,;|/]+", " ", value.strip())

def normalize_name(name: str) -> str:
    """
    Normalise a name for comparison: lowercase, strip accents/punctuation, collapse spaces.
    """
    if not isinstance(name, str):
        return ""
    cleaned = unicodedata.normalize("NFKD", name)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.lower()
    cleaned = re.sub(r"[.,!?:;\'\"\\-_/]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def _nf(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s)

def _bc_normalize_tag_slug(city_or_tag: str) -> str:
    s = (city_or_tag or "").strip().lower()
    s = re.sub(r"[,+]+", " ", s)
    s = re.sub(r"[\s/_]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s

def _bc_make_tag_url(slug: str, page: int) -> str:
    slug = _bc_normalize_tag_slug(slug)
    if not slug:
        return ""
    return f"https://bandcamp.com/tag/{quote(slug)}?tab=artists&page={int(page)}"

def _bc_is_bandcamp_url(u: str) -> bool:
    try:
        parsed = urlparse(u or "")
        return parsed.scheme in ("http", "https") and (parsed.netloc or "").lower().endswith("bandcamp.com")
    except Exception:
        return False

def _bc_url_kind(u: str) -> str:
    if not _bc_is_bandcamp_url(u):
        return "unknown"
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    if host == "bandcamp.com" and path.startswith("/discover"):
        return "discover"
    if host == "bandcamp.com" and "/tag/" in path:
        return "tag"
    if host.endswith(".bandcamp.com"):
        if "/album/" in path or "/track/" in path:
            return "album_or_track"
        return "artist_profile"
    if "/album/" in path or "/track/" in path:
        return "album_or_track"
    return "unknown"

def _bc_parse_discover_params(u: str) -> dict:
    parsed = urlparse(u or "")
    query = parse_qs(parsed.query or "")
    loc = (query.get("loc", [""])[0] or "").strip()
    sort_val = (query.get("s", ["new"])[0] or "new").strip() or "new"
    try:
        page = int((query.get("p", ["1"])[0] or "1").strip() or "1")
    except Exception:
        page = 1
    return {"loc": loc, "s": sort_val, "p": page}

def _bc_api_params_from_url(url: str) -> dict:
    parsed = urlparse((url or "").strip())
    query = parse_qs(parsed.query or "")
    params = {k: (v[-1] if isinstance(v, list) else v) for k, v in query.items()}
    base = {}
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    if segments and segments[0] in {"discover", "tag"} and len(segments) > 1:
        slug = _bc_normalize_tag_slug(segments[-1])
        if slug:
            base["t"] = slug
    if "tag" in params and not base.get("t"):
        slug = _bc_normalize_tag_slug(params["tag"])
        if slug:
            base["t"] = slug
    if not base.get("t") and params.get("t"):
        slug = _bc_normalize_tag_slug(params["t"])
        if slug:
            base["t"] = slug
    base["s"] = params.get("s") or params.get("sort") or "new"
    base["g"] = params.get("g") or "all"
    for key in ("loc", "f", "time", "format", "item_type", "c"):
        if key in params:
            base[key] = params[key]
    if params.get("tab") == "artists":
        base.setdefault("item_type", "a")
    return base

def _bc_discover_city_label_from_html(driver, url: str) -> str:
    try:
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        soup = BeautifulSoup(driver.page_source, "html.parser")
        text_block = soup.get_text(" ", strip=True)
        match = re.search(r"artists\s+from\s+([A-Za-z\-\s\.’']+)", text_block, re.IGNORECASE)
        if match:
            city = match.group(1).strip()
            if _is_valid_city_label(city):
                return city
        label = soup.select_one("[aria-pressed='true'], .selected, .active")
        if label:
            txt = label.get_text(" ", strip=True)
            match = re.search(r"artists\s+from\s+([A-Za-z\-\s\.’']+)", txt, re.IGNORECASE)
            if match:
                city = match.group(1).strip()
                if _is_valid_city_label(city):
                    return city
    except Exception:
        pass
    return ""

def _bc_discover_city_label_from_api(items: list) -> str:
    counts = {}
    for item in items or []:
        loc = (item.get("location_text") or "").strip()
        if not loc:
            continue
        city = (loc.split(",")[0] or "").strip()
        if not city:
            continue
        key = _nf(city)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    key = max(counts, key=counts.get)
    for item in items or []:
        loc = (item.get("location_text") or "").strip()
        if not loc:
            continue
        city = (loc.split(",")[0] or "").strip()
        if city and _nf(city) == key and _is_valid_city_label(city):
            return city
    return ""

def _bc_discover_api_fetch_items(params: dict) -> list:
    api_url = "https://bandcamp.com/api/discover/3/get_web"
    session = build_hardened_session()
    per_page = 40
    page_index = max(int(params.get("p", 1)) - 1, 0)
    query = {
        "loc": params.get("loc", ""),
        "s": params.get("s", "new") or "new",
        "p": page_index,
        "limit": per_page,
        "offset": page_index * per_page,
    }
    try:
        resp = session.get(api_url, params=query, timeout=(6, 15), headers=_rand_headers())
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        print(f"Bandcamp: discover API fetch failed: {exc}")
        return []
    return payload.get("items") or []
def _norm_txt(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value)


_COUNTRY_CANON = {
    "the netherlands": "netherlands",
    "holland": "netherlands",
    "u.k.": "united kingdom",
    "u.s.a.": "usa",
}

_INVALID_DISCOVER_SLUGS = {
    "anywhere-fresh-clear-all-filters-artist-index-new-releases-about-buttons",
}


def _is_valid_city_label(city: str) -> bool:
    slug = _bc_normalize_tag_slug(city)
    return bool(slug and slug not in _INVALID_DISCOVER_SLUGS)


def _canon_location(loc: str) -> str:
    loc = (loc or "").strip()
    if not loc:
        return ""
    parts = [part.strip() for part in loc.split(",")]
    if len(parts) >= 2:
        city = parts[0]
        country = ", ".join(parts[1:]).lower()
        country = _COUNTRY_CANON.get(country, country)
        city_tc = " ".join(word.capitalize() if word.isalpha() else word for word in re.split(r"\s+", city))
        country_tc = " ".join(word.capitalize() for word in country.split())
        return f"{city_tc}, {country_tc}"
    return loc

_BAD_CONTACT_DOMAINS = (
    "get.bandcamp.help",
    "help.bandcamp.com",
    "f0.bcbits.com",
    "f1.bcbits.com",
    "f2.bcbits.com",
    "f3.bcbits.com",
    "f4.bcbits.com",
    "f5.bcbits.com",
    "popplers5.bandcamp.com",
)

_BAD_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".mp3", ".flac", ".wav", ".zip")


def _bandcamp_contact_is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if not host:
            return False
        if any(host.endswith(bad) for bad in _BAD_CONTACT_DOMAINS):
            return False
        if any(path.endswith(ext) for ext in _BAD_EXTS):
            return False
        if parsed.scheme in ("mailto", "tel"):
            return True
        if parsed.scheme not in ("http", "https"):
            return False
        if host.endswith("bandcamp.com"):
            return False
        return True
    except Exception:
        return False


def _bandcamp_contact_is_valid(url: str) -> bool:
    return _bandcamp_contact_is_valid_url(url)

def _bandcamp_collect_contacts(artist_dict: dict) -> list:
    contacts = []
    for extra in artist_dict.get("all_social_links", []) or []:
        if extra and _bandcamp_contact_is_valid(extra):
            contacts.append(extra)
    website = artist_dict.get("website")
    if website and _bandcamp_contact_is_valid(website):
        contacts.append(website)
    socials = artist_dict.get("socials", {})
    for value in socials.values():
        if value and _bandcamp_contact_is_valid(value):
            contacts.append(value)
    email_values = []
    primary_email = (artist_dict.get("email") or "").strip()
    if primary_email:
        email_values.append(primary_email)
    for item in artist_dict.get("emails", []) or []:
        cleaned = (item or "").strip()
        if cleaned and cleaned not in email_values:
            email_values.append(cleaned)
    for email_value in email_values:
        contacts.append(f"mailto:{email_value}")
    seen = set()
    deduped = []
    for link in contacts:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped

_BC_GENRE_FILTER_TOKENS = {
    "rock",
    "pop",
    "punk",
    "metal",
    "jazz",
    "blues",
    "country",
    "electronic",
    "hip hop",
    "rap",
    "reggae",
    "ambient",
    "experimental",
    "indie",
    "alternative",
    "classical",
    "folk",
    "rnb",
    "soul",
}

def _bc_decode_filter(s: str | None) -> str | None:
    if not s:
        return s
    try:
        from urllib.parse import unquote
        decoded = unquote(s)
    except Exception:
        decoded = s
    decoded = decoded.replace("+", " ")
    return decoded

def _bc_canonicalize_location(value: str | None) -> str:
    """
    Canonical form for location comparison:
    - NFKC normalize and strip combining marks
    - lowercase
    - collapse any whitespace (including NBSP) and punctuation separators to a single space
    - remove spaces entirely for the comparison key
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[\u00A0]", " ", normalized)  # collapse NBSP
    normalized = re.sub(r"[\s,;|/\-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    canonical = normalized.replace(" ", "")
    alias_fixes = {
        "mancheter": "manchester",
        "britol": "bristol",
    }
    return alias_fixes.get(canonical, canonical)

def _bc_sanitize_location_filter(label: str | None) -> str:
    raw_label = label or ""
    decoded_label = _bc_decode_filter(raw_label) or ""
    sanitized_label = decoded_label.strip()
    norm = _norm_text_(sanitized_label)
    if norm in _BC_GENRE_FILTER_TOKENS:
        sanitized_label = ""
    if os.environ.get("BC_DEBUG_FILTER_SRC") == "1":
        debug_payload = {
            "raw_label": raw_label,
            "decoded_label": decoded_label,
            "sanitized_label": sanitized_label,
        }
        try:
            print(f"BC_DEBUG_FILTER_SRC: {json.dumps(debug_payload, ensure_ascii=False)}")
        except Exception:
            print(f"BC_DEBUG_FILTER_SRC: {debug_payload}")
    return sanitized_label

def _bandcamp_location_match_(profile_loc: str, api_hint: str, requested_label: str | None, requested_hint: str | None) -> bool:
    requested_label = _bc_decode_filter(requested_label)
    requested_hint = _bc_decode_filter(requested_hint)
    if not requested_label and not requested_hint:
        return True
    profile_norm = _norm_text_(profile_loc).replace("-", " ")
    api_norm = _norm_text_(api_hint).replace("-", " ")
    profile_norm_compact = profile_norm.replace(" ", "")
    api_norm_compact = api_norm.replace(" ", "")
    profile_canon = _bc_canonicalize_location(profile_loc)
    api_canon = _bc_canonicalize_location(api_hint)
    label_canon = _bc_canonicalize_location(requested_label)
    hint_canon = _bc_canonicalize_location(requested_hint)
    canada_subdiv_phrases = {
        "alberta",
        "british columbia",
        "manitoba",
        "new brunswick",
        "newfoundland",
        "newfoundland and labrador",
        "labrador",
        "nova scotia",
        "ontario",
        "prince edward island",
        "quebec",
        "saskatchewan",
        "northwest territories",
        "nunavut",
        "yukon",
    }
    canada_subdiv_tokens = {
        "ab",
        "bc",
        "mb",
        "nb",
        "ns",
        "nl",
        "pei",
        "qc",
        "on",
        "sk",
        "nt",
        "nu",
        "yt",
        "alberta",
        "manitoba",
        "brunswick",
        "newfoundland",
        "labrador",
        "nova",
        "scotia",
        "ontario",
        "prince",
        "edward",
        "island",
        "quebec",
        "saskatchewan",
        "northwest",
        "territories",
        "nunavut",
        "yukon",
        "columbia",  # used only in combination checks
        "british",    # used only in combination checks
    }

    def _has_canadian_subdivision(text: str, tokens: set[str]) -> bool:
        if not text and not tokens:
            return False
        lowered = text or ""
        for phrase in canada_subdiv_phrases:
            if phrase in lowered:
                return True
        combo_sets = [
            {"british", "columbia"},
            {"nova", "scotia"},
            {"prince", "edward", "island"},
            {"new", "brunswick"},
            {"newfoundland", "labrador"},
            {"northwest", "territories"},
        ]
        for combo in combo_sets:
            if combo.issubset(tokens):
                return True
        if tokens.intersection(canada_subdiv_tokens):
            return True
        return False

    requested_is_canada = False
    label_hint_norms = [requested_label or "", requested_hint or ""]
    for value in label_hint_norms:
        normed = _norm_text_(value)
        if "canada" in normed:
            requested_is_canada = True
            break
    alias_corrections = {
        _bc_canonicalize_location("au tralia"): "australia",
        _bc_canonicalize_location("autralia"): "australia",
        _bc_canonicalize_location("austra lia"): "australia",
    }

    def _canonical_variants(text: str) -> set[str]:
        if not text:
            return set()
        norm = _norm_text_(text).replace("-", " ")
        compact = norm.replace(" ", "")
        canon = _bc_canonicalize_location(text)
        variants = {norm, compact, canon}
        for candidate in (canon, compact, norm.replace(" ", "")):
            alias = alias_corrections.get(candidate)
            if alias:
                alias_norm = _norm_text_(alias).replace("-", " ")
                variants.update({alias_norm, alias_norm.replace(" ", ""), _bc_canonicalize_location(alias)})
        return {v for v in variants if v}
    # Simple synonym/alias support for common regions (not exhaustive).
    def _uk_match(text: str) -> bool:
        if not text:
            return False
        aliases = (
            "uk",
            "u k",
            "united kingdom",
            "great britain",
            "gb",
            "england",
            "scotland",
            "wales",
            "northern ireland",
        )
        return any(alias in text for alias in aliases)

    if requested_label:
        label_norm = _norm_text_(requested_label)
        label_norm_clean = label_norm.replace("-", " ")
        label_compact = label_norm_clean.replace(" ", "")
        for variant in _canonical_variants(requested_label):
            if variant in profile_norm or variant in api_norm or variant in profile_norm_compact or variant in api_norm_compact or variant in profile_canon or variant in api_canon:
                return True
        if label_norm_clean and (label_norm_clean in profile_norm or label_norm_clean in api_norm):
            return True
        if label_compact and (label_compact in profile_norm_compact or label_compact in api_norm_compact or label_compact in profile_canon or label_compact in api_canon):
            return True
        # Extra tolerance for UK-region filters that surface localized labels (e.g., "England, Uk").
        if label_norm_clean and _uk_match(label_norm_clean) and (_uk_match(profile_norm) or _uk_match(api_norm)):
            return True
    if requested_hint:
        hint_norm = _norm_text_(requested_hint)
        hint_norm_clean = hint_norm.replace("-", " ")
        hint_compact = hint_norm_clean.replace(" ", "")
        for variant in _canonical_variants(requested_hint):
            if variant in profile_norm or variant in api_norm or variant in profile_norm_compact or variant in api_norm_compact or variant in profile_canon or variant in api_canon:
                return True
        if hint_norm_clean and (hint_norm_clean in profile_norm or hint_norm_clean in api_norm):
            return True
        if hint_compact and (hint_compact in profile_norm_compact or hint_compact in api_norm_compact or hint_compact in profile_canon or hint_compact in api_canon):
            return True
        if hint_norm_clean and _uk_match(hint_norm_clean) and (_uk_match(profile_norm) or _uk_match(api_norm)):
            return True
    # Canonical compact contains check for either label or hint.
    for compact_filter in (label_canon, hint_canon):
        if compact_filter and (compact_filter in profile_canon or compact_filter in api_canon):
            return True
    # Country-level fallback: if filter mentions Canada, accept province/territory-only locations.
    if requested_is_canada:
        profile_tokens = {tok for tok in profile_norm.split() if len(tok) >= 2}
        api_tokens = {tok for tok in api_norm.split() if len(tok) >= 2}
        if _has_canadian_subdivision(profile_norm, profile_tokens) or _has_canadian_subdivision(api_norm, api_tokens):
            return True
    # Fallback: token overlap of two or more characters to avoid over-pruning on partial matches.
    profile_tokens = {tok for tok in profile_norm.split() if len(tok) >= 2}
    req_label_norm = _norm_text_(requested_label or "").replace("-", " ").replace(",", " ")
    req_hint_norm = _norm_text_(requested_hint or "").replace("-", " ").replace(",", " ")
    requested_tokens = {tok for tok in req_label_norm.split() if len(tok) >= 2}
    requested_tokens.update({tok for tok in req_hint_norm.split() if len(tok) >= 2})
    if profile_tokens and requested_tokens and profile_tokens.intersection(requested_tokens):
        return True
    if os.environ.get("BC_DEBUG_LOCATION") == "1":
        debug_payload = {
            "filter_raw": requested_label or requested_hint or "",
            "requested_tokens": sorted(requested_tokens),
            "filter_canon": label_canon or hint_canon,
            "profile_raw": profile_loc,
            "profile_norm": profile_norm,
            "profile_canon": profile_canon,
            "profile_tokens": sorted(profile_tokens),
            "api_hint_raw": api_hint,
            "api_norm": api_norm,
            "api_canon": api_canon,
            "api_tokens": sorted(api_tokens) if 'api_tokens' in locals() else [],
            "reason": "no_match"
        }
        try:
            print(f"BC_DEBUG_LOCATION: {json.dumps(debug_payload, ensure_ascii=False)}")
        except Exception:
            print(f"BC_DEBUG_LOCATION: {debug_payload}")
    return False


def _test_bandcamp_location_match():
    """Lightweight regression checks for _bandcamp_location_match_."""
    assert _bandcamp_location_match_("Montreal, Québec", "", "canada", "canada")
    assert _bandcamp_location_match_("Halifax, Nova Scotia", "", "canada", "canada")
    assert _bandcamp_location_match_("Port Coquitlam, British Columbia", "", "canada", "canada")
    assert not _bandcamp_location_match_("Paris, Île-de-France", "", "canada", "canada")
    assert _bandcamp_location_match_("Montreal, Québec", "", "quebec", "quebec")
    assert _bandcamp_location_match_("Halifax, Nova Scotia", "", "rock nova-", "rock nova-")
    assert _bandcamp_location_match_("Halifax, Nova Scotia", "", "nova-scotia", "nova-scotia")
    assert _bandcamp_location_match_("Québec", "", "qu%C3%A9bec", "qu%C3%A9bec")
    assert _bandcamp_location_match_("Montreal, Québec", "", "qu%C3%A9bec", "qu%C3%A9bec")
    assert not _bandcamp_location_match_("Montreal, Québec", "", "rock", "rock")
    assert _bandcamp_location_match_("Manchester, Uk", "", "manchester", "manchester")
    assert _bandcamp_location_match_("Bristol, Uk", "", "bri tol", "bri tol")
    print("_test_bandcamp_location_match passed")

def _test_bandcamp_location_filter_sanitizer():
    assert _bc_sanitize_location_filter("rock") == ""
    assert _bc_sanitize_location_filter("nova scotia") == "nova scotia"
    assert _bc_sanitize_location_filter("qu%C3%A9bec") == "québec"
    print("_test_bandcamp_location_filter_sanitizer passed")

def _test_bc_location_canonicalization():
    canon = _bc_canonicalize_location
    assert canon("manche ter") == canon("manchester")
    assert canon("Bri tol") == canon("Bristol")
    assert canon("Manchester, Uk").startswith(canon("manchester")) or canon("manchester") in canon("Manchester, Uk")
    print("_test_bc_location_canonicalization passed")

_BANDCAMP_CONTACT_PRIORITY = [
    ("linktr.ee", "linktree", "beacons.ai", "solo.to", "carrd.co", "band.link"),
    ("instagram.com",),
    ("soundcloud.com",),
    ("youtube.com", "youtu.be"),
    ("facebook.com", "fb.me", "m.me"),
    ("twitter.com", "x.com"),
    ("spotify.com",),
    ("bandcamp.com",),
]

def _best_contact_(links: list[str]) -> str:
    if not links:
        return ""
    cleaned = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            cleaned.append(link)
    for group in _BANDCAMP_CONTACT_PRIORITY:
        for link in cleaned:
            lowered = link.lower()
            if any(dom in lowered for dom in group):
                return link
    return cleaned[0]

def _bandcamp_parse_discover_params(url: str) -> dict:
    result = {"g": "all", "s": "new"}
    if not url:
        return result
    normalized = url.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized.lstrip('/')}"
    parsed = urlparse(normalized)
    params = {}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if not values:
            continue
        if key.lower().startswith("utm_"):
            continue
        params[key.lower()] = values[-1]
    if params.get("g"):
        result["g"] = params["g"]
    if params.get("s"):
        result["s"] = params["s"]
    loc_value = params.get("loc") or params.get("location")
    if loc_value:
        result["loc"] = loc_value
        if "loct" in params:
            result["loct"] = params.get("loct")
        else:
            result["loct"] = params.get("location_type", "1")
    tag_value = params.get("t") or params.get("tag")
    if tag_value:
        result["t"] = tag_value
    return result

def _bandcamp_location_label_from_url(url: str) -> dict:
    if not _bandcamp_is_discover_url(url):
        return {"display_label": "", "hint": ""}
    params = _bandcamp_parse_discover_params(url)
    loc_value = params.get("loc") or ""
    loc_value_decoded = _bc_decode_filter(loc_value) or ""
    display_label = ""
    try:
        session = build_hardened_session()
        response = session.get(url, headers=_rand_headers(), timeout=(6, 15))
        response.raise_for_status()
        match = re.search(r'id="DiscoverApp"[^>]+data-blob="([^"]+)"', response.text)
        if match:
            blob = json.loads(html.unescape(match.group(1)))
            locations = (
                blob.get("appData", {})
                    .get("initialState", {})
                    .get("locations", [])
            )
            for entry in locations:
                if str(entry.get("id")) == str(loc_value):
                    display_label = entry.get("label", "") or ""
                    break
    except Exception:
        display_label = ""
    if not display_label and loc_value and not loc_value.isdigit():
        display_label = loc_value_decoded or loc_value
    hint = ""
    if display_label:
        hint = display_label
    elif loc_value.isdigit():
        hint = f"loc:{loc_value}"
    elif loc_value:
        hint = loc_value_decoded or loc_value
    display_label = _bc_decode_filter(display_label) or ""
    hint = _bc_decode_filter(hint) or ""
    return {"display_label": display_label, "hint": hint}


def _bandcamp_fetch_profile_html(profile_url: str, session=None) -> str:
    session = session or _bandcamp_thread_session()
    try:
        response = session.get(profile_url, timeout=(6, 15), headers=_rand_headers())
        response.raise_for_status()
        return response.text
    except Exception:
        return ""

def _bandcamp_quick_visit(driver, profile_url: str) -> str:
    try:
        driver.set_page_load_timeout(20)
    except Exception:
        pass
    try:
        driver.get(profile_url)
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        return driver.page_source
    except Exception as exc:
        print(f"Bandcamp: selenium quick visit failed {profile_url}: {exc}")
        return ""


def _bandcamp_is_recent_enough(artist_dict: dict, search_cutoff: datetime.date | None) -> bool:
    """Return True when artist_dict passes the recency cutoff (or no cutoff)."""
    if not search_cutoff:
        return True
    value = artist_dict.get("latest_release_date", "")
    if isinstance(value, datetime.date):
        parsed_date = value
    else:
        text = str(value).strip()
        if not text or text.lower() == "not present":
            return True
        try:
            parsed_date = datetime.date.fromisoformat(text[:10])
        except Exception:
            try:
                iso_text, _ = _parse_any_date_to_iso(text)
                parsed_date = datetime.date.fromisoformat(iso_text[:10]) if iso_text else None
            except Exception:
                parsed_date = None
    if not parsed_date:
        return True
    return parsed_date >= search_cutoff


def _bandcamp_process_candidate_profiles(
    candidate_profiles: list,
    rows_limit: int,
    requested_label: str,
    requested_hint: str,
    normalized_mode: str,
    normalized_search_location: str,
    contacts_required: bool,
    search_cutoff: datetime.date | None,
    effective_search_domain: str,
    driver,
    *,
    smoke_cap_active: bool = False,
    fetch_html_fn=None,
    parse_html_fn=None,
    quick_visit_fn=None,
):
    """
    Process Bandcamp candidate profiles with early stop once rows_limit is reached.

    When smoke_cap_active=True, fetches are performed sequentially to avoid
    unnecessary HTTP/selenium work beyond the capped target.
    """
    fetch_html_fn = fetch_html_fn or _bandcamp_fetch_profile_html
    parse_html_fn = parse_html_fn or _bandcamp_parse_html
    quick_visit_fn = quick_visit_fn or _bandcamp_quick_visit

    aggregated = {}
    http_success = 0
    selenium_used = 0
    kept_after_location = 0
    rejected_location = 0
    sample_kept = ""
    sample_rejected = ""
    stop_processing = False
    debug_location_samples = {"kept": [], "rejected": []}
    search_skipped_old = 0
    search_skipped_location = 0

    def process_artist(candidate, artist_dict):
        nonlocal kept_after_location, sample_kept, sample_rejected, rejected_location, stop_processing, search_skipped_old, search_skipped_location
        if not artist_dict:
            return
        artist_dict["source_tag"] = candidate.get("source_tag", "")
        profile_location = artist_dict.get("location", "")
        api_hint = candidate.get("api_location", "")
        location_ok = _bandcamp_location_match_(profile_location, api_hint, requested_label, requested_hint)
        if (not location_ok and BANDCAMP_LOCATION_FALLBACK and requested_label and not profile_location):
            label_norm = _norm_text_(requested_label)
            hint_norm = _norm_text_(api_hint)
            if label_norm and label_norm in hint_norm:
                location_ok = True
        if os.environ.get("BC_DEBUG_LOCATION") == "1":
            debug_entry = {
                "raw_filter": requested_label or requested_hint or "",
                "canon_filter": _bc_canonicalize_location(requested_label or requested_hint),
                "raw_tile_loc": profile_location or api_hint or "",
                "canon_tile_loc": _bc_canonicalize_location(profile_location or api_hint),
                "api_hint": api_hint,
                "match": location_ok,
            }
            bucket = "kept" if location_ok else "rejected"
            if len(debug_location_samples.get(bucket, [])) < 3:
                debug_location_samples[bucket].append(debug_entry)
        if not location_ok:
            if normalized_mode == "search" and normalized_search_location and (not profile_location and not api_hint):
                location_ok = True
            else:
                rejected_location += 1
                if normalized_mode == "search" and normalized_search_location:
                    search_skipped_location += 1
                if not sample_rejected:
                    sample_rejected = f"{artist_dict.get('artist_name', '') or 'unknown'} ({artist_dict.get('location', '') or 'n/a'})"
                return
        contacts = _bandcamp_collect_contacts(artist_dict)
        if contacts_required and not contacts:
            return
        tile_info = {
            "artist": (candidate.get("tile_artist") or "").strip(),
            "title": (candidate.get("tile_title") or "").strip(),
        }
        key = artist_dict["profile_url"].rstrip("/").lower()
        if normalized_mode == "search" and search_cutoff:
            if not _bandcamp_is_recent_enough(artist_dict, search_cutoff):
                search_skipped_old += 1
                return
        entry = aggregated.get(key)
        if entry:
            existing_contacts = entry.get("contacts") or []
            if isinstance(existing_contacts, set):
                existing_contacts = list(existing_contacts)
            seen_existing = set(existing_contacts)
            for link in contacts:
                if link not in seen_existing:
                    existing_contacts.append(link)
                    seen_existing.add(link)
            entry["contacts"] = existing_contacts
            current = entry["artist"]
            for field in ["artist_name", "location", "latest_release_title", "latest_release_date", "website", "email"]:
                if not current.get(field) and artist_dict.get(field):
                    current[field] = artist_dict[field]
            entry["artist"] = current
            if tile_info and any(tile_info.values()) and not entry.get("tile"):
                entry["tile"] = tile_info
        else:
            aggregated[key] = {
                "artist": artist_dict,
                "contacts": list(contacts),
                "tile": tile_info
            }
        kept_after_location += 1
        if not sample_kept:
            sample_kept = f"{artist_dict.get('artist_name', '')} ({artist_dict.get('location', '')})"
        if kept_after_location >= rows_limit:
            stop_processing = True

    fallback_candidates = []

    if smoke_cap_active:
        for cand in candidate_profiles:
            if stop_processing:
                break
            html = fetch_html_fn(cand["profile_url"])
            if html:
                http_success += 1
                artist_dict = parse_html_fn(cand["profile_url"], html, cand.get("seed_genre", ""))
                process_artist(cand, artist_dict)
            else:
                fallback_candidates.append(cand)
            if stop_processing:
                break
    else:
        http_workers = min(6, len(candidate_profiles)) or 1
        with ThreadPoolExecutor(max_workers=http_workers) as executor:
            future_map = {}
            for cand in candidate_profiles:
                if stop_processing:
                    break
                fut = executor.submit(fetch_html_fn, cand["profile_url"])
                future_map[fut] = cand
            for future in as_completed(future_map):
                if stop_processing:
                    break
                cand = future_map[future]
                html = future.result()
                if html:
                    http_success += 1
                    artist_dict = parse_html_fn(cand["profile_url"], html, cand.get("seed_genre", ""))
                    process_artist(cand, artist_dict)
                else:
                    fallback_candidates.append(cand)
                if stop_processing:
                    break

    for cand in fallback_candidates:
        if stop_processing:
            break
        html = quick_visit_fn(driver, cand["profile_url"])
        if not html:
            continue
        selenium_used += 1
        artist_dict = parse_html_fn(cand["profile_url"], html, cand.get("seed_genre", ""))
        process_artist(cand, artist_dict)
        if stop_processing:
            break

    return aggregated, {
        "http_success": http_success,
        "selenium_used": selenium_used,
        "kept_after_location": kept_after_location,
        "rejected_location": rejected_location,
        "sample_kept": sample_kept,
        "sample_rejected": sample_rejected,
        "stop_processing": stop_processing,
        "debug_location_samples": debug_location_samples,
        "search_skipped_old": search_skipped_old,
        "search_skipped_location": search_skipped_location,
    }

def scrape_bandcamp(
    seed_tags_or_url,
    pages_per_tag=5,
    existing_csv="artist_social_links.csv",
    max_artists=200,
    progress_path="bandcamp_progress.json",
    mode: str = "discover",
    max_pages: int | None = None,
    max_items: int | None = None,
    search_domain: str = "artists",
    search_location_filter: str = "",
):
    """
    High-level Bandcamp entry point. Accepts any Bandcamp URL (discover/tag/artist/album/track)
    or the legacy list of tag seeds.
    """
    def _read_smoke_seed_cap() -> int:
        raw = os.environ.get("SMOKE_SEED_CAP", "").strip()
        try:
            value = int(raw)
        except Exception:
            return 0
        return value if value > 0 else 0

    driver = setup_driver()
    url_input = ""
    seed_tags = []

    if isinstance(seed_tags_or_url, str):
        candidate = seed_tags_or_url.strip()
        if candidate:
            if _bc_is_bandcamp_url(candidate):
                url_input = candidate
            else:
                seed_tags = [candidate]
    elif isinstance(seed_tags_or_url, (list, tuple, set)):
        for entry in seed_tags_or_url:
            if entry is None:
                continue
            value = str(entry).strip()
            if value:
                seed_tags.append(value)
    else:
        seed_tags = list(BANDCAMP_SEED_TAGS)

    if not seed_tags and not url_input:
        seed_tags = list(BANDCAMP_SEED_TAGS)

    try:
        pages_to_scan = int(max_pages if max_pages is not None else pages_per_tag)
    except Exception:
        pages_to_scan = BANDCAMP_DISCOVER_PAGES_DEFAULT
    if pages_to_scan <= 0:
        pages_to_scan = BANDCAMP_DISCOVER_PAGES_DEFAULT

    normalized_mode = (mode or "discover").strip().lower()
    if normalized_mode not in {"discover", "tag", "search"}:
        raise ValueError(f"Unknown Bandcamp mode: {mode}")
    normalized_search_domain = (search_domain or "artists").strip().lower() or "artists"
    if normalized_search_domain not in {"artists", "tracks"}:
        normalized_search_domain = "artists"
    effective_search_domain = normalized_search_domain if normalized_mode == "search" else ""
    normalized_search_location = _bc_sanitize_location_filter(search_location_filter)

    existing_csv = _bandcamp_mode_output_csv_path(
        existing_csv,
        normalized_mode,
        search_domain=effective_search_domain or None,
        search_location=normalized_search_location if normalized_mode == "search" else None,
    )

    explicit_max = max_items if max_items and max_items > 0 else None
    user_max = explicit_max if explicit_max is not None else (max_artists if max_artists and max_artists > 0 else None)
    rows_limit = user_max if user_max else BANDCAMP_TARGET_ROWS or BANDCAMP_PAGES_PER_TAG
    rows_limit = max(rows_limit, 1)

    smoke_seed_cap = _read_smoke_seed_cap()
    smoke_cap_active = smoke_seed_cap > 0 and normalized_mode == "discover"
    if smoke_cap_active:
        capped_rows_limit = min(rows_limit, smoke_seed_cap)
        if capped_rows_limit != rows_limit:
            print(f"[Bandcamp Smoke Cap] Limiting target rows to {capped_rows_limit} (from SMOKE_SEED_CAP)")
        rows_limit = capped_rows_limit

    # Let explicit user max drive the candidate cap; default keeps prior heuristic.
    if user_max:
        computed_cap = max(user_max * 5, user_max + 40)
    else:
        computed_cap = max(BANDCAMP_TARGET_ROWS * 5, BANDCAMP_TARGET_ROWS + 40)
    if BANDCAMP_MAX_CANDIDATES and BANDCAMP_MAX_CANDIDATES > 0:
        computed_cap = max(computed_cap, BANDCAMP_MAX_CANDIDATES)
    if user_max:
        computed_cap = max(computed_cap, user_max)
    # Keep candidate cap aligned with capped rows to avoid extra pages in smoke mode.
    if smoke_cap_active:
        computed_cap = min(computed_cap, rows_limit * 2)
    max_candidates = max(computed_cap, rows_limit)
    hit_candidate_cap = False

    # Bandcamp resume-from-checkpoint
    progress_path = progress_path or "bandcamp_progress.json"
    progress = load_bandcamp_progress(progress_path)
    progress_base_key = (url_input or "").strip().rstrip("/").lower()
    checkpoint_key = _bandcamp_checkpoint_key(normalized_mode, progress_base_key)
    legacy_key = progress_base_key if normalized_mode == "discover" else ""
    checkpoint_keys = [key for key in {checkpoint_key, legacy_key} if key]
    # If the target CSV was deleted, reset checkpoint for this URL so artists can be re-scraped.
    if checkpoint_keys and not os.path.exists(existing_csv):
        reset_any = False
        for key in checkpoint_keys:
            if progress.pop(key, None) is not None:
                reset_any = True
        if reset_any:
            print(f"Bandcamp: output CSV missing; resetting checkpoint for {checkpoint_keys[0]}")
            save_bandcamp_progress(progress, progress_path)
    elif checkpoint_key and os.path.exists(existing_csv):
        print(f"Bandcamp: appending new artists to existing CSV {existing_csv} (checkpoint retained).")
    seen_artist_urls = set()
    for key in checkpoint_keys:
        seen_artist_urls.update(progress.get(key, {}).get("scraped_artist_urls", []) if key else [])
    progress_save_counter = 0

    bandcamp_rows = []
    enriched_rows = []
    candidate_profiles = []
    seen_profiles = set()
    requested_label = ""
    requested_hint = ""
    slug_parts = []
    loc_guess = ""
    if url_input and _bandcamp_is_discover_url(url_input):
        loc_meta = _bandcamp_location_label_from_url(url_input)
        requested_label = _bc_sanitize_location_filter(loc_meta.get("display_label", "") or "")
        requested_hint = _bc_sanitize_location_filter(loc_meta.get("hint", "") or "")
        if not requested_hint:
            params = _bandcamp_parse_discover_params(url_input)
            requested_hint = _bc_sanitize_location_filter(params.get("loc") or params.get("location") or "")
        parsed = urlparse(url_input)
        segments = [seg for seg in (parsed.path or "").split("/") if seg]
        if len(segments) >= 2 and segments[0] == "discover":
            slug = segments[1]
            slug_parts = [part for part in re.split(r"[+\s]+", slug) if part]
        if len(slug_parts) >= 2:
            loc_guess = " ".join(slug_parts[:-1]).strip()
            loc_guess = _bc_sanitize_location_filter(loc_guess)
            if loc_guess and not requested_hint:
                requested_hint = loc_guess
        # Also include the slug-derived location as a hint so spacing/label issues don't prune everything.
        if loc_guess:
            loc_norm = _norm_text_(loc_guess)
            label_norm = _norm_text_(requested_label)
            if requested_label and loc_norm and loc_norm not in label_norm:
                requested_label = loc_guess
            if requested_hint and loc_guess.lower() not in requested_hint.lower():
                requested_hint = f"{requested_hint} {loc_guess}".strip()
            elif requested_label and loc_guess.lower() not in requested_label.lower():
                requested_hint = loc_guess
        requested_label = _bc_sanitize_location_filter(requested_label)
        requested_hint = _bc_sanitize_location_filter(requested_hint)
        if requested_label or requested_hint:
            print(f"Bandcamp: applying location filter -> {requested_label or requested_hint}")
    elif normalized_mode == "search" and normalized_search_location:
        requested_label = normalized_search_location
        requested_hint = normalized_search_location
        print(f"Bandcamp: applying search location filter -> {normalized_search_location}")
    contacts_required = BANDCAMP_MIN_CONTACT_REQUIREMENT and not bool(url_input)
    search_cutoff = datetime.date.today() - datetime.timedelta(days=730) if normalized_mode == "search" else None
    search_skipped_old = 0
    search_skipped_location = 0

    def enqueue_candidate(source_tag, candidate, api_location=""):
        nonlocal hit_candidate_cap
        profile_url = _bandcamp_resolve_artist_profile_url(candidate.get("url"))
        if not profile_url:
            return False
        key = profile_url.rstrip("/").lower()
        if checkpoint_key and key in seen_artist_urls:
            print(f"Bandcamp: skip (checkpoint) {profile_url}")
            return False
        if key in seen_profiles or len(candidate_profiles) >= max_candidates:
            if len(candidate_profiles) >= max_candidates:
                hit_candidate_cap = True
            return False
        seen_profiles.add(key)
        candidate_profiles.append({
            "profile_url": profile_url,
            "seed_genre": candidate.get("primary_genre", ""),
            "source_tag": source_tag,
            "api_location": api_location,
        })
        if len(candidate_profiles) >= max_candidates:
            hit_candidate_cap = True
        return True

    def collect_tag_candidates(tag_list, params_map=None):
        params_map = params_map or {}
        if not tag_list:
            return
        for tag in tag_list:
            if not tag:
                continue
            print(f"Bandcamp: scanning tag '{tag}', pages={pages_to_scan}")
            candidates = _bandcamp_collect_tag_pages(
                driver,
                tag,
                pages_to_scan,
                search_query=tag,
                api_params=params_map.get(tag),
                max_items=max_candidates
            )
            print(f"Bandcamp: tag '{tag}' yielded {len(candidates)} raw candidates")
            if not candidates:
                continue
            for candidate in candidates:
                if not enqueue_candidate(tag, candidate, candidate.get("location", "")):
                    if hit_candidate_cap:
                        break
            if hit_candidate_cap:
                break

    url_processed = False

    try:
        if url_input:
            if normalized_mode == "discover":
                kind = _bc_url_kind(url_input)
                if kind == "discover":
                    url_processed = True
                    print("Bandcamp: collecting tiles directly from discover page")
                    discover_tiles = scrape_bandcamp_discover(driver, url_input, pages_to_scan, max_items=max_candidates)
                    print(f"Bandcamp: discover yielded {len(discover_tiles)} raw candidates")
                    for tile in discover_tiles:
                        enqueue_candidate("discover", tile, tile.get("location", ""))
                elif kind == "tag":
                    tag_slug = _bandcamp_extract_tag_from_url(url_input)
                    if tag_slug:
                        params_map = {tag_slug: _bc_api_params_from_url(url_input)}
                        collect_tag_candidates([tag_slug], params_map=params_map)
                        url_processed = True
                elif kind in {"artist_profile", "album_or_track"}:
                    resolved = _bandcamp_resolve_artist_profile_url(url_input)
                    if resolved:
                        print(f"Bandcamp: queuing profile {resolved}")
                        url_processed = enqueue_candidate("direct", {"url": resolved, "primary_genre": ""})
                    else:
                        print(f"Bandcamp: unable to resolve artist profile from {url_input}")
                else:
                    extracted_tag = _bandcamp_extract_tag_from_url(url_input)
                    if extracted_tag:
                        collect_tag_candidates([extracted_tag])
                        url_processed = True
                    else:
                        print("Bandcamp: unknown URL shape; falling back to legacy tag flow.")
            elif normalized_mode == "tag":
                url_processed = True
                tag_candidates = scrape_bandcamp_tag(driver, url_input, max_pages=pages_to_scan, max_items=max_candidates)
                for candidate in tag_candidates:
                    enqueue_candidate("tag", candidate, candidate.get("location", ""))
            elif normalized_mode == "search":
                url_processed = True
                search_candidates = scrape_bandcamp_search(
                    driver,
                    url_input,
                    max_pages=pages_to_scan,
                    max_items=max_candidates,
                    search_domain=normalized_search_domain,
                )
                for candidate in search_candidates:
                    enqueue_candidate("search", candidate, candidate.get("location", ""))
        if not url_processed:
            collect_tag_candidates(seed_tags or list(BANDCAMP_SEED_TAGS))

        print(f"Bandcamp: total candidate links found {len(candidate_profiles)}")

        aggregated, stats = _bandcamp_process_candidate_profiles(
            candidate_profiles,
            rows_limit,
            requested_label,
            requested_hint,
            normalized_mode,
            normalized_search_location,
            contacts_required,
            search_cutoff,
            effective_search_domain,
            driver,
            smoke_cap_active=smoke_cap_active,
        )

        http_success = stats["http_success"]
        selenium_used = stats["selenium_used"]
        kept_after_location = stats["kept_after_location"]
        rejected_location = stats["rejected_location"]
        sample_kept = stats["sample_kept"]
        sample_rejected = stats["sample_rejected"]
        stop_processing = stats["stop_processing"]
        debug_location_samples = stats["debug_location_samples"]
        search_skipped_old = stats["search_skipped_old"]
        search_skipped_location = stats["search_skipped_location"]

        stop_reason = "done"
        if kept_after_location >= rows_limit:
            stop_reason = "target"
        elif hit_candidate_cap:
            stop_reason = "cap"

        print(f"Bandcamp: candidates={len(candidate_profiles)} http_ok={http_success} selenium_fallback={selenium_used} kept_after_location={kept_after_location} stop_reason={stop_reason}")
        if normalized_mode == "search" and search_cutoff:
            print(f"Bandcamp (search, domain={normalized_search_domain}): kept_recent={kept_after_location} skipped_old={search_skipped_old} cutoff={search_cutoff.isoformat()}")
            if normalized_search_location:
                print(f"Bandcamp (search): skipped_location_mismatch={search_skipped_location} filter='{normalized_search_location}'")
        if sample_kept:
            print(f"Bandcamp: kept sample -> {sample_kept}")
        if sample_rejected:
            print(f"Bandcamp: rejected (location) sample -> {sample_rejected}")
        if os.environ.get("BC_DEBUG_LOCATION") == "1":
            for entry in debug_location_samples.get("kept", []):
                try:
                    print(f"BC_DEBUG_LOCATION: keep {json.dumps(entry, ensure_ascii=False)}")
                except Exception:
                    print(f"BC_DEBUG_LOCATION: keep {entry}")
            for entry in debug_location_samples.get("rejected", []):
                try:
                    print(f"BC_DEBUG_LOCATION: reject {json.dumps(entry, ensure_ascii=False)}")
                except Exception:
                    print(f"BC_DEBUG_LOCATION: reject {entry}")
        if kept_after_location == 0 and requested_label:
            print("Bandcamp: warning – no profiles matched requested location. Consider widening filters.")

        for entry in aggregated.values():
            artist_dict = entry["artist"]
            raw_contacts = entry.get("contacts") or []
            if isinstance(raw_contacts, set):
                raw_contacts = list(raw_contacts)
            contacts = [link for link in raw_contacts if _bandcamp_contact_is_valid_url(link)]
            best_contact = _best_contact_(contacts) if contacts else ""
            tile = entry.get("tile") or {}
            tile_artist = (tile.get("artist") or "").strip()
            tile_title = (tile.get("title") or "").strip()
            primary_genre_value = artist_dict.get("primary_genre", "")
            if isinstance(primary_genre_value, str):
                primary_genre_value = primary_genre_value.title()
            release_date_value = artist_dict.get("latest_release_date", "") or ""
            contact_payload = ", ".join(contacts) if contacts else best_contact
            email_value = ", ".join(
                [email.strip() for email in (artist_dict.get("emails") or []) if email and email.strip()]
            )
            if not email_value:
                email_value = (artist_dict.get("email", "") or "").strip()
            song_title_value = tile_title or artist_dict.get("latest_release_title", "") or ""
            artist_name_value = tile_artist or artist_dict.get("artist_name", "")
            bandcamp_search_domain = effective_search_domain
            bandcamp_rows.append((
                artist_name_value,
                artist_dict.get("location", ""),
                song_title_value,
                artist_dict.get("sounds_like", ""),
                contact_payload,
                "",
                "",
                "",
                release_date_value,
                primary_genre_value,
                "",
                normalized_mode,
                bandcamp_search_domain,
                email_value
            ))
            socials = artist_dict.get("socials", {})
            enriched_rows.append({
                "Artist Name": artist_name_value,
                "Profile URL": artist_dict.get("profile_url", ""),
                "Website": artist_dict.get("website", ""),
                "Email": email_value,
                "Instagram": socials.get("instagram", ""),
                "Twitter": socials.get("twitter", ""),
                "Facebook": socials.get("facebook", ""),
                "Linktree": socials.get("linktree", ""),
                "YouTube": socials.get("youtube", ""),
                "Location": artist_dict.get("location", ""),
                "Genres": "; ".join(artist_dict.get("genres", [])),
                "Latest Release": artist_dict.get("latest_release_title", ""),
                "Latest Release Date": artist_dict.get("latest_release_date", ""),
                "Latest Release Precision": artist_dict.get("latest_release_precision", ""),
                "Sounds Like": artist_dict.get("sounds_like", ""),
                "Primary Genre": primary_genre_value,
                "Source Tag": artist_dict.get("source_tag", ""),
                "Bandcamp_Source_Mode": normalized_mode,
                "Bandcamp_Search_Domain": bandcamp_search_domain
            })
            # Bandcamp resume-from-checkpoint: persist profile URLs once written to CSV rows
            profile_key = (artist_dict.get("profile_url", "") or "").rstrip("/").lower()
            if checkpoint_keys and profile_key:
                updated_any = False
                for checkpoint in checkpoint_keys:
                    if not isinstance(progress.get(checkpoint), dict):
                        progress[checkpoint] = {"scraped_artist_urls": []}
                    progress_entry = progress.setdefault(checkpoint, {"scraped_artist_urls": []})
                    current_list = progress_entry.get("scraped_artist_urls") or []
                    current_set = {str(u).rstrip("/").lower() for u in current_list if isinstance(u, str)}
                    if profile_key not in current_set:
                        current_set.add(profile_key)
                        progress_entry["scraped_artist_urls"] = list(current_set)
                        seen_artist_urls.add(profile_key)
                        updated_any = True
                if updated_any:
                    progress_save_counter += 1
                    if progress_save_counter % 5 == 0:
                        save_bandcamp_progress(progress, progress_path)
            if len(bandcamp_rows) >= rows_limit:
                break
        if checkpoint_key:
            save_bandcamp_progress(progress, progress_path)
    finally:
        driver.quit()
    if bandcamp_rows:
        save_to_csv(bandcamp_rows, existing_csv)
    else:
        # Ensure the pipeline still produces a tangible CSV, even if location filters prune everything.
        save_to_csv([], existing_csv)
    _bandcamp_write_enriched_csv(enriched_rows, existing_csv)

def _bandcamp_base_from_page(page_url: str) -> str:
    if not page_url:
        return "https://bandcamp.com"
    if "/tag/" in page_url:
        return page_url.split("/tag/")[0] or "https://bandcamp.com"
    if "/discover" in page_url:
        return page_url.split("/discover")[0] or "https://bandcamp.com"
    return "https://bandcamp.com"


def _bandcamp_candidates_from_html(html: str, page_url: str) -> list:
    candidates = []
    if not html:
        return candidates
    try:
        soup = BeautifulSoup(html, 'html.parser')
        base_url = _bandcamp_base_from_page(page_url)
        candidates = _bandcamp_card_candidates_with_genre(soup, base_url)
        if not candidates:
            for anchor in soup.find_all('a', href=True):
                href = anchor['href']
                absolute = urljoin(page_url, href)
                if not absolute:
                    continue
                parsed = urlparse(absolute)
                host = (parsed.netloc or "").lower()
                path = (parsed.path or "").lower()
                if not host.endswith("bandcamp.com") or host in _BANDCAMP_EXCLUDED_HOSTS:
                    continue
                allowed_path = (
                    path in ("", "/") or
                    path.startswith("/album") or
                    path.startswith("/track") or
                    path.startswith("/music")
                )
                if allowed_path:
                    candidates.append({
                        "url": f"{parsed.scheme or 'https'}://{host}{parsed.path}",
                        "primary_genre": ""
                    })
    except Exception as exc:
        print(f"Bandcamp: failed to parse HTML listing {page_url}: {exc}")
    return candidates

_BANDCAMP_GRID_SELECTORS = [
    "li.searchresult",
    ".result-items li",
    "li.results-grid-item",
    ".results-grid-item",
    "[data-test^='results-grid-item']",
    ".music-grid .item",
    ".discover-results .item",
    "ul.results-grid li",
]


def _bandcamp_trigger_pagination(driver) -> bool:
    """Scroll or click a 'load more' button if present."""
    # Ensure we are at the bottom to reveal the button.
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass
    # Try the explicit "View more results" button used on discover grids first.
    view_more_selectors = [
        "#view-more",
        "button[data-test='view-more']",
        "button.g-button#view-more"
    ]
    for sel in view_more_selectors:
        try:
            button = driver.find_element(By.CSS_SELECTOR, sel)
            if button.is_displayed() and button.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", button)
                try:
                    driver.execute_script("arguments[0].click();", button)
                except Exception:
                    button.click()
                try:
                    WebDriverWait(driver, 6).until(
                        EC.invisibility_of_element_located((By.CSS_SELECTOR, "#view-more.g-button[disabled]"))
                    )
                except Exception:
                    pass
                print(f"Bandcamp: clicked view-more via selector {sel}")
                return True
        except Exception:
            continue
    # Fallback: attempt by text
    try:
        button = driver.find_element(By.XPATH, "//button[contains(., 'View more results')]")
        if button.is_displayed() and button.is_enabled():
            driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", button)
            try:
                driver.execute_script("arguments[0].click();", button)
            except Exception:
                button.click()
            print("Bandcamp: clicked view-more via text xpath")
            return True
    except Exception:
        pass

    buttons = [
        "button.more-items",
        "button.show-more",
        "button.load-more",
        "#view-more",
        ".view-more",
        "button[data-action='more']",
    ]
    for selector in buttons:
        try:
            button = driver.find_element(By.CSS_SELECTOR, selector)
            if not button.is_displayed() or not button.is_enabled():
                continue
            driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", button)
            button.click()
            return True
        except Exception:
            continue
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        return True
    except Exception:
        return False


def _bandcamp_collect_with_pagination(driver, start_url: str, selectors: list[str], max_items: int | None = None) -> list:
    """
    Attempt to exhaust 'load more' / infinite scroll on a Bandcamp grid page.
    Stops when no new items appear for a few attempts or when max_items is reached.
    """
    print(f"Bandcamp: pagination start {start_url} max_items={max_items or 'none'} selectors={len(selectors)}")
    tile_selector = ", ".join(selectors) if selectors else "body"
    seen = set()
    collected = []
    no_change_attempts = 0
    max_no_change_attempts = 6
    max_cycles = 60
    try:
        driver.get(start_url)
        WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.CSS_SELECTOR, tile_selector)))
    except Exception as exc:
        print(f"Bandcamp: error loading {start_url}: {exc}")
    for _ in range(max_cycles):
        try:
            html_text = driver.page_source or ""
        except Exception:
            html_text = ""
        page_candidates = _bandcamp_candidates_from_html(html_text, start_url)
        new_added = 0
        for cand in page_candidates:
            key = (cand.get("url") or "").rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(cand)
            new_added += 1
            if max_items and len(collected) >= max_items:
                print(f"Bandcamp: pagination stopping (hit max_items {max_items}) with {len(collected)} collected")
                return collected
        if new_added == 0:
            no_change_attempts += 1
        else:
            no_change_attempts = 0
        print(f"Bandcamp: pagination loop collected={len(collected)} new={new_added} no_change_attempts={no_change_attempts}")
        if max_items and len(collected) >= max_items:
            break
        try:
            prev_count = len(driver.find_elements(By.CSS_SELECTOR, tile_selector))
        except Exception:
            prev_count = len(collected)
        triggered = _bandcamp_trigger_pagination(driver)
        if not triggered:
            print("Bandcamp: pagination could not trigger load-more/scroll; stopping soon")
            no_change_attempts += 1
            if no_change_attempts >= max_no_change_attempts:
                break
        try:
            WebDriverWait(driver, 15).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, tile_selector)) > prev_count
            )
        except Exception:
            pass
        time.sleep(random.uniform(1.0, 1.8))
    print(f"Bandcamp: pagination end with {len(collected)} collected")
    return collected

def _bandcamp_collect_mode_pages(driver, base_url: str, mode_label: str, selectors: list[str], max_pages: int, max_items: int | None = None, empty_streak_limit: int = 3) -> list:
    max_pages = max(1, int(max_pages or 1))
    selectors = selectors or _BANDCAMP_GRID_SELECTORS
    collected = []
    seen = set()
    empty_streak = 0
    for page_index in range(max_pages):
        page_number = page_index + 1
        page_url = _bandcamp_build_paged_url(base_url, page_number)
        try:
            driver.get(page_url)
        except Exception as exc:
            print(f"Bandcamp: {mode_label} load error page {page_number}: {exc}")
        tile_count = _bandcamp_wait_for_discover_tiles(driver, selectors, timeout=15)
        print(f"Bandcamp: {mode_label} page {page_number} raw tiles = {tile_count}")
        try:
            page_candidates = _bandcamp_candidates_from_html(driver.page_source or "", page_url)
        except Exception:
            page_candidates = []
        page_seen = set()
        unique_candidates = []
        for cand in page_candidates:
            key = (cand.get("url") or "").rstrip("/").lower()
            if not key or key in page_seen:
                continue
            page_seen.add(key)
            unique_candidates.append(cand)
        unique_count = len(unique_candidates)
        new_added = 0
        for cand in unique_candidates:
            key = (cand.get("url") or "").rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            collected.append(cand)
            new_added += 1
            if max_items and len(collected) >= max_items:
                break
        print(f"Bandcamp: {mode_label} page {page_number} unique candidates = {unique_count}")
        print(f"Bandcamp: {mode_label} page {page_number} kept_after_filters = {new_added}")
        if max_items and len(collected) >= max_items:
            break
        if new_added == 0:
            empty_streak += 1
            if empty_streak >= empty_streak_limit:
                break
        else:
            empty_streak = 0
    print(f"Bandcamp: {mode_label} collected total = {len(collected)}")
    return collected

def scrape_bandcamp_discover(driver, discover_url: str, max_pages: int = 1, max_items: int | None = None) -> list:
    return _bandcamp_collect_discover_dom(driver, discover_url, max_pages, max_items=max_items)

def scrape_bandcamp_tag(driver, tag_url: str, max_pages: int = 20, max_items: int | None = None) -> list:
    return _bandcamp_collect_mode_pages(driver, tag_url, "tag", _BANDCAMP_GRID_SELECTORS, max_pages, max_items=max_items)

def scrape_bandcamp_search(driver, search_url: str, max_pages: int = 20, max_items: int | None = None, search_domain: str = "artists") -> list:
    # search_domain is accepted for future branching (tracks vs artists), but current DOM collection is shared.
    results = _bandcamp_collect_mode_pages(driver, search_url, "search", _BANDCAMP_GRID_SELECTORS, max_pages, max_items=max_items)
    if results:
        return results
    # Fallback: try pagination/scroll if the first pass yielded nothing (Bandcamp sometimes lazy-loads results).
    try:
        fallback = _bandcamp_collect_with_pagination(driver, search_url, _BANDCAMP_GRID_SELECTORS, max_items=max_items)
        if fallback:
            return fallback
    except Exception:
        pass
    # Last resort: fetch HTML directly without Selenium.
    try:
        session = build_hardened_session()
        resp = session.get(search_url, timeout=(6, 15))
        if resp.ok:
            parsed = _bandcamp_candidates_from_html(resp.text, search_url)
            return parsed
    except Exception:
        pass
    return results


def _bandcamp_collect_city_tag_candidates(driver, city_label: str, pages: int) -> list:
    if not city_label:
        return []
    return _bandcamp_collect_tag_pages(driver, city_label, pages, search_query=city_label)


def _bandcamp_collect_from_tag_page(driver, tag_url, max_items: int | None = None) -> list:
    paginated_candidates = _bandcamp_collect_with_pagination(driver, tag_url, _BANDCAMP_GRID_SELECTORS, max_items=max_items)
    if paginated_candidates:
        return paginated_candidates
    candidates = []
    counts = {}
    try:
        try:
            html_text = driver.page_source or ""
        except Exception:
            html_text = ""
        soup = BeautifulSoup(html_text, 'html.parser')
        base_url = "https://bandcamp.com"
        selector_sets = [
            ("li.searchresult",),
            (".result-items li",),
            ("li.results-grid-item",),
            (".music-grid .item",),
            (".discover-results .item",),
            ("ul.results-grid li",),
        ]
        seen = set()
        for sels in selector_sets:
            total_here = 0
            for sel in sels:
                for card in soup.select(sel):
                    href = None
                    for anchor in card.select("a[href]"):
                        raw = (anchor.get("href") or "").strip()
                        if not raw:
                            continue
                        if raw.startswith("//"):
                            cand = f"https:{raw}"
                        elif raw.startswith("http"):
                            cand = raw
                        elif raw.startswith("/"):
                            cand = urljoin(base_url, raw)
                        elif "bandcamp.com" in raw:
                            cand = f"https://{raw.lstrip('/')}"
                        else:
                            continue
                        lowered = cand.lower()
                        if any(token in lowered for token in ["/album", "/track", ".bandcamp.com"]):
                            href = cand
                            break
                    if not href or href in seen:
                        continue
                    seen.add(href)
                    candidates.append({
                        "url": href,
                        "primary_genre": bandcamp_extract_primary_genre_from_card(card)
                    })
                    total_here += 1
            counts[",".join(s for s in sels)] = total_here
        if not candidates:
            excluded_hosts = {
                "bandcamp.com",
                "store.bandcamp.com",
                "daily.bandcamp.com",
                "blog.bandcamp.com",
                "community.bandcamp.com",
                "supporters.bandcamp.com"
            }
            generic = 0
            for anchor in soup.find_all('a', href=True):
                absolute = urljoin(tag_url, anchor['href'])
                parsed = urlparse(absolute)
                host = (parsed.netloc or "").lower()
                path = (parsed.path or "").lower()
                if not host.endswith("bandcamp.com") or host in excluded_hosts:
                    continue
                if (path in ("", "/") or
                        path.startswith("/album") or
                        path.startswith("/track") or
                        path.startswith("/music")):
                    normalized = f"{parsed.scheme or 'https'}://{host}{parsed.path}"
                    if normalized not in seen:
                        seen.add(normalized)
                        candidates.append({"url": normalized, "primary_genre": ""})
                        generic += 1
            counts["generic_anchor_sweep"] = generic
            if not candidates:
                blob_candidates = _bandcamp_collect_from_tag_blob(html_text)
                if blob_candidates:
                    print(f"Bandcamp: tag blob fallback yielded {len(blob_candidates)} items")
                    candidates.extend(blob_candidates)
        try:
            total = sum(counts.values()) if counts else 0
            print(f"Bandcamp: tag selectors counts → {counts} (total {total})")
        except Exception:
            pass
    except Exception as exc:
        print(f"Bandcamp: failed to collect links from {tag_url}: {exc}")
    return candidates


def _bandcamp_collect_from_search(driver, query, page=1) -> list:
    """
    Fallback: search Bands for the provided query.
    """
    q = quote((query or "").strip())
    url = f"https://bandcamp.com/search?item_type=b&q={q}&page={int(page)}"
    try:
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    except Exception as exc:
        print(f"Bandcamp: error loading {url}: {exc}")
        return []
    soup = BeautifulSoup(driver.page_source or "", "html.parser")
    out = []
    seen = set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://bandcamp.com", href)
        lowered = href.lower()
        if ".bandcamp.com" in lowered and "community.bandcamp.com" not in lowered:
            if href not in seen:
                seen.add(href)
                out.append({"url": href, "primary_genre": ""})
        elif "/album/" in lowered or "/track/" in lowered:
            if href not in seen:
                seen.add(href)
                out.append({"url": href, "primary_genre": ""})
    print(f"Bandcamp: search fallback found {len(out)} candidates on page {page} for '{query}'")
    return out


def _bandcamp_collect_from_tag_blob(html_text: str) -> list:
    candidates = []
    if not html_text:
        return candidates
    soup = BeautifulSoup(html_text, "html.parser")
    blob_attr = None
    blob_holder = soup.select_one("[data-blob]")
    if blob_holder and blob_holder.has_attr("data-blob"):
        blob_attr = blob_holder["data-blob"]
    if not blob_attr:
        match = re.search(r'data-blob="([^"]+)"', html_text)
        if match:
            blob_attr = match.group(1)
    if not blob_attr:
        return candidates
    try:
        blob = json.loads(html.unescape(blob_attr))
    except Exception as exc:
        print(f"Bandcamp: tag blob decode failed: {exc}")
        return candidates
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            url = node.get("item_url") or node.get("tralbum_url") or node.get("url")
            if isinstance(url, str) and url:
                normalized = url.strip()
                if normalized.startswith("//"):
                    normalized = f"https:{normalized}"
                elif normalized.startswith("/"):
                    normalized = f"https://bandcamp.com{normalized}"
                elif not normalized.startswith(("http://", "https://")):
                    normalized = f"https://{normalized.lstrip('/')}"
                lowered = normalized.lower()
                if ("bandcamp.com" in lowered and
                        any(token in lowered for token in ["/album", "/track", ".bandcamp.com"])):
                    if normalized not in seen:
                        seen.add(normalized)
                        candidates.append({
                            "url": normalized,
                            "primary_genre": node.get("genre_text") or node.get("genre") or ""
                        })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blob)
    return candidates




def _bandcamp_collect_tag_via_api(slug: str, page_index: int, base_params: dict | None = None) -> list:
    api_url = "https://bandcamp.com/api/discover/3/get_web"
    session = build_hardened_session()
    params = dict(base_params or {})
    if slug and not params.get("t"):
        params["t"] = slug
    per_page = int(params.get("limit") or 40)
    params["limit"] = per_page
    params["p"] = page_index
    params["offset"] = page_index * per_page
    try:
        resp = session.get(api_url, params=params, timeout=(6, 15), headers=_rand_headers())
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        print(f"Bandcamp: tag API failed (slug={slug}, page={page_index}): {exc}")
        return []
    items = payload.get("items") or []
    candidates = []
    seen = set()
    for item in items:
        hints = item.get("url_hints") or {}
        subdomain = (hints.get("subdomain") or "").strip()
        custom_domain = (hints.get("custom_domain") or "").strip()
        profile_url = ""
        if custom_domain:
            profile_url = custom_domain if custom_domain.startswith(("http://", "https://")) else f"https://{custom_domain.lstrip('/')}"
        elif subdomain:
            profile_url = f"https://{subdomain}.bandcamp.com/"
        if not profile_url:
            continue
        key = profile_url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "url": profile_url,
            "primary_genre": item.get("genre_text") or "",
            "location": item.get("location_text") or "",
            "tile_artist": item.get("secondary_text") or "",
            "tile_title": item.get("primary_text") or "",
        })
    return candidates


def _bandcamp_collect_discover_dom(driver, discover_url: str, max_pages: int = 1, max_items: int | None = None) -> list:
    max_pages = max(1, int(max_pages or 1))
    selectors = [
        ".discover-item",
        ".results-grid-item",
        ".result",
        ".item",
    ]
    candidates = []
    seen = set()
    base_url = discover_url
    for page_index in range(max_pages):
        page_url = _bandcamp_replace_query_param(base_url, "p", page_index)
        page_label = page_index + 1
        attempts = 0
        page_candidates = []
        while attempts < 3:
            try:
                driver.get(page_url)
            except Exception as exc:
                print(f"Bandcamp: discover load error page {page_label} attempt {attempts+1}: {exc}")
            tile_count = _bandcamp_wait_for_discover_tiles(driver, selectors, timeout=15)
            print(f"Bandcamp: discover page {page_label} raw tiles = {tile_count}")
            if tile_count == 0:
                attempts += 1
                if attempts < 3:
                    print(f"Bandcamp: discover page {page_label} reload attempt {attempts+1}")
                    continue
            try:
                page_candidates = _bandcamp_candidates_from_html(driver.page_source or "", page_url)
            except Exception:
                page_candidates = []
            break
        raw_count = len(page_candidates)
        # Deduplicate across pages and checkpoint; track kept_count for stop logic.
        new_added = 0
        for cand in page_candidates:
            key = (cand.get("url") or "").rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(cand)
            new_added += 1
            if max_items and len(candidates) >= max_items:
                break
        print(f"Bandcamp: discover page {page_label} unique candidates = {raw_count}")
        print(f"Bandcamp: discover page {page_label} kept_after_filters = {new_added}")
        print(f"Bandcamp: discover page {page_label} -> {new_added} items")
        if max_items and len(candidates) >= max_items:
            break
    print(f"Bandcamp: discover collected total = {len(candidates)}")
    return candidates


def _bandcamp_collect_tag_pages(driver, tag_label: str, pages: int, search_query: str | None = None, api_params: dict | None = None, max_items: int | None = None) -> list:
    slug = _bc_normalize_tag_slug(tag_label)
    if not slug:
        return []
    total_candidates = []
    page_count = max(1, pages)
    base_params = dict(api_params or {})
    base_params.pop("p", None)
    base_params.pop("limit", None)
    base_params.pop("offset", None)
    if "t" not in base_params and slug:
        base_params["t"] = slug
    for page_index in range(page_count):
        api_candidates = _bandcamp_collect_tag_via_api(slug, page_index, base_params)
        page_candidates = api_candidates
        per_page = int(base_params.get("limit") or 40)
        tag_url = _bc_make_tag_url(slug, page_index + 1)
        need_dom_fallback = False
        print(f"Bandcamp: tag '{slug}' page {page_index+1}/{page_count} via API -> {len(api_candidates)} candidates")
        if not page_candidates:
            need_dom_fallback = True
        elif len(page_candidates) < per_page:
            need_dom_fallback = True
        if max_items and len(total_candidates) >= max_items:
            break
        page_limit = None
        if max_items:
            remaining = max_items - len(total_candidates)
            if remaining <= 0:
                break
            page_limit = remaining
        if need_dom_fallback and tag_url:
            print(f"Bandcamp: tag '{slug}' page {page_index+1} DOM fallback (page_limit={page_limit}) url={tag_url}")
            dom_candidates = _bandcamp_collect_from_tag_page(driver, tag_url, max_items=page_limit)
            if dom_candidates:
                if not page_candidates:
                    page_candidates = dom_candidates
                else:
                    seen_urls = {c.get("url", "").rstrip("/").lower() for c in page_candidates}
                    for cand in dom_candidates:
                        key = (cand.get("url") or "").rstrip("/").lower()
                        if key and key not in seen_urls:
                            page_candidates.append(cand)
                            seen_urls.add(key)
            time.sleep(random.uniform(1.0, 2.0))
        if page_candidates:
            total_candidates.extend(page_candidates)
        print(f"Bandcamp: tag '{slug}' accumulated {len(total_candidates)} candidates so far")
        if BANDCAMP_MAX_CANDIDATES and len(total_candidates) >= BANDCAMP_MAX_CANDIDATES:
            break
        if max_items and len(total_candidates) >= max_items:
            break
    if not total_candidates:
        query = search_query or tag_label or slug
        for page in range(1, page_count + 1):
            search_candidates = _bandcamp_collect_from_search(driver, query, page=page)
            if search_candidates:
                total_candidates.extend(search_candidates)
                break
            if max_items and len(total_candidates) >= max_items:
                break
    return total_candidates


def _bandcamp_collect_from_discover_html_page(session, base_url: str, page_index: int) -> list:
    page_url = _bandcamp_replace_query_param(base_url, "p", page_index)
    try:
        resp = session.get(page_url, timeout=(6, 15), headers=_rand_headers())
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        print(f"Bandcamp: discover HTML fallback failed for page {page_index}: {exc}")
        return []
    return _bandcamp_candidates_from_html(html, page_url)

def _bandcamp_resolve_artist_profile_url(candidate_url: str) -> str:
    """Normalize candidate links and resolve to canonical artist profile (https://artistname.bandcamp.com/)."""
    if not candidate_url:
        return ""
    url = candidate_url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.startswith("http"):
        url = f"https://{url.lstrip('/')}"
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host.endswith("bandcamp.com"):
        return ""
    excluded_hosts = {
        "bandcamp.com",
        "store.bandcamp.com",
        "daily.bandcamp.com",
        "blog.bandcamp.com",
        "community.bandcamp.com",
        "supporters.bandcamp.com"
    }
    if host in excluded_hosts:
        return ""
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}/"
def _bc_extract_artist_name_from_profile_soup(soup: BeautifulSoup) -> str:
    meta_site = soup.find('meta', attrs={'property': 'og:site_name'})
    if meta_site and meta_site.get('content'):
        name = meta_site['content'].strip()
        if name:
            return name
    meta_title = soup.find('meta', attrs={'property': 'og:title'})
    if meta_title and meta_title.get('content'):
        raw = meta_title['content'].strip()
        left = raw.split('·')[0].strip()
        if left:
            return left
    for sel in ['.band-name', 'h1.band-name', 'h1.title', '#name-section h1', 'header h1', 'h2.band-name']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt:
                return txt
    header_link = soup.select_one('header a[href]')
    if header_link:
        txt = header_link.get_text(" ", strip=True)
        if txt:
            return txt
    return ""

def _bandcamp_parse_html(profile_url: str, html: str, seed_primary_genre: str = "") -> dict:
    artist = {
        "artist_name": "",
        "profile_url": profile_url,
        "location": "",
        "website": "",
        "email": "",
        "emails": [],
        "socials": {
            "instagram": "",
            "twitter": "",
            "facebook": "",
            "youtube": "",
            "linktree": "",
            "spotify": "",
            "bandsintown": "",
            "songkick": ""
        },
        "genres": [],
        "latest_release_title": "",
        "latest_release_date": "",
        "latest_release_precision": "",
        "sounds_like": "",
        "primary_genre": "",
        "source_tag": ""
    }
    page_source = ""
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    artist["artist_name"] = _bc_extract_artist_name_from_profile_soup(soup)
    artist["genres"] = bandcamp_extract_genres(soup)
    primary_genre = (seed_primary_genre or (artist["genres"][0] if artist["genres"] else "")).strip()
    artist["primary_genre"] = primary_genre
    if not artist["genres"] and primary_genre:
        artist["genres"] = [primary_genre]
    artist["sounds_like"] = bandcamp_extract_sounds_like(soup)
    release_info = bandcamp_extract_release_date(html)
    if release_info.get("date_iso"):
        artist["latest_release_date"] = release_info.get("date_iso", "")
        artist["latest_release_precision"] = release_info.get("precision", "") or ""
    location_el = soup.find(class_=re.compile('location', re.I))
    if location_el:
        artist["location"] = location_el.get_text(" ", strip=True)
    if not artist["location"]:
        bio_el = soup.find('div', class_=re.compile('location', re.I))
        if bio_el:
            artist["location"] = bio_el.get_text(" ", strip=True)
    artist["location"] = _canon_location(artist.get("location", ""))
    collected_links = []
    seen_links = set()

    def _record_link(url: str):
        if not url:
            return
        if url not in seen_links:
            seen_links.add(url)
            collected_links.append(url)

    def _record_email(value: str):
        if not value:
            return
        cleaned = value.strip()
        if not cleaned:
            return
        if cleaned not in artist["emails"]:
            artist["emails"].append(cleaned)
        if not artist["email"]:
            artist["email"] = cleaned
    def _consume_external_candidate(candidate: str):
        if not candidate:
            return
        candidate = candidate.strip()
        if not candidate:
            return
        candidate = candidate.strip("()[]{}<>.,; ")
        if not candidate:
            return
        if candidate.lower().startswith("mailto:"):
            email_value = candidate.split("mailto:")[-1].split("?")[0]
            if email_value:
                _record_email(email_value)
            return
        normalized = candidate.split("#")[0]
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"
        elif normalized.startswith("/"):
            normalized = urljoin(profile_url, normalized)
        elif normalized.startswith("www."):
            normalized = f"https://{normalized}"
        else:
            try:
                parsed = urlparse(normalized)
                if not parsed.scheme:
                    normalized = f"https://{normalized}"
            except Exception:
                normalized = f"https://{normalized}"
        normalized = normalize_external_url(normalized)
        try:
            parsed = urlparse(normalized)
        except Exception:
            return
        scheme = (parsed.scheme or "").lower()
        if not scheme.startswith("http"):
            return
        netloc = (parsed.netloc or "").lower()
        if not netloc or netloc.endswith("bandcamp.com"):
            return
        _record_link(normalized)
        if "instagram.com" in netloc:
            artist["socials"]["instagram"] = normalized
        elif "facebook.com" in netloc or "fb.me" in netloc:
            artist["socials"]["facebook"] = normalized
        elif "twitter.com" in netloc or "x.com" in netloc:
            artist["socials"]["twitter"] = normalized
        elif "youtube.com" in netloc or "youtu.be" in netloc:
            artist["socials"]["youtube"] = normalized
        elif any(domain in netloc for domain in ["linktr.ee", "linktree", "withkoji.com", "beacons.ai"]):
            artist["socials"]["linktree"] = normalized
        elif "spotify.com" in netloc:
            artist["socials"]["spotify"] = normalized
        elif "bandsintown.com" in netloc:
            artist["socials"]["bandsintown"] = normalized
        elif "songkick.com" in netloc:
            artist["socials"]["songkick"] = normalized
        else:
            if not artist["website"]:
                artist["website"] = normalized

    for anchor in soup.find_all('a', href=True):
        _consume_external_candidate(anchor['href'])
    contact_texts = []
    contact_selectors = [
        "#bio-container",
        "#bio-text",
        ".bio-container",
        ".bio-text",
        ".bio",
        ".band-bio",
        ".profile-bio",
        "#rightColumn",
        "#right-column",
        ".rightColumn",
        ".tralbum-about",
        ".tralbumData",
    ]
    for selector in contact_selectors:
        for node in soup.select(selector):
            text = node.get_text(" ", strip=True)
            if text:
                contact_texts.append(text)
    combined_contact_text = " ".join(contact_texts)
    for block in contact_texts or [combined_contact_text]:
        if not block:
            continue
        for match in _BC_EMAIL_RE.findall(block):
            _record_email(match)
        for candidate in _SOCIAL_TEXT_RE.findall(block):
            _consume_external_candidate(candidate)
        for pattern, template in _BANDCAMP_HANDLE_HINTS:
            for handle in pattern.findall(block):
                handle_clean = handle.strip().lstrip("@").strip(".,/ ")
                if not handle_clean:
                    continue
                _consume_external_candidate(template.format(handle=handle_clean))
    artist["all_social_links"] = collected_links
    release_container = soup.find('li', class_=re.compile('music-grid-item', re.I))
    release_page_html = ""
    if release_container:
        title_el = release_container.find(class_=re.compile('title', re.I))
        if title_el:
            artist["latest_release_title"] = title_el.get_text(strip=True)
        date_el = release_container.find(class_=re.compile('release', re.I))
        if date_el and not artist["latest_release_date"]:
            raw_text = date_el.get_text(strip=True)
            date_iso, prec = _parse_any_date_to_iso(raw_text)
            if date_iso:
                artist["latest_release_date"] = date_iso
                artist["latest_release_precision"] = prec or artist["latest_release_precision"]
            else:
                artist["latest_release_date"] = raw_text
        if not release_page_html:
            release_anchor = release_container.find("a", href=True)
            if release_anchor:
                rel = release_anchor.get("href", "").strip()
                release_url = ""
                if rel.startswith("//"):
                    release_url = f"https:{rel}"
                elif rel.startswith("http"):
                    release_url = rel
                elif rel.startswith("/"):
                    release_url = urljoin(profile_url, rel)
                elif rel:
                    release_url = urljoin(profile_url, f"/{rel.lstrip('/')}")
                if release_url and release_url != profile_url:
                    release_page_html = _bandcamp_fetch_profile_html(release_url)
                    if release_page_html:
                        release_info = bandcamp_extract_release_date(release_page_html)
                        if release_info.get("date_iso"):
                            artist["latest_release_date"] = release_info["date_iso"]
                            artist["latest_release_precision"] = release_info.get("precision") or artist["latest_release_precision"]
                        if not artist["latest_release_title"]:
                            release_soup = BeautifulSoup(release_page_html, "html.parser")
                            title_candidate = (
                                release_soup.select_one("h2.trackTitle")
                                or release_soup.select_one(".trackTitle")
                                or release_soup.select_one("h1")
                            )
                            if title_candidate:
                                artist["latest_release_title"] = title_candidate.get_text(" ", strip=True)
    if not artist["latest_release_title"]:
        track_title = soup.find(class_=re.compile('trackTitle', re.I))
        if track_title:
            artist["latest_release_title"] = track_title.get_text(strip=True)
    if not artist["latest_release_date"]:
        release_text = soup.find(class_=re.compile('release-date', re.I))
        if release_text:
            raw_text = release_text.get_text(strip=True)
            date_iso, prec = _parse_any_date_to_iso(raw_text)
            if date_iso:
                artist["latest_release_date"] = date_iso
                artist["latest_release_precision"] = prec or artist["latest_release_precision"]
            else:
                artist["latest_release_date"] = raw_text
    if not artist["latest_release_date"]:
        credits = soup.find('div', class_=re.compile(r'tralbum-credits', re.I))
        if credits:
            credits_text = credits.get_text(" ", strip=True)
            match = re.search(r"released\s+(.+)", credits_text, re.I)
            if match:
                raw_text = match.group(1).strip()
                date_iso, prec = _parse_any_date_to_iso(raw_text)
                if date_iso:
                    artist["latest_release_date"] = date_iso
                    artist["latest_release_precision"] = prec or artist["latest_release_precision"]
                else:
                    artist["latest_release_date"] = raw_text
            else:
                artist["latest_release_date"] = credits_text.strip()
    if not artist["latest_release_date"]:
        artist["latest_release_date"] = "not present"
    if not artist["latest_release_title"]:
        artist["latest_release_title"] = ""
    return artist

def _bandcamp_parse_artist_profile(driver, profile_url, seed_primary_genre="") -> dict:
    """Visit artist profile via Selenium fallback."""
    page_source = _bandcamp_quick_visit(driver, profile_url)
    if not page_source:
        page_source = _bandcamp_fetch_profile_html(profile_url)
    return _bandcamp_parse_html(profile_url, page_source, seed_primary_genre)

def _bandcamp_is_actionable(artist_dict: dict) -> bool:
    """Return True if website or email or at least one social exists."""
    if not artist_dict:
        return False
    socials = artist_dict.get("socials", {})
    has_social = any(value for value in socials.values())
    return bool(artist_dict.get("website") or artist_dict.get("email") or has_social)

def _bandcamp_write_enriched_csv(rows, existing_csv):
    columns = [
        "Artist Name",
        "Profile URL",
        "Website",
        "Email",
        "Instagram",
        "Twitter",
        "Facebook",
        "Linktree",
        "YouTube",
        "Location",
        "Genres",
        "Latest Release",
        "Latest Release Date",
        "Latest Release Precision",
        "Sounds Like",
        "Source Tag",
        "Bandcamp_Source_Mode",
        "Bandcamp_Search_Domain",
    ]
    base_dir = os.path.dirname(os.path.abspath(existing_csv))
    enriched_path = os.path.join(base_dir, "bandcamp_enriched.csv")
    existing_df = pd.DataFrame(columns=columns)
    if os.path.exists(enriched_path):
        try:
            existing_df = pd.read_csv(enriched_path)
        except Exception:
            existing_df = pd.DataFrame(columns=columns)
    for col in columns:
        if col not in existing_df.columns:
            existing_df[col] = ""
    if not existing_df.empty:
        existing_df = existing_df[columns]
    new_df = pd.DataFrame(rows)
    for col in columns:
        if col not in new_df.columns:
            new_df[col] = ""
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined["__dedupe_key"] = (
        combined["Artist Name"].fillna("").str.strip().str.lower()
        + "||" +
        combined["Profile URL"].fillna("").str.rstrip("/").str.lower()
    )
    combined = combined.drop_duplicates(subset="__dedupe_key")
    combined = combined.drop(columns="__dedupe_key")
    combined = combined[columns]
    _atomic_write_dataframe(combined, enriched_path)

# ---------------------------
# SoundCloud Helpers
# ---------------------------
def clean_display_name(value: str) -> str:
    text = value or ""
    out = []
    for ch in text:
        if unicodedata.category(ch) in SYMBOL_CAT:
            continue
        out.append(ch)
    cleaned = "".join(out)
    cleaned = re.sub(r"[^\w\s\.\&\-\’\'/|]", "", cleaned, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def export_soundcloud_row(data: dict) -> dict:
    fields = [
        "Artist Name", "Location", "Song Title", "Sounds Like", "Social Link",
        "SoundCloud Link", "Played on triple J", "Played on Unearthed",
        "Release Date", "Primary Genre", "Date Added", "Email"
    ]
    row = {field: "" for field in fields}
    handle = data.get("handle", "")
    display = data.get("display_name") or handle
    row["Artist Name"] = clean_display_name(display) if display else ""
    row["SoundCloud Link"] = data.get("soundcloud_link") or (f"https://soundcloud.com/{handle}" if handle else "")
    exts = data.get("external_urls") or []
    emails = data.get("emails") or []
    row["Social Link"] = "; ".join(exts[:5])
    row["Email"] = ", ".join(emails) if emails else ""
    row["Song Title"] = ""
    row["Release Date"] = ""
    row["Played on triple J"] = ""
    row["Played on Unearthed"] = ""
    if row["Social Link"] == "http://firefox.com":
        row["Social Link"] = ""
    return row


def _resolve_country_name(value: str) -> str:
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower in COUNTRY_CODE_OVERRIDES:
        return COUNTRY_CODE_OVERRIDES[lower]
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return raw


def normalize_location(city: str, country: str) -> str:
    city_clean = (city or "").strip()
    country_clean = _resolve_country_name(country)
    if "naarm" in city_clean.lower():
        city_clean = "Melbourne"
        if not country_clean:
            country_clean = "Australia"
    if city_clean and country_clean:
        return f"{city_clean}, {country_clean}"
    return city_clean or country_clean or ""


def choose_primary_genre(user_genre: str, fallback_tags=None) -> str:
    fallback_tags = fallback_tags or []
    primary = (user_genre or "").strip()
    if primary and primary.lower() not in _SC_GENRE_DENY:
        return primary
    for tag in fallback_tags:
        candidate = (tag or "").strip()
        if not candidate:
            continue
        if candidate.lower() in _SC_GENRE_DENY:
            continue
        return candidate
    return ""


class SoundCloudAboutCache:
    def __init__(self, path: str = SC_CACHE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = None

    def _ensure_loaded(self):
        if self._data is not None:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except Exception:
            self._data = {}

    def get(self, handle: str):
        with self._lock:
            self._ensure_loaded()
            entry = self._data.get(handle)
            if not entry:
                return None
            ts = entry.get("ts") or 0
            age_days = (time.time() - ts) / 86400.0
            if age_days > SC_CACHE_MAX_AGE_DAYS:
                return None
            return entry

    def set(self, handle: str, payload: dict, etag: str = "", last_modified: str = ""):
        with self._lock:
            self._ensure_loaded()
            self._data[handle] = {
                "ts": time.time(),
                "etag": etag or "",
                "last_modified": last_modified or "",
                "data": payload,
            }
            self._persist()

    def _persist(self):
        tmp_path = self.path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp_path, self.path)
        except Exception:
            pass


SC_ABOUT_CACHE = SoundCloudAboutCache()
SC_ABOUT_CACHE_REQUIRED_KEYS = (
    "latest_track_title",
    "latest_track_release_date",
    "latest_track_tags",
    "sounds_like",
    "bio_text",
)

SC_CONTACT_TEXT_SELECTORS = (
    ".profileLinks__contactText",
    ".profileLinks__body",
    ".profileLinks__content",
    ".profileLinks__description",
    ".profileLinks__text",
    ".profileLinks__cta",
    "[data-testid='profileLinksContactText']",
    "[data-testid='profile-links-contact-text']",
)

SC_CONTACT_FALLBACK_SELECTORS = (
    ".profileSidebar",
    "[class*='profileLinks']",
)

_SC_THREAD_LOCAL = threading.local()

_BANDCAMP_THREAD_LOCAL = threading.local()

def _bandcamp_thread_session():
    session = getattr(_BANDCAMP_THREAD_LOCAL, "session", None)
    if session is None:
        session = build_hardened_session()
        _BANDCAMP_THREAD_LOCAL.session = session
    return session


def _build_sc_session() -> requests.Session:
    return build_hardened_session()


def is_valid_sc_url(url: str):
    if not url:
        return False, None
    match = SC_HANDLE_RE.match(url.strip())
    if not match:
        return False, None
    slug = match.group(1).lower()
    if slug in SC_HANDLE_BAN:
        return False, slug
    return True, slug


AGG_PREF = SC_AGGREGATOR_PREFERENCE

def _sc_is_people_search_url(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    return parsed.netloc.lower().endswith("soundcloud.com") and parsed.path.strip("/").lower() == "search/people"


def _sc_parse_people_search(value: str) -> dict:
    result = {"q": "", "place": ""}
    if not value:
        return result
    try:
        parsed = urlparse(value.strip())
        query = parse_qs(parsed.query or "")
    except Exception:
        return result
    result["q"] = (query.get("q", [""])[0] or "")
    result["place"] = (query.get("filter.place", [""])[0] or "")
    return result


def sc_parse_people_search_url(source_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a SoundCloud /search/people URL and return (query, place).
    """
    try:
        parsed = urlparse(source_url)
        qs = parse_qs(parsed.query or "")
    except Exception:
        return None, None
    q_vals = qs.get("q") or qs.get("query") or []
    place_vals = qs.get("filter.place") or []
    query = (q_vals[0] if q_vals else None) or None
    place = (place_vals[0] if place_vals else None) or None
    return query, place


def _sc_location_matches_filter(location_text: str, place_filter: str) -> bool:
    """
    Return True if no place filter is set, or if the provided location contains the filter text.
    If the location is missing, allow it to pass so we don't drop candidates due to parse gaps.
    """
    if not place_filter:
        return True
    if not location_text:
        return True
    return place_filter.lower() in (location_text or "").lower()


def expand_for_email(session, url):
    mails = set()
    if not url:
        return sorted(mails)
    try:
        resp = session.get(url, timeout=(6, 12), headers=_rand_headers())
        if resp.status_code >= 400:
            polite_sleep()
            return sorted(mails)
        doc = get_soup(resp.text)
        for a in doc.select('a[href^="mailto:"]'):
            href = (a.get("href") or "").strip()
            if href.startswith("mailto:"):
                mails.add(href.replace("mailto:", "").split("?", 1)[0])
    except Exception:
        pass
    polite_sleep()
    return sorted(mails)


def _sc_track_release_iso(track: dict) -> tuple:
    """
    Pick the best available date field from a track payload and convert
    it to (iso_date, precision). Falls back to created_at if needed.
    """
    if not isinstance(track, dict):
        return ("", "")
    for field in ("release_date", "display_date", "created_at"):
        raw = (track.get(field) or "").strip()
        if not raw:
            continue
        iso, precision = _parse_any_date_to_iso(raw)
        if iso:
            return (iso, precision or "day")
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return (raw[:10], "day")
    return ("", "")


def _sc_norm_user_id(user_id) -> str:
    if not user_id:
        return ""
    if isinstance(user_id, int):
        return str(user_id)
    s = str(user_id).strip()
    if "soundcloud:users:" in s:
        s = s.split("soundcloud:users:")[-1]
    match = re.search("(\\d+)", s)
    return match.group(1) if match else ""


def _sc_fetch_latest_track_rss(session, user_id, handle: str = "") -> dict:
    uid = _sc_norm_user_id(user_id)
    if not uid:
        if SC_DEBUG_LATEST:
            print(f"[scdbg] rss no id user_id={user_id} handle={handle}")
        return {}
    rss_url = f"https://feeds.soundcloud.com/users/soundcloud:users:{uid}/sounds.rss"
    try:
        resp = session.get(rss_url, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        if SC_DEBUG_LATEST:
            print(f"[scdbg] rss fetch handle={handle} uid={uid} status={resp.status_code} bytes={len(resp.content or b'')} url={rss_url}")
        resp.raise_for_status()
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        root = ET.fromstring(resp.text)
        item_count = len(list(root.findall(".//item")))
        if SC_DEBUG_LATEST:
            print(f"[scdbg] rss items handle={handle} uid={uid} count={item_count}")
        first_item = None
        for item in root.findall(".//item"):
            first_item = item
            break
        if first_item is None:
            if SC_DEBUG_LATEST:
                print(f"[scdbg] rss empty uid={uid} handle={handle} items={item_count}")
            return {}
        title = (first_item.findtext("title") or "").strip()
        pub_date_raw = (first_item.findtext("pubDate") or "").strip()
        iso_date = ""
        precision = ""
        if pub_date_raw:
            try:
                dt = parsedate_to_datetime(pub_date_raw)
                if dt:
                    iso_date = dt.date().isoformat()
                    precision = "day"
            except Exception:
                iso_date = ""
                precision = ""
        if SC_DEBUG_LATEST:
            print(f"[scdbg] rss item title=\"{title[:60]}\" pubDate=\"{pub_date_raw}\" items={item_count}")
        track_url = (first_item.findtext("link") or "").strip()
        return {
            "title": title,
            "release_date": iso_date,
            "precision": precision,
            "genre": "",
            "tags": [],
            "permalink_url": track_url,
        }
    except Exception as exc:
        if SC_DEBUG_LATEST:
            print(f"[scdbg] rss parse fail uid={uid} handle={handle} err={exc}")
        print(f"[warn] SoundCloud RSS fallback failed for user_id={user_id}: {exc}")
        return {}


def _sc_fetch_latest_track_metadata(session, client_id: str, user_id, handle: str = "") -> dict:
    """
    Fetch the user's most recent track via the public API so we can
    capture title, release date, and tags without relying on Selenium.
    """
    uid = _sc_norm_user_id(user_id)
    if SC_DEBUG_LATEST:
        print(f"[scdbg] latest-track input handle={handle} user_id={user_id!r} norm_uid={uid!r}")
    if not uid:
        if SC_DEBUG_LATEST:
            print(f"[scdbg] latest-track missing uid user_id={user_id} handle={handle}")
        return {}

    rss_track = _sc_fetch_latest_track_rss(session, uid, handle)
    if rss_track and (rss_track.get("title") or rss_track.get("release_date") or rss_track.get("genre") or rss_track.get("tags")):
        rss_track["source"] = "rss"
        rss_track["permalink_url"] = rss_track.get("permalink_url") or ""
        return rss_track
    else:
        if SC_DEBUG_LATEST:
            print(f"[scdbg] rss empty uid={uid} handle={handle}; attempting tracks API")

    if not client_id:
        return rss_track or {}

    api_url = f"https://api-v2.soundcloud.com/users/{uid}/tracks"
    params = {
        "client_id": client_id,
        "limit": 1,
        "linked_partitioning": 1,
        "order": "published_at",
    }
    try:
        resp = session.get(api_url, params=params, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        if resp.status_code == 403:
            print(f"[warn] SoundCloud tracks API 403 handle={handle or uid}; continuing without API tracks")
            _sc_stat_inc("tracks_api_403")
            return rss_track or {}
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        if "403" in str(exc):
            print(f"[warn] SoundCloud tracks API 403 handle={handle or uid}; continuing without API tracks")
            _sc_stat_inc("tracks_api_403")
            return rss_track or {}
        print(f"[warn] SoundCloud latest-track API failed for user_id={user_id}: {exc}")
        return rss_track or {}
    collection = []
    if isinstance(payload, dict):
        collection = payload.get("collection") or []
    elif isinstance(payload, list):
        collection = payload
    for track in collection:
        if not isinstance(track, dict):
            continue
        iso_date, precision = _sc_track_release_iso(track)
        tag_tokens = _norm_tokens(track.get("tag_list") or "")
        return {
            "title": track.get("title") or "",
            "release_date": iso_date,
            "precision": precision or "day" if iso_date else "",
            "genre": track.get("genre") or "",
            "tags": tag_tokens[:8],
            "source": "api",
            "permalink_url": track.get("permalink_url") or "",
        }
    return rss_track or {}


def _sc_resolve_track_from_url(session, track_url: str, handle: str = "") -> dict:
    client_id = _sc_get_client_id(session)
    if not client_id or not track_url:
        return {}
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": track_url, "client_id": client_id},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("kind") != "track":
        return {}
    _sc_track_release_iso(data)
    tags = _norm_tokens(data.get("tag_list") or data.get("tags") or "")
    return {
        "genre": data.get("genre") or "",
        "tags": tags[:8],
        "source": "resolve",
    }


def _sc_latest_track_from_userobj(session, handle: str) -> dict:
    userobj = _SC_HANDLE_USEROBJ_MAP.get(handle)
    if not isinstance(userobj, dict):
        return {}
    client_id = _sc_get_client_id(session)
    if not client_id:
        return {}
    track_id = None
    track_permalink = ""

    def walk(obj):
        nonlocal track_id, track_permalink
        if track_id and track_permalink:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = (k or "").lower()
                if track_id is None and key in ("track_id", "latest_track_id", "last_track_id", "trackid"):
                    if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
                        track_id = int(v)
                if track_id is None and "track" in key and isinstance(v, dict):
                    vid = v.get("id")
                    if isinstance(vid, int):
                        track_id = vid
                    if not track_permalink:
                        url_candidate = v.get("permalink_url") or v.get("permalink") or v.get("uri") or ""
                        if isinstance(url_candidate, str) and "soundcloud.com/" in url_candidate:
                            if re.search(r"soundcloud\.com/[^/]+/[^/]+", url_candidate):
                                track_permalink = url_candidate
                if not track_permalink and isinstance(v, str) and "soundcloud.com/" in v:
                    if re.search(r"soundcloud\.com/[^/]+/[^/]+", v):
                        track_permalink = v
                if track_id and track_permalink:
                    return
                walk(v)
                if track_id and track_permalink:
                    return
        elif isinstance(obj, list):
            for item in obj:
                if track_id and track_permalink:
                    break
                walk(item)

    walk(userobj)

    if track_id and SC_DEBUG_LATEST:
        print(f"[scdbg] userobj track-id handle={handle} track_id={track_id}")
    if not track_id and track_permalink and SC_DEBUG_LATEST:
        print(f"[scdbg] userobj track-link handle={handle} url={track_permalink}")

    def fetch_track(tid):
        try:
            resp = session.get(
                f"https://api-v2.soundcloud.com/tracks/{tid}",
                params={"client_id": client_id},
                timeout=SC_REQUEST_TIMEOUT,
                headers=_rand_headers(),
            )
            if resp.status_code != 200 and SC_DEBUG_LATEST:
                print(f"[scdbg] track fetch non-200 handle={handle} track_id={tid} status={resp.status_code}")
            resp.raise_for_status()
            track = resp.json() or {}
        except Exception as exc:
            if SC_DEBUG_LATEST:
                print(f"[scdbg] track fetch failed handle={handle} track_id={tid} err={exc}")
            return {}
        iso_date, precision = _sc_track_release_iso(track)
        tags = _norm_tokens(track.get("tag_list") or track.get("tags") or "")
        return {
            "title": track.get("title") or "",
            "release_date": iso_date,
            "precision": precision,
            "genre": track.get("genre") or "",
            "tags": tags[:8],
            "source": "track",
        }

    if track_id:
        return fetch_track(track_id)

    if track_permalink:
        try:
            resp = session.get(
                "https://api-v2.soundcloud.com/resolve",
                params={"url": track_permalink, "client_id": client_id},
                timeout=SC_REQUEST_TIMEOUT,
                headers=_rand_headers(),
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception:
            return {}
        resolved_id = None
        if isinstance(data, dict):
            if data.get("kind") == "track" and data.get("id") is not None:
                resolved_id = data.get("id")
            elif isinstance(data.get("id"), int):
                resolved_id = data.get("id")
        if resolved_id is not None:
            if isinstance(resolved_id, str) and resolved_id.isdigit():
                resolved_id = int(resolved_id)
            if SC_DEBUG_LATEST:
                print(f"[scdbg] userobj resolved track handle={handle} track_id={resolved_id}")
            return fetch_track(resolved_id)
    return {}


def _sc_fetch_api_profile(session, handle: str) -> dict:
    fallback_uid = _SC_HANDLE_UID_MAP.get(handle) or ""
    client_id = _sc_get_client_id(session)
    if not client_id:
        return {}
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={
                "url": f"https://soundcloud.com/{handle}",
                "client_id": client_id,
            },
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        if fallback_uid:
            print(f"[warn] SoundCloud resolve API failed for {handle}; using cached uid fallback ({fallback_uid})")
            data = {"username": handle, "id": fallback_uid}
        else:
            print(f"[warn] SoundCloud resolve API failed for {handle}: {exc}")
            return {}
    profile = {
        "display_name": data.get("full_name") or data.get("username"),
        "city": data.get("city") or "",
        "country": _resolve_country_name(data.get("country_code")),
        "genre": data.get("genre") or data.get("primary_genre") or "",
        "external_urls": [],
        "description": data.get("description") or "",
        "latest_track_title": "",
        "latest_track_release_date": "",
        "latest_track_precision": "",
        "latest_track_genre": "",
        "latest_track_tags": [],
        "latest_track_source": "",
        "latest_track_permalink": "",
        "latest_track_url": "",
        "user_genre": (data.get("genre") or "").strip(),
    }
    user_urn = data.get("urn") or ""
    user_id = data.get("id") or fallback_uid or ""
    user_identifier = user_id or user_urn or fallback_uid
    if SC_DEBUG_LATEST:
        normalized_uid = _sc_norm_user_id(user_identifier)
        print(f"[scdbg] handle={handle} id={user_id} urn={user_urn} uid={normalized_uid}")
    if user_urn:
        try:
            wp_resp = session.get(
                f"https://api-v2.soundcloud.com/users/{user_urn}/web-profiles",
                params={"client_id": client_id},
                timeout=SC_REQUEST_TIMEOUT,
                headers=_rand_headers(),
            )
            if wp_resp.status_code == 200:
                for item in wp_resp.json() or []:
                    url = item.get("url")
                    if url:
                        profile["external_urls"].append(url)
        except Exception as exc:
            print(f"[warn] SoundCloud web profiles failed for {handle}: {exc}")
    latest_track = _sc_fetch_latest_track_metadata(session, client_id, user_identifier, handle)
    if latest_track:
        profile["latest_track_title"] = latest_track.get("title") or ""
        profile["latest_track_release_date"] = latest_track.get("release_date") or ""
        profile["latest_track_precision"] = latest_track.get("precision") or ""
        profile["latest_track_genre"] = latest_track.get("genre") or ""
        profile["latest_track_tags"] = latest_track.get("tags") or []
        profile["latest_track_source"] = latest_track.get("source") or ""
        profile["latest_track_permalink"] = latest_track.get("permalink_url") or ""
        profile["latest_track_url"] = latest_track.get("permalink_url") or ""
    return profile


def _sc_fetch_user_fallback_links(session, handle: str):
    urls, emails = set(), set()
    client_id = _sc_get_client_id(session)
    if not client_id or not handle:
        return urls, emails
    try:
        resolve_resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": f"https://soundcloud.com/{handle}", "client_id": client_id},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resolve_resp.raise_for_status()
        data = resolve_resp.json() or {}
        uid = _sc_norm_user_id(data.get("id") or data.get("urn") or "")
    except Exception as exc:
        print(f"[warn] SoundCloud user fallback resolve failed for {handle}: {exc}")
        return urls, emails

    desc = (data.get("description") or "").strip()
    if desc:
        urls.update(URL_RE.findall(desc))
        _sc_collect_emails_from_text(emails, desc)
    website = data.get("website") or ""
    if website:
        urls.add(website)

    if uid:
        try:
            user_resp = session.get(
                f"https://api-v2.soundcloud.com/users/{uid}",
                params={"client_id": client_id},
                timeout=SC_REQUEST_TIMEOUT,
                headers=_rand_headers(),
            )
            user_resp.raise_for_status()
            user_data = user_resp.json() or {}
            web_profiles = user_data.get("web_profiles") or user_data.get("website_profiles") or []
            if isinstance(web_profiles, list):
                for item in web_profiles:
                    if isinstance(item, str):
                        u = item
                        if u.startswith("http"):
                            urls.add(u)
                    elif isinstance(item, dict):
                        u = item.get("url") or ""
                        if u and u.startswith("http"):
                            urls.add(u)
            website = user_data.get("website") or ""
            if website:
                urls.add(website)
            desc2 = user_data.get("description") or ""
            if desc2 and desc2 != desc:
                urls.update(URL_RE.findall(desc2))
                _sc_collect_emails_from_text(emails, desc2)
        except Exception as exc:
            print(f"[warn] SoundCloud user fallback fetch failed for {handle}: {exc}")
    return urls, emails


def extract_sc_links(session: requests.Session, handle: str) -> dict:
    global _SC_ABOUT_DISABLED, _SC_ABOUT_DISABLE_LOGGED
    cached = SC_ABOUT_CACHE.get(handle)
    if cached:
        cached_data = cached.get("data", {}) or {}
        exts = [u for u in (cached_data.get("external_urls") or []) if u and u.lower() != "http://firefox.com"]
        emails = cached_data.get("emails") or []
        has_required = all(key in cached_data for key in SC_ABOUT_CACHE_REQUIRED_KEYS)
        if cached_data and (exts or emails) and has_required:
            cached_data["external_urls"] = exts
            return cached_data

    external_urls, emails = set(), set()
    display_name = handle
    user_city = ""
    user_country = ""
    user_genre = ""
    t0 = time.perf_counter()
    html = ""
    profile_html = ""
    bio_text = ""
    latest_title = ""
    latest_release = ""
    latest_precision = ""
    latest_genre = ""
    latest_tags = []
    latest_source = ""
    latest_track_url = ""
    root_url = f"https://soundcloud.com/{handle}"
    about_url = f"{root_url}/about"
    print(f"[dbg] fetching {about_url}")
    contact_text_seen = set()

    def _record_contact_text(text):
        if not text:
            return
        normalized = re.sub(r"\s+", " ", text)
        normalized = (normalized or "").strip()
        if not normalized or normalized in contact_text_seen:
            return
        contact_text_seen.add(normalized)
        _sc_collect_emails_from_text(emails, normalized)

    if SC_ADAPTIVE_ABOUT_DISABLE and _SC_ABOUT_DISABLED:
        html = ""
    else:
        _sc_stat_inc("about_attempts")
        try:
            resp = session.get(about_url, timeout=(6, 12), headers=_rand_headers())
            print(f"[dbg] fetched {handle} status={resp.status_code} len={len(resp.text)}")
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            print(f"[warn] {handle} about fetch failed: {exc}")
        finally:
            polite_sleep()

    challenge_page = False
    doc = None
    if html:
        lowered = html.lower()
        if any(term in lowered for term in ("captcha", "verify you are human", "enable javascript", "cloudflare", "attention required", "enable cookies")):
            print(f"[warn] sc about challenge page handle={handle}; skipping about parse")
            challenge_page = True
            _sc_stat_inc("about_challenges")
            if SC_ADAPTIVE_ABOUT_DISABLE:
                with _SC_RUN_LOCK:
                    attempts = int((_SC_RUN_STATS or {}).get("about_attempts", 0))
                    challenges = int((_SC_RUN_STATS or {}).get("about_challenges", 0))
                if attempts and attempts >= SC_ABOUT_CHALLENGE_WINDOW:
                    rate = challenges / attempts
                    if rate >= SC_ABOUT_CHALLENGE_THRESHOLD and not _SC_ABOUT_DISABLED:
                        _SC_ABOUT_DISABLED = True
                        _sc_stat_inc("about_disabled", 1)
                        if not _SC_ABOUT_DISABLE_LOGGED:
                            print(f"SoundCloud: about disabled for this run (challenge_rate={rate:.2f} attempts={attempts} challenges={challenges})")
                            _SC_ABOUT_DISABLE_LOGGED = True
        else:
            doc = get_soup(html)

    if doc:
        name_el = doc.select_one("h1, .profileHeaderInfo__userName, .profileHeaderInfo__content")
        if name_el:
            text = name_el.get_text(strip=True)
            if text:
                display_name = text

        for snippet in _sc_extract_contact_text_sections(doc):
            _record_contact_text(snippet)

        for a in doc.select(
            'a[href^="mailto:"], '
            'a[href*="instagram.com"], a[href*="facebook.com"], '
            'a[href*="linktr.ee"], a[href*="bandcamp.com"], '
            'a[href*="youtube.com"], a[href*="tiktok.com"], '
            'a[href*="twitter.com"], a[href*="x.com"], '
            'a[href*="beacons.ai"], a[href*="carrd.co"], '
            'a[href*="flow.page"], a[href*="solo.to"], '
            'a[href*="hypeddit.com"], a[href*="toneden.io"]'
        ):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            if href.startswith("mailto:"):
                emails.add(href.replace("mailto:", "").split("?", 1)[0])
            else:
                external_urls.add(href)

        for script in doc.find_all("script"):
            txt = script.string or ""
            if not txt or ("http" not in txt and "sameAs" not in txt):
                continue
            external_urls.update(URL_RE.findall(txt))
            try:
                data = json.loads(txt)
            except Exception:
                continue
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for key, value in cur.items():
                        if isinstance(value, (list, tuple)) and key and key.lower() in ("sameas", "externalurls", "externallinks", "external_url", "socials"):
                            for item in value:
                                if isinstance(item, str) and item.startswith("http"):
                                    external_urls.add(item)
                        elif isinstance(value, (dict, list)):
                            stack.append(value)
                        elif isinstance(value, str) and value.startswith("http"):
                            external_urls.add(value)
                elif isinstance(cur, list):
                    stack.extend(cur)

        if not external_urls:
            external_urls.update(URL_RE.findall(html))

        bio_el = (
            doc.select_one(".profileHeaderInfo__bio")
            or doc.select_one(".about__description")
            or doc.select_one("[data-testid='profile-bio']")
        )
        if bio_el:
            bio_text = bio_el.get_text(" ", strip=True)
            _record_contact_text(bio_text)

        try:
            text_blob = doc.get_text(" ", strip=True)
        except Exception:
            text_blob = ""
        _record_contact_text(text_blob)

    global _SC_ROOT_FORBIDDEN, _SC_ROOT_FORBIDDEN_LOGGED
    if not _SC_ROOT_FORBIDDEN:
        try:
            root_resp = session.get(root_url, timeout=(6, 12), headers=_rand_headers())
            print(f"[dbg] fetched root {handle} status={root_resp.status_code} len={len(root_resp.text)}")
            if root_resp.status_code == 403:
                _SC_ROOT_FORBIDDEN = True
                if not _SC_ROOT_FORBIDDEN_LOGGED:
                    print("[warn] SoundCloud root fetch 403; disabling root fetches for this run")
                    _SC_ROOT_FORBIDDEN_LOGGED = True
                _sc_stat_inc("root_403")
            else:
                root_resp.raise_for_status()
                profile_html = root_resp.text
        except Exception as exc:
            print(f"[warn] {handle} profile fetch failed: {exc}")
        finally:
            polite_sleep()

    if (challenge_page or _SC_ROOT_FORBIDDEN or not profile_html) and not external_urls:
        if SC_DEBUG_LATEST:
            print(f"[scdbg] fallback trigger handle={handle} challenge={challenge_page} root_forbid={_SC_ROOT_FORBIDDEN} profile_html={bool(profile_html)}")
        fb_urls, fb_emails = _sc_fetch_user_fallback_links(session, handle)
        if fb_urls or fb_emails:
            external_urls.update(fb_urls)
            emails.update(fb_emails)
            print(f"[info] sc api-user fallback handle={handle} urls={len(fb_urls)} emails={len(fb_emails)}")
            _sc_stat_inc("api_user_fallback_used")

    if profile_html:
        profile_doc = get_soup(profile_html)
        for snippet in _sc_extract_contact_text_sections(profile_doc):
            _record_contact_text(snippet)
        for meta in profile_doc.select(
            "meta[property='og:description'], "
            "meta[property='twitter:description'], "
            "meta[name='description'], "
            "meta[name='twitter:description']"
        ):
            _record_contact_text(meta.get("content") or "")
        try:
            text_blob = profile_doc.get_text(" ", strip=True)
        except Exception:
            text_blob = ""
        if text_blob:
            _record_contact_text(text_blob[:4000])

    aggregator_link = None
    for candidate in sorted(external_urls):
        if _is_aggregator_link(candidate):
            aggregator_link = candidate
            break

    aggregator_emails: List[str] = []
    if aggregator_link:
        aggregator_emails = _fetch_aggregator_emails(session, aggregator_link, display_name)
        for mail in aggregator_emails:
            emails.add(mail)

    api_profile = _sc_fetch_api_profile(session, handle)
    if api_profile:
        if api_profile.get("display_name"):
            display_name = api_profile["display_name"]
        user_city = api_profile.get("city") or user_city
        user_country = api_profile.get("country") or user_country
        user_genre = api_profile.get("user_genre") or api_profile.get("genre") or user_genre
        external_urls.update(api_profile.get("external_urls") or [])
        profile_description = api_profile.get("description") or ""
        if profile_description:
            if not bio_text:
                bio_text = profile_description
            _record_contact_text(profile_description)
        latest_title = api_profile.get("latest_track_title") or latest_title
        latest_release = api_profile.get("latest_track_release_date") or latest_release
        latest_precision = api_profile.get("latest_track_precision") or latest_precision
        latest_genre = api_profile.get("latest_track_genre") or latest_genre
        latest_tags = api_profile.get("latest_track_tags") or latest_tags
        latest_source = api_profile.get("latest_track_source") or latest_source
        if not latest_track_url:
            latest_track_url = api_profile.get("latest_track_permalink") or api_profile.get("latest_track_url") or latest_track_url

    uid_hint = _SC_HANDLE_UID_MAP.get(handle)
    if uid_hint and (not latest_title or not latest_release):
        if SC_DEBUG_LATEST:
            print(f"[scdbg] latest-track via uid_map handle={handle} uid_hint={uid_hint}")
        lt = _sc_fetch_latest_track_metadata(session, _sc_get_client_id(session), uid_hint, handle)
        if lt:
            if not latest_title and lt.get("title"):
                latest_title = lt.get("title")
            if not latest_release and lt.get("release_date"):
                latest_release = lt.get("release_date")
                latest_precision = latest_precision or lt.get("precision") or ""
            if not latest_tags and lt.get("tags"):
                latest_tags = lt.get("tags")
            if not latest_source and lt.get("source"):
                latest_source = lt.get("source")
            if not latest_track_url and lt.get("permalink_url"):
                latest_track_url = lt.get("permalink_url")

    if not latest_title or not latest_release:
        lt_obj = _sc_latest_track_from_userobj(session, handle)
        if lt_obj:
            if not latest_title and lt_obj.get("title"):
                latest_title = lt_obj.get("title")
            if not latest_release and lt_obj.get("release_date"):
                latest_release = lt_obj.get("release_date")
                latest_precision = latest_precision or lt_obj.get("precision") or ""
            if not latest_tags and lt_obj.get("tags"):
                latest_tags = lt_obj.get("tags")
            if not latest_source and lt_obj.get("source"):
                latest_source = lt_obj.get("source")

    tracks_source = latest_source or ("rss" if latest_title or latest_release else "")
    if tracks_source == "rss" and (not latest_genre or not latest_tags):
        rss_track_url = latest_track_url or ""
        if rss_track_url:
            resolved = _sc_resolve_track_from_url(session, rss_track_url, handle)
            if resolved:
                if not latest_genre and resolved.get("genre"):
                    latest_genre = resolved.get("genre")
                if not latest_tags and resolved.get("tags"):
                    latest_tags = resolved.get("tags")
                if not latest_source and resolved.get("source"):
                    latest_source = resolved.get("source")

    norm_exts = []
    seen_norm = set()
    for url in external_urls:
        normalized = normalize_external_url(url)
        if not normalized or normalized in seen_norm:
            continue
        seen_norm.add(normalized)
        norm_exts.append(normalized)

    website_url = ""
    for url in norm_exts:
        host = (urlparse(url).hostname or "").lower()
        if host and host not in _SC_SOCIAL_DOMAINS:
            website_url = url
            break

    elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
    payload = {
        "handle": handle,
        "display_name": display_name,
        "external_urls": norm_exts,
        "emails": sorted(emails),
        "website": website_url,
        "city": user_city,
        "country": user_country,
        "genre": user_genre,
        "user_genre": user_genre,
        "elapsed_ms": elapsed_ms,
        "aggregator_expanded": int(bool(aggregator_link)),
        "_aggregator_tried": int(bool(aggregator_link)),
        "bio_text": bio_text,
        "sounds_like": _sc_sounds_like_from_bio(bio_text),
        "latest_track_title": latest_title,
        "latest_track_release_date": latest_release,
        "latest_track_precision": latest_precision,
        "latest_track_genre": latest_genre,
        "latest_track_tags": latest_tags,
        "latest_track_source": latest_source or ("rss" if latest_title or latest_release else ""),
        "latest_track_permalink": latest_track_url,
        "latest_track_url": latest_track_url,
    }
    if payload["external_urls"] or payload["emails"]:
        SC_ABOUT_CACHE.set(handle, payload)
    return payload


def _sc_thread_session() -> requests.Session:
    session = getattr(_SC_THREAD_LOCAL, "session", None)
    if session is None:
        session = _build_sc_session()
        _SC_THREAD_LOCAL.session = session
    return session


def _sc_fetch_contact_payload(handle: str) -> dict:
    started = time.perf_counter()
    error = ""
    try:
        data = _SC_ENGINE.fetch_profile(handle) or {}
    except Exception as exc:
        error = str(exc)
        data = {"emails": [], "external_urls": [], "aggregator_expanded": 0, "status": "error", "reason": error}
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    emails_len = len(data.get("emails", []) or [])
    links_len = len(data.get("external_urls", []) or [])
    website = 1 if data.get("website") else 0
    elapsed = data.get("elapsed_ms", elapsed_ms)
    site_flag = int(data.get("aggregator_expanded", data.get("_aggregator_tried", 0)))
    tracks_source = data.get("latest_track_source") or data.get("tracks_source") or "none"
    if tracks_source == "rss":
        _sc_stat_inc("rss_used")
    return {
        "data": data,
        "elapsed_ms": elapsed,
        "links": links_len,
        "emails": emails_len,
        "site": site_flag or website,
        "error": error,
    }


def _sc_fetch_contacts_concurrently(handles: list) -> dict:
    results = {}
    if not handles:
        return results
    max_workers = min(SC_MAX_WORKERS, max(1, len(handles)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_sc_fetch_contact_payload, handle): handle for handle in handles}
        for future in as_completed(future_map):
            handle = future_map[future]
            try:
                results[handle] = future.result()
            except Exception as exc:
                print(f"[sc] handle={handle} error={exc}")
                results[handle] = {
                    "data": {"emails": [], "external_urls": [], "_aggregator_tried": 0},
                    "elapsed_ms": 0,
                    "links": 0,
                    "emails": 0,
                    "site": 0,
                    "error": str(exc),
                }
    return results


def _sc_has_contact_links(entry) -> bool:
    if not entry:
        return False
    payload = entry.get("data") if isinstance(entry, dict) else entry
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("emails") or payload.get("external_urls") or payload.get("website"))


def _sc_collect_contact_links(handle_jobs: list, min_yield: int) -> tuple:
    contact_map = {}
    processed_jobs = []
    hits = 0
    if not handle_jobs:
        return contact_map, processed_jobs, hits
    for start in range(0, len(handle_jobs), SC_LINK_BATCH_SIZE):
        chunk = handle_jobs[start:start + SC_LINK_BATCH_SIZE]
        handles = [job["handle"] for job in chunk]
        batch_results = _sc_fetch_contacts_concurrently(handles)
        for job in chunk:
            handle = job["handle"]
            contact_map[handle] = batch_results.get(handle, {
                "data": {"emails": [], "external_urls": [], "aggregator_expanded": 0},
                "elapsed_ms": 0,
                "links": 0,
                "emails": 0,
                "site": 0,
                "error": "not-fetched",
            })
        processed_jobs.extend(chunk)
    hits = sum(1 for handle in (job["handle"] for job in processed_jobs) if _sc_has_contact_links(contact_map.get(handle)))
    return contact_map, processed_jobs, hits


def _sc_apply_row_guards(row: dict):
    socials_raw = row.get("Social Link") or ""
    if socials_raw:
        cleaned_links = []
        for token in [part.strip() for part in socials_raw.split(";")]:
            if not token or token.lower() == "http://firefox.com":
                continue
            cleaned_links.append(token)
        row["Social Link"] = "; ".join(cleaned_links)
    artist = (row.get("Artist Name") or "").strip()
    location = (row.get("Location") or "").strip()
    if artist and location and artist.lower() == location.lower():
        row["Location"] = ""
    genre = (row.get("Primary Genre") or "").strip()
    if genre.lower() in _SC_GENRE_DENY:
        row["Primary Genre"] = ""
    assert row.get("Social Link", "") != "http://firefox.com"


def _sc_extract_contact_text_sections(doc):
    if doc is None:
        return []
    seen = set()
    sections = []
    for selector in SC_CONTACT_TEXT_SELECTORS:
        for node in doc.select(selector):
            text = node.get_text(" ", strip=True)
            normalized = re.sub(r"\s+", " ", text).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                sections.append(normalized)
    if not sections:
        for selector in SC_CONTACT_FALLBACK_SELECTORS:
            for node in doc.select(selector):
                text = node.get_text(" ", strip=True)
                normalized = re.sub(r"\s+", " ", text).strip()
                if not normalized or normalized in seen:
                    continue
                lower = normalized.lower()
                if "@" not in normalized and not any(keyword in lower for keyword in ("book", "contact", "enquiry", "inquiry", "press", "mgmt")):
                    continue
                seen.add(normalized)
                sections.append(normalized)
    return sections


def _sc_collect_emails_from_text(bucket: set, text: str):
    if not text:
        return
    for address in extract_emails(text):
        cleaned = (address or "").strip()
        if cleaned and not cleaned.lower().endswith("@soundcloud.com"):
            bucket.add(cleaned)


def _is_aggregator_link(url: str) -> bool:
    if not url:
        return False
    cleaned = (url or "").strip().strip(" ,.;:)>]\\\"'")
    if not cleaned or cleaned.lower().startswith("mailto:"):
        return False
    if not re.match(r"^[a-z]+://", cleaned, flags=re.IGNORECASE):
        cleaned = "https://" + cleaned.lstrip("/")
    try:
        host = (urlparse(cleaned).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == allow or host.endswith("." + allow) for allow in SC_AGGREGATOR_ALLOWLIST)


def _fetch_aggregator_emails(session: requests.Session, url: str, artist_name: str = "") -> List[str]:
    target = (url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        target = "https://" + target.lstrip("/")
    domain = (urlparse(target).hostname or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    artist_label = artist_name or ""
    print(f"[Aggregator] detected {domain or '<unknown>'} for '{artist_label or '<unknown>'}'")
    emails: List[str] = []
    try:
        resp = session.get(target, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        status = getattr(resp, "status_code", None)
        if status and status >= 400:
            print("[Aggregator] emails_found=0")
            print("[Aggregator] no emails found")
            polite_sleep()
            return []
        html = getattr(resp, "text", "") or ""
        for mail in extract_emails(html):
            normalized = (mail or "").strip().lower()
            if normalized and normalized not in emails:
                emails.append(normalized)
        print(f"[Aggregator] emails_found={len(emails)}")
        if not emails:
            print("[Aggregator] no emails found")
    except Exception:
        print("[Aggregator] emails_found=0")
        print("[Aggregator] no emails found")
    finally:
        polite_sleep()
    return emails


def _spotify_artist_id_from_url(url: str) -> str:
    if not url or "spotify.com" not in url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return ""
    netloc = (parsed.netloc or "").lower()
    if "spotify.com" not in netloc:
        return ""
    parts = (parsed.path or "").split("/")
    for idx, part in enumerate(parts):
        if part == "artist" and idx + 1 < len(parts):
            candidate = parts[idx + 1].split("?")[0].split("#")[0].strip()
            if candidate:
                return candidate
            return ""
    return ""


def _spotify_get_access_token(force_refresh: bool = False) -> str:
    global _SPOTIFY_ACCESS_TOKEN, _SPOTIFY_ACCESS_TOKEN_EXPIRY
    now = time.time()
    with _SPOTIFY_TOKEN_LOCK:
        if not force_refresh and _SPOTIFY_ACCESS_TOKEN and now < (_SPOTIFY_ACCESS_TOKEN_EXPIRY - 30):
            return _SPOTIFY_ACCESS_TOKEN
        client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN", "").strip()
        if not (client_id and client_secret and refresh_token):
            return ""
        try:
            resp = requests.post(
                SPOTIFY_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(client_id, client_secret),
                timeout=10,
            )
        except Exception:
            return ""
        if resp.status_code != 200:
            return ""
        try:
            payload = resp.json()
        except Exception:
            return ""
        access_token = payload.get("access_token") or ""
        expires_in = payload.get("expires_in") or 0
        if access_token:
            _SPOTIFY_ACCESS_TOKEN = access_token
            _SPOTIFY_ACCESS_TOKEN_EXPIRY = now + max(60, int(expires_in or 0))
        return access_token


def _spotify_get_artist_genres(artist_id: str) -> list[str]:
    if not artist_id:
        return []
    with _SPOTIFY_CACHE_LOCK:
        if artist_id in SPOTIFY_ARTIST_GENRE_CACHE:
            return SPOTIFY_ARTIST_GENRE_CACHE[artist_id]
    token = _spotify_get_access_token()
    if not token:
        with _SPOTIFY_CACHE_LOCK:
            SPOTIFY_ARTIST_GENRE_CACHE[artist_id] = []
        return []
    url = f"https://api.spotify.com/v1/artists/{artist_id}"
    headers = {"Authorization": f"Bearer {token}"}
    retried_401 = False
    retried_429 = False
    genres: list[str] = []
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except Exception:
            resp = None
        if resp is None:
            break
        if resp.status_code == 200:
            try:
                data = resp.json()
                genre_list = data.get("genres") or []
                genres = [g for g in genre_list if isinstance(g, str)]
            except Exception:
                genres = []
            break
        if resp.status_code == 401 and not retried_401:
            retried_401 = True
            new_token = _spotify_get_access_token(force_refresh=True)
            if not new_token:
                break
            headers["Authorization"] = f"Bearer {new_token}"
            continue
        if resp.status_code == 429 and not retried_429:
            retried_429 = True
            retry_after = 0
            try:
                retry_after = float(resp.headers.get("Retry-After", 0))
            except Exception:
                retry_after = 0
            time.sleep(min(10, retry_after if retry_after > 0 else 2))
            continue
        break
    with _SPOTIFY_CACHE_LOCK:
        SPOTIFY_ARTIST_GENRE_CACHE[artist_id] = genres
    return genres


def _sc_build_row(handle: str, payload: dict, soundcloud_link: str, fallback_name: str = "",
                  fallback_location: str = "", song_title: str = "", release_date: str = "",
                  sounds_like: str = "", fallback_tags=None, fallback_external=None, fallback_emails=None):
    payload = payload or {}
    fallback_tags = list(fallback_tags or [])
    fallback_external = fallback_external or []
    fallback_emails = fallback_emails or []
    default_name = handle.replace("-", " ").replace("_", " ").title()
    display_name = payload.get("display_name") or fallback_name or default_name
    location_value = normalize_location(
        payload.get("city"),
        payload.get("country") or payload.get("country_name")
    )
    if not location_value:
        location_value = (fallback_location or "").strip()
    latest_tags = payload.get("latest_track_tags") or []
    if latest_tags:
        fallback_tags.extend(latest_tags)
    genre_source = payload.get("genre") or payload.get("latest_track_genre")
    primary_genre_value = choose_primary_genre(genre_source, fallback_tags)
    external_sources = list(payload.get("external_urls") or []) + list(fallback_external or [])
    emails_source = list(payload.get("emails") or []) + list(fallback_emails or [])
    if not song_title:
        song_title = payload.get("latest_track_title") or ""
    release_candidate = payload.get("latest_track_release_date") or ""
    if (not release_date) or (release_date and release_date.strip().lower() == "not present"):
        release_date = release_candidate or release_date
    if not sounds_like:
        sounds_like = payload.get("sounds_like") or ""

    def _dedupe_external(items):
        seen = set()
        cleaned = []
        for item in items:
            if not item or not isinstance(item, str):
                continue
            val = item.strip()
            if not val.startswith(("http://", "https://")):
                continue
            if val.lower() == "http://firefox.com":
                continue
            if val in seen:
                continue
            seen.add(val)
            cleaned.append(val)
        return cleaned

    def _dedupe_emails(items):
        seen = set()
        cleaned = []
        for item in items:
            if not item or not isinstance(item, str):
                continue
            val = item.strip()
            if not val or val.lower().endswith("@soundcloud.com"):
                continue
            if val in seen:
                continue
            seen.add(val)
            cleaned.append(val)
        return cleaned

    external_urls = _dedupe_external(external_sources)
    emails = _dedupe_emails(emails_source)
    row_data = {
        "handle": handle,
        "display_name": display_name,
        "external_urls": external_urls,
        "emails": emails,
        "soundcloud_link": soundcloud_link,
    }
    row = export_soundcloud_row(row_data)
    # Promote latest track genre to artist primary genre (non-destructive)
    def _normalize_primary_genre(value: str) -> str:
        if not value or not isinstance(value, str):
            return ""
        cleaned = value.replace("_", " ")
        cleaned = re.sub(r"[\u2600-\u27BF]|[\U00010000-\U0010FFFF]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
        return cleaned[:32]

    if not primary_genre_value:
        latest_genre = payload.get("latest_track_genre") or ""
        latest_tags = payload.get("latest_track_tags") or []
        promo_candidate = latest_genre or (latest_tags[0] if latest_tags else "")
        promo_candidate = _normalize_primary_genre(promo_candidate)
        if promo_candidate:
            primary_genre_value = promo_candidate
    if not primary_genre_value:
        user_genre = payload.get("user_genre") or payload.get("genre") or ""
        user_genre = _normalize_primary_genre(user_genre)
        if user_genre:
            primary_genre_value = user_genre
    if not primary_genre_value:
        spotify_artist_id = ""
        spotify_candidates = list(payload.get("external_urls") or [])
        if not spotify_candidates:
            social_links_raw = row.get("Social Link") or ""
            spotify_candidates.extend([token.strip() for token in social_links_raw.split(";") if token.strip()])
        for candidate in spotify_candidates:
            spotify_artist_id = _spotify_artist_id_from_url(candidate)
            if spotify_artist_id:
                break
        if spotify_artist_id:
            spotify_genres = _spotify_get_artist_genres(spotify_artist_id)
            if spotify_genres:
                spotify_primary = _normalize_primary_genre(spotify_genres[0])
                if spotify_primary:
                    primary_genre_value = spotify_primary
    row["Location"] = location_value
    row["Primary Genre"] = primary_genre_value
    row["Song Title"] = (song_title or "").strip()
    row["Release Date"] = (release_date or "").strip()
    row["Sounds Like"] = (sounds_like or "").strip()
    _sc_apply_row_guards(row)
    return row, external_urls, emails


def _sc_log_csv_row(handle: str, row: dict, external_urls=None, emails=None):
    external_urls = external_urls or []
    emails = emails or []
    social_ok = bool(row.get("Social Link"))
    print(
        f'[csv] {handle} name="{row.get("Artist Name","")}" '
        f'loc="{row.get("Location","")}" genre="{row.get("Primary Genre","")}"\n'
        f'      social={social_ok} email={bool(row.get("Email"))} links_ct={len(external_urls)}'
    )
    if external_urls and not social_ok:
        print(f"[alert] mapping mismatch: links found but Social Link empty; exts[:3]={external_urls[:3]}")


def _sc_print_dry_run_row(handle: str, row: dict, external_urls: list, emails: list):
    print(f'[dry-run] {handle} name="{row.get("Artist Name","")}" '
          f'social="{row.get("Social Link","")}" email="{row.get("Email","")}" '
          f'external_count={len(external_urls)}')


def _sc_normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/"):
        return "https://soundcloud.com" + u
    return u

def _sc_accept_consent_if_present(driver):
    """Dismiss OneTrust or generic consent banners so content loads."""
    try:
        WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        for xp in [
            "//*[@id='onetrust-accept-btn-handler']",
            "//button[contains(@class,'onetrust-accept-btn-handler')]",
            "//button[contains(., 'Accept All')]",
            "//button[contains(., 'Accept all')]",
            "//button[contains(., 'I agree')]",
            "//button[contains(., 'Accept & Continue')]",
        ]:
            try:
                btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.5)
                break
            except Exception:
                continue
    except Exception:
        pass

def _sc_soft_scroll(driver):
    try:
        for y in (300, 900, 1600):
            driver.execute_script(f"window.scrollTo(0,{y});")
            time.sleep(0.3)
    except Exception:
        pass

def _sc_unwrap_gate(href: str) -> str:
    """
    Unwrap gate.sc redirect URLs that embed the real target in ?url=.
    """
    if not href:
        return href
    try:
        parsed = urlparse(href)
        if parsed.netloc and "gate.sc" in parsed.netloc.lower():
            inner = parse_qs(parsed.query or "").get("url", [None])[0]
            if inner:
                return unquote(inner)
    except Exception:
        pass
    return href

def _sc_handle_from_profile(profile_url: str) -> str:
    try:
        parsed = urlparse(profile_url)
        return (parsed.path or "/").strip("/").split("/")[0]
    except Exception:
        return ""

def _norm_url_general(href: str) -> str:
    if not href:
        return ""
    href = _sc_unwrap_gate(href.strip())
    if href.startswith(("facebook.com/", "m.facebook.com/", "fb.me/")):
        href = "https://" + href
    try:
        parsed = urlparse(href)
        host = (parsed.netloc or "").lower()
        if host in _FB_REDIRECT_HOSTS:
            inner = parse_qs(parsed.query or "").get("u", [None])[0]
            if inner:
                return unquote(inner)
    except Exception:
        pass
    return href

def _sc_try_dismiss_consent(driver):
    selectors = [
        "#onetrust-accept-btn-handler",
        ".onetrust-close-btn-handler",
        "button[aria-label='Accept All']",
        "button[aria-label='Accept all']",
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            el.click()
            time.sleep(0.2)
            return
        except Exception:
            continue

def _sc_try_show_more(driver):
    xpaths = [
        "//button[.//*[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]]",
        "//a[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
        "//button[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'see more')]",
    ]
    for xp in xpaths:
        try:
            btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
            btn.click()
            time.sleep(0.25)
            return
        except Exception:
            continue

def _sc_scroll_sidebar(driver):
    try:
        driver.execute_script("window.scrollTo(0,0);")
        driver.execute_script(
            "const el = document.querySelector('.profileSidebar') || document.querySelector('.profileHeaderInfo') || document.querySelector('ul.profileLinks__linkList');"
            "if (el) el.scrollIntoView({behavior:'instant', block:'center'});"
        )
        time.sleep(0.25)
    except Exception:
        pass

def _norm_fb(url: str) -> str:
    return _norm_url_general(url)

def _is_fb(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(h in host for h in _FB_HOSTS)
    except Exception:
        return False

def _is_company_fb(href: str) -> bool:
    try:
        path = (urlparse(href).path or "").lower()
        return any(path.startswith(deny) for deny in _FB_PATH_DENY)
    except Exception:
        return False

def _sc_extract_objects_from_hydration(html: str) -> list:
    try:
        match = re.search(r"__sc_hydration\s*=\s*(\[[\s\S]*?\])\s*;?", html, flags=re.M)
        if not match:
            return []
        data = json.loads(match.group(1))
        return data if isinstance(data, list) else [data]
    except Exception:
        return []

def _sc_collect_urls_from_obj(obj, bucket: set):
    if isinstance(obj, str):
        if obj.startswith("http") or obj.startswith("mailto:"):
            bucket.add(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            _sc_collect_urls_from_obj(value, bucket)
    elif isinstance(obj, list):
        for value in obj:
            _sc_collect_urls_from_obj(value, bucket)

def _sc_extract_urls_from_hydration(html: str) -> set:
    urls = set()
    for obj in _sc_extract_objects_from_hydration(html):
        _sc_collect_urls_from_obj(obj, urls)
    return urls


def _extract_from_json_or_regex(html: str) -> set:
    urls = set()
    soup = _safe_bs(html)
    for script in soup.find_all("script"):
        txt = script.string or ""
        if not txt or ("http" not in txt and "sameAs" not in txt):
            continue
        urls.update(URL_RE.findall(txt))
        try:
            data = json.loads(txt)
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for key, value in cur.items():
                    if isinstance(value, (list, tuple)) and key and key.lower() in ("sameas", "externallinks", "external_url", "externalurls", "socials"):
                        for item in value:
                            if isinstance(item, str) and item.startswith("http"):
                                urls.add(item)
                    stack.append(value)
            elif isinstance(cur, list):
                stack.extend(cur)
    if not urls:
        urls.update(URL_RE.findall(html or ""))
    return urls

def _sc_extract_urls_from_ldjson(soup) -> set:
    urls = set()
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        _sc_collect_urls_from_obj(data, urls)
    return urls


def _sc_scan_for_user_blob(root):
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            has_location = any(key in cur for key in ("city", "city_name", "country", "country_name", "country_code"))
            has_identity = any(key in cur for key in ("display_name", "full_name", "username", "permalink"))
            if has_location and has_identity:
                return cur
            for value in cur.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            for value in cur:
                if isinstance(value, (dict, list)):
                    stack.append(value)
    return {}


def _sc_extract_user_profile_from_html(html: str) -> dict:
    for obj in _sc_extract_objects_from_hydration(html):
        candidate = _sc_scan_for_user_blob(obj)
        if candidate:
            display_name = (
                candidate.get("display_name")
                or candidate.get("full_name")
                or candidate.get("username")
                or candidate.get("permalink")
                or ""
            )
            city = candidate.get("city") or candidate.get("city_name") or ""
            country_raw = (
                candidate.get("country_name")
                or candidate.get("country")
                or candidate.get("country_code")
                or ""
            )
            country_name = _resolve_country_name(country_raw)
            genre = candidate.get("genre") or candidate.get("music_style") or ""
            return {
                "display_name": display_name,
                "city": city,
                "country": country_name,
                "genre": genre,
            }
    return {}


def _sc_try_linktree_for_contacts(driver, urls: set, timeout=6) -> tuple:
    linktree_url = None
    for candidate in urls or set():
        try:
            host = (urlparse(candidate).netloc or "").lower()
            if any(h in host for h in {"linktr.ee", "linktree.com", "www.linktr.ee", "www.linktree.com"}):
                linktree_url = candidate
                break
        except Exception:
            continue
    if not linktree_url:
        return "", ""
    try:
        driver.get(linktree_url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        soup = BeautifulSoup(driver.page_source, "html.parser")
        fb = ""
        em = ""
        for anchor in soup.find_all("a", href=True):
            href = _norm_url_general(anchor["href"])
            if _is_fb(href) and not _is_company_fb(href):
                fb = href
                break
            if href.startswith("mailto:"):
                candidate_email = href.split("mailto:")[-1].split("?")[0]
                if candidate_email and not candidate_email.lower().endswith("@soundcloud.com"):
                    em = candidate_email
        return fb, em
    except Exception:
        return "", ""


def _sc_collect_from_people_search(driver, search_url, max_handles=200) -> list:
    """
    Given a SoundCloud people search URL, return handles by scrolling.
    """
    handles, seen = [], set()
    try:
        driver.get(search_url)
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _sc_try_dismiss_consent(driver)
    except Exception:
        pass

    for _ in range(10):
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if not href or href in ("/", "#"):
                continue
            absolute = _sc_normalize_url(href)
            ok, slug = is_valid_sc_url(absolute)
            if not ok or not slug:
                continue
            if slug in seen:
                continue
            seen.add(slug)
            handles.append(slug)
            if len(handles) >= max_handles:
                return handles
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            break
        time.sleep(0.8)
    return handles


def _sc_rel_to_iso(text: str) -> str:
    """
    Convert 'x day(s)/week(s)/month(s)/year(s) ago' to an approximate ISO date (YYYY-MM-DD).
    If not parseable, return "not present".
    """
    if not text:
        return "not present"
    s = text.strip().lower()
    m = re.search(r'(\d+)\s*(day|week|month|year)s?\s+ago', s)
    if not m:
        return "not present"
    n = int(m.group(1))
    unit = m.group(2)
    now = datetime.datetime.now()
    try:
        if unit == "day":
            dt = now - relativedelta(days=n)
        elif unit == "week":
            dt = now - relativedelta(weeks=n)
        elif unit == "month":
            dt = now - relativedelta(months=n)
        else:
            dt = now - relativedelta(years=n)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "not present"


def _sc_first_text(soup, selectors):
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return ""


def _sc_preferred_artist_name_from_soup(soup):
    if soup is None:
        return ""
    hero_selectors = [
        ".soundTitle__usernameHeroName",
        ".soundTitle__usernameHeroLink:last-child",
        ".soundTitle__usernameHero a:last-child",
        "[data-testid='playback-sound-badge-artist-name']",
    ]
    for selector in hero_selectors:
        el = soup.select_one(selector)
        if not el:
            continue
        text = el.get_text(" ", strip=True)
        if text:
            return text
    hero_container = soup.select_one(".soundTitle__usernameHero")
    if hero_container:
        text = hero_container.get_text(" ", strip=True)
        parts = [part.strip("•·|- ").strip() for part in re.split(r"[•·|]", text) if part.strip()]
        if len(parts) >= 2:
            candidate = parts[-1]
            if candidate:
                return candidate
    return ""


def _sc_extract_profile_meta(driver, soup_override=None) -> dict:
    """
    Best-effort metadata from a SoundCloud profile root page:
      - artist_name (if present)
      - location (right column: 'Based in ...')
      - song_title (first visible track or spotlight)
      - primary_genre (first '#Tag' chip near the first track)
      - release_date (approx from 'x months ago', else 'not present')
    """
    meta = {
        "artist_name": "",
        "location": "",
        "song_title": "",
        "primary_genre": "",
        "release_date": "not present",
        "sounds_like": ""
    }
    preferred_artist = ""
    if soup_override is None:
        try:
            _sc_try_dismiss_consent(driver)
        except Exception:
            pass
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
    else:
        soup = soup_override
    preferred_artist = _sc_preferred_artist_name_from_soup(soup)

    if preferred_artist:
        meta["artist_name"] = preferred_artist
    if not meta["artist_name"]:
        meta["artist_name"] = _sc_first_text(soup, [
            "h1.soundTitle__username",
            "div.profileHeaderInfo h1",
            "meta[property='og:title']"
        ])
    if not meta["artist_name"]:
        og = soup.select_one("meta[property='og:title']")
        if og and og.get("content"):
            meta["artist_name"] = og["content"].strip()

    loc = _sc_first_text(soup, [
        "div.profileHeaderInfo",
        ".profileSidebar"
    ])
    m = re.search(r"based in\s+(.+?)(?:\s{2,}|$)", loc, flags=re.I)
    if m:
        meta["location"] = m.group(1).strip()

    track_container = None
    for sel in [
        ".spotlight .soundList__item",
        ".profileStream__list .soundList__item",
        ".soundList__item"
    ]:
        track_container = soup.select_one(sel)
        if track_container:
            break

    if track_container:
        t = _sc_first_text(track_container, [
            "a.soundTitle__title",
            "a.trackItem__trackTitle",
            ".soundTitle__title",
            "[data-e2e='track-title']",
        ])
        if t:
            meta["song_title"] = t

        tag = _sc_first_text(track_container, [
            "a[href*='/tags/']",
            ".sc-tag",
            "a[aria-label^='#']"
        ])
        if tag:
            meta["primary_genre"] = tag.lstrip("#").strip().title()

        rel = _sc_first_text(track_container, [
            ".relativeTime",
            "time[datetime]",
            "time",
            "span[aria-label*='ago']"
        ])
        if rel:
            if "datetime" in rel.lower():
                tm = track_container.select_one("time[datetime]")
                if tm and tm.get("datetime"):
                    meta["release_date"] = tm["datetime"][:10]
            if meta["release_date"] == "not present":
                meta["release_date"] = _sc_rel_to_iso(rel)

    return meta

def _sc_collect_profile_links(driver, timeout=6, **_ignored) -> set:
    """
    Return a set of outbound link hrefs visible on a SoundCloud profile page.
    Accepts a timeout kwarg for compatibility. Safe if called with extra kwargs.
    """
    found = set()
    try:
        _sc_try_dismiss_consent(driver)
        _sc_try_show_more(driver)
        _sc_scroll_sidebar(driver)

        # Give the sidebar a brief chance to render
        try:
            WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ul.profileLinks__linkList a[href]"))
            )
        except Exception:
            pass

        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        # 1) Preferred: explicit sidebar list
        for a in soup.select("ul.profileLinks__linkList a[href]"):
            found.add(a.get("href", "").strip())

        # 2) Broad fallback: any obvious social/email anchors
        for a in soup.find_all("a", href=True):
            href = (a["href"] or "").strip()
            low = href.lower()
            if any(k in low for k in [
                "facebook.com", "fb.me/", "mailto:", "linktr.ee", "linktree",
                "gate.sc?url=http", "l.facebook.com/l.php?u="
            ]):
                found.add(href)

        # 3) Hydration fallback (external_links / externalLinks)
        m = re.search(r"__sc_hydration\s*=\s*(\[[\s\S]*?\])\s*;?", html, flags=re.M)
        if m:
            try:
                data = json.loads(m.group(1))
                stack = [data]
                while stack:
                    v = stack.pop()
                    if isinstance(v, dict):
                        ext = v.get("external_links") or v.get("externalLinks")
                        if isinstance(ext, list):
                            for it in ext:
                                if isinstance(it, dict) and isinstance(it.get("url"), str):
                                    found.add(it["url"])
                        for vv in v.values():
                            if isinstance(vv, (dict, list)):
                                stack.append(vv)
                            elif isinstance(vv, str):
                                if any(s in vv for s in [
                                    "facebook.com", "mailto:", "linktr.ee", "linktree",
                                    "gate.sc?url=http", "l.facebook.com/l.php?u="
                                ]):
                                    found.add(vv)
                    elif isinstance(v, list):
                        stack.extend(v)
            except Exception:
                pass
    except Exception:
        pass
    return found

def _sc_collect_from_tag_page(driver, tag_url: str) -> list:
    """
    Extract artist profile URLs from /tags/{tag}?page=N by inspecting track links.
    Only accepts handles that pass _sc_is_valid_handle.
    """
    results = []
    seen = set()
    tag_value = ""
    match = re.search(r"/tags/([^/?#]+)", tag_url or "")
    if match:
        tag_value = match.group(1)
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        anchors = soup.find_all("a", href=True)
        for anchor in anchors:
            href = anchor["href"].strip()
            if not href:
                continue
            if href.startswith("http"):
                parsed = urlparse(href)
                if "soundcloud.com" not in (parsed.netloc or "").lower():
                    continue
                path = (parsed.path or "").strip("/")
            else:
                path = href.strip("/")
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) < 2:
                continue
            handle = parts[0]
            if not _sc_is_valid_handle(handle):
                continue
            profile_url = f"https://soundcloud.com/{handle}"
            if profile_url in seen:
                continue
            seen.add(profile_url)
            results.append({"url": profile_url, "primary_genre": tag_value})
    except Exception as exc:
        print(f"SoundCloud: collect failed on {tag_url}: {exc}")
    return results

def _sc_quick_has_fb_or_email(driver, url: str, timeout=10, debug_prefix="") -> tuple:
    fb, em = "", ""
    try:
        handle = _sc_handle_from_profile(url) or url.rstrip("/").split("/")[-1]
        links_links = set()
        root_links = set()
        if handle:
            try:
                links_url = f"https://soundcloud.com/{handle}/links"
                driver.get(links_url)
                WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                _sc_try_dismiss_consent(driver)
                time.sleep(0.6)
                links_links = _sc_collect_profile_links(driver, timeout=6)
                if debug_prefix:
                    print(f"{debug_prefix} links_found={len(links_links)}")
            except Exception:
                if debug_prefix:
                    print(f"{debug_prefix} links page not available")
        if handle and not links_links:
            try:
                root_url = f"https://soundcloud.com/{handle}"
                driver.get(root_url)
                WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                _sc_try_dismiss_consent(driver)
                _sc_try_show_more(driver)
                _sc_scroll_sidebar(driver)
                time.sleep(0.6)
                root_links = _sc_collect_profile_links(driver, timeout=6)
                if debug_prefix:
                    print(f"{debug_prefix} root_found={len(root_links)}")
            except Exception:
                if debug_prefix:
                    print(f"{debug_prefix} root page not available")
        if not handle:
            driver.get(url)
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            root_links = _sc_collect_profile_links(driver, timeout=timeout)
            if debug_prefix:
                print(f"{debug_prefix} root_found={len(root_links)}")

        found = set(links_links) | set(root_links)
        cleaned = []
        for raw in found:
            normalized = _norm_url_general(raw)
            if not normalized:
                continue
            host = (urlparse(normalized).netloc or "").lower()
            if not host or "soundcloud.com" in host:
                continue
            cleaned.append(normalized)

        for candidate in cleaned:
            if _is_fb(candidate) and not _is_company_fb(candidate):
                fb = candidate
                break

        if not em:
            for candidate in cleaned:
                if candidate.startswith("mailto:"):
                    address = candidate.split("mailto:")[-1].split("?")[0]
                    if address and not address.lower().endswith("@soundcloud.com"):
                        em = address
                        break
        if not em:
            try:
                soup = BeautifulSoup(driver.page_source, "html.parser")
                for address in extract_emails(soup.get_text(" ", strip=True)):
                    if address and not address.lower().endswith("@soundcloud.com"):
                        em = address
                        break
            except Exception:
                pass

        if not fb and not em and cleaned:
            fb, em = _sc_try_linktree_for_contacts(driver, set(cleaned), timeout=6)

        if not fb and not em and debug_prefix:
            print(f"{debug_prefix} no fb/email")

    except Exception as exc:
        if debug_prefix:
            print(f"{debug_prefix} error: {exc}\n{traceback.format_exc()}")
    return fb, em

def _sc_profile_basics(driver, profile_url: str, timeout=10) -> tuple:
    location = ""
    bio_text = ""
    soup = None
    try:
        driver.get(profile_url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        try:
            _sc_try_dismiss_consent(driver)
        except Exception:
            pass
        try:
            WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".profileHeaderInfo")))
        except Exception:
            pass
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        loc_el = (soup.select_one(".profileHeaderInfo__additional") or
                  soup.select_one(".profileHeaderInfo__location") or
                  soup.select_one("[itemprop='addressLocality']"))
        if loc_el:
            location = loc_el.get_text(" ", strip=True)
        bio_el = (soup.select_one(".profileHeaderInfo__bio") or
                  soup.select_one('[data-testid="profile-bio"]'))
        if bio_el:
            bio_text = bio_el.get_text(" ", strip=True)
        if not location or not bio_text:
            for obj in _sc_extract_objects_from_hydration(html):
                if not isinstance(obj, dict):
                    continue
                if not location:
                    city = obj.get("city") or obj.get("address_city")
                    country = obj.get("country_code") or obj.get("address_country")
                    candidate_loc = " ".join([str(city or ""), str(country or "")]).strip()
                    if candidate_loc:
                        location = candidate_loc
                if not bio_text and isinstance(obj.get("description"), str):
                    bio_text = obj.get("description")
                if location and bio_text:
                    break
    except Exception:
        soup = None
    return location, bio_text, soup

def _sc_quick_first_track_meta(driver, profile_url: str, timeout=12, hop=True) -> tuple:
    title = ""
    date_iso = ""
    precision = ""
    genres = []
    try:
        handle = profile_url.rstrip("/").split("/")[-1]
        tracks_url = f"https://soundcloud.com/{handle}/tracks"
        driver.get(tracks_url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        card = None
        first_url = None
        track_candidates = []
        for c in soup.select(".soundList__item, .lazyLoadingList__item, li, article"):
            anchor = (c.select_one("a.soundTitle__title") or
                      c.select_one("a.sc-link-primary") or
                      c.select_one(f"a[href^='/{handle}/']"))
            t = c.find("time")
            if not anchor and not t:
                continue
            candidate_title = ""
            if anchor:
                candidate = anchor.get_text(" ", strip=True)
                if candidate and candidate.lower() not in ("home", "tracks", "likes"):
                    candidate_title = candidate[:200]
            href = ""
            if anchor and anchor.get("href"):
                href = anchor["href"]
                if not href.startswith("http"):
                    href = f"https://soundcloud.com{href}"
            iso_val = ""
            precision_val = ""
            sort_key = None
            if t:
                raw_dt = (t.get("datetime") or "").strip()
                if not raw_dt:
                    raw_dt = t.get_text(" ", strip=True)
                if raw_dt:
                    iso_val, precision_val = _parse_any_date_to_iso(raw_dt)
                    if iso_val:
                        try:
                            sort_key = datetime.datetime.strptime(iso_val, "%Y-%m-%d")
                        except Exception:
                            sort_key = None
            track_candidates.append({
                "card": c,
                "title": candidate_title,
                "href": href,
                "iso": iso_val,
                "precision": precision_val,
                "sort_key": sort_key
            })
        if track_candidates:
            dated = [cand for cand in track_candidates if cand["sort_key"]]
            chosen = max(dated, key=lambda cand: cand["sort_key"]) if dated else track_candidates[0]
            card = chosen["card"]
            if chosen["title"]:
                title = chosen["title"]
            if chosen["iso"]:
                date_iso = chosen["iso"]
                precision = chosen["precision"]
            first_url = chosen["href"] or None
        else:
            first_url = None
        if card:
            anchor = (card.select_one("a.soundTitle__title") or
                      card.select_one("a.sc-link-primary") or
                      card.select_one(f"a[href^='/{handle}/']"))
            if anchor:
                candidate = anchor.get_text(" ", strip=True)
                if candidate and candidate.lower() not in ("home", "tracks", "likes"):
                    if not title:
                        title = candidate[:200]
                if not first_url and anchor.get("href"):
                    href = anchor["href"]
                    first_url = href if href.startswith("http") else f"https://soundcloud.com{href}"
            t = card.find("time")
            if t and not date_iso:
                dt = (t.get("datetime") or "").strip()
                if dt:
                    iso, prec = _parse_any_date_to_iso(dt)
                    if iso:
                        date_iso, precision = iso, prec or "day"
        for obj in _sc_extract_objects_from_hydration(html):
            if not isinstance(obj, dict):
                continue
            g = obj.get("genre")
            if isinstance(g, str) and g.strip():
                genres.append(g.strip().lower())
            tag_list = obj.get("tag_list")
            if isinstance(tag_list, str) and tag_list.strip():
                for token in re.split(r"[, ]+", tag_list.strip()):
                    if token:
                        genres.append(token.lower())
        if hop and (not title or not date_iso):
            if first_url:
                driver.get(first_url)
                WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                track_html = driver.page_source
                track_soup = BeautifulSoup(track_html, "html.parser")
                if not title:
                    og = track_soup.select_one('meta[property="og:title"]')
                    if og and og.get("content"):
                        title = og["content"].strip()[:200]
                if not date_iso:
                    t = track_soup.find("time")
                    if t:
                        dt = (t.get("datetime") or "").strip()
                        if dt:
                            iso, prec = _parse_any_date_to_iso(dt)
                            if iso:
                                date_iso, precision = iso, prec or "day"
                for obj in _sc_extract_objects_from_hydration(track_html):
                    if not isinstance(obj, dict):
                        continue
                    g = obj.get("genre")
                    if isinstance(g, str) and g.strip():
                        genres.append(g.strip().lower())
                    tag_list = obj.get("tag_list")
                    if isinstance(tag_list, str) and tag_list.strip():
                        for token in re.split(r"[, ]+", tag_list.strip()):
                            if token:
                                genres.append(token.lower())
        if genres:
            seen = set()
            clean = []
            for g in genres:
                normalized = re.sub(r"[^a-z0-9 +\-_/]", "", g.lower()).strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    clean.append(normalized)
            genres = clean[:6]
    except Exception:
        pass
    return title, date_iso or "", precision or "", genres

def _sc_resolve_artist_profile_url(candidate_url: str) -> str:
    """
    Normalize a candidate SoundCloud URL to canonical artist profile if possible.
    - If already looks like https://soundcloud.com/{handle}, return as is.
    - If it's a track URL, try to strip to /{handle}.
    """
    if not candidate_url:
        return ""
    try:
        url = _sc_normalize_url(candidate_url)
        parsed = urlparse(url)
        if "soundcloud.com" not in parsed.netloc.lower():
            return ""
        path = (parsed.path or "").strip("/")
        if not path:
            return ""
        parts = path.split("/")
        handle = parts[0].strip()
        if not _sc_is_valid_handle(handle):
            return ""
        return f"https://soundcloud.com/{handle}"
    except Exception:
        return ""

_SC_SOCIAL_DOMAINS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.me": "facebook",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "linktr.ee": "linktree",
    "linktree": "linktree",
    "withkoji.com": "linktree",
    "beacons.ai": "linktree",
    "spotify.com": "spotify",
    "bandsintown.com": "bandsintown",
    "songkick.com": "songkick",
}

_SC_JUNK_HOSTS = (
    _SC_JUNK_HOSTS
    if "_SC_JUNK_HOSTS" in globals()
    else {
        "www.enable-javascript.com", "enable-javascript.com",
        "firefox.com", "www.firefox.com", "mozilla.org", "www.mozilla.org",
        "google.com", "www.google.com", "chrome.com", "www.chrome.com",
    }
)

_SC_STORE_HOSTS = (
    _SC_STORE_HOSTS if "_SC_STORE_HOSTS" in globals() else set()
)
_SC_STORE_HOSTS = set(_SC_STORE_HOSTS) | {"apps.apple.com", "itunes.apple.com", "play.google.com"}

_SC_CONSENT_HOSTS = {"onetrust.com", "www.onetrust.com", "cookiepro.com", "www.cookiepro.com"}
_FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.me", "l.facebook.com", "lm.facebook.com"}
_FB_PATH_DENY = {"/soundcloud"}
_FB_REDIRECT_HOSTS = {"l.facebook.com", "lm.facebook.com"}
_LINKTREE_HOSTS = {"linktr.ee", "www.linktr.ee", "linktree", "linktree.com", "www.linktree.com"}
_SC_HOSTS = {"soundcloud.com", "www.soundcloud.com"}
_SC_SOUNDS_PATTERNS = [
    r"\bffo\b[:\-–]\s*([^.;\n]+)",
    r"\briyl\b[:\-–]\s*([^.;\n]+)",
    r"\bfor\s+fans\s+of\b[:\-–]?\s*([^.;\n]+)",
    r"\bsounds\s+like\b[:\-–]?\s*([^.;\n]+)",
    r"\binfluences?\b[:\-–]?\s*([^.;\n]+)",
    r"\binspired\s+by\b[:\-–]?\s*([^.;\n]+)",
]

_SC_RESERVED_SLUGS = {
    "soundcloud", "feed", "upload", "artist", "artists", "tags", "getstarted", "transparency-reports",
    "terms-of-use", "terms", "privacy", "cookie-policy", "cookies", "legal", "copyright", "imprint",
    "contact", "press", "about", "company", "jobs", "developers", "forartists", "pro", "go",
    "on-soundcloud", "login", "signup", "you", "stream", "discover", "charts", "popular", "stations",
    "settings", "pages", "help", "brand", "policy", "resources", "ads"
}

_RESERVED_SC = {
    "search", "popular", "charts", "stream", "you", "discover", "stations",
    "groups", "pro", "for", "creators", "repost", "likes", "home",
    "soundcloud", "soundcloud-scenes", "radio", "radio-indie"
}

def _sc_handle_ok(h: str) -> bool:
    if not h or "/" in h or h.startswith("_"):
        return False
    h = h.strip().lower()
    if h in _RESERVED_SC:
        return False
    return bool(re.match(r"^[a-z0-9][a-z0-9\-_.]{2,}$", h))

def _sc_is_valid_handle(handle: str) -> bool:
    if not handle:
        return False
    h = handle.strip("/").lower()
    if h in _SC_RESERVED_SLUGS:
        return False
    if not any(ch.isalpha() for ch in h):
        return False
    if len(h) < 2 or len(h) > 50:
        return False
    if re.fullmatch(r"[0-9\-]+", h):
        return False
    return True

def _sc_unpack3(candidate):
    if not candidate:
        return ("", "", "")
    if isinstance(candidate, (str, bytes)):
        return (str(candidate).strip(), "", "")
    if isinstance(candidate, dict):
        url = str(candidate.get("url", "")).strip()
        tag = str(candidate.get("tag", "")).strip()
        genre = str(candidate.get("genre", "")).strip()
        if url:
            return (url, tag, genre)
        candidate = list(candidate.values())
    try:
        seq = list(candidate)
    except Exception:
        return (str(candidate).strip(), "", "")
    url = str(seq[0]).strip() if len(seq) > 0 else ""
    tag = str(seq[1]).strip() if len(seq) > 1 else ""
    genre = str(seq[2]).strip() if len(seq) > 2 else ""
    return (url, tag, genre)

def _sc_append(candidate_profiles, candidate):
    candidate_profiles.append(_sc_unpack3(candidate))

def _sc_sounds_like_from_bio(bio_text: str) -> str:
    if not bio_text:
        return ""
    text = re.sub(r"\s+", " ", bio_text).strip()
    matches = []
    for pattern in _SC_SOUNDS_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.group(1):
            matches.append(match.group(1))
    if not matches:
        return ""
    combined = ", ".join(matches)
    tokens = re.split(r"[,/|•]+|\band\b|\&", combined, flags=re.IGNORECASE)
    seen = set()
    clean = []
    for token in tokens:
        trimmed = re.sub(r"\s+", " ", token).strip(" .;:()[]{}\"")
        lowered = trimmed.lower()
        if trimmed and lowered not in seen:
            seen.add(lowered)
            clean.append(trimmed.title())
    return ", ".join(clean[:6])

def _sc_url_mode(url: str) -> tuple:
    if not url:
        return ("tags", "")
    try:
        parsed = _urlparse.urlparse(url)
        host = (parsed.netloc or "").lower()
        if not any(h in host for h in _SC_HOSTS):
            return ("tags", "")
        path = (parsed.path or "").strip("/")
        if path.startswith("search/people"):
            qs = _urlparse.parse_qs(parsed.query or "")
            query = (qs.get("q", [""])[0] or "").strip()
            return ("search_people", query)
        segments = [seg for seg in path.split("/") if seg]
        if segments and segments[0] not in {"search", "discover"}:
            return ("profile", segments[0])
    except Exception:
        pass
    return ("tags", "")

def _sc_extract_tags(soup) -> list:
    """
    Best-effort tag/genre extraction from profile + meta.
    """
    tags = set()
    meta_k = soup.select_one('meta[name="keywords"]')
    if meta_k and meta_k.get("content"):
        for token in _norm_tokens(meta_k["content"]):
            if token:
                tags.add(token.lower())
    for span in soup.find_all(["a", "span"], class_=re.compile("tag|genre|chip", re.I)):
        txt = span.get_text(" ", strip=True)
        if txt:
            tags.add(txt.lower())
    return list(tags)

def _sc_fetch_latest_track(driver, profile_url: str) -> tuple:
    """Return (title, date_iso, precision) strictly from /tracks with <time datetime>."""
    try:
        parsed = urlparse(profile_url)
        handle = (parsed.path or "/").strip("/").split("/")[0]
        if not handle:
            return "", "", ""
        tracks_url = f"https://soundcloud.com/{handle}/tracks"
        driver.get(tracks_url)
        _sc_accept_consent_if_present(driver)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _sc_soft_scroll(driver)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        card = None
        for c in soup.select(".soundList__item, .lazyLoadingList__item, li, article"):
            t = c.find("time")
            if t and (t.get("datetime") or "").strip():
                card = c
                break
        if not card:
            return "", "", ""
        title = ""
        t_anchor = (card.select_one("a.soundTitle__title") or
                    card.select_one("a.sc-link-primary") or
                    card.select_one(f"a[href*='/{handle}/']"))
        if t_anchor:
            candidate = t_anchor.get_text(" ", strip=True)
            if candidate and candidate.lower() not in ("home", "tracks", "likes"):
                title = candidate[:200]
        date_iso, precision = "", ""
        t = card.find("time")
        if t:
            dt = (t.get("datetime") or "").strip()
            if dt:
                iso, prec = _parse_any_date_to_iso(dt)
                if iso:
                    date_iso, precision = iso, (prec or "day")
        return title, date_iso, precision
    except Exception:
        return "", "", ""

def _sc_parse_profile(driver, profile_url: str, seed_primary_genre="") -> dict:
    """
    Visit artist profile and extract details similar to Bandcamp.
    Very defensive: SoundCloud markup changes often.
    """
    artist = {
        "artist_name": "",
        "profile_url": profile_url,
        "location": "",
        "website": "",
        "email": "",
        "socials": {k: "" for k in ["instagram", "twitter", "facebook", "youtube", "linktree", "spotify", "bandsintown", "songkick"]},
        "genres": [],
        "latest_release_title": "",
        "latest_release_date": "",
        "latest_release_precision": "",
        "sounds_like": "",
        "primary_genre": seed_primary_genre or "",
        "source_tag": ""
    }
    try:
        driver.get(profile_url)
        _sc_accept_consent_if_present(driver)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _sc_soft_scroll(driver)
        profile_html = driver.page_source
        soup = BeautifulSoup(profile_html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            anchor["href"] = _sc_unwrap_gate(anchor.get("href", ""))

        preferred_artist = _sc_preferred_artist_name_from_soup(soup)
        og_title = soup.select_one('meta[property="og:title"]')
        if preferred_artist:
            artist["artist_name"] = preferred_artist
        elif og_title and og_title.get("content"):
            artist["artist_name"] = og_title["content"].strip()
        if not artist["artist_name"]:
            h1 = soup.find(["h1", "h2"], attrs={"itemprop": re.compile("name", re.I)})
            if h1:
                artist["artist_name"] = h1.get_text(strip=True)

        loc = ""
        for el in soup.select("header [class*='location'], header [class*='small'], header [class*='subheader'], .profileHeader"):
            txt = el.get_text(" ", strip=True)
            if txt and 3 <= len(txt) <= 80 and re.search(r"[A-Za-z]", txt):
                loc = txt
                break
        artist["location"] = loc

        for a in soup.find_all("a", href=True):
            href = _sc_unwrap_gate(_sc_normalize_url(a["href"]))
            if not href:
                continue
            if href.startswith("mailto:"):
                artist["email"] = href.split("mailto:")[-1].split("?")[0]
                continue
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if (not host or
                "soundcloud.com" in host or
                host in _SC_JUNK_HOSTS or
                host in _SC_STORE_HOSTS or
                host in _SC_CONSENT_HOSTS):
                continue
            matched = None
            for dom, key in _SC_SOCIAL_DOMAINS.items():
                if dom in host:
                    matched = key
                    break
            if matched:
                if not artist["socials"].get(matched):
                    artist["socials"][matched] = href
            elif not artist["website"]:
                if parsed.scheme in ("http", "https") and len(host.split(".")) >= 2:
                    artist["website"] = href

        more_links = set()
        try:
            parsed_profile = urlparse(profile_url)
            handle = (parsed_profile.path or "/").strip("/").split("/")[0]
            if handle:
                links_url = f"https://soundcloud.com/{handle}/links"
                driver.get(links_url)
                WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                more_links = _sc_collect_profile_links(driver, timeout=6)
        except Exception:
            more_links = set()

        for raw in more_links:
            href = _sc_unwrap_gate(_sc_normalize_url(raw))
            if not href:
                continue
            if href.startswith("mailto:"):
                if not artist["email"]:
                    address = href.split("mailto:")[-1].split("?")[0]
                    if address and not address.lower().endswith("@soundcloud.com"):
                        artist["email"] = address
                continue
            parsed_href = urlparse(href)
            host = (parsed_href.netloc or "").lower()
            if (not host or
                "soundcloud.com" in host or
                host in _SC_JUNK_HOSTS or
                host in _SC_STORE_HOSTS or
                host in _SC_CONSENT_HOSTS):
                continue
            matched = None
            for dom, key in _SC_SOCIAL_DOMAINS.items():
                if dom in host:
                    matched = key
                    break
            if matched:
                if not artist["socials"].get(matched):
                    artist["socials"][matched] = href
            elif not artist["website"]:
                if parsed_href.scheme in ("http", "https") and len(host.split(".")) >= 2:
                    artist["website"] = href

        artist["genres"] = _sc_extract_tags(soup)
        if not artist["primary_genre"]:
            artist["primary_genre"] = (artist["genres"][0] if artist["genres"] else "")

        lt_title, lt_date, lt_prec = _sc_fetch_latest_track(driver, profile_url)
        if lt_title:
            artist["latest_release_title"] = lt_title
        if lt_date:
            artist["latest_release_date"] = lt_date
            artist["latest_release_precision"] = lt_prec
        else:
            artist["latest_release_date"] = artist.get("latest_release_date", "") or "not present"

        if not artist["latest_release_title"]:
            first_title = soup.find(["a", "div"], attrs={"title": True})
            if first_title:
                t = first_title.get("title", "").strip()
                if t and len(t) <= 120 and t.lower() != "home":
                    artist["latest_release_title"] = t

        text_blob = soup.get_text(" ", strip=True)
        if not artist["sounds_like"]:
            for pattern in _BC_SOUNDS_PATTERNS:
                match = re.search(pattern, text_blob, flags=re.IGNORECASE)
                if match and match.group(1):
                    tokens = _norm_tokens(match.group(1))
                    if tokens:
                        artist["sounds_like"] = ", ".join(t.title() for t in tokens[:5])
                        break

        return artist
    except Exception as exc:
        print(f"SoundCloud: profile parse failed {profile_url}: {exc}")
        return {}

def _sc_is_actionable(artist_dict: dict) -> bool:
    if not artist_dict:
        return False
    if artist_dict.get("website") or artist_dict.get("email"):
        return True
    socials = artist_dict.get("socials", {})
    return any(bool(v) for v in socials.values())

def _sc_write_enriched_csv(rows, existing_csv):
    columns = [
        "Artist Name", "Profile URL", "Website", "Email", "Instagram", "Twitter", "Facebook", "Linktree", "YouTube",
        "Location", "Genres", "Latest Release", "Latest Release Date", "Latest Release Precision", "Sounds Like", "Primary Genre", "Source Tag"
    ]
    base_dir = os.path.dirname(os.path.abspath(existing_csv))
    enriched_path = os.path.join(base_dir, "soundcloud_enriched.csv")
    existing_df = pd.DataFrame(columns=columns)
    if os.path.exists(enriched_path):
        try:
            existing_df = pd.read_csv(enriched_path)
        except Exception:
            existing_df = pd.DataFrame(columns=columns)
    for col in columns:
        if col not in existing_df.columns:
            existing_df[col] = ""
    if not existing_df.empty:
        existing_df = existing_df[columns]
    new_df = pd.DataFrame(rows)
    for col in columns:
        if col not in new_df.columns:
            new_df[col] = ""
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined["__dedupe_key"] = (
        combined["Artist Name"].fillna("").str.strip().str.lower()
        + "||" +
        combined["Profile URL"].fillna("").str.rstrip("/").str.lower()
    )
    combined = combined.drop_duplicates(subset="__dedupe_key").drop(columns="__dedupe_key")
    combined = combined[columns]
    combined.to_csv(enriched_path, index=False, encoding="utf-8-sig")

def scrape_soundcloud(website_url, seed_tags=None, pages_per_tag=SOUNDCLOUD_PAGES_PER_TAG,
                      existing_csv="artist_social_links.csv", max_artists=200,
                      max_handles=None, min_yield=3, dry_run=False):
    print("[init] SoundCloud scraper starting…")
    _SC_HANDLE_UID_MAP.clear()
    _SC_HANDLE_USEROBJ_MAP.clear()
    global _SC_RUN_STATS, _SC_ROOT_FORBIDDEN, _SC_ROOT_FORBIDDEN_LOGGED, _SC_ABOUT_DISABLED, _SC_ABOUT_DISABLE_LOGGED
    _SC_ROOT_FORBIDDEN = False
    _SC_ROOT_FORBIDDEN_LOGGED = False
    _SC_ABOUT_DISABLED = False
    _SC_ABOUT_DISABLE_LOGGED = False
    _SC_RUN_STATS = {
        "handles_total": 0,
        "actionable_written": 0,
        "about_attempts": 0,
        "about_challenges": 0,
        "about_disabled": 0,
        "root_403": 0,
        "tracks_api_403": 0,
        "api_user_fallback_used": 0,
        "rss_used": 0,
    }
    # Keep shared engine stats in sync for summary parity.
    _SC_ENGINE.reset_run_stats()
    try:
        import soundcloud_engine as sc_mod  # local import to avoid cycle at module load
        sc_mod._SC_RUN_STATS = _SC_RUN_STATS
    except Exception:
        pass
    driver = setup_driver()
    try:
        discovery_session = build_hardened_session()
    except Exception as exc:
        print(f"SoundCloud: failed to build hardened session ({exc}); falling back to basic session.")
        discovery_session = requests.Session()
        discovery_session.headers.update(_rand_headers())
    candidate_profiles = []
    seen_profiles = set()
    sc_rows = []
    enriched_rows = []
    actionable_count = 0
    ACTIONABLE_LIMIT = max_artists
    fast = bool(SOUNDCLOUD_FAST_FACEBOOK_EMAIL_ONLY)
    place_filter = ""
    try:
        url = (website_url or "").strip()
        url_lower = url.lower()
        use_people_url = _sc_is_people_search_url(url)
        use_profile_url = (
            url_lower.startswith("https://soundcloud.com/")
            and "/search/people" not in url_lower
        )
        print(f"SoundCloud: source_url = {url or '(none)'}")

        handles_with_tags = []

        if use_people_url:
            people_params = _sc_parse_people_search(url)
            query = people_params.get("q", "")
            place = people_params.get("place", "")
            param_parts = []
            if query:
                try:
                    encoded_query = _urlparse.quote(query)
                except Exception:
                    encoded_query = query
                param_parts.append(f"q={encoded_query}")
            if place:
                try:
                    encoded_place = _urlparse.quote(place)
                except Exception:
                    encoded_place = place
                param_parts.append(f"filter.place={encoded_place}")
            if param_parts:
                people_url = f"https://soundcloud.com/search/people?{'&'.join(param_parts)}"
            else:
                people_url = url
            search_cap = max_handles or max_artists
            handles = []
            try:
                handles = _SC_ENGINE.people_search(query=query, place=place, max_results=search_cap or 50)
            except Exception as exc:
                print(f"SoundCloud: people search API fetch failed: {exc}")
            if search_cap:
                handles = handles[:search_cap]
            tag_hint = (query or "").strip() or (place or "").strip()
            place_filter = (place or "")
            print(f"SoundCloud: people search (API) -> {len(handles)} handles (query='{query}' place='{place}')")
            handles_with_tags.extend((h, tag_hint) for h in handles)

        elif use_profile_url:
            parsed = urlparse(url)
            path = (parsed.path or "").strip("/")
            if path and "/" not in path:
                handle = path
                if _sc_handle_ok(handle):
                    print(f"SoundCloud: single profile mode -> {url}")
                    handles_with_tags.append((handle, ""))
                else:
                    print(f"SoundCloud: provided profile URL has an invalid handle: {handle}")
            else:
                print("SoundCloud: provided URL is not a single profile; skipping.")

        else:
            tags = [ (t or "").strip() for t in (seed_tags or []) if (t or "").strip() ]
            if not url and not tags:
                print("SoundCloud: no usable URL or tags provided; nothing to do.")
            elif tags:
                print(f"SoundCloud: fallback to tags (last resort): {tags}")
                for tag in tags:
                    normalized_tag = tag.lower()
                    normalized_tag = re.sub(r"\s+", "-", normalized_tag).strip("-") or normalized_tag
                    try:
                        tag_path = _urlparse.quote(normalized_tag)
                    except Exception:
                        tag_path = normalized_tag
                    tag_url = f"https://soundcloud.com/tags/{tag_path}"
                    try:
                        tag_handles = discover_handles(discovery_session, tag_url)
                    except Exception as exc:
                        print(f"SoundCloud: tag '{tag}' page fetch failed: {exc}")
                        tag_handles = []
                    if tag_handles:
                        print(f"SoundCloud: tag '{tag}' (/tags) -> {len(tag_handles)} handles")
                        handles_with_tags.extend((h, tag) for h in tag_handles)
                    if len(handles_with_tags) >= max_artists:
                        break
                    try:
                        encoded = _urlparse.quote(tag)
                    except Exception:
                        encoded = tag
                    people_url = f"https://soundcloud.com/search/people?q={encoded}"
                    search_cap = max_handles or max_artists
                    handles = []
                    try:
                        handles = discover_handles(discovery_session, people_url, limit=search_cap)
                    except Exception as exc:
                        print(f"SoundCloud: tag '{tag}' people search fetch failed: {exc}")
                    if search_cap:
                        handles = handles[:search_cap]
                    if handles:
                        print(f"SoundCloud: tag '{tag}' (search) -> {len(handles)} handles")
                        handles_with_tags.extend((h, tag) for h in handles)
                    if len(handles_with_tags) >= max_artists:
                        break
            else:
                print("SoundCloud: provided URL is not SoundCloud; no fallback tags, aborting.")

        dedup_handles = []
        seen_handles = set()
        for handle, source_tag in handles_with_tags:
            clean = (handle or "").strip()
            if not _sc_handle_ok(clean):
                continue
            if clean in seen_handles:
                continue
            seen_handles.add(clean)
            dedup_handles.append((clean, source_tag))

        if dry_run:
            dedup_handles = dedup_handles[:10]
            print(f"SoundCloud: dry-run mode limiting to {len(dedup_handles)} handles.")

        if max_handles and max_handles > 0:
            dedup_handles = dedup_handles[:max_handles]

        print(f"SoundCloud: total artist handles to visit {len(dedup_handles)}")
        _sc_stat_inc("handles_total", len(dedup_handles))
        if dedup_handles[:5]:
            print(f"SoundCloud: first 5 handles -> {[h for h, _ in dedup_handles[:5]]}")

        for handle, tag_value in dedup_handles:
            profile_url = f"https://soundcloud.com/{handle}"
            key = profile_url.rstrip("/").lower()
            if key in seen_profiles:
                continue
            seen_profiles.add(key)
            candidate_profiles.append((profile_url, tag_value or "", ""))

        if not candidate_profiles:
            print("SoundCloud: no candidate_profiles after provided input; check URL or filters.")

        if fast and len(candidate_profiles) > SOUNDCLOUD_FAST_MAX_CANDIDATES:
            candidate_profiles = candidate_profiles[:SOUNDCLOUD_FAST_MAX_CANDIDATES]

        print(f"SoundCloud: total artist profiles resolved {len(candidate_profiles)}")
        if not candidate_profiles:
            print("SoundCloud: no candidate_profiles; check tag or page selectors.")
        else:
            preview_handles = [
                (_sc_unpack3(c)[0].rstrip("/").split("/")[-1])
                for c in candidate_profiles[:5]
            ]
            print("SoundCloud: first 5 handles ->", preview_handles)
        weird_shapes = [c for c in candidate_profiles if not isinstance(c, (tuple, list)) or len(c) != 3]
        if weird_shapes:
            print(f"SoundCloud: normalized {len(weird_shapes)} non-3-tuples in candidates")
        if fast:
            handle_jobs = []
            for cand in candidate_profiles:
                profile_url, source_tag, seed_primary_genre = _sc_unpack3(cand)
                if not profile_url:
                    continue
                handle = _sc_handle_from_profile(profile_url)
                if not handle:
                    continue
                handle_jobs.append({
                    "handle": handle,
                    "profile_url": profile_url,
                    "source_tag": source_tag,
                    "seed_primary_genre": seed_primary_genre,
                })
            contact_map, processed_jobs, batch_hits = _sc_collect_contact_links(handle_jobs, min_yield or 0)
            min_yield_msg = None
            if (min_yield or 0) and batch_hits < (min_yield or 0):
                min_yield_msg = f"SoundCloud: last batch produced {batch_hits} contacts (< {min_yield}). Consider adjusting your query."
            for idx, job in enumerate(processed_jobs):
                profile_url = job["profile_url"]
                source_tag = job["source_tag"]
                seed_primary_genre = job["seed_primary_genre"]
                handle = job["handle"]
                if not profile_url:
                    continue
                if not _sc_is_valid_handle(handle):
                    print(f"skip[{idx}] invalid handle: {profile_url}")
                    continue
                contact_payload = contact_map.get(handle, {})
                contact_data = contact_payload.get("data")
                if not _sc_has_contact_links(contact_payload):
                    print(f"skip[{idx}] no links/email/site: {handle}")
                    continue
                contact_song_title = ""
                contact_release_date = ""
                contact_tags = []
                contact_sounds_like = ""
                if isinstance(contact_data, dict):
                    contact_song_title = (contact_data.get("latest_track_title") or "").strip()
                    contact_release_date = (contact_data.get("latest_track_release_date") or "").strip()
                    tags_candidate = contact_data.get("latest_track_tags") or []
                    if isinstance(tags_candidate, (list, tuple)):
                        contact_tags = [tag for tag in tags_candidate if isinstance(tag, str)]
                    contact_sounds_like = (contact_data.get("sounds_like") or "").strip()
                    if not contact_sounds_like:
                        contact_sounds_like = _sc_sounds_like_from_bio(contact_data.get("bio_text", ""))
                location_text, bio_text, profile_soup = _sc_profile_basics(driver, profile_url, timeout=10)
                title, date_iso, prec, genres = _sc_quick_first_track_meta(driver, profile_url, timeout=12, hop=True)
                soup_name = profile_soup
                if soup_name is None:
                    try:
                        driver.get(profile_url)
                        WebDriverWait(driver, SOUNDCLOUD_FAST_TIMEOUT_SEC).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                        soup_name = BeautifulSoup(driver.page_source, "html.parser")
                    except Exception:
                        soup_name = None
                fallback_name = handle.replace("-", " ").replace("_", " ").title()
                if soup_name:
                    try:
                        og = soup_name.select_one('meta[property="og:title"]')
                        if og and og.get("content"):
                            candidate_name = og["content"].strip()
                            if candidate_name:
                                fallback_name = candidate_name[:200]
                    except Exception:
                        pass
                sounds_like_value = _sc_sounds_like_from_bio(bio_text)
                try:
                    meta = _sc_extract_profile_meta(driver, soup_override=soup_name)
                except Exception:
                    meta = {
                        "artist_name": "",
                        "location": "",
                        "song_title": "",
                        "primary_genre": "",
                        "release_date": "not present",
                        "sounds_like": ""
                    }
                fallback_name = (meta.get("artist_name") or fallback_name or "").strip()
                fallback_location = meta.get("location") or location_text or ""
                song_title_value = (meta.get("song_title") or contact_song_title or title or "").strip()
                if meta.get("sounds_like"):
                    sounds_like_value = meta["sounds_like"]
                elif contact_sounds_like:
                    sounds_like_value = contact_sounds_like
                meta_release = meta.get("release_date") or ""
                release_date_value = meta_release if meta_release and meta_release.lower() != "not present" else (contact_release_date or date_iso or "")
                combined_tags = list(genres or [])
                if contact_tags:
                    combined_tags.extend(contact_tags)
                row_payload = contact_data or {}
                if fallback_name and isinstance(row_payload, dict):
                    row_payload = dict(row_payload)
                    row_payload["display_name"] = fallback_name
                row, external_urls, emails = _sc_build_row(
                    handle=handle,
                    payload=row_payload,
                    soundcloud_link=profile_url,
                    fallback_name=fallback_name,
                    fallback_location=fallback_location,
                    song_title=song_title_value,
                    release_date=release_date_value,
                    sounds_like=sounds_like_value,
                    fallback_tags=combined_tags,
                    fallback_external=list((contact_data or {}).get("external_urls") or []),
                    fallback_emails=list((contact_data or {}).get("emails") or []),
                )
                _sc_log_csv_row(handle, row, external_urls, emails)
                if dry_run:
                    _sc_print_dry_run_row(handle, row, external_urls, emails)
                    continue
                sc_rows.append(row)
                actionable_count += 1
                _sc_stat_inc("actionable_written")
                if actionable_count >= ACTIONABLE_LIMIT:
                    break
                time.sleep(random.uniform(0.2, 0.6))
            if dry_run:
                print(f"SoundCloud: dry-run complete – {batch_hits}/{len(processed_jobs)} handles yielded outbound links.")
            if min_yield_msg:
                print(min_yield_msg)
        else:
            for idx, cand in enumerate(candidate_profiles):
                profile_url, source_tag, seed_primary_genre = _sc_unpack3(cand)
                if not profile_url:
                    continue
                handle = _sc_handle_from_profile(profile_url)
                if not _sc_is_valid_handle(handle):
                    print(f"skip[{idx}] invalid handle: {profile_url}")
                    continue
                contact_payload = _sc_fetch_contact_payload(handle)
                contact_data = contact_payload.get("data")
                if not _sc_has_contact_links(contact_payload):
                    print(f"skip[{idx}] no links/email/site: {handle}")
                    continue
                contact_song_title = ""
                contact_release_date = ""
                contact_tags = []
                contact_sounds_like = ""
                if isinstance(contact_data, dict):
                    contact_song_title = (contact_data.get("latest_track_title") or "").strip()
                    contact_release_date = (contact_data.get("latest_track_release_date") or "").strip()
                    tags_candidate = contact_data.get("latest_track_tags") or []
                    if isinstance(tags_candidate, (list, tuple)):
                        contact_tags = [tag for tag in tags_candidate if isinstance(tag, str)]
                    contact_sounds_like = (contact_data.get("sounds_like") or "").strip()
                    if not contact_sounds_like:
                        contact_sounds_like = _sc_sounds_like_from_bio(contact_data.get("bio_text", ""))
                location_text, bio_text, profile_soup = _sc_profile_basics(driver, profile_url, timeout=10)
                title, date_iso, prec, genres = _sc_quick_first_track_meta(driver, profile_url, timeout=12, hop=True)
                soup_name = profile_soup
                if soup_name is None:
                    try:
                        driver.get(profile_url)
                        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                        soup_name = BeautifulSoup(driver.page_source, "html.parser")
                    except Exception:
                        soup_name = None
                fallback_name = handle.replace("-", " ").replace("_", " ").title()
                if soup_name:
                    try:
                        og = soup_name.select_one('meta[property="og:title"]')
                        if og and og.get("content"):
                            candidate_name = og["content"].strip()
                            if candidate_name:
                                fallback_name = candidate_name[:200]
                    except Exception:
                        pass
                sounds_like_value = _sc_sounds_like_from_bio(bio_text)
                try:
                    meta = _sc_extract_profile_meta(driver, soup_override=soup_name)
                except Exception:
                    meta = {
                        "artist_name": "",
                        "location": "",
                        "song_title": "",
                        "primary_genre": "",
                        "release_date": "not present",
                        "sounds_like": ""
                    }
                fallback_name = (meta.get("artist_name") or fallback_name or "").strip()
                fallback_location = meta.get("location") or location_text or ""
                song_title_value = (meta.get("song_title") or contact_song_title or title or "").strip()
                if meta.get("sounds_like"):
                    sounds_like_value = meta["sounds_like"]
                elif contact_sounds_like:
                    sounds_like_value = contact_sounds_like
                meta_release = meta.get("release_date") or ""
                release_date_value = meta_release if meta_release and meta_release.lower() != "not present" else (contact_release_date or date_iso or "")
                combined_tags = list(genres or [])
                if contact_tags:
                    combined_tags.extend(contact_tags)
                row_payload = contact_data or {}
                if fallback_name and isinstance(row_payload, dict):
                    row_payload = dict(row_payload)
                    row_payload["display_name"] = fallback_name
                row, external_urls, emails = _sc_build_row(
                    handle=handle,
                    payload=row_payload,
                    soundcloud_link=profile_url,
                    fallback_name=fallback_name,
                    fallback_location=fallback_location,
                    song_title=song_title_value,
                    release_date=release_date_value,
                    sounds_like=sounds_like_value,
                    fallback_tags=combined_tags,
                    fallback_external=list((contact_data or {}).get("external_urls") or []),
                    fallback_emails=list((contact_data or {}).get("emails") or []),
                )
                _sc_log_csv_row(handle, row, external_urls, emails)
                sc_rows.append(row)
                enriched_rows.append({
                    "Artist Name": fallback_name,
                    "Profile URL": profile_url,
                    "Website": row_payload.get("website", ""),
                    "Email": ", ".join(emails),
                    "Instagram": "",
                    "Twitter": "",
                    "Facebook": "",
                    "Linktree": "",
                    "YouTube": "",
                    "Location": fallback_location,
                    "Genres": "; ".join(combined_tags),
                    "Latest Release": song_title_value,
                    "Latest Release Date": release_date_value,
                    "Latest Release Precision": prec,
                    "Sounds Like": sounds_like_value,
                    "Primary Genre": row.get("Primary Genre", ""),
                    "Source Tag": source_tag
                })
                actionable_count += 1
                _sc_stat_inc("actionable_written")
                if actionable_count >= ACTIONABLE_LIMIT:
                    break
                time.sleep(random.uniform(0.4, 0.9))
        print(f"SoundCloud: total actionable artists written {actionable_count}")
        stats = _SC_ENGINE.run_stats or {}
        print(
            "SoundCloud summary: "
            f"handles={stats.get('handles_total', 0)} "
            f"actionable={stats.get('actionable_written', 0)} "
            f"about_attempts={stats.get('about_attempts', 0)} "
            f"about_challenges={stats.get('about_challenges', 0)} "
            f"about_disabled={1 if _SC_ABOUT_DISABLED else 0} "
            f"root_403={stats.get('root_403', 0)} "
            f"tracks_api_401={stats.get('tracks_api_401', 0)} "
            f"tracks_api_403={stats.get('tracks_api_403', 0)} "
            f"tracks_api_blocked={stats.get('tracks_api_blocked', 0)} "
            f"rss_used={stats.get('rss_used', 0)} "
            f"api_user_fallback_used={stats.get('api_user_fallback_used', 0)}"
        )
    finally:
        try:
            discovery_session.close()
        except Exception:
            pass
        driver.quit()

    if dry_run:
        print("SoundCloud: dry-run requested; skipping CSV write.")
        return

    save_soundcloud_csv(sc_rows, existing_csv)
    if enriched_rows:
        _sc_write_enriched_csv(enriched_rows, existing_csv)
# =============================================================================
# Facebook Scraping Functions (Page 2)
# =============================================================================
FACEBOOK_CLOSE_EXTRA_WINDOWS = True

def close_unexpected_windows(driver, main_window_handle, logger=None):
    """
    Close any Selenium windows/tabs that are not the main scraping window.

    Always switches focus back to the main window at the end.
    Safe to call repeatedly during the scrape.
    """
    def _log(level: str, message: str):
        if not logger:
            return
        try:
            if hasattr(logger, level):
                getattr(logger, level)(message)
            else:
                logger(message)
        except Exception:
            pass

    try:
        handles = driver.window_handles
    except Exception as e:
        _log("warning", f"Could not read window handles: {e}")
        return

    if not main_window_handle:
        _log("warning", "Main window handle not available; skipping unexpected window cleanup.")
        return
    if main_window_handle not in handles:
        _log("warning", "Main window handle missing from current handles; skipping unexpected window cleanup.")
        return

    for handle in handles:
        if handle == main_window_handle:
            continue
        try:
            driver.switch_to.window(handle)
            current_url = ""
            try:
                current_url = driver.current_url
            except Exception:
                current_url = ""
            if "meta-pay" in (current_url or "").lower() or "payments" in (current_url or "").lower():
                _log("info", f"Detected Meta Pay popup window, closing: {current_url}")
            else:
                _log("info", f"Closing unexpected window {handle} (url={current_url})")
            driver.close()
        except Exception as e:
            _log("warning", f"Error closing unexpected window {handle}: {e}")

    try:
        if main_window_handle in driver.window_handles:
            driver.switch_to.window(main_window_handle)
    except Exception as e:
        _log("warning", f"Could not switch back to main window: {e}")

def fb_extract_emails_from_html(html: str) -> list[str]:
    """
    Given Facebook page HTML, return a de-duplicated list of email addresses.
    Reuses the existing Bandcamp email regex for consistency.
    """
    emails = set()
    soup = BeautifulSoup(html or "", "html.parser")
    email_re = _BC_EMAIL_RE
    for text_node in soup.stripped_strings:
        for match in email_re.findall(text_node):
            emails.add(match.strip())
    return sorted(emails)


def fb_scrape_emails_from_page(
    driver,
    page_url: str,
    log_fn=None,
    log_prefix: str = "[FB Enrich]",
    suppress_console: bool = False,
) -> list[str]:
    """
    Open a Facebook page in Selenium and return any emails found.
    """
    def _log(msg):
        prefix = log_prefix or "[FB Enrich]"
        if msg and "[FB Enrich]" in msg:
            msg = msg.replace("[FB Enrich]", prefix)
        elif msg and not msg.startswith("["):
            msg = f"{prefix} {msg}"
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass
        if suppress_console:
            return
        print(msg)

    if not page_url:
        return []

    try:
        _log(f"[FB Enrich] Scraping Facebook page: {page_url}")
        driver.get(page_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        html = driver.page_source or ""
        emails = fb_extract_emails_from_html(html)
        if emails:
            _log(f"[FB Enrich] Found {len(emails)} email(s) on {page_url}")
        else:
            _log(f"[FB Enrich] No emails found on {page_url}")
        return emails
    except Exception as exc:
        _log(f"[FB Enrich] Error scraping {page_url}: {exc}")
        return []

def _fb_is_real_page_url(url: str) -> bool:
    """
    Return True if this looks like a Facebook page/profile URL, and False for search/login/help/etc.
    """
    if not url:
        return False
    url = url.strip()
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    if "facebook.com" not in host:
        return False

    if "/search/" in path:
        return False
    if "/login" in path or "/recover" in path or "/help" in path:
        return False

    if "/pages/" in path or "/people/" in path:
        return True

    if path.count("/") == 1 and len(path) > 1:
        return True

    return False


def _legacy_sanitize_fb_category_text(cat: Optional[str]) -> Optional[str]:
    """
    Mirror night_mode_fb._sanitize_fb_category_text to drop noisy FB category strings.
    """
    if not cat:
        return None
    try:
        cleaned = str(cat).strip()
    except Exception:
        cleaned = ""
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if len(cleaned) > 80:
        return None
    if lowered.startswith("reminder"):
        return None
    if lowered.startswith("you have an event"):
        return None
    return cleaned


def _resolve_fb_page_category(scraped_cat: Optional[str], candidate_cat: Optional[str]) -> tuple[Optional[str], str]:
    """
    Clean + sanitize scraped FB category text and decide the category used for gating/logging.
    Returns (sanitized_scraped, chosen_category_for_gate_and_logs).
    """
    from facebook_enrich import clean_fb_category_text

    cleaned_scraped = clean_fb_category_text(scraped_cat) if scraped_cat else None
    sanitized_scraped = _legacy_sanitize_fb_category_text(cleaned_scraped)
    final_category = sanitized_scraped or (candidate_cat or "")
    return sanitized_scraped, final_category


def fb_find_page_and_emails_by_name(
    driver,
    artist_name: str,
    location: str = "",
    log_fn=None,
    log_prefix: str = "[FB Enrich]",
    suppress_console: bool = False,
    allow_soft_pass_category: bool = False,
    soft_pass_category_allowlist: Optional[List[str]] = None,
) -> tuple[str, list[str]]:
    """
    Use Facebook search to locate a page for an artist and scrape emails.
    Returns (page_url, emails). If nothing found, returns ("", []).
    """
    FB_CATEGORY_BOOSTS = {
        "musician/band": 2.5,
        "artist": 2.2,
        "band": 2.2,
        "singer": 2.0,
        "music": 2.0,
        "record label": 1.8,
        "entertainer": 1.5,
        "public figure": 1.0,
    }
    MUSIC_STRONG = [
        "musician/band", "musician", "band",
        "artist", "music", "singer", "singer-songwriter",
        "rapper", "dj", "producer", "recording studio",
        "music production studio", "music producer",
        "songwriter", "performing artist", "public figure",
        "entertainer",
    ]
    MUSIC_MEDIUM = [
        "record label", "entertainment website",
        "media", "radio station", "podcast",
        "music video", "music award", "festival",
    ]
    NON_MUSIC_CORPORATE = [
        "spa", "care spa", "health spa", "resort",
        "hotel", "boutique", "clothing", "store",
        "shop", "vintage", "retro", "restaurant",
        "bar", "cafe", "coffee shop",
        "real estate", "estate agent", "construction",
        "company", "ltd", "llc", "inc",
        "university", "college", "school",
        "church", "temple", "mosque",
        "government", "politician", "political",
    ]

    def compute_fb_category_boost(category_norm: str | None) -> float:
        """
        Favor musician/artist categories; penalize obvious non-music businesses.
        """
        if not category_norm:
            return 0.0
        cat = category_norm.strip().lower()
        if not cat:
            return 0.0
        if cat in FB_CATEGORY_BOOSTS:
            return FB_CATEGORY_BOOSTS[cat]
        if any(token in cat for token in MUSIC_STRONG):
            return 2.5
        if any(token in cat for token in MUSIC_MEDIUM):
            return 1.5
        if any(token in cat for token in NON_MUSIC_CORPORATE):
            return -3.0
        return 0.0

    def _category_is_music_like(category: str) -> bool:
        allowlist = soft_pass_category_allowlist or [
            "musician",
            "musician/band",
            "band",
            "artist",
            "singer",
            "producer",
            "dj",
            "music",
        ]
        cat = (category or "").strip().lower()
        return any(tok in cat for tok in allowlist)

    def extract_fb_category(result_element, page_name: str = "") -> tuple[str | None, str | None]:
        """
        Best-effort category extraction from a single search result element.
        Returns (raw, norm).
        """
        if result_element is None:
            return None, None
        seen = set()
        name_norm = normalize_name(page_name)

        def _clean(text: str) -> str:
            cleaned = re.sub(r"\s+", " ", text or "").strip()
            return cleaned

        candidates = []
        try:
            for node in result_element.stripped_strings:
                val = _clean(node)
                if not val or val.lower() == (page_name or "").strip().lower():
                    continue
                if name_norm and normalize_name(val) == name_norm:
                    continue
                if len(val) > 80:
                    continue
                if val in seen:
                    continue
                seen.add(val)
                candidates.append(val)
        except Exception:
            candidates = []

        for candidate in candidates:
            lower = candidate.lower()
            if "/" in candidate or "band" in lower or "music" in lower or "artist" in lower or "dj" in lower:
                return candidate, normalize_name(candidate)
            if len(candidate.split()) <= 6:
                return candidate, normalize_name(candidate)
        return (candidates[0], normalize_name(candidates[0])) if candidates else (None, None)

    def _log(msg):
        prefix = log_prefix or "[FB Enrich]"
        if msg and "[FB Enrich]" in msg:
            msg = msg.replace("[FB Enrich]", prefix)
        elif msg and not msg.startswith("["):
            msg = f"{prefix} {msg}"
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass
        if suppress_console:
            return
        print(msg)

    artist_name = (artist_name or "").strip()
    if not artist_name:
        return "", []

    query_value = artist_name
    if location:
        query_value = f"{artist_name} {location}".strip()
    query = quote_plus(query_value)
    search_url = f"https://www.facebook.com/search/pages/?q={query}"
    _log(f"[FB Enrich] Selenium FB search URL: {search_url}")

    try:
        driver.get(search_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except Exception as exc:
        _log(f"[FB Enrich] Facebook search failed for '{artist_name}': {exc}")
        return "", []

    html = driver.page_source or ""
    soup = BeautifulSoup(html, "html.parser")
    artist_norm = normalize_name(artist_name)
    strong_candidates: list[tuple[float, float, float, str, str, str, str, bool]] = []
    fallback_candidates: list[tuple[float, float, float, str, str, str, str, bool]] = []
    generic_candidates: list[tuple[float, float, float, str, str, str, str, bool]] = []
    fb_candidates: list[FbCandidate] = []
    dropped_business = 0
    blocked_tokens = [
        "games", "game",
        "creations",
        "store", "shop",
        "gallery", "galleria",
        "company", "co ", " co.", "corp", "inc", "ltd", "llc",
        "exterior", "exteriors",
        "boutique",
        "farm", "farms",
        "international",
    ]
    downrank_tokens = ["studio", "studios"]
    corporate_tokens = [
        "ltd", "pty", "pty ltd", "inc", "corp", "company", "co.",
        "store", "shop", "boutique", "market",
        "gallery", "galleria",
        "resort", "hotel", "hostel", "motel", "guest house", "guesthouse",
        "real estate", "realestate", "estate agent", "estateagency",
        "spa", "salon", "barber",
        "restaurant", "cafe", "cafè", "coffee shop", "coffeehouse", "pub", "bar",
        "farm", "farms",
        "beauty", "hair", "lash", "lashes", "makeup", "nails", "clinic", "boutique", "brand",
        "journalist", "agency", "market", "grocer", "butcher", "bakery", "store", "shop",
    ]
    music_tokens = [
        "musician",
        "musician/band",
        "artist",
        "recording artist",
        "musical artist",
        "musical group",
        "performing artist",
        "performer",
        "vocalist",
        "band",
        "music",
        "music group",
        "music producer",
        "singer",
        "singer-songwriter",
        "songwriter",
        "rapper",
        "dj",
        "producer",
        "rock band",
        "indie band",
        "pop band",
        "record label",
    ]
    press_tokens = [
        "news",
        "magazine",
        "press",
        "blog",
        "journal",
        "media",
        "publisher",
    ]
    music_category_tokens = [
        "musician",
        "band",
        "artist",
        "music",
        "singer",
        "songwriter",
        "record label",
        "musical artist",
        "music production",
        "recording studio",
        "music producer",
        "producer",
    ]
    producer_dj_keywords = [
        "producer",
        "music producer",
        "record producer",
        "dj",
        "dj/producer",
        "dj / producer",
    ]
    music_link_tokens = [
        "spotify.com",
        "open.spotify.com",
        "bandcamp.com",
        "soundcloud.com",
        "music.apple.com",
        "deezer.com",
        "tidal.com",
        "youtube.com",
        "youtu.be",
        "linktr.ee",
        "distrokid",
        "tunecore",
        "artist.to",
        "songwhip",
    ]
    music_text_tokens = [
        "single",
        "ep",
        "album",
        "track",
        "new song",
        "stream now",
        "listen now",
        "out now",
        "tour",
        "gig",
        "live show",
        "producer",
        "mixing",
        "mastering",
        "recording",
        "studio",
        "band",
        "musician",
        "songwriter",
    ]
    non_music_artist_tokens = [
        "makeup",
        "cosmetic",
        "hair",
        "nail",
        "lashes",
        "lash",
        "brow",
        "tattoo",
        "piercing",
        "barber",
        "beauty",
        "jewelry",
        "clinic",
        "dentist",
        "lawyer",
        "shop",
        "store",
    ]
    from facebook_enrich import (
        FbCandidate,
        clean_fb_category_text,
        is_noisy_fb_text_block,
        looks_like_music_fallback,
        score_fb_candidate,
        select_best_facebook_candidate,
        is_junk_fb_candidate_url,
        fb_reason_code_split,
        _fb_extract_candidates_from_search_dom,
    )

    def _is_corporate_page(name: str, url: str, category: str | None) -> bool:
        text = f"{name} {url} {category or ''}".lower()
        return any(tok in text for tok in corporate_tokens)

    def _has_press_token(name: str, url: str, category: str | None) -> bool:
        text = f"{name} {url} {category or ''}".lower()
        return any(tok in text for tok in press_tokens)

    def _has_music_category(category: str | None) -> bool:
        if not category:
            return False
        cat = category.lower()
        return any(tok in cat for tok in music_category_tokens)

    def _has_music_signals(page_text: str, outbound_links: list[str], category: str | None, page_html: str | None = None) -> bool:
        combined_text = " ".join(part for part in (category or "", page_text or "") if part).lower()
        if any(tok in combined_text for tok in ("artist", "musician")) and not any(bad in combined_text for bad in non_music_artist_tokens):
            return True
        if any(tok in combined_text for tok in producer_dj_keywords):
            return True
        html_lc = (page_html or "").lower()
        if html_lc and any(tok in html_lc for tok in ("artist", "musician")) and not any(bad in html_lc for bad in non_music_artist_tokens):
            return True
        if html_lc and any(tok in html_lc for tok in producer_dj_keywords):
            return True
        text = (page_text or "").lower()
        for link in outbound_links or []:
            l = (link or "").lower()
            if any(tok in l for tok in music_link_tokens):
                return True
        return any(tok in text for tok in music_text_tokens + producer_dj_keywords)

    def _is_music_page(name_lc: str, url_lc: str, category_lc: str) -> bool:
        if _is_corporate_page(name_lc, url_lc, category_lc):
            return False
        if _has_music_category(category_lc):
            return True
        for blob in (name_lc, url_lc):
            for token in music_tokens:
                if token in blob:
                    return True
        return False

    def _is_music_page_final(
        name: str, url: str, category: str | None, page_text: str, outbound_links: list[str], page_html: str | None
    ) -> bool:
        if _is_corporate_page(name, url, category):
            return False
        if _has_press_token(name, url, category) and not _has_music_category(category):
            return False
        if not _has_music_category(category):
            return False
        if not _has_music_signals(page_text, outbound_links, category, page_html):
            return False
        return True
    strong_cat_tokens = ("musician", "band", "artist", "singer", "songwriter", "music", "recording artist")
    dom_candidates = _fb_extract_candidates_from_search_dom(
        html,
        logger=_log,
        debug=os.getenv("FB_DEBUG_DOM_GATE") == "1",
        search_name=artist_name,
    )
    for cand in dom_candidates:
        href = (cand.url or "").strip()
        if not href:
            continue
        text = (cand.name or "").strip()
        category_raw = (getattr(cand, "category", "") or "").strip()
        aria_label = getattr(cand, "aria_label", "") or ""
        try:
            parsed = urlparse(href)
            username = (parsed.path or "").strip("/").split("/")[0] if parsed.path else ""
        except Exception:
            username = ""
        if not category_raw and aria_label and any(tok in aria_label.lower() for tok in music_tokens):
            category_raw = aria_label
        if not category_raw and text and any(tok in text.lower() for tok in music_tokens):
            category_raw = text
        category_norm = normalize_name(category_raw)
        fallback_name = text or username or href
        name_lc = (fallback_name or "").lower()
        url_lc = (href or "").lower()
        category_lc = (category_raw or "").lower()
        corp_hit = False
        for token in corporate_tokens:
            if token in name_lc:
                _log(f"[FB Enrich] Rejecting FB candidate '{text or href}' for '{artist_name}' due to corporate token '{token}' in name.")
                corp_hit = True
                break
            if token in url_lc:
                _log(f"[FB Enrich] Rejecting FB candidate '{text or href}' for '{artist_name}' due to corporate token '{token}' in url.")
                corp_hit = True
                break
            if token in category_lc:
                _log(f"[FB Enrich] Rejecting FB candidate '{text or href}' for '{artist_name}' due to corporate token '{token}' in category.")
                corp_hit = True
                break
        if corp_hit:
            continue
        fb_candidates.append(FbCandidate(name=fallback_name or href, url=href, category=category_raw or ""))
        page_name_norm = normalize_name(text)
        username_norm = normalize_name(username)
        score = 0.0
        if page_name_norm == artist_norm:
            score += 1.0
        elif page_name_norm.startswith(artist_norm):
            score += 0.7
        elif artist_norm in page_name_norm:
            score += 0.4
        if username_norm == artist_norm:
            score += 1.0
        elif username_norm.startswith(artist_norm):
            score += 0.7
        name_score = score
        music_flag = _is_music_page(name_lc, url_lc, category_lc)
        if not music_flag and normalize_name(text) == artist_norm:
            music_flag = True
        if not music_flag and normalize_name(username) == artist_norm:
            music_flag = True
        category_has_strong = any(tok in category_lc for tok in strong_cat_tokens)
        context = " ".join([
            (href or "").lower(),
            username_norm,
            (text or "").lower()
        ])
        corporate_hit = False
        for token in blocked_tokens:
            if token in context and token not in artist_norm:
                corporate_hit = True
                score = 0.0
                _log(f"[FB Enrich] Rejecting FB candidate '{text or href}' for '{artist_name}' due to corporate token '{token}' in URL/name.")
                break
        if not corporate_hit:
            for token in downrank_tokens:
                if token in context and token not in artist_norm:
                    score = max(score - 0.2, 0.0)
                    break
        base_score = score
        cat_boost = 0.0
        if not corporate_hit and score > 0:
            cat_boost = compute_fb_category_boost(category_norm)
            score += cat_boost
        music_cat_bonus = 0.5 if any(tok in category_lc for tok in music_tokens) else 0.0
        score_with_bonus = max(score + music_cat_bonus, 0.01)
        entry = (
            score_with_bonus,
            base_score,
            cat_boost + music_cat_bonus,
            href,
            fallback_name or href,
            category_raw,
            category_norm,
            True,
        )
        if music_flag and category_has_strong:
            strong_candidates.append(entry)
        elif music_flag or (
            not category_has_strong
            and "profile.php" not in url_lc
            and (
                name_score >= 0.1
                or (artist_norm and artist_norm.split() and artist_norm.split()[0] in name_lc)
                or (artist_norm and artist_norm.split()[0] in url_lc)
            )
        ):
            fallback_candidates.append(entry)
        else:
            generic_candidates.append(
                (
                    max(score_with_bonus, 1.0),
                    base_score,
                    cat_boost + music_cat_bonus,
                    href,
                    fallback_name or href,
                    category_raw,
                    category_norm,
                    True,
                )
            )

    using_fallback = False
    using_generic = False
    shared_best = None
    try:
        shared_best = select_best_facebook_candidate(fb_candidates, artist_name, logger=_log)
    except Exception:
        shared_best = None
    if shared_best:
        best_url = shared_best.url
        best_name = shared_best.name or shared_best.url
        best_category_raw = getattr(shared_best, "category", "") or ""
        best_category_norm = normalize_name(best_category_raw)
        best_score = 1.0
        best_base_score = 1.0
        best_cat_boost = 0.0
        try:
            scored = score_fb_candidate(artist_name, best_name or "", best_url or "", best_category_raw)
            if scored:
                best_score, best_base_score, best_cat_boost = scored
        except Exception:
            pass
        best_is_music = True
    elif strong_candidates:
        best_score, best_base_score, best_cat_boost, best_url, best_name, best_category_raw, best_category_norm, best_is_music = max(
            strong_candidates, key=lambda x: x[0]
        )
    elif fallback_candidates:
        best_score, best_base_score, best_cat_boost, best_url, best_name, best_category_raw, best_category_norm, best_is_music = max(
            fallback_candidates, key=lambda x: x[0]
        )
        using_fallback = True
    elif generic_candidates:
        best_score, best_base_score, best_cat_boost, best_url, best_name, best_category_raw, best_category_norm, best_is_music = max(
            generic_candidates, key=lambda x: x[0]
        )
        using_generic = True
    else:
        if dropped_business:
            _log(f"[FB Enrich] No non-junk FB candidates for '{artist_name}' after dropping {dropped_business} junk business UI hits.")
        slug = normalize_name(artist_name).replace(" ", "")
        if slug and len(slug) >= 4:
            fallback_url = f"https://www.facebook.com/{quote(slug)}"
            best_score = 1.0
            best_base_score = 1.0
            best_cat_boost = 0.0
            best_url = fallback_url
            best_name = artist_name
            best_category_raw = "artist"
            best_category_norm = "artist"
            best_is_music = True
            using_fallback = True
            _log(f"[FB Enrich] No FB search candidates for '{artist_name}'; trying slug fallback '{fallback_url}'.")
        else:
            _log(f"[FB Enrich] No Facebook page candidates for '{artist_name}'.")
            return "", []

    if using_generic:
        _log(
            f"[FB Enrich] Trying very loose FB candidate '{best_name or best_url}' for '{artist_name}' (category='{best_category_raw or '<none>'}', base_score={best_base_score:.2f})."
        )
    elif using_fallback:
        _log(
            f"[FB Enrich] Trying uncertain music FB candidate '{best_name or best_url}' for '{artist_name}' (category='{best_category_raw or '<none>'}', base_score={best_base_score:.2f})."
        )

    cat_display = best_category_raw or "<none>"

    # Final validation: scrape page to ensure music signals are present.
    page_music = False
    try:
        driver.get(best_url)
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        page_html = driver.page_source or ""
        page_category_text = None
        page_text_blocks = []
        outbound_links = []
        raw_html_lc = (page_html or "").lower()
        try:
            soup = BeautifulSoup(page_html, "html.parser")
            seen_blocks = set()

            def _add_block(val: str):
                val = (val or "").strip()
                if not val or len(val) > 160:
                    return None
                if is_noisy_fb_text_block(val):
                    return None
                if val in seen_blocks:
                    return None
                seen_blocks.add(val)
                page_text_blocks.append(val)
                return val

            meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
            if meta_desc:
                candidate = _add_block(meta_desc.get("content") or "")
                if candidate and not page_category_text:
                    page_category_text = candidate
            if not page_category_text:
                for tag in soup.find_all(["span", "div"]):
                    val = _add_block(tag.get_text(" ", strip=True))
                    if not val:
                        continue
                    low = val.lower()
                    if "/" in val or any(tok in low for tok in music_tokens):
                        page_category_text = val
                        break
            if not page_category_text:
                og_title = soup.find("meta", attrs={"property": "og:title"})
                if og_title:
                    page_category_text = _add_block(og_title.get("content") or "")
            raw_page_category = page_category_text
            page_category_text, cat_for_gate = _resolve_fb_page_category(raw_page_category, best_category_raw)
            if raw_page_category and page_category_text is None:
                _log(f"[FB Enrich][CategorySanitize] dropped noisy scraped category raw={raw_page_category!r}")
            if not page_category_text and raw_html_lc and "artist" in raw_html_lc:
                if not any(bad in raw_html_lc for bad in non_music_artist_tokens):
                    page_category_text = "Artist"
            # Outbound links
            for tag in soup.find_all("a", href=True):
                href_val = (tag.get("href") or "").strip()
                if href_val.startswith("http"):
                    outbound_links.append(href_val)
            # Quick body scan for music tokens if no category.
            body_music = False
            cat_lc = (page_category_text or "").lower()
            # Reject if the scraped category looks corporate.
            for token in corporate_tokens:
                if token in cat_lc:
                    _log(f"[FB Enrich] Rejecting FB page '{best_url}' for '{artist_name}' after scrape due to corporate token '{token}' in category.")
                    return "", []
            page_text_combined = " ".join(page_text_blocks)
            if _is_corporate_page((best_name or ""), (best_url or ""), page_category_text):
                _log(f"[FB Enrich] Rejecting FB page '{best_url}' for '{artist_name}' after scrape due to corporate markers.")
                return "", []
            if _has_press_token((best_name or ""), (best_url or ""), page_category_text) and not _has_music_category(page_category_text):
                _log(f"[FB Enrich] Rejecting FB page '{best_url}' for '{artist_name}' after scrape due to press/media markers.")
                return "", []
            page_music = _is_music_page_final(
                (best_name or ""),
                (best_url or ""),
                cat_for_gate,
                page_text_combined,
                outbound_links,
                page_html,
            )
            has_reliable_category = bool(page_category_text and any(tok in cat_lc for tok in music_tokens))
            if not page_music and not has_reliable_category and not cat_lc:
                if looks_like_music_fallback(page_text_blocks, artist_name):
                    page_music = True
                    _log(f"[FB Enrich] Falling back to text-based music detection for '{artist_name}' (no FB category; matched name+music tokens)")
            if page_music:
                cat_display = cat_for_gate or "<none>"
                _log(f"[FB Enrich] Confirmed music page for '{artist_name}' with FB category '{cat_display}'.")
        except Exception:
            page_category_text = None
            page_music = False
            # fall through to final gate
    except Exception as exc:
        _log(f"[FB Enrich] Failed to parse FB page '{best_url}' for '{artist_name}': {exc}")

    candidate_url = normalize_external_url(best_url)
    emails = fb_scrape_emails_from_page(
        driver,
        candidate_url,
        log_fn=_log,
        log_prefix=log_prefix,
        suppress_console=suppress_console,
    )

    # If we pulled a contact email, consider override only if identity/music hints are present.
    if not page_music and emails:
        override_accept, override_reason = should_accept_email_override(
            artist_name,
            {"name": best_name, "category": best_category_raw, "base_score": best_base_score},
            {
                "has_music_signals": page_music,
                "category": page_category_text or best_category_raw,
                "music_hint": bool(allow_soft_pass_category and _category_is_music_like(page_category_text or best_category_raw)),
                "score": best_score,
            },
        )
        if override_accept:
            if os.getenv("FB_DEBUG_EMAIL_OVERRIDE") == "1":
                _log(f"[FB Enrich][EmailOverrideDebug] url='{best_url}' cat_raw='{best_category_raw}' allow_soft_pass={bool(allow_soft_pass_category)} reason={override_reason}")
            _log(f"[FB Enrich][EmailOverrideAccept] url='{candidate_url}' emails={len(emails)} reason={override_reason}")
            return candidate_url, emails
        _log(f"[FB Enrich][EmailOverrideReject] url='{candidate_url}' emails={len(emails)} reason={override_reason} category='{page_category_text or best_category_raw}'")
    if not page_music:
        if allow_soft_pass_category and _category_is_music_like(page_category_text or best_category_raw):
            _log(f"[FB Enrich] Soft-pass music gate by category allowlist: category='{page_category_text or best_category_raw}' url='{best_url}'")
        else:
            _log(f"[FB Enrich] Rejecting FB page '{best_url}' for '{artist_name}' after scrape: no music signals found (final gate).")
            return "", []

    _log(f"[FB Enrich] Best FB candidate for '{artist_name}' -> '{best_name}' (final_score={best_score:.2f}, base_score={best_base_score:.2f}, cat_boost={best_cat_boost:.2f}, category='{cat_display}')")
    return candidate_url, emails

def _fb_scoring_sanity_tests():
    """
    Lightweight sanity checks for FB scoring without live requests.
    """
    def cat_boost(cat: str) -> float:
        cat_norm = normalize_name(cat)
        strong = ["musician/band", "musician", "band", "artist", "music", "singer"]
        neg = ["spa", "real estate", "hotel", "boutique", "store"]
        if any(token in cat_norm for token in strong):
            return 2.5
        if any(token in cat_norm for token in neg):
            return -3.0
        return 0.0

    def score(artist, name, category):
        artist_norm = normalize_name(artist)
        base = 0.0
        page_norm = normalize_name(name)
        if page_norm == artist_norm:
            base += 1.0
        elif page_norm.startswith(artist_norm):
            base += 0.7
        elif artist_norm in page_norm:
            base += 0.4
        return base + cat_boost(category or "")

    scenarios = [
        ("Aneya", "Aneya Music", "Musician/band", "Aneya Care Spa", "Health spa"),
        ("Ṣẹwà", "Ṣẹwà", "Musician/band", "Sewa Sandhu - Real Estate", "Real Estate Agent"),
        ("Salle", "Salle", "Musician", "Salle Sells Retro Vintage", "Clothing store"),
        ("The.wav", "The.wav", "Musician/band", "The Wave Resort", "Hotel/Resort"),
    ]
    for artist, good_name, good_cat, bad_name, bad_cat in scenarios:
        assert score(artist, good_name, good_cat) > score(artist, bad_name, bad_cat)
    print("[FB Enrich] Sanity tests passed.")

def extract_emails(text):
    email_pattern = r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
    return list(set(re.findall(email_pattern, text)))

def login_facebook(driver, fb_username, fb_password):
    driver.get('https://www.facebook.com/')
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, 'email')))
    driver.find_element(By.ID, 'email').send_keys(fb_username)
    driver.find_element(By.ID, 'pass').send_keys(fb_password)
    driver.find_element(By.NAME, 'login').click()
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))


class FacebookSessionManager:
    """
    Lightweight session manager to ensure we login once per run and reuse the same driver.
    This keeps behaviour identical for callers while reducing repeated logins.
    """

    def __init__(self, username: str, password: str, driver_factory, logger=None):
        self.username = username
        self.password = password
        self.driver_factory = driver_factory
        self.logger = logger
        self.driver = None
        self.logged_in = False
        self.main_window_handle = None
        self.page_counter = 0
        self._auto_login_disabled_logged = False

    def _log(self, msg: str):
        if not msg:
            return
        if self.logger:
            try:
                self.logger(msg)
            except Exception:
                pass
        else:
            try:
                print(msg)
            except Exception:
                pass

    def ensure_driver(self):
        if self.driver is None:
            self.driver = self.driver_factory()

    def ensure_logged_in(self):
        self.ensure_driver()
        if self.logged_in:
            return self.driver
        allow_auto_login = str(os.environ.get("FB_ALLOW_AUTOMATED_LOGIN", "") or "").strip().lower() in ("1", "true", "yes")
        if not allow_auto_login:
            if not self._auto_login_disabled_logged:
                self._log("[FB Session] Automated login disabled (FB_ALLOW_AUTOMATED_LOGIN not set); skipping credential typing.")
                self._auto_login_disabled_logged = True
            return self.driver
        login_facebook(self.driver, self.username, self.password)
        self.logged_in = True
        self._capture_main_window()
        return self.driver

    def _capture_main_window(self):
        try:
            self.main_window_handle = self.driver.current_window_handle
        except Exception as exc:
            self.main_window_handle = None
            self._log(f"[FB Session] Warning: could not capture main window handle: {exc}")

    def refresh_session(self):
        self._log("[FB Session] Refreshing Facebook session...")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        self.logged_in = False
        self.ensure_logged_in()

    def maybe_refresh(self, refresh_every: int):
        if refresh_every <= 0:
            return
        self.page_counter += 1
        if self.page_counter % refresh_every == 0:
            self.refresh_session()

    def close_extra_windows(self):
        if self.driver is None:
            return
        if self.main_window_handle:
            try:
                close_unexpected_windows(self.driver, self.main_window_handle, logger=self._log)
            except Exception:
                pass

    def navigate(self, url: str):
        driver = self.ensure_logged_in()
        driver.get(url)
        self._capture_main_window()
        self.close_extra_windows()
        return driver

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        self.logged_in = False
        self.main_window_handle = None


_FB_SHARED_SESSION = None
_FB_SHARED_CREDS = None


def get_shared_facebook_session(username: str, password: str, logger=None) -> FacebookSessionManager:
    """
    Return a shared session for the given credentials, creating it if needed.
    """
    global _FB_SHARED_SESSION, _FB_SHARED_CREDS
    creds = (username or "").strip(), (password or "").strip()
    if _FB_SHARED_SESSION is not None and _FB_SHARED_CREDS == creds:
        try:
            _FB_SHARED_SESSION.ensure_logged_in()
            return _FB_SHARED_SESSION
        except Exception:
            try:
                _FB_SHARED_SESSION.close()
            except Exception:
                pass
            _FB_SHARED_SESSION = None
    _FB_SHARED_SESSION = FacebookSessionManager(username, password, setup_facebook_driver, logger=logger)
    _FB_SHARED_SESSION.ensure_logged_in()
    _FB_SHARED_CREDS = creds
    return _FB_SHARED_SESSION


def release_shared_facebook_session():
    global _FB_SHARED_SESSION, _FB_SHARED_CREDS
    try:
        if _FB_SHARED_SESSION:
            _FB_SHARED_SESSION.close()
    except Exception:
        pass
    _FB_SHARED_SESSION = None
    _FB_SHARED_CREDS = None

def _extract_social_links(row):
    """Return all usable social URLs (split on ';' or ',') from likely columns."""
    excluded_snippets = (
        "soundcloud.com/triplejunearthed",
        "tiktok.com/@triplejradio",
        "youtube.com/abcaustralia",
    )
    candidate_columns = [
        "Social Link",
        "social link",
        "SOCIAL LINK",
        "Facebook",
        "facebook",
        "FACEBOOK"
    ]
    urls = []
    for col in candidate_columns:
        if col in row and pd.notna(row[col]):
            value = str(row[col]).strip()
            if value:
                parts = re.split(r"[;,]", value)
                for part in parts:
                    url = part.strip()
                    if url and not any(excl in url.lower() for excl in excluded_snippets):
                        urls.append(url)
    return urls


def _extract_social_link_from_row(row):
    """Maintain backward compatibility: return the first social link if present."""
    links = _extract_social_links(row)
    return links[0] if links else ""


def _legacy_fb_raw_field_candidates(row):
    """Yield explicit Facebook URL candidates in stable preference order."""
    candidate_fields = (
        "Facebook_URL",
        "facebook_url",
        "Facebook URL",
        "FB_URL",
        "Facebook",
        "facebook",
        "FACEBOOK",
        "Social Link",
        "social link",
        "SOCIAL LINK",
    )
    for field in candidate_fields:
        if field not in row or pd.isna(row[field]):
            continue
        raw_value = str(row[field] or "").strip()
        if not raw_value:
            continue
        for part in re.split(r"[|;,\n\r]+", raw_value):
            candidate = part.strip()
            if candidate:
                yield field, candidate


def _legacy_fb_is_explicit_candidate(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return any(token in lowered for token in ("facebook.com", "fb.com", "fb.me"))


def _legacy_fb_is_allowed_canonical_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = (parsed.path or "").rstrip("/")
    if path.lower() == "/profile.php":
        qs = parse_qs(parsed.query or "", keep_blank_values=False)
        ids = qs.get("id", [])
        return len(ids) == 1 and bool(ids[0]) and ids[0].isdigit()
    if parsed.query:
        return False
    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    if len(segments) != 1:
        return False
    return segments[0].lower() != "profile.php"


def _resolve_legacy_facebook_target(row) -> Tuple[str, str, str]:
    """
    Resolve one canonical FB URL for the legacy/day scrape path.

    Returns ``(raw_input_url, canonical_url, status)`` where status is one of:
    ``resolved``, ``unsafe_or_weak``, or ``missing``.
    """
    first_fb_like = ""
    for _field, raw_candidate in _legacy_fb_raw_field_candidates(row):
        if not _legacy_fb_is_explicit_candidate(raw_candidate):
            continue
        if not first_fb_like:
            first_fb_like = raw_candidate
        canonical = canonicalize_facebook_url(raw_candidate)
        if not canonical:
            continue
        if _legacy_fb_is_allowed_canonical_url(canonical):
            return raw_candidate, canonical, "resolved"
    if first_fb_like:
        return first_fb_like, "", "unsafe_or_weak"
    return "", "", "missing"


def _extract_emails_from_loaded_fb_page(driver) -> List[str]:
    """Extract emails from the already-opened Facebook page without navigating."""
    emails = []
    seen = set()

    def _record(values):
        for value in values or []:
            cleaned = (value or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                emails.append(cleaned)

    soup = None
    try:
        page_source = driver.page_source or ""
    except Exception:
        page_source = ""
    if page_source:
        try:
            soup = BeautifulSoup(page_source, "html.parser")
        except Exception:
            soup = None
    if soup is not None:
        _record(extract_emails(soup.get_text(" ", strip=True)))
        for anchor in soup.select('a[href^="mailto:"]'):
            href = anchor.get("href") or ""
            if href.startswith("mailto:"):
                addr = href.split("mailto:", 1)[-1].split("?", 1)[0]
                if addr:
                    _record([addr])
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        _record(extract_emails(getattr(body, "text", "") or ""))
    except Exception:
        pass
    try:
        rendered_text = driver.execute_script("return document.body ? (document.body.innerText || '') : '';")
        _record(extract_emails(str(rendered_text or "")))
    except Exception:
        pass
    return emails


def _log_legacy_fb_row_outcome(raw_input_url: str, canonical_url: str, final_page_url: str, main_found: bool, about_visited: bool, status: str):
    print(
        "[FB Legacy] "
        f"raw={raw_input_url or '-'} "
        f"canonical={canonical_url or '-'} "
        f"final={final_page_url or '-'} "
        f"main_email={'yes' if main_found else 'no'} "
        f"about={'yes' if about_visited else 'no'} "
        f"status={status}"
    )

def row_has_email(row, email_column: str = "Email") -> bool:
    """
    Returns True if the row already has at least one email address
    in the given email_column. Treats whitespace-only strings as empty.
    """
    if row is None:
        return False
    try:
        value = row.get(email_column, "")
    except Exception:
        try:
            value = row[email_column]
        except Exception:
            value = ""
    if value is None or pd.isna(value):
        return False
    text = str(value).strip()
    return len(text) > 0

def _extract_existing_emails(row, precomputed_links=None):
    """Gather any email addresses already present in Email/Social Link fields."""
    emails = []
    seen = set()

    def _record(email_candidate: str):
        cleaned = (email_candidate or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            emails.append(cleaned)

    for key in ("Email", "email"):
        if key in row and pd.notna(row[key]):
            for addr in extract_emails(str(row[key])):
                _record(addr)

    links = precomputed_links if precomputed_links is not None else _extract_social_links(row)
    for link in links:
        text = (link or "").strip()
        if not text:
            continue
        lower = text.lower()
        if lower.startswith("mailto:"):
            addr = text.split("mailto:", 1)[-1].split("?", 1)[0]
            _record(addr)
        else:
            for addr in extract_emails(text):
                _record(addr)

    return emails

def _preserve_row_fields(row) -> dict:
    """Copy a row/Series to a dict, replacing NaN values with empty strings."""
    if row is None:
        return {}
    if isinstance(row, pd.Series):
        source = row.to_dict()
    elif isinstance(row, dict):
        source = dict(row)
    else:
        try:
            source = dict(row)
        except Exception:
            source = {}
    preserved = {}
    for key, value in source.items():
        if pd.isna(value):
            preserved[key] = ""
        else:
            preserved[key] = value
    return preserved

def _build_email_result(row, url, emails, preferred_url=""):
    """Assemble the Facebook email row payload from an input row + emails."""
    unique_emails = []
    seen = set()
    for value in emails or []:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique_emails.append(cleaned)
    if not unique_emails:
        return None, []

    row_data = _preserve_row_fields(row)

    def _row_value(keys, default=""):
        for key in keys:
            value = row_data.get(key)
            if value not in ("", None):
                return str(value)
        return default

    artist_name = _row_value(["Artist Name", "artist"]).replace("-", " ").title()
    song_title = _safe_row_value(row_data, "Song Title", "")
    if (not song_title) and ("Latest Release" in row_data):
        song_title = _safe_row_value(row_data, "Latest Release", "")
    release_date_value = (
        _safe_row_value(row_data, "Release Date", "")
        or _safe_row_value(row_data, "Latest Release Date", "")
        or _safe_row_value(row_data, "latest_release_date", "")
        or _safe_row_value(row_data, "release_date", "")
    )
    primary_genre_value = _safe_row_value(row_data, "Primary Genre", "")
    source_tag = _safe_row_value(row_data, "Source Tag", "")
    final_url = preferred_url or url or _extract_social_link_from_row(row_data) or ""
    payload = dict(row_data)
    payload["artist"] = artist_name
    payload["Artist Name"] = payload.get("Artist Name") or artist_name
    payload["location"] = row_data.get("Location", row_data.get("location", ""))
    payload["song_title"] = song_title
    payload["sounds_like"] = row_data.get("Sounds Like", row_data.get("sounds_like", ""))
    payload["release_date"] = release_date_value
    payload["Release Date"] = release_date_value
    payload["url"] = final_url
    payload["emails"] = ", ".join(unique_emails)
    payload["Played on triple J"] = row_data.get("Played on triple J", row_data.get("Played on tri", ""))
    payload["Played on Unearthed"] = row_data.get("Played on Unearthed", row_data.get("Played on Ur", ""))
    payload["latest_release_date"] = row_data.get("latest_release_date", release_date_value)
    payload["primary_genre"] = primary_genre_value
    payload["source_tag"] = source_tag
    payload["date_added"] = datetime.datetime.now().strftime("%Y-%m-%d")
    if unique_emails and not payload.get("Email"):
        payload["Email"] = unique_emails[0]
    return payload, unique_emails

def _safe_row_value(row, key, fallback=""):
    if key not in row:
        return fallback
    value = row.get(key)
    if pd.isna(value):
        return fallback
    return value


def _prompt_origin_auto_validate(csv_path: str, default_scope: str = "uncertain_only"):
    """
    Offer a lightweight prompt to run origin auto-validate on a CSV.
    Skips in non-interactive contexts (e.g. GUI threads).
    """
    try:
        if os.environ.get("DISABLE_ORIGIN_AUTO_VALIDATE_PROMPT"):
            return None
    except Exception:
        return None
    if not csv_path or not os.path.exists(csv_path):
        return None
    try:
        if QtWidgets.QApplication.instance():
            return None
    except Exception:
        return None
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
    except Exception:
        return None
    print(
        "\nEnable Origin Auto-Validate on the output CSV? (recommended for production batches)\n"
        "This will:\n"
        "  - Re-open origin URLs (Bandcamp/SoundCloud/Unearthed/Spotify)\n"
        "  - Confirm Artist + Song exist on the original page\n"
        "  - Auto-upgrade good matches to OK and auto-block obvious mismatches\n"
    )
    ans = input("Auto-Validate now? [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        return None
    scope_choice = input("Run on 1) Uncertain rows only (default) or 2) All rows? [1/2]: ").strip()
    scope = "all" if scope_choice == "2" else default_scope
    try:
        result_path = run_auto_validate(csv_path, validate_scope=scope, logger=print)
        return result_path
    except Exception as exc:
        print(f"[Auto-Validate] Failed safely: {exc}")
        return None


def _run_auto_validate_only():
    path = input("[Auto-Validate Only]\nInput CSV path: ").strip()
    if not path:
        print("No CSV provided.")
        return
    if not os.path.exists(path):
        print(f"CSV not found: {path}")
        return
    scope_choice = input("Run on:\n  1) Only uncertain rows (REVIEW/BLOCKED or mid-band scores)\n  2) All rows\nSelection [1/2]: ").strip()
    scope = "all" if scope_choice == "2" else "uncertain_only"
    try:
        result_path = run_auto_validate(path, validate_scope=scope, logger=print)
    except Exception as exc:
        print(f"[Auto-Validate] Failed safely: {exc}")


def _run_text_main_menu():
    while True:
        print(
            "\nLead Machine – Main Menu\n"
            "1) Scrape directory (use GUI tabs)\n"
            "2) Facebook scraper (use GUI tabs)\n"
            "3) Spotify enricher (use GUI tabs)\n"
            "4) Run Auto-Validate (origin checks) on existing CSV\n"
            "5) Exit\n"
        )
        choice = input("Selection: ").strip()
        if choice == "4":
            _run_auto_validate_only()
        elif choice == "5":
            return
        else:
            print("For scraping/enriching, please use the GUI tabs. Select option 4 for Auto-Validate.")

def _canonicalize_release_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure only one 'Release Date' column remains and is populated."""
    if df is None:
        return df
    if df.empty and "Release Date" not in df.columns:
        df["Release Date"] = ""
        return df
    if "Release Date" not in df.columns:
        df["Release Date"] = ""
    def _norm(series):
        if series.empty:
            return series
        return series.fillna("").astype(str).str.strip()
    df["Release Date"] = _norm(df["Release Date"])
    for alt in ("release_date", "latest_release_date"):
        if alt not in df.columns:
            continue
        alt_values = _norm(df[alt])
        mask = df["Release Date"].str.len() == 0
        df.loc[mask, "Release Date"] = alt_values[mask]
        df.drop(columns=alt, inplace=True)
    df["Release Date"] = _norm(df["Release Date"])
    return df

def _ensure_dataframe_column(df: pd.DataFrame, column: str, fallback_column: str | None = None) -> pd.DataFrame:
    """Ensure a column exists, optionally cloning values from a fallback column."""
    if df is None:
        return df
    if column in df.columns:
        df[column] = df[column].fillna("").astype(str)
        if fallback_column and fallback_column in df.columns:
            fallback_values = df[fallback_column].fillna("").astype(str)
            mask = df[column].str.strip() == ""
            df.loc[mask, column] = fallback_values[mask]
    elif fallback_column and fallback_column in df.columns:
        df[column] = df[fallback_column].fillna("").astype(str)
    else:
        df[column] = ""
    return df

_FACEBOOK_CANONICAL_COLUMN_MAP = {
    "Artist Name": ["artist"],
    "Location": ["location"],
    "Song Title": ["song_title"],
    "Sounds Like": ["sounds_like"],
    "Social Link": ["url"],
    "Primary Genre": ["Primary Gen", "primary_genre"],
    "Source Tag": ["source_tag"],
    "Date Added": ["date_added"],
    "Email": ["emails", "email"],
}

_FACEBOOK_OUTPUT_COLUMNS = [
    "Artist Name",
    "Location",
    "Song Title",
    "Sounds Like",
    "Social Link",
    "SoundCloud Link",
    "Played on triple J",
    "Played on Unearthed",
    "Release Date",
    "Primary Genre",
    "Source Tag",
    "Date Added",
    "Email",
]

def _finalize_facebook_output_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Merge duplicate columns and enforce canonical ordering for Facebook CSV export."""
    if df is None:
        return df
    if df.empty:
        for col in _FACEBOOK_OUTPUT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[_FACEBOOK_OUTPUT_COLUMNS]
    df = df.copy()
    for canonical, alternatives in _FACEBOOK_CANONICAL_COLUMN_MAP.items():
        if canonical not in df.columns:
            df[canonical] = ""
        df[canonical] = df[canonical].fillna("").astype(str)
        for source in alternatives:
            if source not in df.columns:
                continue
            source_values = df[source].fillna("").astype(str).str.strip()
            mask = df[canonical].astype(str).str.strip() == ""
            df.loc[mask, canonical] = source_values[mask]
    drop_cols = []
    for alternatives in _FACEBOOK_CANONICAL_COLUMN_MAP.values():
        for source in alternatives:
            if source in df.columns:
                drop_cols.append(source)
    if drop_cols:
        df = df.drop(columns=drop_cols)
    df = _canonicalize_release_date_columns(df)
    for col in _FACEBOOK_OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    ordered_cols = _FACEBOOK_OUTPUT_COLUMNS + [c for c in df.columns if c not in _FACEBOOK_OUTPUT_COLUMNS]
    return df[ordered_cols]

def _goto_facebook_about(driver, page_url: str, timeout: float = 5.0) -> bool:
    """
    Try multiple strategies to land on the About tab for a Facebook page.
    Falls back to direct /about URLs when buttons are not available.
    """
    normalized = (page_url or "").strip()
    if not normalized:
        return False
    about_selectors = [
        (By.XPATH, "//a[contains(@href,'about_contact_and_basic_info')]"),
        (By.XPATH, "//a[contains(@href,'about_details')]"),
        (By.XPATH, "//a[contains(@href,'/about')]"),
        (By.XPATH, "//a[.//span[text()='About']]"),
        (By.XPATH, "//a[normalize-space(text())='About']"),
    ]
    for by, locator in about_selectors:
        try:
            target = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, locator)))
            driver.execute_script("arguments[0].click();", target)
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            return True
        except Exception:
            continue
    base = normalized.rstrip("/")
    about_variants = []
    try:
        parsed = urlparse(normalized)
        path = (parsed.path or "").rstrip("/")
        base = f"{parsed.scheme}://{parsed.netloc}{path}"
        about_variants = [
            f"{base}/about_contact_and_basic_info",
            f"{base}/about_details",
            f"{base}/about",
        ]
    except Exception:
        about_variants = [f"{base}/about"]
    for candidate in about_variants:
        try:
            driver.get(candidate)
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            return True
        except Exception:
            continue
    return False

# =============================================================================
# UPDATED scrape_csv Function (Simpler Version with Wait Times of 0.5 sec and Session Refresh every 20 pages)
# =============================================================================
def scrape_csv(input_csv, output_csv, fb_username, fb_password, max_emails=None, use_shared_session: bool = False):
    existing_data = pd.DataFrame()
    if os.path.exists(output_csv):
        try:
            existing_data = pd.read_csv(output_csv)
        except Exception:
            existing_data = pd.DataFrame()
    existing_data = _ensure_dataframe_column(existing_data, "url", "Social Link")
    existing_data = _ensure_dataframe_column(existing_data, "emails", "Email")
    existing_data = _canonicalize_release_date_columns(existing_data)
    data = pd.read_csv(input_csv)
    # Normalize column names to remove extra whitespace.
    data.columns = [col.strip() for col in data.columns]
    results = []
    emails_found = 0
    processed_urls = set()
    main_window_handle = None

    def _fb_log(msg: str):
        try:
            print(msg)
        except Exception:
            pass
    if not existing_data.empty and "url" in existing_data.columns:
        for value in existing_data["url"].astype(str).tolist():
            if not isinstance(value, str):
                continue
            cleaned = value.strip()
            if not cleaned:
                continue
            processed_urls.add(cleaned)
            canonical = canonicalize_facebook_url(cleaned)
            if canonical:
                processed_urls.add(canonical)
    exclude_urls = {"https://www.facebook.com/triplejunearthed/", "https://www.facebook.com/abc/"}
    exclude_urls_lower = {url.lower() for url in exclude_urls}
    exclude_urls_canonical = {canonicalize_facebook_url(url) for url in exclude_urls if canonicalize_facebook_url(url)}
    facebook_rows = []
    for index, row in data.iterrows():
        artist_name = ""
        try:
            artist_name = str(row.get("Artist Name", "")).strip()
        except Exception:
            try:
                artist_name = str(row["Artist Name"]).strip()
            except Exception:
                artist_name = ""
        links = _extract_social_links(row)
        preexisting_emails = _extract_existing_emails(row, links)
        existing_contact_url = ""
        for candidate_link in links or []:
            lowered = candidate_link.lower()
            if "facebook.com" not in lowered:
                existing_contact_url = candidate_link
                break
        if not existing_contact_url and links:
            existing_contact_url = links[0]

        if preexisting_emails:
            payload, _ = _build_email_result(row, "", preexisting_emails, preferred_url=existing_contact_url)
            if payload:
                results.append(payload)

        if row_has_email(row, email_column="Email"):
            print(f"[FB Scraper] Row {index + 1}: skipping '{artist_name}' because Email column is already populated.")
            continue

        raw_fb_url, canonical_fb_url, fb_target_status = _resolve_legacy_facebook_target(row)
        if fb_target_status == "unsafe_or_weak":
            _log_legacy_fb_row_outcome(raw_fb_url, "", "", False, False, "skip_unsafe_url")
            continue

        if not links and not canonical_fb_url:
            continue

        if not canonical_fb_url:
            continue
        canonical_lower = canonical_fb_url.lower()
        if canonical_lower in exclude_urls_lower or canonical_fb_url in exclude_urls_canonical or canonical_fb_url in processed_urls:
            continue
        facebook_rows.append((row, raw_fb_url or canonical_fb_url, canonical_fb_url, preexisting_emails))
    if not facebook_rows:
        if results:
            results_df = pd.DataFrame(results)
            results_df = _ensure_dataframe_column(results_df, "url")
            results_df = _ensure_dataframe_column(results_df, "emails")
            results_df = _canonicalize_release_date_columns(results_df)
            combined_data = pd.concat([existing_data, results_df]).drop_duplicates(subset=['url', 'emails'])
            combined_data = _finalize_facebook_output_dataframe(combined_data)
            fallback_cols = list(existing_data.columns) if isinstance(existing_data, pd.DataFrame) and len(getattr(existing_data, "columns", [])) else ["url", "emails"]
            _safe_atomic_write_csv(combined_data, output_csv, fallback_cols, reason="facebook_results")
            print(f"Scraping completed. Results saved to {output_csv}")
            final_csv_path = output_csv
            from final_checker import run_final_checker
            checked_path = final_csv_path
            try:
                checked_path = run_final_checker(final_csv_path)
                print(f"[Final Checker] Completed → {checked_path}")
            except Exception as e:
                print(f"[Final Checker] Failed safely: {e}")
            _prompt_origin_auto_validate(checked_path or output_csv)
        else:
            print("No Facebook pages to process.")
        return
    session = get_shared_facebook_session(fb_username, fb_password, logger=_fb_log) if use_shared_session else FacebookSessionManager(fb_username, fb_password, setup_facebook_driver, logger=_fb_log)
    driver = session.ensure_logged_in()
    if FACEBOOK_CLOSE_EXTRA_WINDOWS:
        session.close_extra_windows()
    session_counter = 0
    remaining_rows = []
    try:
        for idx, (row, raw_input_url, url, known_emails) in enumerate(facebook_rows):
            if not url:
                continue
            preexisting_emails = list(known_emails or [])
            final_page_url = url
            main_page_found_email = False
            about_visited = False
            status = "no_email"
            try:
                if FACEBOOK_CLOSE_EXTRA_WINDOWS:
                    session.close_extra_windows()
                driver = session.navigate(url)
                if FACEBOOK_CLOSE_EXTRA_WINDOWS:
                    session.close_extra_windows()
                session_counter += 1
                # Refresh the session every 20 pages.
                if session_counter % 20 == 0:
                    session.refresh_session()
                    driver = session.ensure_logged_in()
                time.sleep(1.0)
                WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
                emails = list(preexisting_emails)
                try:
                    final_page_url = getattr(driver, "current_url", "") or url
                except Exception:
                    final_page_url = url
                main_page_emails = _extract_emails_from_loaded_fb_page(driver)
                if main_page_emails:
                    main_page_found_email = True
                    emails.extend(main_page_emails)
                    status = "found_email_main"
                else:
                    navigated = _goto_facebook_about(driver, url, timeout=5)
                    if FACEBOOK_CLOSE_EXTRA_WINDOWS:
                        session.close_extra_windows()
                    about_visited = bool(navigated)
                    if navigated:
                        time.sleep(1.0)
                        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
                        try:
                            final_page_url = getattr(driver, "current_url", "") or final_page_url
                        except Exception:
                            pass
                        about_emails = _extract_emails_from_loaded_fb_page(driver)
                        if about_emails:
                            emails.extend(about_emails)
                            status = "found_email_about"
                payload, unique_emails = _build_email_result(row, url, emails, preferred_url=url)
                if payload:
                    results.append(payload)
                    emails_found += len(unique_emails)
                    if max_emails is not None and emails_found >= max_emails:
                        remaining_rows = facebook_rows[idx + 1 :]
                        _log_legacy_fb_row_outcome(raw_input_url, url, final_page_url, main_page_found_email, about_visited, status)
                        break
                elif status.startswith("found_email"):
                    status = "no_email"
            except Exception as e:
                print(f"Error scraping {url}: {e}")
                status = "error"
                if preexisting_emails:
                    payload, _ = _build_email_result(row, url, preexisting_emails, preferred_url=url)
                    if payload:
                        results.append(payload)
            _log_legacy_fb_row_outcome(raw_input_url, url, final_page_url, main_page_found_email, about_visited, status)
            processed_urls.add(url)
            # Random sleep between 1 and 2 seconds.
            time.sleep(random.uniform(1, 2))
        else:
            remaining_rows = []
    finally:
        if not use_shared_session:
            session.close()
    for row, raw_input_url, url, known_emails in remaining_rows:
        if known_emails:
            payload, _ = _build_email_result(row, url, known_emails, preferred_url=url)
            if payload:
                results.append(payload)
    results_df = pd.DataFrame(results)
    results_df = _ensure_dataframe_column(results_df, "url")
    results_df = _ensure_dataframe_column(results_df, "emails")
    results_df = _canonicalize_release_date_columns(results_df)
    combined_data = pd.concat([existing_data, results_df]).drop_duplicates(subset=['url', 'emails'])
    combined_data = _finalize_facebook_output_dataframe(combined_data)
    fallback_cols = list(existing_data.columns) if isinstance(existing_data, pd.DataFrame) and len(getattr(existing_data, "columns", [])) else ["url", "emails"]
    _safe_atomic_write_csv(combined_data, output_csv, fallback_cols, reason="facebook_results")
    print(f"Scraping completed. Results saved to {output_csv}")
    final_csv_path = output_csv
    from final_checker import run_final_checker
    try:
        checked_path = run_final_checker(final_csv_path)
        print(f"[Final Checker] Completed → {checked_path}")
    except Exception as e:
        print(f"[Final Checker] Failed safely: {e}")
    _prompt_origin_auto_validate(checked_path or output_csv)

# =============================================================================
# PyQt5 GUI Code
# =============================================================================
class ArtistScraperThread(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal()
    def __init__(self, website_url, max_artists, output_csv, source="Unearthed",
                 pages_per_tag=BANDCAMP_PAGES_PER_TAG, seed_tags=None, bandcamp_mode: str = "discover", bandcamp_search_domain: str = "artists", bandcamp_search_location: str = "", parent=None):
        super().__init__(parent)
        self.website_url = website_url
        self.max_artists = max_artists
        self.output_csv = output_csv
        self.source = source
        self.pages_per_tag = pages_per_tag
        self.bandcamp_mode = (bandcamp_mode or "discover").strip().lower()
        self.bandcamp_search_domain = (bandcamp_search_domain or "artists").strip().lower() or "artists"
        self.bandcamp_search_location = (bandcamp_search_location or "").strip()
        if seed_tags is not None:
            self.seed_tags = list(seed_tags)
        elif self.source and self.source.lower() == "soundcloud":
            if website_url and website_url.strip():
                self.seed_tags = []
            else:
                self.seed_tags = list(SOUNDCLOUD_SEED_TAGS)
        elif self.source and self.source.lower().startswith("last.fm"):
            self.seed_tags = []
        else:
            self.seed_tags = list(BANDCAMP_SEED_TAGS)
    def run(self):
        self.log_signal.emit("Starting artist scraping...")
        try:
            if self.source.lower() == "bandcamp":
                target = self.website_url if _bc_is_bandcamp_url((self.website_url or "").strip()) else self.seed_tags
                scrape_bandcamp(
                    target,
                    pages_per_tag=self.pages_per_tag,
                    existing_csv=self.output_csv,
                    max_artists=self.max_artists,
                    mode=self.bandcamp_mode,
                    search_domain=self.bandcamp_search_domain,
                    search_location_filter=self.bandcamp_search_location,
                )
                self.log_signal.emit("Bandcamp scraping completed.")
            elif self.source.lower() == "soundcloud":
                scrape_soundcloud(
                    (self.website_url or "").strip(),
                    seed_tags=self.seed_tags,
                    pages_per_tag=self.pages_per_tag,
                    existing_csv=self.output_csv,
                    max_artists=self.max_artists
                )
                self.log_signal.emit("SoundCloud scraping completed.")
            elif self.source.lower().startswith("last.fm"):
                seed_raw = self.website_url or ""
                seed_parts = re.split(r"[,\n]+", seed_raw) if seed_raw else []
                seed_artists = [s.strip() for s in seed_parts if s.strip()]
                scrape_lastfm_similar(
                    seed_artists,
                    existing_csv=self.output_csv,
                    max_artists=self.max_artists,
                    log_fn=self.log_signal.emit
                )
                self.log_signal.emit("Last.fm similar-artist scraping completed.")
            elif self.source.lower() == "spotify":
                params = {
                    "search_term": (self.website_url or "").strip(),
                    "max_artists": self.max_artists
                }
                rows = scrape_spotify(
                    self.max_artists,
                    params,
                    logger=self.log_signal.emit,
                    progress_callback=None
                )
                if not rows:
                    self.log_signal.emit("Spotify scraping returned no rows.")
                else:
                    spotify_columns = [
                        "Artist Name", "Location", "Song Title", "Sounds Like", "Social Link",
                        "SoundCloud Link", "Played on triple J", "Played on Unearthed",
                        "Release Date", "Primary Genre", "Date Added", "Spotify Playlist",
                        "External Links", "Email", "Spotify_URL", "Spotify_Artist_ID",
                        "Spotify_Website_URL", "Spotify_Playlist_URL", "Spotify_Seed_Position",
                        "Spotify_Genres", "Spotify_Followers", "Spotify_Popularity",
                        "Spotify_Seed_Type", "Spotify_Seed_Query"
                    ]
                    try:
                        existing_df = pd.read_csv(self.output_csv) if os.path.exists(self.output_csv) else pd.DataFrame()
                    except Exception:
                        existing_df = pd.DataFrame()
                    new_df = pd.DataFrame(rows, columns=spotify_columns)
                    for col in spotify_columns:
                        if col not in new_df.columns:
                            new_df[col] = ""
                        if col not in existing_df.columns:
                            existing_df[col] = ""
                    combined = pd.concat([existing_df, new_df], ignore_index=True, sort=False)
                    column_order = list(existing_df.columns)
                    if not column_order:
                        column_order = []
                    for col in spotify_columns:
                        if col not in column_order:
                            column_order.append(col)
                    if not column_order:
                        column_order = spotify_columns
                    combined = combined[column_order]
                    fallback_cols = column_order if column_order else spotify_columns
                    _safe_atomic_write_csv(combined, self.output_csv, fallback_cols, reason="spotify_gui")
                    self.log_signal.emit(f"Spotify scraping completed with {len(new_df)} rows.")
            else:
                scrape_website(self.website_url, existing_csv=self.output_csv, max_artists=self.max_artists)
                self.log_signal.emit("Artist scraping completed.")
        except Exception as e:
            self.log_signal.emit(f"Error in artist scraping: {e}")
        self.finished_signal.emit()

class FacebookScraperThread(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal()
    def __init__(self, input_csv, output_csv, fb_username, fb_password, max_emails, parent=None):
        super().__init__(parent)
        self.input_csv = input_csv
        self.output_csv = output_csv
        self.fb_username = fb_username
        self.fb_password = fb_password
        self.max_emails = max_emails
    def run(self):
        self.log_signal.emit("Starting Facebook scraping...")
        try:
            scrape_csv(self.input_csv, self.output_csv, self.fb_username, self.fb_password, self.max_emails)
            self.log_signal.emit("Facebook scraping completed.")
        except Exception as e:
            self.log_signal.emit(f"Error in Facebook scraping: {e}")
        self.finished_signal.emit()


class AutoValidateWorker(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal(str)

    def __init__(
        self,
        csv_path: str,
        scope: str = "uncertain_only",
        output_path: Optional[str] = None,
        auto_run: bool = False,
        emit_start_log: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.csv_path = csv_path
        self.scope = scope
        self.output_path = output_path
        self.auto_run = auto_run
        self.emit_start_log = emit_start_log

    def run(self):
        try:
            target_path = self.output_path or _derive_origin_output_path(self.csv_path)
            if self.emit_start_log:
                if self.auto_run:
                    self._log("[Auto-Validate] Auto-run enabled (GUI). Validating all rows...")
                else:
                    self._log(f"[Auto-Validate] Starting origin validation on {self.csv_path} (scope: {self.scope})...")
            result = run_auto_validate(
                self.csv_path,
                output_path=target_path,
                validate_scope=self.scope,
                logger=self._log,
            )
            self.finished_signal.emit(result)
        except Exception as exc:
            self._log(f"[Auto-Validate] Failed safely: {exc}")
            self.finished_signal.emit("")

    def _log(self, message: str):
        if message is None:
            return
        try:
            self.log_signal.emit(str(message))
        except Exception:
            pass


CAMPAIGN_PREP_RECENCY_BUCKET_COLUMN = "Recency_Bucket"
CAMPAIGN_PREP_RECENCY_BUCKETS = (
    "0_30_days",
    "30_90_days",
    "90_180_days",
    "180_plus_days",
)
CAMPAIGN_PREP_REGION_SEGMENTS = ("Inside_VIC", "Outside_VIC")
CAMPAIGN_PREP_RADIO_BUCKETS = ("Played_TripleJ", "Played_Unearthed", "Neither")
CAMPAIGN_PREP_RUN_TIMEZONE = datetime.timezone.utc


def _campaign_prep_campaign_filename(region: str, recency_bucket: str, radio_bucket: str) -> str:
    return f"{recency_bucket}/{region}_{radio_bucket}.csv"


CAMPAIGN_PREP_OUTPUT_ORDER = [
    _campaign_prep_campaign_filename(region, recency_bucket, radio_bucket)
    for region in CAMPAIGN_PREP_REGION_SEGMENTS
    for recency_bucket in CAMPAIGN_PREP_RECENCY_BUCKETS
    for radio_bucket in CAMPAIGN_PREP_RADIO_BUCKETS
]


def _campaign_prep_casefold_columns(columns: List[str]) -> Dict[str, str]:
    folded: Dict[str, str] = {}
    for column in columns:
        key = str(column).lower()
        if key not in folded:
            folded[key] = column
    return folded


def _campaign_prep_required_column(columns_by_lower: Dict[str, str], name: str) -> Optional[str]:
    return columns_by_lower.get(name.lower())


def _campaign_prep_resolve_alias(columns_by_lower: Dict[str, str], aliases: Tuple[str, ...]) -> Optional[str]:
    for alias in aliases:
        match = columns_by_lower.get(str(alias).lower())
        if match is not None:
            return match
    return None


def _campaign_prep_required_alias(
    columns_by_lower: Dict[str, str],
    columns: List[str],
    logical_name: str,
    aliases: Tuple[str, ...],
) -> str:
    match = _campaign_prep_resolve_alias(columns_by_lower, aliases)
    if match is None:
        raise ValueError(
            f"Missing required logical field: {logical_name}\n"
            f"Detected columns: {columns}\n"
            f"Accepted aliases: {list(aliases)}"
        )
    return match


def _campaign_prep_truthy(value) -> bool:
    return str(value if value is not None else "").strip().lower() in {"yes", "true", "1", "1.0", "y"}


def _campaign_prep_inside_vic(value) -> bool:
    loc = str(value if value is not None else "").strip().lower()
    if not loc:
        return False
    return "melbourne" in loc or "victoria" in loc or re.search(r"\bvic\b", loc) is not None


def _campaign_prep_email_tokens(value) -> List[str]:
    raw = "" if value is None else str(value)
    tokens: List[str] = []
    for token in raw.split(","):
        cleaned = token.strip()
        if cleaned.lower() in ("", "nan", "none"):
            continue
        tokens.append(cleaned)
    return tokens


def _campaign_prep_has_email_value(value) -> bool:
    cleaned = str(value).strip()
    return cleaned.lower() not in {"", "nan", "none"}


def _campaign_prep_parse_release_date(value) -> Optional[datetime.datetime]:
    cleaned = str(value if value is not None else "").strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        try:
            parsed_ts = pd.to_datetime(cleaned, errors="coerce", dayfirst=True)
        except Exception:
            return None
        if pd.isna(parsed_ts):
            return None
        parsed = parsed_ts.to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CAMPAIGN_PREP_RUN_TIMEZONE)
    return parsed.astimezone(CAMPAIGN_PREP_RUN_TIMEZONE)


def _campaign_prep_recency_bucket(
    parsed_release_date: Optional[datetime.datetime],
    run_reference_date: datetime.datetime,
) -> str:
    if parsed_release_date is None:
        return "180_plus_days"
    release_date = parsed_release_date.astimezone(CAMPAIGN_PREP_RUN_TIMEZONE).date()
    reference_date = run_reference_date.astimezone(CAMPAIGN_PREP_RUN_TIMEZONE).date()
    days_since_release = (reference_date - release_date).days
    if days_since_release < 0:
        days_since_release = 0
    if days_since_release <= 30:
        return "0_30_days"
    if days_since_release <= 90:
        return "30_90_days"
    if days_since_release <= 180:
        return "90_180_days"
    return "180_plus_days"


def _campaign_prep_append_recency_column(columns: List[str]) -> List[str]:
    base_columns = [
        column
        for column in columns
        if str(column).lower() != CAMPAIGN_PREP_RECENCY_BUCKET_COLUMN.lower()
    ]
    return [*base_columns, CAMPAIGN_PREP_RECENCY_BUCKET_COLUMN]


def _campaign_prep_desc_date_key(parsed: datetime.datetime) -> Tuple[int, int, int, int]:
    return (
        -parsed.toordinal(),
        -(parsed.hour * 3600 + parsed.minute * 60 + parsed.second),
        -parsed.microsecond,
        -parsed.fold,
    )


def _campaign_prep_sort_buffer_by_release_date(
    rows: List[Tuple[dict, dict]],
    sort_mode: str,
    release_date_column: Optional[str] = None,
) -> List[Tuple[dict, dict]]:
    if sort_mode in ("", "none", None):
        return rows
    if sort_mode not in ("ascending", "descending"):
        raise ValueError(f"Invalid release_date_sort: {sort_mode}")

    def sort_key(item: Tuple[dict, dict]):
        source_row, prepared_row = item
        parsed = prepared_row.get("parsed_release_date")
        if parsed is None and "parsed_release_date" not in prepared_row:
            if release_date_column is not None:
                release_date_value = source_row.get(release_date_column, "")
            else:
                release_date_value = source_row.get("Release Date", source_row.get("Release_Date", ""))
            parsed = _campaign_prep_parse_release_date(release_date_value)
        if parsed is None:
            return (1, 0)
        if sort_mode == "descending":
            return (0, _campaign_prep_desc_date_key(parsed))
        return (0, parsed)

    return sorted(rows, key=sort_key)


def _campaign_prep_atomic_write_csv(path: Path, rows: List[dict], columns: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        output_df = pd.DataFrame(rows, columns=columns)
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            output_df.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise


def _campaign_prep_build_diagnostics(
    prepared_rows: List[Tuple[dict, dict]],
    run_reference_date: datetime.datetime,
    release_date_column: Optional[str],
    both_release_date_columns_present: bool,
) -> dict:
    bucket_counts = {bucket: 0 for bucket in CAMPAIGN_PREP_RECENCY_BUCKETS}
    invalid_values: List[str] = []
    parsed_dates: List[datetime.datetime] = []
    invalid_count = 0
    for _source_row, prepared_row in prepared_rows:
        bucket = prepared_row.get("recency_bucket", "180_plus_days")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        parsed_release_date = prepared_row.get("parsed_release_date")
        if parsed_release_date is None:
            invalid_count += 1
            if len(invalid_values) < 10:
                invalid_values.append(str(prepared_row.get("release_date_raw", "")))
        else:
            parsed_dates.append(parsed_release_date)

    diagnostics = {
        "run_reference_date": run_reference_date.isoformat(),
        "total_processed_rows": len(prepared_rows),
        "recency_bucket_counts": bucket_counts,
        "invalid_blank_unparseable_release_date_count": invalid_count,
        "min_parsed_release_date": min(parsed_dates).date().isoformat() if parsed_dates else None,
        "max_parsed_release_date": max(parsed_dates).date().isoformat() if parsed_dates else None,
        "sample_invalid_release_date_values": invalid_values,
        "missing_release_date_column": release_date_column is None,
        "resolved_release_date_column": release_date_column,
        "both_release_date_columns_present": both_release_date_columns_present,
    }
    return diagnostics


def _campaign_prep_log_diagnostics(diagnostics: dict) -> None:
    lines = [
        "[Campaign Prep] Export summary diagnostics",
        f"run_reference_date={diagnostics.get('run_reference_date')}",
        f"total_processed_rows={diagnostics.get('total_processed_rows')}",
        f"recency_bucket_counts={diagnostics.get('recency_bucket_counts')}",
        "invalid_blank_unparseable_release_date_count="
        f"{diagnostics.get('invalid_blank_unparseable_release_date_count')}",
        f"min_parsed_release_date={diagnostics.get('min_parsed_release_date')}",
        f"max_parsed_release_date={diagnostics.get('max_parsed_release_date')}",
        f"sample_invalid_release_date_values={diagnostics.get('sample_invalid_release_date_values')}",
        f"missing_release_date_column={diagnostics.get('missing_release_date_column')}",
        f"resolved_release_date_column={diagnostics.get('resolved_release_date_column')}",
        f"both_release_date_columns_present={diagnostics.get('both_release_date_columns_present')}",
    ]
    message = "\n".join(lines)
    logging.getLogger(__name__).info(message)
    print(message)


CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME = "master_export_leads.processed.csv"
CAMPAIGN_PREP_MANIFEST_FILENAME = "campaign_export_manifest.csv"
CAMPAIGN_PREP_SUMMARY_FILENAME = "campaign_export_summary.txt"
CAMPAIGN_PREP_SKIPPED_ROWS_FILENAME = "campaign_export_skipped_rows.csv"
CAMPAIGN_PREP_SKIPPED_ROW_COLUMNS = [
    "Artist",
    "Email",
    "Song Title",
    "Lead_Source",
    "Source_Directory",
    "Source Directory",
    "Segment",
    "Recency_Bucket",
    "reason_skipped",
]
CAMPAIGN_PREP_WOODPECKER_ENCODING_CLEANUP_FIELDS = {
    "Artist",
    "Song Title",
    "Sounds Like",
    "Notes",
    "Location",
}
CAMPAIGN_PREP_MOJIBAKE_REPLACEMENTS = {
    "â€™": "’",
    "â€˜": "‘",
    "â€œ": '"',
    "â€\x9d": '"',
    "â€“": "–",
    "â€”": "—",
    "â€¦": "...",
    "Â": "",
    "Ã©": "é",
    "Ã¨": "è",
    "Ã¡": "á",
    "Ã¢": "â",
    "Ã³": "ó",
    "Ã¶": "ö",
    "Ã¼": "ü",
    "Ãœ": "Ü",
    "Ã±": "ñ",
    "Ã‡": "Ç",
    "Ã§": "ç",
    "‚Äô": "’",
    "‚Äò": "‘",
    "‚Äú": '"',
    "‚Äù": '"',
    "‚Äì": "–",
    "‚Äî": "—",
    "‚Ä¶": "...",
    "√©": "é",
    "√®": "è",
    "√°": "á",
    "√¢": "â",
    "√≥": "ó",
    "√∂": "ö",
    "√º": "ü",
    "√ú": "Ü",
    "√±": "ñ",
    "√á": "Ç",
    "√ß": "ç",
}

CAMPAIGN_PREP_LOCATION_ALIASES = (
    "Location",
    "location",
    "City",
    "city",
    "State",
    "state",
)

CAMPAIGN_PREP_TRIPLEJ_ALIASES = (
    "Played on triple J",
    "Played_On_Triple_J",
    "played_on_triple_j",
    "TripleJ",
    "Triple_J",
)

CAMPAIGN_PREP_UNEARTHED_ALIASES = (
    "Played on Unearthed",
    "Played_On_Unearthed",
    "played_on_unearthed",
    "Unearthed",
)

CAMPAIGN_PREP_RELEASE_DATE_ALIASES = (
    "Release_Date",
    "Release Date",
)

CAMPAIGN_PREP_UPLOAD_DATE_ALIASES = (
    "Upload_Date",
    "Upload Date",
)

CAMPAIGN_PREP_WOODPECKER_COLUMNS = [
    "Email",
    "First Name",
    "Company",
    "Artist",
    "Location",
    "Song Title",
    "Sounds Like",
    "Website",
    "Instagram",
    "Facebook",
    "Source URL",
    "Lead_Source",
    "Source_Directory",
    "Source Directory",
    "Release Date",
    "Upload Date",
    "Notes",
]

CAMPAIGN_PREP_WOODPECKER_ALIASES = [
    ("First Name", ("Contact_Name", "Artist")),
    ("Company", ("Organization", "Artist")),
    ("Artist", ("Artist",)),
    ("Location", CAMPAIGN_PREP_LOCATION_ALIASES),
    ("Song Title", ("Song_Title", "Song Title")),
    ("Sounds Like", ("Sounds Like", "Sounds_Like")),
    ("Website", ("Website",)),
    ("Instagram", ("Instagram_URL", "Instagram")),
    ("Facebook", ("Facebook_URL", "Facebook")),
    ("Source URL", ("Source_URL", "Social Link")),
    ("Lead_Source", ("Lead_Source",)),
    ("Source_Directory", ("Source_Directory",)),
    ("Source Directory", ("Source Directory", "Source_Directory")),
    ("Release Date", CAMPAIGN_PREP_RELEASE_DATE_ALIASES),
    ("Upload Date", CAMPAIGN_PREP_UPLOAD_DATE_ALIASES),
    ("Notes", ("Notes",)),
]


def _campaign_prep_clean_display_text(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\ufffd", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])(?=[^\s,.;:!?])", r"\1 ", text)
    return text.strip()


def _campaign_prep_clean_song_title(title) -> str:
    cleaned = _campaign_prep_clean_display_text(title)
    return cleaned


def _campaign_prep_clean_artist_name(artist) -> str:
    return _campaign_prep_clean_display_text(artist)


def _campaign_prep_repair_export_mojibake(value) -> str:
    text = _campaign_prep_clean_display_text(value)
    if not text:
        return text
    for broken, fixed in CAMPAIGN_PREP_MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(broken, fixed)
    return text


def _campaign_prep_clean_woodpecker_export_row(row: dict) -> dict:
    output = dict(row)
    for column in CAMPAIGN_PREP_WOODPECKER_ENCODING_CLEANUP_FIELDS:
        if column == "Song Title":
            output[column] = _campaign_prep_clean_song_title(output.get(column, ""))
        elif column == "Artist":
            output[column] = _campaign_prep_clean_artist_name(output.get(column, ""))
        else:
            output[column] = _campaign_prep_repair_export_mojibake(output.get(column, ""))
        output[column] = _campaign_prep_repair_export_mojibake(output.get(column, ""))
    return output


def _campaign_prep_segment_name(region: str, radio_bucket: str) -> str:
    return f"{region}_{radio_bucket}"


def _campaign_prep_combined_segment_filename(region: str, radio_bucket: str) -> str:
    return f"{_campaign_prep_segment_name(region, radio_bucket)}_ALL.csv"


def _campaign_prep_skipped_row(
    row: dict,
    columns_by_lower: Dict[str, str],
    email_column: Optional[str],
    segment_name: str,
    recency_bucket: str,
    reason: str,
) -> dict:
    artist_column = _campaign_prep_resolve_alias(columns_by_lower, ("Artist",))
    song_title_column = _campaign_prep_resolve_alias(columns_by_lower, ("Song_Title", "Song Title"))
    lead_source_column = _campaign_prep_resolve_alias(columns_by_lower, ("Lead_Source",))
    source_directory_column = _campaign_prep_resolve_alias(columns_by_lower, ("Source_Directory",))
    legacy_source_directory_column = _campaign_prep_resolve_alias(columns_by_lower, ("Source Directory", "Source_Directory"))
    return {
        "Artist": row.get(artist_column, "") if artist_column is not None else "",
        "Email": row.get(email_column, "") if email_column is not None else "",
        "Song Title": row.get(song_title_column, "") if song_title_column is not None else "",
        "Lead_Source": row.get(lead_source_column, "") if lead_source_column is not None else "",
        "Source_Directory": row.get(source_directory_column, "") if source_directory_column is not None else "",
        "Source Directory": row.get(legacy_source_directory_column, "") if legacy_source_directory_column is not None else "",
        "Segment": segment_name,
        "Recency_Bucket": recency_bucket,
        "reason_skipped": reason,
    }


def _campaign_prep_validate_origin_contract(row: dict, row_number: int) -> None:
    has_origin_contract = "Lead_Source" in row or "Source_Directory" in row
    if not has_origin_contract:
        return
    lead_source = str(row.get("Lead_Source", "") or "").strip()
    source_directory = str(row.get("Source_Directory", "") or "").strip()
    if not lead_source or not source_directory:
        missing = []
        if not lead_source:
            missing.append("Lead_Source")
        if not source_directory:
            missing.append("Source_Directory")
        raise ValueError(
            f"Export contract violation at input row {row_number}: "
            f"missing required field(s): {', '.join(missing)}"
        )


def _campaign_prep_write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise


class CampaignPrepResult(dict):
    def __init__(self, *args, diagnostics: Optional[dict] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = diagnostics or {}


def _campaign_prep_export_row(
    row: dict,
    export_format: str,
    columns_by_lower: Dict[str, str],
    email_column: Optional[str],
) -> dict:
    if export_format in ("lead_machine_full", "input_headers"):
        return dict(row)
    if export_format != "woodpecker":
        raise ValueError(f"Invalid export_format: {export_format}")

    output = {column: "" for column in CAMPAIGN_PREP_WOODPECKER_COLUMNS}
    output["Email"] = row.get(email_column, "") if email_column is not None else ""
    for output_column, aliases in CAMPAIGN_PREP_WOODPECKER_ALIASES:
        source_column = _campaign_prep_resolve_alias(columns_by_lower, aliases)
        output[output_column] = row.get(source_column, "") if source_column is not None else ""
    return output


def generate_campaign_csvs(
    input_csv_path: str,
    output_dir: str,
    split_multiple_emails: bool = False,
    export_format: str = "lead_machine_full",
    remove_rows_without_emails: bool = False,
    release_date_sort: str = "none",
    run_reference_date: Optional[datetime.datetime] = None,
    ledger_operation_reference: Optional[str] = None,
    ledger_created_at: Optional[str] = None,
    email_column_candidates: tuple[str, ...] = (
        "emails",
        "email",
        "Email",
        "Email_All",
        "Primary_Email",
        "All_Emails",
        "Primary Email",
        "All Emails",
    ),
    ) -> dict:
    if export_format not in ("lead_machine_full", "woodpecker", "input_headers"):
        raise ValueError(f"Invalid export_format: {export_format}")
    if release_date_sort not in ("none", "ascending", "descending"):
        raise ValueError(f"Invalid release_date_sort: {release_date_sort}")
    if export_format == "woodpecker":
        operation_reference = ledger_operation_reference or campaign_prep_sidecar.new_operation_reference()
        export_created_at = ledger_created_at or campaign_prep_sidecar.operation_timestamp()
    else:
        operation_reference = ""
        export_created_at = ""
    if run_reference_date is None:
        run_reference_date = datetime.datetime.now(CAMPAIGN_PREP_RUN_TIMEZONE)
    elif run_reference_date.tzinfo is None:
        run_reference_date = run_reference_date.replace(tzinfo=CAMPAIGN_PREP_RUN_TIMEZONE)
    else:
        run_reference_date = run_reference_date.astimezone(CAMPAIGN_PREP_RUN_TIMEZONE)

    read_kwargs = {
        "dtype": str,
        "keep_default_na": False,
    }
    try:
        df = pd.read_csv(input_csv_path, na_filter=False, **read_kwargs)
    except TypeError:
        df = pd.read_csv(input_csv_path, **read_kwargs)
    df = df.where(pd.notna(df), "")
    df = df.astype(str)

    columns = [str(column) for column in df.columns]
    columns_by_lower = _campaign_prep_casefold_columns(columns)
    location_column = _campaign_prep_required_alias(
        columns_by_lower,
        columns,
        "location",
        CAMPAIGN_PREP_LOCATION_ALIASES,
    )
    triplej_column = _campaign_prep_resolve_alias(columns_by_lower, CAMPAIGN_PREP_TRIPLEJ_ALIASES)
    unearthed_column = _campaign_prep_resolve_alias(columns_by_lower, CAMPAIGN_PREP_UNEARTHED_ALIASES)

    email_column = _campaign_prep_resolve_alias(columns_by_lower, email_column_candidates)

    release_date_column = _campaign_prep_resolve_alias(columns_by_lower, CAMPAIGN_PREP_RELEASE_DATE_ALIASES)
    both_release_date_columns_present = (
        columns_by_lower.get("release_date") is not None
        and columns_by_lower.get("release date") is not None
    )

    prepared_rows: List[Tuple[dict, dict]] = []
    skipped_rows: List[dict] = []
    column_indexes = {column: idx for idx, column in enumerate(columns)}
    processed_columns = _campaign_prep_append_recency_column(columns)
    export_base_columns = CAMPAIGN_PREP_WOODPECKER_COLUMNS if export_format == "woodpecker" else columns
    output_columns = _campaign_prep_append_recency_column(export_base_columns)

    for i in range(len(df)):
        row = {
            column: ("" if pd.isna(df.iat[i, column_indexes[column]]) else df.iat[i, column_indexes[column]])
            for column in columns
        }
        _campaign_prep_validate_origin_contract(row, i + 2)
        release_date_value = row.get(release_date_column, "") if release_date_column is not None else ""
        parsed_release_date = _campaign_prep_parse_release_date(release_date_value)
        release_date_invalid = parsed_release_date is None
        rows_for_segmentation = [copy.deepcopy(row)]
        if split_multiple_emails and email_column is not None:
            tokens = _campaign_prep_email_tokens(row.get(email_column, ""))
            if len(tokens) > 1:
                rows_for_segmentation = []
                for token in tokens:
                    split_row = copy.deepcopy(row)
                    split_row[email_column] = token
                    rows_for_segmentation.append(split_row)
            elif len(tokens) == 1:
                split_row = copy.deepcopy(row)
                split_row[email_column] = tokens[0]
                rows_for_segmentation = [split_row]

        for final_row in rows_for_segmentation:
            location_segment = "Inside_VIC" if _campaign_prep_inside_vic(final_row.get(location_column, "")) else "Outside_VIC"
            if _campaign_prep_truthy(final_row.get(triplej_column, "")):
                playback_segment = "Played_TripleJ"
            elif _campaign_prep_truthy(final_row.get(unearthed_column, "")):
                playback_segment = "Played_Unearthed"
            else:
                playback_segment = "Neither"
            recency_bucket = _campaign_prep_recency_bucket(parsed_release_date, run_reference_date)
            segment_name = _campaign_prep_segment_name(location_segment, playback_segment)

            if remove_rows_without_emails:
                if email_column is None:
                    skipped_rows.append(
                        _campaign_prep_skipped_row(
                            final_row,
                            columns_by_lower,
                            email_column,
                            segment_name,
                            recency_bucket,
                            "missing_email_column",
                        )
                    )
                    continue
                if not _campaign_prep_has_email_value(final_row.get(email_column, "")):
                    skipped_rows.append(
                        _campaign_prep_skipped_row(
                            final_row,
                            columns_by_lower,
                            email_column,
                            segment_name,
                            recency_bucket,
                            "missing_email",
                        )
                    )
                    continue

            prepared_rows.append(
                (
                    final_row,
                    {
                        "parsed_release_date": parsed_release_date,
                        "release_date_invalid": release_date_invalid,
                        "release_date_raw": release_date_value,
                        "region": location_segment,
                        "radio_bucket": playback_segment,
                        "recency_bucket": recency_bucket,
                    },
                )
            )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    sorted_prepared_rows = _campaign_prep_sort_buffer_by_release_date(
        prepared_rows,
        release_date_sort,
        release_date_column=release_date_column,
    )

    processed_master_output_rows: List[dict] = []
    campaign_file_rows: Dict[str, List[dict]] = {}
    campaign_file_lineage_rows: Dict[str, List[dict]] = {}
    combined_file_rows: Dict[str, List[dict]] = {}
    combined_file_lineage_rows: Dict[str, List[dict]] = {}
    campaign_counts: Dict[str, int] = {}
    combined_counts: Dict[str, int] = {}
    for source_row, prepared_row in sorted_prepared_rows:
        recency_bucket = prepared_row["recency_bucket"]
        processed_row = dict(source_row)
        processed_row[CAMPAIGN_PREP_RECENCY_BUCKET_COLUMN] = recency_bucket
        export_row = _campaign_prep_export_row(processed_row, export_format, columns_by_lower, email_column)
        if export_format == "woodpecker":
            export_row = _campaign_prep_clean_woodpecker_export_row(export_row)
        export_row[CAMPAIGN_PREP_RECENCY_BUCKET_COLUMN] = recency_bucket
        filename = _campaign_prep_campaign_filename(
            prepared_row["region"],
            recency_bucket,
            prepared_row["radio_bucket"],
        )
        combined_filename = _campaign_prep_combined_segment_filename(
            prepared_row["region"],
            prepared_row["radio_bucket"],
        )
        processed_master_output_rows.append(processed_row)
        campaign_file_rows.setdefault(filename, []).append(
            {column: export_row.get(column, "") for column in output_columns}
        )
        campaign_file_lineage_rows.setdefault(filename, []).append(dict(source_row))
        campaign_counts[filename] = campaign_counts.get(filename, 0) + 1
        combined_file_rows.setdefault(combined_filename, []).append(
            {column: export_row.get(column, "") for column in output_columns}
        )
        combined_file_lineage_rows.setdefault(combined_filename, []).append(dict(source_row))
        combined_counts[combined_filename] = combined_counts.get(combined_filename, 0) + 1

    input_rows = len(sorted_prepared_rows) + len(skipped_rows)
    written_rows = len(processed_master_output_rows)
    skipped_row_count = len(skipped_rows)
    if input_rows != written_rows + skipped_row_count:
        raise RuntimeError(
            "Campaign Prep export accounting mismatch: "
            f"input_rows={input_rows} written_rows={written_rows} skipped_rows={skipped_row_count}"
        )

    diagnostics = _campaign_prep_build_diagnostics(
        sorted_prepared_rows,
        run_reference_date,
        release_date_column,
        both_release_date_columns_present,
    )
    diagnostics["input_rows"] = input_rows
    diagnostics["written_rows"] = written_rows
    diagnostics["skipped_rows"] = skipped_row_count
    _campaign_prep_log_diagnostics(diagnostics)
    print(
        "[Campaign Prep] Export accounting "
        f"input_rows={input_rows} written_rows={written_rows} skipped_rows={skipped_row_count}"
    )
    if skipped_row_count > 0:
        print("Export completed with skipped rows — review campaign_export_skipped_rows.csv")
    written_buckets = {
        filename.split("/", 1)[0]
        for filename, count in campaign_counts.items()
        if count and "/" in filename
    }
    if len(written_buckets) > 1:
        print("Export split across recency buckets — see campaign_export_summary.txt")

    diagnostics["ledger_sidecar_status"] = "not_applicable"
    diagnostics["ledger_sidecar_error"] = ""
    diagnostics["ledger_operation_reference"] = operation_reference
    diagnostics["ledger_created_at"] = export_created_at
    ledger_can_write = export_format == "woodpecker"
    if ledger_can_write:
        try:
            campaign_prep_sidecar.invalidate_existing_sidecar(output_path)
        except Exception as exc:
            ledger_can_write = False
            diagnostics["ledger_sidecar_status"] = "failed"
            diagnostics["ledger_sidecar_error"] = f"Could not invalidate prior ledger sidecar: {exc}"

    _campaign_prep_atomic_write_csv(
        output_path / CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME,
        processed_master_output_rows,
        processed_columns,
    )

    manifest_rows: List[dict] = [
        {
            "segment_name": "Processed_Master",
            "recency_bucket": "ALL",
            "rows_written": len(processed_master_output_rows),
            "output_file": CAMPAIGN_PREP_PROCESSED_MASTER_FILENAME,
        }
    ]
    result: Dict[str, int] = {}
    ledger_artifacts: List[dict] = []
    for filename in CAMPAIGN_PREP_OUTPUT_ORDER:
        rows = campaign_file_rows.get(filename)
        if not rows:
            continue
        _campaign_prep_atomic_write_csv(output_path / filename, rows, output_columns)
        bucket, basename = filename.split("/", 1)
        segment_name = basename[:-4]
        manifest_rows.append(
            {
                "segment_name": segment_name,
                "recency_bucket": bucket,
                "rows_written": len(rows),
                "output_file": filename,
            }
        )
        result[filename] = campaign_counts[filename]
        if export_format == "woodpecker":
            ledger_artifacts.append(
                {"filename": filename, "lineage_rows": campaign_file_lineage_rows[filename]}
            )

    for region in CAMPAIGN_PREP_REGION_SEGMENTS:
        for radio_bucket in CAMPAIGN_PREP_RADIO_BUCKETS:
            combined_filename = _campaign_prep_combined_segment_filename(region, radio_bucket)
            rows = combined_file_rows.get(combined_filename)
            if not rows:
                continue
            _campaign_prep_atomic_write_csv(output_path / combined_filename, rows, output_columns)
            manifest_rows.append(
                {
                    "segment_name": _campaign_prep_segment_name(region, radio_bucket),
                    "recency_bucket": "ALL",
                    "rows_written": len(rows),
                    "output_file": combined_filename,
                }
            )
            if export_format == "woodpecker":
                ledger_artifacts.append(
                    {"filename": combined_filename, "lineage_rows": combined_file_lineage_rows[combined_filename]}
                )

    _campaign_prep_atomic_write_csv(
        output_path / CAMPAIGN_PREP_SKIPPED_ROWS_FILENAME,
        skipped_rows,
        CAMPAIGN_PREP_SKIPPED_ROW_COLUMNS,
    )
    manifest_rows.append(
        {
            "segment_name": "Skipped_Rows",
            "recency_bucket": "ALL",
            "rows_written": len(skipped_rows),
            "output_file": CAMPAIGN_PREP_SKIPPED_ROWS_FILENAME,
        }
    )

    summary_lines: List[str] = []
    for region in CAMPAIGN_PREP_REGION_SEGMENTS:
        for radio_bucket in CAMPAIGN_PREP_RADIO_BUCKETS:
            segment_name = _campaign_prep_segment_name(region, radio_bucket)
            combined_filename = _campaign_prep_combined_segment_filename(region, radio_bucket)
            all_count = combined_counts.get(combined_filename, 0)
            if all_count == 0:
                continue
            if summary_lines:
                summary_lines.append("")
            summary_lines.append(segment_name)
            summary_lines.append(f"- ALL: {all_count}")
            for bucket in CAMPAIGN_PREP_RECENCY_BUCKETS:
                bucket_filename = _campaign_prep_campaign_filename(region, bucket, radio_bucket)
                summary_lines.append(f"- {bucket}: {campaign_counts.get(bucket_filename, 0)}")
    if summary_lines:
        summary_lines.append("")
    summary_lines.extend(
        [
            "Export accounting",
            f"- input_rows: {input_rows}",
            f"- written_rows: {written_rows}",
            f"- skipped_rows: {skipped_row_count}",
        ]
    )
    _campaign_prep_write_text_atomic(
        output_path / CAMPAIGN_PREP_SUMMARY_FILENAME,
        "\n".join(summary_lines) + "\n",
    )

    manifest_rows.append(
        {
            "segment_name": "Summary",
            "recency_bucket": "ALL",
            "rows_written": 0,
            "output_file": CAMPAIGN_PREP_SUMMARY_FILENAME,
        }
    )
    manifest_rows.append(
        {
            "segment_name": "Manifest",
            "recency_bucket": "ALL",
            "rows_written": len(manifest_rows) + 1,
            "output_file": CAMPAIGN_PREP_MANIFEST_FILENAME,
        }
    )
    _campaign_prep_atomic_write_csv(
        output_path / CAMPAIGN_PREP_MANIFEST_FILENAME,
        manifest_rows,
        ["segment_name", "recency_bucket", "rows_written", "output_file"],
    )
    if ledger_can_write and ledger_artifacts:
        try:
            campaign_prep_sidecar.write_campaign_export_sidecar(
                output_path,
                ledger_artifacts,
                operation_reference=operation_reference,
                created_at=export_created_at,
                source_dataset_reference=Path(input_csv_path).name,
            )
            diagnostics["ledger_sidecar_status"] = "written"
        except Exception as exc:
            diagnostics["ledger_sidecar_status"] = "failed"
            diagnostics["ledger_sidecar_error"] = str(exc)
            try:
                campaign_prep_sidecar.invalidate_existing_sidecar(output_path)
            except Exception:
                pass
            warning = f"Campaign CSVs were generated, but the Lead Engine ledger sidecar failed: {exc}"
            logging.getLogger(__name__).error(warning)
            print(warning)
    elif export_format == "woodpecker" and not ledger_artifacts and ledger_can_write:
        diagnostics["ledger_sidecar_status"] = "not_written_empty"
    return CampaignPrepResult(result, diagnostics=diagnostics)


class CampaignPrepTab(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(
            LM_PADDING_CONTAINER,
            LM_PADDING_CONTAINER,
            LM_PADDING_CONTAINER,
            LM_PADDING_CONTAINER,
        )
        layout.setSpacing(LM_SPACING_SECTION)

        config_group, config_layout = _lm_section("Job Configuration")
        self.input_csv_edit = QtWidgets.QLineEdit()
        self.input_csv_edit.setPlaceholderText("Select enriched/master CSV.")
        self.input_csv_edit.textChanged.connect(lambda text: self._update_path_tooltip(self.input_csv_edit, text))
        input_browse = QtWidgets.QPushButton("Browse...")
        input_browse.clicked.connect(self._browse_input_csv)
        config_layout.addLayout(_lm_row("Master CSV:", self.input_csv_edit, input_browse))

        self.output_dir_edit = QtWidgets.QLineEdit()
        self.output_dir_edit.setPlaceholderText("Select folder for campaign CSVs.")
        self.output_dir_edit.textChanged.connect(lambda text: self._update_path_tooltip(self.output_dir_edit, text))
        output_browse = QtWidgets.QPushButton("Browse...")
        output_browse.clicked.connect(self._browse_output_dir)
        config_layout.addLayout(_lm_row("Output folder:", self.output_dir_edit, output_browse))

        self.split_emails_checkbox = QtWidgets.QCheckBox("Split multiple emails into separate rows")
        self.split_emails_checkbox.setChecked(False)
        config_layout.addLayout(_lm_control_row(self.split_emails_checkbox))

        self.remove_rows_without_emails_checkbox = QtWidgets.QCheckBox("Remove rows without emails")
        self.remove_rows_without_emails_checkbox.setChecked(False)
        config_layout.addLayout(_lm_control_row(self.remove_rows_without_emails_checkbox))

        self.release_date_sort_combo = QtWidgets.QComboBox()
        self.release_date_sort_combo.addItem("None", "none")
        self.release_date_sort_combo.addItem("Ascending (Oldest → Newest)", "ascending")
        self.release_date_sort_combo.addItem("Descending (Newest → Oldest)", "descending")
        self.release_date_sort_combo.setCurrentIndex(0)
        config_layout.addLayout(_lm_row("Sort by Release Date:", self.release_date_sort_combo, add_stretch=True))

        self.export_format_combo = QtWidgets.QComboBox()
        self.export_format_combo.addItem("Lead Machine / Full Export", "lead_machine_full")
        self.export_format_combo.addItem("Woodpecker", "woodpecker")
        self.export_format_combo.addItem("Input Headers / Custom", "input_headers")
        self.export_format_combo.setCurrentIndex(0)
        config_layout.addLayout(_lm_row("Export format:", self.export_format_combo, add_stretch=True))
        layout.addWidget(config_group)

        output_group, output_layout = _lm_section("Run Output + Logs")
        self.generate_button = QtWidgets.QPushButton("Generate Campaign CSVs")
        self.generate_button.clicked.connect(self._generate_campaign_csvs)
        output_layout.addLayout(_lm_control_row(self.generate_button))

        self.summary_view = QtWidgets.QPlainTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        self.summary_view.setPlaceholderText("Generated file summary will appear here.")
        output_layout.addWidget(self.summary_view)
        layout.addWidget(output_group)
        _lm_scrolled_tab(self, layout)

    def _update_path_tooltip(self, widget: QtWidgets.QLineEdit, text: Optional[str] = None):
        tooltip_text = (text if text is not None else widget.text()).strip()
        widget.setToolTip(tooltip_text)

    def _set_line_edit_path(self, widget: QtWidgets.QLineEdit, text: str):
        widget.setText(text)
        self._update_path_tooltip(widget, text)
        widget.setCursorPosition(0)

    def _browse_input_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Master CSV",
            "",
            "CSV Files (*.csv);;All Files (*)",
        )
        if file_path:
            self._set_line_edit_path(self.input_csv_edit, file_path)

    def _browse_output_dir(self):
        folder_path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select Campaign Output Folder",
            "",
        )
        if folder_path:
            self._set_line_edit_path(self.output_dir_edit, folder_path)

    def _generate_campaign_csvs(self):
        input_csv_path = self.input_csv_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()
        if not input_csv_path:
            QtWidgets.QMessageBox.warning(self, "Campaign Prep", "Select a master CSV before generating campaign files.")
            return
        if not os.path.isfile(input_csv_path):
            QtWidgets.QMessageBox.warning(self, "Campaign Prep", f"Master CSV not found:\n{input_csv_path}")
            return
        if not output_dir:
            QtWidgets.QMessageBox.warning(self, "Campaign Prep", "Select an output folder before generating campaign files.")
            return

        self.generate_button.setEnabled(False)
        self.summary_view.setPlainText("Generating campaign CSVs...")
        QtWidgets.QApplication.processEvents()
        try:
            result = generate_campaign_csvs(
                input_csv_path,
                output_dir,
                split_multiple_emails=self.split_emails_checkbox.isChecked(),
                export_format=self.export_format_combo.currentData() or "lead_machine_full",
                remove_rows_without_emails=self.remove_rows_without_emails_checkbox.isChecked(),
                release_date_sort=self.release_date_sort_combo.currentData() or "none",
            )
            lines = [f"{filename}: {count} rows" for filename, count in result.items()]
            diagnostics = getattr(result, "diagnostics", {})
            if diagnostics.get("skipped_rows", 0) > 0:
                lines.append("")
                lines.append("Export completed with skipped rows — review campaign_export_skipped_rows.csv")
            written_buckets = {
                filename.split("/", 1)[0]
                for filename, count in result.items()
                if count and "/" in filename
            }
            if len(written_buckets) > 1:
                lines.append("")
                lines.append("Export split across recency buckets — see campaign_export_summary.txt")
            ledger_failed = diagnostics.get("ledger_sidecar_status") == "failed"
            if ledger_failed:
                lines.append("")
                lines.append(
                    "Campaign CSVs were generated, but Lead Engine ledger tracking failed: "
                    f"{diagnostics.get('ledger_sidecar_error', 'unknown error')}"
                )
            summary = "\n".join(lines)
            self.summary_view.setPlainText(summary)
            if ledger_failed:
                QtWidgets.QMessageBox.warning(self, "Campaign CSVs Generated With Warning", summary)
            else:
                QtWidgets.QMessageBox.information(self, "Campaign CSVs Generated", summary)
        except Exception as exc:
            message = f"Could not generate campaign CSVs:\n{exc}"
            self.summary_view.setPlainText(message)
            QtWidgets.QMessageBox.critical(self, "Campaign Prep Error", message)
        finally:
            self.generate_button.setEnabled(True)


class LeadVaultWorker(QtCore.QThread):
    finished_signal = QtCore.pyqtSignal(dict)
    error_signal = QtCore.pyqtSignal(str)

    def __init__(
        self,
        mode: str,
        source_path: str,
        header_overrides: Optional[Dict[str, str]] = None,
        ignored_headers: Optional[List[str]] = None,
        master_path: Optional[str] = None,
        duplicate_strategy: str = "update",
        parent=None,
    ):
        super().__init__(parent)
        self.mode = mode
        self.source_path = source_path
        self.header_overrides = dict(header_overrides or {})
        self.ignored_headers = list(ignored_headers or [])
        self.master_path = master_path or str(get_default_master_csv_path())
        self.duplicate_strategy = duplicate_strategy

    def run(self):
        try:
            if self.mode == "preview":
                result = preview_csv_import(
                    self.source_path,
                    header_overrides=self.header_overrides,
                    ignored_headers=self.ignored_headers,
                    master_path=self.master_path,
                )
            elif self.mode == "merge_preview":
                result = preview_csv_merge_counts(
                    self.source_path,
                    header_overrides=self.header_overrides,
                    ignored_headers=self.ignored_headers,
                    master_path=self.master_path,
                    duplicate_strategy="merge_consolidate",
                )
            elif self.mode == "import":
                result = merge_csv_into_master(
                    self.source_path,
                    header_overrides=self.header_overrides,
                    ignored_headers=self.ignored_headers,
                    master_path=self.master_path,
                    duplicate_strategy=self.duplicate_strategy,
                )
            else:
                raise ValueError(f"Unknown Lead Vault worker mode: {self.mode}")
            self.finished_signal.emit(result)
        except Exception as exc:
            self.error_signal.emit(str(exc))


class LeadVaultTab(QtWidgets.QWidget):
    IGNORE_OPTION = "Ignore column"
    _MAPPING_PRESETS_STATE_KEY = "mapping_presets"
    _SELECTED_MASTER_STATE_KEY = "selected_master_csv"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: Optional[LeadVaultWorker] = None
        self.preview_result: Optional[dict] = None
        self.merge_preview_result: Optional[dict] = None
        self._build_ui()
        self._restore_master_selection()

    def _build_ui(self):
        outer_layout = QtWidgets.QVBoxLayout()
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        files_group = QtWidgets.QGroupBox("Import Files")
        files_layout = QtWidgets.QGridLayout()
        files_layout.setHorizontalSpacing(10)
        files_layout.setVerticalSpacing(8)

        source_label = QtWidgets.QLabel("Import CSV:")
        self.source_edit = QtWidgets.QLineEdit()
        self.source_edit.setPlaceholderText("Select a CSV to preview and merge into the Lead Vault.")
        self.source_edit.textChanged.connect(self._reset_preview_state)
        self.source_edit.textChanged.connect(lambda text: self._update_path_tooltip(self.source_edit, text))
        self._update_path_tooltip(self.source_edit)
        source_browse = QtWidgets.QPushButton("Browse...")
        source_browse.clicked.connect(self._browse_csv)
        self.preview_button = QtWidgets.QPushButton("Preview Import")
        self.preview_button.clicked.connect(self._start_preview)
        files_layout.addWidget(source_label, 0, 0)
        files_layout.addWidget(self.source_edit, 0, 1)
        files_layout.addWidget(source_browse, 0, 2)
        files_layout.addWidget(self.preview_button, 0, 3)

        master_label = QtWidgets.QLabel("Master CSV:")
        self.master_selector = QtWidgets.QComboBox()
        self.master_selector.currentIndexChanged.connect(self._handle_master_selector_changed)
        self.create_master_button = QtWidgets.QPushButton("Create New")
        self.create_master_button.clicked.connect(self._create_master_csv)
        self.master_path_label = QtWidgets.QLineEdit()
        self.master_path_label.setReadOnly(True)
        self._set_line_edit_path(self.master_path_label, str(get_default_master_csv_path()))
        files_layout.addWidget(master_label, 1, 0)
        files_layout.addWidget(self.master_selector, 1, 1)
        files_layout.addWidget(self.create_master_button, 1, 2)
        files_layout.addWidget(self.master_path_label, 2, 1, 1, 3)
        files_group.setLayout(files_layout)
        layout.addWidget(files_group)

        overview_row = QtWidgets.QHBoxLayout()
        overview_row.setSpacing(12)

        summary_group = QtWidgets.QGroupBox("Lead Vault Summary")
        summary_layout = QtWidgets.QVBoxLayout()
        summary_layout.setSpacing(8)
        lead_vault_summary_controls = QtWidgets.QHBoxLayout()
        self.refresh_summary_button = QtWidgets.QPushButton("Refresh Summary")
        self.refresh_summary_button.clicked.connect(self._refresh_master_summary)
        lead_vault_summary_controls.addWidget(self.refresh_summary_button)
        lead_vault_summary_controls.addStretch()
        summary_layout.addLayout(lead_vault_summary_controls)

        self.master_summary_view = QtWidgets.QPlainTextEdit()
        self.master_summary_view.setReadOnly(True)
        self.master_summary_view.setMinimumHeight(110)
        self.master_summary_view.setMaximumHeight(180)
        self.master_summary_view.setPlaceholderText("Press Refresh Summary to inspect the current master dataset.")
        summary_layout.addWidget(self.master_summary_view)
        summary_group.setLayout(summary_layout)
        overview_row.addWidget(summary_group, 2)

        preview_group = QtWidgets.QGroupBox("Preview Mapping")
        preview_layout = QtWidgets.QGridLayout()
        preview_layout.setHorizontalSpacing(12)
        preview_layout.setVerticalSpacing(6)
        detected_label = QtWidgets.QLabel("Detected Headers")
        preview_layout.addWidget(detected_label, 0, 0)
        self.detected_headers_view = QtWidgets.QListWidget()
        self.detected_headers_view.setMinimumHeight(90)
        self.detected_headers_view.setMaximumHeight(120)
        self.detected_headers_view.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.detected_headers_view.setFocusPolicy(QtCore.Qt.NoFocus)
        self.detected_headers_view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.detected_headers_view.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Maximum,
        )
        preview_layout.addWidget(self.detected_headers_view, 1, 0)

        mapped_label = QtWidgets.QLabel("Auto-Mapped Columns")
        preview_layout.addWidget(mapped_label, 0, 1)
        self.mapped_headers_view = QtWidgets.QListWidget()
        self.mapped_headers_view.setMinimumHeight(90)
        self.mapped_headers_view.setMaximumHeight(120)
        self.mapped_headers_view.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.mapped_headers_view.setFocusPolicy(QtCore.Qt.NoFocus)
        self.mapped_headers_view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.mapped_headers_view.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Maximum,
        )
        preview_layout.addWidget(self.mapped_headers_view, 1, 1)

        preview_group.setLayout(preview_layout)
        overview_row.addWidget(preview_group, 3)
        layout.addLayout(overview_row)

        mapping_group = QtWidgets.QGroupBox("Manual Mapping For Unmapped Columns")
        mapping_layout = QtWidgets.QVBoxLayout()
        mapping_layout.setSpacing(8)
        controls = QtWidgets.QHBoxLayout()
        self.auto_map_known_button = QtWidgets.QPushButton("Auto Map Known")
        self.auto_map_known_button.setEnabled(False)
        self.auto_map_known_button.clicked.connect(self._auto_map_known_headers)
        controls.addWidget(self.auto_map_known_button)
        self.ignore_all_button = QtWidgets.QPushButton("Ignore All")
        self.ignore_all_button.setEnabled(False)
        self.ignore_all_button.clicked.connect(self._ignore_all_headers)
        controls.addWidget(self.ignore_all_button)
        self.save_mapping_preset_button = QtWidgets.QPushButton("Save Preset")
        self.save_mapping_preset_button.setEnabled(False)
        self.save_mapping_preset_button.clicked.connect(self._save_mapping_preset)
        controls.addWidget(self.save_mapping_preset_button)
        self.load_mapping_preset_button = QtWidgets.QPushButton("Load Preset")
        self.load_mapping_preset_button.setEnabled(False)
        self.load_mapping_preset_button.clicked.connect(self._load_mapping_preset)
        controls.addWidget(self.load_mapping_preset_button)
        controls.addStretch()
        self.import_button = QtWidgets.QPushButton("Run Lead Vault Import")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._start_import)
        self.import_mode_combo = QtWidgets.QComboBox()
        self.import_mode_combo.addItem("Append Only (existing)", "append_only")
        self.import_mode_combo.addItem("Merge + Consolidate", "merge_consolidate")
        controls.addWidget(self.import_mode_combo)
        controls.addWidget(self.import_button)
        mapping_layout.addLayout(controls)
        self.unmapped_table = QtWidgets.QTableWidget(0, 2)
        self.unmapped_table.setHorizontalHeaderLabels(["Incoming Header", "Map To"])
        self.unmapped_table.setMinimumHeight(200)
        unmapped_header = self.unmapped_table.horizontalHeader()
        unmapped_header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        unmapped_header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.unmapped_table.setColumnWidth(0, 280)
        self.unmapped_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        mapping_layout.addWidget(self.unmapped_table)
        mapping_group.setLayout(mapping_layout)
        layout.addWidget(mapping_group, 5)

        lower_row = QtWidgets.QHBoxLayout()
        lower_row.setSpacing(12)

        import_summary_group = QtWidgets.QGroupBox("Import Summary")
        import_summary_layout = QtWidgets.QVBoxLayout()
        import_summary_layout.setSpacing(8)
        self.summary_view = QtWidgets.QPlainTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setMinimumHeight(120)
        self.summary_view.setPlaceholderText("Preview and import results will appear here.")
        import_summary_layout.addWidget(self.summary_view)
        import_summary_group.setLayout(import_summary_layout)
        lower_row.addWidget(import_summary_group, 3)

        export_group = QtWidgets.QGroupBox("Lead Vault Export")
        export_group.setMinimumHeight(220)
        export_layout = QtWidgets.QVBoxLayout()
        export_layout.setSpacing(8)

        preset_row = QtWidgets.QHBoxLayout()
        preset_label = QtWidgets.QLabel("Preset:")
        self.preset_selector = QtWidgets.QComboBox()
        for preset in self._available_export_presets():
            self.preset_selector.addItem(str(preset.get("name", "")), preset)
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.preset_selector)
        preset_row.addStretch()
        export_layout.addLayout(preset_row)

        output_file_label = QtWidgets.QLabel("Output file:")
        export_layout.addWidget(output_file_label)

        export_row = QtWidgets.QHBoxLayout()
        self.export_output_path = QtWidgets.QLineEdit()
        self._last_suggested_export_path = self._default_export_output_path(self._selected_export_preset())
        self._set_line_edit_path(self.export_output_path, self._last_suggested_export_path)
        self.export_output_path.setPlaceholderText("Select where to write the export CSV.")
        self.export_output_path.textChanged.connect(lambda text: self._update_path_tooltip(self.export_output_path, text))
        self._update_path_tooltip(self.export_output_path)
        self.browse_export_button = QtWidgets.QPushButton("Browse...")
        self.browse_export_button.clicked.connect(self._browse_export_output)
        export_row.addWidget(self.export_output_path)
        export_row.addWidget(self.browse_export_button)
        export_layout.addLayout(export_row)
        self.preset_selector.currentIndexChanged.connect(self._handle_export_preset_changed)

        export_controls = QtWidgets.QHBoxLayout()
        self.generate_export_button = QtWidgets.QPushButton("Generate Export")
        self.generate_export_button.clicked.connect(self._generate_export)
        export_controls.addWidget(self.generate_export_button)
        export_controls.addStretch()
        export_layout.addLayout(export_controls)

        export_group.setLayout(export_layout)
        lower_row.addWidget(export_group, 4)
        layout.addLayout(lower_row, 3)

        content.setLayout(layout)
        scroll_area.setWidget(content)
        outer_layout.addWidget(scroll_area)
        self.setLayout(outer_layout)

    def _update_path_tooltip(self, widget: QtWidgets.QLineEdit, text: Optional[str] = None):
        tooltip_text = (text if text is not None else widget.text()).strip()
        widget.setToolTip(tooltip_text)

    def _set_line_edit_path(self, widget: QtWidgets.QLineEdit, text: str):
        widget.setText(text)
        self._update_path_tooltip(widget, text)
        widget.setCursorPosition(0)

    def _browse_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Lead Vault Import CSV",
            "",
            "CSV Files (*.csv);;All Files (*)",
        )
        if file_path:
            self._set_line_edit_path(self.source_edit, file_path)

    def _master_csv_directory(self) -> Path:
        return get_default_master_csv_path().parent

    def _lead_vault_state_path(self) -> Path:
        return self._master_csv_directory() / LEAD_VAULT_UI_STATE_FILENAME

    def _load_lead_vault_ui_state(self) -> Dict[str, object]:
        state_path = self._lead_vault_state_path()
        try:
            with open(state_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _persist_lead_vault_ui_state(self, payload: Dict[str, object]):
        state_path = self._lead_vault_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, state_path)

    def _available_master_csv_paths(self) -> List[Path]:
        master_dir = self._master_csv_directory()
        available_paths = sorted(
            [path for path in master_dir.glob("*.csv") if path.is_file()],
            key=lambda path: path.name.lower(),
        )
        default_path = get_default_master_csv_path()
        if default_path not in available_paths:
            available_paths.insert(0, default_path)
        return available_paths

    def _display_name_for_master_path(self, path: Path) -> str:
        if path == get_default_master_csv_path():
            return f"{path.name} (default)"
        return path.name

    @staticmethod
    def _validate_master_filename(filename: str) -> str:
        candidate = str(filename or "").strip()
        if not candidate:
            raise ValueError("Enter a CSV filename.")
        if candidate in {".", ".."} or Path(candidate).name != candidate:
            raise ValueError("Enter a filename only, not a folder path.")
        if not candidate.lower().endswith(".csv"):
            raise ValueError("Master CSV filenames must end with .csv.")
        return candidate

    @classmethod
    def _normalize_new_master_filename(cls, raw_name: str) -> str:
        candidate = str(raw_name or "").strip()
        if not candidate:
            raise ValueError("Enter a CSV filename.")
        if candidate in {".", ".."} or Path(candidate).name != candidate:
            raise ValueError("Enter a filename only, not a folder path.")
        stem = candidate[:-4] if candidate.lower().endswith(".csv") else candidate
        stem = re.sub(r"\s+", "_", stem.strip())
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
        stem = stem.strip("._-")
        if not stem:
            raise ValueError("Enter a valid CSV filename.")
        return cls._validate_master_filename(f"{stem}.csv")

    def _resolve_master_csv_path(self, filename: Optional[str] = None) -> Path:
        default_path = get_default_master_csv_path()
        candidate_name = filename if filename is not None else self.master_selector.currentData()
        try:
            safe_name = self._validate_master_filename(str(candidate_name or default_path.name))
        except ValueError:
            return default_path

        master_dir = self._master_csv_directory().resolve()
        resolved_path = (master_dir / safe_name).resolve()
        if resolved_path.parent != master_dir:
            return default_path
        return resolved_path

    def _refresh_master_selector(self, selected_filename: Optional[str] = None):
        available_paths = self._available_master_csv_paths()
        if not available_paths:
            available_paths = [get_default_master_csv_path()]
        target_path = self._resolve_master_csv_path(selected_filename)
        target_filename = target_path.name
        available_filenames = {path.name for path in available_paths}
        if target_filename not in available_filenames:
            target_path = get_default_master_csv_path()
            target_filename = target_path.name

        self.master_selector.blockSignals(True)
        self.master_selector.clear()
        selected_index = 0
        for index, path in enumerate(available_paths):
            self.master_selector.addItem(self._display_name_for_master_path(path), path.name)
            if path.name == target_filename:
                selected_index = index
        self.master_selector.setCurrentIndex(selected_index)
        self.master_selector.blockSignals(False)

    def _load_persisted_master_filename(self) -> str:
        default_filename = get_default_master_csv_path().name
        payload = self._load_lead_vault_ui_state()
        try:
            return self._validate_master_filename(str(payload.get(self._SELECTED_MASTER_STATE_KEY) or default_filename))
        except ValueError:
            return default_filename

    def _persist_selected_master_filename(self, filename: str):
        payload = self._load_lead_vault_ui_state()
        payload[self._SELECTED_MASTER_STATE_KEY] = self._validate_master_filename(filename)
        self._persist_lead_vault_ui_state(payload)

    def _load_mapping_presets_state(self) -> Dict[str, Dict[str, str]]:
        payload = self._load_lead_vault_ui_state()
        raw_presets = payload.get(self._MAPPING_PRESETS_STATE_KEY)
        if not isinstance(raw_presets, dict):
            return {}

        cleaned_presets: Dict[str, Dict[str, str]] = {}
        canonical_fields = set(get_canonical_master_schema())
        valid_targets = canonical_fields | {self.IGNORE_OPTION}
        for preset_name, raw_mapping in raw_presets.items():
            name = str(preset_name or "").strip()
            if not name or not isinstance(raw_mapping, dict):
                continue

            cleaned_mapping: Dict[str, str] = {}
            for raw_header, raw_target in raw_mapping.items():
                header_name = str(raw_header or "").strip()
                target_name = str(raw_target or "").strip()
                if not header_name or target_name not in valid_targets:
                    continue
                cleaned_mapping[header_name] = target_name
            cleaned_presets[name] = cleaned_mapping
        return cleaned_presets

    def _persist_mapping_presets_state(self, presets: Dict[str, Dict[str, str]]):
        payload = self._load_lead_vault_ui_state()
        payload[self._MAPPING_PRESETS_STATE_KEY] = dict(sorted(presets.items(), key=lambda item: item[0].lower()))
        self._persist_lead_vault_ui_state(payload)

    def _apply_master_selection(
        self,
        filename: Optional[str] = None,
        *,
        persist: bool = True,
        clear_views: bool = True,
    ):
        resolved_path = self._resolve_master_csv_path(filename)
        selected_filename = resolved_path.name
        if self.master_selector.findData(selected_filename) < 0:
            self._refresh_master_selector(selected_filename)
        index = self.master_selector.findData(selected_filename)
        if index >= 0 and self.master_selector.currentIndex() != index:
            self.master_selector.blockSignals(True)
            self.master_selector.setCurrentIndex(index)
            self.master_selector.blockSignals(False)

        previous_path = self.master_path_label.text().strip()
        resolved_text = str(resolved_path)
        self._set_line_edit_path(self.master_path_label, resolved_text)
        if persist:
            self._persist_selected_master_filename(selected_filename)
        if clear_views and previous_path != resolved_text:
            self._reset_preview_state()
            self.master_summary_view.clear()

    def _restore_master_selection(self):
        selected_filename = self._load_persisted_master_filename()
        self._refresh_master_selector(selected_filename)
        self._apply_master_selection(selected_filename, persist=False, clear_views=False)

    def _handle_master_selector_changed(self):
        self._apply_master_selection(self.master_selector.currentData())

    def _create_master_csv(self):
        raw_name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Create Lead Vault Master CSV",
            "New master CSV filename:",
            QtWidgets.QLineEdit.Normal,
            "",
        )
        if not ok:
            return

        try:
            filename = self._normalize_new_master_filename(raw_name)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Lead Vault", str(exc))
            return

        target_path = self._resolve_master_csv_path(filename)
        try:
            if target_path.exists():
                QtWidgets.QMessageBox.information(
                    self,
                    "Lead Vault",
                    f"Master CSV already exists:\n{target_path}",
                )
            else:
                ensure_master_csv_exists(target_path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Lead Vault", f"Could not create master CSV:\n{exc}")
            return

        self._refresh_master_selector(target_path.name)
        self._apply_master_selection(target_path.name)

    def _available_export_presets(self) -> List[dict]:
        presets = list(EXPORT_PRESETS.values())
        return presets or [WOODPECKER_EXPORT_PRESET]

    def _default_export_output_path(self, preset: Optional[dict] = None) -> str:
        selected_preset = preset or self._selected_export_preset()
        filename = str(selected_preset.get("filename_pattern") or WOODPECKER_EXPORT_PRESET["filename_pattern"])
        return str(get_default_master_csv_path().parent / filename)

    def _handle_export_preset_changed(self):
        suggested_path = self._default_export_output_path(self._selected_export_preset())
        current_path = self.export_output_path.text().strip()
        known_default_paths = {self._default_export_output_path(preset) for preset in self._available_export_presets()}
        if not current_path or current_path == self._last_suggested_export_path or current_path in known_default_paths:
            self._set_line_edit_path(self.export_output_path, suggested_path)
        self._last_suggested_export_path = suggested_path

    def _browse_export_output(self):
        current_path = self.export_output_path.text().strip() or self._default_export_output_path(self._selected_export_preset())
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Select Lead Vault export output CSV",
            current_path,
            "CSV Files (*.csv);;All Files (*)",
        )
        if file_path:
            self._set_line_edit_path(self.export_output_path, file_path)

    def _reset_preview_state(self):
        self.preview_result = None
        self.merge_preview_result = None
        self.detected_headers_view.clear()
        self.mapped_headers_view.clear()
        self.summary_view.clear()
        self.unmapped_table.setRowCount(0)
        self.import_button.setEnabled(False)
        self.auto_map_known_button.setEnabled(False)
        self.ignore_all_button.setEnabled(False)
        self.save_mapping_preset_button.setEnabled(False)
        self.load_mapping_preset_button.setEnabled(False)

    def _selected_export_preset(self) -> dict:
        return self.preset_selector.currentData() or WOODPECKER_EXPORT_PRESET

    def _refresh_master_summary(self):
        master_csv_path = str(self._resolve_master_csv_path())
        try:
            result = summarize_master_dataset(master_csv_path)
        except Exception as exc:
            self.master_summary_view.setPlainText(f"Lead Vault summary unavailable:\n{exc}")
            return
        self.master_summary_view.setPlainText(self._format_master_summary(result))

    def _start_preview(self):
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self, "Lead Vault", "A Lead Vault task is already running.")
            return
        source_path = self.source_edit.text().strip()
        if not source_path or not os.path.exists(source_path):
            QtWidgets.QMessageBox.warning(self, "Lead Vault", "Select a valid import CSV first.")
            return
        self.preview_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.auto_map_known_button.setEnabled(False)
        self.ignore_all_button.setEnabled(False)
        self.summary_view.setPlainText("Previewing import...")
        self._start_worker("preview", source_path, {}, [])

    def _start_import(self):
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.information(self, "Lead Vault", "A Lead Vault task is already running.")
            return
        if not self.preview_result:
            QtWidgets.QMessageBox.warning(self, "Lead Vault", "Preview the CSV before importing.")
            return
        source_path = self.source_edit.text().strip()
        if not source_path or not os.path.exists(source_path):
            QtWidgets.QMessageBox.warning(self, "Lead Vault", "The selected import CSV no longer exists.")
            return
        header_overrides, ignored_headers, unresolved_headers = self._collect_manual_mapping_state()
        if unresolved_headers:
            QtWidgets.QMessageBox.warning(
                self,
                "Lead Vault",
                "Resolve or ignore every unmapped column before importing.",
            )
            self._refresh_import_button()
            return
        import_mode = self.import_mode_combo.currentData() or "append_only"
        if import_mode == "merge_consolidate":
            self.preview_button.setEnabled(False)
            self.import_button.setEnabled(False)
            self.auto_map_known_button.setEnabled(False)
            self.ignore_all_button.setEnabled(False)
            self.summary_view.setPlainText("Running Merge + Consolidate preview...")
            self._start_worker(
                "merge_preview",
                source_path,
                header_overrides,
                ignored_headers,
                duplicate_strategy="merge_consolidate",
            )
            return
        else:
            duplicate_strategy = self._choose_duplicate_strategy(source_path, header_overrides, ignored_headers)
            if duplicate_strategy is None:
                self._refresh_import_button()
                return
        self.preview_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.auto_map_known_button.setEnabled(False)
        self.ignore_all_button.setEnabled(False)
        self.summary_view.setPlainText("Running Lead Vault import...")
        self._start_worker("import", source_path, header_overrides, ignored_headers, duplicate_strategy=duplicate_strategy)

    def _start_worker(
        self,
        mode: str,
        source_path: str,
        header_overrides: Dict[str, str],
        ignored_headers: List[str],
        duplicate_strategy: str = "update",
    ):
        self._stop_worker()
        self.worker = LeadVaultWorker(
            mode=mode,
            source_path=source_path,
            header_overrides=header_overrides,
            ignored_headers=ignored_headers,
            master_path=str(self._resolve_master_csv_path()),
            duplicate_strategy=duplicate_strategy,
        )
        if mode == "merge_preview":
            finished_handler = self._handle_merge_preview_finished
        elif mode == "preview":
            finished_handler = self._handle_preview_finished
        else:
            finished_handler = self._handle_import_finished
        self.worker.finished_signal.connect(
            finished_handler
        )
        self.worker.error_signal.connect(self._handle_worker_error)
        self.worker.start()

    def _choose_duplicate_strategy(
        self,
        source_path: str,
        header_overrides: Dict[str, str],
        ignored_headers: List[str],
    ) -> Optional[str]:
        try:
            preview = preview_csv_merge_counts(
                source_path,
                header_overrides=header_overrides,
                ignored_headers=ignored_headers,
                master_path=str(self._resolve_master_csv_path()),
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Lead Vault",
                f"Could not evaluate duplicate contacts before import:\n{exc}",
            )
            return None

        duplicate_count = int(preview.get("rows_duplicates_detected", 0) or 0)
        if duplicate_count <= 0:
            return "update"

        message_box = QtWidgets.QMessageBox(self)
        message_box.setIcon(QtWidgets.QMessageBox.Question)
        message_box.setWindowTitle("Lead Vault")
        message_box.setText(f"Duplicates detected: {duplicate_count} existing contacts found.")
        message_box.setInformativeText(
            "How would you like to proceed?"
            "\n\n1. Update existing contacts (recommended)"
            "\n2. Skip duplicates (keep existing data unchanged)"
            "\n3. Keep both (allow duplicates)"
        )
        update_button = message_box.addButton(
            "Update existing contacts",
            QtWidgets.QMessageBox.AcceptRole,
        )
        skip_button = message_box.addButton(
            "Skip duplicates",
            QtWidgets.QMessageBox.ActionRole,
        )
        keep_both_button = message_box.addButton(
            "Keep both",
            QtWidgets.QMessageBox.ActionRole,
        )
        cancel_button = message_box.addButton(QtWidgets.QMessageBox.Cancel)
        message_box.setDefaultButton(update_button)
        message_box.exec_()

        clicked_button = message_box.clickedButton()
        if clicked_button == update_button:
            return "update"
        if clicked_button == skip_button:
            return "skip"
        if clicked_button == keep_both_button:
            return "keep_both"
        if clicked_button == cancel_button:
            self.summary_view.setPlainText("Import cancelled.")
        return None

    def _handle_preview_finished(self, result: dict):
        self.preview_result = result
        self.detected_headers_view.clear()
        self.detected_headers_view.addItems(result.get("detected_headers", []))
        mapped_lines = [
            f"{raw_header} -> {canonical_field}"
            for raw_header, canonical_field in sorted(result.get("mapped_headers", {}).items())
        ]
        self.mapped_headers_view.clear()
        self.mapped_headers_view.addItems(mapped_lines)
        self._populate_unmapped_table(result.get("unmapped_headers", []))
        self.summary_view.setPlainText(self._format_summary(result, preview_only=True))
        self.preview_button.setEnabled(True)
        self._stop_worker()
        self._refresh_import_button()

    def _handle_import_finished(self, result: dict):
        self.preview_result = result
        self.summary_view.setPlainText(self._format_summary(result, preview_only=False))
        self.preview_button.setEnabled(True)
        self._stop_worker()
        self._refresh_import_button()

    def _handle_merge_preview_finished(self, result: dict):
        self.merge_preview_result = result
        self.summary_view.setPlainText(self._format_merge_preview_summary(result))
        self.preview_button.setEnabled(True)
        self._stop_worker()
        self._refresh_import_button()
        if result.get("unmapped_headers"):
            QtWidgets.QMessageBox.warning(self, "Lead Vault", "Resolve or ignore every unmapped column before merging.")
            return
        dialog = self._build_merge_preview_dialog(result)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            self.summary_view.setPlainText("Merge preview cancelled. No changes made.")
            self.merge_preview_result = None
            return
        try:
            confirmed = confirm_csv_merge_preview(
                result,
                preview_session_id=str(result.get("preview_session_id") or ""),
            )
        except Exception as exc:
            self.summary_view.setPlainText(
                self._format_merge_preview_summary(result)
                + f"\n\nMerge blocked: {exc}\nRe-run preview before confirming."
            )
            QtWidgets.QMessageBox.warning(self, "Lead Vault", f"Merge blocked:\n{exc}\n\nRe-run preview before confirming.")
            self.merge_preview_result = None
            return
        self._handle_import_finished(confirmed)

    def _build_merge_preview_dialog(self, result: dict) -> QtWidgets.QDialog:
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Merge + Consolidate Preview")
        dialog.setMinimumSize(1000, 650)

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header_label = QtWidgets.QLabel("Merge preview is ready.")
        header_font = header_label.font()
        header_font.setPointSize(header_font.pointSize() + 2)
        header_font.setBold(True)
        header_label.setFont(header_font)
        layout.addWidget(header_label)

        summary_group = QtWidgets.QGroupBox("Summary")
        summary_layout = QtWidgets.QGridLayout(summary_group)
        summary_layout.setColumnStretch(1, 1)
        for row_index, (label, value) in enumerate(self._merge_preview_summary_items(result)):
            name_label = QtWidgets.QLabel(label)
            value_label = QtWidgets.QLabel(self._preview_cell_text(value))
            value_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            summary_layout.addWidget(name_label, row_index, 0)
            summary_layout.addWidget(value_label, row_index, 1)
        layout.addWidget(summary_group)

        upgrade_rows = result.get("merge_preview_upgrade_rows", []) or []
        total_upgrades = self._merge_preview_count_value(result, "UPGRADE")
        displayed_rows = list(upgrade_rows[:100])

        total_upgrades_int = self._preview_count_as_int(total_upgrades)
        if total_upgrades_int == 0:
            table_message = "No upgrade rows."
        elif total_upgrades_int > 100:
            table_message = f"Showing first 100 of {self._preview_cell_text(total_upgrades)} upgrade rows."
        else:
            table_message = ""
        table_message_label = QtWidgets.QLabel(table_message)
        layout.addWidget(table_message_label)

        table = QtWidgets.QTableWidget(0, 8)
        table.setObjectName("mergePreviewUpgradeTable")
        table.setHorizontalHeaderLabels(
            [
                "Artist",
                "Key",
                "Existing Email",
                "Incoming Email",
                "Existing Email_All",
                "Incoming Email_All",
                "Existing Score",
                "Incoming Score",
            ]
        )
        table.setSortingEnabled(False)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        table.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.AdjustIgnored)
        table.verticalHeader().setVisible(False)

        table.setUpdatesEnabled(False)
        table.setRowCount(len(displayed_rows))
        field_names = [
            ("artist_name",),
            ("key",),
            ("existing_email",),
            ("incoming_email",),
            ("existing_email_all", "existing_email_all_count"),
            ("incoming_email_all", "incoming_email_all_count"),
            ("existing_score",),
            ("incoming_score",),
        ]
        for row_index, row in enumerate(displayed_rows):
            row_data = row if isinstance(row, dict) else {}
            for column_index, names in enumerate(field_names):
                table.setItem(
                    row_index,
                    column_index,
                    QtWidgets.QTableWidgetItem(self._first_preview_cell_value(row_data, names)),
                )
        table.setUpdatesEnabled(True)

        table_header = table.horizontalHeader()
        table_header.setStretchLastSection(False)
        for column_index in range(table.columnCount()):
            table_header.setSectionResizeMode(column_index, QtWidgets.QHeaderView.Interactive)
        table_header.setSectionResizeMode(6, QtWidgets.QHeaderView.Fixed)
        table_header.setSectionResizeMode(7, QtWidgets.QHeaderView.Fixed)
        column_widths = [180, 180, 220, 220, 170, 170, 95, 95]
        for column_index, width in enumerate(column_widths):
            table.setColumnWidth(column_index, width)

        layout.addWidget(table, 1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        confirm_button = buttons.addButton("Confirm Merge", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel_button = buttons.button(QtWidgets.QDialogButtonBox.Cancel)
        cancel_button.setText("Cancel")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        confirm_button.setDefault(True)
        layout.addWidget(buttons)
        return dialog

    def _handle_worker_error(self, message: str):
        self.preview_button.setEnabled(True)
        self._stop_worker()
        self.import_button.setEnabled(False)
        self.summary_view.setPlainText(f"Lead Vault task failed:\n{message}")
        QtWidgets.QMessageBox.warning(self, "Lead Vault", message or "Lead Vault task failed safely.")

    def _populate_unmapped_table(self, unmapped_headers: List[str]):
        self.unmapped_table.setRowCount(len(unmapped_headers))
        canonical_fields = get_canonical_master_schema()
        for row_index, header in enumerate(unmapped_headers):
            self.unmapped_table.setItem(row_index, 0, QtWidgets.QTableWidgetItem(header))
            combo = QtWidgets.QComboBox()
            combo.addItem("Select mapping...", "")
            combo.addItem(self.IGNORE_OPTION, self.IGNORE_OPTION)
            for canonical_field in canonical_fields:
                combo.addItem(canonical_field, canonical_field)
            combo.currentIndexChanged.connect(self._refresh_import_button)
            self.unmapped_table.setCellWidget(row_index, 1, combo)

    def _iter_unmapped_mapping_rows(self):
        for row_index in range(self.unmapped_table.rowCount()):
            header_item = self.unmapped_table.item(row_index, 0)
            combo = self.unmapped_table.cellWidget(row_index, 1)
            header_name = header_item.text() if header_item else ""
            if not header_name or combo is None:
                continue
            yield header_name, combo

    def _set_mapping_combo_value(self, combo: QtWidgets.QComboBox, value: str):
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(value)
        if index >= 0 and combo.currentIndex() != index:
            combo.setCurrentIndex(index)

    def _current_mapping_preset_payload(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for header_name, combo in self._iter_unmapped_mapping_rows():
            selected_value = str(combo.currentData() or "").strip()
            if selected_value:
                mapping[header_name] = selected_value
        return mapping

    def _apply_mapping_preset_payload(self, preset_mapping: Dict[str, str]) -> int:
        applied_count = 0
        for header_name, combo in self._iter_unmapped_mapping_rows():
            target_value = str(preset_mapping.get(header_name) or "").strip()
            if not target_value:
                continue
            previous_value = str(combo.currentData() or "")
            self._set_mapping_combo_value(combo, target_value)
            if str(combo.currentData() or "") == target_value and previous_value != target_value:
                applied_count += 1
        self._refresh_import_button()
        return applied_count

    def _save_mapping_preset(self):
        if self.unmapped_table.rowCount() <= 0:
            QtWidgets.QMessageBox.information(self, "Lead Vault", "Preview a CSV with mapping rows before saving a preset.")
            return

        preset_name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Save Mapping Preset",
            "Preset name:",
            QtWidgets.QLineEdit.Normal,
            "",
        )
        if not ok:
            return

        safe_name = str(preset_name or "").strip()
        if not safe_name:
            QtWidgets.QMessageBox.warning(self, "Lead Vault", "Enter a preset name.")
            return

        presets = self._load_mapping_presets_state()
        if safe_name in presets:
            overwrite = QtWidgets.QMessageBox.question(
                self,
                "Lead Vault",
                f"Replace existing preset '{safe_name}'?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if overwrite != QtWidgets.QMessageBox.Yes:
                return

        presets[safe_name] = self._current_mapping_preset_payload()
        self._persist_mapping_presets_state(presets)
        QtWidgets.QMessageBox.information(
            self,
            "Lead Vault",
            f"Saved preset '{safe_name}' with {len(presets[safe_name])} mapped header(s).",
        )
        self._refresh_import_button()

    def _load_mapping_preset(self):
        if self.unmapped_table.rowCount() <= 0:
            QtWidgets.QMessageBox.information(self, "Lead Vault", "Preview a CSV with mapping rows before loading a preset.")
            return

        presets = self._load_mapping_presets_state()
        if not presets:
            QtWidgets.QMessageBox.information(self, "Lead Vault", "No mapping presets saved yet.")
            return

        preset_names = sorted(presets.keys(), key=str.lower)
        preset_name, ok = QtWidgets.QInputDialog.getItem(
            self,
            "Load Mapping Preset",
            "Preset:",
            preset_names,
            0,
            False,
        )
        if not ok:
            return

        safe_name = str(preset_name or "").strip()
        if not safe_name:
            return

        applied_count = self._apply_mapping_preset_payload(presets.get(safe_name, {}))
        QtWidgets.QMessageBox.information(
            self,
            "Lead Vault",
            f"Loaded preset '{safe_name}'. Applied {applied_count} header mapping(s) to the current table.",
        )

    def _ignore_all_headers(self):
        for _, combo in self._iter_unmapped_mapping_rows():
            self._set_mapping_combo_value(combo, self.IGNORE_OPTION)
        self._refresh_import_button()

    def _auto_map_known_headers(self):
        for header_name, combo in self._iter_unmapped_mapping_rows():
            resolved_target = map_headers_to_canonical([header_name]).get(header_name) or self.IGNORE_OPTION
            self._set_mapping_combo_value(combo, resolved_target)
        self._refresh_import_button()

    def _collect_manual_mapping_state(self) -> Tuple[Dict[str, str], List[str], List[str]]:
        overrides: Dict[str, str] = {}
        ignored_headers: List[str] = []
        unresolved_headers: List[str] = []
        for header_name, combo in self._iter_unmapped_mapping_rows():
            selected_value = combo.currentData() or ""
            if not selected_value:
                unresolved_headers.append(header_name)
                continue
            if selected_value == self.IGNORE_OPTION:
                ignored_headers.append(header_name)
                continue
            overrides[header_name] = str(selected_value)
        return overrides, ignored_headers, unresolved_headers

    def _refresh_import_button(self):
        has_mapping_rows = self.unmapped_table.rowCount() > 0
        controls_enabled = bool(self.preview_result) and has_mapping_rows and not bool(
            getattr(self.worker, "isRunning", lambda: False)()
        )
        self.auto_map_known_button.setEnabled(
            controls_enabled
        )
        self.ignore_all_button.setEnabled(
            controls_enabled
        )
        self.save_mapping_preset_button.setEnabled(controls_enabled)
        self.load_mapping_preset_button.setEnabled(controls_enabled)
        if not self.preview_result or bool(getattr(self.worker, "isRunning", lambda: False)()):
            self.import_button.setEnabled(False)
            return
        _, _, unresolved_headers = self._collect_manual_mapping_state()
        self.import_button.setEnabled(not unresolved_headers)

    def _format_merge_preview_summary(self, result: dict) -> str:
        return "\n".join(f"{label} {self._preview_cell_text(value)}" for label, value in self._merge_preview_summary_items(result))

    def _format_merge_confirm_summary(self, result: dict) -> str:
        return "Merge confirmed.\n\n" + self._format_merge_preview_summary(result)

    def _merge_preview_summary_items(self, result: dict) -> List[Tuple[str, object]]:
        return [
            ("New artists:", self._merge_preview_count_value(result, "NEW")),
            ("Upgrades:", self._merge_preview_count_value(result, "UPGRADE")),
            ("Kept existing:", self._merge_preview_count_value(result, "KEEP_EXISTING")),
            ("Unchanged:", self._merge_preview_count_value(result, "UNCHANGED")),
            ("Final row count:", result.get("rows_final", "")),
        ]

    def _merge_preview_count_value(self, result: dict, key: str) -> object:
        counts = result.get("merge_preview_counts", {}) or {}
        return counts.get(key, "")

    def _preview_cell_text(self, value: object) -> str:
        if value is None:
            return ""
        return str(value)

    def _first_preview_cell_value(self, row: dict, names: Tuple[str, ...]) -> str:
        for name in names:
            if name in row:
                return self._preview_cell_text(row.get(name))
        return ""

    def _preview_count_as_int(self, value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _format_summary(self, result: dict, preview_only: bool) -> str:
        if not preview_only and result.get("duplicate_strategy") == "merge_consolidate":
            return self._format_merge_confirm_summary(result)
        lines = [
            f"Source: {result.get('source_path', '')}",
            f"Master: {result.get('master_path', '')}",
            f"Rows read: {result.get('row_count', 0)}",
        ]
        if preview_only:
            lines.append(f"Detected headers: {len(result.get('detected_headers', []))}")
            lines.append(f"Auto-mapped headers: {len(result.get('mapped_headers', {}))}")
            lines.append(f"Unmapped headers: {len(result.get('unmapped_headers', []))}")
        else:
            lines.extend(
                [
                    "",
                    "Import Summary:",
                    f"Total rows processed: {result.get('row_count', 0)}",
                    f"New contacts added: {result.get('rows_added', 0)}",
                    f"Existing contacts updated: {result.get('rows_updated', 0)}",
                    f"New artists added: {result.get('rows_added_new', result.get('rows_added', 0))}",
                    f"Existing artists upgraded: {result.get('rows_replaced', result.get('rows_updated', 0))}",
                    f"Existing artists unchanged: {result.get('rows_kept_existing', result.get('rows_skipped_duplicates', 0))}",
                    f"Total final row count: {result.get('rows_final', '')}",
                    f"Duplicates skipped: {result.get('rows_skipped_duplicates', 0)}",
                    f"Duplicates kept: {result.get('rows_kept_duplicates', 0)}",
                    f"Duplicates detected: {result.get('rows_duplicates_detected', 0)}",
                    f"Rows unresolved mapping: {result.get('rows_unresolved_mapping', 0)}",
                    f"Rows ambiguous: {result.get('rows_ambiguous', 0)}",
                    f"Rows errors: {result.get('rows_errors', 0)}",
                ]
            )
        warnings = result.get("warnings", []) or []
        if warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(str(item) for item in warnings)
        return "\n".join(lines)

    def _format_master_summary(self, result: dict) -> str:
        lines = [
            f"Total leads: {result.get('total_rows', 0)}",
            f"Leads with email: {result.get('rows_with_email', 0)}",
            f"Needs review: {result.get('needs_review', 0)}",
            "",
            "Sources",
        ]
        sources = result.get("sources", {}) or {}
        if sources:
            lines.extend(f"{source_name}: {count}" for source_name, count in sources.items())
        else:
            lines.append("No sources found.")
        return "\n".join(lines)

    def _generate_export(self):
        master_csv_path = self._resolve_master_csv_path()
        if not master_csv_path.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "Lead Vault Export",
                f"Master CSV not found:\n{master_csv_path}",
            )
            return

        output_path = self.export_output_path.text().strip()
        if not output_path:
            QtWidgets.QMessageBox.warning(
                self,
                "Lead Vault Export",
                "Select an output CSV path before generating an export.",
            )
            return

        preset = self._selected_export_preset()
        try:
            result = export_with_preset(preset, master_csv_path, Path(output_path))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Lead Vault Export",
                f"Export failed:\n{exc}",
            )
            return

        QtWidgets.QMessageBox.information(
            self,
            "Lead Vault Export",
            "\n".join(
                [
                    "Export complete",
                    "",
                    f"Preset: {result.get('preset', '')}",
                    f"Rows read: {result.get('rows_read', 0)}",
                    f"Rows exported: {result.get('rows_exported', 0)}",
                    f"Rows skipped: {result.get('rows_skipped', 0)}",
                    "",
                    "Output file:",
                    str(result.get("output_file", "")),
                ]
            ),
        )

    def _stop_worker(self):
        worker = self.worker
        if not worker:
            return
        if worker.isRunning():
            try:
                worker.wait(2000)
            except Exception:
                try:
                    worker.terminate()
                    worker.wait(2000)
                except Exception:
                    pass
        self.worker = None

    def shutdown(self):
        self._stop_worker()


class CrossDirectoryEnricherTab(QtWidgets.QWidget):
    def __init__(self, parent=None, enricher_module=None):
        super().__init__(parent)
        self.enricher_module = enricher_module
        self.worker = None
        self.output_path = ""
        self.av_worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout()
        files_group, files_layout = _lm_section("Job Configuration")
        self.seed_edit = self._add_file_row(
            files_layout,
            "Seed CSV (Spotify):",
            "Select Spotify playlist CSV",
            required=True,
        )
        self.bandcamp_edit = self._add_file_row(
            files_layout,
            "Bandcamp CSV:",
            "Optional Bandcamp directory CSV",
        )
        self.soundcloud_edit = self._add_file_row(
            files_layout,
            "SoundCloud CSV:",
            "Optional SoundCloud directory CSV",
        )
        self.unearthed_edit = self._add_file_row(
            files_layout,
            "Unearthed CSV:",
            "Optional Triple J Unearthed CSV",
        )
        self.lastfm_edit = self._add_file_row(
            files_layout,
            "Last.fm CSV:",
            "Optional Last.fm directory CSV",
        )
        layout.addWidget(files_group)

        enrich_group, enrich_layout = _lm_section("Enrichment Controls")
        self.live_search_checkbox = QtWidgets.QCheckBox(
            "Try online directory search for unmatched artists"
        )
        self.live_search_checkbox.setChecked(True)
        self.live_search_checkbox.stateChanged.connect(self._toggle_live_controls)
        enrich_layout.addLayout(_lm_control_row(self.live_search_checkbox))
        self.auto_validate_checkbox = QtWidgets.QCheckBox("Enable Origin Auto-Validate after this run")
        self.auto_validate_checkbox.setChecked(False)
        enrich_layout.addLayout(_lm_control_row(self.auto_validate_checkbox))
        self.max_live_spin = QtWidgets.QSpinBox()
        self.max_live_spin.setRange(0, 1000)
        self.max_live_spin.setValue(50)
        enrich_layout.addLayout(_lm_row("Max live searches (0 = unlimited):", self.max_live_spin, add_stretch=True))
        layout.addWidget(enrich_group)

        run_group, run_layout = _lm_section("Run Settings")
        self.start_button = QtWidgets.QPushButton("Start Enrichment")
        self.start_button.clicked.connect(self.start_enrichment)
        run_layout.addLayout(_lm_control_row(self.start_button))
        layout.addWidget(run_group)

        output_group, output_layout = _lm_section("Run Output + Logs")
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        output_layout.addWidget(_lm_apply_control_sizing(self.progress_bar))
        self.log_console = QtWidgets.QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        output_layout.addWidget(self.log_console)
        layout.addWidget(output_group)
        if self.enricher_module is None:
            self.start_button.setEnabled(False)
            self.log_console.setPlainText(
                "cross_directory_enricher.py not found. Place the module in the project root."
            )
        _lm_scrolled_tab(self, layout)

    def _add_file_row(self, parent_layout, label_text, placeholder, required=False):
        line_edit = QtWidgets.QLineEdit()
        line_edit.setPlaceholderText(placeholder)
        browse_button = QtWidgets.QPushButton("Browse...")
        browse_button.clicked.connect(lambda: self._browse_csv(line_edit))
        parent_layout.addLayout(_lm_row(label_text, line_edit, browse_button))
        if required:
            line_edit.setProperty("required", True)
        return line_edit

    def _browse_csv(self, target_edit):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select CSV File",
            "",
            "CSV Files (*.csv);;All Files (*)",
        )
        if file_path:
            target_edit.setText(file_path)

    def _toggle_live_controls(self):
        enabled = self.live_search_checkbox.isChecked()
        self.max_live_spin.setEnabled(enabled)

    def start_enrichment(self):
        if self.enricher_module is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Enricher not available",
                "cross_directory_enricher.py not found.",
            )
            return
        if self.worker:
            self._append_log("Enrichment already running.")
            return
        seed_path = self.seed_edit.text().strip()
        if not seed_path:
            QtWidgets.QMessageBox.warning(
                self,
                "Missing seed CSV",
                "Please select a Spotify seed CSV.",
            )
            return
        if not os.path.exists(seed_path):
            QtWidgets.QMessageBox.warning(
                self,
                "Seed CSV not found",
                f"The selected seed CSV does not exist:\n{seed_path}",
            )
            return
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base, ext = os.path.splitext(seed_path)
        ext = ext or ".csv"
        self.output_path = f"{base}_enriched_{timestamp}{ext}"
        self.log_console.clear()
        self.progress_bar.setValue(0)
        enable_live = self.live_search_checkbox.isChecked()
        max_live = self.max_live_spin.value()
        self.worker = self.enricher_module.CrossDirectoryEnricherWorker(
            seed_csv_path=seed_path,
            output_csv_path=self.output_path,
            bandcamp_csv_path=self.bandcamp_edit.text().strip(),
            soundcloud_csv_path=self.soundcloud_edit.text().strip(),
            unearthed_csv_path=self.unearthed_edit.text().strip(),
            lastfm_csv_path=self.lastfm_edit.text().strip(),
            enable_live_search=enable_live,
            max_live_searches=max_live,
        )
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log_message.connect(self._append_log)
        self.worker.finished.connect(self._handle_finished)
        self.start_button.setEnabled(False)
        self._append_log("Starting enrichment...")
        self.worker.start()

    def _handle_finished(self, output_path: str):
        success = bool(output_path)
        self.start_button.setEnabled(True)
        worker = self.worker
        if worker:
            try:
                worker.wait()
            except Exception:
                pass
        self.worker = None
        if success:
            QtWidgets.QMessageBox.information(
                self,
                "Enrichment complete",
                f"Output written to:\n{output_path}",
            )
            if self.auto_validate_checkbox.isChecked():
                self._append_log("[Auto-Validate] Queueing origin validation...")
                target_path = _derive_origin_output_path(output_path)
                self._start_auto_validate(
                    output_path,
                    scope="all",
                    output_path=target_path,
                    auto_run=True,
                )
        else:
            QtWidgets.QMessageBox.warning(
                self,
                "Enrichment failed",
                "An error occurred during enrichment. Check the log for details.",
            )

    def _append_log(self, message: str):
        self.log_console.appendPlainText(message)
        scrollbar = self.log_console.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())

    def shutdown(self):
        worker = self.worker
        if not worker:
            return
        if worker.isRunning():
            try:
                finished = worker.wait(2000)
            except Exception:
                finished = False
            if not finished and worker.isRunning():
                try:
                    worker.terminate()
                    worker.wait(2000)
                except Exception:
                    pass
        self.worker = None
        self._stop_auto_validate_worker()

    def _start_auto_validate(
        self,
        csv_path: str,
        scope: str = "uncertain_only",
        output_path: Optional[str] = None,
        auto_run: bool = False,
    ):
        self._stop_auto_validate_worker()
        if not csv_path or not os.path.exists(csv_path):
            self._append_log(f"[Auto-Validate] Output CSV not found: {csv_path}")
            return
        target_path = output_path or _derive_origin_output_path(csv_path)
        self.av_worker = AutoValidateWorker(
            csv_path=csv_path,
            scope=scope,
            output_path=target_path,
            auto_run=auto_run,
        )
        self.av_worker.log_signal.connect(self._append_log)
        self.av_worker.finished_signal.connect(self._on_auto_validate_finished)
        self.av_worker.start()

    def _on_auto_validate_finished(self, output_path: str):
        if output_path:
            self._append_log(f"Origin Auto-Validate finished. Output: {output_path}")
        else:
            self._append_log("Origin Auto-Validate finished with errors.")
        self._stop_auto_validate_worker()

    def _stop_auto_validate_worker(self):
        worker = getattr(self, "av_worker", None)
        if not worker:
            return
        if worker.isRunning():
            try:
                worker.wait(2000)
            except Exception:
                try:
                    worker.terminate()
                    worker.wait(2000)
                except Exception:
                    pass
        self.av_worker = None


class AutoValidateTab(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout()
        path_row = QtWidgets.QHBoxLayout()
        path_label = QtWidgets.QLabel("CSV Path:")
        self.csv_path_edit = QtWidgets.QLineEdit()
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_csv)
        path_row.addWidget(path_label)
        path_row.addWidget(self.csv_path_edit)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        scope_layout = QtWidgets.QHBoxLayout()
        scope_label = QtWidgets.QLabel("Scope:")
        self.scope_uncertain = QtWidgets.QRadioButton("Uncertain rows only (recommended)")
        self.scope_all = QtWidgets.QRadioButton("All rows")
        self.scope_uncertain.setChecked(True)
        scope_group = QtWidgets.QButtonGroup(self)
        scope_group.addButton(self.scope_uncertain)
        scope_group.addButton(self.scope_all)
        scope_layout.addWidget(scope_label)
        scope_layout.addWidget(self.scope_uncertain)
        scope_layout.addWidget(self.scope_all)
        scope_layout.addStretch()
        layout.addLayout(scope_layout)

        self.run_button = QtWidgets.QPushButton("Run Origin Auto-Validate")
        self.run_button.clicked.connect(self._run_validation)
        layout.addWidget(self.run_button)

        self.log_console = QtWidgets.QPlainTextEdit()
        self.log_console.setReadOnly(True)
        layout.addWidget(self.log_console)
        self.setLayout(layout)

    def _browse_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select CSV File",
            "",
            "CSV Files (*.csv);;All Files (*)",
        )
        if file_path:
            self.csv_path_edit.setText(file_path)

    def _run_validation(self):
        if self.worker:
            self._append_log("Auto-Validate already running.")
            return
        csv_path = self.csv_path_edit.text().strip()
        if not csv_path or not os.path.exists(csv_path):
            QtWidgets.QMessageBox.warning(
                self,
                "CSV not found",
                "Please choose a valid CSV file.",
            )
            return
        scope = "all" if self.scope_all.isChecked() else "uncertain_only"
        self.log_console.clear()
        self._append_log(f"[Auto-Validate] Starting on {csv_path} (scope: {scope})...")
        self.run_button.setEnabled(False)
        target_path = _derive_origin_output_path(csv_path)
        self.worker = AutoValidateWorker(
            csv_path=csv_path,
            scope=scope,
            output_path=target_path,
            emit_start_log=False,
        )
        self.worker.log_signal.connect(self._append_log)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _append_log(self, message: str):
        self.log_console.appendPlainText(message)
        scrollbar = self.log_console.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())

    def _on_finished(self, output_path: str):
        if output_path:
            self._append_log(f"Origin Auto-Validate finished. Output: {output_path}")
        else:
            self._append_log("Origin Auto-Validate finished with errors.")
        self.run_button.setEnabled(True)
        self._stop_worker()

    def _stop_worker(self):
        worker = self.worker
        if not worker:
            return
        if worker.isRunning():
            try:
                worker.wait(2000)
            except Exception:
                try:
                    worker.terminate()
                    worker.wait(2000)
                except Exception:
                    pass
        self.worker = None

    def shutdown(self):
        self._stop_worker()
class NightModeWorker(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal(int)

    def __init__(self, command: list[str], workdir: str, env: Optional[dict] = None, secrets: Optional[list[str]] = None, parent=None):
        super().__init__(parent)
        self.command = command
        self.workdir = workdir
        self.env = env
        self.secrets = [s for s in (secrets or []) if s]
        self._process = None
        self._stop_requested = False

    def _mask(self, text: str) -> str:
        masked = text or ""
        for secret in self.secrets:
            if secret:
                masked = masked.replace(secret, "***")
        return masked

    def run(self):
        exit_code = -1
        try:
            pretty_cmd = " ".join(self.command)
            masked_cmd = self._mask(pretty_cmd)
            self.log_signal.emit(f"[Night Mode] Running: {masked_cmd}")
            self._process = subprocess.Popen(
                self.command,
                cwd=self.workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self.env,
            )
            stdout = self._process.stdout
            while stdout:
                if self._stop_requested:
                    break
                ready, _, _ = select.select([stdout], [], [], 0.5)
                if ready:
                    line = stdout.readline()
                    if line:
                        self.log_signal.emit(self._mask(line.rstrip("\n")))
                        continue
                if self._process.poll() is not None:
                    if ready:
                        for line in stdout:
                            self.log_signal.emit(self._mask(line.rstrip("\n")))
                    break
            if self._process:
                exit_code = self._process.wait()
        except Exception as exc:
            self.log_signal.emit(f"[Night Mode] Error: {self._mask(str(exc))}")
        self.finished_signal.emit(exit_code)

    def stop(self):
        self._stop_requested = True
        proc = self._process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


class FbDriverRecoveryWorker(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal(int, object)

    def __init__(self, export_csv: str, batch_size: int, in_place: bool, parent=None):
        super().__init__(parent)
        self.export_csv = export_csv
        self.batch_size = batch_size
        self.in_place = in_place

    def run(self):
        try:
            result = _run_fb_driver_recovery_chain(
                self.export_csv,
                batch_size=self.batch_size,
                in_place=self.in_place,
                logger_fn=self.log_signal.emit,
            )
        except Exception as exc:
            self.log_signal.emit(f"[FB Driver Recovery] Error: {exc}")
            self.finished_signal.emit(1, {"error": str(exc)})
            return
        self.finished_signal.emit(0, result)


class ManualFbRecoveryWorker(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal(int, object)

    def __init__(
        self,
        input_csv: str,
        *,
        recover_share: bool,
        recover_driver: bool,
        batch_size: int,
        in_place: bool,
        dry_run: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.input_csv = input_csv
        self.recover_share = recover_share
        self.recover_driver = recover_driver
        self.batch_size = batch_size
        self.in_place = in_place
        self.dry_run = dry_run

    def run(self):
        try:
            result = _run_manual_fb_recovery(
                self.input_csv,
                recover_share=self.recover_share,
                recover_driver=self.recover_driver,
                batch_size=self.batch_size,
                in_place=self.in_place,
                dry_run=self.dry_run,
                logger_fn=self.log_signal.emit,
            )
        except Exception as exc:
            self.log_signal.emit(f"[Manual FB Recovery] Error: {exc}")
            self.finished_signal.emit(1, {"error": str(exc)})
            return
        self.finished_signal.emit(0, result)


class ManualFbShareRecoveryWorker(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal(int, object)

    def __init__(
        self,
        input_csv: str,
        *,
        batch_size: int,
        in_place: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.input_csv = input_csv
        self.batch_size = batch_size
        self.in_place = in_place

    def run(self):
        try:
            result = _run_manual_fb_share_recovery(
                self.input_csv,
                batch_size=self.batch_size,
                in_place=self.in_place,
                logger_fn=self.log_signal.emit,
                popen_factory=subprocess.Popen,
            )
        except Exception as exc:
            self.log_signal.emit(f"[FB /share Manual Recovery] Error: {exc}")
            self.finished_signal.emit(1, {"error": str(exc)})
            return
        self.finished_signal.emit(0, result)


class NightModeTab(QtWidgets.QWidget):
    UNEARTHED_RESUME_MODE_OPTIONS = [
        ("Auto (resume from checkpoint or cursor)", "auto"),
        ("Continue from last position (cursor only)", "cursor"),
        ("Start fresh (ignore previous progress)", "fresh"),
        ("Selected cursor entry point", "selected"),
    ]
    UNEARTHED_SOURCE_MODE_OPTIONS = [
        ("Discover from website", False),
        ("Use saved URL index", True),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.recovery_worker = None
        self.jobs = []
        self._active_unearthed_index_path = _unearthed_artist_url_index_path()
        self._bootstrap_stage = None  # None | "headless" | "headed" | "final_headless"
        self._log_buffer: list[str] = []
        self._build_ui()
        self._progress_timer = QtCore.QTimer(self)
        self._progress_timer.setInterval(1500)
        self._progress_timer.timeout.connect(self._refresh_runtime_progress)
        self._progress_timer.start()
        self._refresh_runtime_progress()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout()

        job_group, job_layout = _lm_section("Job Configuration")
        self.config_path_edit = QtWidgets.QLineEdit("overnight_jobs.json")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_config)
        reload_btn = QtWidgets.QPushButton("Reload")
        reload_btn.clicked.connect(self._load_config_summary)
        job_layout.addLayout(_lm_row("Night Mode config (JSON):", self.config_path_edit, browse_btn, reload_btn))

        self.jobs_summary = QtWidgets.QPlainTextEdit()
        self.jobs_summary.setReadOnly(True)
        self.jobs_summary.setPlaceholderText("Load a config to view jobs (job_id, directory, mode, target_valid_leads, max_hours).")
        self.jobs_summary.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        job_layout.addWidget(self.jobs_summary)

        jobs_label = QtWidgets.QLabel("Configure Night Mode jobs here. You can add multiple directories to run overnight in sequence.")
        jobs_label.setWordWrap(True)
        job_layout.addLayout(_lm_control_row(jobs_label))

        self.jobs_table = QtWidgets.QTableWidget(0, 6)
        self.jobs_table.setHorizontalHeaderLabels(["#", "Directory", "Mode", "Input/Seed", "Target Leads", "Max Hours"])
        self.jobs_table.horizontalHeader().setStretchLastSection(True)
        self.jobs_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.jobs_table.setMinimumHeight(LM_TABLE_MIN_HEIGHT)
        job_layout.addWidget(self.jobs_table)

        add_btn = QtWidgets.QPushButton("Add job")
        edit_btn = QtWidgets.QPushButton("Edit job")
        remove_btn = QtWidgets.QPushButton("Remove job")
        save_btn = QtWidgets.QPushButton("Save config")
        load_btn = QtWidgets.QPushButton("Load config")
        add_btn.clicked.connect(self._add_job_dialog)
        edit_btn.clicked.connect(self._edit_job_dialog)
        remove_btn.clicked.connect(self._remove_selected_job)
        save_btn.clicked.connect(self._save_config_to_file)
        load_btn.clicked.connect(self._browse_config)
        job_layout.addLayout(_lm_control_row(add_btn, edit_btn, remove_btn, save_btn, load_btn))
        layout.addWidget(job_group)

        output_resume_group, output_resume_layout = _lm_section("Output / Resume Settings")
        self.export_mode_combo = QtWidgets.QComboBox()
        self.export_mode_combo.addItems(["both", "per_directory", "combined"])
        output_resume_layout.addLayout(_lm_row("Export mode:", self.export_mode_combo, add_stretch=True))
        self.unearthed_resume_mode_combo = QtWidgets.QComboBox()
        for label, value in self.UNEARTHED_RESUME_MODE_OPTIONS:
            self.unearthed_resume_mode_combo.addItem(label, value)
        self._set_unearthed_resume_mode("auto")
        output_resume_layout.addLayout(_lm_row("Unearthed Resume Mode:", self.unearthed_resume_mode_combo, add_stretch=True))
        self.unearthed_source_mode_combo = QtWidgets.QComboBox()
        for label, value in self.UNEARTHED_SOURCE_MODE_OPTIONS:
            self.unearthed_source_mode_combo.addItem(label, value)
        self._set_unearthed_source_mode(False)
        output_resume_layout.addLayout(_lm_row("Unearthed Source Mode:", self.unearthed_source_mode_combo, add_stretch=True))
        self.unearthed_index_combo = QtWidgets.QComboBox()
        self.unearthed_index_combo.currentIndexChanged.connect(self._handle_unearthed_index_selection)
        self.unearthed_duplicate_index_button = QtWidgets.QPushButton("Duplicate Index")
        self.unearthed_duplicate_index_button.clicked.connect(self._save_current_unearthed_index_as)
        output_resume_layout.addLayout(_lm_row("Index File:", self.unearthed_index_combo, self.unearthed_duplicate_index_button))
        self.resume_checkbox = QtWidgets.QCheckBox("Resume unfinished jobs")
        self.stop_on_failure_checkbox = QtWidgets.QCheckBox("Stop on first failure")
        output_resume_layout.addLayout(_lm_control_row(self.resume_checkbox, self.stop_on_failure_checkbox))

        self.unearthed_selected_cursor_label = QtWidgets.QLabel("Selected cursor checkpoint URL:")
        self.unearthed_selected_cursor_edit = QtWidgets.QLineEdit()
        self.unearthed_selected_cursor_edit.setPlaceholderText(
            "https://www.abc.net.au/triplejunearthed/artist/artist-slug"
        )
        output_resume_layout.addLayout(_lm_row_with_label_widget(self.unearthed_selected_cursor_label, self.unearthed_selected_cursor_edit))
        self.unearthed_start_index_label = QtWidgets.QLabel("Start from index position:")
        self.unearthed_start_index_edit = QtWidgets.QLineEdit()
        self.unearthed_start_index_edit.setPlaceholderText("Optional, e.g. 1500")
        self.unearthed_start_index_edit.setValidator(QtGui.QIntValidator(0, 100000000, self))
        output_resume_layout.addLayout(_lm_row_with_label_widget(self.unearthed_start_index_label, self.unearthed_start_index_edit))
        self.unearthed_resume_mode_combo.currentIndexChanged.connect(self._sync_unearthed_resume_controls)

        self.unearthed_index_status_label = QtWidgets.QLabel("")
        self.unearthed_index_status_label.setWordWrap(True)
        output_resume_layout.addLayout(_lm_control_row(self.unearthed_index_status_label))
        self.unearthed_source_mode_combo.currentIndexChanged.connect(self._sync_unearthed_source_mode_controls)
        layout.addWidget(output_resume_group)

        run_group, run_layout = _lm_section("🌙 Night Mode")
        self.fb_user_edit = QtWidgets.QLineEdit()
        self.fb_pass_edit = QtWidgets.QLineEdit()
        self.fb_pass_edit.setEchoMode(QtWidgets.QLineEdit.Password)

        self.fb_auto_resume_checkbox = QtWidgets.QCheckBox("Auto-resume FB after captcha")
        self.fb_cooldown_spin = QtWidgets.QSpinBox()
        self.fb_cooldown_spin.setRange(0, 36000)
        self.fb_cooldown_spin.setValue(600)
        self.fb_max_attempts_spin = QtWidgets.QSpinBox()
        self.fb_max_attempts_spin.setRange(0, 10)
        self.fb_max_attempts_spin.setValue(1)
        self.fb_max_rows_spin = QtWidgets.QSpinBox()
        self.fb_max_rows_spin.setRange(0, 100000)
        self.fb_max_rows_spin.setValue(0)

        self.fb_driver_recovery_checkbox = QtWidgets.QCheckBox("Retry failed Facebook attempts (recommended for best results)")
        self.fb_driver_recovery_checkbox.setChecked(False)
        self.fb_driver_recovery_batch_spin = QtWidgets.QSpinBox()
        self.fb_driver_recovery_batch_spin.setRange(1, 1000)
        self.fb_driver_recovery_batch_spin.setValue(40)
        self.fb_driver_recovery_copy_radio = QtWidgets.QRadioButton("Write recovered copy")
        self.fb_driver_recovery_copy_radio.setChecked(True)
        self.fb_driver_recovery_in_place_radio = QtWidgets.QRadioButton("In-place update")
        self.fb_driver_recovery_mode_group = QtWidgets.QButtonGroup(self)
        self.fb_driver_recovery_mode_group.addButton(self.fb_driver_recovery_copy_radio)
        self.fb_driver_recovery_mode_group.addButton(self.fb_driver_recovery_in_place_radio)

        self.fb_share_recovery_checkbox = QtWidgets.QCheckBox("Recover unresolved Facebook /share links (recommended for best results)")
        self.fb_share_recovery_checkbox.setChecked(False)
        self.fb_share_recovery_batch_spin = QtWidgets.QSpinBox()
        self.fb_share_recovery_batch_spin.setRange(1, 1000)
        self.fb_share_recovery_batch_spin.setValue(FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE)
        self.fb_share_recovery_copy_radio = QtWidgets.QRadioButton("Write recovered copy")
        self.fb_share_recovery_copy_radio.setChecked(True)
        self.fb_share_recovery_in_place_radio = QtWidgets.QRadioButton("In-place update")
        self.fb_share_recovery_mode_group = QtWidgets.QButtonGroup(self)
        self.fb_share_recovery_mode_group.addButton(self.fb_share_recovery_copy_radio)
        self.fb_share_recovery_mode_group.addButton(self.fb_share_recovery_in_place_radio)

        self.master_enrich_checkbox = QtWidgets.QCheckBox("Enrich leads using all available sources (recommended)")
        self.master_enrich_checkbox.setChecked(True)

        self.master_live_checkbox = QtWidgets.QCheckBox("Allow live searching for additional links during enrichment")
        self.master_live_checkbox.setChecked(True)
        self.master_live_spin = QtWidgets.QSpinBox()
        self.master_live_spin.setRange(0, 10000)
        self.master_live_spin.setValue(0)
        self.master_enrich_checkbox.stateChanged.connect(self._toggle_master_live_controls)
        self.master_live_checkbox.stateChanged.connect(self._toggle_master_live_controls)

        self.sc_meta_checkbox = QtWidgets.QCheckBox("Run SoundCloud metadata enricher (fills missing genre/date)")
        self.sc_meta_checkbox.setChecked(False)
        run_layout.addLayout(_lm_control_row(self.sc_meta_checkbox))

        self.run_root_edit = QtWidgets.QLineEdit()
        run_root_browse = QtWidgets.QPushButton("Browse...")
        run_root_browse.clicked.connect(self._browse_run_root)

        self.refresh_run_summary_button = QtWidgets.QPushButton("Refresh Summary")
        self.refresh_run_summary_button.clicked.connect(self._refresh_run_summary)
        self.run_summary_view = QtWidgets.QPlainTextEdit()
        self.run_summary_view.setReadOnly(True)
        self.run_summary_view.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        self.run_summary_view.setPlaceholderText(NIGHT_MODE_RUN_SUMMARY_PLACEHOLDER)

        self.start_button = QtWidgets.QPushButton("Run Night Mode")
        self.start_button.clicked.connect(self._start_night_mode)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.clicked.connect(self._stop_night_mode)
        self.stop_button.setEnabled(False)
        clear_log_btn = QtWidgets.QPushButton("Clear log")
        clear_log_btn.clicked.connect(self._clear_log)
        run_layout.addLayout(_lm_control_row(self.start_button))
        run_layout.addLayout(_lm_control_row(self.master_enrich_checkbox))
        run_layout.addLayout(_lm_row("Cooldown:", self.fb_cooldown_spin, add_stretch=True))

        advanced_container, self.advanced_toggle_button, _advanced_content, advanced_layout = _lm_collapsible_section("Advanced Settings")
        advanced_help = QtWidgets.QLabel("These settings are optional and usually not required.")
        advanced_help.setWordWrap(True)
        advanced_layout.addLayout(_lm_control_row(advanced_help))

        advanced_layout.addLayout(_lm_row("FB Username (optional):", self.fb_user_edit))
        advanced_layout.addLayout(_lm_row("FB Password (optional):", self.fb_pass_edit))
        advanced_layout.addLayout(_lm_control_row(self.fb_auto_resume_checkbox))
        advanced_layout.addLayout(_lm_row("Facebook processing limit (0 = no limit):", self.fb_max_rows_spin, add_stretch=True))

        optimisation_group, optimisation_layout = _lm_section("🛠 Post-Run Optimisation")
        optimisation_layout.addLayout(_lm_control_row(self.fb_driver_recovery_checkbox))
        self.fb_driver_recovery_options_widget = QtWidgets.QWidget()
        fb_driver_recovery_options_layout = QtWidgets.QVBoxLayout()
        fb_driver_recovery_options_layout.setContentsMargins(LM_LABEL_COLUMN_WIDTH, 0, 0, 0)
        fb_driver_recovery_options_layout.setSpacing(LM_SPACING_ROW)
        self.fb_driver_recovery_options_widget.setLayout(fb_driver_recovery_options_layout)
        fb_driver_recovery_options_layout.addLayout(_lm_row("Batch size:", self.fb_driver_recovery_batch_spin, add_stretch=True))
        fb_driver_recovery_options_layout.addLayout(_lm_control_row(self.fb_driver_recovery_copy_radio, self.fb_driver_recovery_in_place_radio))
        optimisation_layout.addWidget(self.fb_driver_recovery_options_widget)

        optimisation_layout.addLayout(_lm_control_row(self.fb_share_recovery_checkbox))
        self.fb_share_recovery_options_widget = QtWidgets.QWidget()
        fb_share_recovery_options_layout = QtWidgets.QVBoxLayout()
        fb_share_recovery_options_layout.setContentsMargins(LM_LABEL_COLUMN_WIDTH, 0, 0, 0)
        fb_share_recovery_options_layout.setSpacing(LM_SPACING_ROW)
        self.fb_share_recovery_options_widget.setLayout(fb_share_recovery_options_layout)
        fb_share_recovery_options_layout.addLayout(_lm_row("Batch size:", self.fb_share_recovery_batch_spin, add_stretch=True))
        fb_share_recovery_options_layout.addLayout(_lm_control_row(self.fb_share_recovery_copy_radio, self.fb_share_recovery_in_place_radio))
        optimisation_layout.addWidget(self.fb_share_recovery_options_widget)

        optimisation_help = QtWidgets.QLabel(
            "Improves results for difficult or partially failed cases.\n"
            "Not always required for clean runs."
        )
        optimisation_help.setWordWrap(True)
        optimisation_layout.addLayout(_lm_control_row(optimisation_help))
        advanced_layout.addWidget(optimisation_group)
        self.fb_driver_recovery_checkbox.stateChanged.connect(self._toggle_recovery_option_controls)
        self.fb_share_recovery_checkbox.stateChanged.connect(self._toggle_recovery_option_controls)

        enrichment_group, enrichment_layout = _lm_section("🔍 Enrichment Options")
        enrichment_layout.addLayout(_lm_control_row(self.master_live_checkbox))
        enrichment_layout.addLayout(_lm_row("Max live searches (0 = unlimited):", self.master_live_spin, add_stretch=True))
        enrichment_layout.addLayout(_lm_control_row(self.sc_meta_checkbox))
        advanced_layout.addWidget(enrichment_group)

        advanced_layout.addLayout(_lm_row("Run root (optional):", self.run_root_edit, run_root_browse))
        advanced_layout.addLayout(_lm_control_row(self.refresh_run_summary_button))
        advanced_layout.addWidget(self.run_summary_view)
        run_layout.addWidget(advanced_container)
        layout.addWidget(run_group)

        output_group, output_layout = _lm_section("Run Output + Logs")
        self.status_label = QtWidgets.QLabel("Status: idle")
        self.status_label.setWordWrap(True)
        output_layout.addLayout(_lm_control_row(self.status_label))
        self.runtime_progress_bar = QtWidgets.QProgressBar()
        self.runtime_progress_bar.setRange(0, 1000)
        self.runtime_progress_bar.setValue(0)
        output_layout.addWidget(_lm_apply_control_sizing(self.runtime_progress_bar))
        self.runtime_progress_detail = QtWidgets.QLabel("idle")
        self.runtime_progress_detail.setWordWrap(True)
        output_layout.addLayout(_lm_control_row(self.runtime_progress_detail))
        self.log_console = QtWidgets.QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        output_layout.addWidget(self.log_console)
        output_layout.addLayout(_lm_control_row(self.stop_button, clear_log_btn))
        layout.addWidget(output_group)

        _lm_scrolled_tab(self, layout)
        self._refresh_unearthed_index_selector()
        self._sync_unearthed_resume_controls()
        self._sync_unearthed_source_mode_controls()
        self._toggle_master_live_controls()
        self._toggle_recovery_option_controls()
        self._load_config_summary()
        self._refresh_run_summary()

    def _night_mode_run_root(self) -> str:
        configured = self.run_root_edit.text().strip()
        if configured:
            return configured
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "overnight_runs")

    def _refresh_run_summary(self):
        summary = _load_latest_night_mode_run_summary(self._night_mode_run_root())
        if not summary:
            self.run_summary_view.setPlainText(NIGHT_MODE_RUN_SUMMARY_PLACEHOLDER)
            return
        self.run_summary_view.setPlainText(self._format_run_summary(summary))

    def _format_run_summary(self, summary: dict) -> str:
        lines = [
            f"Seeds processed: {int(summary.get('seeds_processed', 0) or 0)}",
            f"Artists processed: {int(summary.get('artists_processed', 0) or 0)}",
            f"Domains discovered: {int(summary.get('domains_discovered', 0) or 0)}",
            f"Emails discovered: {int(summary.get('emails_discovered', 0) or 0)}",
            f"Reusable orgs created: {int(summary.get('orgs_created', 0) or 0)}",
            "",
            "Lead Vault",
            f"Rows added: {int(summary.get('vault_rows_added', 0) or 0)}",
            f"Rows updated: {int(summary.get('vault_rows_updated', 0) or 0)}",
        ]
        return "\n".join(lines)

    def _format_eta(self, eta_seconds) -> str:
        try:
            seconds = int(eta_seconds)
        except Exception:
            return "ETA unknown"
        if seconds < 0:
            return "ETA unknown"
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"ETA {hours}h {minutes}m"
        if minutes:
            return f"ETA {minutes}m {secs}s"
        return f"ETA {secs}s"

    def _refresh_runtime_progress(self):
        progress = read_progress()
        phase = str(progress.get("phase") or "idle")
        processed = int(progress.get("processed_rows") or 0)
        total = progress.get("total_rows")
        percentage = progress.get("percentage")
        if percentage is None:
            self.runtime_progress_bar.setRange(0, 0 if phase not in {"idle", "complete"} else 1000)
            self.runtime_progress_bar.setValue(0)
            pct_text = "--"
        else:
            self.runtime_progress_bar.setRange(0, 1000)
            pct_value = max(0.0, min(float(percentage), 100.0))
            self.runtime_progress_bar.setValue(int(round(pct_value * 10)))
            pct_text = f"{pct_value:.1f}%"
        rows_text = f"{processed} / {total}" if total is not None else f"{processed} / unknown"
        emails = int(progress.get("emails_found") or 0)
        source = str(progress.get("current_source") or "").strip()
        status = str(progress.get("current_status") or "").strip()
        source_status = " | ".join(part for part in (source, status) if part)
        details = [
            f"{phase} | {pct_text} | rows {rows_text}",
            f"emails {emails}",
            self._format_eta(progress.get("eta_seconds")),
        ]
        if source_status:
            details.append(source_status)
        self.runtime_progress_detail.setText(" | ".join(details))

    def _update_jobs_summary_from_jobs(self):
        lines = []
        for job in self.jobs:
            line = (
                f"job_id={job.get('job_id', '')} | "
                f"dir={job.get('directory', '')} | "
                f"mode={job.get('mode', '')} | "
                f"target={job.get('target_valid_leads', '')} | "
                f"max_hours={job.get('max_hours', '')}"
            )
            lines.append(line)
        if not lines:
            lines.append("No jobs configured.")
        self.jobs_summary.setPlainText("\n".join(lines))

    def _refresh_jobs_table(self):
        self.jobs_table.setRowCount(len(self.jobs))
        for idx, job in enumerate(self.jobs):
            values = [
                str(idx + 1),
                job.get("directory", ""),
                job.get("mode", ""),
                job.get("input_seed_csv", ""),
                str(job.get("target_valid_leads", "")),
                str(job.get("max_hours", "")),
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                self.jobs_table.setItem(idx, col, item)

    def _current_unearthed_resume_mode(self) -> str:
        value = str(self.unearthed_resume_mode_combo.currentData() or "auto").strip().lower()
        if value not in {"auto", "cursor", "fresh", "selected"}:
            return "auto"
        return value

    def _set_unearthed_resume_mode(self, value) -> None:
        normalized = str(value or "auto").strip().lower()
        if normalized not in {"auto", "cursor", "fresh", "selected"}:
            normalized = "auto"
        idx = self.unearthed_resume_mode_combo.findData(normalized)
        if idx < 0:
            idx = 0
        self.unearthed_resume_mode_combo.setCurrentIndex(idx)

    def _current_unearthed_selected_cursor(self) -> str:
        return str(self.unearthed_selected_cursor_edit.text() or "").strip()

    def _set_unearthed_selected_cursor(self, value) -> None:
        self.unearthed_selected_cursor_edit.setText(str(value or "").strip())

    def _current_unearthed_start_index_position(self) -> str:
        return str(self.unearthed_start_index_edit.text() or "").strip()

    def _set_unearthed_start_index_position(self, value) -> None:
        self.unearthed_start_index_edit.setText(str(value or "").strip())

    def _sync_unearthed_resume_controls(self) -> None:
        selected_mode = self._current_unearthed_resume_mode() == "selected"
        self.unearthed_selected_cursor_label.setEnabled(selected_mode)
        self.unearthed_selected_cursor_edit.setEnabled(selected_mode)

    def _current_unearthed_use_url_index(self) -> bool:
        return bool(self.unearthed_source_mode_combo.currentData())

    def _set_unearthed_source_mode(self, use_url_index) -> None:
        target = bool(use_url_index)
        for idx in range(self.unearthed_source_mode_combo.count()):
            if bool(self.unearthed_source_mode_combo.itemData(idx)) == target:
                self.unearthed_source_mode_combo.setCurrentIndex(idx)
                return
        self.unearthed_source_mode_combo.setCurrentIndex(0)

    def _unearthed_url_index_path(self) -> str:
        return self._active_unearthed_index_path

    def _set_unearthed_url_index_path(self, path: str) -> None:
        resolved = os.path.abspath(str(path or "").strip() or _unearthed_artist_url_index_path())
        self._active_unearthed_index_path = resolved
        self._refresh_unearthed_index_selector()
        self._sync_unearthed_source_mode_controls()

    def _normalize_unearthed_index_filename(self, raw_name: str) -> str:
        name = os.path.basename(str(raw_name or "").strip())
        if not name:
            raise ValueError("Enter an index filename.")
        if name in {".", ".."} or os.path.sep in name or (os.path.altsep and os.path.altsep in name):
            raise ValueError("Index filename must not include folders.")
        if not name.lower().endswith(".csv"):
            name = f"{name}.csv"
        return name

    def _unearthed_custom_index_path(self, raw_name: str) -> str:
        name = self._normalize_unearthed_index_filename(raw_name)
        return os.path.join(_ensure_unearthed_custom_index_dir(), name)

    def _prompt_unearthed_index_filename(self, title: str) -> str | None:
        raw_name, ok = QtWidgets.QInputDialog.getText(self, title, "Filename:")
        if not ok:
            return None
        try:
            return self._unearthed_custom_index_path(raw_name)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Index File", str(exc))
            return None

    def _refresh_unearthed_index_selector(self) -> None:
        if not hasattr(self, "unearthed_index_combo"):
            return
        _ensure_unearthed_custom_index_dir()
        active_path = os.path.abspath(self._active_unearthed_index_path or _unearthed_artist_url_index_path())
        default_path = os.path.abspath(_unearthed_artist_url_index_path())
        self.unearthed_index_combo.blockSignals(True)
        self.unearthed_index_combo.clear()
        default_label = f"Default ({os.path.relpath(default_path, os.path.dirname(os.path.abspath(__file__)))})"
        self.unearthed_index_combo.addItem(default_label, default_path)
        for name in sorted(os.listdir(UNEARTHED_CUSTOM_INDEX_DIR)):
            if not name.lower().endswith(".csv"):
                continue
            path = os.path.abspath(os.path.join(UNEARTHED_CUSTOM_INDEX_DIR, name))
            self.unearthed_index_combo.addItem(name, path)
        self.unearthed_index_combo.addItem("Create New Index...", "__create__")
        self.unearthed_index_combo.addItem("Save Current Index As...", "__save_as__")
        idx = self.unearthed_index_combo.findData(active_path)
        if idx < 0:
            self.unearthed_index_combo.insertItem(1, os.path.basename(active_path), active_path)
            idx = 1
        self.unearthed_index_combo.setCurrentIndex(idx)
        self.unearthed_index_combo.blockSignals(False)

    def _handle_unearthed_index_selection(self) -> None:
        data = self.unearthed_index_combo.currentData()
        previous_path = self._active_unearthed_index_path
        if data == "__create__":
            self._create_new_unearthed_index()
            return
        if data == "__save_as__":
            self._save_current_unearthed_index_as()
            return
        if data:
            self._active_unearthed_index_path = os.path.abspath(str(data))
        if previous_path != self._active_unearthed_index_path:
            self._sync_unearthed_source_mode_controls()

    def _create_new_unearthed_index(self) -> None:
        path = self._prompt_unearthed_index_filename("Create New Index")
        if not path:
            self._refresh_unearthed_index_selector()
            return
        if os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "Index File", f"Index already exists:\n{path}")
            self._refresh_unearthed_index_selector()
            return
        try:
            _write_empty_unearthed_artist_url_index(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Index File", f"Could not create index:\n{exc}")
            self._refresh_unearthed_index_selector()
            return
        self._set_unearthed_url_index_path(path)

    def _copy_unearthed_index_atomic(self, source_path: str, dest_path: str) -> None:
        _validate_unearthed_index_access(source_path, require_existing=True)
        if os.path.abspath(source_path) == os.path.abspath(dest_path):
            raise ValueError("Destination index must be different from the active index.")
        if os.path.exists(dest_path):
            raise FileExistsError(f"Index already exists: {dest_path}")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        tmp_path = f"{dest_path}.tmp"
        with open(source_path, "rb") as source, open(tmp_path, "wb") as dest:
            shutil.copyfileobj(source, dest)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(tmp_path, dest_path)

    def _save_current_unearthed_index_as(self) -> None:
        source_path = self._active_unearthed_index_path
        path = self._prompt_unearthed_index_filename("Save Current Index As")
        if not path:
            self._refresh_unearthed_index_selector()
            return
        try:
            self._copy_unearthed_index_atomic(source_path, path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Index File", f"Could not save index copy:\n{exc}")
            self._refresh_unearthed_index_selector()
            return
        self._set_unearthed_url_index_path(path)

    def _unearthed_url_index_status_text(self) -> str:
        index_path = self._unearthed_url_index_path()
        if not os.path.exists(index_path):
            return f"ACTIVE INDEX: {os.path.basename(index_path)} | Index not found"
        try:
            with open(index_path, "r", encoding="utf-8", newline="") as f:
                rows = [row for row in csv.reader(f) if any(str(cell).strip() for cell in row)]
            count = max(len(rows) - 1, 0)
            modified = datetime.datetime.fromtimestamp(os.path.getmtime(index_path)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            return f"ACTIVE INDEX: {os.path.basename(index_path)} | Index error: {exc}"
        return f"ACTIVE INDEX: {os.path.basename(index_path)} | Index contains: {count} artist URLs | Modified: {modified}"

    def _sync_unearthed_source_mode_controls(self) -> None:
        self.unearthed_index_status_label.setText(self._unearthed_url_index_status_text())
        self.unearthed_index_status_label.setVisible(True)

    def _set_unearthed_index_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.unearthed_index_combo,
            self.unearthed_duplicate_index_button,
            self.unearthed_source_mode_combo,
        ):
            widget.setEnabled(enabled)

    def _validate_active_unearthed_index_for_launch(self) -> bool:
        has_unearthed_job = any(
            str(job.get("directory") or "").strip().lower() == "unearthed"
            for job in self.jobs
        )
        if not has_unearthed_job:
            return True
        try:
            _validate_unearthed_index_access(self._active_unearthed_index_path, require_existing=True)
        except Exception as exc:
            self._append_log(f"[Night Mode] Index file error: {exc}")
            QtWidgets.QMessageBox.warning(self, "Index File", f"Selected index file is not usable:\n{exc}")
            self._sync_unearthed_source_mode_controls()
            return False
        return True

    def _night_mode_jobs_for_config(self):
        resume_mode = self._current_unearthed_resume_mode()
        selected_cursor = self._current_unearthed_selected_cursor()
        start_index_position = self._current_unearthed_start_index_position()
        use_url_index = self._current_unearthed_use_url_index()
        config_jobs = []
        for job in self.jobs:
            job_copy = dict(job)
            if str(job_copy.get("directory") or "").strip().lower() == "unearthed":
                job_copy["unearthed_resume_mode"] = resume_mode
                job_copy["use_unearthed_url_index"] = use_url_index
                job_copy["unearthed_url_index_path"] = self._active_unearthed_index_path
                if selected_cursor:
                    job_copy["unearthed_selected_cursor"] = selected_cursor
                if start_index_position:
                    job_copy["unearthed_start_index_position"] = start_index_position
            config_jobs.append(job_copy)
        return config_jobs

    def _fb_share_recovery_config_for_gui(self) -> Dict[str, object]:
        return {
            "enabled": self.fb_share_recovery_checkbox.isChecked(),
            "batch_size": _coerce_fb_share_recovery_batch_size(self.fb_share_recovery_batch_spin.value()),
            "output_mode": "in_place" if self.fb_share_recovery_in_place_radio.isChecked() else "copy",
        }

    def _open_job_dialog(self, existing_job=None, index=None):
        dialog = NightModeJobDialog(existing_job, parent=self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            job = dialog.get_job()
            if existing_job is None:
                if not job.get("job_id"):
                    job["job_id"] = f"job_{job.get('directory', 'dir')}_{len(self.jobs)+1}"
                self.jobs.append(job)
            else:
                self.jobs[index] = job
            self._refresh_jobs_table()
            self._update_jobs_summary_from_jobs()

    def _add_job_dialog(self):
        self._open_job_dialog()

    def _edit_job_dialog(self):
        row = self.jobs_table.currentRow()
        if row < 0 or row >= len(self.jobs):
            QtWidgets.QMessageBox.information(self, "Select a job", "Please select a job to edit.")
            return
        self._open_job_dialog(existing_job=self.jobs[row], index=row)

    def _remove_selected_job(self):
        row = self.jobs_table.currentRow()
        if row < 0 or row >= len(self.jobs):
            QtWidgets.QMessageBox.information(self, "Select a job", "Please select a job to remove.")
            return
        del self.jobs[row]
        self._refresh_jobs_table()
        self._update_jobs_summary_from_jobs()

    def _save_config_to_file(self):
        if not self.jobs:
            QtWidgets.QMessageBox.warning(self, "No jobs", "Add at least one job before saving a config.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Night Mode config",
            self.config_path_edit.text().strip() or "overnight_jobs.json",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        config = {
            "export_mode": self.export_mode_combo.currentText().strip(),
            "jobs": self._night_mode_jobs_for_config(),
            "unearthed_resume_mode": self._current_unearthed_resume_mode(),
            "unearthed_selected_cursor": self._current_unearthed_selected_cursor(),
            "unearthed_start_index_position": self._current_unearthed_start_index_position(),
            "unearthed_url_index_path": self._active_unearthed_index_path,
        }
        config["use_phased_runner"] = True
        config["facebook"] = {
            "auto_resume_after_captcha": self.fb_auto_resume_checkbox.isChecked(),
            "cooldown_seconds": int(self.fb_cooldown_spin.value()),
            "max_auto_resume_attempts": int(self.fb_max_attempts_spin.value()),
            "max_rows_per_run": int(self.fb_max_rows_spin.value()),
        }
        config["fb_driver_recovery"] = {
            "enabled": self.fb_driver_recovery_checkbox.isChecked(),
            "batch_size": int(self.fb_driver_recovery_batch_spin.value()),
            "output_mode": "in_place" if self.fb_driver_recovery_in_place_radio.isChecked() else "copy",
        }
        config["fb_share_recovery"] = self._fb_share_recovery_config_for_gui()
        config["master_enrichment"] = {
            "enabled": self.master_enrich_checkbox.isChecked(),
            "enable_live_search": self.master_live_checkbox.isChecked(),
            "max_live_searches": int(self.master_live_spin.value()),
        }
        config["soundcloud_meta_enricher"] = {"enabled": self.sc_meta_checkbox.isChecked()}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            self.config_path_edit.setText(path)
            self._append_log(f"[Night Mode] Saved config to {path}")
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Save failed", f"Could not save config:\n{exc}")

    def _browse_config(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Night Mode config",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if file_path:
            self.config_path_edit.setText(file_path)
            self._load_config_summary()

    def _browse_run_root(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select run root directory",
            "",
        )
        if directory:
            self.run_root_edit.setText(directory)

    def _load_config_summary(self):
        path = self.config_path_edit.text().strip()
        if not path or not os.path.exists(path):
            self.jobs_summary.setPlainText("Config not found. Please choose a valid JSON file.")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid config", f"Could not read config:\n{exc}")
            self.jobs_summary.setPlainText("Config could not be parsed.")
            return
        self._set_unearthed_resume_mode(config.get("unearthed_resume_mode", "auto"))
        self._set_unearthed_selected_cursor(config.get("unearthed_selected_cursor", ""))
        self._set_unearthed_start_index_position(config.get("unearthed_start_index_position", ""))
        self._sync_unearthed_resume_controls()
        use_url_index = bool(config.get("use_unearthed_url_index", False))
        index_path = str(config.get("unearthed_url_index_path") or "").strip()
        jobs = config.get("jobs", [])
        if isinstance(jobs, list):
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                if str(job.get("directory") or "").strip().lower() == "unearthed":
                    use_url_index = bool(job.get("use_unearthed_url_index", False))
                    index_path = str(job.get("unearthed_url_index_path") or index_path).strip()
                    if not self._current_unearthed_start_index_position():
                        self._set_unearthed_start_index_position(job.get("unearthed_start_index_position", ""))
                    break
        self._set_unearthed_url_index_path(index_path or _unearthed_artist_url_index_path())
        self._set_unearthed_source_mode(use_url_index)
        self._sync_unearthed_source_mode_controls()
        export_mode = (config.get("export_mode") or "both").strip().lower()
        if export_mode in {"both", "per_directory", "combined"}:
            idx = self.export_mode_combo.findText(export_mode)
            if idx >= 0:
                self.export_mode_combo.setCurrentIndex(idx)
        fb_cfg = config.get("facebook", {}) or {}
        self.fb_auto_resume_checkbox.setChecked(bool(fb_cfg.get("auto_resume_after_captcha", False)))
        try:
            self.fb_cooldown_spin.setValue(int(fb_cfg.get("cooldown_seconds", self.fb_cooldown_spin.value())))
        except Exception:
            pass
        try:
            self.fb_max_attempts_spin.setValue(int(fb_cfg.get("max_auto_resume_attempts", self.fb_max_attempts_spin.value())))
        except Exception:
            pass
        self.fb_max_rows_spin.setValue(0)
        fb_recovery_cfg = config.get("fb_driver_recovery", {}) or {}
        self.fb_driver_recovery_checkbox.setChecked(False)
        try:
            self.fb_driver_recovery_batch_spin.setValue(int(fb_recovery_cfg.get("batch_size", self.fb_driver_recovery_batch_spin.value())))
        except Exception:
            pass
        if str(fb_recovery_cfg.get("output_mode", "copy") or "copy").strip().lower() in {"in_place", "in-place", "inplace"}:
            self.fb_driver_recovery_in_place_radio.setChecked(True)
        else:
            self.fb_driver_recovery_copy_radio.setChecked(True)
        fb_share_recovery_cfg = config.get("fb_share_recovery", {}) or {}
        self.fb_share_recovery_checkbox.setChecked(False)
        self.fb_share_recovery_batch_spin.setValue(
            _coerce_fb_share_recovery_batch_size(
                fb_share_recovery_cfg.get("batch_size", self.fb_share_recovery_batch_spin.value())
            )
        )
        if str(fb_share_recovery_cfg.get("output_mode", "copy") or "copy").strip().lower() in {"in_place", "in-place", "inplace"}:
            self.fb_share_recovery_in_place_radio.setChecked(True)
        else:
            self.fb_share_recovery_copy_radio.setChecked(True)
        master_enrich_cfg = config.get("master_enrichment", {}) or {}
        self.master_enrich_checkbox.setChecked(bool(master_enrich_cfg.get("enabled", True)))
        self.master_live_checkbox.setChecked(True)
        self.master_live_spin.setValue(0)
        self._toggle_master_live_controls()
        self._toggle_recovery_option_controls()
        sc_meta_cfg = config.get("soundcloud_meta_enricher", {}) or {}
        self.sc_meta_checkbox.setChecked(bool(sc_meta_cfg.get("enabled", False)))
        if isinstance(jobs, list):
            self.jobs = jobs
            self._refresh_jobs_table()
        self._update_jobs_summary_from_jobs()

    def _start_night_mode(self):
        if (self.worker and self.worker.isRunning()) or (self.recovery_worker and self.recovery_worker.isRunning()):
            QtWidgets.QMessageBox.information(self, "Night Mode", "Night Mode is already running.")
            return
        if not self._validate_active_unearthed_index_for_launch():
            return
        self._bootstrap_stage = "headless"
        self._launch_night_mode(headless=True)

    def _launch_night_mode(self, headless: bool):
        # Fresh log buffer per run to avoid stale auth signals.
        self._log_buffer = []
        config_path = self.config_path_edit.text().strip()
        config_path_to_use = ""
        if self.jobs:
            config = {
                "export_mode": self.export_mode_combo.currentText().strip(),
                "jobs": self._night_mode_jobs_for_config(),
                "unearthed_resume_mode": self._current_unearthed_resume_mode(),
                "unearthed_selected_cursor": self._current_unearthed_selected_cursor(),
                "unearthed_start_index_position": self._current_unearthed_start_index_position(),
                "unearthed_url_index_path": self._active_unearthed_index_path,
            }
            config["use_phased_runner"] = True
            config["facebook"] = {
                "auto_resume_after_captcha": self.fb_auto_resume_checkbox.isChecked(),
                "cooldown_seconds": int(self.fb_cooldown_spin.value()),
                "max_auto_resume_attempts": int(self.fb_max_attempts_spin.value()),
                "max_rows_per_run": int(self.fb_max_rows_spin.value()),
            }
            config["fb_driver_recovery"] = {
                "enabled": self.fb_driver_recovery_checkbox.isChecked(),
                "batch_size": int(self.fb_driver_recovery_batch_spin.value()),
                "output_mode": "in_place" if self.fb_driver_recovery_in_place_radio.isChecked() else "copy",
            }
            config["fb_share_recovery"] = self._fb_share_recovery_config_for_gui()
            config["master_enrichment"] = {
                "enabled": self.master_enrich_checkbox.isChecked(),
                "enable_live_search": self.master_live_checkbox.isChecked(),
                "max_live_searches": int(self.master_live_spin.value()),
            }
            config["soundcloud_meta_enricher"] = {"enabled": self.sc_meta_checkbox.isChecked()}
            try:
                temp_dir = tempfile.mkdtemp(prefix="nightmode_")
                config_path_to_use = os.path.join(temp_dir, "overnight_jobs_gui.json")
                with open(config_path_to_use, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2)
                self._append_log(f"[Night Mode] Using GUI-configured jobs ({len(self.jobs)} job(s)).")
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Config error", f"Could not write temp config:\n{exc}")
                return
        else:
            if not config_path or not os.path.exists(config_path):
                QtWidgets.QMessageBox.warning(self, "Config missing", "Add jobs in the table or select a valid night mode config JSON.")
                return
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                config.setdefault("facebook", {})
                config["facebook"].update({
                    "auto_resume_after_captcha": self.fb_auto_resume_checkbox.isChecked(),
                    "cooldown_seconds": int(self.fb_cooldown_spin.value()),
                    "max_auto_resume_attempts": int(self.fb_max_attempts_spin.value()),
                    "max_rows_per_run": int(self.fb_max_rows_spin.value()),
                })
                config["fb_share_recovery"] = self._fb_share_recovery_config_for_gui()
                config["use_phased_runner"] = True
                config["master_enrichment"] = {
                    "enabled": self.master_enrich_checkbox.isChecked(),
                    "enable_live_search": self.master_live_checkbox.isChecked(),
                    "max_live_searches": int(self.master_live_spin.value()),
                }
                temp_dir = tempfile.mkdtemp(prefix="nightmode_")
                config_path_to_use = os.path.join(temp_dir, "overnight_jobs_gui.json")
                with open(config_path_to_use, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Config error", f"Could not prepare config:\n{exc}")
                return
        base_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(base_dir, "night_mode_runner.py")
        cmd = [sys.executable, script_path, "--config", config_path_to_use]
        export_mode = self.export_mode_combo.currentText().strip()
        if export_mode:
            cmd.extend(["--export-mode", export_mode])
        if self.resume_checkbox.isChecked():
            cmd.append("--resume")
        if self.stop_on_failure_checkbox.isChecked():
            cmd.append("--stop-on-failure")
        run_root = self.run_root_edit.text().strip()
        if run_root:
            cmd.extend(["--run-root", run_root])
        if self.fb_auto_resume_checkbox.isChecked():
            cmd.append("--fb-auto-resume")
        cmd.extend(["--fb-cooldown-seconds", str(int(self.fb_cooldown_spin.value()))])
        cmd.extend(["--fb-max-auto-resume-attempts", str(int(self.fb_max_attempts_spin.value()))])
        cmd.extend(["--fb-max-rows-per-run", str(int(self.fb_max_rows_spin.value()))])
        if self.sc_meta_checkbox.isChecked():
            cmd.append("--with-sc-meta")
        if self.fb_share_recovery_checkbox.isChecked():
            cmd.append("--enable-fb-share-recovery")
            cmd.extend([
                "--fb-share-recovery-batch-size",
                str(_coerce_fb_share_recovery_batch_size(self.fb_share_recovery_batch_spin.value())),
            ])
            if self.fb_share_recovery_in_place_radio.isChecked():
                cmd.append("--fb-share-recovery-in-place")

        self.log_console.clear()
        mode_label = "headless" if headless else "headed"
        self.status_label.setText(f"Status: running ({mode_label})")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_unearthed_index_controls_enabled(False)
        env = os.environ.copy()
        fb_user = self.fb_user_edit.text().strip()
        fb_pass = self.fb_pass_edit.text().strip()
        secrets_to_mask = []
        if fb_user:
            env["FB_USERNAME"] = fb_user
            self._append_log(f"[Night Mode] FB username provided (len={len(fb_user)})")
        if fb_pass:
            env["FB_PASSWORD"] = fb_pass
            secrets_to_mask.append(fb_pass)
        # Force Night Mode FB to use persistent profile (mode controlled by env/defaults).
        env["NIGHT_FB_PROFILE_DIR"] = "/Users/hughmiddleton/Lead Machine/Lead Machine Code/night_fb_profile"
        self.worker = NightModeWorker(cmd, workdir=base_dir, env=env, secrets=secrets_to_mask)
        self.worker.log_signal.connect(self._append_log)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _stop_night_mode(self):
        if not self.worker:
            return
        self._append_log("[Night Mode] Stop requested.")
        self.worker.stop()
        self.stop_button.setEnabled(False)

    def _on_finished(self, exit_code: int):
        stage = self._bootstrap_stage
        if exit_code != 0 and stage == "headless":
            if self._auth_failure_seen():
                QtWidgets.QMessageBox.information(
                    self,
                    "Night Mode Facebook",
                    "Headless run could not use the saved Facebook session.\nA headed browser will open so you can log in once.\nAfter login, return and the run will continue.",
                )
                self._bootstrap_stage = "headed"
                self._launch_night_mode(headless=False)
                return
        if exit_code == 0 and stage == "headed":
            if self._auth_success_seen():
                self._bootstrap_stage = "final_headless"
                self._launch_night_mode(headless=True)
                return
            QtWidgets.QMessageBox.information(
                self,
                "Night Mode Facebook",
                "Facebook login was not detected in the headed run. Please log in and try again.",
            )

        if exit_code == 0 and self.fb_driver_recovery_checkbox.isChecked():
            self.worker = None
            self._bootstrap_stage = None
            if self._start_fb_driver_recovery():
                return

        status = "completed" if exit_code == 0 else f"finished with errors (code {exit_code})"
        self.status_label.setText(f"Status: {status}")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_unearthed_index_controls_enabled(True)
        self.worker = None
        self._bootstrap_stage = None
        self._refresh_run_summary()

    def _latest_master_export_path(self) -> Optional[str]:
        latest_run_dir = _discover_latest_night_mode_run_dir(self._night_mode_run_root())
        if latest_run_dir is None:
            return None
        export_path = latest_run_dir / "master_export_leads.csv"
        return str(export_path) if export_path.exists() else None

    def _start_fb_driver_recovery(self) -> bool:
        export_path = self._latest_master_export_path()
        if not export_path:
            self._append_log("[FB Driver Recovery] Skipped: final export master_export_leads.csv was not found.")
            return False
        self.status_label.setText("Status: running FB driver recovery")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self._set_unearthed_index_controls_enabled(False)
        self.recovery_worker = FbDriverRecoveryWorker(
            export_path,
            int(self.fb_driver_recovery_batch_spin.value()),
            self.fb_driver_recovery_in_place_radio.isChecked(),
            parent=self,
        )
        self.recovery_worker.log_signal.connect(self._append_log)
        self.recovery_worker.finished_signal.connect(self._on_fb_driver_recovery_finished)
        self.recovery_worker.start()
        return True

    def _on_fb_driver_recovery_finished(self, exit_code: int, result):
        status = "completed" if exit_code == 0 else f"finished with errors (code {exit_code})"
        self.status_label.setText(f"Status: {status}")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_unearthed_index_controls_enabled(True)
        self.recovery_worker = None
        self._refresh_run_summary()

    def _toggle_master_live_controls(self):
        enabled = self.master_enrich_checkbox.isChecked()
        self.master_live_checkbox.setEnabled(enabled)
        self.master_live_spin.setEnabled(enabled and self.master_live_checkbox.isChecked())

    def _toggle_recovery_option_controls(self):
        self.fb_driver_recovery_options_widget.setVisible(self.fb_driver_recovery_checkbox.isChecked())
        self.fb_share_recovery_options_widget.setVisible(self.fb_share_recovery_checkbox.isChecked())

    def _append_log(self, message: str):
        msg = message or ""
        self._log_buffer.append(msg)
        if len(self._log_buffer) > 300:
            self._log_buffer = self._log_buffer[-300:]
        self.log_console.appendPlainText(msg)
        scrollbar = self.log_console.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())

    def _auth_failure_seen(self) -> bool:
        phrases = [
            "headless session unauthenticated",
            "missing c_user cookie",
            "manual login timed out",
            "facebook session unauthenticated",
            "[night fb] headless session unauthenticated",
        ]
        buf = "\n".join(self._log_buffer).lower()
        return any(p in buf for p in phrases)

    def _auth_success_seen(self) -> bool:
        phrases = [
            "auth check after fb homepage (headed): authed=true",
            "auth check after facebook homepage (headed): authed=true",
        ]
        buf = "\n".join(self._log_buffer).lower()
        return any(p in buf for p in phrases)

    def _clear_log(self):
        self.log_console.clear()

    def shutdown(self):
        worker = self.worker
        if worker and worker.isRunning():
            try:
                worker.stop()
                worker.wait(2000)
            except Exception:
                try:
                    worker.terminate()
                    worker.wait(2000)
                except Exception:
                    pass
        self.worker = None
        recovery_worker = self.recovery_worker
        if recovery_worker and recovery_worker.isRunning():
            try:
                recovery_worker.wait(2000)
            except Exception:
                try:
                    recovery_worker.terminate()
                    recovery_worker.wait(2000)
                except Exception:
                    pass
        self.recovery_worker = None


class NightModeJobDialog(QtWidgets.QDialog):
    def __init__(self, job: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Night Mode Job")
        self.job = job or {}
        self._build_ui()
        if job:
            self._load_job(job)

    def _build_ui(self):
        layout = QtWidgets.QFormLayout()
        self.job_id_edit = QtWidgets.QLineEdit()
        self.directory_combo = QtWidgets.QComboBox()
        self.directory_combo.addItems(["spotify", "bandcamp", "soundcloud", "unearthed"])
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["", "playlist", "discover", "search", "people", "tracks"])
        self.input_edit = QtWidgets.QLineEdit()
        self.target_spin = QtWidgets.QSpinBox()
        self.target_spin.setRange(0, 100000)
        self.target_spin.setValue(100)
        self.max_hours_spin = QtWidgets.QDoubleSpinBox()
        self.max_hours_spin.setRange(0, 168)
        self.max_hours_spin.setDecimals(1)
        self.max_hours_spin.setValue(0.0)
        self.notes_edit = QtWidgets.QLineEdit()

        layout.addRow("Job ID (optional):", self.job_id_edit)
        layout.addRow("Directory:", self.directory_combo)
        layout.addRow("Mode:", self.mode_combo)
        layout.addRow("Input/Seed:", self.input_edit)
        layout.addRow("Target leads:", self.target_spin)
        layout.addRow("Max hours (0 = no limit):", self.max_hours_spin)
        layout.addRow("Notes:", self.notes_edit)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addLayout(layout)
        main_layout.addWidget(buttons)
        self.setLayout(main_layout)

    def _load_job(self, job: dict):
        self.job_id_edit.setText(job.get("job_id", ""))
        directory = job.get("directory", "")
        idx = self.directory_combo.findText(directory)
        if idx >= 0:
            self.directory_combo.setCurrentIndex(idx)
        mode = job.get("mode", "")
        idx = self.mode_combo.findText(mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        self.input_edit.setText(job.get("input_seed_csv", ""))
        try:
            self.target_spin.setValue(int(job.get("target_valid_leads", 100)))
        except Exception:
            pass
        try:
            self.max_hours_spin.setValue(float(job.get("max_hours", 0.0)))
        except Exception:
            pass
        self.notes_edit.setText(job.get("notes", ""))

    def get_job(self) -> dict:
        job = dict(self.job)
        job_id = self.job_id_edit.text().strip()
        if job_id:
            job["job_id"] = job_id
        job["directory"] = self.directory_combo.currentText().strip()
        job["mode"] = self.mode_combo.currentText().strip()
        job["input_seed_csv"] = self.input_edit.text().strip()
        job["target_valid_leads"] = int(self.target_spin.value())
        max_hours = float(self.max_hours_spin.value())
        job["max_hours"] = max_hours if max_hours > 0 else 0
        notes = self.notes_edit.text().strip()
        if notes:
            job["notes"] = notes
        return job
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Artist & Facebook Scraper")
        self.setMinimumSize(800, 600)
        self.artist_thread = None
        self.fb_thread = None
        self.fb_av_worker = None
        self.manual_fb_recovery_worker = None
        self.manual_fb_share_recovery_worker = None
        self.current_artist_source = ""
        self.create_menu()
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.artist_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.artist_tab, "Artist Scraping")
        self.create_artist_tab()
        self.facebook_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.facebook_tab, "Facebook Scraping")
        self.create_facebook_tab()
        self.cross_enricher_tab = CrossDirectoryEnricherTab(
            enricher_module=cross_directory_enricher
        )
        self.tabs.addTab(self.cross_enricher_tab, "Cross-Directory Enricher")
        self.lead_vault_tab = LeadVaultTab()
        self.tabs.addTab(self.lead_vault_tab, "Lead Vault")
        self.campaign_prep_tab = CampaignPrepTab()
        self.tabs.addTab(self.campaign_prep_tab, "Campaign Prep")
        self.auto_validate_tab = AutoValidateTab()
        self.tabs.addTab(self.auto_validate_tab, "Auto-Validate")
        self.night_mode_tab = NightModeTab()
        self.tabs.addTab(self.night_mode_tab, "Night Mode")
    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        save_as_action = QtWidgets.QAction("Save As (Facebook Output)...", self)
        save_as_action.triggered.connect(self.save_as_facebook_csv)
        file_menu.addAction(save_as_action)
    def save_as_facebook_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Facebook Output CSV As", "", "CSV Files (*.csv)")
        if file_path:
            self.output_csv_edit.setText(file_path)
    def browse_artist_output_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Select Artist Output CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.artist_output_csv_edit.setText(file_path)
    def browse_input_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Input CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.input_csv_edit.setText(file_path)
    def browse_output_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Select Facebook Output CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.output_csv_edit.setText(file_path)
    def browse_manual_fb_recovery_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select CSV for FB Recovery", "", "CSV Files (*.csv)")
        if file_path:
            self.manual_fb_recovery_csv_edit.setText(file_path)
    def browse_manual_fb_share_recovery_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select CSV for FB /share Recovery", "", "CSV Files (*.csv)")
        if file_path:
            self.manual_fb_share_recovery_csv_edit.setText(file_path)
    def create_artist_tab(self):
        layout = QtWidgets.QVBoxLayout()
        config_group, config_layout = _lm_section("Job Configuration")
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItems(["Unearthed", "Bandcamp", "SoundCloud", "Last.fm Similar", "Spotify"])
        self.source_combo.currentTextChanged.connect(self.on_source_changed)
        config_layout.addLayout(_lm_row("Source:", self.source_combo, add_stretch=True))
        self.url_label = QtWidgets.QLabel("Website URL:")
        self.url_edit = QtWidgets.QLineEdit(UNEARTHED_DEFAULT_URL)
        self.url_edit.setPlaceholderText(UNEARTHED_DEFAULT_URL)
        config_layout.addLayout(_lm_row_with_label_widget(self.url_label, self.url_edit))
        self.pages_per_tag_edit = QtWidgets.QLineEdit(str(BANDCAMP_PAGES_PER_TAG))
        self.pages_per_tag_edit.setEnabled(False)
        config_layout.addLayout(_lm_row("Pages per Tag:", self.pages_per_tag_edit))
        self.max_artists_edit = QtWidgets.QLineEdit("200")
        config_layout.addLayout(_lm_row("Max Artists:", self.max_artists_edit))
        self.sc_meta_checkbox = QtWidgets.QCheckBox("Run SoundCloud metadata enricher (fill missing genre/date)")
        self.sc_meta_checkbox.setChecked(False)
        self.sc_meta_checkbox.setVisible(False)
        config_layout.addLayout(_lm_control_row(self.sc_meta_checkbox))
        self.artist_output_csv_edit = QtWidgets.QLineEdit("artist_social_links.csv")
        artist_output_browse = QtWidgets.QPushButton("Browse")
        artist_output_browse.clicked.connect(self.browse_artist_output_csv)
        config_layout.addLayout(_lm_row("Output CSV:", self.artist_output_csv_edit, artist_output_browse))
        layout.addWidget(config_group)

        run_group, run_layout = _lm_section("Run Settings")
        self.artist_start_button = QtWidgets.QPushButton("Start Artist Scraping")
        self.artist_start_button.clicked.connect(self.start_artist_scraping)
        run_layout.addLayout(_lm_control_row(self.artist_start_button))
        layout.addWidget(run_group)

        output_group, output_layout = _lm_section("Run Output + Logs")
        self.artist_progress_bar = QtWidgets.QProgressBar()
        self.artist_progress_bar.setRange(0, 0)
        self.artist_progress_bar.setVisible(False)
        output_layout.addWidget(_lm_apply_control_sizing(self.artist_progress_bar))
        self.artist_log = QtWidgets.QTextEdit()
        self.artist_log.setReadOnly(True)
        self.artist_log.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        output_layout.addWidget(self.artist_log)
        layout.addWidget(output_group)
        _lm_scrolled_tab(self.artist_tab, layout)
    def on_source_changed(self, source_text):
        if source_text == "Bandcamp":
            self.url_label.setText("Website URL:")
            self.url_edit.setPlaceholderText(BANDCAMP_DEFAULT_TAG_URL)
            current = self.url_edit.text().strip()
            if not current or current in (UNEARTHED_DEFAULT_URL, SOUNDCLOUD_DEFAULT_TAG_URL):
                self.url_edit.setText(BANDCAMP_DEFAULT_TAG_URL)
            self.pages_per_tag_edit.setEnabled(True)
            self.sc_meta_checkbox.setVisible(False)
        elif source_text == "SoundCloud":
            self.url_label.setText("Website URL:")
            self.url_edit.setPlaceholderText(SOUNDCLOUD_DEFAULT_TAG_URL)
            current = self.url_edit.text().strip()
            if not current or current in (UNEARTHED_DEFAULT_URL, BANDCAMP_DEFAULT_TAG_URL):
                self.url_edit.setText(SOUNDCLOUD_DEFAULT_TAG_URL)
            self.pages_per_tag_edit.setEnabled(True)
            self.sc_meta_checkbox.setVisible(True)
        elif source_text == "Last.fm Similar":
            self.url_label.setText("Seed Artists:")
            self.url_edit.setPlaceholderText("Seed artist names, comma separated (e.g. Hope D, Jaguar Jonze)")
            current = self.url_edit.text().strip()
            if not current or current in (UNEARTHED_DEFAULT_URL, BANDCAMP_DEFAULT_TAG_URL, SOUNDCLOUD_DEFAULT_TAG_URL):
                self.url_edit.clear()
            self.pages_per_tag_edit.setEnabled(False)
            self.sc_meta_checkbox.setVisible(False)
        else:
            self.url_label.setText("Website URL:")
            self.url_edit.setPlaceholderText(UNEARTHED_DEFAULT_URL)
            current = self.url_edit.text().strip()
            if not current or current in (BANDCAMP_DEFAULT_TAG_URL, SOUNDCLOUD_DEFAULT_TAG_URL):
                self.url_edit.setText(UNEARTHED_DEFAULT_URL)
            self.pages_per_tag_edit.setEnabled(False)
            self.sc_meta_checkbox.setVisible(False)
    def create_facebook_tab(self):
        layout = QtWidgets.QVBoxLayout()
        config_group, config_layout = _lm_section("Job Configuration")
        self.input_csv_edit = QtWidgets.QLineEdit("test_artist_social_links.csv")
        input_browse = QtWidgets.QPushButton("Browse")
        input_browse.clicked.connect(self.browse_input_csv)
        config_layout.addLayout(_lm_row("Input CSV:", self.input_csv_edit, input_browse))
        self.output_csv_edit = QtWidgets.QLineEdit("test_combined_artist_data.csv")
        output_browse = QtWidgets.QPushButton("Browse")
        output_browse.clicked.connect(self.browse_output_csv)
        config_layout.addLayout(_lm_row("Output CSV:", self.output_csv_edit, output_browse))
        layout.addWidget(config_group)

        run_group, run_layout = _lm_section("Run Settings")
        self.fb_username_edit = QtWidgets.QLineEdit()
        run_layout.addLayout(_lm_row("Facebook Username:", self.fb_username_edit))
        self.fb_password_edit = QtWidgets.QLineEdit()
        self.fb_password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        run_layout.addLayout(_lm_row("Facebook Password:", self.fb_password_edit))
        self.max_emails_edit = QtWidgets.QLineEdit()
        run_layout.addLayout(_lm_row("Max Emails (optional):", self.max_emails_edit))
        self.fb_auto_validate_checkbox = QtWidgets.QCheckBox("Enable Origin Auto-Validate after this run")
        self.fb_auto_validate_checkbox.setChecked(False)
        run_layout.addLayout(_lm_control_row(self.fb_auto_validate_checkbox))
        self.fb_start_button = QtWidgets.QPushButton("Start Facebook Scraping")
        self.fb_start_button.clicked.connect(self.start_facebook_scraping)
        run_layout.addLayout(_lm_control_row(self.fb_start_button))
        layout.addWidget(run_group)

        recovery_group, recovery_layout = _lm_section("📦 Full FB Recovery (Driver + Share)")
        self.manual_fb_recovery_csv_edit = QtWidgets.QLineEdit()
        manual_recovery_browse = QtWidgets.QPushButton("Select CSV...")
        manual_recovery_browse.clicked.connect(self.browse_manual_fb_recovery_csv)
        recovery_layout.addLayout(_lm_row("CSV:", self.manual_fb_recovery_csv_edit, manual_recovery_browse))
        self.manual_fb_share_checkbox = QtWidgets.QCheckBox("Recover /share rows")
        self.manual_fb_share_checkbox.setChecked(True)
        self.manual_fb_driver_checkbox = QtWidgets.QCheckBox("Retry driver_error rows")
        self.manual_fb_driver_checkbox.setChecked(True)
        recovery_layout.addLayout(_lm_control_row(self.manual_fb_share_checkbox, self.manual_fb_driver_checkbox))
        self.manual_fb_batch_spin = QtWidgets.QSpinBox()
        self.manual_fb_batch_spin.setRange(1, 1000)
        self.manual_fb_batch_spin.setValue(40)
        recovery_layout.addLayout(_lm_row("Batch size:", self.manual_fb_batch_spin, add_stretch=True))
        self.manual_fb_copy_radio = QtWidgets.QRadioButton("Write recovered copy")
        self.manual_fb_copy_radio.setChecked(True)
        self.manual_fb_in_place_radio = QtWidgets.QRadioButton("In-place update")
        self.manual_fb_output_mode_group = QtWidgets.QButtonGroup(self)
        self.manual_fb_output_mode_group.addButton(self.manual_fb_copy_radio)
        self.manual_fb_output_mode_group.addButton(self.manual_fb_in_place_radio)
        recovery_layout.addLayout(_lm_control_row(QtWidgets.QLabel("Output mode:"), self.manual_fb_copy_radio, self.manual_fb_in_place_radio))
        self.manual_fb_dry_run_button = QtWidgets.QPushButton("Dry Run")
        self.manual_fb_dry_run_button.clicked.connect(lambda: self.start_manual_fb_recovery(dry_run=True))
        self.manual_fb_run_button = QtWidgets.QPushButton("▶ Run Full Recovery")
        self.manual_fb_run_button.clicked.connect(lambda: self.start_manual_fb_recovery(dry_run=False))
        recovery_layout.addLayout(_lm_control_row(self.manual_fb_dry_run_button, self.manual_fb_run_button))
        layout.addWidget(recovery_group)

        share_recovery_group, share_recovery_layout = _lm_section("📦 FB /share Recovery (Targeted)")
        self.manual_fb_share_recovery_csv_edit = QtWidgets.QLineEdit()
        manual_share_recovery_browse = QtWidgets.QPushButton("Browse...")
        manual_share_recovery_browse.clicked.connect(self.browse_manual_fb_share_recovery_csv)
        share_recovery_layout.addLayout(_lm_row("Input CSV:", self.manual_fb_share_recovery_csv_edit, manual_share_recovery_browse))
        self.manual_fb_share_copy_radio = QtWidgets.QRadioButton("Write recovered copy")
        self.manual_fb_share_copy_radio.setChecked(True)
        self.manual_fb_share_in_place_radio = QtWidgets.QRadioButton("In-place update")
        self.manual_fb_share_output_mode_group = QtWidgets.QButtonGroup(self)
        self.manual_fb_share_output_mode_group.addButton(self.manual_fb_share_copy_radio)
        self.manual_fb_share_output_mode_group.addButton(self.manual_fb_share_in_place_radio)
        share_recovery_layout.addLayout(_lm_control_row(QtWidgets.QLabel("Output Mode"), self.manual_fb_share_copy_radio, self.manual_fb_share_in_place_radio))
        self.manual_fb_share_batch_spin = QtWidgets.QSpinBox()
        self.manual_fb_share_batch_spin.setRange(1, 1000)
        self.manual_fb_share_batch_spin.setValue(FB_SHARE_RECOVERY_DEFAULT_BATCH_SIZE)
        share_recovery_layout.addLayout(_lm_row("Batch size:", self.manual_fb_share_batch_spin, add_stretch=True))
        self.manual_fb_share_run_button = QtWidgets.QPushButton("▶ Run /share Recovery")
        self.manual_fb_share_run_button.clicked.connect(self.start_manual_fb_share_recovery)
        share_recovery_layout.addLayout(_lm_control_row(self.manual_fb_share_run_button))
        self.manual_fb_share_summary_view = QtWidgets.QPlainTextEdit()
        self.manual_fb_share_summary_view.setReadOnly(True)
        self.manual_fb_share_summary_view.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        share_recovery_layout.addWidget(self.manual_fb_share_summary_view)
        layout.addWidget(share_recovery_group)

        output_group, output_layout = _lm_section("Run Output + Logs")
        self.fb_progress_bar = QtWidgets.QProgressBar()
        self.fb_progress_bar.setRange(0, 0)
        self.fb_progress_bar.setVisible(False)
        output_layout.addWidget(_lm_apply_control_sizing(self.fb_progress_bar))
        self.fb_log = QtWidgets.QTextEdit()
        self.fb_log.setReadOnly(True)
        self.fb_log.setMinimumHeight(LM_LOG_MIN_HEIGHT)
        output_layout.addWidget(self.fb_log)
        layout.addWidget(output_group)
        _lm_scrolled_tab(self.facebook_tab, layout)
    def _append_manual_fb_recovery_log(self, message: str):
        self.fb_log.append(str(message or ""))
    def _set_manual_fb_recovery_running(self, running: bool):
        self.manual_fb_dry_run_button.setEnabled(not running)
        self.manual_fb_run_button.setEnabled(not running)
    def _set_manual_fb_share_recovery_running(self, running: bool):
        self.manual_fb_share_run_button.setEnabled(not running)
    def _validate_manual_fb_share_recovery_inputs(self) -> Optional[str]:
        csv_path = self.manual_fb_share_recovery_csv_edit.text().strip()
        try:
            _validate_manual_fb_share_recovery_csv(csv_path)
            output_path = _manual_fb_share_recovery_output_path(
                csv_path,
                in_place=self.manual_fb_share_in_place_radio.isChecked(),
            )
            if not self.manual_fb_share_in_place_radio.isChecked() and os.path.exists(output_path):
                return f"Output already exists: {output_path}"
        except Exception as exc:
            return str(exc)
        return None
    def _validate_manual_fb_recovery_inputs(self) -> Optional[str]:
        csv_path = self.manual_fb_recovery_csv_edit.text().strip()
        if not csv_path:
            return "Select a CSV before running manual FB recovery."
        if not os.path.exists(csv_path):
            return f"CSV not found: {csv_path}"
        if not self.manual_fb_share_checkbox.isChecked() and not self.manual_fb_driver_checkbox.isChecked():
            return "Select at least one recovery mode."
        return None
    def start_manual_fb_recovery(self, *, dry_run: bool):
        if self.manual_fb_recovery_worker and self.manual_fb_recovery_worker.isRunning():
            QtWidgets.QMessageBox.information(self, "Manual FB Recovery", "Manual FB recovery is already running.")
            return
        validation_error = self._validate_manual_fb_recovery_inputs()
        if validation_error:
            self._append_manual_fb_recovery_log(validation_error)
            QtWidgets.QMessageBox.warning(self, "Manual FB Recovery", validation_error)
            return
        self._set_manual_fb_recovery_running(True)
        self.manual_fb_recovery_worker = ManualFbRecoveryWorker(
            self.manual_fb_recovery_csv_edit.text().strip(),
            recover_share=self.manual_fb_share_checkbox.isChecked(),
            recover_driver=self.manual_fb_driver_checkbox.isChecked(),
            batch_size=int(self.manual_fb_batch_spin.value()),
            in_place=self.manual_fb_in_place_radio.isChecked(),
            dry_run=dry_run,
            parent=self,
        )
        self.manual_fb_recovery_worker.log_signal.connect(self._append_manual_fb_recovery_log)
        self.manual_fb_recovery_worker.finished_signal.connect(self._on_manual_fb_recovery_finished)
        self.manual_fb_recovery_worker.start()
    def _on_manual_fb_recovery_finished(self, exit_code: int, result):
        status = "complete" if exit_code == 0 else f"failed (code {exit_code})"
        self._append_manual_fb_recovery_log(f"[Manual FB Recovery] {status}")
        self._set_manual_fb_recovery_running(False)
        self.manual_fb_recovery_worker = None
    def start_manual_fb_share_recovery(self):
        if self.manual_fb_share_recovery_worker and self.manual_fb_share_recovery_worker.isRunning():
            QtWidgets.QMessageBox.information(self, "FB /share Manual Recovery", "FB /share recovery is already running.")
            return
        validation_error = self._validate_manual_fb_share_recovery_inputs()
        if validation_error:
            self._append_manual_fb_recovery_log(validation_error)
            QtWidgets.QMessageBox.warning(self, "FB /share Manual Recovery", validation_error)
            return
        self.manual_fb_share_summary_view.clear()
        self._set_manual_fb_share_recovery_running(True)
        self.manual_fb_share_recovery_worker = ManualFbShareRecoveryWorker(
            self.manual_fb_share_recovery_csv_edit.text().strip(),
            batch_size=int(self.manual_fb_share_batch_spin.value()),
            in_place=self.manual_fb_share_in_place_radio.isChecked(),
            parent=self,
        )
        self.manual_fb_share_recovery_worker.log_signal.connect(self._append_manual_fb_recovery_log)
        self.manual_fb_share_recovery_worker.finished_signal.connect(self._on_manual_fb_share_recovery_finished)
        self.manual_fb_share_recovery_worker.start()
    def _on_manual_fb_share_recovery_finished(self, exit_code: int, result):
        status = "complete" if exit_code == 0 else f"failed (code {exit_code})"
        self._append_manual_fb_recovery_log(f"[FB /share Manual Recovery] {status}")
        if isinstance(result, dict) and isinstance(result.get("summary"), dict):
            lines = []
            summary = result.get("summary") or {}
            for key in MANUAL_FB_SHARE_RECOVERY_SUMMARY_KEYS:
                if key in summary:
                    lines.append(f"{key}={summary[key]}")
            if result.get("output_csv"):
                lines.append(f"output={result.get('output_csv')}")
            self.manual_fb_share_summary_view.setPlainText("\n".join(lines))
        elif isinstance(result, dict) and result.get("error"):
            self.manual_fb_share_summary_view.setPlainText(str(result.get("error")))
        self._set_manual_fb_share_recovery_running(False)
        self.manual_fb_share_recovery_worker = None
    def start_artist_scraping(self):
        source = self.source_combo.currentText()
        url = self.url_edit.text().strip()
        bandcamp_mode = "discover"
        bandcamp_search_domain = "artists"
        bandcamp_search_location = ""
        if source in ("Bandcamp", "SoundCloud") and not url:
            default_url = BANDCAMP_DEFAULT_TAG_URL if source == "Bandcamp" else SOUNDCLOUD_DEFAULT_TAG_URL
            url = default_url
            self.url_edit.setText(url)
        if source == "Unearthed" and not url:
            self.artist_log.append("Please enter a valid website URL.")
            return
        if source == "Last.fm Similar" and not url:
            self.artist_log.append("Please enter at least one seed artist (comma separated).")
            return
        try:
            max_artists = int(self.max_artists_edit.text().strip())
        except ValueError:
            max_artists = 200
        try:
            pages_per_tag = int(self.pages_per_tag_edit.text().strip())
        except ValueError:
            if source == "Bandcamp":
                pages_per_tag = BANDCAMP_PAGES_PER_TAG
            elif source == "SoundCloud":
                pages_per_tag = SOUNDCLOUD_PAGES_PER_TAG
            else:
                pages_per_tag = BANDCAMP_PAGES_PER_TAG
        if max_artists <= 0:
            max_artists = 200
        if source == "Bandcamp":
            default_pages = BANDCAMP_PAGES_PER_TAG
            bandcamp_mode = "discover"
            bandcamp_base_url = url
            # Prompt for mode selection when running Bandcamp.
            choice_text, ok = QtWidgets.QInputDialog.getText(
                self,
                "Bandcamp mode",
                "Select mode [1) Discover, 2) Search]:",
                QtWidgets.QLineEdit.Normal,
                "1",
            )
            if ok and choice_text.strip() == "2":
                bandcamp_mode = "search"
            self.artist_log.append(f"Bandcamp: mode={bandcamp_mode}")
            print(f"Bandcamp: mode={bandcamp_mode}")
            if bandcamp_mode == "search":
                domain_choice_text, domain_ok = QtWidgets.QInputDialog.getText(
                    self,
                    "Bandcamp search domain",
                    "Bandcamp search domain:\n  1) Artists & labels\n  2) Tracks\nSelect [1/2] (default 1):",
                    QtWidgets.QLineEdit.Normal,
                    "1",
                )
                if domain_ok and domain_choice_text.strip() == "2":
                    bandcamp_search_domain = "tracks"
                else:
                    bandcamp_search_domain = "artists"
                self.artist_log.append(f"Bandcamp: search domain={bandcamp_search_domain}")
                print(f"Bandcamp: search domain={bandcamp_search_domain}")
                search_loc_value, search_loc_ok = QtWidgets.QInputDialog.getText(
                    self,
                    "Bandcamp search location (optional)",
                    "Optional location filter (keeps rows whose location contains this text; leave blank for no filter):",
                    QtWidgets.QLineEdit.Normal,
                    "",
                )
                bandcamp_search_location = (search_loc_value if search_loc_ok else "").strip()
                if bandcamp_search_location:
                    self.artist_log.append(f"Bandcamp: search location filter={bandcamp_search_location}")
                    print(f"Bandcamp: search location filter={bandcamp_search_location}")
                raw_prompt = bandcamp_base_url or ""
                raw_value, raw_ok = QtWidgets.QInputDialog.getText(
                    self,
                    "Bandcamp search",
                    "Bandcamp search (keywords OR full URL):",
                    QtWidgets.QLineEdit.Normal,
                    raw_prompt,
                )
                raw = (raw_value if raw_ok else raw_prompt).strip()
                if raw.lower().startswith("http"):
                    bandcamp_base_url = raw
                else:
                    q = quote_plus(raw)
                    if bandcamp_search_domain == "tracks":
                        bandcamp_base_url = f"https://bandcamp.com/search?q={q}&item_type=t&sort_field=date"
                    else:
                        bandcamp_base_url = f"https://bandcamp.com/search?q={q}&item_type=b&sort_field=date"
                self.artist_log.append(f"Bandcamp: base URL={bandcamp_base_url}")
                print(f"Bandcamp: base URL={bandcamp_base_url}")
                url = bandcamp_base_url
            else:
                bandcamp_base_url = url
            output_csv = _bandcamp_mode_output_csv_path(
                self.artist_output_csv_edit.text().strip(),
                bandcamp_mode,
                search_domain=bandcamp_search_domain if bandcamp_mode == "search" else None,
                search_location=bandcamp_search_location if bandcamp_mode == "search" else None,
            )
            self.artist_output_csv_edit.setText(output_csv)
            self.artist_log.append(f"Bandcamp: output CSV={output_csv}")
            print(f"Bandcamp: output CSV={output_csv}")
        elif source == "SoundCloud":
            default_pages = SOUNDCLOUD_PAGES_PER_TAG
        else:
            default_pages = BANDCAMP_PAGES_PER_TAG
        if pages_per_tag <= 0:
            pages_per_tag = default_pages
        seed_tags = None
        if source == "Bandcamp":
            if _bandcamp_is_discover_url(url):
                seed_tags = []
            else:
                extracted_tag = _bandcamp_extract_tag_from_url(url)
                seed_tags = [extracted_tag] if extracted_tag else list(BANDCAMP_SEED_TAGS)
        elif source == "SoundCloud":
            match = re.search(r"/tags/([^/?#]+)", url)
            seed_tags = [match.group(1).lower()] if match else list(SOUNDCLOUD_SEED_TAGS)
        output_csv = self.artist_output_csv_edit.text().strip()
        self.artist_start_button.setEnabled(False)
        self.artist_progress_bar.setVisible(True)
        self.artist_log.append("Initiating artist scraping...")
        self.current_artist_source = source
        self.artist_thread = ArtistScraperThread(
            url,
            max_artists,
            output_csv,
            source=source,
            pages_per_tag=pages_per_tag,
            seed_tags=seed_tags,
            bandcamp_mode=bandcamp_mode if source == "Bandcamp" else "discover",
            bandcamp_search_domain=bandcamp_search_domain if source == "Bandcamp" else "artists",
            bandcamp_search_location=bandcamp_search_location if source == "Bandcamp" else ""
        )
        self.artist_thread.log_signal.connect(self.update_artist_log)
        self.artist_thread.finished_signal.connect(self.artist_scraping_finished)
        self.artist_thread.start()
    def update_artist_log(self, message):
        self.artist_log.append(message)
    def artist_scraping_finished(self):
        self.artist_log.append("Artist scraping thread finished.")
        self.artist_progress_bar.setVisible(False)
        self.artist_start_button.setEnabled(True)
        thread = self.artist_thread
        if thread:
            try:
                thread.wait()
            except Exception:
                pass
        if (self.current_artist_source or "").lower() == "soundcloud" and self.sc_meta_checkbox.isChecked():
            sc_raw = self.artist_output_csv_edit.text().strip()
            if sc_raw and os.path.isfile(sc_raw):
                try:
                    self.artist_log.append(f"[SoundCloud] Running metadata enricher for {sc_raw} ...")
                    enriched_path = enrich_soundcloud_metadata(
                        input_csv=sc_raw,
                        output_csv=None,
                        max_rows=None,
                        skip_existing=True,
                        sleep_seconds=1.5,
                    )
                    self.artist_log.append(f"[SoundCloud] Metadata enrichment complete: {enriched_path}")
                    self.artist_output_csv_edit.setText(enriched_path)
                except Exception as exc:
                    self.artist_log.append(f"[SoundCloud] Metadata enrichment failed safely: {exc}")
            else:
                self.artist_log.append(f"[SoundCloud] Metadata enrichment skipped: raw CSV not found at {sc_raw}")
        self.artist_thread = None
    def start_facebook_scraping(self):
        input_csv = self.input_csv_edit.text().strip()
        output_csv = self.output_csv_edit.text().strip()
        fb_username = self.fb_username_edit.text().strip()
        fb_password = self.fb_password_edit.text().strip()
        max_emails_text = self.max_emails_edit.text().strip()
        max_emails = int(max_emails_text) if max_emails_text.isdigit() else None
        if not input_csv or not output_csv or not fb_username or not fb_password:
            self.fb_log.append("Please fill in all required fields for Facebook scraping.")
            return
        self.fb_start_button.setEnabled(False)
        self.fb_progress_bar.setVisible(True)
        self.fb_log.append("Initiating Facebook scraping...")
        self.fb_thread = FacebookScraperThread(input_csv, output_csv, fb_username, fb_password, max_emails)
        self.fb_thread.log_signal.connect(self.update_fb_log)
        self.fb_thread.finished_signal.connect(self.facebook_scraping_finished)
        self.fb_thread.start()
    def update_fb_log(self, message):
        self.fb_log.append(message)
    def facebook_scraping_finished(self):
        self.fb_log.append("Facebook scraping thread finished.")
        self.fb_progress_bar.setVisible(False)
        self.fb_start_button.setEnabled(True)
        thread = self.fb_thread
        if thread:
            try:
                thread.wait()
            except Exception:
                pass
        self.fb_thread = None
        if self.fb_auto_validate_checkbox.isChecked():
            output_csv = self.output_csv_edit.text().strip()
            if output_csv:
                base, ext = os.path.splitext(output_csv)
                checked_candidate = f"{base}_checked{ext or '.csv'}"
                target = checked_candidate if os.path.exists(checked_candidate) else output_csv
                target_path = _derive_origin_output_path(target)
                self._start_fb_auto_validate(
                    target,
                    scope="all",
                    output_path=target_path,
                    auto_run=True,
                )
            else:
                self.fb_log.append("[Auto-Validate] No output CSV provided; skipping.")

    def _start_fb_auto_validate(
        self,
        csv_path: str,
        scope: str = "uncertain_only",
        output_path: Optional[str] = None,
        auto_run: bool = False,
    ):
        self._stop_fb_auto_validate()
        if not csv_path or not os.path.exists(csv_path):
            self.fb_log.append(f"[Auto-Validate] Output CSV not found: {csv_path}")
            return
        target_path = output_path or _derive_origin_output_path(csv_path)
        self.fb_av_worker = AutoValidateWorker(
            csv_path=csv_path,
            scope=scope,
            output_path=target_path,
            auto_run=auto_run,
        )
        self.fb_av_worker.log_signal.connect(self.update_fb_log)
        self.fb_av_worker.finished_signal.connect(self._on_fb_auto_validate_finished)
        self.fb_av_worker.start()

    def _on_fb_auto_validate_finished(self, output_path: str):
        if output_path:
            self.fb_log.append(f"Origin Auto-Validate finished. Output: {output_path}")
        else:
            self.fb_log.append("Origin Auto-Validate finished with errors.")
        self._stop_fb_auto_validate()

    def _stop_fb_auto_validate(self):
        worker = getattr(self, "fb_av_worker", None)
        if not worker:
            return
        if worker.isRunning():
            try:
                worker.wait(2000)
            except Exception:
                try:
                    worker.terminate()
                    worker.wait(2000)
                except Exception:
                    pass
        self.fb_av_worker = None

    def closeEvent(self, event):
        self._shutdown_threads()
        super().closeEvent(event)

    def _shutdown_threads(self):
        for attr in ("artist_thread", "fb_thread", "manual_fb_recovery_worker", "manual_fb_share_recovery_worker"):
            thread = getattr(self, attr, None)
            if thread and thread.isRunning():
                try:
                    thread.requestInterruption()
                except Exception:
                    pass
                try:
                    finished = thread.wait(10000)
                except Exception:
                    finished = False
                if not finished:
                    try:
                        thread.terminate()
                        thread.wait(2000)
                    except Exception:
                        pass
            if attr in {"manual_fb_recovery_worker", "manual_fb_share_recovery_worker"}:
                setattr(self, attr, None)
        if (
            hasattr(self, "cross_enricher_tab")
            and hasattr(self.cross_enricher_tab, "shutdown")
        ):
            self.cross_enricher_tab.shutdown()
        if hasattr(self, "lead_vault_tab") and hasattr(self.lead_vault_tab, "shutdown"):
            self.lead_vault_tab.shutdown()
        if hasattr(self, "auto_validate_tab") and hasattr(self.auto_validate_tab, "shutdown"):
            self.auto_validate_tab.shutdown()
        if hasattr(self, "night_mode_tab") and hasattr(self.night_mode_tab, "shutdown"):
            self.night_mode_tab.shutdown()
        self._stop_fb_auto_validate()


def _handle_cli_entry(argv=None):
    argv = argv or sys.argv
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--soundcloud-url", dest="soundcloud_url")
    parser.add_argument("--soundcloud-tags", nargs="*", dest="soundcloud_tags")
    parser.add_argument("--max-artists", type=int, dest="max_artists")
    parser.add_argument("--max-handles", type=int, dest="max_handles")
    parser.add_argument("--min-yield", type=int, dest="min_yield", default=3)
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--soundcloud-cli", action="store_true", dest="soundcloud_cli")
    parser.add_argument("--auto-validate", dest="auto_validate_csv")
    parser.add_argument("--validate-scope", dest="validate_scope", default="uncertain_only")
    parser.add_argument("--main-menu", action="store_true", dest="main_menu")
    args, remaining = parser.parse_known_args(argv[1:])
    cli_requested = bool(args.soundcloud_cli or args.soundcloud_url or (args.soundcloud_tags and len(args.soundcloud_tags) > 0))
    if args.main_menu:
        _run_text_main_menu()
        return True, [argv[0]] + remaining
    if args.auto_validate_csv:
        csv_path = args.auto_validate_csv.strip()
        scope = (args.validate_scope or "uncertain_only").strip().lower()
        try:
            result_path = run_auto_validate(csv_path, validate_scope=scope, logger=print)
        except Exception as exc:
            print(f"[Auto-Validate] Failed safely: {exc}")
        return True, [argv[0]] + remaining
    if cli_requested:
        scrape_soundcloud(
            (args.soundcloud_url or "").strip(),
            seed_tags=args.soundcloud_tags,
            existing_csv="artist_social_links.csv",
            max_artists=args.max_artists or 200,
            max_handles=args.max_handles,
            min_yield=args.min_yield if args.min_yield is not None else 3,
            dry_run=args.dry_run,
        )
        return True, [argv[0]] + remaining
    return False, [argv[0]] + remaining


if __name__ == "__main__":
    ran_cli, qt_args = _handle_cli_entry(sys.argv)
    if ran_cli:
        sys.exit(0)
    app = QtWidgets.QApplication(qt_args)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

# ---------------------------
# Unearthed full pipeline entrypoint (wrapper)
# ---------------------------
def run_unearthed_pipeline(
    search_term: str = "",
    region: str | None = None,
    max_results: int | None = None,
    headless: bool = True,
    output_csv: str | None = None,
    job_config: dict | None = None,
    fb_session=None,
):
    """
    Best-effort Unearthed pipeline wrapper to match legacy entrypoints.

    - Accepts a search_term/URL and optional max_results.
    - Reuses the existing Unearthed scraper (scrape_website), which already
      visits each profile, collects socials, and attempts email extraction
      from the profile page + linked Facebook pages when available.
    - Writes to output_csv if provided; otherwise returns the path used.
    """
    target_url = (search_term or "").strip() or UNEARTHED_DEFAULT_URL
    target_max = max_results or (job_config.get("target_valid_leads") if job_config else None)
    out_path = output_csv or (job_config.get("output_csv") if job_config else "") or "unearthed_output.csv"
    if job_config and job_config.get("backfill_unearthed_url_index"):
        backfill_sources = job_config.get("unearthed_url_index_backfill_sources") or []
        index_path = _resolve_unearthed_url_index_path(job_config)
        backfill_unearthed_artist_url_index(backfill_sources, index_path=index_path)
        if job_config.get("unearthed_url_index_backfill_only"):
            save_to_csv([], out_path)
            return out_path
    scrape_website(
        target_url,
        existing_csv=out_path,
        max_artists=target_max or 200,
        fb_session=fb_session,
        job_config=job_config,
    )
    return out_path

# ---------------------------
# Stop caffeinate if it was started (macOS)
# ---------------------------
if caffeinate_proc:
    print("Stopping caffeinate. You may now allow sleep.")
    caffeinate_proc.kill()
