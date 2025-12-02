"""Thin wrapper layer to invoke existing Lead Machine scrapers and enrichment steps.

This module isolates Night Mode from the core scraper logic so that future
changes to scrapers do not require updating the orchestration layer.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import logging
import os
import random
import shutil
import tempfile
import time
import datetime
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from night_mode_fb import NightModeFacebookEnricher

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


def _safe_log_console(logger: LoggerFn, message: str) -> None:
    """
    Log via provided logger and also echo to stdout for real-time GUI visibility.
    """
    _safe_log(logger, message)
    try:
        print(message)
    except Exception:
        pass


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


def run_master_enrichment(seed_csv_path: str, output_csv_path: str, logger: LoggerFn = None) -> str:
    """
    Run the cross-directory enricher on a single combined CSV.

    This wraps the existing cross_directory_enricher logic used by the standalone tool.
    """
    _safe_log(logger, f"[Master Enrich] Starting cross-directory enrichment for {seed_csv_path}")
    try:
        import cross_directory_enricher
    except Exception as exc:
        _safe_log(logger, f"[Master Enrich] cross_directory_enricher unavailable: {exc}")
        shutil.copyfile(seed_csv_path, output_csv_path)
        return output_csv_path

    try:
        cross_directory_enricher.run_cross_directory_enrichment(
            seed_csv_path,
            output_csv_path,
            bandcamp_csv_path="",
            soundcloud_csv_path="",
            unearthed_csv_path="",
            lastfm_csv_path="",
            enable_live_search=True,
            max_live_searches=getattr(cross_directory_enricher, "LIVE_SEARCH_MAX_ATTEMPTS", 50),
            logger=logger,
        )
    except Exception as exc:
        _safe_log(logger, f"[Master Enrich] Enricher failed safely: {exc}")
        shutil.copyfile(seed_csv_path, output_csv_path)
        return output_csv_path

    _safe_log(logger, f"[Master Enrich] Completed cross-directory enrichment -> {output_csv_path}")
    return output_csv_path


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


def run_enrichment(raw_csv_path: str, enriched_output_path: str, logger: LoggerFn = None, night_mode: bool = False) -> str:
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
            night_mode=night_mode,
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
    name = row.get("Artist Name", "")
    try:
        if pd.isna(name):
            name = ""
    except Exception:
        pass
    name = str(name or "").strip()
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
    if "fb_status" not in df.columns:
        df["fb_status"] = "pending"
    else:
        df["fb_status"] = df["fb_status"].fillna("pending")
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
    attempted_ids = set(eligible_df["__row_id"].astype(int).tolist())

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
                # Update fb_status for attempted rows based on email presence.
                for rid_int in attempted_ids:
                    try:
                        email_val = updated_df.at[rid_int, "Email"] if "Email" in updated_df.columns else ""
                    except Exception:
                        email_val = ""
                    status = "email_found" if str(email_val or "").strip() else "no_email"
                    try:
                        existing_status = str(updated_df.at[rid_int, "fb_status"]) if "fb_status" in updated_df.columns else ""
                    except Exception:
                        existing_status = ""
                    if not existing_status or existing_status == "pending":
                        updated_df.at[rid_int, "fb_status"] = status
        except Exception:
            pass

    # Ensure rows with existing emails are marked as done.
    if "fb_status" in df.columns:
        for idx, row in df.iterrows():
            email_val = row.get("Email", "")
            if pd.isna(email_val):
                email_val = ""
            if str(email_val or "").strip():
                current = str(row.get("fb_status", "") or "")
                if not current or current == "pending":
                    df.at[idx, "fb_status"] = "email_found"

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


@dataclass
class FacebookGlobalPassStatus:
    processed_rows: int
    total_rows: int
    completed: bool
    hit_captcha: bool
    limit_reached: bool
    attempted_total: int


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
) -> FacebookGlobalPassStatus:
    """
    Night Mode–specific global FB enrichment pass.

    Applies:
      - skip logic (rows with email or no FB clues)
      - randomized per-row delay
      - periodic short/long breaks
      - per-run max row limit
      - stateful resume from state_path

    Returns FacebookGlobalPassStatus describing outcome for the current run.
    """
    if not input_csv or not os.path.exists(input_csv):
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    fb_username = os.environ.get("FB_USERNAME", "").strip()
    fb_password = os.environ.get("FB_PASSWORD", "").strip()

    # Use existing output if present so resumes keep prior enrichments.
    base_path = output_csv if output_csv and os.path.exists(output_csv) else input_csv
    df = pd.read_csv(base_path)
    if "fb_status" not in df.columns:
        df["fb_status"] = "pending"
    else:
        df["fb_status"] = df["fb_status"].fillna("pending")
    df["__row_id"] = range(len(df))

    total_rows = len(df.index)
    state = _load_fb_state(state_path)
    last_index = int(state.get("fb_last_index", -1) or -1)
    attempted_total = int(state.get("fb_attempted_total", 0) or 0)
    captcha_flag = bool(state.get("fb_captcha_flag", False))
    completed_rows = int(state.get("fb_completed", 0) or 0)

    try:
        os.environ["DISABLE_ORIGIN_AUTO_VALIDATE_PROMPT"] = "1"
    except Exception:
        pass

    if fb_username and fb_password:
        module = _load_legacy_module()
        if not hasattr(module, "scrape_csv"):
            _safe_log_console(logger, "[FB Night] scrape_csv missing on legacy module; skipping.")
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
            return FacebookGlobalPassStatus(
                processed_rows=completed_rows,
                total_rows=total_rows,
                completed=True,
                hit_captcha=False,
                limit_reached=False,
                attempted_total=attempted_total,
            )
    else:
        _safe_log_console(logger, "[FB Night] Missing FB credentials; passing through without enrichment.")
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
        return FacebookGlobalPassStatus(
            processed_rows=total_rows,
            total_rows=total_rows,
            completed=True,
            hit_captcha=False,
            limit_reached=False,
            attempted_total=attempted_total,
        )

    processed_this_run = 0
    limit_reached = False
    captcha_detected = False
    fb_helper = NightModeFacebookEnricher(
        module,
        fb_username,
        fb_password,
        logger=lambda msg: _safe_log_console(logger, msg),
    )

    with fb_helper:
        for idx, row in df.iterrows():
            if idx <= last_index:
                continue
            completed_rows += 1
            last_index = idx

            email_val = row.get("Email", "")
            if pd.isna(email_val):
                email_val = ""
            has_email = bool(str(email_val or "").strip())
            fb_status_val = str(row.get("fb_status", "") or "").strip().lower()
            facebook_url_hint = str(row.get("Facebook_URL", "") or "").strip()
            has_clue = _has_facebook_clue(row)
            should_run_night_fb = (not has_email) and ((fb_status_val in ("", "pending")) or (facebook_url_hint and not has_email))
            if has_email or not has_clue or not should_run_night_fb:
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
                _safe_log_console(logger, f"[FB Night] Sleeping {delay:.2f}s before next row (index={idx}).")
                _safe_sleep(delay)

            processed_this_run += 1
            attempted_total += 1

            if short_break_every > 0 and processed_this_run % short_break_every == 0:
                pause = random.uniform(*short_break_range) if short_break_range else 0.0
                _safe_log_console(logger, f"[FB Night] Short break for {pause:.2f}s after {processed_this_run} rows.")
                _safe_sleep(pause)
            if long_break_every > 0 and processed_this_run % long_break_every == 0:
                pause = random.uniform(*long_break_range) if long_break_range else 0.0
                _safe_log_console(logger, f"[FB Night] Long break for {pause:.2f}s after {processed_this_run} rows.")
                _safe_sleep(pause)

            try:
                clean_row = {k: ("" if pd.isna(v) else v) for k, v in row.to_dict().items()}
                enriched = fb_helper.enrich_row_with_facebook_night(clean_row, row_index=idx)
            except Exception as exc:  # pragma: no cover - defensive
                if _is_captcha_error(exc):
                    captcha_flag = True
                    captcha_detected = True
                    _safe_log_console(logger, f"[FB Night] Captcha detected at row {idx}; stopping early.")
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
                _safe_log_console(logger, f"[FB Night] Night FB enrich failed at row {idx}: {exc}")
                enriched = None

            if enriched:
                for col in ("Email", "Email_All", "Email_Type", "Facebook_URL"):
                    if col in enriched:
                        df.at[idx, col] = enriched.get(col, "")
                status_val = str(enriched.get("fb_status", "") or "")
                if not status_val:
                    email_now = enriched.get("Email", "") or ""
                    fb_url_now = enriched.get("Facebook_URL", "") or ""
                    status_val = "email_found" if str(email_now).strip() else ("no_email" if fb_url_now else "no_candidates")
                df.at[idx, "fb_status"] = status_val
            else:
                # Attempted but no enrichment result; mark as no_candidates to avoid repeated retries.
                df.at[idx, "fb_status"] = "no_candidates"

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
                _safe_log_console(logger, f"[FB Night] Hit max_rows_per_run={max_rows_per_run}; stopping.")
                break

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
    return FacebookGlobalPassStatus(
        processed_rows=completed_rows,
        total_rows=total_rows,
        completed=run_completed,
        hit_captcha=captcha_detected or captcha_flag,
        limit_reached=limit_reached,
        attempted_total=attempted_total,
    )


DEFAULT_EXPORT_COLUMNS: Sequence[str] = [
    "Artist Name",
    "Location",
    "Song Title",
    "Sounds Like",
    "Social Link",
    "SoundCloud Link",
    "Spotify_URL",
    "Spotify_Artist_ID",
    "Spotify_Website_URL",
    "External Links",
    "Facebook_URL",
    "Email",
    "Email_All",
    "Email_Type",
    "fb_status",
    "Played on triple J",
    "Played on Unearthed",
    "Release Date",
    "Primary Genre",
    "Date Added",
    "Spotify Playlist",
    "Source Directory",
    "Source URL",
    "Review_Urls",
    "final_status",
]


def export_master_leads(
    input_csv: str,
    output_csv: str,
    logger: Optional[logging.Logger] = None,
    export_columns: Optional[Sequence[str]] = None,
) -> None:
    export_logger = logger or logging.getLogger(__name__)
    if not input_csv or not os.path.exists(input_csv):
        export_logger.warning("[Master] Export skipped; input not found: %s", input_csv)
        return

    columns = list(export_columns) if export_columns is not None else list(DEFAULT_EXPORT_COLUMNS)
    export_logger.info("[Master] Exporting client-facing CSV: %s -> %s", input_csv, output_csv)
    _ensure_parent(output_csv)
    row_count = 0
    try:
        with open(input_csv, "r", encoding="utf-8", newline="") as infile, open(
            output_csv, "w", encoding="utf-8", newline=""
        ) as outfile:
            reader = csv.DictReader(infile)
            writer = csv.DictWriter(outfile, fieldnames=columns)
            writer.writeheader()
            for row in reader:
                if row is None:
                    continue
                writer.writerow({col: row.get(col, "") for col in columns})
                row_count += 1
    except FileNotFoundError:
        export_logger.warning("[Master] Export skipped; input not found during read: %s", input_csv)
        return
    except Exception as exc:  # pragma: no cover - defensive
        export_logger.error("[Master] Export failed safely: %s", exc)
        return

    export_logger.info("[Master] Export wrote %s rows to %s", row_count, output_csv)
