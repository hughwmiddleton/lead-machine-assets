"""Shared Facebook email skip-gate helpers."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, List, Optional, Tuple, Union

from email_normalizer import is_obvious_placeholder_email

LoggerFn = Optional[Union[Callable[[str], None], logging.Logger]]


def _cell_str(value: Any) -> str:
    try:
        import pandas as _pd  # type: ignore

        if _pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value or "").strip()


def _safe_log(logger: LoggerFn, message: str) -> None:
    if not logger:
        return
    try:
        if hasattr(logger, "info"):
            logger.info(message)  # type: ignore[union-attr]
        else:
            logger(message)  # type: ignore[misc]
    except Exception:
        pass


def _is_valid_email_shape(email: str) -> bool:
    if not email or " " in email:
        return False
    if email.count("@") != 1:
        return False
    local, domain = email.split("@", 1)
    return bool(local and domain and "." in domain)


def normalize_emails_for_fb_skip(value: Any) -> List[str]:
    text = "" if value is None else str(value)
    text = re.sub(r"\s*@\s*", "@", text)
    parts = re.split(r"[\s,;]+", text)
    seen: set[str] = set()
    cleaned: List[str] = []
    for part in parts:
        email = part.strip().lower()
        if not email or not _is_valid_email_shape(email) or email in seen:
            continue
        seen.add(email)
        cleaned.append(email)
    return sorted(cleaned)


def is_quarantined_repeat_email_row(row: Any) -> bool:
    email_source = _cell_str(row.get("Email Source"))
    suspect_email = _cell_str(row.get("Suspect_Email"))
    suspect_email_all = _cell_str(row.get("Suspect_Email_All"))
    email_val = _cell_str(row.get("Email"))
    email_all_val = _cell_str(row.get("Email_All"))
    suspect_present = bool(suspect_email or suspect_email_all)
    cleared_email_fields = email_val == "" and email_all_val == ""
    return email_source == "Quarantined (repeat email)" or (cleared_email_fields and suspect_present)


def row_has_usable_email_for_fb_skip(row: Any) -> Tuple[bool, str]:
    email_all_clean = _cell_str(row.get("Email_All"))
    suspect_email = _cell_str(row.get("Suspect_Email"))
    suspect_email_all = _cell_str(row.get("Suspect_Email_All"))
    suspect_present = bool(suspect_email or suspect_email_all)
    normalized_emails = normalize_emails_for_fb_skip(email_all_clean)
    has_email_raw = bool(email_all_clean)
    has_non_placeholder_email = any(not is_obvious_placeholder_email(email) for email in normalized_emails)
    if has_email_raw and not normalized_emails:
        has_non_placeholder_email = True
    quarantined_repeat = is_quarantined_repeat_email_row(row)
    return bool(has_non_placeholder_email and not quarantined_repeat and not suspect_present), email_all_clean


def should_skip_row_due_to_email_for_fb(
    row: Any, skip_rows_with_email: bool = True, logger: LoggerFn = None
) -> bool:
    has_email_effective, email_all_clean = row_has_usable_email_for_fb_skip(row)
    quarantined_repeat = is_quarantined_repeat_email_row(row)

    if quarantined_repeat and email_all_clean:
        row_id = row.get("__row_id", getattr(row, "name", None))
        artist = _cell_str(row.get("Artist Name"))
        _safe_log(
            logger,
            f"[FB SkipGate] allowing quarantined repeat-email row {row_id} ('{artist}') despite Email_All present",
        )
        return False

    return bool(skip_rows_with_email and has_email_effective)
