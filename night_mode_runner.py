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
import re
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import pipeline_runner
from pipeline_runner import (
    FacebookGlobalPassStatus,
    run_directory_job,
    run_enrichment,
    run_facebook_global_pass_nightmode,
    run_master_enrichment,
)

DEFAULT_EXPORT_MODE = "both"
MAX_CONSECUTIVE_ERRORS = 10
CHECKPOINT_INTERVAL_ROWS = 5
STATE_FILENAME = "state.json"
LOG_FILENAME = "log.txt"
FACEBOOK_STATE_FILENAME = "facebook_state.json"
EMAIL_PRIORITY_COLS = getattr(pipeline_runner, "EMAIL_PRIORITY_COLS", ("Email", "Email_All", "Directory_Email", "Unearthed_Email"))
EXCLUDED_URL_SUBSTRINGS = (
    "soundcloud.com/triplejunearthed",
    "tiktok.com/@triplejradio",
    "youtube.com/abcaustralia",
)


def _strip_excluded_urls(url_val: str) -> str:
    """
    Remove known platform-owned URLs (e.g., triple j Unearthed) so they never reach the client CSV.
    """
    raw = str(url_val or "")
    if not raw:
        return ""
    # Split on whitespace or common separators (pipe/semicolon/comma).
    parts = re.split("[\\s,;|]+", raw)
    kept = [p for p in parts if p and not any(ex in p.lower() for ex in EXCLUDED_URL_SUBSTRINGS)]
    return " | ".join(kept)


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


def _coalesce_emails(df: pd.DataFrame) -> pd.DataFrame:
    """
    Backfill the Email column across known email fields without overwriting
    existing directory-provided values with blanks.
    """
    if df is None or df.empty:
        return df
    existing = [c for c in EMAIL_PRIORITY_COLS if c in df.columns]
    if not existing:
        return df
    # Ensure Email_All includes Email when Email_All is empty
    if "Email_All" in df.columns and "Email" in df.columns:
        email_all = df["Email_All"].fillna("").astype(str)
        email_col = df["Email"].fillna("").astype(str)
        mask_all = email_all.str.strip() == ""
        df.loc[mask_all, "Email_All"] = email_col[mask_all]
    email_series = df[existing].bfill(axis=1).iloc[:, 0].fillna("").astype(str)
    df["Email"] = email_series.str.strip()
    return df


def _dedupe_master(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work["__email_key"] = work.get("Email", pd.Series(dtype=str)).fillna("").astype(str).str.strip().str.lower()
    work["__artist_key"] = work.get("Artist Name", pd.Series(dtype=str)).fillna("").astype(str).str.strip().str.lower()
    work["__primary_url"] = work.apply(_primary_url_from_row, axis=1)
    work["__has_email"] = work.get("Email", pd.Series(dtype=str)).fillna("").astype(str).str.strip() != ""
    # Prefer rows that carry a Facebook clue when de-duplicating.
    def _has_fb(row: pd.Series) -> bool:
        for col in ("Facebook_URL", "Social Link", "External Links"):
            if col not in row:
                continue
            val = str(row.get(col) or "").lower()
            if "facebook.com" in val or "fb.me" in val:
                return True
        return False

    work["__has_fb"] = work.apply(_has_fb, axis=1)

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
    work = work.sort_values(["__dedupe_key", "__has_email", "__has_fb"], ascending=[True, False, False])
    deduped = work.drop_duplicates(subset="__dedupe_key").drop(columns=["__dedupe_key", "__email_key", "__artist_key", "__primary_url", "__has_email", "__has_fb"])
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
    for col in ("Source URL", "SoundCloud Link", "Social Link", "External Links", "Facebook_URL"):
        if col in combined.columns:
            combined[col] = combined[col].fillna("").astype(str).apply(_strip_excluded_urls)

    combined = _coalesce_emails(combined)
    try:
        unearthed_mask = combined.get("Source Directory", pd.Series(dtype=str)).astype(str).str.contains("unearthed", case=False, na=False)
        sample = combined.loc[unearthed_mask, ["Artist Name", "Source Directory", "Email", "Email_All"]].head()
        if not sample.empty:
            logger.info("[Debug Unearthed] Sample after raw merge:\n%s", sample.to_string(index=False))
    except Exception:
        pass
    deduped = _dedupe_master(combined)
    master_path = os.path.join(run_dir, "master_pre_fb.csv")
    deduped.to_csv(master_path, index=False)
    logger.info("[Master] Wrote merged pre-Facebook CSV: %s (rows=%s)", master_path, len(deduped.index))
    return master_path


def _merge_raw_master(run_dir: str, job_states: List[Dict[str, Any]], logger: logging.Logger) -> Optional[str]:
    raw_paths = []
    for state in job_states:
        raw_path = state.get("raw_csv") or ""
        if raw_path and os.path.exists(raw_path):
            raw_paths.append((state.get("job_id", ""), raw_path))
    if not raw_paths:
        logger.warning("[Master] No raw CSVs found to merge.")
        return None

    frames = []
    for job_id, path in raw_paths:
        try:
            df = pd.read_csv(path)
            df["__source_job"] = job_id
            frames.append(df)
        except Exception as exc:
            logger.warning("[Master] Skipping %s due to read error: %s", path, exc)
    if not frames:
        logger.warning("[Master] No data available after reading raw files.")
        return None
    combined = pd.concat(frames, ignore_index=True, sort=False)
    for col in ("Source URL", "SoundCloud Link", "Social Link", "External Links", "Facebook_URL"):
        if col in combined.columns:
            combined[col] = combined[col].fillna("").astype(str).apply(_strip_excluded_urls)
    combined = _coalesce_emails(combined)
    try:
        unearthed_mask = combined.get("Source Directory", pd.Series(dtype=str)).astype(str).str.contains("unearthed", case=False, na=False)
        sample = combined.loc[unearthed_mask, ["Artist Name", "Source Directory", "Email", "Email_All"]].head()
        if not sample.empty:
            logger.info("[Debug Unearthed] Sample after enriched merge:\n%s", sample.to_string(index=False))
    except Exception:
        pass
    master_path = os.path.join(run_dir, "master_raw.csv")
    combined.to_csv(master_path, index=False)
    logger.info("[Master] Wrote merged raw CSV: %s (rows=%s)", master_path, len(combined.index))
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
    per_job_validate: bool = True,
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

        if per_job_validate:
            logger.info("Starting enrichment for job %s", job_id)
            final_enriched = run_enrichment(state["raw_csv"], state["enriched_csv"], logger=logger.info, night_mode=True)
            state["enriched_csv"] = final_enriched
            state["valid_leads_so_far"] = _count_rows(final_enriched)
            state["status"] = "completed"
            _write_json(state_path, state)
            logger.info("Completed job %s", job_id)
        else:
            # Skip per-job validation; leave enrichment for master stage.
            state["enriched_csv"] = state["raw_csv"]
            state["status"] = "completed"
            _write_json(state_path, state)
            logger.info("Completed job %s (raw only; master enrichment pending)", job_id)
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
    export_profile_override: Optional[str] = None,
    run_root: str = "overnight_runs",
    fb_auto_resume_override: Optional[bool] = None,
    fb_cooldown_override: Optional[int] = None,
    fb_max_attempts_override: Optional[int] = None,
    fb_max_rows_override: Optional[int] = None,
) -> Dict[str, Any]:
    config = _load_json(config_path)
    export_mode = (export_mode_override or config.get("export_mode") or DEFAULT_EXPORT_MODE).strip().lower()
    if export_mode not in {"per_directory", "combined", "both"}:
        export_mode = DEFAULT_EXPORT_MODE
    export_profile = (export_profile_override or config.get("export_profile") or "full_dump").strip().lower()
    if export_profile not in {"studio_safe", "studio_plus", "unearthed_social", "full_dump"}:
        export_profile = "full_dump"
    master_enrich_cfg = config.get("master_enrichment", {})
    master_enrichment_enabled = master_enrich_cfg.get("enabled", True)

    fb_cfg = config.get("facebook", {}) or {}
    fb_auto_resume = fb_cfg.get("auto_resume_after_captcha", False) if fb_auto_resume_override is None else bool(fb_auto_resume_override)
    fb_cooldown_seconds = int(fb_cfg.get("cooldown_seconds", 600)) if fb_cooldown_override is None else int(fb_cooldown_override)
    fb_max_auto_resume_attempts = (
        int(fb_cfg.get("max_auto_resume_attempts", 1)) if fb_max_attempts_override is None else int(fb_max_attempts_override)
    )
    fb_max_rows_config = fb_cfg.get("max_rows_per_run", config.get("facebook_max_rows_per_run", 100))
    fb_max_rows_per_run = fb_max_rows_config if fb_max_rows_override is None else fb_max_rows_override
    if fb_max_rows_per_run is None:
        fb_max_rows_per_run = 100
    try:
        fb_max_rows_per_run = int(fb_max_rows_per_run)
    except Exception:
        fb_max_rows_per_run = 100
    if fb_max_rows_per_run < 0:
        fb_max_rows_per_run = 0
    run_dir, created_new = _ensure_run_dir(resume, run_root)
    if created_new:
        snapshot_path = os.path.join(run_dir, "config_snapshot.json")
        _write_json(snapshot_path, config)

    job_states: List[Dict[str, Any]] = []
    for job in config.get("jobs", []):
        result_state = _process_job(
            job,
            run_dir,
            resume=resume,
            stop_on_failure=stop_on_failure,
            per_job_validate=not master_enrichment_enabled,
        )
        job_states.append(result_state)
        if stop_on_failure and result_state.get("status") in {"failed"}:
            break

    master_raw = None
    master_enriched = None
    master_pre_fb = None
    master_post_fb = None
    master_final = None
    if export_mode in {"combined", "both"}:
        logger = _setup_logger(os.path.join(run_dir, "master_log.txt"), "master")
        if master_enrichment_enabled:
            master_raw = _merge_raw_master(run_dir, job_states, logger)
            if master_raw and os.path.exists(master_raw):
                master_enriched = os.path.join(run_dir, "master_enriched.csv")
                master_enriched = run_master_enrichment(master_raw, master_enriched, logger=logger.info)
                master_pre_fb = os.path.join(run_dir, "master_pre_fb.csv")
                master_pre_fb = run_enrichment(master_enriched, master_pre_fb, logger=logger.info, night_mode=True)
        else:
            master_pre_fb = _merge_master(run_dir, job_states, logger)
        if master_pre_fb and os.path.exists(master_pre_fb):
            master_post_fb = os.path.join(run_dir, "master_post_fb.csv")
            fb_state_path = os.path.join(run_dir, FACEBOOK_STATE_FILENAME)
            fb_state = _load_state(fb_state_path)
            fb_completed = bool(fb_state.get("fb_run_completed") and not fb_state.get("fb_captcha_flag"))
            fb_limit_hit = bool(fb_state.get("fb_limit_reached"))
            fb_rows_total = fb_state.get("fb_total_rows")
            try:
                fb_status: Optional[FacebookGlobalPassStatus] = None
                if resume and fb_completed and os.path.exists(master_post_fb):
                    logger.info("[Master] Resume: Facebook global pass already completed; skipping.")
                    fb_status = FacebookGlobalPassStatus(
                        processed_rows=fb_state.get("fb_completed", 0) or 0,
                        total_rows=fb_state.get("fb_total_rows", 0) or 0,
                        completed=True,
                        hit_captcha=False,
                        limit_reached=False,
                        attempted_total=fb_state.get("fb_attempted_total", 0) or 0,
                    )
                else:
                    attempt = 0
                    while True:
                        fb_status = run_facebook_global_pass_nightmode(
                            master_pre_fb,
                            master_post_fb,
                            state_path=fb_state_path,
                            max_rows_per_run=fb_max_rows_per_run,
                            logger=logger.info,
                        )
                        fb_state = _load_state(fb_state_path)
                        fb_completed = fb_status.completed
                        fb_limit_hit = fb_status.limit_reached
                        fb_rows_total = fb_state.get("fb_total_rows")
                        if fb_status.hit_captcha and fb_auto_resume and attempt < fb_max_auto_resume_attempts:
                            attempt += 1
                            logger.info(
                                "[Master] Captcha detected; cooling down for %s seconds before retry (%s/%s).",
                                fb_cooldown_seconds,
                                attempt,
                                fb_max_auto_resume_attempts,
                            )
                            time.sleep(max(fb_cooldown_seconds, 0))
                            continue
                        break
                if fb_status and fb_status.completed:
                    logger.info("[Master] Facebook global pass completed: %s", master_post_fb)
                else:
                    logger.info(
                        "[Master] Facebook global pass partial (limit_hit=%s captcha=%s total_rows=%s)",
                        fb_limit_hit,
                        bool(fb_state.get("fb_captcha_flag")),
                        fb_rows_total,
                    )
            except Exception as exc:
                logger.error("[Master] Facebook global pass failed safely: %s", exc)
                master_post_fb = master_pre_fb
                fb_completed = False
            master_final = os.path.join(run_dir, "master_enriched_deduped.csv")
            if fb_completed:
                try:
                    run_enrichment(master_post_fb, master_final, logger=logger.info, night_mode=True)
                    logger.info("[Master] Validation completed: %s", master_final)
                    export_path = os.path.join(run_dir, "master_export_leads.csv")
                    pipeline_runner.export_master_leads(
                        input_csv=master_final,
                        output_csv=export_path,
                        logger=logger,
                        export_profile=export_profile,
                    )
                    logger.info("[Master] Exported client-facing leads CSV: %s", export_path)
                except Exception as exc:
                    logger.error("[Master] Final validation failed safely: %s", exc)
                    master_final = master_post_fb
            else:
                master_final = master_post_fb

    return {
        "run_dir": run_dir,
        "jobs": job_states,
        "master_raw": master_raw,
        "master_enriched": master_enriched,
        "master_pre_fb": master_pre_fb,
        "master_post_fb": master_post_fb,
        "master_csv": master_final,
        "export_mode": export_mode,
    }


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
        "--export-profile",
        choices=["studio_safe", "studio_plus", "unearthed_social", "full_dump"],
        help="Filter exported leads by strictness profile",
    )
    parser.add_argument(
        "--run-root",
        default="overnight_runs",
        help="Root directory for overnight run outputs (defaults to ./overnight_runs)",
    )
    parser.add_argument("--fb-auto-resume", action="store_true", help="Automatically resume FB pass after captcha once configured")
    parser.add_argument("--fb-cooldown-seconds", type=int, help="Cooldown in seconds before auto-resume after captcha")
    parser.add_argument(
        "--fb-max-auto-resume-attempts",
        type=int,
        help="Maximum auto-resume attempts for FB pass after captcha (default from config or 1)",
    )
    parser.add_argument("--fb-max-rows-per-run", type=int, help="Limit FB rows per Night Mode run")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    result = run_night_mode(
        config_path=args.config,
        resume=args.resume,
        stop_on_failure=args.stop_on_failure,
        export_mode_override=args.export_mode,
        export_profile_override=args.export_profile,
        run_root=args.run_root,
        fb_auto_resume_override=args.fb_auto_resume,
        fb_cooldown_override=args.fb_cooldown_seconds,
        fb_max_attempts_override=args.fb_max_auto_resume_attempts,
        fb_max_rows_override=args.fb_max_rows_per_run,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
