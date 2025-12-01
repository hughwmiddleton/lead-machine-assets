"""Night Mode orchestration layer for Lead Machine.

This module coordinates multiple directory scrapes + enrichment steps in a
single unattended run, writing per-job outputs and an optional merged master.

Usage:
    python night_mode_runner.py --config overnight_jobs.json
    python night_mode_runner.py --config overnight_jobs.json --resume
    python night_mode_runner.py --config overnight_jobs.json --export-mode both
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from pipeline_runner import run_directory_job, run_enrichment

DEFAULT_EXPORT_MODE = "both"
MAX_CONSECUTIVE_ERRORS = 10
CHECKPOINT_INTERVAL_ROWS = 5
STATE_FILENAME = "state.json"
LOG_FILENAME = "log.txt"


def _setup_logger(log_path: str, job_id: str) -> logging.Logger:
    logger = logging.getLogger(f"night_mode.{job_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream_handler_present = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers)
    if not stream_handler_present:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    file_handler_present = any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(log_path) for h in logger.handlers)
    if not file_handler_present:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload["last_update"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _count_rows(csv_path: str) -> int:
    if not csv_path or not os.path.exists(csv_path):
        return 0
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        try:
            df = pd.read_csv(csv_path)
            return len(df.index)
        except Exception:
            return 0


def _primary_url_from_row(row: pd.Series) -> str:
    for col in ("Source URL", "SoundCloud Link", "Spotify_URL", "External Links", "Social Link"):
        if col not in row:
            continue
        value = str(row.get(col) or "").strip()
        if not value:
            continue
        if col == "External Links" and "|" in value:
            return value.split("|", 1)[0].strip()
        return value
    return ""


def _dedupe_master(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work["__email_key"] = work.get("Email", pd.Series(dtype=str)).fillna("").astype(str).str.strip().str.lower()
    work["__artist_key"] = work.get("Artist Name", pd.Series(dtype=str)).fillna("").astype(str).str.strip().str.lower()
    work["__primary_url"] = work.apply(_primary_url_from_row, axis=1)

    def _key(row: pd.Series) -> str:
        email = row.get("__email_key", "")
        if email:
            return f"email::{email}"
        artist = row.get("__artist_key", "")
        url = str(row.get("__primary_url", "") or "").strip().lower()
        if artist and url:
            return f"artist::{artist}|{url}"
        return f"row::{row.name}"

    work["__dedupe_key"] = work.apply(_key, axis=1)
    deduped = work.drop_duplicates(subset="__dedupe_key").drop(columns=["__dedupe_key", "__email_key", "__artist_key", "__primary_url"])
    return deduped


def _discover_latest_run_dir(root: str) -> Optional[str]:
    if not os.path.exists(root):
        return None
    candidates = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            candidates.append((os.path.getmtime(path), path))
    if not candidates:
        return None
    candidates.sort(key=lambda tup: tup[0], reverse=True)
    return candidates[0][1]


def _ensure_run_dir(resume: bool, run_root: str) -> Tuple[str, bool]:
    os.makedirs(run_root, exist_ok=True)
    if resume:
        latest = _discover_latest_run_dir(run_root)
        if latest:
            return latest, False
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = os.path.join(run_root, timestamp)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir, True


def _merge_master(run_dir: str, job_states: List[Dict[str, Any]], logger: logging.Logger) -> Optional[str]:
    enriched_paths = []
    for state in job_states:
        enriched_path = state.get("enriched_csv") or ""
        if enriched_path and os.path.exists(enriched_path):
            enriched_paths.append((state.get("job_id", ""), enriched_path))
    if not enriched_paths:
        logger.warning("[Master] No enriched CSVs found to merge.")
        return None

    frames = []
    for job_id, path in enriched_paths:
        try:
            df = pd.read_csv(path)
            df["__source_job"] = job_id
            frames.append(df)
        except Exception as exc:
            logger.warning("[Master] Skipping %s due to read error: %s", path, exc)
    if not frames:
        logger.warning("[Master] No data available after reading enriched files.")
        return None

    combined = pd.concat(frames, ignore_index=True, sort=False)
    deduped = _dedupe_master(combined)
    master_path = os.path.join(run_dir, "master_enriched_deduped.csv")
    deduped.to_csv(master_path, index=False)
    logger.info("[Master] Wrote merged deduped CSV: %s (rows=%s)", master_path, len(deduped.index))
    return master_path


def _load_state(state_path: str) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        return {}
    try:
        return _load_json(state_path)
    except Exception:
        return {}


def _process_job(
    job: Dict[str, Any],
    run_dir: str,
    resume: bool,
    stop_on_failure: bool,
) -> Dict[str, Any]:
    job_id = job.get("job_id") or f"job_{len(job)}"
    job_dir = os.path.join(run_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    log_path = os.path.join(job_dir, LOG_FILENAME)
    state_path = os.path.join(job_dir, STATE_FILENAME)
    logger = _setup_logger(log_path, job_id)
    state = _load_state(state_path)
    state.update(
        {
            "job_id": job_id,
            "raw_csv": os.path.join(job_dir, "raw.csv"),
            "enriched_csv": os.path.join(job_dir, "enriched.csv"),
            "input_seed_csv": job.get("input_seed_csv", ""),
            "status": state.get("status") or "pending",
            "error_count": state.get("error_count", 0),
        }
    )

    if resume and state.get("status") == "completed":
        logger.info("Skipping %s (already completed).", job_id)
        return state

    start_time = time.time()
    state["status"] = "running"
    _write_json(state_path, state)

    try:
        if resume and os.path.exists(state["raw_csv"]):
            logger.info("Resume: raw CSV already exists at %s; skipping scrape step.", state["raw_csv"])
        else:
            logger.info("Starting scrape for job %s", job_id)
            run_directory_job(job, state["raw_csv"], logger=logger.info)
        state["current_row_index"] = max(_count_rows(state["raw_csv"]) - 1, 0)
        state["valid_leads_so_far"] = state.get("valid_leads_so_far", 0) + state["current_row_index"]
        _write_json(state_path, state)

        max_hours = job.get("max_hours")
        if max_hours and (time.time() - start_time) > float(max_hours) * 3600:
            state["status"] = "partial_timeout"
            _write_json(state_path, state)
            logger.warning("Job %s hit max_hours=%s; marking partial_timeout.", job_id, max_hours)
            return state

        logger.info("Starting enrichment for job %s", job_id)
        final_enriched = run_enrichment(state["raw_csv"], state["enriched_csv"], logger=logger.info)
        state["enriched_csv"] = final_enriched
        state["valid_leads_so_far"] = _count_rows(final_enriched)
        state["status"] = "completed"
        _write_json(state_path, state)
        logger.info("Completed job %s", job_id)
        return state
    except Exception as exc:
        state["error_count"] = state.get("error_count", 0) + 1
        state["status"] = "failed" if state["error_count"] >= MAX_CONSECUTIVE_ERRORS else "partial_error"
        _write_json(state_path, state)
        logger.error("Job %s failed: %s", job_id, exc)
        logger.error(traceback.format_exc())
        if stop_on_failure:
            raise
        return state


def run_night_mode(
    config_path: str,
    resume: bool = False,
    stop_on_failure: bool = False,
    export_mode_override: Optional[str] = None,
    run_root: str = "overnight_runs",
) -> Dict[str, Any]:
    config = _load_json(config_path)
    export_mode = (export_mode_override or config.get("export_mode") or DEFAULT_EXPORT_MODE).strip().lower()
    if export_mode not in {"per_directory", "combined", "both"}:
        export_mode = DEFAULT_EXPORT_MODE
    run_dir, created_new = _ensure_run_dir(resume, run_root)
    if created_new:
        snapshot_path = os.path.join(run_dir, "config_snapshot.json")
        _write_json(snapshot_path, config)

    job_states: List[Dict[str, Any]] = []
    for job in config.get("jobs", []):
        result_state = _process_job(job, run_dir, resume=resume, stop_on_failure=stop_on_failure)
        job_states.append(result_state)
        if stop_on_failure and result_state.get("status") in {"failed"}:
            break

    master_path = None
    if export_mode in {"combined", "both"}:
        logger = _setup_logger(os.path.join(run_dir, "master_log.txt"), "master")
        master_path = _merge_master(run_dir, job_states, logger)

    return {"run_dir": run_dir, "jobs": job_states, "master_csv": master_path, "export_mode": export_mode}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Night Mode runner for Lead Machine")
    parser.add_argument("--config", required=True, help="Path to overnight_jobs.json config file")
    parser.add_argument("--resume", action="store_true", help="Resume the latest overnight run")
    parser.add_argument("--stop-on-failure", action="store_true", help="Abort all jobs if any job fails")
    parser.add_argument(
        "--export-mode",
        choices=["per_directory", "combined", "both"],
        help="Override export mode from the config file",
    )
    parser.add_argument(
        "--run-root",
        default="overnight_runs",
        help="Root directory for overnight run outputs (defaults to ./overnight_runs)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    result = run_night_mode(
        config_path=args.config,
        resume=args.resume,
        stop_on_failure=args.stop_on_failure,
        export_mode_override=args.export_mode,
        run_root=args.run_root,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
