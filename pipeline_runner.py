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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

import pandas as pd

try:  # Shared FB helper; safe fallback if unavailable.
    from facebook_enrich import is_fb_login_redirect  # type: ignore
except Exception:  # pragma: no cover - defensive
    def is_fb_login_redirect(url: str) -> bool:  # type: ignore
        return False

from night_mode_fb import FacebookDriverError, NightModeFacebookEnricher

LoggerFn = Optional[Callable[[str], None]]

_LEGACY_MODULE = None
_LOGGER = logging.getLogger(__name__)
EMAIL_PRIORITY_COLS: Sequence[str] = ("Email", "Email_All", "Directory_Email", "Unearthed_Email")


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


def normalize_emails(value) -> List[str]:
    """
    Split on commas, semicolons, whitespace, newlines. Lowercase, strip, validate
    basic email shape, dedupe, and return sorted list for stable output.
    """
    text = "" if value is None else str(value)
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


def _set_email_all(df: pd.DataFrame, idx: int, new_emails: Union[str, Sequence[str]], source: str, logger: LoggerFn = None) -> str:
    """Centralized Email_All setter with merge + logging + guard."""
    existing_val = _cell_str(df.at[idx, "Email_All"] if "Email_All" in df.columns else "")
    merged_list = _merge_email_lists(existing_val, new_emails)
    merged_str = ";".join(merged_list)
    df.at[idx, "Email_All"] = merged_str
    artist = _cell_str(df.at[idx, "Artist Name"]) if "Artist Name" in df.columns else ""
    log_enabled = os.getenv("EMAIL_ALL_LOG") in {"1", "true", "TRUE"}
    if log_enabled:
        _log_email_all_change(idx, artist, existing_val, merged_str, source, logger)
    # Guard may emit its own logs only when EMAIL_ALL_GUARD is enabled and a suspicious merge occurs.
    _guard_email_all_sources(df.loc[idx], merged_str, logger)
    return merged_str

def emails_to_string(emails: List[str]) -> str:
    """Return ", ".join(emails) or empty string."""
    return ", ".join(emails) if emails else ""


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

    def _build_email_all(row: pd.Series) -> str:
        if _is_quarantined(row):
            return _cell_str(row.get("Email_All"))
        collected: List[str] = []
        for field in legacy_fields:
            if field in row:
                collected.extend(normalize_emails(row.get(field, "")))
        unique_sorted = sorted(set(collected)) if collected else []
        return emails_to_string(unique_sorted)

    df["Email_All"] = df.apply(_build_email_all, axis=1)
    try:
        for idx in range(len(df.index)):
            if _is_quarantined(df.loc[idx]):
                continue
            _set_email_all(df, idx, df.at[idx, "Email_All"], source="consolidate", logger=_LOGGER.info)
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
]

WOODPECKER_EXPORT_COLUMNS: Sequence[str] = [
    "Artist Name",
    "Primary Email",
    "All Emails",
    "Email Source",
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
]

_AU_STATE_TOKENS = ("nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt")
_FINAL_STATUS_KEEP = {"OK", "WARN", "BLOCK"}
_COUNTRY_ALIASES = [
    (("united kingdom", "uk", "u.k.", "u.k", "england", "scotland", "wales", "northern ireland"), "United Kingdom"),
    (("united states", "usa", "u.s.a", "u.s.a.", "us", "u.s.", "america"), "United States"),
    (("australia", "aus"), "Australia"),
    (("canada", "can."), "Canada"),
]


def _normalize_date_string(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
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


def _derive_primary_email(email: str, email_all: str) -> str:
    email_clean = (email or "").strip()
    if email_clean:
        return email_clean
    combined = (email_all or "").strip()
    if not combined:
        return ""
    candidates = [part.strip() for part in re.split(r"[;,]", combined) if part and part.strip()]
    return candidates[0] if candidates else ""


def _derive_all_emails(email: str, email_all: str) -> str:
    combined = (email_all or "").strip()
    if combined:
        return combined
    email_clean = (email or "").strip()
    return email_clean if email_clean else ""


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
    fb_status = str(row.get("FB_Status") or "").lower()
    src_dir = str(row.get("Source Directory") or "").lower()
    src_url = str(row.get("Source URL") or "").lower()

    if email_type == "fb_night" or fb_status.startswith("ok"):
        return "Facebook About"

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
        final_status = str(row.get("final_status", "") or "").strip()
        status_normalized = str(row.get("_status_normalized", "") or "").strip()
        needs_review = status_normalized == "BLOCK"

        location = str(row.get("Location", "") or "").strip()
        existing_country = str(row.get("Country_Derived", "") or "").strip()
        country = existing_country or normalize_country_from_location(location)

        email = str(row.get("Email", "") or "")
        email_all = str(row.get("Email_All", "") or "")
        primary_email = _derive_primary_email(email, email_all)
        all_emails = _derive_all_emails(email, email_all)

        social_link = str(row.get("Social Link", "") or "")
        contact_mode = _derive_contact_mode(primary_email, social_link)

        spotify_playlist = str(row.get("Spotify Playlist", "") or "")
        source_directory = str(row.get("Source Directory", "") or "")
        discovery_source = infer_discovery_source(row)

        release_date_norm = _normalize_date_string(row.get("Release Date", ""))
        date_added_norm = _normalize_date_string(row.get("Date Added", ""))

        row_for_email_source = row.copy()
        row_for_email_source["Primary Email"] = primary_email
        email_source = infer_email_source(row_for_email_source)

        external_links = str(row.get("External Links", "") or "").strip()
        review_urls = str(row.get("Review_Urls", "") or row.get("Review Urls", "") or "").strip()

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


def build_final_export_view(input_csv_path: Union[Path, str], output_csv_path: Union[Path, str], logger: Optional[logging.Logger] = None) -> None:
    """
    Read the enriched/master CSV and write a client-facing final export view.
    """
    export_logger = logger or logging.getLogger(__name__)
    input_path = Path(input_csv_path)
    output_path = Path(output_csv_path)
    if not input_path.exists():
        export_logger.warning("[Final Export] Input not found: %s", input_path)
        return

    try:
        df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    except Exception as exc:
        export_logger.error("[Final Export] Failed to read %s: %s", input_path, exc)
        return

    df["Country_Derived"] = df.apply(infer_country_from_context, axis=1)
    df["Location"] = df.apply(infer_location_for_export, axis=1)

    total_rows = len(df.index)
    final_df = _build_final_export_frame(df)
    kept_rows = len(final_df.index)
    needs_review_count = (
        int(final_df["Needs_Review"].astype(str).str.upper().eq("TRUE").sum()) if "Needs_Review" in final_df else 0
    )
    export_logger.info(
        "[Final Export] rows_in=%s rows_kept=%s needs_review=%s",
        total_rows,
        kept_rows,
        needs_review_count,
    )
    _ensure_parent(str(output_path))
    final_df.to_csv(output_path, index=False)
    export_logger.info("[Final Export] Wrote final export CSV: %s", output_path)


def _build_woodpecker_frame(final_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build Woodpecker-friendly view from the already built final export DataFrame.
    """
    if final_df is None or final_df.empty:
        return pd.DataFrame(columns=WOODPECKER_EXPORT_COLUMNS)
    df = final_df.copy()
    for col in WOODPECKER_EXPORT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    # All Emails column is derived from canonical Email_All during export prep.
    email_mask_source = "All Emails" if "All Emails" in df.columns else "Primary Email"
    mask = df[email_mask_source].fillna("").astype(str).str.strip() != ""
    filtered = df.loc[mask].copy()
    return filtered.loc[:, list(WOODPECKER_EXPORT_COLUMNS)]


def write_final_and_woodpecker_exports(
    input_csv_path: Union[Path, str],
    final_export_csv_path: Union[Path, str],
    woodpecker_export_csv_path: Union[Path, str],
    logger: Optional[logging.Logger] = None,
    consolidated_df: Optional[pd.DataFrame] = None,
) -> None:
    export_logger = logger or logging.getLogger(__name__)
    input_path = Path(input_csv_path)
    if not input_path.exists():
        export_logger.warning("[Final Export] Input not found: %s", input_path)
        return
    try:
        if consolidated_df is not None:
            df = consolidated_df.copy()
        else:
            df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
            df = df.fillna("")
            df = _consolidate_email_all(df)
    except Exception as exc:
        export_logger.error("[Final Export] Failed to read %s: %s", input_path, exc)
        return

    df["Country_Derived"] = df.apply(infer_country_from_context, axis=1)
    df["Location"] = df.apply(infer_location_for_export, axis=1)

    final_df = _build_final_export_frame(df)
    total_rows = len(final_df.index)
    needs_review_final = (
        int(final_df["Needs_Review"].astype(str).str.upper().eq("TRUE").sum()) if "Needs_Review" in final_df else 0
    )

    _ensure_parent(str(final_export_csv_path))
    final_df.to_csv(final_export_csv_path, index=False, encoding="utf-8")
    export_logger.info(
        "[Final Export] rows_kept=%s needs_review=%s -> %s",
        total_rows,
        needs_review_final,
        final_export_csv_path,
    )

    woodpecker_df = _build_woodpecker_frame(final_df)
    wood_rows = len(woodpecker_df.index)
    wood_needs_review = (
        int(woodpecker_df["Needs_Review"].astype(str).str.upper().eq("TRUE").sum())
        if "Needs_Review" in woodpecker_df
        else 0
    )
    _ensure_parent(str(woodpecker_export_csv_path))
    woodpecker_df.to_csv(woodpecker_export_csv_path, index=False, encoding="utf-8")
    export_logger.info(
        "[Woodpecker Export] rows_with_email=%s needs_review=%s -> %s",
        wood_rows,
        wood_needs_review,
        woodpecker_export_csv_path,
    )


def run_master_enrichment(
    seed_csv_path: str,
    output_csv_path: str,
    logger: LoggerFn = None,
    enable_live_search: bool = True,
    max_live_searches: Optional[int] = None,
    night_mode: bool = False,
) -> str:
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
        max_live = getattr(cross_directory_enricher, "LIVE_SEARCH_MAX_ATTEMPTS", 50)
        if max_live_searches is not None:
            try:
                max_live = int(max_live_searches)
            except Exception:
                max_live = getattr(cross_directory_enricher, "LIVE_SEARCH_MAX_ATTEMPTS", 50)
            if max_live < 0:
                max_live = 0

        cross_directory_enricher.run_cross_directory_enrichment(
            seed_csv_path,
            output_csv_path,
            bandcamp_csv_path="",
            soundcloud_csv_path="",
            unearthed_csv_path="",
            lastfm_csv_path="",
            enable_live_search=enable_live_search,
            max_live_searches=max_live,
            logger=logger,
            night_mode=night_mode,
        )
    except Exception as exc:
        _safe_log(logger, f"[Master Enrich] Enricher failed safely: {exc}")
        shutil.copyfile(seed_csv_path, output_csv_path)
        return output_csv_path

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
                fb_session=fb_session,
            )
            if isinstance(result, str) and result:
                return result
        except TypeError:
            try:
                result = pipeline_entry(search_term, region=job_config.get("region"), max_results=max_results or None, fb_session=fb_session)
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
    )
    return raw_output_path


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

    _safe_log(logger, f"[Directory] Running job for directory={directory} slug={job_config.get('slug', '')} job_id={job_config.get('job_id', '')}")

    if "unearthed" in directory:
        # Try full Unearthed pipeline with contact/email pass first.
        try:
            full_csv = _run_unearthed_full_pipeline(job_config, raw_output_path, module, logger)
        except Exception:
            full_csv = None
        if full_csv:
            return full_csv
        _safe_log(logger, f"[Unearthed] Falling back to listing-only scrape_website for job: {job_config}")
        url = job_config.get("input_seed_csv") or job_config.get("seed") or job_config.get("url") or ""
        if not url:
            url = getattr(module, "UNEARTHED_DEFAULT_URL", "")
        module.scrape_website(
            url,
            existing_csv=raw_output_path,
            max_artists=target_count or 200,
        )
        return raw_output_path

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
        return _run_unearthed_full_pipeline(job_config, raw_output_path, module, logger)

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


def _should_skip_row_due_to_email(
    row: pd.Series, skip_rows_with_email: bool = True, logger: LoggerFn = _LOGGER
) -> bool:
    email_all_clean = str(row.get("Email_All") or "").strip()
    suspect_email = _cell_str(row.get("Suspect_Email"))
    suspect_email_all = _cell_str(row.get("Suspect_Email_All"))
    suspect_present = bool(suspect_email or suspect_email_all)
    has_email_raw = bool(email_all_clean)
    quarantined_repeat = _is_quarantined_repeat(row)
    # Suspect email flags override Email_All presence; treat as no usable email.
    has_email_effective = has_email_raw and not quarantined_repeat and not suspect_present

    if quarantined_repeat and has_email_raw:
        row_id = row.get("__row_id", row.name)
        artist = _cell_str(row.get("Artist Name"))
        _safe_log(
            logger,
            f"[FB SkipGate] allowing quarantined repeat-email row {row_id} ('{artist}') despite Email_All present",
        )
        return False

    return bool(skip_rows_with_email and has_email_effective)


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
                        _maybe_set_email(updated_df, rid_int, email_val)
                        updated_df.at[rid_int, "FB_Status"] = fb_status_val or "ok"
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
    if fb_username:
        _safe_log_console(logger, "[Night FB] FB username provided (length only logged).")
    else:
        _safe_log_console(logger, "[Night FB] FB username missing; Night FB will run unauthenticated.")

    # Use existing output if present so resumes keep prior enrichments.
    base_path = output_csv if output_csv and os.path.exists(output_csv) else input_csv
    df = pd.read_csv(base_path)
    df = df.fillna("")
    df = _consolidate_email_all(df)
    if "FB_Status" not in df.columns and "fb_status" in df.columns:
        df.rename(columns={"fb_status": "FB_Status"}, inplace=True)
    if "FB_Status" not in df.columns:
        df["FB_Status"] = ""
    else:
        df["FB_Status"] = df["FB_Status"].fillna("")
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
        ),
    )

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
        use_shared_session=False,
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
            return df

        failure_logged = False
        for idx, row in df.iterrows():
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
            has_email_raw = bool(email_all_clean)
            quarantined_repeat = _is_quarantined_repeat(row)
            has_email_effective = has_email_raw and not quarantined_repeat

            fb_status_val_raw = str(row.get("FB_Status", "") or "").strip()
            fb_status_val = fb_status_val_raw.lower()

            should_skip_due_to_email = _should_skip_row_due_to_email(row, skip_rows_with_email, logger)

            if should_skip_due_to_email:
                _safe_log_console(
                    logger,
                    f"[Night FB] Skipping row {idx} ('{artist_label}') – email already present (Email_All='{email_all_clean}').",
                )
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
                continue

            terminal_statuses = {"no_candidates", "unearthed_no_emails"}
            if fb_status_val in terminal_statuses:
                _safe_log_console(
                    logger,
                    f"[Night FB] Skipping row {idx} ('{artist_label}') – terminal FB_Status='{fb_status_val_raw}'.",
                )
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
                continue

            facebook_url_hint = str(row.get("Facebook_URL", "") or "").strip()
            has_clue = _has_facebook_clue(row)
            final_fb_statuses = {"login_redirect", "no_candidates", "ok", "found"} | terminal_statuses
            should_run_night_fb = (not has_email_effective) and (fb_status_val not in final_fb_statuses)
            if should_skip_due_to_email or fb_status_val in final_fb_statuses or not has_clue or not should_run_night_fb:
                if should_skip_due_to_email or fb_status_val in final_fb_statuses:
                    email_state = "present" if has_email_effective else "missing"
                    _safe_log_console(
                        logger,
                        f"[Night FB] Skipping FB lookup for '{artist_label}' (FB_Status='{fb_status_val_raw}', email='{email_state}', skipped_due_to_email={int(should_skip_due_to_email)}).",
                    )
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
            except FacebookDriverError as exc:
                _safe_log_console(logger, f"[FB Night] Driver error at row {idx}: {exc}")
                enriched = {"FB_Status": "driver_error"}
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

            if enriched:
                _maybe_set_email(df, idx, enriched.get("Email"))
                if "Email_All" in enriched:
                    _set_email_all(df, idx, enriched.get("Email_All", ""), source="fb_global_pass", logger=logger)
                for col in ("Email_Type", "Facebook_URL"):
                    if col in enriched:
                        df.at[idx, col] = enriched.get(col, "")
                status_val = str(enriched.get("FB_Status", "") or "")
                if not status_val:
                    email_now = enriched.get("Email", "") or ""
                    fb_url_now = enriched.get("Facebook_URL", "") or ""
                    status_val = "ok" if (str(email_now).strip() or fb_url_now) else "no_candidates"
                df.at[idx, "FB_Status"] = status_val
            else:
                # Attempted but no enrichment result; mark as no_candidates to avoid repeated retries.
                df.at[idx, "FB_Status"] = "no_candidates"

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
    "FB_Status",
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
    final_export_csv: Optional[str] = None,
    woodpecker_export_csv: Optional[str] = None,
) -> None:
    export_logger = logger or logging.getLogger(__name__)
    if not input_csv or not os.path.exists(input_csv):
        export_logger.warning("[Master] Export skipped; input not found: %s", input_csv)
        return

    columns = list(export_columns) if export_columns is not None else list(DEFAULT_EXPORT_COLUMNS)
    export_logger.info("[Master] Exporting client-facing CSV: %s -> %s", input_csv, output_csv)
    if not final_export_csv:
        final_export_csv = os.path.join(os.path.dirname(os.path.abspath(input_csv)), "final_export.csv")
    else:
        final_export_csv = str(final_export_csv)
    if not woodpecker_export_csv:
        woodpecker_export_csv = os.path.join(os.path.dirname(os.path.abspath(input_csv)), "woodpecker_export.csv")
    else:
        woodpecker_export_csv = str(woodpecker_export_csv)

    _ensure_parent(output_csv)
    row_count = 0
    consolidated_df: Optional[pd.DataFrame] = None
    try:
        from final_checker import filter_rows_for_export

        consolidated_df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
        consolidated_df = consolidated_df.fillna("")
        consolidated_df = _consolidate_email_all(consolidated_df)
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

    try:
        # Final export and Woodpecker export views; legacy export kept for compatibility.
        write_final_and_woodpecker_exports(
            input_csv_path=input_csv,
            final_export_csv_path=final_export_csv,
            woodpecker_export_csv_path=woodpecker_export_csv,
            logger=export_logger,
            consolidated_df=consolidated_df,
        )
    except Exception as exc:  # pragma: no cover - defensive
        export_logger.error("[Master] Final export view failed safely: %s", exc)
