"""Shared helpers for email normalisation and obfuscation cleanup."""

from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

LoggerFn = Optional[Callable[[str], None]]

# Matches common obfuscations: local [at] domain, (at), or " at " with spacing.
_OBFUSCATED_EMAIL_PATTERN = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\bat\b)\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)


def normalize_obfuscated_email_patterns(text: str, logger: LoggerFn = None, max_logs: int = 3) -> Tuple[str, int]:
    """Replace common obfuscated email separators with '@'.

    Returns (normalized_text, replacements_made).
    """

    if not text:
        return text, 0

    replacements = 0
    log_budget = max_logs

    def _repl(match: re.Match) -> str:
        nonlocal replacements, log_budget
        before = match.group(0)
        normalized = f"{match.group(1)}@{match.group(2)}"
        replacements += 1
        if logger and log_budget > 0 and before != normalized:
            log_budget -= 1
            try:
                logger(f"[Email] normalized pattern: {before} -> {normalized}")
            except Exception:
                pass
        return normalized

    normalized = _OBFUSCATED_EMAIL_PATTERN.sub(_repl, text)
    # Remove incidental whitespace around '@' that tends to appear with obfuscations.
    normalized = re.sub(r"\s*@\s*", "@", normalized)
    return normalized, replacements


def normalize_email_value(value: str) -> str:
    """Lowercase + trim + collapse whitespace around '@'."""
    if value is None:
        return ""
    normalized = str(value).strip()
    if not normalized:
        return ""
    normalized = re.sub(r"\s*@\s*", "@", normalized)
    normalized = normalized.lower()
    if "@" not in normalized:
        return ""
    local, domain = normalized.split("@", 1)
    if not local or not domain or "." not in domain:
        return ""
    return normalized
