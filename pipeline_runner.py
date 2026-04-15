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
import re
import shutil
import tempfile
import time
import datetime
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union, MutableMapping

import pandas as pd
from email_provenance import (
    EMAIL_PROVENANCE_JSON_COL,
    _set_email_with_provenance,
    get_email_provenance_entry,
    get_row_email_provenance,
    merge_email_provenance_into_target,
    normalize_email_key,
    parse_email_provenance_json,
)
from email_normalizer import filter_system_telemetry_emails, is_obvious_placeholder_email
from fb_attribution import (
    FB_ATTEMPT_STATE_COL,
    FB_ATTRIBUTION_COLUMNS,
    FB_DEBUG_REASON_COL,
    FB_GATE_STATE_COL,
    FB_OPPORTUNITY_STATE_COL,
    FB_WRITE_STATE_COL,
    apply_fb_opportunity_state_df,
    ensure_fb_attribution_columns,
)
from html_fetcher import close_job_browser
from source_scheduler import (
    canonicalize_facebook_url,
    ensure_canonical_facebook_url,
    is_spotify_origin_row,
    preferred_upstream_identity_hint,
    promote_facebook_url,
)

try:  # Shared FB helper; safe fallback if unavailable.
    from facebook_enrich import is_fb_login_redirect  # type: ignore
except Exception:  # pragma: no cover - defensive
    def is_fb_login_redirect(url: str) -> bool:  # type: ignore
        return False

from night_mode_fb import (
    FacebookDriverError,
    NightFBRunState,
    NightModeFacebookEnricher,
    close_night_fb_run_state,
    create_night_fb_run_state,
    explicit_fb_entrypoint_urls_for_row,
    normalize_night_fb_session_source,
)

LoggerFn = Optional[Callable[[str], None]]

_LEGACY_MODULE = None
_LOGGER = logging.getLogger(__name__)
EMAIL_PRIORITY_COLS: Sequence[str] = ("Email", "Email_All", "Directory_Email", "Unearthed_Email")

_EMAIL_SUMMARY = {"emails_found": 0, "pattern_emails": 0}
ENRICHMENT_YIELD_SOURCE_ORDER: Sequence[str] = (
    "website",
    "facebook",
    "soundcloud",
    "lastfm",
    "domain_reuse",
    "bandcamp",
    "bandcamp_directory",
    "unearthed",
    "instagram",
)
FB_DISCOVERY_ATTEMPT_FLAG_COL = "__fb_discovery_attempted_this_run"
FB_DISCOVERY_ATTEMPT_FLAG_ALIASES: Sequence[str] = (
    FB_DISCOVERY_ATTEMPT_FLAG_COL,
    "__fb_discovery_attempted",
    "fb_discovery_attempted",
)

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNEARTHED_TRACK_URL_COLUMNS = (
    "track url",
    "track_url",
    "trackurl",
    "source url",
    "source_url",
    "unearthed url",
    "unearthed_url",
    "profile url",
    "profile_url",
)

_UNEARTHED_LINK_COLUMNS = (
    "Social Link",
    "External Links",
    "Website",
    "Facebook_URL",
    "Facebook URL",
    "Instagram_URL",
    "YouTube_URL",
    "TikTok_URL",
)

_EMAIL_ROLE_PRIORITY: Dict[str, int] = {
    "booking": 0,
    "bookings": 0,
    "agent": 0,
    "agents": 0,
    "mgmt": 1,
    "management": 1,
    "manager": 1,
    "press": 2,
    "media": 2,
    "pr": 2,
    "contact": 3,
    "info": 10,
    "hello": 10,
    "enquiries": 11,
    "enquiry": 11,
    "office": 12,
    "support": 90,
    "help": 90,
    "admin": 91,
    "accounts": 92,
    "billing": 92,
    "legal": 93,
    "privacy": 93,
    "webmaster": 94,
    "noreply": 95,
    "donotreply": 95,
}

_LINK_TOKEN_SPLIT_RE = re.compile(r"\s*\|\s*|\s*,\s*|\s+")


def _normalize_ws_lower(value: str) -> str:
    """Lowercase, strip, and collapse whitespace for stable comparisons."""
    text = (value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _normalize_track_urlish(url: str) -> str:
    """
    Normalize a URL-like string for equality:
    - strip/trim
    - lower scheme/netloc
    - drop query/fragment
    - remove trailing slash
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        path = parts.path.rstrip("/")
        norm = f"{(parts.scheme or 'http').lower()}://{parts.netloc.lower()}{path}"
    else:
        norm = raw
    return norm.lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _find_unearthed_track_url_column(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        if col and str(col).lower() in _UNEARTHED_TRACK_URL_COLUMNS:
            return col
    return None


def _linkish_columns(df: pd.DataFrame, track_col: Optional[str]) -> List[str]:
    """Return list of social/external link columns present in df."""
    cols: List[str] = []
    for col in df.columns:
        if col == track_col:
            continue
        if col in _UNEARTHED_LINK_COLUMNS or ("link" in str(col).lower() and str(col).strip()):
            cols.append(col)
    return cols


def _split_links(cell: Any) -> List[str]:
    """Tokenize a linkish cell into ordered, de-duped list of strings."""
    if cell is None:
        return []
    text = str(cell).strip()
    if not text:
        return []
    tokens = _LINK_TOKEN_SPLIT_RE.split(text)
    seen = set()
    result: List[str] = []
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
    return result


def _merge_link_columns(target: MutableMapping[str, Any], incoming: MutableMapping[str, Any], cols: Sequence[str]) -> None:
    """
    Merge linkish columns from incoming into target in-place:
    - union tokens, preserve first-seen order
    - skip empty sources
    """
    for col in cols:
        if col not in incoming:
            continue
        incoming_tokens = _split_links(incoming.get(col, ""))
        if not incoming_tokens:
            continue
        existing_tokens = _split_links(target.get(col, ""))
        ordered: List[str] = []
        seen = set()
        for tok in existing_tokens + incoming_tokens:
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(tok)
        target[col] = " | ".join(ordered)


def _dedupe_unearthed_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop duplicate Unearthed rows by stable key while merging link fields:
      1) Prefer normalized track URL when present.
      2) Otherwise use (artist_norm, song_norm).
    Keep first occurrence; preserve order; merge social/external links from
    duplicates into the kept row.
    """
    if df is None or df.empty:
        return df

    track_col = _find_unearthed_track_url_column(df)
    track_norm = (
        df[track_col].fillna("").astype(str).apply(_normalize_track_urlish)
        if track_col
        else pd.Series("", index=df.index)
    )
    artist_norm = df.get("Artist Name", pd.Series("", index=df.index)).fillna("").astype(str).apply(_normalize_ws_lower)
    song_norm = df.get("Song Title", pd.Series("", index=df.index)).fillna("").astype(str).apply(_normalize_ws_lower)

    seen_track: set[str] = set()
    seen_artist_song: set[tuple[str, str]] = set()
    track_to_row: dict[str, int] = {}
    artist_song_to_row: dict[tuple[str, str], int] = {}
    output_rows: list[MutableMapping[str, Any]] = []

    mergeable_cols = _linkish_columns(df, track_col)

    for idx in df.index:
        key = (artist_norm.at[idx], song_norm.at[idx])
        tnorm = track_norm.at[idx] if track_col else ""
        row_dict: MutableMapping[str, Any] = df.loc[idx].to_dict()
        if tnorm:
            if tnorm in seen_track:
                target_idx = track_to_row[tnorm]
                _merge_link_columns(output_rows[target_idx], row_dict, mergeable_cols)
                if key not in artist_song_to_row:
                    artist_song_to_row[key] = target_idx
                continue
            seen_track.add(tnorm)
            # Tie the URL and artist+song namespaces so url-less duplicates are dropped.
            seen_artist_song.add(key)
            artist_song_to_row[key] = len(output_rows)
            track_to_row[tnorm] = len(output_rows)
            output_rows.append(row_dict)
            continue

        if key in seen_artist_song:
            target_idx = artist_song_to_row.get(key)
            if target_idx is not None:
                _merge_link_columns(output_rows[target_idx], row_dict, mergeable_cols)
            continue
        seen_artist_song.add(key)
        artist_song_to_row[key] = len(output_rows)
        output_rows.append(row_dict)

    if not output_rows:
        return df.iloc[0:0].copy()

    deduped = pd.DataFrame(output_rows)
    return deduped.reindex(columns=df.columns).reset_index(drop=True)


def _dedupe_unearthed_csv(path: str, logger: LoggerFn = None) -> None:
    """Best-effort dedupe for Unearthed temp CSVs before promotion."""
    if not path or not os.path.exists(path):
        return
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        return
    before = len(df.index)
    if before <= 1:
        return
    deduped = _dedupe_unearthed_rows(df)
    after = len(deduped.index)
    if after == before:
        return
    try:
        deduped.to_csv(path, index=False, encoding="utf-8-sig")
        _safe_log(logger, f"[Unearthed] Deduped temp CSV: removed {before - after} duplicates (kept {after}).")
    except Exception:
        # If writing fails, keep original to avoid data loss.
        pass


@dataclass
class AtomicCSVResult:
    final_path: Path
    tmp_path: Path
    row_count: int
    raw_bytes: int


def _should_keep_tmp_on_failure() -> bool:
    flag = os.getenv("KEEP_TMP_ON_FAILURE", "")
    return flag in {"1", "true", "TRUE"}


def _bump_email_summary(key: str, delta: int = 1) -> None:
    if delta <= 0:
        return
    if key in _EMAIL_SUMMARY:
        _EMAIL_SUMMARY[key] += delta


def increment_pattern_emails(delta: int = 1) -> None:
    _bump_email_summary("pattern_emails", delta)


def reset_email_summary_counts() -> None:
    for key in _EMAIL_SUMMARY:
        _EMAIL_SUMMARY[key] = 0


def get_email_summary_counts() -> Dict[str, int]:
    return dict(_EMAIL_SUMMARY)


def record_email_summary_row_change(before_row: Any, after_row: Any) -> int:
    """Count newly landed row emails from an actual before/after transition."""

    def _emails(row_like: Any) -> set[str]:
        if row_like is None or not hasattr(row_like, "get"):
            return set()
        return set(
            _merge_email_lists(
                row_like.get("Email", ""),
                row_like.get("Email_All", ""),
            )
        )

    before_emails = _emails(before_row)
    after_emails = _emails(after_row)
    delta = len(after_emails - before_emails)
    _bump_email_summary("emails_found", delta)
    return delta


def _ordered_enrichment_yield_items(counts: Dict[str, int]) -> List[Tuple[str, int]]:
    ordered: List[Tuple[str, int]] = []
    seen: Set[str] = set()
    for source in ENRICHMENT_YIELD_SOURCE_ORDER:
        count = int(counts.get(source, 0) or 0)
        if count <= 0:
            continue
        ordered.append((source, count))
        seen.add(source)
    for source in sorted(counts):
        if source in seen:
            continue
        count = int(counts.get(source, 0) or 0)
        if count <= 0:
            continue
        ordered.append((source, count))
    return ordered


def emit_enrichment_yield_summary(logger: LoggerFn, counts: Dict[str, int]) -> None:
    _safe_log(logger, "[Enrichment Yield]")
    for source, count in _ordered_enrichment_yield_items(counts):
        _safe_log(logger, f"{source}={count}")


def _safe_count_rows(csv_path: Union[str, Path]) -> int:
    """Cheap row counter that avoids loading the whole CSV; returns total data rows (excludes header if present)."""
    try:
        with open(csv_path, "rb") as f:
            count = sum(1 for _ in f)
        return max(0, count - 1) if count > 0 else 0
    except Exception:
        return 0


def _count_csv_rows(csv_path: Path) -> int:
    """
    Lightweight row counter for CSVs; counts lines minus header.
    Returns -1 on error.
    """
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            count = sum(1 for _ in f)
        return max(0, count - 1) if count > 0 else 0
    except Exception:
        return -1


def _is_valid_email_shape(email: str) -> bool:
    """Basic, permissive email shape validation."""
    if not email or " " in email:
        return False
    if email.count("@") != 1:
        return False
    local, domain = email.split("@", 1)
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    return True


def _fb_status_is_rejected(status: str) -> bool:
    """Return True when FB_Status denotes a rejected/mismatched/blocked candidate."""
    status_norm = (status or "").lower()
    return any(tok in status_norm for tok in ("reject", "blocked"))


def normalize_emails(value) -> List[str]:
    """
    Split on commas, semicolons, whitespace, newlines. Lowercase, strip, validate
    basic email shape, dedupe, and return sorted list for stable output.
    """
    text = "" if value is None else str(value)
    text = re.sub(r"\s*@\s*", "@", text)
    parts = re.split(r"[\s,;]+", text)
    seen: set[str] = set()
    cleaned: List[str] = []
    for part in parts:
        email = part.strip().lower()
        if not email:
            continue
        if not _is_valid_email_shape(email):
            continue
        if email not in seen:
            seen.add(email)
            cleaned.append(email)
    return sorted(cleaned)

def _merge_email_lists(existing: Union[str, Sequence[str]], new_values: Union[str, Sequence[str]]) -> List[str]:
    """Merge email-like values with normalization + dedupe."""
    merged: List[str] = []
    seen: set[str] = set()

    def _ingest(raw) -> None:
        if raw is None:
            return
        # Treat iterable containers (except strings) as multiple inputs.
        if isinstance(raw, (list, tuple, set)):
            items = list(raw)
        else:
            items = [raw]
        for item in items:
            try:
                if pd.isna(item):
                    continue
            except Exception:
                pass
            for email in normalize_emails(item):
                if email not in seen:
                    seen.add(email)
                    merged.append(email)

    _ingest(existing)
    _ingest(new_values)
    return merged


def _append_suspect_email_all(current: str, addition: str) -> str:
    """Append a suspect email_all value while keeping order/dedupe."""
    merged = _merge_email_lists(current, addition)
    return ";".join(merged)


def _log_email_all_change(row_idx: int, artist: str, before: str, after: str, source: str, logger: LoggerFn = None) -> None:
    """Emit debug log for Email_All mutations when enabled."""
    if os.getenv("EMAIL_ALL_LOG", "1") not in {"1", "true", "TRUE"}:
        return
    msg = (
        f"[EmailAll][{source}] row={row_idx} artist='{artist}' "
        f"before='{before[:120]}' after='{after[:120]}'"
    )
    _safe_log(logger or _LOGGER.info, msg)


def _guard_email_all_sources(row: pd.Series, email_all: str, logger: LoggerFn = None) -> None:
    """Debug-only guard: warn if Email_All contains emails not present in row-local sources."""
    if os.getenv("EMAIL_ALL_GUARD", "0") not in {"1", "true", "TRUE"}:
        return
    sources = []
    for col in row.index:
        if "Email" not in col:
            continue
        if col in {"Email_All", "Suspect_Email_All"}:
            continue
        sources.extend(normalize_emails(_cell_str(row.get(col))))
    source_set = set(sources)
    for email in normalize_emails(email_all):
        if email not in source_set:
            artist = _cell_str(row.get("Artist Name"))
            _safe_log(
                logger or _LOGGER.error,
                f"[EmailAll][GUARD] email_not_in_sources='{email}' row={row.get('__row_id', row.name)} artist='{artist}'",
            )


def _set_email_all(
    df: pd.DataFrame,
    idx: int,
    new_emails: Union[str, Sequence[str]],
    source: str,
    logger: LoggerFn = None,
    *,
    source_url: str = "",
    source_type: str = "",
    method: str = "regex",
    surface: str = "",
) -> str:
    """Centralized Email_All setter with merge + logging + guard."""
    existing_val = _cell_str(df.at[idx, "Email_All"] if "Email_All" in df.columns else "")
    before_list = filter_system_telemetry_emails(normalize_emails(existing_val))
    before_count = len(before_list)
    if source_url or source_type or surface:
        merge_email_provenance_into_target(
            (df, idx),
            new_emails,
            source_url=source_url,
            source_type=source_type,
            method=method,
            surface=surface,
        )
    merged_list = _rank_contact_emails_for_row(df.loc[idx], _merge_email_lists(existing_val, new_emails))
    merged_str = ";".join(merged_list)
    _bump_email_summary("emails_found", max(0, len(merged_list) - before_count))
    df.at[idx, "Email_All"] = merged_str
    artist = _cell_str(df.at[idx, "Artist Name"]) if "Artist Name" in df.columns else ""
    log_enabled = os.getenv("EMAIL_ALL_LOG") in {"1", "true", "TRUE"}
    if log_enabled:
        _log_email_all_change(idx, artist, existing_val, merged_str, source, logger)
    # Guard may emit its own logs only when EMAIL_ALL_GUARD is enabled and a suspicious merge occurs.
    _guard_email_all_sources(df.loc[idx], merged_str, logger)
    return merged_str


def _fb_write_surface_snapshot(row_like: Any) -> Dict[str, Any]:
    email_val = _cell_str(row_like.get("Email", "")) if hasattr(row_like, "get") else ""
    email_all_val = _cell_str(row_like.get("Email_All", "")) if hasattr(row_like, "get") else ""
    return {
        "email": email_val,
        "email_all": email_all_val,
        "email_set": set(normalize_emails(email_val)),
        "email_all_set": set(normalize_emails(email_all_val)),
    }


def _fb_discovery_attempt_already_recorded(row_like: Any) -> bool:
    if row_like is None or not hasattr(row_like, "get"):
        return False
    for key in FB_DISCOVERY_ATTEMPT_FLAG_ALIASES:
        if _cell_str(row_like.get(key, "")):
            return True
    return False


def _classify_fb_write_state(before: Dict[str, Any], after: Dict[str, Any], attempt_state: str) -> str:
    attempt_state_norm = _cell_str(attempt_state)
    before_email = bool(before.get("email_set"))
    after_email = bool(after.get("email_set"))
    email_changed = bool(after.get("email_set", set()) - before.get("email_set", set()))
    email_all_changed = bool(after.get("email_all_set", set()) - before.get("email_all_set", set()))

    if attempt_state_norm == "attempted_fb_rejected_by_acceptance_guard":
        return "fb_found_email_not_applied"
    if email_changed and after_email:
        return "fb_wrote_email"
    if email_all_changed:
        return "fb_wrote_email_all_only"
    if attempt_state_norm == "attempted_fb_found_email":
        return "fb_found_email_not_applied"
    return "fb_no_email_written"


def _classify_fb_attempt_state_from_status(status: str, existing: str = "") -> str:
    existing_clean = _cell_str(existing)
    if existing_clean and existing_clean != "attempted_fb":
        return existing_clean
    status_norm = _cell_str(status).lower()
    if "reject" in status_norm or "blocked" in status_norm:
        return "attempted_fb_rejected_by_acceptance_guard"
    if any(token in status_norm for token in ("login_wall", "login_redirect", "checkpoint", "warning")):
        return "attempted_fb_login_wall_or_checkpoint"
    if "content_unavailable" in status_norm:
        return "attempted_fb_content_unavailable"
    if any(token in status_norm for token in ("timeout", "fetch_error", "driver_error", "no_display")):
        return "attempted_fb_timeout_or_fetch_error"
    if status_norm:
        return "attempted_fb_no_email_on_page"
    return existing_clean or "attempted_fb"


def _classify_fb_debug_reason(row_like: Any) -> str:
    status_norm = _cell_str(row_like.get("FB_Status", "")).lower() if hasattr(row_like, "get") else ""
    opportunity_norm = _cell_str(row_like.get(FB_OPPORTUNITY_STATE_COL, "")).lower() if hasattr(row_like, "get") else ""
    gate_norm = _cell_str(row_like.get(FB_GATE_STATE_COL, "")).lower() if hasattr(row_like, "get") else ""
    attempt_state_norm = _cell_str(row_like.get(FB_ATTEMPT_STATE_COL, "")).lower() if hasattr(row_like, "get") else ""
    write_state_norm = _cell_str(row_like.get(FB_WRITE_STATE_COL, "")).lower() if hasattr(row_like, "get") else ""

    if write_state_norm in {"fb_wrote_email", "fb_wrote_email_all_only"}:
        return "email_written"
    if write_state_norm == "fb_found_email_not_applied":
        return "email_found_not_applied"

    if attempt_state_norm == "attempted_fb_login_wall_or_checkpoint":
        return "login_required_or_blocked"
    if attempt_state_norm == "attempted_fb_content_unavailable":
        return "content_unavailable"
    if attempt_state_norm == "attempted_fb_timeout_or_fetch_error":
        return "timeout_or_fetch_error"
    if attempt_state_norm in {"attempted_fb_no_email_on_page", "attempted_fb"}:
        return "no_email_visible"

    if gate_norm in {"skipped_no_identity_anchor", "skipped_no_canonical_facebook_url"}:
        return "no_fb_candidate"
    if gate_norm in {
        "skipped_duplicate_fb_discovery",
        "skipped_existing_usable_email",
        "skipped_other_gate",
        "skipped_terminal_fb_status",
    }:
        return ""
    if opportunity_norm == "no_fb_opportunity":
        return "no_fb_candidate"

    if any(token in status_norm for token in ("checkpoint", "login_redirect", "login_wall", "skipped_warning", "warning")):
        return "login_required_or_blocked"
    if "content_unavailable" in status_norm:
        return "content_unavailable"
    if any(token in status_norm for token in ("timeout", "fetch_error", "driver_error", "no_display")):
        return "timeout_or_fetch_error"
    if status_norm in {"no_candidates", "unearthed_no_candidates"}:
        return "no_fb_candidate"

    return ""


FB_DEBUG_SUMMARY_ORDER: Sequence[str] = (
    "email_written",
    "email_found_not_applied",
    "no_email_visible",
    "login_required_or_blocked",
    "content_unavailable",
    "timeout_or_fetch_error",
    "no_fb_candidate",
)
FB_DEBUG_SUMMARY_VALID: Set[str] = set(FB_DEBUG_SUMMARY_ORDER)
FB_REFINE_SUMMARY_ORDER: Sequence[str] = (
    "allowed",
    "skipped_junk_gate",
    "skipped_suppressed",
    "skipped_overlay",
    "skipped_not_allowed",
)
FB_REFINE_SUMMARY_VALID: Set[str] = set(FB_REFINE_SUMMARY_ORDER)


def _log_fb_debug_summary(df: Optional[pd.DataFrame], logger: LoggerFn = None) -> None:
    counts = {reason: 0 for reason in FB_DEBUG_SUMMARY_ORDER}
    if isinstance(df, pd.DataFrame) and FB_DEBUG_REASON_COL in df.columns:
        for raw_value in df[FB_DEBUG_REASON_COL].tolist():
            reason = _cell_str(raw_value)
            if reason in FB_DEBUG_SUMMARY_VALID:
                counts[reason] += 1

    _safe_log_console(logger, "[FB Debug Summary]")
    for reason in FB_DEBUG_SUMMARY_ORDER:
        _safe_log_console(logger, f"{reason}={counts[reason]}")


def _log_fb_refine_summary(df: Optional[pd.DataFrame], logger: LoggerFn = None) -> None:
    if not isinstance(df, pd.DataFrame) or "FB_Refine_Decision" not in df.columns:
        return

    counts = {decision: 0 for decision in FB_REFINE_SUMMARY_ORDER}
    saw_valid_decision = False
    for raw_value in df["FB_Refine_Decision"].tolist():
        decision = _cell_str(raw_value)
        if decision in FB_REFINE_SUMMARY_VALID:
            counts[decision] += 1
            saw_valid_decision = True

    if not saw_valid_decision:
        return

    _safe_log_console(logger, "[FB Refine Summary]")
    for decision in FB_REFINE_SUMMARY_ORDER:
        count = counts[decision]
        if count:
            _safe_log_console(logger, f"{decision}={count}")


def _row_has_valid_email(row: pd.Series) -> Tuple[bool, List[str]]:
    """Return (has_email, normalized_emails) using the pipeline's permissive parser."""
    emails = _merge_email_lists(row.get("Email", ""), row.get("Email_All", ""))
    return (len(emails) > 0, emails)


def has_contact_email_for_short_circuit(row: pd.Series) -> bool:
    """Return True when row has a contact-worthy email (filters noreply/support/system)."""
    has_email, emails = _row_has_valid_email(row)
    if not has_email:
        return False
    forbidden = ("noreply", "no-reply", "donotreply", "support", "system")
    for email in emails:
        lower = (email or "").lower()
        if "@" not in lower or "." not in lower:
            continue
        if any(token in lower for token in forbidden):
            continue
        return True
    return False


def _strong_domain_match_short_name(artist_name: str, emails: List[str]) -> bool:
    """
    Optional helper: for very short artist names, treat a domain token match as a strong signal
    that directory_conflict-based BLOCKs are over-aggressive.
    """
    artist_slug = re.sub(r"[^a-z0-9]+", "", (artist_name or "").lower())
    if not artist_slug or len(artist_slug) > 4:
        return False
    for email in emails:
        if "@" not in email:
            continue
        domain = email.split("@", 1)[1].lower()
        if artist_slug in domain:
            return True
    return False


def recompute_final_status_post_enrichment(df: pd.DataFrame, logger: LoggerFn = None) -> pd.DataFrame:
    """
    Post-enrichment guardrail to repair stale non-OK statuses after enrichment.

    Existing BLOCK repair behaviour is preserved for origin/directory cases.
    Other non-OK rows are reclassified with the canonical final checker so
    stale WARN rows can be promoted to OK after successful enrichment.
    """
    if df is None or df.empty:
        return df

    if "Needs_Review" not in df.columns:
        df["Needs_Review"] = ""
    if "FB_Review_Reason" not in df.columns:
        df["FB_Review_Reason"] = ""

    status_col = "final_status" if "final_status" in df.columns else None
    if not status_col:
        return df

    import final_checker

    def _truthy(val) -> bool:
        return str(val).strip().lower() in {"1", "true", "yes", "y"}

    def _parse_floatlike(val, default: float = 0.0) -> float:
        text = str(val or "").strip()
        if not text:
            return default
        try:
            parsed = float(text)
        except Exception:
            return default
        if parsed != parsed:
            return default
        return parsed

    def _parse_intlike(val, default: int = 0) -> int:
        try:
            return int(_parse_floatlike(val, float(default)))
        except Exception:
            return default

    for idx, row in df.iterrows():
        status = str(row.get(status_col, "") or "").strip().upper()
        if status == "OK":
            continue

        if status == "BLOCK":
            # Preserve the existing late BLOCK repair contract.
            if "FB_Status" in df.columns:
                fb_status_raw = str(row.get("FB_Status", "") or "")
                if _fb_status_is_rejected(fb_status_raw):
                    continue
            if "duplicate_email_flag" in df.columns and _truthy(row.get("duplicate_email_flag", 0)):
                continue
            if "duplicate_artist_flag" in df.columns and _truthy(row.get("duplicate_artist_flag", 0)):
                continue

            dup_email = _parse_intlike(row.get("duplicate_email_flag", 0), 0)
            dup_artist = _parse_intlike(row.get("duplicate_artist_flag", 0), 0)
            origin_flag = _parse_intlike(row.get("origin_match_flag", 1), 1)
            dir_conflict = _parse_intlike(row.get("directory_conflict_flag", 0), 0)
            name_flag = _parse_intlike(row.get("name_consistency_flag", 0), 0)

            # Hard BLOCK guards
            fb_status_val = str(row.get("FB_Status", "") or "")
            if _fb_status_is_rejected(fb_status_val):
                continue
            if dup_email == 1 or dup_artist == 1:
                continue

            has_email, emails = _row_has_valid_email(row)
            if not has_email:
                continue
            if not emails or not all(_is_valid_email_shape(e) for e in emails):
                continue

            origin_or_dir_conflict = (origin_flag == 0) or (dir_conflict == 1)
            short_name_domain_match = _strong_domain_match_short_name(str(row.get("Artist Name", "")), emails)
            if not origin_or_dir_conflict and not short_name_domain_match:
                continue
            if name_flag != 1:
                continue

            df.at[idx, status_col] = "WARN"
            df.at[idx, "Needs_Review"] = "TRUE"
            df.at[idx, "FB_Review_Reason"] = "origin_mismatch_downgraded"

            artist = str(row.get("Artist Name", "") or "").strip()
            primary_email = emails[0] if emails else ""
            log_msg = (
                f"[PostEnrichStatus] Downgraded '{artist}' email='{primary_email}' "
                f"from BLOCK -> WARN (reason=origin_mismatch_downgraded)"
            )
            try:
                if logger and hasattr(logger, "debug"):
                    logger.debug(log_msg)
                elif _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(log_msg)
            except Exception:
                pass
            continue

        row_dict = df.loc[idx].to_dict()
        required_classifier_inputs = (
            "name_consistency_flag",
            "directory_conflict_flag",
            "duplicate_email_flag",
            "duplicate_artist_flag",
            "match_score_overall",
        )
        if any(str(row_dict.get(column, "") or "").strip() == "" for column in required_classifier_inputs):
            continue
        name_consistency_flag = _parse_intlike(row_dict.get("name_consistency_flag", 0), 0)
        flags = {
            "name_flag": 0 if name_consistency_flag == 1 else 1,
            "dir_conflict_flag": _parse_intlike(row_dict.get("directory_conflict_flag", 0), 0),
            "dup_email_flag": _parse_intlike(row_dict.get("duplicate_email_flag", 0), 0),
            "dup_artist_flag": _parse_intlike(row_dict.get("duplicate_artist_flag", 0), 0),
            "genre_outlier_flag": _parse_intlike(row_dict.get("genre_outlier_flag", 0), 0),
        }
        match_score = _parse_floatlike(row_dict.get("match_score_overall", 0), 0.0)
        computed_status = final_checker.compute_final_status(row_dict, flags, match_score)
        if computed_status != status:
            df.at[idx, status_col] = computed_status

    return df

def emails_to_string(emails: List[str]) -> str:
    """Return ", ".join(emails) or empty string."""
    return ", ".join(emails) if emails else ""


def _email_role_priority(email: str) -> int:
    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        return 50
    local = normalized.split("@", 1)[0]
    compact = re.sub(r"[^a-z0-9]+", "", local)
    tokens = {token for token in re.split(r"[^a-z0-9]+", local) if token}

    for alias, priority in _EMAIL_ROLE_PRIORITY.items():
        alias_compact = re.sub(r"[^a-z0-9]+", "", alias)
        if compact == alias_compact or alias in tokens:
            return priority
    return 50


_ARTIST_OWNED_WEBSITE_FIELDS: Sequence[str] = (
    "Spotify_Website_URL",
    "Website",
    "Website URL",
    "Websites",
    "External Links",
    "Source URL",
)
_NON_ARTIST_WEBSITE_HOSTS: Tuple[str, ...] = (
    "facebook.com",
    "instagram.com",
    "soundcloud.com",
    "bandcamp.com",
    "last.fm",
    "spotify.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "linktr.ee",
    "beacons.ai",
)
_WEAK_EXTERNAL_EMAIL_DOMAINS: Tuple[str, ...] = (
    "bandcamp.com",
    "soundcloud.com",
    "spotify.com",
    "mailchimp.com",
    "list-manage.com",
    "substack.com",
    "squarespace.com",
    "wix.com",
)
_STRONG_DIRECT_SURFACES = {"facebook_about", "facebook_main", "website_contact_page", "website_homepage"}
_PROFILE_DIRECT_SURFACES = {
    "instagram_profile",
    "soundcloud_profile",
    "bandcamp_contact_follow",
    "bandcamp_profile",
    "bandcamp_track_follow",
    "lastfm_profile",
    "spotify_profile",
}


def _iter_urlish_tokens(value: Any) -> Iterable[str]:
    text = _cell_str(value)
    if not text:
        return []
    return [token for token in re.split(r"[\s,;|]+", text) if token.startswith(("http://", "https://"))]


def _normalized_host(url: str) -> str:
    raw = _cell_str(url)
    if not raw:
        return ""
    try:
        host = (urlsplit(raw).netloc or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _email_domain(email: str) -> str:
    normalized = normalize_email_key(email)
    if "@" not in normalized:
        return ""
    return normalized.split("@", 1)[1]


def _identity_token_parts(value: Any) -> List[str]:
    text = _cell_str(value).lower()
    if not text:
        return []
    return [token for token in re.split(r"[^a-z0-9]+", text) if len(token) >= 3]


def _row_email_identity_tokens(row_like: Any, artist_domain: str) -> Tuple[str, ...]:
    if row_like is None or not hasattr(row_like, "get"):
        return ()

    ordered_tokens: List[str] = []
    seen: Set[str] = set()

    def _append_tokens(tokens: Iterable[str]) -> None:
        for token in tokens:
            if not token or token in seen:
                continue
            seen.add(token)
            ordered_tokens.append(token)

    _append_tokens(_identity_token_parts(row_like.get("Artist Name", "")))
    _append_tokens(_identity_token_parts(preferred_upstream_identity_hint(row_like)))
    if artist_domain:
        for label in artist_domain.split(".")[:-1]:
            _append_tokens(_identity_token_parts(label))

    return tuple(ordered_tokens)


def _email_identity_score(email: str, identity_tokens: Sequence[str]) -> int:
    normalized = normalize_email_key(email)
    if "@" not in normalized or not identity_tokens:
        return 0

    local_part, domain = normalized.split("@", 1)
    domain_labels = [label for label in domain.split(".") if label]
    local_match = any(token in local_part for token in identity_tokens)
    domain_match = any(
        token in label
        for token in identity_tokens
        for label in domain_labels
    )
    return (2 if local_match else 0) + (1 if domain_match else 0)


def _row_artist_owned_domain(row_like: Any) -> str:
    if row_like is None or not hasattr(row_like, "get"):
        return ""

    def _candidate_host(url: str) -> str:
        host = _normalized_host(url)
        if not host:
            return ""
        if any(host == blocked or host.endswith(f".{blocked}") for blocked in _NON_ARTIST_WEBSITE_HOSTS):
            return ""
        return host

    for field in _ARTIST_OWNED_WEBSITE_FIELDS:
        for token in _iter_urlish_tokens(row_like.get(field, "")):
            host = _candidate_host(token)
            if host:
                return host

    for meta in get_row_email_provenance(row_like).values():
        source_type = _cell_str(meta.get("source_type", "")).lower()
        source_url = _cell_str(meta.get("source_url", ""))
        if not source_url or not source_type.startswith("website"):
            continue
        host = _candidate_host(source_url)
        if host:
            return host
    return ""


def _email_surface_bucket(row_like: Any, email: str, meta: Mapping[str, Any], artist_domain: str) -> int:
    surface = _cell_str(meta.get("surface", "")).lower()
    source_type = _cell_str(meta.get("source_type", "")).lower()
    source_host = _normalized_host(meta.get("source_url", ""))
    email_domain = _email_domain(email)
    artist_match = bool(artist_domain and email_domain == artist_domain)
    source_matches_artist = bool(artist_domain and source_host == artist_domain)

    if surface in {"facebook_about", "facebook_main"} or source_type.startswith("facebook"):
        return 0
    if surface in {"website_contact_page", "website_homepage"} or source_type.startswith("website"):
        weak_external_email = bool(
            email_domain
            and email_domain != artist_domain
            and (
                _email_role_priority(email) >= 90
                or any(
                    email_domain == blocked or email_domain.endswith(f".{blocked}")
                    for blocked in _WEAK_EXTERNAL_EMAIL_DOMAINS
                )
            )
        )
        if artist_domain and source_matches_artist and weak_external_email and not artist_match:
            return 2
        if not artist_domain or artist_match or source_matches_artist:
            return 0
        return 2
    if surface in _PROFILE_DIRECT_SURFACES:
        return 1
    return 2


def _get_email_source_trust(source_type: Any = "", surface: Any = "") -> int:
    source_type_clean = _cell_str(source_type).lower()
    surface_clean = _cell_str(surface).lower()
    if source_type_clean.startswith(("facebook", "instagram")) or surface_clean.startswith(("facebook_", "instagram_")):
        return 3
    if source_type_clean.startswith("website") or surface_clean.startswith("website_"):
        return 2
    if source_type_clean.startswith(("soundcloud", "bandcamp", "lastfm", "spotify", "unearthed")):
        return 1
    if source_type_clean in {"domain_reuse", "live_search"}:
        return 1
    if surface_clean in _PROFILE_DIRECT_SURFACES or surface_clean in {"domain_reuse", "live_search"}:
        return 1
    return 0


def _rank_contact_emails_for_row(row_like: Any, values: Union[str, Sequence[str], None]) -> List[str]:
    normalized = filter_system_telemetry_emails(_merge_email_lists("", values or []))
    if not normalized:
        return []

    artist_domain = _row_artist_owned_domain(row_like)
    identity_tokens = _row_email_identity_tokens(row_like, artist_domain)
    explicit_provenance = {}
    if row_like is not None and hasattr(row_like, "get"):
        explicit_provenance = parse_email_provenance_json(row_like.get(EMAIL_PROVENANCE_JSON_COL, ""))
    current_selected = ""
    preserve_legacy_current = False
    if row_like is not None and hasattr(row_like, "get"):
        current_selected = normalize_email_key(row_like.get("Email") or row_like.get("Primary Email") or "")
    if current_selected:
        preserve_legacy_current = (
            _email_role_priority(current_selected) < 90
            or bool(artist_domain and _email_domain(current_selected) == artist_domain)
        )
    indexed = list(enumerate(normalized))

    def _sort_key(item: Tuple[int, str]) -> Tuple[int, int, int, int, int, int, int, int, str]:
        index, email = item
        meta = get_email_provenance_entry(row_like, email)
        bucket = _email_surface_bucket(row_like, email, meta, artist_domain)
        source_trust = _get_email_source_trust(meta.get("source_type", ""), meta.get("surface", ""))
        if source_trust == 2 and bucket >= 2:
            # Keep weak/external website finds from outranking profile evidence just because they were website-sourced.
            source_trust = 1
        extract_method = _cell_str(meta.get("extract_method", "")).lower()
        if extract_method == "regex":
            extract_method_penalty = 0
        elif extract_method == "mailto":
            extract_method_penalty = 1
        elif extract_method:
            extract_method_penalty = 2
        else:
            extract_method_penalty = 3
        artist_domain_penalty = 0 if artist_domain and _email_domain(email) == artist_domain else 1
        identity_penalty = 0 if _email_identity_score(email, identity_tokens) > 0 else 1
        legacy_current_penalty = 0 if (not explicit_provenance and preserve_legacy_current and email == current_selected) else 1
        has_provenance_penalty = 0 if meta else 1
        return (
            3 - source_trust,
            bucket,
            artist_domain_penalty,
            identity_penalty,
            legacy_current_penalty,
            _email_role_priority(email),
            has_provenance_penalty,
            extract_method_penalty,
            index,
            email,
        )

    ranked = sorted(indexed, key=_sort_key)
    return [email for _, email in ranked]


def _select_primary_email_for_row(row_like: Any, email: str, email_all: str) -> Tuple[str, List[str]]:
    ranked = _rank_contact_emails_for_row(row_like, [email_all, email])
    return (ranked[0] if ranked else "", ranked)


def _align_row_email_provenance(df: pd.DataFrame, idx: int, selected_email: str) -> None:
    if df is None or idx not in df.index or not selected_email:
        return
    meta = get_email_provenance_entry(df.loc[idx], selected_email)
    if not meta:
        return

    for column, key in (
        ("Email_Source_URL", "source_url"),
        ("Email_Source_Type", "source_type"),
        ("Email_Extract_Method", "extract_method"),
    ):
        if column not in df.columns:
            df[column] = ""
        value = _cell_str(meta.get(key, ""))
        if value:
            df.at[idx, column] = value


def _rank_contact_emails(values: Union[str, Sequence[str], None]) -> List[str]:
    normalized = filter_system_telemetry_emails(_merge_email_lists("", values or []))
    indexed = list(enumerate(normalized))
    ranked = sorted(indexed, key=lambda item: (_email_role_priority(item[1]), item[0], item[1]))
    return [email for _, email in ranked]


def _consolidate_email_all(df: pd.DataFrame) -> pd.DataFrame:
    """Populate Email_All as canonical, normalized union of known email fields."""
    if df is None or df.empty:
        return df

    def _is_quarantined(row: pd.Series) -> bool:
        source_val = _cell_str(row.get("Email Source"))
        suspect_all_val = _cell_str(row.get("Suspect_Email_All"))
        needs_review_val = _cell_str(row.get("Needs_Review"))
        source_is_quarantined = source_val.lower().startswith("quarantined") if source_val else False
        needs_review_flag = needs_review_val.strip().upper() in ("TRUE", "1", "YES", "Y")
        needs_review_quarantine = needs_review_flag and bool(suspect_all_val)
        return source_is_quarantined or needs_review_quarantine

    # Strip seed-directory scraped emails from Email/Email_All before consolidation.
    if "Email Source" in df.columns:
        source_series = df["Email Source"].fillna("").astype(str).str.lower()
        seed_mask = source_series.str.startswith("seed directory")
        if seed_mask.any():
            if "Email" in df.columns:
                df.loc[seed_mask, "Email"] = ""
            if "Email_All" in df.columns:
                df.loc[seed_mask, "Email_All"] = ""

    legacy_fields: Sequence[str] = (
        "Email_All",
        "Email",
        "Emails",
        "email",
        "emails",
        "Email Address",
        "Directory_Email",
        "Unearthed_Email",
    )

    for field in legacy_fields:
        if field not in df.columns:
            continue
        df[field] = df[field].fillna("")

    if "Email_All" not in df.columns:
        df["Email_All"] = ""
    if "Email" not in df.columns:
        df["Email"] = ""

    def _build_email_all(row: pd.Series) -> str:
        if _is_quarantined(row):
            return _cell_str(row.get("Email_All"))
        collected: List[str] = []
        for field in legacy_fields:
            if field in row:
                collected.extend(normalize_emails(row.get(field, "")))
        ranked = _rank_contact_emails(collected)
        return emails_to_string(ranked)

    df["Email_All"] = df.apply(_build_email_all, axis=1)
    try:
        for idx in range(len(df.index)):
            if _is_quarantined(df.loc[idx]):
                continue
            merged_str = _set_email_all(df, idx, df.at[idx, "Email_All"], source="consolidate", logger=_LOGGER.info)
            primary_email, ranked = _select_primary_email_for_row(df.loc[idx], df.at[idx, "Email"], merged_str)
            df.at[idx, "Email"] = primary_email if ranked else ""
            _align_row_email_provenance(df, idx, primary_email)
    except Exception:
        pass
    return df


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
    Emit the message once: prefer the provided logger; otherwise fall back to stdout.
    Avoid double-emitting the same message.
    """
    if not message:
        return

    try:
        if logger:
            if callable(logger):  # bound logger method such as logger.info
                logger(message)
                return
            if hasattr(logger, "info") and callable(getattr(logger, "info", None)):
                logger.info(message)
                return
    except Exception:
        pass

    try:
        print(message)
    except Exception:
        pass


def _ensure_string_columns(df: pd.DataFrame, cols: Iterable[str]) -> None:
    """
    Cast selected columns to pandas string dtype to avoid FutureWarning when assigning
    empty strings into numeric columns.
    """
    for col in cols:
        if col in df.columns:
            try:
                df[col] = df[col].astype("string")
            except Exception:
                df[col] = df[col].astype(object)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _estimate_min_header_bytes(columns: Sequence[str]) -> Optional[int]:
    """Best-effort minimum byte count for a CSV header line using utf-8-sig."""
    try:
        header = ",".join([str(c) for c in columns]) if columns is not None else ""
        header_line = f"{header}\n"
        return len(header_line.encode("utf-8-sig"))
    except Exception:
        return None


def _fsync_path(path: Path) -> None:
    """Best-effort fsync to ensure contents hit disk before replace."""
    try:
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass


def _safe_atomic_write_csv(df, path: str, fallback_columns: List[str], reason: str = "") -> AtomicCSVResult:
    """
    Guard against pandas emitting a lone newline when DataFrame has zero columns.
    Ensures headers exist, writes to <final>.tmp in the same directory, fsyncs, verifies,
    then atomically replaces the final path.
    """
    if df is None:
        df = pd.DataFrame(columns=fallback_columns)
    elif not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)

    columns = getattr(df, "columns", None)
    columns_count = len(columns) if columns is not None else 0
    if columns_count == 0:
        df = pd.DataFrame(columns=fallback_columns)

    final_path = Path(path)
    tmp_path = _derive_tmp_csv_path(final_path)
    _ensure_parent(str(tmp_path))
    keep_tmp = _should_keep_tmp_on_failure()
    row_count = len(df.index)
    header_min_bytes = _estimate_min_header_bytes(list(df.columns))

    _remove_if_exists(tmp_path)
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8-sig") as handle:
            df.to_csv(handle, index=False)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass

        tmp_bytes = tmp_path.stat().st_size if tmp_path.exists() else 0
        if tmp_bytes <= 0 or (header_min_bytes is not None and tmp_bytes < header_min_bytes):
            raise IOError(f"Temp CSV invalid (bytes={tmp_bytes}, header_min={header_min_bytes})")

        _fsync_path(tmp_path)
        os.replace(tmp_path, final_path)

        if not final_path.exists():
            raise IOError("Atomic replace completed but final CSV missing")

        final_bytes = final_path.stat().st_size if final_path.exists() else 0
        if final_bytes <= 0 or (header_min_bytes is not None and final_bytes < header_min_bytes):
            _LOGGER.error(
                "[CSV WRITE] Final verification failed rows=%s bytes=%s header_min=%s path=%s",
                row_count,
                final_bytes,
                header_min_bytes,
                final_path,
            )
            _remove_if_exists(final_path)
            _remove_if_exists(tmp_path)
            raise IOError("Final CSV verification failed")

        _LOGGER.info(
            "[CSV WRITE]%s rows=%s bytes=%s path=%s",
            f" {reason}" if reason else "",
            row_count,
            final_bytes,
            final_path,
        )
        return AtomicCSVResult(final_path=final_path, tmp_path=tmp_path, row_count=row_count, raw_bytes=int(final_bytes))
    except Exception:
        if tmp_path.exists() and not keep_tmp:
            _remove_if_exists(tmp_path)
        raise


def _derive_tmp_csv_path(final_path: Union[str, Path]) -> Path:
    target = Path(final_path)
    if target.name == "raw.csv":
        return target.with_name("raw.tmp.csv")
    return target.with_name(f"{target.stem}.tmp{target.suffix}")


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        pass


def _cleanup_leftover_tmp(tmp_path: Path, job_label: str) -> None:
    if not tmp_path.exists():
        return
    try:
        tmp_path.unlink()
        _LOGGER.warning("[CSV CLEANUP] Leftover tmp CSV removed for job=%s path=%s", job_label, tmp_path)
    except Exception as exc:
        _LOGGER.warning(
            "[CSV CLEANUP] Leftover tmp CSV present for job=%s path=%s (cleanup failed: %s)",
            job_label,
            tmp_path,
            exc,
        )


def _read_csv_header_columns(tmp_path: Path) -> List[str]:
    """Return header columns from a CSV by reading only the first row."""
    last_exc: Optional[Exception] = None
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with open(tmp_path, "r", newline="", encoding=enc) as handle:
                reader = csv.reader(handle)
                for row in reader:
                    return row
                return []
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
        except Exception as exc:
            last_exc = exc
            break
    if last_exc:
        raise last_exc
    return []


def _count_csv_data_rows(tmp_path: Path) -> int:
    """Count data rows (excluding header) via streaming csv.reader."""
    last_exc: Optional[Exception] = None
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with open(tmp_path, "r", newline="", encoding=enc) as handle:
                reader = csv.reader(handle)
                try:
                    next(reader)
                except StopIteration:
                    return 0
                row_count = 0
                for _ in reader:
                    row_count += 1
                return row_count
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
        except Exception as exc:
            last_exc = exc
            break
    if last_exc:
        raise last_exc
    return 0


def _finalize_tmp_csv(tmp_path: Path, final_path: Path) -> AtomicCSVResult:
    """
    Validate and atomically promote tmp CSV to final_path using lightweight streaming.
    Returns AtomicCSVResult describing the finalised file.
    """
    if not tmp_path.exists():
        raise FileNotFoundError(f"Expected temp CSV missing: {tmp_path}")

    keep_tmp = _should_keep_tmp_on_failure()

    try:
        columns = _read_csv_header_columns(tmp_path)
        if not columns:
            raise ValueError("Seed job produced CSV with no columns")

        row_count = _count_csv_data_rows(tmp_path)
        if row_count == 0:
            # ensure header-only
            with open(tmp_path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError:
                    pass

        header_min_bytes = _estimate_min_header_bytes(columns)
        tmp_bytes = tmp_path.stat().st_size if tmp_path.exists() else 0
        if tmp_bytes <= 0 or (header_min_bytes is not None and tmp_bytes < header_min_bytes):
            raise IOError(f"Temp CSV invalid (bytes={tmp_bytes}, header_min={header_min_bytes})")

        _fsync_path(tmp_path)
        os.replace(tmp_path, final_path)

        final_bytes = final_path.stat().st_size if final_path.exists() else 0
        if final_bytes <= 0 or (header_min_bytes is not None and final_bytes < header_min_bytes):
            _LOGGER.error(
                "[CSV FINALIZE] Verification failed rows=%s bytes=%s header_min=%s path=%s",
                row_count,
                final_bytes,
                header_min_bytes,
                final_path,
            )
            _remove_if_exists(final_path)
            _remove_if_exists(tmp_path)
            raise IOError("Final CSV verification failed")

        _LOGGER.info(
            "[CSV FINALIZE] promoted tmp rows=%s bytes=%s path=%s",
            row_count,
            final_bytes,
            final_path,
        )
        return AtomicCSVResult(final_path=final_path, tmp_path=tmp_path, row_count=int(row_count), raw_bytes=int(final_bytes))
    except Exception:
        if tmp_path.exists() and not keep_tmp:
            _remove_if_exists(tmp_path)
        raise


def ensure_final_raw_csv(raw_output_path: Union[str, Path], job_label: str = "", logger: LoggerFn = None) -> Optional[AtomicCSVResult]:
    """
    Promote a per-job temp CSV (raw.tmp.csv) to raw.csv when it is present.

    This is a lightweight guard for resumed jobs or orchestrators that may
    skip the normal finalize path. Returns the AtomicCSVResult when a tmp file
    was promoted; otherwise returns None.
    """
    final_path = Path(raw_output_path)
    tmp_path = _derive_tmp_csv_path(final_path)

    if not tmp_path.exists():
        return None

    try:
        job_label_lc = (job_label or final_path.parent.name or "").lower()
        if "unearthed" in job_label_lc:
            _dedupe_unearthed_csv(tmp_path.as_posix(), logger=logger)
        res = _finalize_tmp_csv(tmp_path, final_path)
        _safe_log(
            logger,
            f"[CSV FINALIZE] job={job_label or final_path.parent.name} promoted {tmp_path.name} -> {final_path.name}",
        )
        return res
    except Exception as exc:  # pragma: no cover - surfaced to caller
        _safe_log(logger, f"[CSV FINALIZE] job={job_label or final_path.parent.name} failed to promote tmp: {exc}")
        raise


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


def _facebook_about_url(raw_url: str) -> str:
    """Return a best-effort Facebook About URL for a profile/page link."""
    url = _cell_str(raw_url).strip()
    if not url:
        return ""
    # Drop hash/query noise and normalize trailing slash before appending.
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if url.lower().endswith("/about"):
        return url
    return f"{url}/about"


def _fill_email_provenance_fields(
    df: pd.DataFrame,
    idx: int,
    source: Optional[MutableMapping[str, Any]] = None,
    fb_url_hint: str = "",
    default_source_type: str = "facebook_enrich",
    default_method: str = "regex",
) -> None:
    """
    Backfill provenance columns on df[idx] using the enrichment payload.

    - Only populates when Email is present.
    - Prefers explicit payload values; falls back to the About page for a FB URL.
    - Does not overwrite non-empty existing cells (minimal disturbance).
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return
    try:
        if idx not in df.index:
            return
    except Exception:
        return

    email_val = _cell_str(df.at[idx, "Email"] if "Email" in df.columns else "")
    if not email_val:
        return

    def _get_from_source(key: str) -> str:
        if source is None:
            return ""
        try:
            return _cell_str(source.get(key))  # type: ignore[attr-defined]
        except Exception:
            return ""

    source_url = _get_from_source("Email_Source_URL")
    source_type = _get_from_source("Email_Source_Type")
    method = _get_from_source("Email_Extract_Method")

    fb_url = fb_url_hint or _get_from_source("Facebook_URL")
    if not fb_url and "Facebook_URL" in df.columns:
        fb_url = _cell_str(df.at[idx, "Facebook_URL"])

    if not source_url:
        source_url = _facebook_about_url(fb_url)

    source_type = source_type or default_source_type
    method = method or default_method

    merge_email_provenance_into_target(
        (df, idx),
        [email_val],
        source_url=source_url,
        source_type=source_type,
        method=method,
        surface=_facebook_email_surface_hint(source) if source_type.startswith("facebook") else "",
    )

    if source_url:
        if "Email_Source_URL" not in df.columns:
            df["Email_Source_URL"] = ""
        if not _cell_str(df.at[idx, "Email_Source_URL"]):
            df.at[idx, "Email_Source_URL"] = source_url

    if source_type:
        if "Email_Source_Type" not in df.columns:
            df["Email_Source_Type"] = ""
        if not _cell_str(df.at[idx, "Email_Source_Type"]):
            df.at[idx, "Email_Source_Type"] = source_type

    if method:
        if "Email_Extract_Method" not in df.columns:
            df["Email_Extract_Method"] = ""
        if not _cell_str(df.at[idx, "Email_Extract_Method"]):
            df.at[idx, "Email_Extract_Method"] = method



def _read_seed_list(seed_path: Optional[str]) -> List[str]:
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


def _coalesce_emails(df: pd.DataFrame) -> pd.DataFrame:
    """
    Preserve any existing directory-provided emails by backfilling across known columns and syncing Email/Email_All.
    """
    if df is None or df.empty:
        return df
    existing = [c for c in EMAIL_PRIORITY_COLS if c in df.columns]
    if not existing:
        return df
    # Make sure Email_All includes Email where Email_All is blank.
    if "Email_All" in df.columns and "Email" in df.columns:
        email_all = df["Email_All"].fillna("").astype(str)
        email_col = df["Email"].fillna("").astype(str)
        mask_all = email_all.str.strip() == ""
        df.loc[mask_all, "Email_All"] = email_col[mask_all]

    # Use object dtype during bfill to avoid pandas string-array backfill bug
    # that can smear a single email across all rows.
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


def _promote_fb_urls_df(
    df: pd.DataFrame,
    logger: LoggerFn = None,
    *,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> pd.DataFrame:
    """Promote Facebook links from generic link fields into facebook_url/Facebook_URL."""
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
        new_url, source = ensure_canonical_facebook_url(df.loc[idx], set_row=False, share_resolver=share_resolver)
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
        _safe_log(logger, f"[FB Promotion] facebook_url populated for {populated} rows")
    if logger and canonical_from_alias:
        _safe_log(logger, f"[FB Promotion] canonical Facebook_URL backfilled from alias fields for {canonical_from_alias} rows")
    if logger and canonical_from_links:
        _safe_log(logger, f"[FB Promotion] canonical Facebook_URL backfilled from Social Link / External Links for {canonical_from_links} rows")
    return df


def _build_night_fb_share_promotion_resolver(
    *,
    fb_username: str,
    fb_password: str,
    night_fb_run_state: Optional[NightFBRunState],
    logger: LoggerFn = None,
) -> Callable[[str], Optional[str]]:
    helper: Optional[NightModeFacebookEnricher] = None
    authed_session_available: Optional[bool] = None

    def _resolve(candidate: str) -> Optional[str]:
        nonlocal helper, authed_session_available
        raw_candidate = str(candidate or "").strip()
        if not raw_candidate:
            return None
        if helper is None:
            helper = NightModeFacebookEnricher(
                _load_legacy_module(),
                fb_username,
                fb_password,
                logger=lambda msg: _safe_log_console(logger, msg),
                use_shared_session=True,
                run_state=night_fb_run_state,
            )
        if authed_session_available is None:
            authed_session_available = helper._has_authenticated_session()
        if not authed_session_available:
            return None
        resolved_url = helper._resolve_pass_a_explicit_scrape_url(
            raw_candidate,
            authed_session_available=True,
        )
        canonical = canonicalize_facebook_url(resolved_url)
        return canonical or None

    return _resolve


def _maybe_set_email(df: pd.DataFrame, idx: int, new_email: Optional[str]) -> None:
    """
    Only set Email when a non-empty value is available and the existing cell is empty.
    """
    if "Email" not in df.columns:
        df["Email"] = ""
    new_clean = (new_email or "").strip()
    if not new_clean:
        return
    current = df.at[idx, "Email"] if idx in df.index else ""
    if pd.isna(current):
        current = ""
    if str(current or "").strip():
        return
    df.at[idx, "Email"] = new_clean


RAW_FALLBACK_COLUMNS: List[str] = [
    "Artist Name",
    "Location",
    "Song Title",
    "Sounds Like",
    "Social Link",
    "SoundCloud Link",
    "Release Date",
    "Primary Genre",
    "Date Added",
    "External Links",
    "Email",
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
    EMAIL_PROVENANCE_JSON_COL,
    "Source Directory",
]


def _write_rows_to_csv(rows: Iterable[Any], path: str, source_directory: str = "") -> AtomicCSVResult:
    _ensure_parent(path)
    materialized: List[Any] = list(rows or [])
    fallback_cols = RAW_FALLBACK_COLUMNS.copy()
    if not materialized:
        df = pd.DataFrame(columns=fallback_cols)
        if source_directory:
            df["Source Directory"] = source_directory
        return _safe_atomic_write_csv(df, path, fallback_cols, reason=f"job={source_directory or 'unknown'}")
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
    fallback_cols = list(df.columns) if len(getattr(df, "columns", [])) else fallback_cols
    return _safe_atomic_write_csv(df, path, fallback_cols, reason=f"job={source_directory or 'unknown'}")


FINAL_EXPORT_COLUMNS: Sequence[str] = [
    "Artist Name",
    "Location",
    "Country_Derived",
    "Song Title",
    "Primary Genre",
    "Unearthed_Genre_Raw",
    "Social Link",
    "SoundCloud Link",
    "Spotify_URL",
    "External Links",
    "Primary Email",
    "All Emails",
    "Email Source",
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
    "Contact_Mode",
    "Discovery Source",
    "Source Directory",
    "Source URL",
    "Review_Urls",
    "Played on triple J",
    "Played on Unearthed",
    "Release Date",
    "Date Added",
    "final_status",
    "Needs_Review",
    "FB_Review_Reason",
    FB_OPPORTUNITY_STATE_COL,
    FB_GATE_STATE_COL,
    FB_ATTEMPT_STATE_COL,
    FB_WRITE_STATE_COL,
    FB_DEBUG_REASON_COL,
]

WOODPECKER_EXPORT_COLUMNS: Sequence[str] = [
    "Artist Name",
    "Primary Email",
    "All Emails",
    "Email Source",
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
    "Contact_Mode",
    "Location",
    "Country_Derived",
    "Song Title",
    "Primary Genre",
    "Unearthed_Genre_Raw",
    "Social Link",
    "SoundCloud Link",
    "Spotify_URL",
    "External Links",
    "Discovery Source",
    "Source Directory",
    "Source URL",
    "Review_Urls",
    "Played on triple J",
    "Played on Unearthed",
    "Release Date",
    "Date Added",
    "final_status",
    "Needs_Review",
    "FB_Review_Reason",
]

_AU_STATE_TOKENS = ("nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt")
_FINAL_STATUS_KEEP = {"OK", "WARN", "BLOCK"}
_COUNTRY_ALIASES = [
    (("united kingdom", "uk", "u.k.", "u.k", "england", "scotland", "wales", "northern ireland"), "United Kingdom"),
    (("united states", "usa", "u.s.a", "u.s.a.", "us", "u.s.", "america"), "United States"),
    (("australia", "aus"), "Australia"),
    (("canada", "can."), "Canada"),
]


def _safe_parse_date(value):
    """Parse dates while avoiding pandas ISO+dayfirst warning; keeps AU-style parsing intact."""
    if isinstance(value, str):
        text = value.strip()
        if ISO_DATE_RE.match(text):
            return pd.to_datetime(text, format="%Y-%m-%d", dayfirst=False, errors="coerce")
    return pd.to_datetime(value, dayfirst=True, errors="coerce")


def _normalize_date_string(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _safe_parse_date(text)
    if pd.isna(parsed):
        return ""
    try:
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return ""


def normalize_country_from_location(location_raw: str) -> str:
    """
    Canonicalise country names based on the Location text.
    - Case-insensitive
    - Splits on commas/slashes and prefers the first matching token
    """
    if not location_raw:
        return ""

    loc = str(location_raw or "").strip().lower()
    if not loc:
        return ""

    tokens = [t.strip() for t in re.split(r"[,/]+", loc) if t.strip()]

    for token in tokens:
        for aliases, canonical in _COUNTRY_ALIASES:
            if any(alias in token for alias in aliases):
                return canonical

    for aliases, canonical in _COUNTRY_ALIASES:
        if any(alias in loc for alias in aliases):
            return canonical

    if any(token in loc for token in _AU_STATE_TOKENS):
        return "Australia"

    return ""


def _derive_primary_email(email: str, email_all: str, row_like: Any = None) -> str:
    _, ranked = _select_primary_email_for_row(row_like, email, email_all)
    return ranked[0] if ranked else ""


def _derive_all_emails(email: str, email_all: str, row_like: Any = None) -> str:
    ranked = _rank_contact_emails_for_row(row_like, [email_all, email])
    return ";".join(ranked)


def _derive_contact_mode(primary_email: str, social_link: str) -> str:
    has_email = bool(primary_email.strip())
    has_social = bool((social_link or "").strip())
    if has_email and has_social:
        return "email+dm"
    if has_email:
        return "email_only"
    if has_social:
        return "dm_only"
    return "unknown"


def _derive_discovery_source(spotify_playlist: str, source_directory: str) -> str:
    playlist = (spotify_playlist or "").strip()
    source_dir = (source_directory or "").strip()
    if playlist and source_dir:
        return f"{playlist} ({source_dir})"
    if playlist:
        return playlist
    if source_dir:
        return source_dir
    return ""


def infer_discovery_source(row: pd.Series) -> str:
    playlist = str(row.get("Spotify Playlist") or "").strip()
    src_dir = str(row.get("Source Directory") or "").strip()
    raw_job = str(row.get("__source_job") or "").strip()

    lower_src = src_dir.lower()
    lower_job = raw_job.lower()
    label = ""

    if playlist:
        label = f"{playlist} ({src_dir})" if lower_src else playlist

    if not label:
        source_token = lower_src or lower_job
        if "unearthed" in source_token:
            label = "Triple J Unearthed"
        elif "soundcloud" in source_token:
            label = "SoundCloud directory"
        elif "bandcamp" in source_token:
            label = "Bandcamp directory"
        elif "spotify" in source_token:
            label = "Spotify directory"
        else:
            label = src_dir or raw_job or ""

    return label


def infer_email_source(row: pd.Series) -> str:
    email = str(row.get("Email") or row.get("Primary Email") or "").strip()
    if not email:
        return ""

    email_type = str(row.get("Email_Type") or "").lower()
    email_source_type = str(row.get("Email_Source_Type") or "").lower()
    fb_status = str(row.get("FB_Status") or "").lower()
    src_dir = str(row.get("Source Directory") or "").lower()
    src_url = str(row.get("Source URL") or "").lower()

    if email_type == "fb_night" or fb_status.startswith("ok") or email_source_type == "facebook_enrich":
        return "Facebook About"

    if email_type == "website_enrich" or email_source_type == "website_enrich":
        return "Website"

    if "unearthed" in src_dir or "unearthed" in src_url:
        return "Triple J Unearthed profile"

    if "soundcloud" in src_dir or "soundcloud.com" in src_url:
        return "SoundCloud profile"

    if "bandcamp" in src_dir or "bandcamp.com" in src_url:
        return "Bandcamp page"

    if "spotify" in src_dir or "spotify.com" in src_url:
        return "Spotify / linked website"

    if email and not email_type and not fb_status:
        return "Seed directory (site/email scrape)"

    return "Unknown"


def _selected_email_provenance(row_like: Any, selected_email: str) -> Dict[str, str]:
    if not selected_email:
        return {}
    return get_email_provenance_entry(row_like, selected_email)


def _facebook_email_surface_hint(source: Any) -> str:
    if source is None or not hasattr(source, "get"):
        return "facebook_main"
    surface_raw = _cell_str(source.get("FB_Email_Source") or source.get("email_source")).lower()
    if surface_raw == "about":
        return "facebook_about"
    if surface_raw == "main":
        return "facebook_main"
    source_url = _cell_str(source.get("Email_Source_URL") or source.get("email_source_url") or source.get("Facebook_URL"))
    return "facebook_about" if "/about" in source_url.lower() else "facebook_main"


def infer_country_from_context(row: pd.Series) -> str:
    """
    Best-effort country inference when Country_Derived is missing.
    Uses playlist / directory hints only and never overwrites non-empty values.
    """
    existing = str(row.get("Country_Derived") or "").strip()
    if existing:
        return existing

    playlist = str(row.get("Spotify Playlist") or "").lower()
    src_dir = str(row.get("Source Directory") or "").lower()

    if "uk & ie" in playlist or "uk/ie" in playlist or "uk & ireland" in playlist:
        return "United Kingdom"
    if "australia" in playlist or "au & nz" in playlist or "au/nz" in playlist:
        return "Australia"
    if "canada" in playlist:
        return "Canada"
    if "usa" in playlist or "us " in playlist or "united states" in playlist:
        return "United States"
    if "brazil" in playlist:
        return "Brazil"
    if "mexico" in playlist:
        return "Mexico"

    if "unearthed" in src_dir:
        return "Australia"

    return existing


def infer_location_for_export(row: pd.Series) -> str:
    """
    Prefer existing Location, then Country_Derived, then country from context.
    """
    loc = str(row.get("Location") or "").strip()
    if loc:
        return loc

    country = str(row.get("Country_Derived") or "").strip()
    if not country:
        country = infer_country_from_context(row)

    return country


def _export_email_looks_suspicious(email: str) -> bool:
    """Conservative export-time guard for obvious system/ingestion destinations."""
    email_norm = str(email or "").strip().lower()
    if not _is_valid_email_shape(email_norm):
        return True

    local, domain = email_norm.split("@", 1)
    compact_local = re.sub(r"[^a-z0-9]+", "", local)
    local_tokens = {token for token in re.split(r"[^a-z0-9]+", local) if token}
    domain_tokens = {token for token in re.split(r"[^a-z0-9]+", domain) if token}

    if "noreply" in compact_local or "donotreply" in compact_local:
        return True
    if {"ingest", "sentry", "system"} & local_tokens:
        return True
    if {"sentry", "ingest", "mailgun", "sendgrid", "postmark", "amazonses"} & domain_tokens:
        return True
    return False


def _compute_export_needs_review(row: pd.Series, primary_email: str, email_source: str) -> bool:
    """Apply conservative review policy at final export time only."""
    status_normalized = str(row.get("_status_normalized", "") or "").strip().upper()
    existing_needs_review = str(row.get("Needs_Review", "") or "").strip().upper() == "TRUE"
    email_source_url = str(row.get("Email_Source_URL", "") or "").strip()
    email_source_type = str(row.get("Email_Source_Type", "") or "").strip().lower()
    review_urls = str(row.get("Review_Urls", "") or row.get("Review Urls", "") or "").strip()
    fb_review_reason = str(row.get("FB_Review_Reason", "") or "").strip()
    safe_explicit_source = email_source_type in {"facebook_enrich", "website_enrich"} or email_source in {
        "Facebook About",
        "Website",
    }

    if existing_needs_review:
        return True
    if status_normalized == "BLOCK":
        return True
    if status_normalized == "WARN":
        return not safe_explicit_source
    if not _is_valid_email_shape(primary_email):
        return True
    if not email_source_url:
        return True
    if review_urls or fb_review_reason:
        return True
    if _export_email_looks_suspicious(primary_email):
        return True

    # Domain-reuse rows are not exposed with a stable export-time label yet, so only
    # auto-approve explicit FB/website provenance and keep ambiguous sources under review.
    if status_normalized == "OK" and safe_explicit_source:
        return False
    return True


def _build_final_export_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build the client-facing final export view without mutating the input frame.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=FINAL_EXPORT_COLUMNS)

    work = df.copy()
    status_series = work.get("final_status", pd.Series("", index=work.index)).astype(str).str.strip().str.upper()
    filtered = work.loc[status_series.isin(_FINAL_STATUS_KEEP)].copy()
    if filtered.empty:
        return pd.DataFrame(columns=FINAL_EXPORT_COLUMNS)

    filtered["_status_normalized"] = status_series.loc[filtered.index]

    rows: List[Dict[str, str]] = []
    for _, row in filtered.iterrows():
        location = str(row.get("Location", "") or "").strip()
        existing_country = str(row.get("Country_Derived", "") or "").strip()
        country = existing_country or normalize_country_from_location(location)

        email = str(row.get("Email", "") or "")
        email_all = str(row.get("Email_All", "") or "")

        final_status = str(row.get("final_status", "") or "").strip()
        email_source_url_val = str(row.get("Email_Source_URL", "") or "").strip()

        # Defensive guard: strip FB-applied emails when FB_Status signals rejection.
        fb_status_val = str(row.get("FB_Status", "") or "")
        fb_applied_raw = str(row.get("__fb_emails_applied", "") or "")
        if _fb_status_is_rejected(fb_status_val) and fb_applied_raw:
            fb_emails = set(normalize_emails(fb_applied_raw))
            if fb_emails:
                email_list = [e for e in normalize_emails(email) if e not in fb_emails]
                email_all_list = [e for e in normalize_emails(email_all) if e not in fb_emails]
                email = ";".join(email_list)
                email_all = ";".join(email_all_list)
        primary_email = _derive_primary_email(email, email_all, row)
        all_emails = _derive_all_emails(email, email_all, row)
        selected_meta = _selected_email_provenance(row, primary_email)
        if selected_meta:
            email_source_url_val = _cell_str(selected_meta.get("source_url", "")) or email_source_url_val

        social_link = str(row.get("Social Link", "") or "")
        contact_mode = _derive_contact_mode(primary_email, social_link)

        spotify_playlist = str(row.get("Spotify Playlist", "") or "")
        source_directory = str(row.get("Source Directory", "") or "")
        discovery_source = infer_discovery_source(row)

        release_date_norm = _normalize_date_string(row.get("Release Date", ""))
        date_added_norm = _normalize_date_string(row.get("Date Added", ""))

        row_for_email_source = row.copy()
        row_for_email_source["Primary Email"] = primary_email
        if selected_meta:
            row_for_email_source["Email_Source_URL"] = _cell_str(selected_meta.get("source_url", ""))
            row_for_email_source["Email_Source_Type"] = _cell_str(selected_meta.get("source_type", ""))
            row_for_email_source["Email_Extract_Method"] = _cell_str(selected_meta.get("extract_method", ""))
        email_source = infer_email_source(row_for_email_source)
        needs_review = _compute_export_needs_review(row_for_email_source, primary_email, email_source)

        external_links = str(row.get("External Links", "") or "").strip()
        review_urls = str(row.get("Review_Urls", "") or row.get("Review Urls", "") or "").strip()
        fb_review_reason = str(row.get("FB_Review_Reason", "") or "").strip()

        rows.append(
            {
                "Artist Name": str(row.get("Artist Name", "") or "").strip(),
                "Location": location,
                "Country_Derived": country,
                "Song Title": str(row.get("Song Title", "") or "").strip(),
                "Primary Genre": str(row.get("Primary Genre", "") or "").strip(),
                "Unearthed_Genre_Raw": str(row.get("Unearthed_Genre_Raw", "") or "").strip(),
                "Social Link": social_link.strip(),
                "SoundCloud Link": str(row.get("SoundCloud Link", "") or "").strip(),
                "Spotify_URL": str(row.get("Spotify_URL", "") or "").strip(),
                "External Links": external_links,
                "Primary Email": primary_email,
                "All Emails": all_emails,
                "Email Source": email_source,
                "Email_Source_URL": email_source_url_val,
                "Email_Source_Type": _cell_str(row_for_email_source.get("Email_Source_Type", "")),
                "Email_Extract_Method": _cell_str(row_for_email_source.get("Email_Extract_Method", "")),
                "Contact_Mode": contact_mode,
                "Discovery Source": discovery_source,
                "Source Directory": source_directory,
                "Source URL": str(row.get("Source URL", "") or "").strip(),
                "Review_Urls": review_urls,
                "Played on triple J": str(row.get("Played on triple J", "") or "").strip(),
                "Played on Unearthed": str(row.get("Played on Unearthed", "") or "").strip(),
                "Release Date": release_date_norm,
                "Date Added": date_added_norm,
                "final_status": final_status,
                "Needs_Review": "TRUE" if needs_review else "FALSE",
                "FB_Review_Reason": fb_review_reason,
                FB_OPPORTUNITY_STATE_COL: str(row.get(FB_OPPORTUNITY_STATE_COL, "") or "").strip(),
                FB_GATE_STATE_COL: str(row.get(FB_GATE_STATE_COL, "") or "").strip(),
                FB_ATTEMPT_STATE_COL: str(row.get(FB_ATTEMPT_STATE_COL, "") or "").strip(),
                FB_WRITE_STATE_COL: str(row.get(FB_WRITE_STATE_COL, "") or "").strip(),
                FB_DEBUG_REASON_COL: str(row.get(FB_DEBUG_REASON_COL, "") or "").strip(),
            }
        )

    df_out = pd.DataFrame(rows, columns=FINAL_EXPORT_COLUMNS)

    # Safety gate: only flag repeated seed-directory emails (do not blank them).
    if not df_out.empty and "Primary Email" in df_out.columns and "Email Source" in df_out.columns:
        email_series = df_out["Primary Email"].fillna("").astype(str).str.strip()
        email_source_series = df_out["Email Source"].fillna("").astype(str).str.lower()
        seed_mask = email_source_series.str.startswith("seed directory")
        seed_emails = email_series.where(seed_mask)
        email_counts = seed_emails.str.lower().value_counts()
        flagged_emails = [email for email, count in email_counts.items() if email and count >= 5]
        if flagged_emails:
            mask = seed_mask & email_series.str.lower().isin(flagged_emails)
            df_out.loc[mask, "Needs_Review"] = "TRUE"
            df_out.loc[mask, "Email Source"] = "Seed directory (repeat email)"
            try:
                unique_artists = sorted(set(df_out.loc[mask, "Artist Name"]))
                print(
                    f"[Final Export] Safety gate: repeated seed-directory emails {flagged_emails} "
                    f"across {len(unique_artists)} artists; marked Needs_Review."
                )
            except Exception:
                pass

    return df_out
def _merge_festival_expansion_output(
    main_output_csv_path: str,
    expansion_output_csv_path: str,
    normalise_artist_name_fn: Callable[[str], str],
    logger: LoggerFn = None,
) -> int:
    if not os.path.exists(main_output_csv_path) or not os.path.exists(expansion_output_csv_path):
        return 0
    main_df = pd.read_csv(main_output_csv_path, dtype=str, keep_default_na=False).fillna("")
    expansion_df = pd.read_csv(expansion_output_csv_path, dtype=str, keep_default_na=False).fillna("")
    if expansion_df.empty or "Artist Name" not in expansion_df.columns:
        return 0

    existing_keys = {
        normalise_artist_name_fn(str(name or ""))
        for name in main_df.get("Artist Name", pd.Series(dtype=str)).tolist()
        if normalise_artist_name_fn(str(name or ""))
    }
    staged_keys: Set[str] = set()
    kept_rows: List[Dict[str, Any]] = []
    for _, row in expansion_df.iterrows():
        artist_name = str(row.get("Artist Name", "") or "").strip()
        artist_key = normalise_artist_name_fn(artist_name)
        if not artist_key or artist_key in existing_keys or artist_key in staged_keys:
            continue
        kept_rows.append(row.to_dict())
        staged_keys.add(artist_key)

    if not kept_rows:
        return 0

    kept_df = pd.DataFrame(kept_rows)
    merged_df = pd.concat([main_df, kept_df], ignore_index=True, sort=False)
    merged_df.to_csv(main_output_csv_path, index=False, encoding="utf-8")
    _safe_log(logger, f"[FestivalExpansion] merged enriched expansion rows={len(kept_rows)}")
    return len(kept_rows)


def run_master_enrichment(
    seed_csv_path: str,
    output_csv_path: str,
    logger: LoggerFn = None,
    enable_live_search: bool = True,
    max_live_searches: Optional[int] = None,
    night_mode: bool = False,
    bandcamp_csv_path: Optional[str] = None,
    night_fb_run_state: Optional[NightFBRunState] = None,
) -> str:
    """
    Run the cross-directory enricher on a single combined CSV.

    This wraps the existing cross_directory_enricher logic used by the standalone tool.
    """
    _safe_log(logger, f"[Master Enrich] Starting cross-directory enrichment for {seed_csv_path}")
    local_night_fb_run_state = False
    if night_mode and night_fb_run_state is None:
        night_fb_run_state = create_night_fb_run_state(
            os.environ.get("FB_USERNAME", "").strip(),
            os.environ.get("FB_PASSWORD", "").strip(),
        )
        local_night_fb_run_state = True
    try:
        import cross_directory_enricher
    except Exception as exc:
        _safe_log(logger, f"[Master Enrich] cross_directory_enricher unavailable: {exc}")
        shutil.copyfile(seed_csv_path, output_csv_path)
        return output_csv_path

    try:
        bandcamp_path_final = bandcamp_csv_path or ""
        soundcloud_path_final = ""
        lastfm_path_final = ""
        unearthed_path_final = ""
        run_dir = Path(output_csv_path).resolve().parent
        yield_tracker = cross_directory_enricher.EnrichmentYieldTracker()
        try:
            DETECT_RETRIES = 6
            DETECT_SLEEP_S = 1.0

            def _select_best(paths: List[Path]) -> Tuple[Optional[Path], int]:
                best = None
                best_rows_local = -1
                for candidate in sorted(paths):
                    rows_local = -1
                    try:
                        with candidate.open("r", encoding="utf-8", errors="ignore") as handle:
                            line_count = sum(1 for _ in handle)
                        rows_local = max(0, line_count - 1)
                    except Exception:
                        rows_local = -1
                    if best is None or rows_local > best_rows_local:
                        best_rows_local = rows_local
                        best = candidate
                return best, best_rows_local

            def _detect_directory_csv(
                directory_name: str,
                *,
                label: str,
                explicit_path: str = "",
                enriched_filenames: Optional[Tuple[str, ...]] = None,
            ) -> str:
                if explicit_path:
                    _safe_log(logger, f"[Master] {label} directory CSV -> {explicit_path} (explicit)")
                    return explicit_path

                chosen_path: Optional[Path] = None
                selected_kind: Optional[str] = None
                best_rows: int = -1
                attempts_used: int = 0
                enriched_names = enriched_filenames or ()
                job_glob = f"job_{directory_name}_*"

                for attempt in range(DETECT_RETRIES):
                    attempts_used = attempt + 1
                    enriched: List[Path] = []
                    for filename in enriched_names:
                        enriched.extend(run_dir.glob(f"{job_glob}/{filename}"))
                    raw = list(run_dir.glob(f"{job_glob}/raw.csv"))

                    if enriched:
                        chosen_path, best_rows = _select_best(enriched)
                        selected_kind = "enriched"
                    elif raw:
                        chosen_path, best_rows = _select_best(raw)
                        selected_kind = "raw"
                    else:
                        chosen_path = None
                        selected_kind = None
                        best_rows = -1

                    if chosen_path is not None:
                        break
                    if attempt < DETECT_RETRIES - 1:
                        time.sleep(DETECT_SLEEP_S)

                if chosen_path is not None:
                    detected_path = chosen_path.as_posix()
                    rows_text = best_rows if best_rows >= 0 else "?"
                    attempts_text = f", attempts={attempts_used}" if attempts_used > 1 else ""
                    _safe_log(
                        logger,
                        f"[Master] {label} directory CSV -> {detected_path} (rows={rows_text}, kind={selected_kind}{attempts_text})",
                    )
                    return detected_path

                _safe_log(logger, f"[Master] {label} directory CSV -> (none) in {run_dir} (attempts={DETECT_RETRIES})")
                return ""

            bandcamp_path_final = _detect_directory_csv(
                "bandcamp",
                label="Bandcamp",
                explicit_path=bandcamp_path_final,
                enriched_filenames=("bandcamp_enriched.csv",),
            )
            soundcloud_path_final = _detect_directory_csv(
                "soundcloud",
                label="SoundCloud",
                enriched_filenames=("soundcloud_enriched.csv",),
            )
            lastfm_path_final = _detect_directory_csv(
                "lastfm",
                label="Last.fm",
                enriched_filenames=("lastfm_enriched.csv",),
            )
            unearthed_path_final = _detect_directory_csv(
                "unearthed",
                label="Unearthed",
                enriched_filenames=("unearthed_enriched.csv",),
            )
        except Exception as exc:
            _safe_log(logger, f"[Master] directory CSV detection failed: {type(exc).__name__}: {exc}")
        _safe_log(logger, f"[Master] Passing bandcamp_csv_path={bandcamp_path_final or ''}")
        _safe_log(logger, f"[Master] Passing soundcloud_csv_path={soundcloud_path_final or ''}")
        _safe_log(logger, f"[Master] Passing lastfm_csv_path={lastfm_path_final or ''}")
        _safe_log(logger, f"[Master] Passing unearthed_csv_path={unearthed_path_final or ''}")

        max_live = getattr(cross_directory_enricher, "LIVE_SEARCH_MAX_ATTEMPTS", 50)
        if max_live_searches is not None:
            try:
                max_live = int(max_live_searches)
            except Exception:
                max_live = getattr(cross_directory_enricher, "LIVE_SEARCH_MAX_ATTEMPTS", 50)
            if max_live < 0:
                max_live = 0

        first_pass_state: Dict[str, Any] = {}
        cross_directory_enricher.run_cross_directory_enrichment(
            seed_csv_path,
            output_csv_path,
            bandcamp_csv_path=bandcamp_path_final or "",
            soundcloud_csv_path=soundcloud_path_final or "",
            unearthed_csv_path=unearthed_path_final or "",
            lastfm_csv_path=lastfm_path_final or "",
            enable_live_search=enable_live_search,
            max_live_searches=max_live,
            logger=logger,
            night_mode=night_mode,
            yield_tracker=yield_tracker,
            state_sink=first_pass_state,
            night_fb_run_state=night_fb_run_state,
        )
        try:
            expansion_raw_csv_path = cross_directory_enricher._festival_expansion_raw_path(output_csv_path)
        except Exception:
            expansion_raw_csv_path = ""
        if expansion_raw_csv_path and os.path.exists(expansion_raw_csv_path):
            try:
                expansion_raw_df = pd.read_csv(
                    expansion_raw_csv_path,
                    dtype=str,
                    keep_default_na=False,
                ).fillna("")
                expansion_rows = len(expansion_raw_df.index)
                if expansion_rows > 0:
                    base, ext = os.path.splitext(output_csv_path)
                    expansion_output_csv_path = f"{base}_festival_expansion_enriched{ext or '.csv'}"
                    second_pass_state: Dict[str, Any] = {}
                    _safe_log(
                        logger,
                        f"[FestivalExpansion] running bounded second enrichment pass rows={expansion_rows}",
                    )
                    cross_directory_enricher.run_cross_directory_enrichment(
                        expansion_raw_csv_path,
                        expansion_output_csv_path,
                        bandcamp_csv_path=bandcamp_path_final or "",
                        soundcloud_csv_path=soundcloud_path_final or "",
                        unearthed_csv_path=unearthed_path_final or "",
                        lastfm_csv_path=lastfm_path_final or "",
                        enable_live_search=enable_live_search,
                        max_live_searches=max_live,
                        logger=logger,
                        night_mode=night_mode,
                        yield_tracker=yield_tracker,
                        state_source=first_pass_state,
                        state_sink=second_pass_state,
                        night_fb_run_state=night_fb_run_state,
                    )
                    _merge_festival_expansion_output(
                        output_csv_path,
                        expansion_output_csv_path,
                        cross_directory_enricher.normalise_artist_name,
                        logger=logger,
                    )
                    try:
                        merged_profile_index = cross_directory_enricher._merge_domain_profile_indexes(
                            first_pass_state.get("domain_profile_index"),
                            second_pass_state.get("domain_profile_index"),
                        )
                        merged_reuse_index = cross_directory_enricher._merge_domain_email_reuse_indexes(
                            merged_profile_index,
                            first_pass_state.get("domain_email_reuse_index"),
                            second_pass_state.get("domain_email_reuse_index"),
                        )
                        cross_directory_enricher._write_domain_org_sidecar(
                            output_csv_path,
                            merged_profile_index,
                            merged_reuse_index,
                            log_fn=logger,
                        )
                    except Exception as exc:
                        _safe_log(logger, f"[DomainOrg] failed to write merged sidecar safely: {exc}")
            except Exception as exc:
                _safe_log(logger, f"[FestivalExpansion] second pass skipped after error: {exc}")
        emit_enrichment_yield_summary(logger, dict(getattr(yield_tracker, "counts", {}) or {}))
    except Exception as exc:
        _safe_log(logger, f"[Master Enrich] Enricher failed safely: {exc}")
        shutil.copyfile(seed_csv_path, output_csv_path)
        return output_csv_path
    finally:
        if local_night_fb_run_state:
            close_night_fb_run_state(night_fb_run_state)

    _safe_log(logger, f"[Master Enrich] Completed cross-directory enrichment -> {output_csv_path}")
    return output_csv_path


def _run_unearthed_full_pipeline(job_config: Dict[str, Any], raw_output_path: str, module, logger: LoggerFn) -> str:
    """
    Night/legacy helper: run the full Unearthed pipeline, including the contact/email pass.
    Falls back to the existing scrape_website implementation if a dedicated pipeline
    entrypoint is not available.
    """
    search_term = (
        job_config.get("input_seed_csv")
        or job_config.get("seed")
        or job_config.get("url")
        or getattr(module, "UNEARTHED_DEFAULT_URL", "")
    )
    max_results = int(job_config.get("target_valid_leads") or job_config.get("max_results") or job_config.get("max_artists") or 0)
    pipeline_entry = None
    entrypoint_hint = (job_config.get("entrypoint") or "").strip()
    candidate_names = [
        entrypoint_hint,
        "run_unearthed_pipeline",
        "unearthed_pipeline",
        "run_unearthed_full_pipeline",
    ]

    # Try dedicated module import first (if present in repo), then fall back to legacy module attrs.
    fb_session = None
    try:
        fb_username = os.environ.get("FB_USERNAME", "").strip()
        fb_password = os.environ.get("FB_PASSWORD", "").strip()
        allow_auto_login = str(os.environ.get("FB_ALLOW_AUTOMATED_LOGIN", "") or "").strip().lower() in ("1", "true", "yes")
        get_shared = getattr(module, "get_shared_facebook_session", None)
        if fb_username and fb_password and callable(get_shared):
            if allow_auto_login:
                fb_session = get_shared(fb_username, fb_password, logger=logger)
            else:
                _safe_log(logger, "[Unearthed] FB creds supplied but automated login is disabled (set FB_ALLOW_AUTOMATED_LOGIN=1 to enable); proceeding without shared session.")
    except Exception:
        fb_session = None

    try:
        import unearthed_pipeline  # type: ignore

        for name in candidate_names:
            if name and hasattr(unearthed_pipeline, name):
                pipeline_entry = getattr(unearthed_pipeline, name)
                break
    except Exception:
        pipeline_entry = None

    if pipeline_entry is None:
        for name in candidate_names:
            if not name:
                continue
            if hasattr(module, name):
                pipeline_entry = getattr(module, name)
                break

    if callable(pipeline_entry):
        _safe_log(logger, f"[Unearthed] Using FULL pipeline entrypoint: {getattr(pipeline_entry, '__name__', pipeline_entry)}")
        try:
            result = pipeline_entry(
                search_term=search_term,
                region=job_config.get("region"),
                max_results=max_results or None,
                headless=True,
                output_csv=raw_output_path,
                job_config=job_config,
                fb_session=fb_session,
            )
            if isinstance(result, str) and result:
                return result
        except TypeError:
            try:
                result = pipeline_entry(
                    search_term,
                    region=job_config.get("region"),
                    max_results=max_results or None,
                    job_config=job_config,
                    fb_session=fb_session,
                )
                if isinstance(result, str) and result:
                    return result
            except Exception:
                pass
        except Exception:
            pass
        _safe_log(logger, "[Unearthed] FULL pipeline entry failed; falling back to listing-only scrape_website.")
    else:
        _safe_log(logger, "[Unearthed] FULL pipeline entry not found; falling back to listing-only scrape_website.")
    module.scrape_website(
        search_term,
        existing_csv=raw_output_path,
        max_artists=max_results or 200,
        job_config=job_config,
    )
    return raw_output_path


def run_directory_job(job_config: Dict[str, Any], raw_output_path: str, logger: LoggerFn = None) -> str:
    """
    Run a single directory scraper based on job_config.

    This wrapper intentionally keeps the surface area small and delegates
    behaviour to the existing scrapers without changing their defaults.
    """
    final_path = Path(raw_output_path)
    tmp_path = _derive_tmp_csv_path(final_path)
    _remove_if_exists(tmp_path)
    _ensure_parent(str(tmp_path))
    output_path = tmp_path.as_posix()

    module = _load_legacy_module()
    directory = (job_config.get("directory") or "").strip().lower()
    target_count = int(job_config.get("target_valid_leads") or job_config.get("target_count") or 0)
    mode = (job_config.get("mode") or "").strip().lower()
    job_label = job_config.get("job_id") or job_config.get("slug") or directory or "unknown"

    _safe_log(logger, f"[Directory] Running job for directory={directory} slug={job_config.get('slug', '')} job_id={job_config.get('job_id', '')}")

    result_path: Optional[str] = None
    success = False

    try:
        if "unearthed" in directory:
            # Try full Unearthed pipeline with contact/email pass first.
            try:
                full_csv = _run_unearthed_full_pipeline(job_config, output_path, module, logger)
            except Exception:
                full_csv = None
            if full_csv:
                _dedupe_unearthed_csv(full_csv, logger=logger)
                finalize_result = _finalize_tmp_csv(tmp_path, final_path)
                result_path = str(finalize_result.final_path)
                success = True
            else:
                _safe_log(logger, f"[Unearthed] Falling back to listing-only scrape_website for job: {job_config}")
                url = job_config.get("input_seed_csv") or job_config.get("seed") or job_config.get("url") or ""
                if not url:
                    url = getattr(module, "UNEARTHED_DEFAULT_URL", "")
                module.scrape_website(
                    url,
                    existing_csv=output_path,
                    max_artists=target_count or 200,
                    job_config=job_config,
                )
                _dedupe_unearthed_csv(output_path, logger=logger)
                finalize_result = _finalize_tmp_csv(tmp_path, final_path)
                result_path = str(finalize_result.final_path)
                success = True

        elif directory == "spotify":
            params = {
                "playlist_ids": job_config.get("playlist_ids"),
                "search_term": job_config.get("search_term") or job_config.get("input_seed_csv") or "",
                "spotify_client_id": job_config.get("spotify_client_id") or os.environ.get("SPOTIFY_CLIENT_ID"),
                "spotify_client_secret": job_config.get("spotify_client_secret") or os.environ.get("SPOTIFY_CLIENT_SECRET"),
            }
            rows = module.scrape_spotify(target_count, params, logger=logger)
            write_result = _write_rows_to_csv(rows, final_path.as_posix(), source_directory="spotify")
            result_path = str(write_result.final_path)
            success = True

        elif directory == "festival":
            from festival_scraper import scrape_festivals

            params = {
                "festival_keys": job_config.get("festival_keys"),
                "festival_sources": job_config.get("festival_sources"),
                "festival_source": job_config.get("festival_source"),
                "festival": job_config.get("festival"),
                "input_seed_csv": job_config.get("input_seed_csv") or "",
            }
            rows = scrape_festivals(target_count or None, params, logger=logger)
            write_result = _write_rows_to_csv(rows, final_path.as_posix(), source_directory="festival")
            result_path = str(write_result.final_path)
            success = True

        elif directory == "bandcamp":
            seed = (
                job_config.get("bandcamp_seed")
                or job_config.get("input_seed_csv")
                or job_config.get("seed")
                or job_config.get("url")
                or ""
            )
            progress_path = os.path.join(os.path.dirname(os.path.abspath(output_path)), "bandcamp_progress.json")
            module.scrape_bandcamp(
                seed,
                pages_per_tag=job_config.get("pages_per_tag", getattr(module, "BANDCAMP_PAGES_PER_TAG", 5)),
                existing_csv=output_path,
                max_artists=target_count or getattr(module, "BANDCAMP_TARGET_ROWS", 200),
                progress_path=progress_path,
                mode=mode or "discover",
                max_pages=job_config.get("max_pages"),
                max_items=job_config.get("max_items"),
                search_domain=job_config.get("search_domain", "artists"),
                search_location_filter=job_config.get("search_location", ""),
            )
            finalize_result = _finalize_tmp_csv(tmp_path, final_path)
            result_path = str(finalize_result.final_path)
            success = True

        elif directory == "soundcloud":
            url = job_config.get("soundcloud_url") or job_config.get("input_seed_csv") or job_config.get("seed") or ""
            module.scrape_soundcloud(
                url,
                seed_tags=job_config.get("seed_tags"),
                pages_per_tag=job_config.get("pages_per_tag", getattr(module, "SOUNDCLOUD_PAGES_PER_TAG", 5)),
                existing_csv=output_path,
                max_artists=target_count or 200,
                max_handles=job_config.get("max_handles"),
                min_yield=job_config.get("min_yield", 3),
                dry_run=bool(job_config.get("dry_run", False)),
            )
            finalize_result = _finalize_tmp_csv(tmp_path, final_path)
            result_path = str(finalize_result.final_path)
            success = True

        elif directory == "lastfm":
            seeds = _read_seed_list(job_config.get("input_seed_csv"))
            module.scrape_lastfm_similar(
                seeds,
                existing_csv=output_path,
                max_artists=target_count or getattr(module, "LASTFM_MAX_SIMILAR_PER_SEED", 200),
                log_fn=logger,
            )
            finalize_result = _finalize_tmp_csv(tmp_path, final_path)
            result_path = str(finalize_result.final_path)
            success = True

        elif directory == "unearthed":
            _run_unearthed_full_pipeline(job_config, output_path, module, logger)
            finalize_result = _finalize_tmp_csv(tmp_path, final_path)
            result_path = str(finalize_result.final_path)
            success = True

        else:
            raise ValueError(f"Unsupported directory: {directory}")

    except Exception as exc:
        _safe_log(logger, f"[Directory] Job failed for directory={directory}: {type(exc).__name__}: {exc}")
        try:
            if hasattr(logger, "exception") and callable(getattr(logger, "exception", None)):
                logger.exception("[Directory] Job failure traceback", exc_info=exc)
            else:
                _LOGGER.exception("[Directory] Job failure traceback", exc_info=exc)
        except Exception:
            pass
        if not _should_keep_tmp_on_failure():
            _remove_if_exists(tmp_path)
        raise
    finally:
        if success:
            _cleanup_leftover_tmp(tmp_path, job_label)
        try:
            close_job_browser(job_label)
        except Exception:
            pass

    return result_path if result_path is not None else str(final_path)


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
    def _write_empty_enriched(headers: Optional[Sequence[str]] = None) -> None:
        df_empty = pd.DataFrame(columns=list(headers) if headers is not None else [])
        df_empty.to_csv(enriched_output_path, index=False)

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
        if os.path.exists(raw_csv_path):
            shutil.copyfile(raw_csv_path, enriched_output_path)
        else:
            try:
                headers = list(pd.read_csv(raw_csv_path, nrows=0).columns)
            except Exception:
                headers = list(DEFAULT_EXPORT_COLUMNS) if "DEFAULT_EXPORT_COLUMNS" in globals() else []
            _write_empty_enriched(headers)
        result_path = enriched_output_path

    final_path = result_path
    try:
        df_for_checker = pd.read_csv(result_path, dtype=str, keep_default_na=False)
        df_for_checker = df_for_checker.fillna("")
        df_for_checker = _consolidate_email_all(df_for_checker)
        df_for_checker.to_csv(result_path, index=False)
    except Exception as exc:  # pragma: no cover - defensive fallback
        _safe_log(logger, f"[Enrich] Pre-check email consolidation failed safely: {exc}")
    try:
        checked_path = final_checker.run_final_checker(result_path)
        if checked_path and os.path.exists(checked_path):
            try:
                same_file = os.path.exists(enriched_output_path) and os.path.samefile(checked_path, enriched_output_path)
            except Exception:
                same_file = False
            if same_file:
                final_path = enriched_output_path
            else:
                shutil.copyfile(checked_path, enriched_output_path)
                final_path = enriched_output_path
        else:
            final_path = result_path
    except Exception as exc:  # pragma: no cover - defensive fallback
        _safe_log(logger, f"[Enrich] Final checker failed safely: {exc}")
        final_path = result_path
    # Canonicalize Email_All once post-enrichment for downstream consumers.
    try:
        df_final = pd.read_csv(final_path, dtype=str, keep_default_na=False)
        df_final = df_final.fillna("")
        df_final = _consolidate_email_all(df_final)
        df_final = _promote_fb_urls_df(df_final, logger=logger)
        df_final = recompute_final_status_post_enrichment(df_final, logger)
        df_final.to_csv(final_path, index=False)
    except Exception as exc:  # pragma: no cover - defensive
        _safe_log(logger, f"[Enrich] Email consolidation failed safely: {exc}")

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

def _is_quarantined_repeat(row: pd.Series) -> bool:
    email_source = _cell_str(row.get("Email Source"))
    suspect_email = _cell_str(row.get("Suspect_Email"))
    suspect_email_all = _cell_str(row.get("Suspect_Email_All"))
    email_val = _cell_str(row.get("Email"))
    email_all_val = _cell_str(row.get("Email_All"))
    suspect_present = bool(suspect_email or suspect_email_all)
    cleared_email_fields = (email_val == "" and email_all_val == "")
    return email_source == "Quarantined (repeat email)" or (cleared_email_fields and suspect_present)


def _is_unearthed_source_row(row: pd.Series) -> bool:
    values = (
        _cell_str(row.get("Source Directory")).lower(),
        _cell_str(row.get("Source Tag")).lower(),
        _cell_str(row.get("__source_job")).lower(),
    )
    return any(("unearthed" in value) or ("triple j" in value) for value in values)


def _should_skip_row_due_to_email(
    row: pd.Series, skip_rows_with_email: bool = True, logger: LoggerFn = _LOGGER
) -> bool:
    email_all_clean = str(row.get("Email_All") or "").strip()
    suspect_email = _cell_str(row.get("Suspect_Email"))
    suspect_email_all = _cell_str(row.get("Suspect_Email_All"))
    suspect_present = bool(suspect_email or suspect_email_all)
    normalized_emails = normalize_emails(email_all_clean)
    has_email_raw = bool(email_all_clean)
    has_non_placeholder_email = any(not is_obvious_placeholder_email(email) for email in normalized_emails)
    if has_email_raw and not normalized_emails:
        has_non_placeholder_email = True
    quarantined_repeat = _is_quarantined_repeat(row)
    # Suspect email flags override Email_All presence; treat as no usable email.
    has_email_effective = has_non_placeholder_email and not quarantined_repeat and not suspect_present

    if quarantined_repeat and has_email_raw:
        row_id = row.get("__row_id", row.name)
        artist = _cell_str(row.get("Artist Name"))
        _safe_log(
            logger,
            f"[FB SkipGate] allowing quarantined repeat-email row {row_id} ('{artist}') despite Email_All present",
        )
        return False

    return bool(skip_rows_with_email and has_email_effective)


def _night_fb_has_upstream_identity_anchor(row: pd.Series) -> bool:
    try:
        hint = preferred_upstream_identity_hint(row)
    except Exception:
        hint = ""
    if bool(str(hint or "").strip()):
        return True
    try:
        return bool(is_spotify_origin_row(row))
    except Exception:
        return False


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
    df = df.fillna("")
    df = _consolidate_email_all(df)
    df = _promote_fb_urls_df(df, logger=_LOGGER)
    # Normalize FB_Status column (empty string default). Preserve legacy lowercase if present.
    if "FB_Status" not in df.columns and "fb_status" in df.columns:
        df.rename(columns={"fb_status": "FB_Status"}, inplace=True)
    if "FB_Status" not in df.columns:
        df["FB_Status"] = ""
    else:
        df["FB_Status"] = df["FB_Status"].fillna("")
    df["__row_id"] = range(len(df))

    def _eligible(row: pd.Series) -> bool:
        if _should_skip_row_due_to_email(row, skip_rows_with_email, _LOGGER):
            return False
        if skip_rows_with_no_facebook_clue and not _has_facebook_clue(row):
            return False
        return True

    eligible_df = df[df.apply(_eligible, axis=1)].copy()
    fb_cap_raw = os.getenv("FB_PASS_CAP", "0")
    try:
        fb_cap = int(fb_cap_raw or "0")
    except Exception:
        fb_cap = 0
    if fb_cap > 0:
        _LOGGER.info("[FB Smoke Cap] Limiting FB pass to %s rows", fb_cap)
        eligible_df = eligible_df.head(fb_cap)
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
                if "FB_Status" not in fb_df.columns and "fb_status" in fb_df.columns:
                    fb_df.rename(columns={"fb_status": "FB_Status"}, inplace=True)
                if "FB_Status" not in fb_df.columns:
                    fb_df["FB_Status"] = ""
                for _, row in fb_df.iterrows():
                    rid = row.get("__row_id")
                    email_val = str(row.get("Email", "") or "").strip()
                    fb_url_val = str(row.get("Facebook_URL", "") or "").strip()
                    fb_status_val = str(row.get("FB_Status", "") or "")
                    artist_for_log = row.get("Artist Name", "") or row.get("Artist", "") or "<unknown>"
                    if not fb_status_val and is_fb_login_redirect(fb_url_val):
                        fb_status_val = "login_redirect"
                        fb_url_val = ""
                        _LOGGER.info(
                            "[FB Enrich] Detected login redirect for '%s' -> %s, marking FB_Status='login_redirect'.",
                            artist_for_log,
                            row.get("Facebook_URL") or "",
                        )
                    if email_val and not pd.isna(rid):
                        try:
                            rid_int = int(float(rid))
                        except Exception:
                            continue
                        filtered_email = filter_system_telemetry_emails([email_val])
                        filtered_email_all = filter_system_telemetry_emails(str(row.get("Email_All", "") or ""))
                        if not filtered_email and not filtered_email_all:
                            email_val = ""
                        else:
                            email_val = filtered_email[0] if filtered_email else ""
                        source_url = row.get("Email_Source_URL") or ""
                        if not source_url:
                            source_url = _facebook_about_url(fb_url_val)
                        source_url = source_url or fb_url_val or ""
                        source_type = row.get("Email_Source_Type") or "facebook_enrich"
                        method = row.get("Email_Extract_Method") or "regex"
                        if email_val:
                            _set_email_with_provenance(
                                (updated_df, rid_int),
                                email_val,
                                source_url,
                                source_type,
                                method,
                                _facebook_email_surface_hint(row),
                            )
                            _fill_email_provenance_fields(
                                updated_df,
                                rid_int,
                                source=row,
                                fb_url_hint=fb_url_val,
                                default_source_type="facebook_enrich",
                                default_method=method or "regex",
                            )
                        if filtered_email_all:
                            _set_email_all(
                                updated_df,
                                rid_int,
                                filtered_email_all,
                                source="fb_global_pass",
                                logger=_LOGGER.info,
                                source_url=source_url,
                                source_type=source_type,
                                method=method,
                                surface=_facebook_email_surface_hint(row),
                            )
                        updated_df.at[rid_int, "FB_Status"] = (
                            fb_status_val or ("ok" if email_val or filtered_email_all else "no_candidates")
                        )
                        if fb_url_val:
                            updated_df.at[rid_int, "Facebook_URL"] = fb_url_val
                    elif not email_val and not fb_url_val and not pd.isna(rid):
                        try:
                            rid_int = int(float(rid))
                        except Exception:
                            continue
                        existing_status = str(updated_df.at[rid_int, "FB_Status"] if "FB_Status" in updated_df.columns else "")
                        if not existing_status:
                            updated_df.at[rid_int, "FB_Status"] = fb_status_val or "no_candidates"
                            if not fb_status_val:
                                _LOGGER.info(
                                    "[FB Enrich] No usable FB candidates for '%s', marking FB_Status='no_candidates'.",
                                    artist_for_log,
                                )
                # Ensure attempted rows get a status if still empty.
                for rid_int in attempted_ids:
                    existing_status = str(updated_df.at[rid_int, "FB_Status"]) if "FB_Status" in updated_df.columns else ""
                    if not existing_status:
                        email_val = updated_df.at[rid_int, "Email"] if "Email" in updated_df.columns else ""
                        fb_url_val = updated_df.at[rid_int, "Facebook_URL"] if "Facebook_URL" in updated_df.columns else ""
                        updated_df.at[rid_int, "FB_Status"] = "ok" if str(email_val or fb_url_val or "").strip() else "no_candidates"
        except Exception:
            pass

    # Ensure rows with existing emails are marked as done.
    if "FB_Status" in df.columns:
        for idx, row in df.iterrows():
            email_val = row.get("Email", "")
            if pd.isna(email_val):
                email_val = ""
            if str(email_val or "").strip():
                current = str(row.get("FB_Status", "") or "")
                if not current:
                    df.at[idx, "FB_Status"] = "ok"

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
    skip_rows_with_email: bool = True,
    night_fb_run_state: Optional[NightFBRunState] = None,
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
    local_night_fb_run_state = False
    if night_fb_run_state is None:
        night_fb_run_state = create_night_fb_run_state(fb_username, fb_password)
        local_night_fb_run_state = True
    night_fb_session_source = night_fb_run_state.session_source
    source_label = night_fb_session_source.mode or "none"
    decision_label = (
        "disabled_for_run"
        if night_fb_run_state.disabled_for_run
        else ("probe_pending" if night_fb_session_source.can_probe else "disabled_for_run")
    )
    session_gate_reason = (
        night_fb_run_state.disable_reason
        if night_fb_run_state.disabled_for_run and night_fb_run_state.disable_reason
        else night_fb_session_source.reason
    )
    profile_suffix = (
        f" profile_dir={night_fb_session_source.profile_dir}"
        if night_fb_session_source.profile_dir
        else ""
    )
    _safe_log_console(
        logger,
        f"[Night FB][Session Gate] source={source_label} decision={decision_label} reason={session_gate_reason}{profile_suffix}",
    )

    # Always start from the full input to avoid losing rows when smoke caps are used.
    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False).fillna("")

    # If an output already exists, overlay any previously enriched columns onto the
    # full input frame instead of trusting the prior (possibly truncated) file.
    if output_csv and os.path.exists(output_csv):
        try:
            prev_df = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
            if len(prev_df) != len(df):
                _safe_log_console(
                    logger,
                    f"[Night FB] Existing master_post_fb row count mismatch (input={len(df)} output={len(prev_df)}); rebuilding from input.",
                )
            overlap_len = min(len(prev_df), len(df))
            shared_cols = [col for col in prev_df.columns if col in df.columns]
            if overlap_len and shared_cols:
                df.loc[: overlap_len - 1, shared_cols] = prev_df.loc[: overlap_len - 1, shared_cols]
            extra_cols = [col for col in prev_df.columns if col not in df.columns]
            for col in extra_cols:
                df[col] = ""
                if overlap_len:
                    df.loc[: overlap_len - 1, col] = prev_df.loc[: overlap_len - 1, col]
        except Exception:
            pass

    df = _consolidate_email_all(df)
    share_resolver = None
    if not night_fb_run_state.disabled_for_run and night_fb_session_source.can_probe:
        share_resolver = _build_night_fb_share_promotion_resolver(
            fb_username=fb_username,
            fb_password=fb_password,
            night_fb_run_state=night_fb_run_state,
            logger=logger or _LOGGER,
        )
    df = _promote_fb_urls_df(df, logger=logger or _LOGGER, share_resolver=share_resolver)
    if "FB_Status" not in df.columns and "fb_status" in df.columns:
        df.rename(columns={"fb_status": "FB_Status"}, inplace=True)
    if "FB_Status" not in df.columns:
        df["FB_Status"] = ""
    else:
        df["FB_Status"] = df["FB_Status"].fillna("")
    df = ensure_fb_attribution_columns(df)
    df = apply_fb_opportunity_state_df(df, overwrite=False)
    # Clear pandas NA values in FB-relevant string columns to avoid ambiguous boolean checks.
    for col in ("Facebook_URL", "Facebook URL", "Social Link", "External Links", "Email", "Email_All"):
        if col in df.columns:
            df[col] = df[col].fillna("")
    df["__row_id"] = range(len(df))
    _ensure_string_columns(
        df,
        (
            "Email",
            "Email_All",
            "Email_Type",
            "Facebook_URL",
            "FB_Status",
            *FB_ATTRIBUTION_COLUMNS,
        ),
    )

    fb_cap_raw = os.getenv("FB_PASS_CAP", "0")
    try:
        fb_cap = int(fb_cap_raw or "0")
    except Exception:
        fb_cap = 0

    total_rows = len(df.index)
    iter_indices = list(df.index)
    if fb_cap > 0:
        _safe_log_console(logger, f"[FB Smoke Cap] Limiting FB attempts to first {fb_cap} rows (output will retain all rows)")
        iter_indices = iter_indices[:fb_cap]
    disable_reason_code = str(
        (night_fb_run_state.disable_reason if night_fb_run_state.disabled_for_run else "") or night_fb_session_source.reason or ""
    ).strip().lower()
    allow_preseeded_fb_attempt_pass = False
    if (
        night_fb_run_state.disabled_for_run
        and not night_fb_run_state.session_invalid
        and disable_reason_code == "missing_session_source"
    ):
        for idx in iter_indices:
            row = df.loc[idx]
            if _cell_str(row.get(FB_OPPORTUNITY_STATE_COL, "")).lower() != "fb_opportunity_present":
                continue
            canonical_fb_url, _ = ensure_canonical_facebook_url(row, set_row=False)
            if canonical_fb_url:
                allow_preseeded_fb_attempt_pass = True
                break
    state = _load_fb_state(state_path)
    last_index = int(state.get("fb_last_index", -1) or -1)
    attempted_total = int(state.get("fb_attempted_total", 0) or 0)
    captcha_flag = bool(state.get("fb_captcha_flag", False))
    completed_rows = int(state.get("fb_completed", 0) or 0)

    try:
        os.environ["DISABLE_ORIGIN_AUTO_VALIDATE_PROMPT"] = "1"
    except Exception:
        pass

    if night_fb_run_state.disabled_for_run and not allow_preseeded_fb_attempt_pass:
        _safe_log_console(
            logger,
            f"[FB Night] Night FB already disabled for this run; passing through without enrichment (reason={night_fb_run_state.disable_reason or 'disabled'}).",
        )
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
        _log_fb_debug_summary(df, logger)
        if local_night_fb_run_state:
            close_night_fb_run_state(night_fb_run_state)
        return FacebookGlobalPassStatus(
            processed_rows=total_rows,
            total_rows=total_rows,
            completed=True,
            hit_captcha=False,
            limit_reached=False,
            attempted_total=attempted_total,
        )

    if night_fb_session_source.can_probe or allow_preseeded_fb_attempt_pass:
        if allow_preseeded_fb_attempt_pass and not night_fb_session_source.can_probe:
            _safe_log_console(
                logger,
                "[FB Night] Continuing direct FB attempt pass for preseeded FB opportunity rows without a live session source.",
            )
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
            _log_fb_debug_summary(df, logger)
            if local_night_fb_run_state:
                close_night_fb_run_state(night_fb_run_state)
            return FacebookGlobalPassStatus(
                processed_rows=completed_rows,
                total_rows=total_rows,
                completed=True,
                hit_captcha=False,
                limit_reached=False,
                attempted_total=attempted_total,
            )
    else:
        _safe_log_console(
            logger,
            f"[FB Night] No usable Night FB session source; passing through without enrichment (reason={night_fb_session_source.reason}).",
        )
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
        _log_fb_debug_summary(df, logger)
        if local_night_fb_run_state:
            close_night_fb_run_state(night_fb_run_state)
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
        use_shared_session=False,
        run_state=None if allow_preseeded_fb_attempt_pass else night_fb_run_state,
    )

    def _write_state_with_pass_a(extra: Dict[str, Any]) -> None:
        try:
            counts = fb_helper.get_pass_a_counts()
            extra = dict(extra or {})
            extra.update({f"pass_a_{k}": v for k, v in counts.items()})
        except Exception:
            pass
        state.update(extra)
        _write_fb_state(state_path, state)

    with fb_helper:
        failed, fail_reason = (fb_helper.get_session_failure() if hasattr(fb_helper, "get_session_failure") else (False, ""))  # type: ignore[attr-defined]
        if failed:
            _safe_log_console(logger, f"[FB Night] Skipping FB pass: session failed to start ({fail_reason or 'unknown'})")
            if local_night_fb_run_state:
                close_night_fb_run_state(night_fb_run_state)
            return df

        failure_logged = False
        for idx in iter_indices:
            row = df.loc[idx]
            failed, fail_reason = (fb_helper.get_session_failure() if hasattr(fb_helper, "get_session_failure") else (False, ""))  # type: ignore[attr-defined]
            if failed:
                if not failure_logged:
                    _safe_log_console(logger, f"[FB Night] FB session unavailable; stopping early ({fail_reason or 'unknown'}).")
                    failure_logged = True
                break
            if idx <= last_index:
                continue
            completed_rows += 1
            last_index = idx

            artist_label = row.get("Artist Name", "") or row.get("Artist", "") or "<unknown>"

            email_all_val = row.get("Email_All", "")
            if pd.isna(email_all_val):
                email_all_val = ""
            email_all_clean = str(email_all_val or "").strip()
            normalized_emails = normalize_emails(email_all_clean)
            has_email_raw = bool(email_all_clean)
            has_non_placeholder_email = any(not is_obvious_placeholder_email(email) for email in normalized_emails)
            if has_email_raw and not normalized_emails:
                has_non_placeholder_email = True
            quarantined_repeat = _is_quarantined_repeat(row)
            has_email_effective = has_non_placeholder_email and not quarantined_repeat

            fb_status_val_raw = str(row.get("FB_Status", "") or "").strip()
            fb_status_val = fb_status_val_raw.lower()

            should_skip_due_to_email = _should_skip_row_due_to_email(row, skip_rows_with_email, logger)
            terminal_statuses = {"no_candidates", "unearthed_no_emails"}
            canonical_facebook_url, _ = ensure_canonical_facebook_url(row, set_row=False)
            has_canonical_facebook_url = bool(canonical_facebook_url)
            explicit_fb_entrypoints = explicit_fb_entrypoint_urls_for_row(row.to_dict())
            has_explicit_fb_entrypoint = bool(explicit_fb_entrypoints)
            is_unearthed_source = _is_unearthed_source_row(row)
            unearthed_fb_first_active = bool(
                is_unearthed_source and (has_canonical_facebook_url or has_explicit_fb_entrypoint)
            )
            if should_skip_due_to_email and unearthed_fb_first_active:
                _safe_log_console(
                    logger,
                    f"[Unearthed Path] forcing FB extraction despite existing email row={idx} artist={artist_label!r}",
                )
            effective_skip_due_to_email = bool(should_skip_due_to_email and not unearthed_fb_first_active)
            final_fb_statuses = {"login_redirect", "no_candidates", "ok", "found"} | terminal_statuses
            has_upstream_identity_anchor = _night_fb_has_upstream_identity_anchor(row)
            should_run_night_fb = (
                (not has_email_effective) or unearthed_fb_first_active
            ) and (fb_status_val not in final_fb_statuses)
            unearthed_no_url_discovery_eligible = bool(
                is_unearthed_source
                and (not has_canonical_facebook_url)
                and (not has_explicit_fb_entrypoint)
                and should_run_night_fb
                and not effective_skip_due_to_email
                and fb_status_val not in terminal_statuses
            )
            discovery_fallback_eligible = bool(
                (not has_canonical_facebook_url)
                and (not has_explicit_fb_entrypoint)
                and should_run_night_fb
                and not effective_skip_due_to_email
                and fb_status_val not in terminal_statuses
                and (has_upstream_identity_anchor or unearthed_no_url_discovery_eligible)
            )
            if has_explicit_fb_entrypoint and _cell_str(df.at[idx, FB_OPPORTUNITY_STATE_COL]) in {"", "no_fb_opportunity"}:
                df.at[idx, FB_OPPORTUNITY_STATE_COL] = "fb_opportunity_present"
            if discovery_fallback_eligible and _cell_str(df.at[idx, FB_OPPORTUNITY_STATE_COL]) in {"", "no_fb_opportunity"}:
                df.at[idx, FB_OPPORTUNITY_STATE_COL] = "fb_discovery_fallback_eligible"
            eligible_for_fb = bool(
                should_run_night_fb
                and not effective_skip_due_to_email
                and fb_status_val not in terminal_statuses
                and (
                    has_canonical_facebook_url
                    or has_explicit_fb_entrypoint
                    or has_upstream_identity_anchor
                    or unearthed_no_url_discovery_eligible
                )
            )
            _safe_log_console(
                logger,
                f"[Night FB][Row Gate] row={idx} artist={artist_label!r} "
                f"email_present={has_email_effective} fb_url_present={has_canonical_facebook_url} "
                f"fb_entrypoint_present={has_explicit_fb_entrypoint} "
                f"eligible_for_fb={eligible_for_fb}",
            )

            skip_row = False

            if effective_skip_due_to_email:
                df.at[idx, FB_GATE_STATE_COL] = "skipped_existing_usable_email"
                if not _cell_str(df.at[idx, FB_WRITE_STATE_COL]):
                    df.at[idx, FB_WRITE_STATE_COL] = "fb_no_email_written"
                _safe_log_console(
                    logger,
                    f"[Night FB] Skipping row {idx} ('{artist_label}') – email already present (Email_All='{email_all_clean}').",
                )
                skip_row = True

            elif fb_status_val in terminal_statuses:
                df.at[idx, FB_GATE_STATE_COL] = "skipped_terminal_fb_status"
                if not _cell_str(df.at[idx, FB_WRITE_STATE_COL]):
                    df.at[idx, FB_WRITE_STATE_COL] = "fb_no_email_written"
                _safe_log_console(
                    logger,
                    f"[Night FB] Skipping row {idx} ('{artist_label}') – terminal FB_Status='{fb_status_val_raw}'.",
                )
                skip_row = True

            elif (
                not has_canonical_facebook_url
                and not has_explicit_fb_entrypoint
                and not has_upstream_identity_anchor
                and not unearthed_no_url_discovery_eligible
            ):
                df.at[idx, FB_GATE_STATE_COL] = "skipped_no_identity_anchor"
                if not _cell_str(df.at[idx, FB_WRITE_STATE_COL]):
                    df.at[idx, FB_WRITE_STATE_COL] = "fb_no_email_written"
                _safe_log_console(
                    logger,
                    f"[Night FB] Skipping row {idx} ('{artist_label}') - no canonical Facebook URL, explicit FB entrypoint, or upstream identity anchor.",
                )
                skip_row = True

            if not skip_row and (fb_status_val in final_fb_statuses or not should_run_night_fb):
                if not has_canonical_facebook_url and not discovery_fallback_eligible:
                    if _cell_str(df.at[idx, FB_OPPORTUNITY_STATE_COL]) != "no_fb_opportunity":
                        df.at[idx, FB_GATE_STATE_COL] = "skipped_no_canonical_facebook_url"
                else:
                    df.at[idx, FB_GATE_STATE_COL] = "skipped_other_gate"
                if not _cell_str(df.at[idx, FB_WRITE_STATE_COL]):
                    df.at[idx, FB_WRITE_STATE_COL] = "fb_no_email_written"
                if fb_status_val in final_fb_statuses:
                    email_state = "present" if has_email_effective else "missing"
                    _safe_log_console(
                        logger,
                        f"[Night FB] Skipping FB lookup for '{artist_label}' (FB_Status='{fb_status_val_raw}', email='{email_state}', skipped_due_to_email={int(effective_skip_due_to_email)}).",
                    )
                skip_row = True

            elif not skip_row:
                multiplier = 1.0
                try:
                    if hasattr(fb_helper, "get_slow_mode_multiplier"):
                        multiplier = max(1.0, float(fb_helper.get_slow_mode_multiplier()))
                except Exception:
                    multiplier = 1.0

                if processed_this_run > 0:
                    delay = (random.uniform(*per_row_delay_range) if per_row_delay_range else 0.0) * multiplier
                    _safe_log_console(logger, f"[FB Night] Sleeping {delay:.2f}s before next row (index={idx}).")
                    _safe_sleep(delay)

                processed_this_run += 1
                attempted_total += 1

                if short_break_every > 0 and processed_this_run % short_break_every == 0:
                    pause = (random.uniform(*short_break_range) if short_break_range else 0.0) * multiplier
                    _safe_log_console(logger, f"[FB Night] Short break for {pause:.2f}s after {processed_this_run} rows.")
                    _safe_sleep(pause)
                if long_break_every > 0 and processed_this_run % long_break_every == 0:
                    pause = (random.uniform(*long_break_range) if long_break_range else 0.0) * multiplier
                    _safe_log_console(logger, f"[FB Night] Long break for {pause:.2f}s after {processed_this_run} rows.")
                    _safe_sleep(pause)

                try:
                    clean_row = {k: ("" if pd.isna(v) else v) for k, v in row.to_dict().items()}
                    enriched = fb_helper.enrich_row_with_facebook_night(clean_row, row_index=idx)
                except FacebookDriverError as exc:
                    _safe_log_console(logger, f"[FB Night] Driver error at row {idx}: {exc}")
                    enriched = {"FB_Status": "driver_error", FB_ATTEMPT_STATE_COL: "attempted_fb_timeout_or_fetch_error"}
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
                        _write_state_with_pass_a(state)
                        break
                    _safe_log_console(logger, f"[FB Night] Night FB enrich failed at row {idx}: {exc}")
                    enriched = None

                write_before = _fb_write_surface_snapshot(df.loc[idx])
                if enriched:
                    status_val = str(enriched.get("FB_Status", "") or "")
                    fb_rejected = _fb_status_is_rejected(status_val)
                    if fb_rejected:
                        artist_label = row.get("Artist Name", "") or row.get("Artist", "") or "<unknown>"
                        fb_url = enriched.get("Facebook_URL") or (df.at[idx, "Facebook_URL"] if "Facebook_URL" in df.columns else "")
                        fb_url = str(fb_url or "").strip() or "<unknown>"
                        reason = status_val or str(enriched.get("FB_Reason", "") or "reject")
                        _safe_log_console(
                            logger,
                            f"[FB Guard] Discarding emails from rejected FB page '{fb_url}' for '{artist_label}' (reason={reason})",
                        )
                    else:
                        source_url = enriched.get("Email_Source_URL") or ""
                        fb_url_hint = enriched.get("Facebook_URL") or ""
                        if not source_url:
                            source_url = _facebook_about_url(fb_url_hint)
                        source_url = source_url or fb_url_hint or ""
                        source_type = enriched.get("Email_Source_Type") or "facebook_enrich"
                        method = enriched.get("Email_Extract_Method") or "regex"
                        _set_email_with_provenance(
                            (df, idx),
                            enriched.get("Email"),
                            source_url,
                            source_type,
                            method,
                            _facebook_email_surface_hint(enriched),
                        )
                        _fill_email_provenance_fields(
                            df,
                            idx,
                            source=enriched,
                            fb_url_hint=fb_url_hint,
                            default_source_type=source_type or "facebook_enrich",
                            default_method=method or "regex",
                        )
                        if "Email_All" in enriched:
                            _set_email_all(
                                df,
                                idx,
                                enriched.get("Email_All", ""),
                                source="fb_global_pass",
                                logger=logger,
                                source_url=source_url,
                                source_type=source_type,
                                method=method,
                                surface=_facebook_email_surface_hint(enriched),
                            )
                    cols_to_copy = [
                        "Facebook_URL",
                        "__fb_emails_applied",
                        "FB_Match_Level",
                        "FB_Selected_By",
                        "FB_Name_Consistency_Flag",
                        "FB_Review_Reason",
                        "FB_Refine_Decision",
                        "FB_Refine_Executed",
                    ]
                    if not fb_rejected:
                        cols_to_copy.append("Email_Type")
                    for col in cols_to_copy:
                        if col in enriched:
                            df.at[idx, col] = enriched.get(col, "")
                    if not status_val:
                        email_now = enriched.get("Email", "") or ""
                        fb_url_now = enriched.get("Facebook_URL", "") or ""
                        status_val = "ok" if (str(email_now).strip() or fb_url_now) else "no_candidates"
                    df.at[idx, "FB_Status"] = status_val
                    attempt_state = _classify_fb_attempt_state_from_status(
                        status_val,
                        enriched.get(FB_ATTEMPT_STATE_COL, ""),
                    )
                    df.at[idx, FB_ATTEMPT_STATE_COL] = attempt_state
                else:
                    # Attempted but no enrichment result; mark as no_candidates to avoid repeated retries.
                    df.at[idx, "FB_Status"] = "no_candidates"
                    attempt_state = "attempted_fb_timeout_or_fetch_error"
                    df.at[idx, FB_ATTEMPT_STATE_COL] = attempt_state

                write_after = _fb_write_surface_snapshot(df.loc[idx])
                df.at[idx, FB_WRITE_STATE_COL] = _classify_fb_write_state(write_before, write_after, attempt_state)

            df.at[idx, FB_DEBUG_REASON_COL] = _classify_fb_debug_reason(df.loc[idx])

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
            _write_state_with_pass_a(state)

            if skip_row:
                continue

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
    _write_state_with_pass_a(state)

    try:
        counts = fb_helper.get_pass_a_counts()
    except Exception:
        counts = {}
    _safe_log_console(
        logger,
        "[FB Night][PASS A Summary] "
        f"attempted={counts.get('attempted',0)} "
        f"found_email={counts.get('found_email',0)} "
        f"no_email_on_page={counts.get('no_email_on_page',0)} "
        f"login_wall={counts.get('login_wall',0)} "
        f"fetch_error={counts.get('fetch_error',0)} "
        f"skipped_no_fb_url={counts.get('skipped_no_fb_url',0)}",
    )
    try:
        email_stats = fb_helper.get_email_stats()
    except Exception:
        email_stats = {}
    if email_stats:
        _safe_log_console(
            logger,
            "[FB Email][Summary] "
            f"pages_visited={email_stats.get('fb_email_pages_visited',0)} "
            f"emails_found={email_stats.get('fb_emails_found',0)} "
            f"skipped_checkpoint={email_stats.get('fb_rows_skipped_reason_checkpoint',0)} "
            f"skipped_challenge={email_stats.get('fb_rows_skipped_reason_challenge',0)} "
            f"skipped_cooldown={email_stats.get('fb_rows_skipped_reason_cooldown',0)} "
            f"skipped_no_opportunity={email_stats.get('fb_rows_skipped_reason_no_opportunity',0)}",
        )

    df.drop(columns=["__row_id"], inplace=True, errors="ignore")
    df.to_csv(output_csv, index=False)
    _log_fb_refine_summary(df, logger)
    _log_fb_debug_summary(df, logger)
    if local_night_fb_run_state:
        close_night_fb_run_state(night_fb_run_state)
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
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
    EMAIL_PROVENANCE_JSON_COL,
    "Email_Type",
    "FB_Status",
    FB_OPPORTUNITY_STATE_COL,
    FB_GATE_STATE_COL,
    FB_ATTEMPT_STATE_COL,
    FB_WRITE_STATE_COL,
    FB_DEBUG_REASON_COL,
    "Played on triple J",
    "Played on Unearthed",
    "Release Date",
    "Primary Genre",
    "Unearthed_Genre_Raw",
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
    export_profile: str = "full_dump",
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
        from final_checker import filter_rows_for_export

        consolidated_df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        consolidated_df = consolidated_df.fillna("")
        consolidated_df = _consolidate_email_all(consolidated_df)
        consolidated_df = recompute_final_status_post_enrichment(consolidated_df, export_logger)
        rows = consolidated_df.to_dict(orient="records")

        with open(output_csv, "w", encoding="utf-8", newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=columns)
            writer.writeheader()
            filtered_rows = filter_rows_for_export(export_profile or "studio_safe", rows)
            for row in filtered_rows:
                writer.writerow({col: row.get(col, "") for col in columns})
            row_count = len(filtered_rows)
            export_logger.info(
                "[Exporter] Export profile=%s: input rows=%s, exported rows=%s",
                (export_profile or "studio_safe"),
                len(rows),
                row_count,
            )
    except FileNotFoundError:
        export_logger.warning("[Master] Export skipped; input not found during read: %s", input_csv)
        return
    except Exception as exc:  # pragma: no cover - defensive
        export_logger.error("[Master] Export failed safely: %s", exc)
        return

    export_logger.info("[Master] Export wrote %s rows to %s", row_count, output_csv)
