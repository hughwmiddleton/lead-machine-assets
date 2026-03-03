"""Seed-phase runner for Night Mode v2.

This module is intentionally minimal and self-contained. It reuses the
existing v1 job execution helpers without altering their behaviour.
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from night_mode_v2.cache_policy import should_skip_phase
from night_mode_v2.manifest import config_hash, load_manifest, write_manifest
from night_mode_v2.schema_registry import validate_schema

import night_mode_runner
import pipeline_runner


def _ensure_manifest_skeleton(manifest: Dict[str, Any], run_dir: str, cfg_hash: str) -> Dict[str, Any]:
    manifest.setdefault("schema_version", "2.0")
    manifest["config_hash"] = cfg_hash
    manifest.setdefault("run_dir", run_dir)
    phases = manifest.setdefault("phases", {})
    seed_phase = phases.setdefault("seed", {})
    seed_phase.setdefault("status", "pending")
    seed_phase.setdefault("jobs", {})
    enrich_phase = phases.setdefault("enrich", {})
    enrich_phase.setdefault("status", "pending")
    enrich_phase.setdefault("outputs", {})
    contact_phase = phases.setdefault("contact", {})
    contact_phase.setdefault("status", "pending")
    contact_phase.setdefault("outputs", {})
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

def _job_status_path(job_dir: str) -> str:
    return os.path.join(job_dir, "job_status.json")


def _write_job_status(job_dir: str, payload: Dict[str, Any]) -> None:
    path = _job_status_path(job_dir)
    os.makedirs(job_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


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
        raw_tmp = os.path.join(job_dir, "raw.tmp.csv")
        _write_job_status(
            job_dir,
            {
                "job_id": job_id,
                "status": "running",
                "row_count": 0,
                "raw_exists": False,
                "raw_bytes": 0,
                "error": "",
            },
        )
        if os.path.exists(raw_tmp):
            try:
                os.remove(raw_tmp)
            except Exception:
                pass

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

        note_msg: Optional[str] = None

        if should_skip:
            # Ensure metadata is present even when skipping.
            row_count = job_entry.get("row_count") or _safe_count_rows(raw_csv)
            schema_hash = job_entry.get("schema_hash")
            if not schema_hash and df_for_schema is not None:
                schema_hash = _schema_hash_from_df(df_for_schema)
            status = job_entry.get("status") or "completed"
            raw_exists = os.path.exists(raw_csv)
            raw_bytes = os.path.getsize(raw_csv) if raw_exists else 0
            error_msg = job_entry.get("error", "")
        else:
            error_msg = ""
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
                pipeline_runner.ensure_final_raw_csv(raw_csv, job_id, logger=None)
            except Exception as exc:
                # Best effort: mark failure and continue.
                row_count = _safe_count_rows(raw_csv)
                schema_hash = ""
                status = "failed"
                raw_exists = os.path.exists(raw_csv)
                raw_bytes = os.path.getsize(raw_csv) if raw_exists else 0
                error_msg = f"{exc.__class__.__name__}: {exc}"
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

                raw_exists = os.path.exists(raw_csv)
                raw_bytes = os.path.getsize(raw_csv) if raw_exists else 0

                zero_rows = row_count == 0
                status = "completed" if raw_exists else "failed"

                if not raw_exists:
                    error_msg = error_msg or "raw.csv missing"
                elif zero_rows:
                    # Successful scrape that legitimately returned zero results.
                    error_msg = None
                    note_msg = "zero rows"
                elif not error_msg:
                    error_msg = ""

        if status == "completed" and not raw_exists:
            status = "failed"
            if not error_msg:
                error_msg = "raw.csv missing"

        seed_jobs[job_id] = {
            "status": status,
            "directory": job_dir,
            "raw_csv": raw_csv,
            "row_count": int(row_count),
            "schema_hash": schema_hash or "",
        }

        status_payload = {
            "job_id": job_id,
            "status": status,
            "row_count": int(row_count),
            "raw_exists": bool(raw_exists),
            "raw_bytes": int(raw_bytes),
            "error": None if (status == "completed" and raw_exists and row_count == 0) else (error_msg or ""),
        }
        if note_msg:
            status_payload["note"] = note_msg

        _write_job_status(job_dir, status_payload)

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


def _coerce_config(config: Any) -> Dict[str, Any]:
    if isinstance(config, str):
        return _load_config(config)
    return dict(config or {})


def _build_job_states(seed_jobs: Dict[str, Any]) -> List[Dict[str, Any]]:
    job_states: List[Dict[str, Any]] = []
    for job_id, job_info in (seed_jobs or {}).items():
        state = dict(job_info or {})
        state.setdefault("job_id", job_id)
        job_states.append(state)
    return job_states


def _record_output(outputs: Dict[str, Any], name: str, path: Optional[str], df: Optional[pd.DataFrame] = None) -> int:
    row_count = _safe_count_rows(path) if path else 0
    entry: Dict[str, Any] = {"path": path or "", "row_count": int(row_count)}
    if df is not None:
        try:
            entry["schema_hash"] = _schema_hash_from_df(df)
        except Exception:
            pass
    outputs[name] = entry
    return int(row_count)


def run_enrich_phase(config: Any, run_dir: str, seed_result: Dict[str, Any], resume: bool = False) -> Dict[str, Any]:
    """
    Phase 2: merge raw outputs, run master enrichment, run validation, then quarantine.

    This mirrors the v1 master pipeline while persisting state to the v2 manifest.
    """
    cfg = _coerce_config(config)
    cfg_hash = config_hash(cfg)
    manifest_path = os.path.join(run_dir, "run_manifest_v2.json")

    existing_manifest: Dict[str, Any] = {}
    if os.path.exists(manifest_path):
        try:
            existing_manifest = load_manifest(manifest_path)
        except ValueError:
            existing_manifest = {}
    if not existing_manifest and seed_result:
        existing_manifest = seed_result

    manifest = _ensure_manifest_skeleton(existing_manifest or {}, run_dir, cfg_hash)
    phases = manifest.setdefault("phases", {})
    seed_phase = phases.get("seed", {})
    enrich_phase = phases.setdefault("enrich", {})
    outputs = enrich_phase.setdefault("outputs", {})

    os.makedirs(run_dir, exist_ok=True)

    master_raw_path = os.path.join(run_dir, "master_raw.csv")
    master_enriched_path = os.path.join(run_dir, "master_enriched.csv")
    master_pre_fb_path = os.path.join(run_dir, "master_pre_fb.csv")

    existing_pre_df: Optional[pd.DataFrame] = None
    existing_raw_count = _safe_count_rows(master_raw_path)
    existing_enriched_count = _safe_count_rows(master_enriched_path)
    existing_pre_count = _safe_count_rows(master_pre_fb_path)
    if existing_pre_count > 0:
        try:
            existing_pre_df = pd.read_csv(master_pre_fb_path)
        except Exception:
            existing_pre_df = None
    schema_valid = False
    if existing_pre_df is not None:
        try:
            schema_valid, _, _ = validate_schema(existing_pre_df, phase="pre_fb")
        except Exception:
            schema_valid = False

    required_outputs_exist = all(count > 0 for count in (existing_raw_count, existing_enriched_count, existing_pre_count))
    should_skip = resume and manifest.get("config_hash") == cfg_hash and required_outputs_exist and schema_valid

    if should_skip:
        _record_output(outputs, "master_raw", master_raw_path)
        _record_output(outputs, "master_enriched", master_enriched_path)
        _record_output(outputs, "master_pre_fb", master_pre_fb_path, df=existing_pre_df)
        enrich_phase["status"] = "skipped_cached"
        manifest["phases"]["enrich"] = enrich_phase
        write_manifest(manifest_path, manifest)
        return manifest

    job_states = _build_job_states(seed_phase.get("jobs", {}))

    log_path = os.path.join(run_dir, "master_log_v2.txt")
    logger = night_mode_runner._setup_logger(log_path, "master_v2")

    merge_fn = getattr(night_mode_runner, "_merge_raw_master", None)
    master_raw = merge_fn(run_dir, job_states, logger) if callable(merge_fn) else None
    raw_rows = _record_output(outputs, "master_raw", master_raw)
    if not master_raw or raw_rows == 0:
        enrich_phase["status"] = "failed"
        manifest["phases"]["enrich"] = enrich_phase
        write_manifest(manifest_path, manifest)
        return manifest

    master_enrich_cfg = cfg.get("master_enrichment", {}) or {}
    live_search_enabled = master_enrich_cfg.get("enable_live_search", True)
    max_live_searches_raw = master_enrich_cfg.get("max_live_searches")
    try:
        max_live_searches = int(max_live_searches_raw) if max_live_searches_raw is not None else None
    except Exception:
        max_live_searches = None
    if max_live_searches is not None and max_live_searches < 0:
        max_live_searches = 0

    master_enriched = pipeline_runner.run_master_enrichment(
        master_raw,
        master_enriched_path,
        logger=logger.info,
        enable_live_search=live_search_enabled,
        max_live_searches=max_live_searches,
        night_mode=True,
    )
    enriched_rows = _record_output(outputs, "master_enriched", master_enriched)
    if not master_enriched or enriched_rows == 0:
        enrich_phase["status"] = "failed"
        manifest["phases"]["enrich"] = enrich_phase
        write_manifest(manifest_path, manifest)
        return manifest

    master_pre_fb = pipeline_runner.run_enrichment(
        master_enriched,
        master_pre_fb_path,
        logger=logger.info,
        night_mode=True,
    )

    df_master: Optional[pd.DataFrame] = None
    try:
        df_master = pd.read_csv(master_pre_fb, dtype=str, keep_default_na=False).fillna("")
        df_master = night_mode_runner.quarantine_repeated_emails(df_master, min_repeats=5, logger=logger)
        df_master.to_csv(master_pre_fb, index=False)
    except Exception:
        df_master = None

    pre_rows = _record_output(outputs, "master_pre_fb", master_pre_fb, df=df_master)
    enrich_phase["status"] = "completed" if pre_rows > 0 else "failed"
    manifest["phases"]["enrich"] = enrich_phase
    write_manifest(manifest_path, manifest)
    return manifest


def run_contact_phase(config: Any, run_dir: str, enrich_manifest: Dict[str, Any], resume: bool = False, **kwargs) -> Dict[str, Any]:
    """
    Phase 3: Facebook global pass + final validation + exports.

    Mirrors the v1 ordering while persisting state to the v2 manifest.
    """

    cfg = _coerce_config(config)
    cfg_hash = config_hash(cfg)
    manifest_path = os.path.join(run_dir, "run_manifest_v2.json")

    existing_manifest: Dict[str, Any] = {}
    if os.path.exists(manifest_path):
        try:
            existing_manifest = load_manifest(manifest_path)
        except ValueError:
            existing_manifest = {}
    if not existing_manifest and enrich_manifest:
        existing_manifest = enrich_manifest

    manifest = _ensure_manifest_skeleton(existing_manifest or {}, run_dir, cfg_hash)
    phases = manifest.setdefault("phases", {})
    contact_phase = phases.setdefault("contact", {})
    outputs = contact_phase.setdefault("outputs", {})

    os.makedirs(run_dir, exist_ok=True)

    master_pre_fb_path = os.path.join(run_dir, "master_pre_fb.csv")
    master_post_fb_path = os.path.join(run_dir, "master_post_fb.csv")
    master_final_path = os.path.join(run_dir, "master_final.csv")
    master_export_path = os.path.join(run_dir, "master_export_leads.csv")
    final_export_path = os.path.join(run_dir, "final_export.csv")
    woodpecker_export_path = os.path.join(run_dir, "woodpecker_export.csv")
    fb_state_path = os.path.join(run_dir, "facebook_state.json")

    existing_post_df: Optional[pd.DataFrame] = None
    post_rows = _safe_count_rows(master_post_fb_path)
    final_rows = _safe_count_rows(master_final_path)
    export_rows = _safe_count_rows(master_export_path)
    if post_rows > 0:
        try:
            existing_post_df = pd.read_csv(master_post_fb_path, dtype=str, keep_default_na=False)
            schema_valid, _, _ = validate_schema(existing_post_df, phase="post_fb")
        except Exception:
            schema_valid = False
    else:
        schema_valid = False

    required_outputs_exist = post_rows > 0 and final_rows > 0 and export_rows > 0
    manifest_for_skip = {
        "config_hash": manifest.get("config_hash"),
        "phases": {"contact": {"status": contact_phase.get("status")}},
    }

    if resume and should_skip_phase(
        manifest_for_skip,
        phase="contact",
        current_config_hash=cfg_hash,
        required_outputs_exist=required_outputs_exist,
        schema_valid=schema_valid,
    ):
        _record_output(outputs, "master_post_fb", master_post_fb_path, df=existing_post_df)
        _record_output(outputs, "master_final", master_final_path)
        _record_output(outputs, "master_export_leads", master_export_path)
        _record_output(outputs, "final_export", final_export_path)
        _record_output(outputs, "woodpecker_export", woodpecker_export_path)
        contact_phase["status"] = "skipped_cached"
        manifest["phases"]["contact"] = contact_phase
        write_manifest(manifest_path, manifest)
        return manifest

    if not os.path.exists(master_pre_fb_path):
        contact_phase["status"] = "failed"
        manifest["phases"]["contact"] = contact_phase
        write_manifest(manifest_path, manifest)
        return manifest

    log_path = os.path.join(run_dir, "contact_log_v2.txt")
    logger = night_mode_runner._setup_logger(log_path, "contact_v2")

    try:
        pipeline_runner.run_facebook_global_pass_nightmode(
            master_pre_fb_path,
            master_post_fb_path,
            state_path=fb_state_path,
            logger=logger.info,
        )
    except Exception:
        # Fall back to the pre-FB file if the pass fails.
        try:
            pd.read_csv(master_pre_fb_path, dtype=str, keep_default_na=False).to_csv(master_post_fb_path, index=False)
        except Exception:
            contact_phase["status"] = "failed"
            manifest["phases"]["contact"] = contact_phase
            write_manifest(manifest_path, manifest)
            return manifest

    if not os.path.exists(master_post_fb_path):
        try:
            pd.read_csv(master_pre_fb_path, dtype=str, keep_default_na=False).to_csv(master_post_fb_path, index=False)
        except Exception:
            contact_phase["status"] = "failed"
            manifest["phases"]["contact"] = contact_phase
            write_manifest(manifest_path, manifest)
            return manifest

    try:
        fb_df = pd.read_csv(master_post_fb_path, dtype=str, keep_default_na=False).fillna("")
    except Exception:
        fb_df = None

    _record_output(outputs, "master_post_fb", master_post_fb_path, df=fb_df)

    try:
        pipeline_runner.run_enrichment(
            master_post_fb_path,
            master_final_path,
            logger=logger.info,
            night_mode=True,
        )
    except Exception:
        # Best-effort fallback
        try:
            pd.read_csv(master_post_fb_path, dtype=str, keep_default_na=False).to_csv(master_final_path, index=False)
        except Exception:
            contact_phase["status"] = "failed"
            manifest["phases"]["contact"] = contact_phase
            write_manifest(manifest_path, manifest)
            return manifest

    try:
        final_df = pd.read_csv(master_final_path, dtype=str, keep_default_na=False).fillna("")
    except Exception:
        final_df = None

    final_rows = _record_output(outputs, "master_final", master_final_path, df=final_df)

    try:
        pipeline_runner.export_master_leads(
            input_csv=master_final_path,
            output_csv=master_export_path,
            logger=logger,
            export_profile=(cfg.get("export_profile") or "full_dump"),
            final_export_csv=final_export_path,
            woodpecker_export_csv=woodpecker_export_path,
        )
    except Exception:
        contact_phase["status"] = "failed"
        manifest["phases"]["contact"] = contact_phase
        write_manifest(manifest_path, manifest)
        return manifest

    _record_output(outputs, "master_export_leads", master_export_path)
    _record_output(outputs, "final_export", final_export_path)
    _record_output(outputs, "woodpecker_export", woodpecker_export_path)

    contact_phase["status"] = "completed" if final_rows > 0 else "failed"
    manifest["phases"]["contact"] = contact_phase
    write_manifest(manifest_path, manifest)
    return manifest


def run_phased_night_mode(
    config_path: str,
    run_dir: Optional[str] = None,
    run_root: Optional[str] = None,
    resume: bool = False,
    stop_on_failure: bool = False,
    export_mode: Optional[str] = None,
    export_profile: Optional[str] = None,
    fb_auto_resume_override: Optional[bool] = None,
    fb_cooldown_override: Optional[int] = None,
    fb_max_attempts_override: Optional[int] = None,
    fb_max_rows_override: Optional[int] = None,
    with_sc_meta: bool = False,
    **kwargs,
) -> Dict[str, Any]:
    """
    Minimal CLI entrypoint for phased Night Mode runs.

    Mirrors the v1 signature so callers can forward CLI args verbatim.
    """
    # Preserve v1 semantics for run directory discovery/creation.
    root = run_root or run_dir or "overnight_runs"
    run_dir_path, _ = night_mode_runner._ensure_run_dir(resume=resume, run_root=root)

    cfg = _load_config(config_path)

    seed_manifest = run_seed_phase(config_path, run_dir_path, resume=resume)
    enrich_manifest = run_enrich_phase(cfg, run_dir_path, seed_manifest, resume=resume)
    contact_manifest = run_contact_phase(cfg, run_dir_path, enrich_manifest, resume=resume)
    return contact_manifest
