#!/usr/bin/env python3
"""Recover missed Facebook /share/ rows from an existing master CSV."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fb_attribution import (  # noqa: E402
    FB_ATTEMPT_STATE_COL,
    FB_DEBUG_REASON_COL,
    FB_GATE_STATE_COL,
    FB_OPPORTUNITY_STATE_COL,
    FB_WRITE_STATE_COL,
)
from source_scheduler import canonicalize_facebook_url, ensure_canonical_facebook_url  # noqa: E402
from source_scheduler import _is_allowed_fb_share_entrypoint_url  # noqa: E402

try:  # noqa: E402
    from night_mode_fb import _classify_night_fb_attempt_state
except Exception:  # pragma: no cover - defensive import fallback
    _classify_night_fb_attempt_state = None  # type: ignore


DIRECT_FB_ALIAS_FIELDS = (
    "Facebook_URL",
    "facebook_url",
    "Facebook URL",
    "FB_URL",
    "facebook",
    "Facebook",
)

FB_SHARE_SOURCE_FIELDS = DIRECT_FB_ALIAS_FIELDS + (
    "Social Link",
    "External Links",
    "Website",
    "Websites",
    "Website URL",
)

EMAIL_WRITE_COLUMNS = {
    "Email",
    "Email_All",
    "Email_Type",
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
    "Email_Provenance_JSON",
    "FB_Email_Source",
    "FB_About_Attempted",
    "FB_About_Result",
    "__fb_emails_applied",
}

FB_RESULT_COPY_COLUMNS = {
    "Facebook_URL",
    "facebook_url",
    "Facebook URL",
    "FB_Status",
    "FB_Reason",
    "FB_Match_Level",
    "FB_Selected_By",
    "FB_Name_Consistency_Flag",
    "FB_Review_Reason",
    "FB_Refine_Decision",
    "FB_Refine_Executed",
    FB_GATE_STATE_COL,
    FB_ATTEMPT_STATE_COL,
    FB_WRITE_STATE_COL,
    FB_DEBUG_REASON_COL,
}

_URL_SPLIT_RE = re.compile(r"[\s|,;]+")


@dataclass
class RecoverySummary:
    rows_scanned: int = 0
    candidates: int = 0
    resolved: int = 0
    enriched: int = 0
    failed: int = 0
    skipped_existing: int = 0
    duration_sec: int = 0
    fb_email_found: int = 0
    rows_unchanged: int = 0
    rows_written: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "rows_scanned": self.rows_scanned,
            "candidates": self.candidates,
            "resolved": self.resolved,
            "enriched": self.enriched,
            "failed": self.failed,
            "skipped_existing": self.skipped_existing,
            "duration_sec": self.duration_sec,
            "fb_email_found": self.fb_email_found,
            "rows_unchanged": self.rows_unchanged,
            "rows_written": self.rows_written,
        }


def _cell(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value or "").strip()


def _row_value(row: Mapping[str, Any], field: str) -> str:
    for key, value in row.items():
        if str(key).lower() == field.lower():
            return _cell(value)
    return ""


def _iter_field_values(row: Mapping[str, Any], fields: Iterable[str]) -> Iterable[Tuple[str, str]]:
    seen: set[str] = set()
    for field in fields:
        key_l = field.lower()
        if key_l in seen:
            continue
        seen.add(key_l)
        value = _row_value(row, field)
        if value:
            yield field, value


def find_fb_share_url(row: Mapping[str, Any]) -> Tuple[str, str]:
    for field, value in _iter_field_values(row, FB_SHARE_SOURCE_FIELDS):
        for part in _URL_SPLIT_RE.split(value):
            candidate = part.strip().strip('"').strip("'")
            if not candidate:
                continue
            if "facebook.com/share/" not in candidate.lower():
                continue
            if _is_allowed_fb_share_entrypoint_url(candidate):
                return candidate, field
    return "", ""


def _has_existing_canonical_direct_fb(row: Mapping[str, Any]) -> bool:
    for field in DIRECT_FB_ALIAS_FIELDS:
        raw = _row_value(row, field)
        if not raw:
            continue
        canonical = canonicalize_facebook_url(raw)
        if canonical:
            return True
    return False


def _failed_share_state_present(row: Mapping[str, Any]) -> bool:
    for field in ("FB_Status", "state", FB_GATE_STATE_COL):
        if _row_value(row, field).lower() == "fb_share_resolution_failed":
            return True
    return False


def _fb_status_indicates_email_found(row: Mapping[str, Any]) -> bool:
    status = _cell(row.get("FB_Status", "")).lower()
    return bool(status and ("found_email" in status or status in {"found", "fb_enrich_found_email"}))


def _email_shape_present(value: str) -> bool:
    return bool(re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", value or ""))


def _has_fb_sourced_usable_email(row: Mapping[str, Any]) -> bool:
    email_blob = " ".join(_cell(row.get(col, "")) for col in ("Email", "Email_All"))
    if not _email_shape_present(email_blob):
        return False
    source_blob = " ".join(
        _cell(row.get(col, ""))
        for col in (
            "Email_Source_Type",
            "Email_Source_URL",
            "Email_Extract_Method",
            "Email_Provenance_JSON",
            "FB_Email_Source",
        )
    ).lower()
    return "facebook" in source_blob or "fb_" in source_blob


def _previous_fb_terminal_state(row: Mapping[str, Any]) -> bool:
    terminal_markers = {
        "found_email",
        "fb_enrich_found_email",
        "fb_no_email_written",
        "attempted_fb_no_email_on_page",
        "recovered_share_url",
    }
    fields = (
        "FB_Status",
        "state",
        FB_ATTEMPT_STATE_COL,
        FB_WRITE_STATE_COL,
        FB_GATE_STATE_COL,
        FB_DEBUG_REASON_COL,
    )
    for field in fields:
        value = _row_value(row, field).lower()
        if not value:
            continue
        if value in terminal_markers:
            return True
        if any(marker in value for marker in terminal_markers):
            return True
    return False


def row_qualifies_for_recovery(row: Mapping[str, Any]) -> bool:
    raw_share, _ = find_fb_share_url(row)
    if not raw_share:
        return False
    if not _failed_share_state_present(row):
        return False
    if _has_existing_canonical_direct_fb(row):
        return False
    if _previous_fb_terminal_state(row):
        return False
    if _fb_status_indicates_email_found(row):
        return False
    if _has_fb_sourced_usable_email(row):
        return False
    return True


def _promote_resolved_fb_url(row: MutableMapping[str, Any], resolved_fb_url: str) -> None:
    row["Facebook_URL"] = resolved_fb_url
    if "facebook_url" in row:
        row["facebook_url"] = resolved_fb_url
    if "Facebook URL" in row:
        row["Facebook URL"] = resolved_fb_url
    ensure_canonical_facebook_url(row, set_row=True)


class _DefaultShareResolver:
    def __init__(self) -> None:
        self._helper = None
        self._authed_session_available: Optional[bool] = None

    def __call__(self, raw_fb_url: str) -> Optional[str]:
        if self._helper is None:
            from night_mode_fb import NightModeFacebookEnricher, create_night_fb_run_state
            from pipeline_runner import _load_legacy_module

            run_state = create_night_fb_run_state(
                os.environ.get("FB_USERNAME", "").strip(),
                os.environ.get("FB_PASSWORD", "").strip(),
            )
            self._helper = NightModeFacebookEnricher(
                _load_legacy_module(),
                os.environ.get("FB_USERNAME", "").strip(),
                os.environ.get("FB_PASSWORD", "").strip(),
                logger=print,
                use_shared_session=True,
                run_state=run_state,
            )
        if self._authed_session_available is None:
            self._authed_session_available = bool(self._helper._has_authenticated_session())
        if not self._authed_session_available:
            return None
        return self._helper._resolve_pass_a_explicit_scrape_url(
            str(raw_fb_url or "").strip(),
            authed_session_available=True,
        )

    def close(self) -> None:
        if self._helper is not None:
            close = getattr(self._helper, "close", None)
            if callable(close):
                close()


class _DefaultFbEnricher:
    def __init__(self) -> None:
        self._helper = None

    def __call__(self, row: Dict[str, str], row_index: int) -> Dict[str, str]:
        if self._helper is None:
            from night_mode_fb import NightModeFacebookEnricher, create_night_fb_run_state
            from pipeline_runner import _load_legacy_module

            run_state = create_night_fb_run_state(
                os.environ.get("FB_USERNAME", "").strip(),
                os.environ.get("FB_PASSWORD", "").strip(),
            )
            self._helper = NightModeFacebookEnricher(
                _load_legacy_module(),
                os.environ.get("FB_USERNAME", "").strip(),
                os.environ.get("FB_PASSWORD", "").strip(),
                logger=print,
                use_shared_session=False,
                run_state=run_state,
            )
            enter = getattr(self._helper, "__enter__", None)
            if callable(enter):
                entered = enter()
                if entered is not None:
                    self._helper = entered
        return self._helper.enrich_row_with_facebook_night(row, row_index=row_index)

    def close(self) -> None:
        if self._helper is not None:
            close = getattr(self._helper, "close", None)
            if callable(close):
                close()


def _copy_allowed_result_columns(target: MutableMapping[str, Any], enriched: Mapping[str, Any]) -> None:
    allowed = set(FB_RESULT_COPY_COLUMNS) | set(EMAIL_WRITE_COLUMNS)
    for key, value in enriched.items():
        if key == "FB_Status":
            continue
        if key in allowed or str(key).startswith("FB_"):
            target[key] = "" if value is None else value


def _classify_attempt_state(status: str, existing: str) -> str:
    if callable(_classify_night_fb_attempt_state):
        return _classify_night_fb_attempt_state(status, existing)  # type: ignore[misc]
    status_norm = _cell(status).lower()
    if status_norm:
        return "attempted_fb_no_email_on_page"
    return _cell(existing) or "attempted_fb"


def _row_has_fb_email_after(row: Mapping[str, Any]) -> bool:
    if _fb_status_indicates_email_found(row):
        return True
    return _has_fb_sourced_usable_email(row)


def recover_dataframe(
    df: pd.DataFrame,
    *,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
    fb_enricher: Optional[Callable[[Dict[str, str], int], Mapping[str, Any]]] = None,
    offset: int = 0,
    limit: Optional[int] = None,
) -> Tuple[pd.DataFrame, RecoverySummary]:
    if df is None:
        raise ValueError("df is required")
    started = time.monotonic()
    out = df.copy(deep=True).fillna("")
    if "Facebook_URL" not in out.columns:
        raise ValueError("Input CSV must include Facebook_URL column for share recovery.")
    if "FB_Status" not in out.columns:
        raise ValueError("Input CSV must include FB_Status column for share recovery.")

    owned_resolver: Optional[_DefaultShareResolver] = None
    if share_resolver is None:
        owned_resolver = _DefaultShareResolver()
        resolver = owned_resolver
    else:
        resolver = share_resolver
    owned_enricher: Optional[_DefaultFbEnricher] = None
    if fb_enricher is None:
        owned_enricher = _DefaultFbEnricher()
        fb_enricher = owned_enricher

    summary = RecoverySummary(rows_scanned=len(out.index))
    changed_rows: set[int] = set()
    offset = max(int(offset or 0), 0)
    remaining = None if limit is None else max(int(limit), 0)
    candidate_ordinal = 0

    try:
        for idx in out.index:
            before = out.loc[idx].copy(deep=True)
            row = {col: _cell(out.at[idx, col]) for col in out.columns}
            raw_share, _source = find_fb_share_url(row)
            if raw_share and _failed_share_state_present(row) and _has_existing_canonical_direct_fb(row):
                summary.skipped_existing += 1
            if not row_qualifies_for_recovery(row):
                continue

            candidate_ordinal += 1
            if candidate_ordinal <= offset:
                continue
            if remaining is not None:
                if remaining <= 0:
                    continue
                remaining -= 1

            summary.candidates += 1
            raw_fb_url = raw_share

            resolved_raw = resolver(raw_fb_url)
            resolved_fb_url = canonicalize_facebook_url(resolved_raw)
            if not resolved_fb_url:
                summary.failed += 1
                continue

            summary.resolved += 1

            working = {col: _cell(out.at[idx, col]) for col in out.columns}
            _promote_resolved_fb_url(working, resolved_fb_url)
            if FB_OPPORTUNITY_STATE_COL in out.columns:
                working[FB_OPPORTUNITY_STATE_COL] = "fb_opportunity_present"
            if FB_ATTEMPT_STATE_COL in out.columns:
                working[FB_ATTEMPT_STATE_COL] = "attempted_fb"
            working["FB_Status"] = "recovered_share_url"

            for key, value in working.items():
                if key in out.columns and (key in FB_RESULT_COPY_COLUMNS or key in DIRECT_FB_ALIAS_FIELDS):
                    out.at[idx, key] = value

            summary.enriched += 1
            enriched = fb_enricher({k: _cell(v) for k, v in working.items()}, int(idx))
            if enriched:
                merged = {col: _cell(out.at[idx, col]) for col in out.columns}
                _copy_allowed_result_columns(merged, enriched)
                for key, value in merged.items():
                    if key in out.columns and (key in FB_RESULT_COPY_COLUMNS or key in EMAIL_WRITE_COLUMNS or str(key).startswith("FB_")):
                        out.at[idx, key] = value
            out.at[idx, "Facebook_URL"] = resolved_fb_url
            out.at[idx, "FB_Status"] = "recovered_share_url"

            attempt_state = _cell(out.at[idx, FB_ATTEMPT_STATE_COL]) if FB_ATTEMPT_STATE_COL in out.columns else ""
            if FB_ATTEMPT_STATE_COL in out.columns and attempt_state == "attempted_fb":
                out.at[idx, FB_ATTEMPT_STATE_COL] = _classify_attempt_state(
                    out.at[idx, "FB_Status"] if "FB_Status" in out.columns else "",
                    attempt_state,
                )
            if _row_has_fb_email_after({col: _cell(out.at[idx, col]) for col in out.columns}):
                summary.fb_email_found += 1

            if not out.loc[idx].equals(before):
                changed_rows.add(int(idx))
    finally:
        if owned_resolver is not None:
            owned_resolver.close()
        if owned_enricher is not None:
            owned_enricher.close()

    summary.rows_unchanged = summary.rows_scanned - len(changed_rows)
    summary.rows_written = len(out.index)
    summary.duration_sec = int(round(time.monotonic() - started))
    return out, summary


def recover_csv(
    input_csv: str,
    output_csv: str,
    *,
    in_place: bool = False,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
    fb_enricher: Optional[Callable[[Dict[str, str], int], Mapping[str, Any]]] = None,
    offset: int = 0,
    limit: Optional[int] = None,
) -> RecoverySummary:
    input_path = Path(input_csv)
    target_path = input_path if in_place else Path(output_csv)
    df = pd.read_csv(input_path, dtype=str, keep_default_na=False).fillna("")
    recovered, summary = recover_dataframe(
        df,
        share_resolver=share_resolver,
        fb_enricher=fb_enricher,
        offset=offset,
        limit=limit,
    )
    recovered.to_csv(target_path, index=False)
    return summary


def _default_output_path(input_csv: str) -> str:
    path = Path(input_csv)
    return str(path.with_name(f"{path.stem}__fb_share_recovered{path.suffix or '.csv'}"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Recover missed Facebook /share/ rows in an existing CSV.")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", help="Output CSV path. Defaults to a separate __fb_share_recovered.csv file.")
    parser.add_argument("--in-place", action="store_true", help="Overwrite the input CSV explicitly.")
    parser.add_argument("--force", action="store_true", help="Allow overwriting an existing --output/default copy.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many eligible recovery candidates before processing.")
    parser.add_argument("--limit", type=int, help="Process at most this many eligible recovery candidates.")
    args = parser.parse_args(argv)

    output = args.output or _default_output_path(args.input)
    if args.in_place and args.output:
        parser.error("--output cannot be combined with --in-place")
    if not args.in_place and Path(output).exists() and not args.force:
        parser.error(f"output already exists: {output} (use --force to overwrite)")
    summary = recover_csv(args.input, output, in_place=bool(args.in_place), offset=args.offset, limit=args.limit)
    print(
        "[FB Share Recovery] "
        f"candidates={summary.candidates} "
        f"resolved={summary.resolved} "
        f"enriched={summary.enriched} "
        f"failed={summary.failed} "
        f"skipped_existing={summary.skipped_existing} "
        f"duration_sec={summary.duration_sec}"
    )
    for key, value in summary.as_dict().items():
        print(f"{key}={value}")
    if args.in_place:
        print(f"output={args.input}")
    else:
        print(f"output={output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
