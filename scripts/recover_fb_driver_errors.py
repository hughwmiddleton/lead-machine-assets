#!/usr/bin/env python3
"""Retry Facebook enrichment only for rows that failed with driver_error."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from email_provenance import get_row_email_provenance, normalize_email_keys  # noqa: E402
from fb_attribution import (  # noqa: E402
    FB_ATTEMPT_STATE_COL,
    FB_DEBUG_REASON_COL,
    FB_GATE_STATE_COL,
    FB_OPPORTUNITY_STATE_COL,
    FB_WRITE_STATE_COL,
    ensure_fb_attribution_columns,
)

try:  # noqa: E402
    from night_mode_fb import _classify_night_fb_attempt_state
except Exception:  # pragma: no cover - defensive import fallback
    _classify_night_fb_attempt_state = None  # type: ignore


ELIGIBLE_OPPORTUNITY_STATES = {
    "fb_opportunity_present",
    "fb_discovery_fallback_eligible",
}

RECOVERY_TRACE_COLUMNS = (
    "retry_attempted",
    "retry_success",
    "previous_fb_debug_reason",
    "new_FB_Status",
    "new_FB_Attempt_State",
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


@dataclass
class RecoverySummary:
    rows_scanned: int = 0
    candidates_found: int = 0
    retry_attempted: int = 0
    retry_success: int = 0
    fb_email_found: int = 0
    rows_unchanged: int = 0
    rows_written: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "rows_scanned": self.rows_scanned,
            "candidates_found": self.candidates_found,
            "retry_attempted": self.retry_attempted,
            "retry_success": self.retry_success,
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


def _email_values(row: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for col in ("Email", "Email_All"):
        values.extend(normalize_email_keys(_cell(row.get(col, ""))))
    return values


def has_usable_fb_email(row: Mapping[str, Any]) -> bool:
    emails = set(_email_values(row))
    if not emails:
        return False
    provenance = get_row_email_provenance(row)
    for email in emails:
        source_type = _cell((provenance.get(email) or {}).get("source_type", "")).lower()
        if source_type == "facebook_enrich":
            return True
    return False


def _fb_status_indicates_email_found(row: Mapping[str, Any]) -> bool:
    status = _cell(row.get("FB_Status", "")).lower()
    return bool(status and ("found_email" in status or status in {"found", "fb_enrich_found_email"}))


def _attempt_state_completed_success(row: Mapping[str, Any]) -> bool:
    attempt = _cell(row.get(FB_ATTEMPT_STATE_COL, "")).lower()
    return attempt in {"attempted_fb_found_email", "fb_enrich_found_email"} or (
        "success" in attempt and "driver_error" not in attempt
    )


def row_qualifies_for_recovery(row: Mapping[str, Any]) -> bool:
    debug_reason = _cell(row.get(FB_DEBUG_REASON_COL, "")).lower()
    if "driver_error" not in debug_reason:
        return False
    opportunity = _cell(row.get(FB_OPPORTUNITY_STATE_COL, "")).lower()
    if opportunity not in ELIGIBLE_OPPORTUNITY_STATES:
        return False
    if opportunity == "no_fb_opportunity":
        return False
    if _fb_status_indicates_email_found(row):
        return False
    if has_usable_fb_email(row):
        return False
    if _attempt_state_completed_success(row):
        return False
    return True


def _classify_attempt_state(status: str, existing: str) -> str:
    if callable(_classify_night_fb_attempt_state):
        return _classify_night_fb_attempt_state(status, existing)  # type: ignore[misc]
    status_norm = _cell(status).lower()
    if "driver_error" in status_norm or "timeout" in status_norm or "fetch_error" in status_norm:
        return "attempted_fb_timeout_or_fetch_error"
    if status_norm:
        return "attempted_fb_no_email_on_page"
    return _cell(existing) or "attempted_fb"


def _retry_completed(status: str, attempt_state: str) -> bool:
    status_norm = _cell(status).lower()
    attempt_norm = _cell(attempt_state).lower()
    if "driver_error" in status_norm or "unearthed_driver_error" in status_norm:
        return False
    if "driver_error" in attempt_norm:
        return False
    return bool(status_norm or attempt_norm.startswith("attempted_fb_"))


def _row_has_fb_email_after(row: Mapping[str, Any]) -> bool:
    return _fb_status_indicates_email_found(row) or has_usable_fb_email(row)


def _existing_non_fb_primary_email(row: Mapping[str, Any]) -> bool:
    if not normalize_email_keys(_cell(row.get("Email", ""))):
        return False
    return not has_usable_fb_email(row)


def _merge_email_all(existing: str, proposed: str) -> str:
    existing_emails = normalize_email_keys(existing)
    proposed_emails = normalize_email_keys(proposed)
    seen = set(existing_emails)
    merged = list(existing_emails)
    for email in proposed_emails:
        if email not in seen:
            seen.add(email)
            merged.append(email)
    return "; ".join(merged) if merged else _cell(existing or proposed)


def _merge_provenance_json(existing: Any, proposed: Any) -> str:
    def _load(raw: Any) -> Dict[str, Any]:
        text = _cell(raw)
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    merged = _load(existing)
    for email, meta in _load(proposed).items():
        if email not in merged:
            merged[email] = meta
    if not merged:
        return _cell(existing or proposed)
    return json.dumps(merged, sort_keys=True)


def _copy_allowed_result_columns(
    target: MutableMapping[str, Any],
    enriched: Mapping[str, Any],
    before: Mapping[str, Any],
) -> None:
    preserve_primary_email = _existing_non_fb_primary_email(before)
    preserve_primary_cols = {
        "Email",
        "Email_Type",
        "Email_Source_URL",
        "Email_Source_Type",
        "Email_Extract_Method",
    }
    allowed = set(FB_RESULT_COPY_COLUMNS) | set(EMAIL_WRITE_COLUMNS)
    for key, value in enriched.items():
        if key not in allowed and not str(key).startswith("FB_"):
            continue
        if preserve_primary_email and key in preserve_primary_cols:
            continue
        if preserve_primary_email and key == "Email_All":
            target[key] = _merge_email_all(_cell(before.get("Email_All", "")) or _cell(before.get("Email", "")), value)
            continue
        if preserve_primary_email and key == "Email_Provenance_JSON":
            target[key] = _merge_provenance_json(before.get("Email_Provenance_JSON", ""), value)
            continue
        target[key] = "" if value is None else value


class _DefaultFbEnricherBatch:
    def __init__(self, batch_size: int = 50) -> None:
        self.batch_size = max(int(batch_size or 50), 1)
        self._helper = None
        self._attempts_in_batch = 0

    def __call__(self, row: Dict[str, str], row_index: int) -> Dict[str, str]:
        if self._helper is None or self._attempts_in_batch >= self.batch_size:
            self.close()
            self._helper = self._new_helper()
            self._attempts_in_batch = 0
        self._attempts_in_batch += 1
        return self._helper.enrich_row_with_facebook_night(row, row_index=row_index)

    @staticmethod
    def _new_helper():
        from night_mode_fb import NightModeFacebookEnricher, create_night_fb_run_state
        from pipeline_runner import _load_legacy_module

        run_state = create_night_fb_run_state(
            os.environ.get("FB_USERNAME", "").strip(),
            os.environ.get("FB_PASSWORD", "").strip(),
        )
        return NightModeFacebookEnricher(
            _load_legacy_module(),
            os.environ.get("FB_USERNAME", "").strip(),
            os.environ.get("FB_PASSWORD", "").strip(),
            logger=print,
            use_shared_session=False,
            run_state=run_state,
        )

    def close(self) -> None:
        if self._helper is not None:
            close = getattr(self._helper, "close", None)
            if callable(close):
                close()
        self._helper = None


def _ensure_output_columns(out: pd.DataFrame) -> None:
    ensure_fb_attribution_columns(out)
    if "FB_Status" not in out.columns:
        out["FB_Status"] = ""
    if "Facebook_URL" not in out.columns:
        out["Facebook_URL"] = ""
    for col in RECOVERY_TRACE_COLUMNS:
        if col not in out.columns:
            out[col] = ""


def recover_dataframe(
    df: pd.DataFrame,
    *,
    fb_enricher: Optional[Callable[[Dict[str, str], int], Mapping[str, Any]]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    batch_size: int = 50,
) -> Tuple[pd.DataFrame, RecoverySummary]:
    if df is None:
        raise ValueError("df is required")
    out = df.copy(deep=True).fillna("")
    _ensure_output_columns(out)

    owned_enricher: Optional[_DefaultFbEnricherBatch] = None
    if fb_enricher is None and not dry_run:
        owned_enricher = _DefaultFbEnricherBatch(batch_size=batch_size)
        fb_enricher = owned_enricher

    summary = RecoverySummary(rows_scanned=len(out.index))
    changed_rows: set[int] = set()
    attempted_candidates = 0

    try:
        for idx in out.index:
            before = out.loc[idx].copy(deep=True)
            row = {col: _cell(out.at[idx, col]) for col in out.columns}
            if not row_qualifies_for_recovery(row):
                continue
            summary.candidates_found += 1
            if limit is not None and attempted_candidates >= limit:
                continue

            if dry_run:
                continue

            attempted_candidates += 1
            previous_debug = _cell(row.get(FB_DEBUG_REASON_COL, ""))
            out.at[idx, "retry_attempted"] = "true"
            out.at[idx, "retry_success"] = "false"
            out.at[idx, "previous_fb_debug_reason"] = previous_debug
            summary.retry_attempted += 1

            working = {col: _cell(out.at[idx, col]) for col in out.columns}
            try:
                enriched = fb_enricher({k: _cell(v) for k, v in working.items()}, int(idx)) if fb_enricher else working
            except Exception as exc:
                enriched = dict(working)
                enriched["FB_Status"] = "driver_error"
                enriched[FB_DEBUG_REASON_COL] = previous_debug or "driver_error"
                enriched["FB_Reason"] = str(exc)

            if enriched:
                merged = {col: _cell(out.at[idx, col]) for col in out.columns}
                _copy_allowed_result_columns(merged, enriched, before)
                for key, value in merged.items():
                    if key not in out.columns:
                        out[key] = ""
                    if (
                        key in RECOVERY_TRACE_COLUMNS
                        or key in FB_RESULT_COPY_COLUMNS
                        or key in EMAIL_WRITE_COLUMNS
                        or str(key).startswith("FB_")
                    ):
                        out.at[idx, key] = _cell(value)

            attempt_state = _cell(out.at[idx, FB_ATTEMPT_STATE_COL])
            if attempt_state == "attempted_fb" or not attempt_state:
                out.at[idx, FB_ATTEMPT_STATE_COL] = _classify_attempt_state(
                    out.at[idx, "FB_Status"] if "FB_Status" in out.columns else "",
                    attempt_state,
                )

            new_status = _cell(out.at[idx, "FB_Status"])
            new_attempt = _cell(out.at[idx, FB_ATTEMPT_STATE_COL])
            success = _retry_completed(new_status, new_attempt)
            out.at[idx, "retry_success"] = "true" if success else "false"
            out.at[idx, "new_FB_Status"] = new_status
            out.at[idx, "new_FB_Attempt_State"] = new_attempt
            if success:
                summary.retry_success += 1
            if _row_has_fb_email_after({col: _cell(out.at[idx, col]) for col in out.columns}):
                summary.fb_email_found += 1

            if not out.loc[idx].equals(before):
                changed_rows.add(int(idx))
    finally:
        if owned_enricher is not None:
            owned_enricher.close()

    summary.rows_unchanged = summary.rows_scanned - len(changed_rows)
    summary.rows_written = 0 if dry_run else len(out.index)
    return out, summary


def candidate_indices(df: pd.DataFrame, *, limit: Optional[int] = None) -> List[int]:
    if df is None:
        raise ValueError("df is required")
    out = df.copy(deep=True).fillna("")
    _ensure_output_columns(out)
    matches: List[int] = []
    for idx in out.index:
        row = {col: _cell(out.at[idx, col]) for col in out.columns}
        if row_qualifies_for_recovery(row):
            matches.append(int(idx))
            if limit is not None and len(matches) >= limit:
                break
    return matches


def recover_csv(
    input_csv: str,
    output_csv: Optional[str] = None,
    *,
    in_place: bool = False,
    fb_enricher: Optional[Callable[[Dict[str, str], int], Mapping[str, Any]]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    batch_size: int = 50,
) -> RecoverySummary:
    input_path = Path(input_csv)
    target_path = input_path if in_place else Path(output_csv or _default_output_path(input_csv))
    df = pd.read_csv(input_path, dtype=str, keep_default_na=False).fillna("")
    recovered, summary = recover_dataframe(
        df,
        fb_enricher=fb_enricher,
        limit=limit,
        dry_run=dry_run,
        batch_size=batch_size,
    )
    if not dry_run:
        recovered.to_csv(target_path, index=False)
    return summary


def _default_output_path(input_csv: str) -> str:
    path = Path(input_csv)
    return str(path.with_name(f"{path.stem}.fb_driver_recovered{path.suffix or '.csv'}"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Retry Night Mode FB enrichment for driver_error rows only.")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", help="Output CSV path. Defaults to .fb_driver_recovered.csv")
    parser.add_argument("--in-place", action="store_true", help="Overwrite the input CSV explicitly.")
    parser.add_argument("--limit", type=int, help="Maximum candidate rows to retry.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidate counts without writing or retrying.")
    parser.add_argument("--batch-size", type=int, default=50, help="Rows per fresh FB driver/session batch.")
    args = parser.parse_args(argv)

    output = args.output or _default_output_path(args.input)
    summary = recover_csv(
        args.input,
        output,
        in_place=bool(args.in_place),
        limit=args.limit,
        dry_run=bool(args.dry_run),
        batch_size=args.batch_size,
    )
    for key, value in summary.as_dict().items():
        print(f"{key}={value}")
    if args.dry_run:
        dry_df = pd.read_csv(args.input, dtype=str, keep_default_na=False).fillna("")
        candidates = candidate_indices(dry_df, limit=args.limit)
        print("candidate_rows=" + ",".join(str(idx) for idx in candidates))
        print("output=<dry-run>")
    elif args.in_place:
        print(f"output={args.input}")
    else:
        print(f"output={output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
