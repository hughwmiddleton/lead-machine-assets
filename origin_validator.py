#!/usr/bin/env python3
"""Origin-based auto validation for directory rows."""

from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pandas as pd
import requests
from html_fetcher import fetch_html, _detect_soft_block
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# Tunable thresholds
ARTIST_MATCH_THRESHOLD = 0.80
TITLE_MATCH_THRESHOLD = 0.80
UNCERTAIN_SCORE_LOW = 0.55
UNCERTAIN_SCORE_HIGH = 0.9
UPGRADE_MIN_SCORE = 0.75
BLOCK_MAX_SCORE = 0.85

# Column aliases
SOURCE_DIR_COLUMNS = ["Source Directory", "Source_Directory", "source_directory", "Source"]
SOURCE_URL_COLUMNS = ["Source URL", "Source_URL", "source_url", "Source Link"]

# Convenience logger type
Logger = Callable[[str], None]


@dataclass
class OriginMatchResult:
    origin_match_flag: bool
    artist_score: float
    title_score: float
    best_title_match: Optional[str]
    reason: str


def _log(logger: Optional[Logger], message: str) -> None:
    if not message:
        return
    if logger:
        try:
            logger(message)
            return
        except Exception:
            pass
    print(message)


def normalize_text(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9\s]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def simple_ratio(a: str, b: str) -> float:
    a_norm, b_norm = normalize_text(a), normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    try:
        return fuzz.ratio(a_norm, b_norm) / 100.0
    except Exception:
        return 0.0


def similarity_score(a: str, b: str) -> float:
    """
    Softer similarity:
    - token_set_ratio when available
    - substring bonus to handle title tails
    """
    a_norm, b_norm = normalize_text(a), normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm in b_norm or b_norm in a_norm:
        return 0.95
    try:
        return fuzz.token_set_ratio(a_norm, b_norm) / 100.0
    except Exception:
        return simple_ratio(a_norm, b_norm)


def extract_core_title(raw_title: str) -> str:
    """
    Clean a raw track title down to its 'core' for matching.

    Operations (in order):
    - Remove leading 'Artist - ' prefix if present.
    - Split on '|' and keep only the first segment.
    - Remove bracketed/parenthesised sections.
    - Remove trailing prod.* info.
    """
    if not raw_title:
        return ""

    title = str(raw_title)

    if " - " in title:
        _, right = title.split(" - ", 1)
        title = right

    if "|" in title:
        title = title.split("|", 1)[0]

    title = re.sub(r"\([^)]*\)", "", title)
    title = re.sub(r"\bprod\.?.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _fetch_html(url: str, session: requests.Session, cache: Dict[str, Optional[str]], logger: Optional[Logger]) -> Optional[str]:
    if not url:
        return None
    cached = cache.get(url)
    if cached is not None:
        return cached
    try:
        result = fetch_html(url, session=session, directory="origin_validator")
        html = result.get("html") or ""
        status = result.get("status")
        mode_used = result.get("mode_used")
        if html and (status is None or (status or 0) < 400):
            cache[url] = html
        elif html and mode_used == "playwright" and not _detect_soft_block(html):
            cache[url] = html
        else:
            cache[url] = None
        return cache[url]
    except Exception as exc:
        _log(logger, f"[Auto-Validate] Fetch failed for {url}: {exc}")
        cache[url] = None
        return None


def _extract_meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        meta = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if meta and meta.get("content"):
            return meta["content"]
    return ""


def _bandcamp_parse(html: str) -> Tuple[str, List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    artist = ""
    track_titles: List[str] = []

    meta_title = _extract_meta_content(soup, "og:title", "title")
    if meta_title:
        if " by " in meta_title:
            artist = meta_title.rsplit(" by ", 1)[-1]
        elif " | " in meta_title:
            artist = meta_title.split(" | ")[0]
        else:
            artist = meta_title

    heading = soup.find("h3", {"itemprop": "byArtist"}) or soup.find("span", {"itemprop": "byArtist"})
    if heading:
        artist = heading.get_text(" ", strip=True) or artist

    for selector in [
        ".trackTitle",
        "[itemprop='name']",
        ".title",
    ]:
        for tag in soup.select(selector):
            text = tag.get_text(" ", strip=True)
            if text:
                track_titles.append(text)

    if not track_titles:
        # Fallback: JSON blob inside data-tralbum
        for script in soup.find_all("script"):
            data = script.get_text()
            if "trackinfo" in data and "title" in data:
                for match in re.finditer(r'"title"\s*:\s*"([^"]+)"', data):
                    title = match.group(1)
                    if title:
                        track_titles.append(title)
    return artist, track_titles


def check_bandcamp_origin(url: str, artist_name: str, song_title: str, session: requests.Session, cache: Dict[str, Optional[str]], logger: Optional[Logger] = None) -> OriginMatchResult:
    html = _fetch_html(url, session, cache, logger)
    if not html:
        return OriginMatchResult(False, 0.0, 0.0, None, "fetch_error")
    artist, titles = _bandcamp_parse(html)
    artist_score = similarity_score(artist, artist_name)
    row_core_title = normalize_text(extract_core_title(song_title))
    best_title = ""
    best_score = 0.0
    for title in titles or []:
        page_core = normalize_text(extract_core_title(title))
        score = similarity_score(row_core_title, page_core)
        if score > best_score:
            best_score = score
            best_title = title
    if not best_score and song_title and song_title.lower() in (html or "").lower():
        best_score = 0.5
        best_title = song_title
    match_flag = artist_score >= ARTIST_MATCH_THRESHOLD and best_score >= TITLE_MATCH_THRESHOLD
    reason = "ok" if match_flag else ("artist_mismatch" if artist_score < ARTIST_MATCH_THRESHOLD else "title_not_found")
    return OriginMatchResult(match_flag, artist_score, best_score, best_title or None, reason)


def _soundcloud_parse(html: str) -> Tuple[str, List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    artist = ""
    track_titles: List[str] = []

    meta_title = _extract_meta_content(soup, "og:title", "twitter:title")
    if meta_title:
        if " - " in meta_title:
            artist = meta_title.split(" - ", 1)[0]
        elif " | " in meta_title:
            artist = meta_title.split(" | ", 1)[0]
        else:
            artist = meta_title

    profile_header = soup.select_one("a.profileHeaderInfo__userName, .profileHeaderInfo__userName")
    if profile_header:
        artist = profile_header.get_text(" ", strip=True) or artist

    for sel in [".soundTitle__title", "a[href*='/tracks/']", "[itemprop='name']"]:
        for tag in soup.select(sel):
            text = tag.get_text(" ", strip=True)
            if text:
                track_titles.append(text)
    if not track_titles:
        for match in re.finditer(r'"title"\s*:\s*"([^"]+)"', html or ""):
            val = match.group(1)
            if val:
                track_titles.append(val)
    return artist, track_titles


def check_soundcloud_origin(url: str, artist_name: str, song_title: str, session: requests.Session, cache: Dict[str, Optional[str]], logger: Optional[Logger] = None) -> OriginMatchResult:
    html = _fetch_html(url, session, cache, logger)
    if not html:
        return OriginMatchResult(False, 0.0, 0.0, None, "fetch_error")
    artist, titles = _soundcloud_parse(html)
    artist_score = similarity_score(artist, artist_name)
    row_core_title = normalize_text(extract_core_title(song_title))
    best_title = ""
    best_score = 0.0
    for title in titles or []:
        page_core = normalize_text(extract_core_title(title))
        score = similarity_score(row_core_title, page_core)
        if score > best_score:
            best_score = score
            best_title = title
    match_flag = artist_score >= ARTIST_MATCH_THRESHOLD and best_score >= TITLE_MATCH_THRESHOLD
    reason = "ok" if match_flag else ("artist_mismatch" if artist_score < ARTIST_MATCH_THRESHOLD else "title_not_found")
    return OriginMatchResult(match_flag, artist_score, best_score, best_title or None, reason)


def _unearthed_parse(html: str) -> Tuple[str, List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    artist = ""
    track_titles: List[str] = []
    meta_title = _extract_meta_content(soup, "og:title", "title")
    if meta_title:
        if "|" in meta_title:
            artist = meta_title.split("|")[0]
        else:
            artist = meta_title
    heading = soup.find("h1")
    if heading:
        artist = heading.get_text(" ", strip=True) or artist
    for cls in [".fRXHI", ".card", "[data-component='TrackCard']"]:
        for tag in soup.select(cls):
            text = tag.get_text(" ", strip=True)
            if text:
                track_titles.append(text)
    if not track_titles:
        for match in re.finditer(r'"trackTitle"\s*:\s*"([^"]+)"', html or "", re.IGNORECASE):
            val = match.group(1)
            if val:
                track_titles.append(val)
    return artist, track_titles


def check_unearthed_origin(url: str, artist_name: str, song_title: str, session: requests.Session, cache: Dict[str, Optional[str]], logger: Optional[Logger] = None) -> OriginMatchResult:
    html = _fetch_html(url, session, cache, logger)
    if not html:
        return OriginMatchResult(False, 0.0, 0.0, None, "fetch_error")
    artist, titles = _unearthed_parse(html)
    artist_score = similarity_score(artist, artist_name)
    row_core_title = normalize_text(extract_core_title(song_title))
    best_title = ""
    best_score = 0.0
    for title in titles or []:
        page_core = normalize_text(extract_core_title(title))
        score = similarity_score(row_core_title, page_core)
        if score > best_score:
            best_score = score
            best_title = title
    match_flag = artist_score >= ARTIST_MATCH_THRESHOLD and best_score >= TITLE_MATCH_THRESHOLD
    reason = "ok" if match_flag else ("artist_mismatch" if artist_score < ARTIST_MATCH_THRESHOLD else "title_not_found")
    return OriginMatchResult(match_flag, artist_score, best_score, best_title or None, reason)


def _spotify_parse(html: str) -> Tuple[str, List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    artist = ""
    track_titles: List[str] = []
    meta_title = _extract_meta_content(soup, "og:title", "twitter:title")
    meta_desc = _extract_meta_content(soup, "og:description", "description")
    if meta_title:
        if " - song and lyrics" in meta_title.lower():
            artist = meta_title.split(" - ", 1)[0]
        elif " | " in meta_title:
            parts = meta_title.split(" | ")
            if len(parts) >= 2:
                track_titles.append(parts[0])
                artist = parts[1]
        else:
            artist = meta_title
    if meta_desc and not artist:
        if "by" in meta_desc.lower():
            artist = meta_desc.split("by", 1)[-1]
    for match in re.finditer(r'"name"\s*:\s*"([^"]+)"', html or ""):
        candidate = match.group(1)
        if candidate:
            if not artist:
                artist = candidate
            track_titles.append(candidate)
            if len(track_titles) > 25:
                break
    return artist, track_titles


def check_spotify_origin(url: str, artist_name: str, song_title: str, session: requests.Session, cache: Dict[str, Optional[str]], logger: Optional[Logger] = None) -> OriginMatchResult:
    html = _fetch_html(url, session, cache, logger)
    if not html:
        return OriginMatchResult(False, 0.0, 0.0, None, "fetch_error")
    artist, titles = _spotify_parse(html)
    artist_score = similarity_score(artist, artist_name)
    row_core_title = normalize_text(extract_core_title(song_title))
    best_title = ""
    best_score = 0.0
    for title in titles or []:
        page_core = normalize_text(extract_core_title(title))
        score = similarity_score(row_core_title, page_core)
        if score > best_score:
            best_score = score
            best_title = title
    match_flag = artist_score >= ARTIST_MATCH_THRESHOLD and best_score >= TITLE_MATCH_THRESHOLD
    reason = "ok" if match_flag else ("artist_mismatch" if artist_score < ARTIST_MATCH_THRESHOLD else "title_not_found")
    return OriginMatchResult(match_flag, artist_score, best_score, best_title or None, reason)


def _normalise_status(value: str) -> str:
    if not value:
        return ""
    text = str(value).strip().upper()
    return text


def _has_contact_email(row: pd.Series) -> bool:
    """
    Treat any non-empty Email/Email_All as a valid contact signal.
    Used to avoid blocking rows where we already have a reachable contact.
    """
    for col in ("Email", "Email_All"):
        if col not in row:
            continue
        val = str(row.get(col, "") or "").strip()
        if val:
            return True
    return False


def _infer_directory_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
    except Exception:
        return ""
    if "bandcamp.com" in host:
        return "bandcamp"
    if "soundcloud.com" in host:
        return "soundcloud"
    if "triplejunearthed" in host or "abc.net.au" in host:
        return "unearthed"
    if "spotify.com" in host:
        return "spotify"
    return ""


def _normalise_source_directory(raw_dir: str) -> Optional[str]:
    raw = str(raw_dir or "")
    text = raw.strip().lower()
    if "soundcloud" in text:
        return "soundcloud"
    if "bandcamp" in text:
        return "bandcamp"
    if "unearthed" in text or "triple j" in text:
        return "unearthed"
    if "spotify" in text:
        return "spotify"
    return None


def _choose_checker(directory: str):
    mapping = {
        "bandcamp": check_bandcamp_origin,
        "soundcloud": check_soundcloud_origin,
        "unearthed": check_unearthed_origin,
        "spotify": check_spotify_origin,
    }
    key = _normalise_source_directory(directory)
    if not key and directory:
        key = _normalise_source_directory(_infer_directory_from_url(directory))
    return mapping.get(key)


def _ensure_column(df: pd.DataFrame, candidates: List[str], default: str = "") -> str:
    for name in candidates:
        if name in df.columns:
            df[name] = df[name].fillna("").astype(str)
            return name
    primary = candidates[0]
    df[primary] = default
    return primary


def _first_url_from_cell(value: str) -> str:
    if not value:
        return ""
    text = str(value)
    # Split on common delimiters
    parts = re.split(r"[;,|]", text)
    for part in parts:
        candidate = part.strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    return ""


def dedupe_pre_auto_validate(
    df: pd.DataFrame,
    source_dir_col: str,
    night_mode: bool = False,
    return_mapping: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, Dict[int, List[int]]]:
    """
    Removes duplicate rows before origin auto-validate using the composite key:
    normalised Artist Name + core Song Title + Source Directory (+ Email/first link if present).

    When ``return_mapping`` is True, also returns a mapping of deduped row index ->
    list of original DataFrame indices that shared the same composite key. This is
    used to re-apply validation results back onto the full (non-deduped) frame so
    the written CSV retains the original row count.
    """
    if df is None:
        return (df, {}) if return_mapping else df
    total_before = len(df.index)
    if total_before == 0:
        _log(None, "[Deduper] Removed 0 duplicate rows before Auto-Validate (kept 0 unique rows)")
        return df

    def _contact_key(row: pd.Series) -> str:
        email = str(row.get("Email", "") or "").strip().lower()
        if email:
            return email
        for col in ("External Links", "Spotify_URL", "SoundCloud Link", "Social Link"):
            if col not in row:
                continue
            url = _first_url_from_cell(row.get(col, ""))
            if url:
                return url.strip().lower()
        return ""

    def _song_key(row: pd.Series) -> str:
        raw = row.get("Song Title", "") or ""
        core = extract_core_title(str(raw))
        return normalize_text(core)

    seen = set()
    keep_indices: List[int] = []
    best_contact: Dict[Tuple[str, str, str, str], Tuple[int, Tuple[int, int, int]]] = {}
    composite_members: Dict[Tuple[str, str, str, str], List[int]] = {}

    def _night_mode_row_score(row: pd.Series) -> Tuple[int, int, int]:
        def _has_value(val: Any) -> int:
            if pd.isna(val):
                return 0
            text = str(val or "").strip()
            return 1 if text else 0

        has_email = _has_value(row.get("Email", ""))
        has_fb = _has_value(row.get("Facebook_URL", ""))
        has_social = _has_value(row.get("Social Link", ""))
        if not night_mode:
            return (has_email, 0, 0)
        return (has_email, has_fb, has_social)
    for idx, row in df.iterrows():
        artist_norm = normalize_text(str(row.get("Artist Name", "") or ""))
        track_norm = _song_key(row)
        source_raw = str(row.get(source_dir_col, "") or "")
        source_norm = _normalise_source_directory(source_raw) or normalize_text(source_raw)
        contact_norm = _contact_key(row)
        composite = (artist_norm, track_norm, source_norm, contact_norm)
        score = _night_mode_row_score(row)
        composite_members.setdefault(composite, []).append(idx)

        if composite in seen:
            prev_idx, prev_score = best_contact.get(composite, (-1, (0, 0, 0)))
            if score > prev_score:
                try:
                    keep_indices.remove(prev_idx)
                except ValueError:
                    pass
                keep_indices.append(idx)
                best_contact[composite] = (idx, score)
            continue
        seen.add(composite)
        best_contact[composite] = (idx, score)
        keep_indices.append(idx)

    deduped = df.loc[keep_indices].copy()
    deduped.reset_index(drop=True, inplace=True)
    removed = total_before - len(deduped.index)
    _log(None, f"[Deduper] Removed {removed} duplicate rows before Auto-Validate (kept {len(deduped.index)} unique rows)")

    if not return_mapping:
        return deduped

    # Map deduped row index -> original indices that shared the composite key
    mapping: Dict[int, List[int]] = {}
    best_idx_to_composite: Dict[int, Tuple[str, str, str, str]] = {}
    for composite, (best_idx, _) in best_contact.items():
        best_idx_to_composite[best_idx] = composite

    for dedup_idx, orig_idx in enumerate(keep_indices):
        composite = best_idx_to_composite.get(orig_idx)
        members = composite_members.get(composite, [orig_idx]) if composite else [orig_idx]
        mapping[dedup_idx] = members

    return deduped, mapping


def _select_rows_to_validate(df: pd.DataFrame, scope: str, match_col: str, status_col: str) -> Tuple[List[int], Dict[str, int]]:
    scope = (scope or "uncertain_only").strip().lower()
    reasons: Dict[str, int] = defaultdict(int)
    if scope == "all":
        indices = list(df.index)
        if indices:
            reasons["scope_all"] += len(indices)
        return indices, reasons
    selected: List[int] = []
    score_series = pd.to_numeric(df.get(match_col, 0), errors="coerce").fillna(0.0)
    status_series = df.get(status_col, "").fillna("").astype(str)
    for idx in df.index:
        score = float(score_series.iloc[idx]) if idx < len(score_series) else 0.0
        status = _normalise_status(status_series.iloc[idx]) if idx < len(status_series) else ""
        if status in {"REVIEW", "BLOCKED", "BLOCK"}:
            selected.append(idx)
            reasons["status_flagged"] += 1
            continue
        if UNCERTAIN_SCORE_LOW <= score <= UNCERTAIN_SCORE_HIGH:
            selected.append(idx)
            reasons["uncertain_score"] += 1
    return selected, reasons


def _update_status_with_origin(row: pd.Series, result: OriginMatchResult) -> Tuple[str, float]:
    status_value = _normalise_status(row.get("final_status", ""))
    current_score = 0.0
    try:
        current_score = float(row.get("match_score_overall", 0) or 0)
    except Exception:
        current_score = 0.0
    updated_status = status_value or ""
    updated_score = current_score
    has_email = _has_contact_email(row)
    if result.origin_match_flag:
        avg_score = (result.artist_score + result.title_score) / 2.0
        if avg_score > updated_score:
            updated_score = min(1.0, avg_score)
        if status_value in {"REVIEW", "BLOCK", "WARN", "BLOCKED"} and avg_score >= UPGRADE_MIN_SCORE:
            updated_status = "OK"
    else:
        # clear mismatches: low artist score or explicit mismatch
        if result.artist_score < 0.6 or result.reason == "artist_mismatch":
            if has_email:
                # keep reachable rows out of BLOCK; downgrade hard blocks to REVIEW
                if status_value in {"BLOCK", "BLOCKED", "BLOCKED_BY_ORIGIN"}:
                    updated_status = "REVIEW"
                else:
                    updated_status = status_value or "REVIEW"
            else:
                updated_status = "BLOCKED_BY_ORIGIN"
        else:
            # borderline: keep or downgrade to REVIEW, do not over-block
            if status_value not in {"BLOCKED_BY_ORIGIN", "BLOCKED"}:
                updated_status = "REVIEW"
            # if artist looks good but title is messy, prefer REVIEW
            if result.reason == "title_not_found" and result.artist_score >= 0.9 and result.title_score >= 0.7:
                updated_status = "REVIEW"
    return updated_status, updated_score


def _derive_origin_output_path(input_path: str) -> str:
    base, ext = os.path.splitext(input_path)
    if not ext:
        ext = ".csv"
    if base.endswith("_checked_origin"):
        return f"{base}{ext}"
    if base.endswith("_checked"):
        return f"{base}_origin{ext}"
    return f"{base}_origin{ext}"


def run_auto_validate(
    csv_path: str,
    output_path: Optional[str] = None,
    validate_scope: str = "uncertain_only",
    logger: Optional[Logger] = None,
    night_mode: bool = False,
) -> str:
    """
    Run origin-based auto validation on the given CSV.
    """
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    raw_df = pd.read_csv(csv_path)
    if raw_df.empty:
        _log(logger, "[Auto-Validate] CSV is empty; nothing to validate.")
        return csv_path

    source_dir_col = _ensure_column(raw_df, SOURCE_DIR_COLUMNS)
    source_url_col = _ensure_column(raw_df, SOURCE_URL_COLUMNS)

    df_deduped, dedupe_map = dedupe_pre_auto_validate(
        raw_df,
        source_dir_col,
        night_mode=night_mode,
        return_mapping=True,
    )

    for col in ("final_status", "match_score_overall", "origin_match_flag", "origin_match_reason", "origin_artist_score", "origin_title_score"):
        if col not in df_deduped.columns:
            df_deduped[col] = "" if col.endswith("_reason") else 0
    for col in ("origin_artist_score", "origin_title_score", "match_score_overall"):
        if col in df_deduped.columns:
            df_deduped[col] = pd.to_numeric(df_deduped[col], errors="coerce").fillna(0.0).astype(float)
    for col in ("final_status", "origin_match_reason"):
        if col in df_deduped.columns:
            df_deduped[col] = df_deduped[col].fillna("").astype(str)

    match_col = "match_score_overall"
    status_col = "final_status"
    validate_indices, selection_reasons = _select_rows_to_validate(df_deduped, validate_scope, match_col, status_col)
    _log(logger, f"[Auto-Validate] Loaded {len(df_deduped)} deduped rows (from {len(raw_df)}), validating {len(validate_indices)} selected rows...")
    if selection_reasons:
        reason_summary = ", ".join(f"{key}={selection_reasons[key]}" for key in sorted(selection_reasons))
    else:
        reason_summary = "none"
    _log(logger, f"[Auto-Validate] Selection reasons: {reason_summary}")

    html_cache: Dict[str, Optional[str]] = {}
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (AutoValidate)"})
    dir_counts: Dict[str, int] = {}
    upgraded = 0
    blocked = 0

    for idx in validate_indices:
        row = df_deduped.loc[idx]
        artist_name = str(row.get("Artist Name", "") or "").strip()
        song_title = str(row.get("Song Title", "") or "").strip()
        raw_source_dir = str(row.get(source_dir_col, "") or "")
        source_dir = raw_source_dir.strip()
        source_url = str(row.get(source_url_col, "") or "").strip()
        if not source_url:
            for alt_col in ("SoundCloud Link", "Social Link", "Spotify_URL", "External Links"):
                if alt_col in df.columns:
                    source_url = _first_url_from_cell(row.get(alt_col, ""))
                if source_url:
                    break
        if not source_dir:
            inferred_dir = _infer_directory_from_url(source_url)
            if inferred_dir:
                source_dir = inferred_dir
                df_deduped.at[idx, source_dir_col] = inferred_dir
        origin_type = _normalise_source_directory(source_dir)
        if not origin_type:
            inferred_dir = _infer_directory_from_url(source_url)
            origin_type = _normalise_source_directory(inferred_dir)
            if origin_type:
                df_deduped.at[idx, source_dir_col] = origin_type
        if not origin_type:
            if source_dir or source_url:
                df.at[idx, "origin_match_flag"] = 0
                df.at[idx, "origin_match_reason"] = "unsupported_directory"
                df.at[idx, "origin_artist_score"] = 0.0
                df.at[idx, "origin_title_score"] = 0.0
            continue
        if not source_url:
            continue
        checker = _choose_checker(origin_type)
        if not checker:
            continue
        dir_key = origin_type.lower()
        dir_counts[dir_key] = dir_counts.get(dir_key, 0) + 1
        try:
            result = checker(source_url, artist_name, song_title, session, html_cache, logger)
        except Exception as exc:
            _log(logger, f"[Auto-Validate] Error validating {source_url}: {exc}")
            result = OriginMatchResult(False, 0.0, 0.0, None, "parse_error")
        df_deduped.at[idx, "origin_match_flag"] = 1 if result.origin_match_flag else 0
        df_deduped.at[idx, "origin_match_reason"] = result.reason
        df_deduped.at[idx, "origin_artist_score"] = round(result.artist_score, 3)
        df_deduped.at[idx, "origin_title_score"] = round(result.title_score, 3)
        new_status, new_score = _update_status_with_origin(row, result)
        if not result.origin_match_flag and max(result.artist_score, result.title_score) >= 0.6:
            # debug-level logging if logger supports debug()
            if hasattr(logger, "debug"):
                try:
                    logger.debug(
                        "[Auto-Validate][Row %s] artist_row=%r artist_score=%.3f title_row=%r best_title=%r title_score=%.3f reason=%s",
                        idx,
                        artist_name,
                        result.artist_score,
                        song_title,
                        result.best_title_match,
                        result.title_score,
                        result.reason,
                    )
                except Exception:
                    pass
        if new_status and new_status != row.get("final_status", ""):
            df_deduped.at[idx, "final_status"] = new_status
            if new_status == "OK":
                upgraded += 1
            elif new_status == "BLOCKED_BY_ORIGIN":
                blocked += 1
        df_deduped.at[idx, "match_score_overall"] = new_score

    # Re-apply validation results back onto the original, non-deduped frame to
    # preserve all rows and their original order.
    df_out = raw_df.copy()
    cols_to_copy = [
        source_dir_col,
        "origin_match_flag",
        "origin_match_reason",
        "origin_artist_score",
        "origin_title_score",
        "final_status",
        "match_score_overall",
    ]
    for dedup_idx, orig_indices in dedupe_map.items():
        for col in cols_to_copy:
            if col not in df_out.columns:
                df_out[col] = "" if col.endswith("_reason") else 0
            value = df_deduped.at[dedup_idx, col] if col in df_deduped.columns else ""
            # Avoid dtype warnings by casting to objects before assignment
            df_out[col] = df_out[col].astype(object)
            df_out.loc[orig_indices, col] = value

    if output_path:
        target_path = output_path
    else:
        target_path = _derive_origin_output_path(csv_path)
    df_out.to_csv(target_path, index=False)
    for dir_name, count in dir_counts.items():
        _log(logger, f"[Auto-Validate] {dir_name}: checked {count} URL(s)")
    _log(logger, f"[Auto-Validate] Completed. Upgraded: {upgraded}, Blocked: {blocked}. Output: {target_path}")
    return target_path
