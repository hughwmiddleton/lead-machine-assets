from __future__ import annotations

from typing import Any

import pandas as pd

from source_scheduler import canonicalize_facebook_url, promote_facebook_url

FB_OPPORTUNITY_STATE_COL = "FB_Opportunity_State"
FB_GATE_STATE_COL = "FB_Gate_State"
FB_ATTEMPT_STATE_COL = "FB_Attempt_State"
FB_WRITE_STATE_COL = "FB_Write_State"
FB_DEBUG_REASON_COL = "FB_Debug_Reason"

FB_ATTRIBUTION_COLUMNS = (
    FB_OPPORTUNITY_STATE_COL,
    FB_GATE_STATE_COL,
    FB_ATTEMPT_STATE_COL,
    FB_WRITE_STATE_COL,
    FB_DEBUG_REASON_COL,
)


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
    if df is None:
        return df
    for col in FB_ATTRIBUTION_COLUMNS:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    return df


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
