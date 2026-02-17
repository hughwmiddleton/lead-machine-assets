"""Seed-phase runner for Night Mode v2.

This module is intentionally minimal and self-contained. It reuses the
existing v1 job execution helpers without altering their behaviour.
"""

import json
import os
import hashlib
from typing import Any, Dict

import pandas as pd

from night_mode_v2.manifest import write_manifest, load_manifest, config_hash
from night_mode_v2.cache_policy import should_skip_phase
from night_mode_v2.schema_registry import validate_schema

import pipeline_runner
import night_mode_runner


def _ensure_manifest_skeleton(manifest: Dict[str, Any], run_dir: str, cfg_hash: str) -> Dict[str, Any]:
    manifest.setdefault("schema_version", "2.0")
    manifest["config_hash"] = cfg_hash
    manifest.setdefault("run_dir", run_dir)
    phases = manifest.setdefault("phases", {})
    seed_phase = phases.setdefault("seed", {})
    seed_phase.setdefault("status", "pending")
    seed_phase.setdefault("jobs", {})
    return manifest


def _safe_count_rows(csv_path: str) -> int:
    if not os.path.exists(csv_path):
        return 0
    try:
        df = pd.read_csv(csv_path)
        return len(df.index)
    except Exception:
        return 0


def _schema_hash_from_df(df: pd.DataFrame) -> str:
    cols = sorted(df.columns)
    joined = "|".join(cols)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_seed_phase(config_path: str, run_dir: str, resume: bool = False) -> Dict[str, Any]:
    config = _load_config(config_path)
    cfg_hash = config_hash(config)
    manifest_path = os.path.join(run_dir, "run_manifest_v2.json")

    existing_manifest: Dict[str, Any] = {}
    if resume and os.path.exists(manifest_path):
        try:
            existing_manifest = load_manifest(manifest_path)
        except ValueError:
            existing_manifest = {}

    manifest = _ensure_manifest_skeleton(existing_manifest or {}, run_dir, cfg_hash)
    seed_phase = manifest["phases"]["seed"]
    seed_jobs = seed_phase.get("jobs", {})

    os.makedirs(run_dir, exist_ok=True)

    jobs = config.get("jobs", []) or []
    total_jobs = len(jobs)
    completed_jobs = 0
    failed_jobs = 0

    for idx, job in enumerate(jobs):
        job_id = job.get("job_id") or job.get("id") or f"job_{idx + 1}"
        job_dir = os.path.join(run_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        raw_csv = os.path.join(job_dir, "raw.csv")

        required_outputs_exist = os.path.exists(raw_csv)
        schema_valid = False
        df_for_schema = None
        if required_outputs_exist:
            try:
                df_for_schema = pd.read_csv(raw_csv)
                schema_valid, _, _ = validate_schema(df_for_schema, phase="seed")
            except Exception:
                schema_valid = False

        job_entry = seed_jobs.get(job_id, {})
        job_completed = job_entry.get("status") == "completed"

        manifest_for_skip = {
            "config_hash": manifest.get("config_hash"),
            "phases": {"seed": {"status": job_entry.get("status") or seed_phase.get("status")}},
        }

        should_skip = False
        if resume and job_completed:
            should_skip = should_skip_phase(
                manifest_for_skip,
                phase="seed",
                current_config_hash=cfg_hash,
                required_outputs_exist=required_outputs_exist,
                schema_valid=schema_valid,
            )

        if should_skip:
            # Ensure metadata is present even when skipping.
            row_count = job_entry.get("row_count") or _safe_count_rows(raw_csv)
            schema_hash = job_entry.get("schema_hash")
            if not schema_hash and df_for_schema is not None:
                schema_hash = _schema_hash_from_df(df_for_schema)
            status = job_entry.get("status") or "completed"
        else:
            try:
                try:
                    pipeline_runner.run_directory_job(job, raw_csv, logger=None)
                except TypeError:
                    night_mode_runner._process_job(
                        job=job,
                        run_dir=run_dir,
                        resume=False,
                        stop_on_failure=False,
                        per_job_validate=False,
                    )
            except Exception:
                # Best effort: mark failure and continue.
                row_count = _safe_count_rows(raw_csv)
                schema_hash = ""
                status = "failed"
            else:
                try:
                    df_for_schema = pd.read_csv(raw_csv) if os.path.exists(raw_csv) else None
                except Exception:
                    df_for_schema = None

                if df_for_schema is not None:
                    row_count = len(df_for_schema.index)
                    schema_hash = _schema_hash_from_df(df_for_schema)
                else:
                    row_count = _safe_count_rows(raw_csv)
                    schema_hash = ""

                status = "completed" if row_count > 0 else "failed"

        seed_jobs[job_id] = {
            "status": status,
            "directory": job_dir,
            "raw_csv": raw_csv,
            "row_count": int(row_count),
            "schema_hash": schema_hash or "",
        }

        if status == "completed":
            completed_jobs += 1
        else:
            failed_jobs += 1

        seed_phase["jobs"] = seed_jobs
        write_manifest(manifest_path, manifest)

    if total_jobs == 0:
        phase_status = "completed"
    elif completed_jobs == total_jobs:
        phase_status = "completed"
    elif completed_jobs > 0:
        phase_status = "partial"
    else:
        phase_status = "failed"

    seed_phase["status"] = phase_status
    manifest["phases"]["seed"] = seed_phase
    write_manifest(manifest_path, manifest)
    return manifest


def run_seed_only(config_path: str, run_dir: str, resume: bool = False) -> Dict[str, Any]:
    return run_seed_phase(config_path, run_dir, resume=resume)

