"""Shared helpers for email normalisation and obfuscation cleanup."""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional, Tuple

LoggerFn = Optional[Callable[[str], None]]

# Matches common obfuscations: local [at] domain, (at), or " at " with spacing.
_OBFUSCATED_EMAIL_PATTERN = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\bat\b)\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
_EMAIL_VALUE_SPLIT_RE = re.compile(r"[\s,;|]+")
_OBVIOUS_PLACEHOLDER_EMAILS = frozenset(
    {
        "email@example.com",
        "example@example.com",
        "user@domain.com",
        "name@example.com",
        "test@example.com",
        "test@test.com",
        "user@example.com",
        "you@example.com",
    }
)

# Local parts that, when paired with a platform domain, indicate a platform-owned
# support/admin/automated address rather than an artist contact email.
_PLATFORM_SUPPORT_LOCAL_PARTS = frozenset(
    {
        "support",
        "help",
        "noreply",
        "no-reply",
        "no_reply",
        "abuse",
        "security",
        "admin",
        "postmaster",
        "hostmaster",
        "webmaster",
        "root",
        "legal",
        "privacy",
        "dpo",
        "copyright",
        "dmca",
        "phishing",
        "feedback",
        "suggestions",
        "complaints",
        "donotreply",
        "do-not-reply",
    }
)

# Platform domains where the above local parts should be rejected.
# Includes both exact matches and suffix matches (e.g. *.bandcamp.com).
_PLATFORM_SUPPORT_DOMAINS_EXACT = frozenset(
    {
        "bandcamp.com",
        "get.bandcamp.help",
        "help.bandcamp.com",
        "bandcamp.help",
        "soundcloud.com",
        "facebook.com",
        "fb.com",
        "instagram.com",
        "instagr.am",
        "spotify.com",
        "last.fm",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "linktr.ee",
        "beacons.ai",
        "mailchimp.com",
        "list-manage.com",
        "substack.com",
        "squarespace.com",
        "wix.com",
    }
)

_PLATFORM_SUPPORT_DOMAINS_SUFFIX = frozenset(
    {
        ".bandcamp.com",
        ".soundcloud.com",
        ".facebook.com",
        ".fb.com",
        ".instagram.com",
        ".instagr.am",
        ".spotify.com",
        ".last.fm",
        ".youtube.com",
        ".youtu.be",
        ".tiktok.com",
        ".twitter.com",
        ".x.com",
        ".linktr.ee",
        ".beacons.ai",
        ".mailchimp.com",
        ".list-manage.com",
        ".substack.com",
        ".squarespace.com",
        ".wix.com",
    }
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


def is_obvious_placeholder_email(value: str) -> bool:
    """Return True for a tiny set of exact placeholder emails."""
    normalized = normalize_email_value(value)
    if not normalized:
        return False
    return normalized in _OBVIOUS_PLACEHOLDER_EMAILS


def filter_obvious_placeholder_emails(
    values: Iterable[str] | str | None,
) -> list[str]:
    """Normalize, dedupe, and drop exact obvious placeholder emails."""
    if values is None:
        return []

    if isinstance(values, str):
        raw_items = _EMAIL_VALUE_SPLIT_RE.split(values)
    else:
        raw_items = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                raw_items.extend(_EMAIL_VALUE_SPLIT_RE.split(value))
            else:
                raw_items.append(str(value))

    filtered: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        normalized = normalize_email_value(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if is_obvious_placeholder_email(normalized):
            continue
        filtered.append(normalized)
    return filtered


def is_system_telemetry_email(value: str) -> bool:
    """Return True for known non-contact telemetry/system destinations."""
    normalized = normalize_email_value(value)
    if not normalized:
        return False
    _, domain = normalized.split("@", 1)
    return domain == "sentry.io" or domain.endswith(".sentry.io")


def is_platform_support_email(value: str) -> bool:
    """Return True for platform-owned support/admin/automated addresses.

    Rejects emails such as support@*.bandcamp.com, noreply@*.soundcloud.com,
    help@*.facebook.com, etc.  Does NOT reject artist-owned custom domains.
    """
    normalized = normalize_email_value(value)
    if not normalized:
        return False
    local, domain = normalized.split("@", 1)
    if local not in _PLATFORM_SUPPORT_LOCAL_PARTS:
        return False
    if domain in _PLATFORM_SUPPORT_DOMAINS_EXACT:
        return True
    if any(domain.endswith(suffix) for suffix in _PLATFORM_SUPPORT_DOMAINS_SUFFIX):
        return True
    return False


def filter_platform_support_emails(values: Iterable[str] | str | None) -> list[str]:
    """Normalize, dedupe, and drop platform support/admin emails."""
    if values is None:
        return []

    if isinstance(values, str):
        raw_items = _EMAIL_VALUE_SPLIT_RE.split(values)
    else:
        raw_items = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                raw_items.extend(_EMAIL_VALUE_SPLIT_RE.split(value))
            else:
                raw_items.append(str(value))

    filtered: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        normalized = normalize_email_value(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if is_platform_support_email(normalized):
            continue
        filtered.append(normalized)
    return filtered


def filter_system_telemetry_emails(values: Iterable[str] | str | None) -> list[str]:
    """Normalize, dedupe, and drop known telemetry/system emails."""
    if values is None:
        return []

    if isinstance(values, str):
        raw_items = _EMAIL_VALUE_SPLIT_RE.split(values)
    else:
        raw_items = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                raw_items.extend(_EMAIL_VALUE_SPLIT_RE.split(value))
            else:
                raw_items.append(str(value))

    filtered: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        normalized = normalize_email_value(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if is_system_telemetry_email(normalized):
            continue
        filtered.append(normalized)
    return filtered
