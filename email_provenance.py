from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Mapping, MutableMapping
from urllib.parse import urlsplit

import pandas as pd

from email_normalizer import (
    filter_system_telemetry_emails,
    is_obvious_placeholder_email,
    normalize_email_value,
)

EMAIL_PROVENANCE_JSON_COL = "Email_Provenance_JSON"

_PROVENANCE_FIELDS = ("source_type", "surface", "source_url", "extract_method")
_WEBSITE_CONTACT_HINTS = (
    "/about",
    "/book",
    "/booking",
    "/contact",
    "/management",
    "/manager",
    "/media",
    "/press",
    "/team",
)
_SURFACE_PRIORITY = {
    "facebook_about": 0,
    "facebook_main": 1,
    "website_contact_page": 2,
    "website_homepage": 3,
    "instagram_profile": 10,
    "soundcloud_profile": 11,
    "bandcamp_contact_follow": 12,
    "bandcamp_profile": 13,
    "bandcamp_track_follow": 14,
    "lastfm_profile": 15,
    "spotify_profile": 16,
    "domain_reuse": 40,
    "live_search": 50,
}


def _clean_str(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _iter_email_tokens(values: Any) -> Iterable[str]:
    items = values if isinstance(values, (list, tuple, set)) else [values]
    for item in items:
        text = _clean_str(item)
        if not text:
            continue
        for token in re.split(r"[\s,;]+", text):
            if token:
                yield token


def normalize_email_key(email: Any) -> str:
    normalized = normalize_email_value(email)
    if not normalized:
        return ""
    filtered = filter_system_telemetry_emails([normalized])
    return filtered[0] if filtered else ""


def normalize_email_keys(values: Any) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for token in _iter_email_tokens(values):
        normalized = normalize_email_key(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        emails.append(normalized)
    return emails


def infer_email_surface(
    source_type: Any = "",
    source_url: Any = "",
    surface: Any = "",
) -> str:
    surface_clean = _clean_str(surface).lower()
    if surface_clean:
        return surface_clean

    source_type_clean = _clean_str(source_type).lower()
    source_url_clean = _clean_str(source_url)
    path = ""
    try:
        path = (urlsplit(source_url_clean).path or "").lower()
    except Exception:
        path = ""

    if source_type_clean.startswith("facebook"):
        if "/about" in path or "/contact" in path:
            return "facebook_about"
        return "facebook_main"
    if source_type_clean.startswith("website"):
        if any(token in path for token in _WEBSITE_CONTACT_HINTS):
            return "website_contact_page"
        return "website_homepage"
    if source_type_clean.startswith("instagram"):
        return "instagram_profile"
    if source_type_clean.startswith("soundcloud"):
        return "soundcloud_profile"
    if source_type_clean.startswith("bandcamp"):
        if "/track/" in path or "/album/" in path:
            return "bandcamp_track_follow"
        if any(token in path for token in _WEBSITE_CONTACT_HINTS):
            return "bandcamp_contact_follow"
        return "bandcamp_profile"
    if source_type_clean.startswith("lastfm"):
        return "lastfm_profile"
    if source_type_clean.startswith("spotify"):
        return "spotify_profile"
    if source_type_clean == "domain_reuse":
        return "domain_reuse"
    if source_type_clean == "live_search":
        return "live_search"
    return source_type_clean


def _build_provenance_entry(
    source_url: Any = "",
    source_type: Any = "",
    method: Any = "regex",
    surface: Any = "",
) -> Dict[str, str]:
    entry = {
        "source_type": _clean_str(source_type).lower(),
        "surface": infer_email_surface(source_type=source_type, source_url=source_url, surface=surface),
        "source_url": _clean_str(source_url),
        "extract_method": _clean_str(method or "regex") or "regex",
    }
    return {key: value for key, value in entry.items() if value}


def _provenance_sort_key(entry: Mapping[str, Any]) -> tuple[int, int, int, int, str, str]:
    surface = _clean_str(entry.get("surface", "")).lower()
    source_type = _clean_str(entry.get("source_type", "")).lower()
    source_url = _clean_str(entry.get("source_url", ""))
    extract_method = _clean_str(entry.get("extract_method", ""))
    completeness = sum(bool(_clean_str(entry.get(field, ""))) for field in _PROVENANCE_FIELDS)
    return (
        _SURFACE_PRIORITY.get(surface, 80 if surface or source_type or source_url else 99),
        0 if source_url else 1,
        0 if extract_method else 1,
        -completeness,
        source_type,
        source_url,
    )


def parse_email_provenance_json(raw_value: Any) -> Dict[str, Dict[str, str]]:
    if isinstance(raw_value, Mapping):
        payload = raw_value
    else:
        text = _clean_str(raw_value)
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
    if not isinstance(payload, Mapping):
        return {}

    parsed: Dict[str, Dict[str, str]] = {}
    for email_key, meta in payload.items():
        normalized_email = normalize_email_key(email_key)
        if not normalized_email or not isinstance(meta, Mapping):
            continue
        cleaned_meta = _build_provenance_entry(
            source_url=meta.get("source_url", ""),
            source_type=meta.get("source_type", ""),
            method=meta.get("extract_method", ""),
            surface=meta.get("surface", ""),
        )
        if cleaned_meta:
            parsed[normalized_email] = cleaned_meta
    return parsed


def get_row_email_provenance(row_like: Any) -> Dict[str, Dict[str, str]]:
    if row_like is None or not hasattr(row_like, "get"):
        return {}

    provenance = parse_email_provenance_json(row_like.get(EMAIL_PROVENANCE_JSON_COL, ""))
    selected_email = normalize_email_key(row_like.get("Email") or row_like.get("Primary Email") or "")
    if selected_email and selected_email not in provenance:
        fallback_entry = _build_provenance_entry(
            source_url=row_like.get("Email_Source_URL", ""),
            source_type=row_like.get("Email_Source_Type", "") or row_like.get("Email_Type", ""),
            method=row_like.get("Email_Extract_Method", "") or "regex",
        )
        if fallback_entry:
            provenance[selected_email] = fallback_entry
    return provenance


def get_email_provenance_entry(row_like: Any, email: Any) -> Dict[str, str]:
    normalized = normalize_email_key(email)
    if not normalized:
        return {}
    return dict(get_row_email_provenance(row_like).get(normalized) or {})


def row_has_successful_source_url_provenance(
    row_like: Any,
    *,
    source_type: str,
    source_url: Any,
    canonicalize_url,
) -> bool:
    """Return True only for a row-local usable email from the same source URL."""
    if row_like is None or not hasattr(row_like, "get"):
        return False

    requested_type = _clean_str(source_type).lower()
    if not requested_type:
        return False
    try:
        requested_canonical = _clean_str(canonicalize_url(source_url))
    except Exception:
        requested_canonical = ""
    if not requested_canonical:
        return False

    suspect_present = bool(
        _clean_str(row_like.get("Suspect_Email", ""))
        or _clean_str(row_like.get("Suspect_Email_All", ""))
    )
    if suspect_present:
        return False
    email_source = _clean_str(row_like.get("Email Source", ""))
    if email_source == "Quarantined (repeat email)":
        return False

    for email, meta in get_row_email_provenance(row_like).items():
        normalized_email = normalize_email_key(email)
        if not normalized_email or is_obvious_placeholder_email(normalized_email):
            continue
        provenance_type = _clean_str(meta.get("source_type", "")).lower()
        if not provenance_type.startswith(requested_type):
            continue
        try:
            provenance_canonical = _clean_str(canonicalize_url(meta.get("source_url", "")))
        except Exception:
            provenance_canonical = ""
        if provenance_canonical and provenance_canonical == requested_canonical:
            return True
    return False


def merge_email_provenance_map(
    existing_map: Mapping[str, Mapping[str, Any]] | None,
    emails: Any,
    *,
    source_url: Any = "",
    source_type: Any = "",
    method: Any = "regex",
    surface: Any = "",
) -> Dict[str, Dict[str, str]]:
    merged = parse_email_provenance_json(existing_map or {})
    normalized_emails = normalize_email_keys(emails)
    if not normalized_emails:
        return merged

    candidate_entry = _build_provenance_entry(
        source_url=source_url,
        source_type=source_type,
        method=method,
        surface=surface,
    )
    if not candidate_entry:
        return merged

    candidate_key = _provenance_sort_key(candidate_entry)
    for email in normalized_emails:
        current_entry = dict(merged.get(email) or {})
        if not current_entry:
            merged[email] = dict(candidate_entry)
            continue

        current_key = _provenance_sort_key(current_entry)
        if candidate_key < current_key:
            merged[email] = dict(candidate_entry)
            continue

        for field in _PROVENANCE_FIELDS:
            value = candidate_entry.get(field, "")
            if value and not current_entry.get(field, ""):
                current_entry[field] = value
        merged[email] = current_entry
    return merged


def dump_email_provenance_json(provenance_map: Mapping[str, Mapping[str, Any]] | None) -> str:
    cleaned = parse_email_provenance_json(provenance_map or {})
    if not cleaned:
        return ""
    ordered_payload = {email: cleaned[email] for email in sorted(cleaned)}
    return json.dumps(ordered_payload, sort_keys=True, separators=(",", ":"))


def merge_email_provenance_json(
    raw_value: Any,
    emails: Any,
    *,
    source_url: Any = "",
    source_type: Any = "",
    method: Any = "regex",
    surface: Any = "",
) -> str:
    merged = merge_email_provenance_map(
        parse_email_provenance_json(raw_value),
        emails,
        source_url=source_url,
        source_type=source_type,
        method=method,
        surface=surface,
    )
    return dump_email_provenance_json(merged)


def merge_email_provenance_into_target(
    target: Any,
    emails: Any,
    *,
    source_url: Any = "",
    source_type: Any = "",
    method: Any = "regex",
    surface: Any = "",
) -> None:
    if isinstance(target, MutableMapping):
        merged = merge_email_provenance_json(
            target.get(EMAIL_PROVENANCE_JSON_COL, ""),
            emails,
            source_url=source_url,
            source_type=source_type,
            method=method,
            surface=surface,
        )
        if merged or EMAIL_PROVENANCE_JSON_COL in target:
            target[EMAIL_PROVENANCE_JSON_COL] = merged
        return

    if (
        isinstance(target, tuple)
        and len(target) == 2
        and isinstance(target[0], pd.DataFrame)
    ):
        df, idx = target
        if EMAIL_PROVENANCE_JSON_COL not in df.columns:
            df[EMAIL_PROVENANCE_JSON_COL] = ""
        merged = merge_email_provenance_json(
            df.at[idx, EMAIL_PROVENANCE_JSON_COL],
            emails,
            source_url=source_url,
            source_type=source_type,
            method=method,
            surface=surface,
        )
        df.at[idx, EMAIL_PROVENANCE_JSON_COL] = merged


def _set_email_with_provenance(
    target,
    email: str,
    source_url: str = "",
    source_type: str = "",
    method: str = "regex",
    surface: str = "",
) -> None:
    filtered_emails = filter_system_telemetry_emails([email])
    if not filtered_emails:
        return
    email_clean = filtered_emails[0]

    merge_email_provenance_into_target(
        target,
        [email_clean],
        source_url=source_url,
        source_type=source_type,
        method=method,
        surface=surface,
    )

    if isinstance(target, MutableMapping):
        target["Email"] = email_clean

        if not str(target.get("Email_All", "")).strip():
            target["Email_All"] = email_clean

        if source_url and not str(target.get("Email_Source_URL", "")).strip():
            target["Email_Source_URL"] = source_url

        if source_type and not str(target.get("Email_Source_Type", "")).strip():
            target["Email_Source_Type"] = source_type

        if method and not str(target.get("Email_Extract_Method", "")).strip():
            target["Email_Extract_Method"] = method

        return

    if (
        isinstance(target, tuple)
        and len(target) == 2
        and isinstance(target[0], pd.DataFrame)
    ):
        df, idx = target

        if "Email" not in df.columns:
            df["Email"] = ""
        df.at[idx, "Email"] = email_clean

        if "Email_All" not in df.columns:
            df["Email_All"] = ""
        if not str(df.at[idx, "Email_All"]).strip():
            df.at[idx, "Email_All"] = email_clean

        if source_url:
            if "Email_Source_URL" not in df.columns:
                df["Email_Source_URL"] = ""
            if not str(df.at[idx, "Email_Source_URL"]).strip():
                df.at[idx, "Email_Source_URL"] = source_url

        if source_type:
            if "Email_Source_Type" not in df.columns:
                df["Email_Source_Type"] = ""
            if not str(df.at[idx, "Email_Source_Type"]).strip():
                df.at[idx, "Email_Source_Type"] = source_type

        if method:
            if "Email_Extract_Method" not in df.columns:
                df["Email_Extract_Method"] = ""
            if not str(df.at[idx, "Email_Extract_Method"]).strip():
                df.at[idx, "Email_Extract_Method"] = method
