"""Night Mode orchestration layer for Lead Machine.

This module coordinates multiple directory scrapes + enrichment steps in a
single unattended run, writing per-job outputs and an optional merged master.

Usage:
    python night_mode_runner.py --config overnight_jobs.json
    python night_mode_runner.py --config overnight_jobs.json --resume
    python night_mode_runner.py --config overnight_jobs.json --export-mode both
    python night_mode_runner.py --config overnight_jobs.json --with-sc-meta
"""

from __future__ import annotations

import argparse
import datetime
import inspect
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from lead_vault.merge import preview_csv_merge_counts
from fb_attribution import apply_fb_opportunity_state_df, ensure_fb_attribution_columns
from night_mode_fb import close_night_fb_run_state, create_night_fb_run_state
import pipeline_runner
from pipeline_runner import (
    FacebookGlobalPassStatus,
    run_directory_job,
    run_enrichment,
    run_facebook_global_pass_nightmode,
    run_master_enrichment,
)
from source_scheduler import canonicalize_facebook_url, ensure_canonical_facebook_url, promote_facebook_url
from soundcloud_metadata_enricher import enrich_soundcloud_metadata


def _call_with_optional_night_fb_run_state(fn, *args, night_fb_run_state=None, **kwargs):
    target = getattr(fn, "side_effect", None) or fn
    try:
        signature = inspect.signature(target)
    except Exception:
        signature = None
    if signature is not None:
        params = signature.parameters
        if "night_fb_run_state" in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        ):
            kwargs["night_fb_run_state"] = night_fb_run_state
    return fn(*args, **kwargs)


def _call_with_optional_master_enrichment_kwargs(
    fn,
    *args,
    night_fb_run_state=None,
    night_runtime_reset_interval_rows=None,
    **kwargs,
):
    target = getattr(fn, "side_effect", None) or fn
    try:
        signature = inspect.signature(target)
    except Exception:
        signature = None
    if signature is not None:
        params = signature.parameters
        accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
        if "night_fb_run_state" in params or accepts_kwargs:
            kwargs["night_fb_run_state"] = night_fb_run_state
        if "night_runtime_reset_interval_rows" in params or accepts_kwargs:
            kwargs["night_runtime_reset_interval_rows"] = night_runtime_reset_interval_rows
    return fn(*args, **kwargs)

def _ensure_string_columns(df: pd.DataFrame, cols: List[str]) -> None:
    for col in cols:
        if col in df.columns:
            try:
                df[col] = df[col].astype("string")
            except Exception:
                df[col] = df[col].astype(object)


def _promote_fb_urls_df(df: pd.DataFrame, logger: Optional[Callable[[str], None]] = None) -> pd.DataFrame:
    """Promote Facebook links from generic fields into facebook_url/Facebook_URL."""
    if df is None or df.empty:
        return df
    if "facebook_url" not in df.columns:
        df["facebook_url"] = ""
    if "Facebook_URL" not in df.columns:
        df["Facebook_URL"] = ""
    if "Facebook URL" not in df.columns:
        df["Facebook URL"] = ""
    populated = 0
    canonical_from_alias = 0
    canonical_from_links = 0
    for idx in df.index:
        new_url, source = ensure_canonical_facebook_url(df.loc[idx], set_row=False)
        if not new_url:
            continue
        wrote = False
        current_canonical_raw = str(df.loc[idx, "Facebook_URL"] or "").strip()
        current_canonical = canonicalize_facebook_url(current_canonical_raw)
        if current_canonical and current_canonical_raw != current_canonical:
            df.loc[idx, "Facebook_URL"] = current_canonical
        elif not current_canonical:
            df.loc[idx, "Facebook_URL"] = new_url
            wrote = True
            if source in {"Social Link", "External Links", "Website", "Websites", "Website URL"}:
                canonical_from_links += 1
            elif source and source != "Facebook_URL":
                canonical_from_alias += 1
        if not canonicalize_facebook_url(df.loc[idx, "facebook_url"]):
            df.loc[idx, "facebook_url"] = new_url
            wrote = True
        if "Facebook URL" in df.columns and not canonicalize_facebook_url(df.loc[idx, "Facebook URL"]):
            df.loc[idx, "Facebook URL"] = new_url
            wrote = True
        if wrote:
            populated += 1
    if logger and populated:
        logger(f"[FB Promotion] facebook_url populated for {populated} rows")
    if logger and canonical_from_alias:
        logger(f"[FB Promotion] canonical Facebook_URL backfilled from alias fields for {canonical_from_alias} rows")
    if logger and canonical_from_links:
        logger(f"[FB Promotion] canonical Facebook_URL backfilled from Social Link / External Links for {canonical_from_links} rows")
    return df

DEFAULT_EXPORT_MODE = "both"
MAX_CONSECUTIVE_ERRORS = 10
CHECKPOINT_INTERVAL_ROWS = 5
STATE_FILENAME = "state.json"
LOG_FILENAME = "log.txt"
FACEBOOK_STATE_FILENAME = "facebook_state.json"
RUN_SUMMARY_FILENAME = "run_summary.json"
EMAIL_PRIORITY_COLS = getattr(pipeline_runner, "EMAIL_PRIORITY_COLS", ("Email", "Email_All", "Directory_Email", "Unearthed_Email"))
EXCLUDED_URL_SUBSTRINGS = (
    "soundcloud.com/triplejunearthed",
    "tiktok.com/@triplejradio",
    "youtube.com/abcaustralia",
)
SPOTIFY_FALLBACK_ALLOWED_SOURCES = ("unearthed", "bandcamp", "soundcloud")


@dataclass
class SmokeStats:
    jobs_attempted: int = 0
    jobs_merged: int = 0
    jobs_skipped: List[Dict[str, str]] = field(default_factory=list)
    raw_rows: int = 0
    enrichment_ran: bool = False
    enrichment_rows: int = 0
    sc_challenge_detected: int = 0
    sc_breaker_tripped: bool = False
    sc_live_disabled_reason: str = ""
    bandcamp_http_403_count: int = 0
    lastfm_http_406_count: int = 0
    emails_missing_source_url: int = 0
    emails_total: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "jobs_attempted": self.jobs_attempted,
            "jobs_merged": self.jobs_merged,
            "jobs_skipped": self.jobs_skipped,
            "raw_rows": self.raw_rows,
            "enrichment_ran": self.enrichment_ran,
            "enrichment_rows": self.enrichment_rows,
            "sc_challenge_detected": self.sc_challenge_detected,
            "sc_breaker_tripped": self.sc_breaker_tripped,
            "sc_live_disabled_reason": self.sc_live_disabled_reason,
            "bandcamp_http_403_count": self.bandcamp_http_403_count,
            "lastfm_http_406_count": self.lastfm_http_406_count,
            "emails_missing_source_url": self.emails_missing_source_url,
            "emails_total": self.emails_total,
        }


@dataclass(frozen=True)
class SpotifyZeroRowFallbackConfig:
    enabled: bool
    order: Tuple[str, ...]
    invalid_tokens: Tuple[str, ...] = ()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_spotify_seed_fallback_order(raw_value: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    valid: List[str] = []
    invalid: List[str] = []
    seen = set()
    for token in str(raw_value or "").split(","):
        normalized = token.strip().lower()
        if not normalized:
            continue
        if normalized not in SPOTIFY_FALLBACK_ALLOWED_SOURCES:
            invalid.append(normalized)
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        valid.append(normalized)
    return tuple(valid), tuple(invalid)


def _load_spotify_zero_row_fallback_config() -> SpotifyZeroRowFallbackConfig:
    enabled = _env_flag("SPOTIFY_ZERO_ROW_FALLBACK")
    order, invalid = _parse_spotify_seed_fallback_order(os.getenv("SPOTIFY_SEED_FALLBACK_ORDER", ""))
    return SpotifyZeroRowFallbackConfig(enabled=enabled, order=order, invalid_tokens=invalid)


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


def _update_stats_from_log(stats: Optional[SmokeStats], message: str) -> None:
    if not stats or not message:
        return
    lower = message.lower()
    if "bandcamp" in lower and "403" in lower:
        stats.bandcamp_http_403_count += 1
    if "last.fm" in lower and "406" in lower:
        stats.lastfm_http_406_count += 1
    if ("challenge" in lower and "[sc" in lower) or "rss-only mode" in lower:
        stats.sc_challenge_detected += 1
    if "circuit breaker tripped" in lower and "[sc" in lower:
        stats.sc_breaker_tripped = True
    if "live enrichment disabled" in lower and "reason=" in lower and not stats.sc_live_disabled_reason:
        match = re.search(r"reason=([^)\\s]+)", message)
        if match:
            stats.sc_live_disabled_reason = match.group(1)


def _wrap_logger_for_stats(logger_fn, stats: Optional[SmokeStats]):
    def _log(msg: str) -> None:
        try:
            if logger_fn:
                logger_fn(msg)
        finally:
            try:
                _update_stats_from_log(stats, msg)
            except Exception:
                pass
    return _log


def _emit_smoke_summary(stats: SmokeStats, logger: logging.Logger) -> None:
    degraded_reasons: List[str] = []
    if stats.jobs_skipped:
        degraded_reasons.append("job raw CSV read errors")
    if stats.raw_rows == 0:
        degraded_reasons.append("master_raw has zero rows")
    if stats.bandcamp_http_403_count:
        degraded_reasons.append("bandcamp 403s")
    if stats.lastfm_http_406_count:
        degraded_reasons.append("lastfm 406s")
    if stats.sc_breaker_tripped or stats.sc_challenge_detected:
        degraded_reasons.append("soundcloud challenges/breaker")
    if stats.sc_live_disabled_reason:
        degraded_reasons.append(f"sc_live_disabled={stats.sc_live_disabled_reason}")
    if stats.emails_missing_source_url:
        degraded_reasons.append("emails missing provenance")

    result_line = "SMOKE RESULT: PASS"
    if degraded_reasons:
        result_line += f" (DEGRADED: {', '.join(degraded_reasons)})"

    summary_lines = [
        "Smoke Summary:",
        f" - jobs attempted: {stats.jobs_attempted}",
        f" - jobs merged: {stats.jobs_merged}",
        f" - jobs skipped (read errors): {len(stats.jobs_skipped)}",
        f" - master_raw rows: {stats.raw_rows}",
        f" - enrichment ran: {'yes' if stats.enrichment_ran else 'no'}",
        f" - enrichment rows: {stats.enrichment_rows}",
        f" - SC challenges: {stats.sc_challenge_detected}",
        f" - SC breaker tripped: {stats.sc_breaker_tripped}",
        f" - SC live disabled reason: {stats.sc_live_disabled_reason or 'n/a'}",
        f" - Bandcamp HTTP 403 count: {stats.bandcamp_http_403_count}",
        f" - Last.fm HTTP 406 count: {stats.lastfm_http_406_count}",
        f" - Emails with missing provenance URL: {stats.emails_missing_source_url}",
        f" - Emails total (raw merged): {stats.emails_total}",
    ]

    logger.info(result_line)
    for line in summary_lines:
        logger.info(line)


def _normalise_seed_source_name(raw_directory: str) -> str:
    text = str(raw_directory or "").strip().lower()
    if not text:
        return ""
    if "unearthed" in text or "triple j" in text:
        return "unearthed"
    if "bandcamp" in text:
        return "bandcamp"
    if "soundcloud" in text:
        return "soundcloud"
    if "spotify" in text:
        return "spotify"
    if "lastfm" in text or "last.fm" in text:
        return "lastfm"
    return text


def _emit_seed_source_summary(
    jobs: List[Dict[str, Any]],
    processed_states: Dict[str, Dict[str, Any]],
    logger: logging.Logger,
) -> None:
    ordered_sources: List[str] = []
    counts: Dict[str, int] = {}

    for idx, job in enumerate(jobs):
        job_id = _job_id_for_index(job, idx)
        state = processed_states.get(job_id) or {}
        if str(state.get("status") or "").strip().lower() != "completed":
            continue

        source = _normalise_seed_source_name(job.get("directory") or "")
        if not source:
            continue

        if source not in counts:
            counts[source] = 0
            ordered_sources.append(source)

        try:
            row_count = int(state.get("row_count") or 0)
        except Exception:
            row_count = 0
        counts[source] += max(row_count, 0)

    if not ordered_sources:
        return

    logger.info("[Seed Summary]")
    for source in ordered_sources:
        logger.info("%s=%s", source, counts.get(source, 0))


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


def _count_data_rows(csv_path: str) -> int:
    if not csv_path or not os.path.exists(csv_path):
        return 0
    try:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        return len(df.index)
    except Exception:
        return 0


def _job_row_count(state: Dict[str, Any]) -> int:
    raw_count = state.get("row_count")
    if raw_count is not None:
        try:
            return max(int(raw_count), 0)
        except Exception:
            pass
    return _count_data_rows(str(state.get("raw_csv") or ""))


def _spotify_zero_row_reason(state: Dict[str, Any]) -> str:
    for key in ("error", "last_error", "reason"):
        value = str(state.get(key) or "").strip()
        if value:
            return f"Reason: {value}"
    status_value = str(state.get("status") or "").strip()
    error_count = state.get("error_count")
    if status_value and error_count not in (None, "", 0, "0"):
        return f"Reason: status={status_value} error_count={error_count}"
    return "Reason: playlist empty / inaccessible / extraction returned no artists."


def _job_id_for_index(job: Dict[str, Any], idx: int) -> str:
    return str(job.get("job_id") or job.get("id") or f"job_{idx + 1}")


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
    _ensure_string_columns(df, list(EMAIL_PRIORITY_COLS) + ["Email_All", "Email"])
    existing = [c for c in EMAIL_PRIORITY_COLS if c in df.columns]
    if not existing:
        return df
    # Ensure Email_All includes Email when Email_All is empty
    if "Email_All" in df.columns and "Email" in df.columns:
        email_all = df["Email_All"].fillna("").astype(str)
        email_col = df["Email"].fillna("").astype(str)
        mask_all = email_all.str.strip() == ""
        df.loc[mask_all, "Email_All"] = email_col[mask_all]

    # Cast to object before bfill to avoid pandas string-array backfill bug
    # that was smearing a single email across all rows.
    email_series = (
        df[existing]
        .astype(object)
        .bfill(axis=1)
        .iloc[:, 0]
        .fillna("")
        .astype(str)
    )
    df["Email"] = email_series.str.strip()
    return df


def _guard_against_email_smear(df: pd.DataFrame, logger: Optional[logging.Logger] = None, min_repeats: int = 5) -> pd.DataFrame:
    """Clear obviously smeared emails across unrelated jobs in raw merge output.

    - Flags any email value that appears across >= min_repeats rows.
    - If that email spans multiple __source_job values, pick an origin job based on
      highest frequency (tie-break: alphabetical) and clear the email fields on
      rows from other jobs.
    - Cleared rows keep suspects and are marked for manual review.
    - Emits concise log lines prefixed with [SmearGuard].
    """
    if df is None or df.empty:
        return df

    if "__source_job" not in df.columns:
        return df

    email_cols = [col for col in ("Email", "Email_All") if col in df.columns]
    if not email_cols:
        return df

    work = df.copy(deep=True)
    # Preserve any pre-merge email signals, if present, so origin detection
    # can ignore values that were accidentally smeared after the scrape.
    orig_email_col = "__email_orig"
    orig_email_all_col = "__email_all_orig"
    has_orig = orig_email_col in work.columns or orig_email_all_col in work.columns

    for col in ("Suspect_Email", "Suspect_Email_All", "Needs_Review", "Email Source"):
        if col not in work.columns:
            work[col] = ""

    _ensure_string_columns(
        work,
        email_cols
        + ["__source_job", "Suspect_Email", "Suspect_Email_All", "Needs_Review", "Email Source"],
    )

    total_counts: Dict[str, int] = {}
    email_job_counts: Dict[str, Dict[str, int]] = {}
    email_job_evidence: Dict[str, Dict[str, int]] = {}

    def _row_evidence_score(row: pd.Series, job_lower: str, primary_url: str) -> int:
        primary_url_lower = primary_url.lower()
        score = 0

        if "soundcloud" in job_lower:
            if "soundcloud.com" in primary_url_lower:
                score += 1
            if _cell_str(row.get("SoundCloud Link")):
                score += 1
            if "soundcloud.com" in _cell_str(row.get("Source URL")).lower():
                score += 1

        if "bandcamp" in job_lower:
            if primary_url_lower.endswith(".bandcamp.com") or "bandcamp.com" in primary_url_lower:
                score += 1

        if "facebook" in job_lower and ("facebook.com" in primary_url_lower):
            score += 1

        return score

    for _, row in work.iterrows():
        job = _cell_str(row.get("__source_job"))
        job_lower = job.lower()
        primary_url = _primary_url_from_row(row)
        evidence_score = _row_evidence_score(row, job_lower, primary_url)

        row_emails: set[str] = set()
        # Prefer original email fields when available to avoid counting
        # values that were introduced by an upstream smear.
        if has_orig:
            row_emails.update(
                pipeline_runner.normalize_emails(_cell_str(row.get(orig_email_col)))
            )
            row_emails.update(
                pipeline_runner.normalize_emails(_cell_str(row.get(orig_email_all_col)))
            )
        if not row_emails:
            for col in email_cols:
                row_emails.update(pipeline_runner.normalize_emails(_cell_str(row.get(col))))
        if not row_emails:
            continue
        for email in row_emails:
            total_counts[email] = total_counts.get(email, 0) + 1
            job_counts = email_job_counts.setdefault(email, {})
            job_counts[job] = job_counts.get(job, 0) + 1
            if evidence_score:
                evidence_counts = email_job_evidence.setdefault(email, {})
                evidence_counts[job] = evidence_counts.get(job, 0) + evidence_score

    suspect_emails = {email for email, count in total_counts.items() if count >= max(1, int(min_repeats))}
    if not suspect_emails:
        return work

    rows_cleared: Dict[str, int] = {email: 0 for email in suspect_emails}

    for email in sorted(suspect_emails):
        jobs = email_job_counts.get(email, {})
        evidence = email_job_evidence.get(email, {})
        if len(jobs) <= 1:
            continue
        evidence_values = [evidence.get(job, 0) for job in jobs]
        if any(val > 0 for val in evidence_values):
            origin_job = sorted(jobs.keys(), key=lambda job: (-evidence.get(job, 0), -jobs[job], job))[0]
        else:
            origin_job = sorted(jobs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        for idx, row in work.iterrows():
            job = _cell_str(row.get("__source_job"))
            row_emails: set[str] = set()
            if has_orig:
                row_emails.update(
                    pipeline_runner.normalize_emails(_cell_str(row.get(orig_email_col)))
                )
                row_emails.update(
                    pipeline_runner.normalize_emails(_cell_str(row.get(orig_email_all_col)))
                )
            if not row_emails:
                for col in email_cols:
                    row_emails.update(pipeline_runner.normalize_emails(_cell_str(row.get(col))))
            if email not in row_emails:
                continue
            if job == origin_job:
                continue

            suspect_email_val = _cell_str(row.get("Email")) if "Email" in work.columns else ""
            suspect_email_all_val = _cell_str(row.get("Email_All")) if "Email_All" in work.columns else ""

            if suspect_email_val:
                work.at[idx, "Suspect_Email"] = suspect_email_val or email
            elif not _cell_str(row.get("Suspect_Email")):
                work.at[idx, "Suspect_Email"] = email

            combined_suspect_all = pipeline_runner._append_suspect_email_all(
                _cell_str(row.get("Suspect_Email_All")), suspect_email_all_val or suspect_email_val or email
            )
            work.at[idx, "Suspect_Email_All"] = combined_suspect_all

            for col in email_cols:
                work.at[idx, col] = ""

            work.at[idx, "Needs_Review"] = "TRUE"
            work.at[idx, "Email Source"] = "Quarantined (smear guard)"
            rows_cleared[email] += 1

        jobs_summary = ", ".join(
            f"{job}:{count}/{evidence.get(job, 0)}" for job, count in sorted(jobs.items())
        )
        cleared = rows_cleared.get(email, 0)
        msg = (
            f"[SmearGuard] email={email} total={total_counts.get(email, 0)} "
            f"jobs=[{jobs_summary}] origin_job={origin_job} rows_cleared={cleared}"
        )
        try:
            if logger:
                logger.info(msg)
            else:
                logging.getLogger(__name__).info(msg)
        except Exception:
            pass

    return work


def _log_quarantine(message: str, logger: Optional[logging.Logger]) -> None:
    if not message:
        return
    try:
        if logger and hasattr(logger, "info"):
            logger.info(message)
            return
    except Exception:
        pass
    try:
        logging.getLogger(__name__).info(message)
    except Exception:
        pass


def _cell_str(v) -> str:
    if v is None:
        return ""
    try:
        is_na = pd.isna(v)
    except Exception:
        return str(v)
    try:
        if bool(is_na):
            return ""
    except Exception:
        return str(v)
    return str(v)


def quarantine_repeated_emails(df: pd.DataFrame, min_repeats: int = 5, logger: Optional[logging.Logger] = None) -> pd.DataFrame:
    """
    Quarantine highly repeated emails so Night FB can attempt recovery.

    - Identifies emails that appear across >= min_repeats rows (Email or Email_All).
    - Preserves the repeated values in Suspect_Email / Suspect_Email_All.
    - Clears Email / Email_All only on rows that are unlikely sources so FB can run.
    - Heuristic for keeping source rows:
        * row __source_job matches the job where the email first appeared, OR
        * the email originated on a SoundCloud row and the current row is SoundCloud.
    - Marks quarantined rows with Needs_Review="TRUE" and Email Source="Quarantined (repeat email)".
    - Emits a summary log per repeated email.
    """
    if df is None or df.empty:
        return df

    work = df.copy(deep=True)
    _ensure_string_columns(
        work,
        [
            "Email",
            "Email_All",
            "Suspect_Email",
            "Suspect_Email_All",
            "Needs_Review",
            "Email Source",
            "__source_job",
            "Source Directory",
            "Directory_Email",
            "Unearthed_Email",
        ],
    )

    email_counts: Dict[str, int] = {}
    first_seen: Dict[str, Tuple[int, str, str]] = {}
    email_has_soundcloud_origin: Dict[str, bool] = {}

    for idx, row in work.iterrows():
        row_job = _cell_str(row.get("__source_job"))
        row_job_lower = row_job.lower()
        row_dir = _cell_str(row.get("Source Directory"))
        row_dir_lower = row_dir.lower()
        emails = set(
            pipeline_runner.normalize_emails(_cell_str(row.get("Email_All")))
            + pipeline_runner.normalize_emails(_cell_str(row.get("Email")))
        )
        if not emails:
            continue
        for email in emails:
            email_counts[email] = email_counts.get(email, 0) + 1
            if email not in first_seen:
                first_seen[email] = (
                    idx,
                    row_job,
                    row_dir,
                )
            has_sc_signal = ("soundcloud" in row_dir_lower) or ("soundcloud" in row_job_lower)
            if has_sc_signal:
                email_has_soundcloud_origin[email] = True
            elif email not in email_has_soundcloud_origin:
                email_has_soundcloud_origin[email] = False

    repeated = {email: count for email, count in email_counts.items() if count >= max(1, int(min_repeats))}
    if not repeated:
        return work

    cleared_counter: Dict[str, int] = {email: 0 for email in repeated}
    kept_counter: Dict[str, int] = {email: 0 for email in repeated}

    for idx, row in work.iterrows():
        row_emails = set(
            pipeline_runner.normalize_emails(_cell_str(row.get("Email_All")))
            + pipeline_runner.normalize_emails(_cell_str(row.get("Email")))
        )
        repeated_here = [email for email in row_emails if email in repeated]
        if not repeated_here:
            continue

        row_job = _cell_str(row.get("__source_job"))
        row_job_lower = row_job.lower()
        row_dir_lower = _cell_str(row.get("Source Directory")).lower()

        # Decide keep/clear per email once, then aggregate.
        decisions = []
        for email in repeated_here:
            origin_idx, origin_job, _ = first_seen[email]
            has_sc_origin = email_has_soundcloud_origin.get(email, False)
            if has_sc_origin:
                keep_email = ("soundcloud" in row_dir_lower) or ("soundcloud" in row_job_lower)
            else:
                keep_email = idx == origin_idx
            decisions.append((email, keep_email))

        keep_any = any(keep for _, keep in decisions)

        suspect_email_val = _cell_str(row.get("Email"))
        suspect_email_all_val = _cell_str(row.get("Email_All"))
        suspect_directory_email_val = _cell_str(row.get("Directory_Email"))
        suspect_unearthed_email_val = _cell_str(row.get("Unearthed_Email"))
        if suspect_email_val != "":
            work.at[idx, "Suspect_Email"] = suspect_email_val
        combined_suspect_all = ";".join(
            [
                val
                for val in (
                    suspect_email_all_val,
                    suspect_email_val,
                    suspect_directory_email_val,
                    suspect_unearthed_email_val,
                )
                if val
            ]
        )
        if combined_suspect_all:
            existing_suspect_all = _cell_str(row.get("Suspect_Email_All"))
            merged_suspect_all = pipeline_runner._append_suspect_email_all(existing_suspect_all, combined_suspect_all)
            work.at[idx, "Suspect_Email_All"] = merged_suspect_all

        if not keep_any:
            email_cols_to_clear = [
                "Email",
                "Email_All",
                "Directory_Email",
                "Unearthed_Email",
                "Email Address",
                "Primary Email",
                "All Emails",
                "emails",
                "email",
                "Emails",
            ]
            for col in email_cols_to_clear:
                if col in work.columns:
                    work.at[idx, col] = ""
            work.at[idx, "Needs_Review"] = "TRUE"
            work.at[idx, "Email Source"] = "Quarantined (repeat email)"
            artist = _cell_str(row.get("Artist Name"))
            _log_quarantine(
                f"[Quarantine][Row] cleared_for_fb row={idx} artist='{artist}' "
                f"old_email='{suspect_email_val}' old_email_all='{suspect_email_all_val}' "
                f"suspect_email_all_after='{work.at[idx, 'Suspect_Email_All']}'",
                logger,
            )

        for email, keep_email in decisions:
            if keep_email:
                kept_counter[email] += 1
            else:
                cleared_counter[email] += 1

    for email, count in repeated.items():
        rows_cleared = cleared_counter.get(email, 0)
        rows_kept = kept_counter.get(email, 0)
        _log_quarantine(
            f"[Quarantine] email={email} count={count} rows_cleared={rows_cleared} rows_kept={rows_kept}",
            logger,
        )

    return work


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


def _domain_org_sidecar_path(csv_path: str) -> str:
    base, ext = os.path.splitext(csv_path)
    return f"{base}_domain_org_index{ext or '.csv'}"


def _final_master_candidates(
    run_dir: str,
    master_final: Optional[str] = None,
    master_post_fb: Optional[str] = None,
    master_pre_fb: Optional[str] = None,
    master_enriched: Optional[str] = None,
) -> List[str]:
    candidates: List[str] = []
    seen = set()
    for path in (
        master_final,
        master_post_fb,
        master_pre_fb,
        master_enriched,
        os.path.join(run_dir, "master_enriched_deduped.csv"),
        os.path.join(run_dir, "master_post_fb.csv"),
        os.path.join(run_dir, "master_pre_fb.csv"),
        os.path.join(run_dir, "master_enriched.csv"),
    ):
        normalized = str(path or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def _select_existing_artifact(paths: List[str]) -> str:
    for path in paths:
        if path and os.path.exists(path):
            return path
    return ""


def _discover_domain_org_sidecar(
    run_dir: str,
    artifact_candidates: List[str],
) -> str:
    for artifact_path in artifact_candidates:
        sidecar_path = _domain_org_sidecar_path(artifact_path)
        if os.path.exists(sidecar_path):
            return sidecar_path
    try:
        sidecar_names = sorted(
            name for name in os.listdir(run_dir) if name.endswith("_domain_org_index.csv")
        )
    except Exception:
        sidecar_names = []
    if not sidecar_names:
        return ""
    return os.path.join(run_dir, sidecar_names[-1])


def _count_nonempty_email_rows(csv_path: str) -> int:
    if not csv_path or not os.path.exists(csv_path):
        return 0
    try:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False).fillna("")
    except Exception:
        return 0
    if df.empty:
        return 0
    email_col = df["Email"] if "Email" in df.columns else ""
    email_all_col = df["Email_All"] if "Email_All" in df.columns else ""
    if isinstance(email_col, str):
        email_col = pd.Series([""] * len(df.index))
    if isinstance(email_all_col, str):
        email_all_col = pd.Series([""] * len(df.index))
    has_email = email_col.astype(str).str.strip().ne("") | email_all_col.astype(str).str.strip().ne("")
    return int(has_email.sum())


def _preview_lead_vault_counts(csv_path: str) -> Tuple[int, int]:
    if not csv_path or not os.path.exists(csv_path):
        return 0, 0
    try:
        preview = preview_csv_merge_counts(csv_path)
    except Exception:
        return 0, 0
    try:
        rows_added = max(int(preview.get("rows_added") or 0), 0)
    except Exception:
        rows_added = 0
    try:
        rows_updated = max(int(preview.get("rows_updated") or 0), 0)
    except Exception:
        rows_updated = 0
    return rows_added, rows_updated


def _default_run_summary(run_dir: str) -> Dict[str, Any]:
    return {
        "run_timestamp": os.path.basename(os.path.abspath(run_dir)),
        "seeds_processed": 0,
        "artists_processed": 0,
        "domains_discovered": 0,
        "emails_discovered": 0,
        "orgs_created": 0,
        "vault_rows_added": 0,
        "vault_rows_updated": 0,
    }


def _build_run_summary(
    run_dir: str,
    master_raw: Optional[str] = None,
    master_enriched: Optional[str] = None,
    master_pre_fb: Optional[str] = None,
    master_post_fb: Optional[str] = None,
    master_final: Optional[str] = None,
) -> Dict[str, Any]:
    summary = _default_run_summary(run_dir)
    artifact_candidates = _final_master_candidates(
        run_dir,
        master_final=master_final,
        master_post_fb=master_post_fb,
        master_pre_fb=master_pre_fb,
        master_enriched=master_enriched,
    )
    final_artifact = _select_existing_artifact(artifact_candidates)
    domain_sidecar = _discover_domain_org_sidecar(run_dir, artifact_candidates)

    try:
        summary["seeds_processed"] = _count_data_rows(str(master_raw or os.path.join(run_dir, "master_raw.csv")))
    except Exception:
        pass
    try:
        summary["artists_processed"] = _count_data_rows(final_artifact)
    except Exception:
        pass
    try:
        summary["domains_discovered"] = _count_data_rows(domain_sidecar)
    except Exception:
        pass
    try:
        summary["emails_discovered"] = _count_nonempty_email_rows(final_artifact)
    except Exception:
        pass
    try:
        summary["orgs_created"] = _count_data_rows(domain_sidecar)
    except Exception:
        pass
    rows_added, rows_updated = _preview_lead_vault_counts(final_artifact)
    summary["vault_rows_added"] = rows_added
    summary["vault_rows_updated"] = rows_updated
    return summary


def _write_run_summary(run_dir: str, summary: Dict[str, Any]) -> str:
    path = os.path.join(run_dir, RUN_SUMMARY_FILENAME)
    os.makedirs(run_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    return path


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
    combined = _promote_fb_urls_df(combined, logger=logger.info if logger else None)
    # Integrity guard: Email must have provenance
    if "Needs_Review" not in combined.columns:
        combined["Needs_Review"] = ""
    if "Email_Source_URL" not in combined.columns:
        combined["Email_Source_URL"] = ""

    email_series = combined.get("Email", "").fillna("").astype(str).str.strip()
    prov_series = combined.get("Email_Source_URL", "").fillna("").astype(str).str.strip()

    mask = (email_series != "") & (prov_series == "")
    combined.loc[mask, "Needs_Review"] = "TRUE"
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


def _merge_raw_master(
    run_dir: str, job_states: List[Dict[str, Any]], logger: logging.Logger, stats: Optional[SmokeStats] = None
) -> Optional[str]:
    def _log_skip(job_id: str, path: str, raw_bytes: int, reason: str, exc: Optional[Exception] = None) -> None:
        msg = f"[Master] Skipping {path} (job={job_id}) bytes={raw_bytes} reason={reason}"
        if exc:
            logger.warning("%s: %s", msg, exc)
        else:
            logger.warning("%s", msg)
        if stats is not None:
            stats.jobs_skipped.append({"path": path, "reason": reason, "bytes": raw_bytes, "job_id": job_id})

    def _load_job_csv(job_id: str, path: str) -> Optional[pd.DataFrame]:
        if not path or not os.path.exists(path):
            _log_skip(job_id, path, 0, "missing file")
            return None
        try:
            raw_bytes = os.path.getsize(path)
        except Exception:
            raw_bytes = 0
        if raw_bytes <= 1:
            _log_skip(job_id, path, raw_bytes, "empty/too small")
            return None
        try:
            df_local = pd.read_csv(path, dtype=str, keep_default_na=False)
            return df_local
        except pd.errors.EmptyDataError as exc:
            _log_skip(job_id, path, raw_bytes, "pandas EmptyDataError", exc)
            return None
        except pd.errors.ParserError as exc:
            _log_skip(job_id, path, raw_bytes, "pandas ParserError", exc)
            return None
        except Exception as exc:
            _log_skip(job_id, path, raw_bytes, f"{exc.__class__.__name__}", exc)
            return None

    frames = []
    non_empty_jobs_merged = 0
    for state in job_states:
        job_id = state.get("job_id", "")
        path = state.get("raw_csv") or ""
        df = _load_job_csv(job_id, path)
        if df is None:
            continue
        if not df.empty:
            non_empty_jobs_merged += 1
        df["__source_job"] = job_id
        # Keep a copy of the per-job email fields before any merge/consolidation
        # so SmearGuard can rely on the originals if a later step smears values.
        if "Email" in df.columns:
            df["__email_orig"] = df["Email"]
        if "Email_All" in df.columns:
            df["__email_all_orig"] = df["Email_All"]
        frames.append(df)
    if not frames:
        logger.warning("[Master] No data available after reading raw files.")
        return None
    if stats is not None:
        stats.jobs_merged = non_empty_jobs_merged
    combined = pd.concat(frames, ignore_index=True, sort=False)
    for col in ("Source URL", "SoundCloud Link", "Social Link", "External Links", "Facebook_URL"):
        if col in combined.columns:
            combined[col] = combined[col].fillna("").astype(str).apply(_strip_excluded_urls)
    combined = _coalesce_emails(combined)
    combined = _promote_fb_urls_df(combined, logger=logger.info if logger else None)
    # Integrity guard: Email must have provenance
    if "Needs_Review" not in combined.columns:
        combined["Needs_Review"] = ""
    if "Email_Source_URL" not in combined.columns:
        combined["Email_Source_URL"] = ""

    email_series = combined.get("Email", "").fillna("").astype(str).str.strip()
    prov_series = combined.get("Email_Source_URL", "").fillna("").astype(str).str.strip()

    mask = (email_series != "") & (prov_series == "")
    combined.loc[mask, "Needs_Review"] = "TRUE"
    combined = _guard_against_email_smear(combined, logger=logger, min_repeats=5)
    # Email provenance completeness metrics
    if stats is not None:
        email_col = combined.get("Email", pd.Series(dtype=str)).fillna("").astype(str)
        provenance_col = combined.get("Email_Source_URL", pd.Series(dtype=str)).fillna("").astype(str)
        stats.emails_total = int((email_col.str.strip() != "").sum())
        missing_mask = (email_col.str.strip() != "") & (provenance_col.str.strip() == "")
        stats.emails_missing_source_url = int(missing_mask.sum())
    # Drop helper columns before writing the master file.
    for helper_col in ("__email_orig", "__email_all_orig"):
        if helper_col in combined.columns:
            combined = combined.drop(columns=[helper_col])
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
    if stats is not None:
        stats.raw_rows = len(combined.index)
    return master_path


def _load_state(state_path: str) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        return {}
    try:
        return _load_json(state_path)
    except Exception:
        return {}


def _mark_job_skipped_for_spotify_fallback(
    job: Dict[str, Any],
    run_dir: str,
    per_job_validate: bool,
) -> Dict[str, Any]:
    job_id = job.get("job_id") or job.get("id") or "job_unknown"
    directory = (job.get("directory") or "").strip().lower()
    job_dir = os.path.join(run_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    log_path = os.path.join(job_dir, LOG_FILENAME)
    state_path = os.path.join(job_dir, STATE_FILENAME)
    logger = _setup_logger(log_path, job_id)
    raw_csv = os.path.join(job_dir, "raw.csv")
    enriched_csv = os.path.join(job_dir, "enriched.csv")
    # Keep the normal per-job artifact contract intact for resume/merge stages.
    # This is a completed zero-row placeholder only; downstream summaries should
    # treat it as row_count=0 rather than as a data-contributing merged job.
    pipeline_runner._write_rows_to_csv([], raw_csv, source_directory=directory)
    if per_job_validate:
        shutil.copyfile(raw_csv, enriched_csv)
    else:
        enriched_csv = raw_csv

    state = _load_state(state_path)
    state.update(
        {
            "job_id": job_id,
            "raw_csv": raw_csv,
            "enriched_csv": enriched_csv,
            "input_seed_csv": job.get("input_seed_csv", ""),
            "status": "completed",
            "error_count": state.get("error_count", 0),
            "current_row_index": 0,
            "valid_leads_so_far": 0,
            "row_count": 0,
            "skipped_reason": "spotify_zero_row_fallback_short_circuit",
        }
    )
    _write_json(state_path, state)
    logger.info("[Seed Fallback] Skipping job %s after earlier fallback success.", job_id)
    return state


def _process_job(
    job: Dict[str, Any],
    run_dir: str,
    resume: bool,
    stop_on_failure: bool,
    per_job_validate: bool = True,
    with_sc_meta: bool = False,
) -> Dict[str, Any]:
    job_id = job.get("job_id") or f"job_{len(job)}"
    directory = (job.get("directory") or "").strip().lower()
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
        runtime_job = job
        if directory == "unearthed":
            runtime_job = dict(job)

            def _persist_runtime_state() -> None:
                _write_json(state_path, state)

            runtime_job["_night_mode_state"] = state
            runtime_job["_night_mode_state_persist"] = _persist_runtime_state

        if resume and os.path.exists(state["raw_csv"]):
            logger.info("Resume: raw CSV already exists at %s; skipping scrape step.", state["raw_csv"])
            pipeline_runner.ensure_final_raw_csv(state["raw_csv"], job_id, logger=logger.info)
        else:
            logger.info("Starting scrape for job %s", job_id)
            run_directory_job(runtime_job, state["raw_csv"], logger=logger.info)
            pipeline_runner.ensure_final_raw_csv(state["raw_csv"], job_id, logger=logger.info)
        raw_row_count = _count_data_rows(state["raw_csv"])
        state["row_count"] = raw_row_count
        state["current_row_index"] = max(_count_rows(state["raw_csv"]) - 1, 0)
        state["valid_leads_so_far"] = state.get("valid_leads_so_far", 0) + state["current_row_index"]
        _write_json(state_path, state)

        if with_sc_meta and directory == "soundcloud":
            sc_raw_path = state.get("raw_csv")
            if sc_raw_path and os.path.isfile(sc_raw_path):
                logger.info("[SoundCloud] Running metadata enricher for %s", sc_raw_path)
                try:
                    enriched_path = enrich_soundcloud_metadata(
                        input_csv=sc_raw_path,
                        output_csv=None,
                        max_rows=None,
                        skip_existing=True,
                        sleep_seconds=1.5,
                    )
                    state["raw_csv"] = enriched_path
                    state["row_count"] = _count_data_rows(enriched_path)
                    state["current_row_index"] = max(_count_rows(enriched_path) - 1, 0)
                    state["valid_leads_so_far"] = state["current_row_index"]
                    _write_json(state_path, state)
                    logger.info("[SoundCloud] Metadata enrichment complete: %s", enriched_path)
                except Exception as exc:
                    logger.warning("[SoundCloud] Metadata enrichment failed safely for %s: %s", sc_raw_path, exc)
            else:
                logger.info("[SoundCloud] Metadata enrichment skipped; CSV not found at %s", sc_raw_path)

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
            state["row_count"] = _count_data_rows(state["raw_csv"])
            state["valid_leads_so_far"] = _count_rows(final_enriched)
            completion_log_message = "Completed job %s"
        else:
            # Skip per-job validation; leave enrichment for master stage.
            state["enriched_csv"] = state["raw_csv"]
            state["row_count"] = _count_data_rows(state["raw_csv"])
            completion_log_message = "Completed job %s (raw only; master enrichment pending)"
        state["status"] = "completed"
        if directory == "unearthed":
            state["unearthed_last_profile_url"] = None
        _write_json(state_path, state)
        logger.info(completion_log_message, job_id)
        return state
    except Exception as exc:
        state["error_count"] = state.get("error_count", 0) + 1
        state["status"] = "failed" if state["error_count"] >= MAX_CONSECUTIVE_ERRORS else "partial_error"
        state["last_error"] = f"{exc.__class__.__name__}: {exc}"
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
    with_sc_meta: bool = False,
) -> Dict[str, Any]:
    stats = SmokeStats()
    config = _load_json(config_path)
    stats.jobs_attempted = len(config.get("jobs", []))
    export_mode = (export_mode_override or config.get("export_mode") or DEFAULT_EXPORT_MODE).strip().lower()
    if export_mode not in {"per_directory", "combined", "both"}:
        export_mode = DEFAULT_EXPORT_MODE
    export_profile = (export_profile_override or config.get("export_profile") or "full_dump").strip().lower()
    if export_profile not in {"studio_safe", "studio_plus", "unearthed_social", "full_dump"}:
        export_profile = "full_dump"
    master_enrich_cfg = config.get("master_enrichment", {}) or {}
    master_enrichment_enabled = master_enrich_cfg.get("enabled", True)
    master_live_search_enabled = master_enrich_cfg.get("enable_live_search", True)
    master_max_live_searches_raw = master_enrich_cfg.get("max_live_searches")
    master_night_runtime_reset_interval_rows = master_enrich_cfg.get(
        "night_runtime_reset_interval_rows",
        config.get("night_runtime_reset_interval_rows"),
    )
    try:
        master_max_live_searches = int(master_max_live_searches_raw) if master_max_live_searches_raw is not None else None
    except Exception:
        master_max_live_searches = None
    if master_max_live_searches is not None and master_max_live_searches < 0:
        master_max_live_searches = 0

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
    # Night Mode SoundCloud budgets (Night Mode only; legacy unaffected).
    sc_cfg = config.get("soundcloud", {}) or {}
    try:
        sc_budget_seconds = float(sc_cfg.get("night_sc_budget_seconds", 6))
    except Exception:
        sc_budget_seconds = 6.0
    try:
        sc_max_fetches = int(sc_cfg.get("night_sc_max_fetches", 3))
    except Exception:
        sc_max_fetches = 3
    if sc_budget_seconds < 0:
        sc_budget_seconds = 0.0
    if sc_max_fetches < 0:
        sc_max_fetches = 0
    os.environ["NIGHT_SC_BUDGET_SECONDS"] = str(sc_budget_seconds)
    os.environ["NIGHT_SC_MAX_FETCHES"] = str(sc_max_fetches)
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
    master_logger = _setup_logger(os.path.join(run_dir, "master_log.txt"), "master")
    spotify_fallback_cfg = _load_spotify_zero_row_fallback_config()
    if spotify_fallback_cfg.enabled and spotify_fallback_cfg.invalid_tokens:
        master_logger.warning(
            "[Seed Fallback] ignoring invalid fallback sources: %s",
            ", ".join(spotify_fallback_cfg.invalid_tokens),
        )

    jobs = config.get("jobs", []) or []
    processed_states: Dict[str, Dict[str, Any]] = {}
    pending_skip_job_ids: Dict[str, str] = {}
    job_states: List[Dict[str, Any]] = []

    def _process_job_now(job: Dict[str, Any], *, allow_stop_on_failure: bool) -> Dict[str, Any]:
        return _process_job(
            job,
            run_dir,
            resume=resume,
            stop_on_failure=allow_stop_on_failure,
            per_job_validate=not master_enrichment_enabled,
            with_sc_meta=with_sc_meta,
        )

    def _find_pending_job_for_source(source: str, start_index: int) -> Optional[Tuple[int, Dict[str, Any]]]:
        for candidate_idx in range(start_index, len(jobs)):
            candidate_job = jobs[candidate_idx]
            candidate_job_id = _job_id_for_index(candidate_job, candidate_idx)
            if candidate_job_id in processed_states or candidate_job_id in pending_skip_job_ids:
                continue
            candidate_directory = str(candidate_job.get("directory") or "").strip().lower()
            if source == "unearthed":
                if "unearthed" in candidate_directory:
                    return candidate_idx, candidate_job
            elif candidate_directory == source:
                return candidate_idx, candidate_job
        return None

    for idx, job in enumerate(jobs):
        job_id = _job_id_for_index(job, idx)
        if job_id in processed_states:
            job_states.append(processed_states[job_id])
            continue
        if job_id in pending_skip_job_ids:
            skipped_state = _mark_job_skipped_for_spotify_fallback(
                job,
                run_dir,
                per_job_validate=not master_enrichment_enabled,
            )
            processed_states[job_id] = skipped_state
            job_states.append(skipped_state)
            continue

        result_state = _process_job_now(job, allow_stop_on_failure=stop_on_failure)
        processed_states[job_id] = result_state
        job_states.append(result_state)

        directory = str(job.get("directory") or "").strip().lower()
        spotify_zero_rows = directory == "spotify" and _job_row_count(result_state) == 0
        if spotify_zero_rows:
            master_logger.info("[Spotify][Seed] 0 artists discovered from playlist.")
            master_logger.info(_spotify_zero_row_reason(result_state))
            master_logger.info("Continuing pipeline.")

            if spotify_fallback_cfg.enabled:
                if spotify_fallback_cfg.order:
                    master_logger.info(
                        "[Spotify][Seed] 0 rows detected; triggering fallback chain: %s",
                        ",".join(spotify_fallback_cfg.order),
                    )
                    successful_source = ""
                    for source_pos, source in enumerate(spotify_fallback_cfg.order):
                        # Fallback only reuses pending configured jobs already present
                        # later in the current run config; env vars do not inject jobs.
                        master_logger.info("[Seed Fallback] attempting configured fallback source: %s", source)
                        pending = _find_pending_job_for_source(source, idx + 1)
                        if pending is None:
                            master_logger.info("[Seed Fallback] no configured pending job found for source: %s", source)
                            continue
                        fallback_idx, fallback_job = pending
                        fallback_job_id = _job_id_for_index(fallback_job, fallback_idx)
                        fallback_state = _process_job_now(fallback_job, allow_stop_on_failure=False)
                        processed_states[fallback_job_id] = fallback_state
                        fallback_rows = _job_row_count(fallback_state)
                        if fallback_rows > 0:
                            master_logger.info("[Seed Fallback] %s produced %s artists", source, fallback_rows)
                            successful_source = source
                            for remaining_source in spotify_fallback_cfg.order[source_pos + 1 :]:
                                remaining_pending = _find_pending_job_for_source(remaining_source, idx + 1)
                                if remaining_pending is None:
                                    continue
                                remaining_idx, remaining_job = remaining_pending
                                remaining_job_id = _job_id_for_index(remaining_job, remaining_idx)
                                pending_skip_job_ids[remaining_job_id] = remaining_source
                            break
                        master_logger.info("[Seed Fallback] %s produced 0 artists", source)
                    if not successful_source:
                        master_logger.info("[Seed Fallback] no fallback source produced artists")
                else:
                    master_logger.info("[Seed Fallback] fallback enabled but no valid order remains after validation; doing nothing")

        if stop_on_failure and result_state.get("status") in {"failed"}:
            break

    _emit_seed_source_summary(jobs, processed_states, master_logger)

    master_raw = None
    master_enriched = None
    master_pre_fb = None
    master_post_fb = None
    master_final = None
    night_fb_run_state = None
    if export_mode in {"combined", "both"}:
        night_fb_run_state = create_night_fb_run_state(
            os.environ.get("FB_USERNAME", "").strip(),
            os.environ.get("FB_PASSWORD", "").strip(),
        )
    if export_mode in {"combined", "both"}:
        logger = master_logger
        stats_logger = _wrap_logger_for_stats(logger.info, stats)
        try:
            if master_enrichment_enabled:
                master_raw = _merge_raw_master(run_dir, job_states, logger, stats=stats)
                if master_raw and os.path.exists(master_raw):
                    master_enriched = os.path.join(run_dir, "master_enriched.csv")
                    master_enriched = _call_with_optional_master_enrichment_kwargs(
                        run_master_enrichment,
                        master_raw,
                        master_enriched,
                        logger=stats_logger,
                        enable_live_search=master_live_search_enabled,
                        max_live_searches=master_max_live_searches,
                        night_mode=True,
                        night_fb_run_state=night_fb_run_state,
                        night_runtime_reset_interval_rows=master_night_runtime_reset_interval_rows,
                    )
                    master_pre_fb = os.path.join(run_dir, "master_pre_fb.csv")
                    master_pre_fb = run_enrichment(master_enriched, master_pre_fb, logger=stats_logger, night_mode=True)
                    stats.enrichment_ran = True
                    try:
                        stats.enrichment_rows = _count_rows(master_pre_fb)
                    except Exception:
                        pass
            else:
                master_pre_fb = _merge_master(run_dir, job_states, logger)
            if master_pre_fb and os.path.exists(master_pre_fb):
                try:
                    df_master = pd.read_csv(master_pre_fb, dtype=str, keep_default_na=False).fillna("")
                    df_master = ensure_fb_attribution_columns(df_master)
                    df_master = apply_fb_opportunity_state_df(df_master, overwrite=False)
                    df_master = quarantine_repeated_emails(df_master, min_repeats=5, logger=logger)
                    df_master.to_csv(master_pre_fb, index=False)
                except Exception as exc:
                    logger.warning("[Master] Quarantine repeated emails failed safely: %s", exc)

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
                            fb_status = _call_with_optional_night_fb_run_state(
                                run_facebook_global_pass_nightmode,
                                master_pre_fb,
                                master_post_fb,
                                state_path=fb_state_path,
                                max_rows_per_run=fb_max_rows_per_run,
                                logger=logger.info,
                                night_fb_run_state=night_fb_run_state,
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
        finally:
            close_night_fb_run_state(night_fb_run_state)

    summary_logger = master_logger or _setup_logger(os.path.join(run_dir, "master_log.txt"), "master")
    final_artifact = _select_existing_artifact(
        _final_master_candidates(
            run_dir,
            master_final=master_final,
            master_post_fb=master_post_fb,
            master_pre_fb=master_pre_fb,
            master_enriched=master_enriched,
        )
    )
    if final_artifact:
        try:
            stats.emails_total = _count_nonempty_email_rows(final_artifact)
        except Exception:
            pass
    _emit_smoke_summary(stats, summary_logger)
    try:
        run_summary = _build_run_summary(
            run_dir,
            master_raw=master_raw,
            master_enriched=master_enriched,
            master_pre_fb=master_pre_fb,
            master_post_fb=master_post_fb,
            master_final=master_final,
        )
        run_summary_path = _write_run_summary(run_dir, run_summary)
        summary_logger.info("[Run Summary] Wrote run summary: %s", run_summary_path)
    except Exception as exc:
        summary_logger.warning("[Run Summary] Failed safely: %s", exc)

    return {
        "run_dir": run_dir,
        "jobs": job_states,
        "master_raw": master_raw,
        "master_enriched": master_enriched,
        "master_pre_fb": master_pre_fb,
        "master_post_fb": master_post_fb,
        "master_csv": master_final,
        "export_mode": export_mode,
        "smoke_stats": stats.as_dict(),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Night Mode runner for Lead Machine")
    parser.add_argument("--config", required=True, help="Path to overnight_jobs.json config file")
    parser.add_argument("--resume", action="store_true", help="Resume the latest overnight run")
    parser.add_argument(
        "--phased",
        action="store_true",
        default=False,
        help="Run Night Mode via v2 phased runner (opt-in).",
    )
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
    parser.add_argument(
        "--with-sc-meta",
        action="store_true",
        help=(
            "After scraping SoundCloud, run the SoundCloud metadata enricher to fill missing Primary Genre / Release Date fields."
        ),
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
    if args.phased:
        from night_mode_v2.phased_runner import run_phased_night_mode

        result = run_phased_night_mode(
            config_path=args.config,
            run_root=args.run_root,
            resume=args.resume,
            stop_on_failure=args.stop_on_failure,
            export_mode=args.export_mode,
            export_profile=args.export_profile,
            fb_auto_resume_override=args.fb_auto_resume,
            fb_cooldown_override=args.fb_cooldown_seconds,
            fb_max_attempts_override=args.fb_max_auto_resume_attempts,
            fb_max_rows_override=args.fb_max_rows_per_run,
            with_sc_meta=args.with_sc_meta,
        )
    else:
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
            with_sc_meta=args.with_sc_meta,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
