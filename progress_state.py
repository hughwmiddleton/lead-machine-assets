"""Runtime progress state for Lead Machine runs.

This module is intentionally observational: callers own row counts and phase
transitions; this module only persists the supplied state with deterministic
derived fields.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional


DEFAULT_PROGRESS_FILE = os.path.join("data", "runtime_progress", "current_run_progress.json")
PROGRESS_FILE_ENV = "LEAD_MACHINE_PROGRESS_FILE"
ALLOWED_META_FIELDS = {
    "emails_found",
    "rows_skipped",
    "duplicates_removed",
    "discovered_urls",
    "current_source",
    "current_status",
    "phase",
}
IDLE_PROGRESS = {"phase": "idle", "processed_rows": 0, "total_rows": None}

_last_discovery_write_at = 0.0


def _progress_file_path() -> str:
    return os.environ.get(PROGRESS_FILE_ENV, "").strip() or DEFAULT_PROGRESS_FILE


def _now() -> float:
    return time.time()


def _filtered_meta(meta: Optional[dict]) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    return {key: value for key, value in meta.items() if key in ALLOWED_META_FIELDS}


def _percentage(processed_rows: int, total_rows: Optional[int]) -> Optional[float]:
    if total_rows is None:
        return None
    if total_rows <= 0:
        return 100.0 if processed_rows > 0 else 0.0
    if processed_rows > total_rows:
        return 100.0
    return round((processed_rows / total_rows) * 100, 1)


def _eta_seconds(processed_rows: int, total_rows: Optional[int], elapsed_seconds: float) -> Optional[int]:
    if processed_rows < 10 or total_rows is None:
        return None
    if elapsed_seconds <= 0 or elapsed_seconds < 1:
        return None
    rows_per_second = processed_rows / elapsed_seconds
    if rows_per_second <= 0:
        return None
    remaining = max(total_rows - processed_rows, 0)
    return int(round(remaining / rows_per_second))


def _atomic_write(payload: Dict[str, Any]) -> None:
    path = _progress_file_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_total(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _build_payload(
    *,
    base: Optional[Dict[str, Any]],
    processed_rows: int,
    total_rows: Optional[int],
    meta: Optional[dict],
    now: float,
) -> Dict[str, Any]:
    payload = dict(base or {})
    start_time = payload.get("start_time")
    if start_time is None:
        start_time = now
    try:
        start_time = float(start_time)
    except Exception:
        start_time = now
    elapsed_seconds = now - start_time
    payload.update(
        {
            "processed_rows": int(processed_rows),
            "total_rows": total_rows,
            "percentage": _percentage(int(processed_rows), total_rows),
            "start_time": start_time,
            "last_update_time": now,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": _eta_seconds(int(processed_rows), total_rows, elapsed_seconds),
        }
    )
    payload.update(_filtered_meta(meta))
    return payload


def init_progress(total_rows: Optional[int], run_id: str, meta: Optional[dict] = None):
    """Start a fresh progress file, overwriting any previous run."""
    global _last_discovery_write_at
    _last_discovery_write_at = 0.0
    now = _now()
    payload = _build_payload(
        base={"run_id": str(run_id or ""), "phase": "processing"},
        processed_rows=0,
        total_rows=_coerce_total(total_rows),
        meta=meta,
        now=now,
    )
    _atomic_write(payload)


def update_progress(processed_rows: int, meta: Optional[dict] = None):
    """Persist caller-owned progress, deriving percentage and ETA only."""
    global _last_discovery_write_at
    current = read_progress()
    now = _now()
    meta_fields = _filtered_meta(meta)
    if meta_fields.get("phase") == "discovery":
        if _last_discovery_write_at and (now - _last_discovery_write_at) < 0.25:
            return
        _last_discovery_write_at = now
    total_rows = _coerce_total(current.get("total_rows"))
    payload = _build_payload(
        base=current if current.get("phase") != "idle" else {},
        processed_rows=_coerce_int(processed_rows),
        total_rows=total_rows,
        meta=meta_fields,
        now=now,
    )
    _atomic_write(payload)


def finalize_progress(meta: Optional[dict] = None):
    """Mark the active run complete while preserving final row counters."""
    current = read_progress()
    now = _now()
    processed_rows = _coerce_int(current.get("processed_rows"))
    total_rows = _coerce_total(current.get("total_rows"))
    payload = _build_payload(
        base=current if current.get("phase") != "idle" else {},
        processed_rows=processed_rows,
        total_rows=total_rows,
        meta=meta,
        now=now,
    )
    payload.update({"phase": "complete", "percentage": 100.0, "eta_seconds": 0})
    _atomic_write(payload)


def read_progress() -> dict:
    """Return the current progress payload, or idle for missing/malformed files."""
    path = _progress_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return dict(IDLE_PROGRESS)
        return payload
    except Exception:
        return dict(IDLE_PROGRESS)
