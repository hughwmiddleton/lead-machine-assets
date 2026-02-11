"""Shared email-override gating for Facebook candidates.

This helper is used by both the legacy/day Facebook flow and Night Mode to
decide whether an extracted email is sufficient to accept a candidate when
music signals were not found.
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple


SERVICE_CATEGORY_TOKENS = (
    "store",
    "shop",
    "boutique",
    "market",
    "restaurant",
    "cafe",
    "bar",
    "salon",
    "spa",
    "real estate",
    "agency",
    "marketing",
    "consulting",
    "school",
    "digital creator",
    "content creator",
    "public figure",
)

MUSIC_ROLE_TOKENS = (
    "musician/band",
    "musician",
    "band",
    "artist",
    "music",
    "singer",
    "producer",
    "dj",
    "rapper",
    "songwriter",
    "record label",
)


def _to_lower(text: Any) -> str:
    try:
        return str(text or "").strip().lower()
    except Exception:
        return ""


def _normalize_name(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", _to_lower(text))


def _classify_name_match(query: str, candidate_name: str) -> str:
    q = _normalize_name(query)
    c = _normalize_name(candidate_name)
    if not q or not c:
        return "unknown"
    if q == c:
        return "exact"
    if q in c or c in q:
        return "near"
    return "mismatch"


def _text_has_any(text: str, tokens: Tuple[str, ...]) -> bool:
    lowered = _to_lower(text)
    return any(tok in lowered for tok in tokens)


def _coerce_score(*vals: Any) -> float:
    for val in vals:
        try:
            if val is None:
                continue
            return float(val)
        except Exception:
            continue
    return 0.0


def should_accept_email_override(
    query: str,
    cand: Mapping[str, Any] | Any,
    extracted_signals: Mapping[str, Any] | None = None,
) -> Tuple[bool, str]:
    """
    Decide whether an email override may accept a Facebook candidate.

    Returns (accept: bool, reason: str) where reason is a deterministic
    code in the form `email_override_accept:<why>` or
    `email_override_reject:<why>`.
    """

    extracted = dict(extracted_signals or {})
    cand_obj: Mapping[str, Any]
    if isinstance(cand, Mapping):
        cand_obj = cand
    else:
        # Fallback for simple objects with attributes
        cand_obj = {k: getattr(cand, k, None) for k in ("name", "category", "score", "base_score", "music_hint")}

    name = cand_obj.get("name") or cand_obj.get("page_name") or ""
    category = cand_obj.get("category") or cand_obj.get("raw_category") or extracted.get("category") or ""
    score = _coerce_score(cand_obj.get("score"), cand_obj.get("base_score"), extracted.get("score"))

    has_music_signals = bool(extracted.get("has_music_signals") or extracted.get("page_music"))
    music_hint = bool(
        extracted.get("music_hint")
        or cand_obj.get("music_hint")
        or _text_has_any(category, MUSIC_ROLE_TOKENS)
        or _text_has_any(extracted.get("descriptor", ""), MUSIC_ROLE_TOKENS)
    )
    service_category = _text_has_any(category, SERVICE_CATEGORY_TOKENS)
    name_match = extracted.get("name_match") or _classify_name_match(query, name)

    if has_music_signals:
        return True, "email_override_accept:music_signals"

    if service_category and not music_hint:
        return False, "email_override_reject:service_category"

    if name_match == "mismatch":
        return False, "email_override_reject:name_mismatch"

    if music_hint:
        return True, "email_override_accept:music_hint"

    if name_match in ("exact", "near") and score > 0:
        return True, "email_override_accept:identity_softpass"

    return False, "email_override_reject:no_identity"
