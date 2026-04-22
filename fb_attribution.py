from __future__ import annotations

from typing import Any, Dict, Tuple

import pandas as pd

from source_scheduler import canonicalize_facebook_url, promote_facebook_url

FB_OPPORTUNITY_STATE_COL = "FB_Opportunity_State"
FB_GATE_STATE_COL = "FB_Gate_State"
FB_ATTEMPT_STATE_COL = "FB_Attempt_State"
FB_EXTRACT_STATE_COL = "FB_Extract_State"
FB_WRITE_STATE_COL = "FB_Write_State"
FB_DEBUG_REASON_COL = "FB_Debug_Reason"
FB_TERMINAL_REASON_COL = "FB_Terminal_Reason"

IG_OPPORTUNITY_STATE_COL = "IG_Opportunity_State"
IG_ATTEMPT_STATE_COL = "IG_Attempt_State"
IG_EXTRACT_STATE_COL = "IG_Extract_State"
IG_WRITE_STATE_COL = "IG_Write_State"
IG_TERMINAL_REASON_COL = "IG_Terminal_Reason"
IG_EXECUTION_PATH_COL = "IG_Execution_Path"
IG_SURFACE_REASON_COL = "IG_Surface_Reason"

FB_NORMALIZED_TERMINAL_OUTCOME_COL = "FB_Normalized_Terminal_Outcome"
FB_NORMALIZED_TERMINAL_REASON_COL = "FB_Normalized_Terminal_Reason"
IG_NORMALIZED_TERMINAL_OUTCOME_COL = "IG_Normalized_Terminal_Outcome"
IG_NORMALIZED_TERMINAL_REASON_COL = "IG_Normalized_Terminal_Reason"

SUCCESS_OUTCOME = "success"
GENUINE_NO_EMAIL_OUTCOME = "genuine_no_email"
PLATFORM_BLOCKED_OUTCOME = "platform_blocked_or_gated"
UNKNOWN_OUTCOME = "unknown_or_indeterminate"

FB_ATTRIBUTION_COLUMNS = (
    FB_OPPORTUNITY_STATE_COL,
    FB_GATE_STATE_COL,
    FB_ATTEMPT_STATE_COL,
    FB_EXTRACT_STATE_COL,
    FB_WRITE_STATE_COL,
    FB_DEBUG_REASON_COL,
    FB_TERMINAL_REASON_COL,
)

IG_ATTRIBUTION_COLUMNS = (
    IG_OPPORTUNITY_STATE_COL,
    IG_ATTEMPT_STATE_COL,
    IG_EXTRACT_STATE_COL,
    IG_WRITE_STATE_COL,
    IG_TERMINAL_REASON_COL,
    IG_EXECUTION_PATH_COL,
    IG_SURFACE_REASON_COL,
)

FB_NORMALIZATION_COLUMNS = (
    FB_NORMALIZED_TERMINAL_OUTCOME_COL,
    FB_NORMALIZED_TERMINAL_REASON_COL,
)

IG_NORMALIZATION_COLUMNS = (
    IG_NORMALIZED_TERMINAL_OUTCOME_COL,
    IG_NORMALIZED_TERMINAL_REASON_COL,
)


def _ensure_columns(df: pd.DataFrame, columns: Tuple[str, ...]) -> pd.DataFrame:
    if df is None:
        return df
    for col in columns:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    return df


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def ensure_fb_attribution_columns(df: pd.DataFrame) -> pd.DataFrame:
    return _ensure_columns(df, FB_ATTRIBUTION_COLUMNS + FB_NORMALIZATION_COLUMNS)


def ensure_ig_attribution_columns(df: pd.DataFrame) -> pd.DataFrame:
    return _ensure_columns(df, IG_ATTRIBUTION_COLUMNS + IG_NORMALIZATION_COLUMNS)


def canonical_fb_url_from_row(row: Any) -> str:
    if row is None:
        return ""
    raw = ""
    try:
        raw = _clean_text(row.get("Facebook_URL", ""))  # type: ignore[attr-defined]
    except Exception:
        raw = ""
    canonical = canonicalize_facebook_url(raw)
    if canonical:
        return canonical
    try:
        promoted = promote_facebook_url(row, set_row=False)
    except Exception:
        promoted = raw
    return canonicalize_facebook_url(promoted)


def classify_fb_opportunity_state(row: Any) -> str:
    return "fb_opportunity_present" if canonical_fb_url_from_row(row) else "no_fb_opportunity"


def apply_fb_opportunity_state_df(df: pd.DataFrame, *, overwrite: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return ensure_fb_attribution_columns(df)
    ensure_fb_attribution_columns(df)
    for idx in df.index:
        current = _clean_text(df.at[idx, FB_OPPORTUNITY_STATE_COL])
        if current and not overwrite:
            continue
        df.at[idx, FB_OPPORTUNITY_STATE_COL] = classify_fb_opportunity_state(df.loc[idx])
    return df


def _row_text(row_like: Any, column: str) -> str:
    if not hasattr(row_like, "get"):
        return ""
    return _clean_text(row_like.get(column, ""))


def _first_non_empty(*values: str) -> str:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return ""


def _fb_has_real_opportunity(opportunity_state: str) -> bool:
    return _clean_text(opportunity_state).lower() in {
        "fb_opportunity_present",
        "fb_discovery_fallback_eligible",
    }


def _fb_blocked_reason(row_like: Any) -> str:
    attempt_state = _row_text(row_like, FB_ATTEMPT_STATE_COL).lower()
    terminal_reason = _row_text(row_like, FB_TERMINAL_REASON_COL)
    if attempt_state == "attempted_fb_login_wall_or_checkpoint":
        return _first_non_empty(terminal_reason, "fb_login_required_or_blocked")
    if attempt_state == "attempted_fb_content_unavailable":
        return _first_non_empty(terminal_reason, "fb_content_unavailable")
    if terminal_reason in {"fb_login_required_or_blocked", "fb_content_unavailable"}:
        return terminal_reason
    return ""


def classify_fb_normalized_terminal(row_like: Any) -> Dict[str, str]:
    write_state = _row_text(row_like, FB_WRITE_STATE_COL).lower()
    attempt_state = _row_text(row_like, FB_ATTEMPT_STATE_COL).lower()
    extract_state = _row_text(row_like, FB_EXTRACT_STATE_COL).lower()
    opportunity_state = _row_text(row_like, FB_OPPORTUNITY_STATE_COL)
    terminal_reason = _row_text(row_like, FB_TERMINAL_REASON_COL)
    gate_state = _row_text(row_like, FB_GATE_STATE_COL)

    if write_state in {"fb_wrote_email", "fb_wrote_email_all_only"}:
        return {
            "outcome": SUCCESS_OUTCOME,
            "reason": _first_non_empty(terminal_reason, write_state),
        }

    blocked_reason = _fb_blocked_reason(row_like)
    if blocked_reason:
        return {
            "outcome": PLATFORM_BLOCKED_OUTCOME,
            "reason": blocked_reason,
        }

    if (
        _fb_has_real_opportunity(opportunity_state)
        and attempt_state == "attempted_fb_no_email_on_page"
        and extract_state == "fb_no_usable_email_found"
        and write_state == "fb_no_email_written"
    ):
        return {
            "outcome": GENUINE_NO_EMAIL_OUTCOME,
            "reason": _first_non_empty(terminal_reason, "fb_no_email_found"),
        }

    return {
        "outcome": UNKNOWN_OUTCOME,
        "reason": _first_non_empty(
            terminal_reason,
            gate_state,
            write_state,
            extract_state,
            attempt_state,
            opportunity_state,
            "fb_indeterminate",
        ),
    }


def _ig_surface_reason_is_platform_limited(surface_reason: str) -> bool:
    surface_reason_norm = _clean_text(surface_reason).lower()
    if not surface_reason_norm:
        return False
    if surface_reason_norm.startswith("profile_fetch_http_"):
        return True
    return surface_reason_norm in {
        "bridge_not_profile_surface_or_unavailable",
        "profile_fetch_soft_block",
        "profile_fetch_unusable_surface",
    }


def _ig_attempt_state_has_real_attempt(attempt_state: str) -> bool:
    return _clean_text(attempt_state).lower().startswith("attempted_ig")


def classify_ig_normalized_terminal(row_like: Any) -> Dict[str, str]:
    write_state = _row_text(row_like, IG_WRITE_STATE_COL).lower()
    attempt_state = _row_text(row_like, IG_ATTEMPT_STATE_COL).lower()
    extract_state = _row_text(row_like, IG_EXTRACT_STATE_COL).lower()
    opportunity_state = _row_text(row_like, IG_OPPORTUNITY_STATE_COL).lower()
    terminal_reason = _row_text(row_like, IG_TERMINAL_REASON_COL)
    surface_reason = _row_text(row_like, IG_SURFACE_REASON_COL)
    if not _ig_attempt_state_has_real_attempt(attempt_state):
        surface_reason = ""

    if write_state in {"ig_wrote_email", "ig_wrote_email_all_only"}:
        return {
            "outcome": SUCCESS_OUTCOME,
            "reason": _first_non_empty(terminal_reason, write_state),
        }

    if attempt_state == "attempted_ig_blocked_or_unavailable" or terminal_reason == "ig_blocked_or_unavailable":
        return {
            "outcome": PLATFORM_BLOCKED_OUTCOME,
            "reason": _first_non_empty(surface_reason, terminal_reason, attempt_state),
        }

    if _ig_surface_reason_is_platform_limited(surface_reason):
        return {
            "outcome": PLATFORM_BLOCKED_OUTCOME,
            "reason": surface_reason,
        }

    if (
        opportunity_state == "ig_opportunity_present"
        and attempt_state == "attempted_ig_no_email_found"
        and extract_state == "ig_no_usable_email_found"
        and write_state == "ig_no_email_written"
    ):
        return {
            "outcome": GENUINE_NO_EMAIL_OUTCOME,
            "reason": _first_non_empty(terminal_reason, "ig_no_email_found"),
        }

    return {
        "outcome": UNKNOWN_OUTCOME,
        "reason": _first_non_empty(
            terminal_reason,
            surface_reason,
            write_state,
            extract_state,
            attempt_state,
            opportunity_state,
            "ig_indeterminate",
        ),
    }


def finalize_fb_normalized_terminal(df: pd.DataFrame, row_idx: Any) -> None:
    if df is None or row_idx not in getattr(df, "index", []):
        return
    ensure_fb_attribution_columns(df)
    normalized = classify_fb_normalized_terminal(df.loc[row_idx])
    df.at[row_idx, FB_NORMALIZED_TERMINAL_OUTCOME_COL] = normalized["outcome"]
    df.at[row_idx, FB_NORMALIZED_TERMINAL_REASON_COL] = normalized["reason"]


def finalize_ig_normalized_terminal(df: pd.DataFrame, row_idx: Any) -> None:
    if df is None or row_idx not in getattr(df, "index", []):
        return
    ensure_ig_attribution_columns(df)
    if not _ig_attempt_state_has_real_attempt(_row_text(df.loc[row_idx], IG_ATTEMPT_STATE_COL)):
        df.at[row_idx, IG_SURFACE_REASON_COL] = ""
    normalized = classify_ig_normalized_terminal(df.loc[row_idx])
    df.at[row_idx, IG_NORMALIZED_TERMINAL_OUTCOME_COL] = normalized["outcome"]
    df.at[row_idx, IG_NORMALIZED_TERMINAL_REASON_COL] = normalized["reason"]


def apply_platform_terminal_normalization_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        ensure_fb_attribution_columns(df)
        ensure_ig_attribution_columns(df)
        return df
    ensure_fb_attribution_columns(df)
    ensure_ig_attribution_columns(df)
    for idx in df.index:
        finalize_fb_normalized_terminal(df, idx)
        finalize_ig_normalized_terminal(df, idx)
    return df
