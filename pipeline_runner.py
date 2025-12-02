"""Thin wrapper layer to invoke existing Lead Machine scrapers and enrichment steps.

This module isolates Night Mode from the core scraper logic so that future
changes to scrapers do not require updating the orchestration layer.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import shutil
import tempfile
import time
import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

LoggerFn = Optional[Callable[[str], None]]

_LEGACY_MODULE = None
_LOGGER = logging.getLogger(__name__)


def _load_legacy_module():
    """
    Load the main Lead Machine module without triggering its __main__ entrypoint.
    The file name contains spaces, so importlib is used instead of a normal import.
    """
    global _LEGACY_MODULE
    if _LEGACY_MODULE is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        legacy_path = os.path.join(base_dir, "Lead Machine (Final Update 5).py")
        spec = importlib.util.spec_from_file_location("lead_machine_main", legacy_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load legacy module from {legacy_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[arg-type]
        _LEGACY_MODULE = module
    return _LEGACY_MODULE


def _safe_log(logger: LoggerFn, message: str) -> None:
    if not message:
        return
    if logger:
        try:
            logger(message)
            return
        except Exception:
            pass
    _LOGGER.info(message)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _read_seed_list(seed_path: str | None) -> List[str]:
    if not seed_path:
        return []
    path = os.path.abspath(seed_path)
    if not os.path.exists(path):
        return []
    try:
        df = pd.read_csv(path)
        if df.empty:
            return []
        first_col = df.columns[0]
        seeds = [str(v).strip() for v in df[first_col].tolist() if str(v).strip()]
        return seeds
    except Exception:
        return []


def _write_rows_to_csv(rows: Iterable[Any], path: str, source_directory: str = "") -> str:
    _ensure_parent(path)
    materialized: List[Any] = list(rows or [])
    if not materialized:
        pd.DataFrame().to_csv(path, index=False)
        return path
    if isinstance(materialized[0], dict):
        columns = []
        for row in materialized:
            columns.extend(list((row or {}).keys()))
        # Preserve deterministic order for stability.
        seen = set()
        ordered_columns = []
        for col in columns:
            if col not in seen:
                ordered_columns.append(col)
                seen.add(col)
        df = pd.DataFrame(materialized, columns=ordered_columns)
    else:
        df = pd.DataFrame(materialized)
    if source_directory and "Source Directory" not in df.columns:
        df["Source Directory"] = source_directory
    df.to_csv(path, index=False)
    return path


def run_directory_job(job_config: Dict[str, Any], raw_output_path: str, logger: LoggerFn = None) -> str:
    """
    Run a single directory scraper based on job_config.

    This wrapper intentionally keeps the surface area small and delegates
    behaviour to the existing scrapers without changing their defaults.
    """
    module = _load_legacy_module()
    directory = (job_config.get("directory") or "").strip().lower()
    target_count = int(job_config.get("target_valid_leads") or job_config.get("target_count") or 0)
    mode = (job_config.get("mode") or "").strip().lower()

    if directory == "spotify":
        params = {
            "playlist_ids": job_config.get("playlist_ids"),
            "search_term": job_config.get("search_term") or job_config.get("input_seed_csv") or "",
            "spotify_client_id": job_config.get("spotify_client_id") or os.environ.get("SPOTIFY_CLIENT_ID"),
            "spotify_client_secret": job_config.get("spotify_client_secret") or os.environ.get("SPOTIFY_CLIENT_SECRET"),
        }
        rows = module.scrape_spotify(target_count, params, logger=logger)
        return _write_rows_to_csv(rows, raw_output_path, source_directory="spotify")

    if directory == "bandcamp":
        seed = (
            job_config.get("bandcamp_seed")
            or job_config.get("input_seed_csv")
            or job_config.get("seed")
            or job_config.get("url")
            or ""
        )
        progress_path = os.path.join(os.path.dirname(os.path.abspath(raw_output_path)), "bandcamp_progress.json")
        module.scrape_bandcamp(
            seed,
            pages_per_tag=job_config.get("pages_per_tag", getattr(module, "BANDCAMP_PAGES_PER_TAG", 5)),
            existing_csv=raw_output_path,
            max_artists=target_count or getattr(module, "BANDCAMP_TARGET_ROWS", 200),
            progress_path=progress_path,
            mode=mode or "discover",
            max_pages=job_config.get("max_pages"),
            max_items=job_config.get("max_items"),
            search_domain=job_config.get("search_domain", "artists"),
            search_location_filter=job_config.get("search_location", ""),
        )
        return raw_output_path

    if directory == "soundcloud":
        url = job_config.get("soundcloud_url") or job_config.get("input_seed_csv") or job_config.get("seed") or ""
        module.scrape_soundcloud(
            url,
            seed_tags=job_config.get("seed_tags"),
            pages_per_tag=job_config.get("pages_per_tag", getattr(module, "SOUNDCLOUD_PAGES_PER_TAG", 5)),
            existing_csv=raw_output_path,
            max_artists=target_count or 200,
            max_handles=job_config.get("max_handles"),
            min_yield=job_config.get("min_yield", 3),
            dry_run=bool(job_config.get("dry_run", False)),
        )
        return raw_output_path

    if directory == "lastfm":
        seeds = _read_seed_list(job_config.get("input_seed_csv"))
        module.scrape_lastfm_similar(
            seeds,
            existing_csv=raw_output_path,
            max_artists=target_count or getattr(module, "LASTFM_MAX_SIMILAR_PER_SEED", 200),
            log_fn=logger,
        )
        return raw_output_path

    if directory == "unearthed":
        url = job_config.get("input_seed_csv") or job_config.get("seed") or job_config.get("url") or ""
        if not url:
            url = getattr(module, "UNEARTHED_DEFAULT_URL", "")
        module.scrape_website(
            url,
            existing_csv=raw_output_path,
            max_artists=target_count or 200,
        )
        return raw_output_path

    raise ValueError(f"Unsupported directory: {directory}")


def run_enrichment(raw_csv_path: str, enriched_output_path: str, logger: LoggerFn = None) -> str:
    """
    Invoke the existing enrichment/validation pipeline on a CSV.

    Currently this runs:
      - origin_validator.run_auto_validate (reusable validation stage)
      - final_checker.run_final_checker (adds duplicate/consistency flags)

    The final CSV is always written to enriched_output_path.
    """
    import origin_validator
    import final_checker

    _safe_log(logger, f"[Enrich] Starting enrichment for {raw_csv_path}")
    _ensure_parent(enriched_output_path)
    result_path = enriched_output_path
    try:
        result_path = origin_validator.run_auto_validate(
            raw_csv_path,
            output_path=enriched_output_path,
            validate_scope="uncertain_only",
            logger=logger,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        _safe_log(logger, f"[Enrich] Auto-validate failed safely: {exc}")
        shutil.copyfile(raw_csv_path, enriched_output_path)
        result_path = enriched_output_path

    final_path = result_path
    try:
        checked_path = final_checker.run_final_checker(result_path)
        if checked_path and os.path.exists(checked_path):
            shutil.copyfile(checked_path, enriched_output_path)
            final_path = enriched_output_path
        else:
            final_path = result_path
    except Exception as exc:  # pragma: no cover - defensive fallback
        _safe_log(logger, f"[Enrich] Final checker failed safely: {exc}")
        final_path = result_path

    _safe_log(logger, f"[Enrich] Completed enrichment -> {final_path}")
    return final_path


def _has_facebook_clue(row: pd.Series) -> bool:
    """Determine if a row has any Facebook signal to try."""
    try:
        for value in row:
            if not isinstance(value, str):
                continue
            lower = value.lower()
            if "facebook.com" in lower:
                return True
    except Exception:
        pass
    # Fallback: if we have an artist name, we can attempt a search as a clue.
    name = str(row.get("Artist Name", "") or "").strip()
    return bool(name)


def run_facebook_global_pass(
    input_csv: str,
    output_csv: str,
    skip_rows_with_email: bool = True,
    skip_rows_with_no_facebook_clue: bool = True,
) -> None:
    """
    Run a global Facebook enrichment pass on the merged CSV.

    - Skips rows that already have an Email (when requested).
    - Skips rows with no Facebook signal (when requested).
    - Uses the existing Facebook scraper logic (scrape_csv) from the legacy module.
    """
    if not input_csv or not os.path.exists(input_csv):
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    fb_username = os.environ.get("FB_USERNAME", "").strip()
    fb_password = os.environ.get("FB_PASSWORD", "").strip()

    df = pd.read_csv(input_csv)
    df["__row_id"] = range(len(df))

    def _eligible(row: pd.Series) -> bool:
        email_val = row.get("Email", "")
        if pd.isna(email_val):
            email_val = ""
        if skip_rows_with_email and str(email_val or "").strip():
            return False
        if skip_rows_with_no_facebook_clue and not _has_facebook_clue(row):
            return False
        return True

    eligible_df = df[df.apply(_eligible, axis=1)].copy()
    if eligible_df.empty:
        df.drop(columns=["__row_id"], inplace=True)
        df.to_csv(output_csv, index=False)
        return

    if not fb_username or not fb_password:
        # No credentials; pass through without modification.
        df.drop(columns=["__row_id"], inplace=True)
        df.to_csv(output_csv, index=False)
        return

    module = _load_legacy_module()
    if not hasattr(module, "scrape_csv"):
        df.drop(columns=["__row_id"], inplace=True)
        df.to_csv(output_csv, index=False)
        return

    temp_dir = tempfile.mkdtemp(prefix="fb_global_")
    temp_input = os.path.join(temp_dir, "fb_input.csv")
    temp_output = os.path.join(temp_dir, "fb_output.csv")
    eligible_df.to_csv(temp_input, index=False)

    try:
        module.scrape_csv(temp_input, temp_output, fb_username, fb_password, max_emails=None)
    except Exception:
        # Fail safely: emit original data.
        df.drop(columns=["__row_id"], inplace=True)
        df.to_csv(output_csv, index=False)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    updated_df = df.copy()
    if os.path.exists(temp_output):
        try:
            fb_df = pd.read_csv(temp_output)
            if "__row_id" in fb_df.columns:
                for _, row in fb_df.iterrows():
                    rid = row.get("__row_id")
                    email_val = str(row.get("Email", "") or "").strip()
                    if email_val and not pd.isna(rid):
                        try:
                            rid_int = int(float(rid))
                        except Exception:
                            continue
                        current_email = updated_df.at[rid_int, "Email"] if "Email" in updated_df.columns else ""
                        if pd.isna(current_email):
                            current_email = ""
                        if not str(current_email).strip():
                            updated_df.at[rid_int, "Email"] = email_val
        except Exception:
            pass

    updated_df.drop(columns=["__row_id"], inplace=True, errors="ignore")
    updated_df.to_csv(output_csv, index=False)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _safe_sleep(duration: float) -> None:
    try:
        if duration > 0:
            time.sleep(duration)
    except Exception:
        pass


def _load_fb_state(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_fb_state(path: str, payload: Dict[str, Any]) -> None:
    _ensure_parent(path)
    payload = dict(payload or {})
    payload["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    temp_fd, temp_path = tempfile.mkstemp(prefix="fb_state_", suffix=".json", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def _is_captcha_error(exc: BaseException) -> bool:
    """Minimal, non-invasive captcha heuristic."""
    try:
        name = exc.__class__.__name__.lower()
        message = str(exc).lower()
        if "captcha" in name or "captcha" in message:
            return True
        if getattr(exc, "is_captcha", False):
            return True
    except Exception:
        pass
    return False


def run_facebook_global_pass_nightmode(
    input_csv: str,
    output_csv: str,
    state_path: str,
    max_rows_per_run: int = 100,
    per_row_delay_range: tuple[float, float] = (2.0, 7.0),
    short_break_every: int = 20,
    short_break_range: tuple[float, float] = (25.0, 45.0),
    long_break_every: int = 80,
    long_break_range: tuple[float, float] = (120.0, 360.0),
    logger: LoggerFn = None,
) -> None:
    """
    Night Mode–specific global FB enrichment pass.

    Applies:
      - skip logic (rows with email or no FB clues)
      - randomized per-row delay
      - periodic short/long breaks
      - per-run max row limit
      - stateful resume from state_path
    """
    if not input_csv or not os.path.exists(input_csv):
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    fb_username = os.environ.get("FB_USERNAME", "").strip()
    fb_password = os.environ.get("FB_PASSWORD", "").strip()

    # Use existing output if present so resumes keep prior enrichments.
    base_path = output_csv if output_csv and os.path.exists(output_csv) else input_csv
    df = pd.read_csv(base_path)
    df["__row_id"] = range(len(df))

    total_rows = len(df.index)
    state = _load_fb_state(state_path)
    last_index = int(state.get("fb_last_index", -1) or -1)
    attempted_total = int(state.get("fb_attempted_total", 0) or 0)
    captcha_flag = bool(state.get("fb_captcha_flag", False))
    completed_rows = int(state.get("fb_completed", 0) or 0)

    if fb_username and fb_password:
        module = _load_legacy_module()
        if not hasattr(module, "scrape_csv"):
            _safe_log(logger, "[FB Night] scrape_csv missing on legacy module; skipping.")
            df.drop(columns=["__row_id"], inplace=True, errors="ignore")
            df.to_csv(output_csv, index=False)
            return
    else:
        _safe_log(logger, "[FB Night] Missing FB credentials; passing through without enrichment.")
        state.update(
            {
                "fb_last_index": total_rows - 1,
                "fb_completed": total_rows,
                "fb_attempted_total": attempted_total,
                "fb_captcha_flag": False,
                "fb_total_rows": total_rows,
                "fb_run_completed": True,
                "fb_limit_reached": False,
                "fb_resume_input": os.path.abspath(input_csv),
            }
        )
        _write_fb_state(state_path, state)
        df.drop(columns=["__row_id"], inplace=True, errors="ignore")
        df.to_csv(output_csv, index=False)
        return

    processed_this_run = 0
    limit_reached = False
    captcha_detected = False
    temp_dir = tempfile.mkdtemp(prefix="fb_nightmode_")
    temp_input = os.path.join(temp_dir, "fb_input.csv")
    temp_output = os.path.join(temp_dir, "fb_output.csv")

    try:
        for idx, row in df.iterrows():
            if idx <= last_index:
                continue
            completed_rows += 1
            last_index = idx

            email_val = row.get("Email", "")
            if pd.isna(email_val):
                email_val = ""
            has_email = bool(str(email_val or "").strip())
            has_clue = _has_facebook_clue(row)
            if has_email or not has_clue:
                state.update(
                    {
                        "fb_last_index": last_index,
                        "fb_completed": completed_rows,
                        "fb_attempted_total": attempted_total,
                        "fb_captcha_flag": captcha_flag,
                        "fb_total_rows": total_rows,
                        "fb_resume_input": os.path.abspath(input_csv),
                    }
                )
                _write_fb_state(state_path, state)
                continue

            if processed_this_run > 0:
                delay = random.uniform(*per_row_delay_range) if per_row_delay_range else 0.0
                _safe_log(logger, f"[FB Night] Sleeping {delay:.2f}s before next row (index={idx}).")
                _safe_sleep(delay)

            processed_this_run += 1
            attempted_total += 1

            if short_break_every > 0 and processed_this_run % short_break_every == 0:
                pause = random.uniform(*short_break_range) if short_break_range else 0.0
                _safe_log(logger, f"[FB Night] Short break for {pause:.2f}s after {processed_this_run} rows.")
                _safe_sleep(pause)
            if long_break_every > 0 and processed_this_run % long_break_every == 0:
                pause = random.uniform(*long_break_range) if long_break_range else 0.0
                _safe_log(logger, f"[FB Night] Long break for {pause:.2f}s after {processed_this_run} rows.")
                _safe_sleep(pause)

            single_df = pd.DataFrame([row])
            single_df.to_csv(temp_input, index=False)
            # Ensure fresh output target for this pass while retaining prior runs for dedupe.
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass
            try:
                module.scrape_csv(temp_input, temp_output, fb_username, fb_password, max_emails=None)
            except Exception as exc:
                if _is_captcha_error(exc):
                    captcha_flag = True
                    captcha_detected = True
                    _safe_log(logger, f"[FB Night] Captcha detected at row {idx}; stopping early.")
                    state.update(
                        {
                            "fb_last_index": last_index,
                            "fb_completed": completed_rows,
                            "fb_attempted_total": attempted_total,
                            "fb_captcha_flag": True,
                            "fb_total_rows": total_rows,
                            "fb_resume_input": os.path.abspath(input_csv),
                        }
                    )
                    _write_fb_state(state_path, state)
                    break
                _safe_log(logger, f"[FB Night] scrape_csv failed at row {idx}: {exc}")
                state.update(
                    {
                        "fb_last_index": last_index,
                        "fb_completed": completed_rows,
                        "fb_attempted_total": attempted_total,
                        "fb_captcha_flag": captcha_flag,
                        "fb_total_rows": total_rows,
                        "fb_resume_input": os.path.abspath(input_csv),
                        "fb_last_error": str(exc),
                    }
                )
                _write_fb_state(state_path, state)
                # Continue to next row safely.
                continue

            if os.path.exists(temp_output):
                try:
                    fb_df = pd.read_csv(temp_output)
                    if "__row_id" in fb_df.columns:
                        for _, fb_row in fb_df.iterrows():
                            rid = fb_row.get("__row_id")
                            if pd.isna(rid):
                                continue
                            try:
                                rid_int = int(float(rid))
                            except Exception:
                                continue
                            email_out = str(fb_row.get("Email", "") or "").strip()
                            if not email_out:
                                continue
                            current_email = df.at[rid_int, "Email"] if "Email" in df.columns else ""
                            if pd.isna(current_email):
                                current_email = ""
                            if not str(current_email).strip():
                                df.at[rid_int, "Email"] = email_out
                except Exception:
                    pass

            state.update(
                {
                    "fb_last_index": last_index,
                    "fb_completed": completed_rows,
                    "fb_attempted_total": attempted_total,
                    "fb_captcha_flag": captcha_flag,
                    "fb_total_rows": total_rows,
                    "fb_resume_input": os.path.abspath(input_csv),
                }
            )
            _write_fb_state(state_path, state)

            if max_rows_per_run and processed_this_run >= max_rows_per_run:
                limit_reached = True
                _safe_log(logger, f"[FB Night] Hit max_rows_per_run={max_rows_per_run}; stopping.")
                break
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    run_completed = (last_index >= total_rows - 1) and not captcha_detected
    state.update(
        {
            "fb_last_index": last_index,
            "fb_completed": completed_rows,
            "fb_attempted_total": attempted_total,
            "fb_captcha_flag": captcha_detected or captcha_flag,
            "fb_total_rows": total_rows,
            "fb_run_completed": run_completed,
            "fb_limit_reached": limit_reached,
            "fb_resume_input": os.path.abspath(input_csv),
        }
    )
    _write_fb_state(state_path, state)

    df.drop(columns=["__row_id"], inplace=True, errors="ignore")
    df.to_csv(output_csv, index=False)
