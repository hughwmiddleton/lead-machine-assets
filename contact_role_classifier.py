"""Deterministic, network-independent contact-role classification."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


ROLE_VALUES = (
    "artist_direct",
    "management",
    "booking",
    "label",
    "pr_publicity",
    "team_business",
    "unknown",
)

_REPRESENTATIVE_TERMS = {
    "management": ("management", "manager", "mgmt"),
    "booking": ("booking", "bookings", "agent"),
    "pr_publicity": ("press", "publicity", "publicist", "pr", "media"),
    "label": ("label", "records", "recordings"),
    "team_business": ("team", "business", "licensing", "licence", "sync"),
}
_DISTINCTIVE_COMPOUND_TERMS = {
    "management",
    "manager",
    "mgmt",
    "booking",
    "bookings",
    "publicity",
    "publicist",
    "recordings",
    "records",
    "licensing",
}
_CONTEXT_FIELDS = (
    "Contact_Role",
    "Contact Role",
    "Domain_Role",
    "Domain Role",
    "Organization_Type",
    "Organization Type",
)
_GENERIC_ARTIST_IDENTITIES = {"artist", "band", "music", "official", "theband"}


@dataclass(frozen=True)
class RoleClassification:
    role: str
    evidence: str


def _text(value: Any) -> str:
    try:
        text = str(value or "")
    except Exception:
        return ""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower().strip()


def _tokens(value: Any) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", _text(value)) if token}


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value))


def _matching_role(value: Any) -> tuple[str, str]:
    tokens = _tokens(value)
    compact = _compact(value)
    for role, terms in _REPRESENTATIVE_TERMS.items():
        for term in terms:
            if term in tokens or compact == term or (term in _DISTINCTIVE_COMPOUND_TERMS and term in compact):
                return role, term
    return "", ""


def _email_parts(email: Any) -> tuple[str, str]:
    normalized = _text(email)
    if normalized.count("@") != 1:
        return "", ""
    local, domain = normalized.split("@", 1)
    if not local or not domain:
        return "", ""
    return local, domain


def classify_email_role(
    email: Any,
    *,
    artist_name: Any = "",
    source_url: Any = "",
    row_context: Mapping[str, Any] | None = None,
) -> RoleClassification:
    """Classify one accepted email conservatively from stable row-local evidence."""
    local, domain = _email_parts(email)
    if not local:
        return RoleClassification("unknown", "no_valid_email")

    role, term = _matching_role(local)
    if role:
        return RoleClassification(role, f"local_part:{term}")

    domain_label = domain.split(".", 1)[0]
    role, term = _matching_role(domain_label)
    if role:
        return RoleClassification(role, f"domain:{term}")

    artist_compact = _compact(artist_name)
    local_compact = _compact(local)
    domain_compact = _compact(domain_label)
    if len(artist_compact) >= 5 and artist_compact not in _GENERIC_ARTIST_IDENTITIES:
        if artist_compact in local_compact:
            return RoleClassification("artist_direct", "identity:artist_name_in_local_part")
        if artist_compact in domain_compact:
            return RoleClassification("artist_direct", "identity:artist_name_in_domain")

    context = row_context if isinstance(row_context, Mapping) else {}
    for field in _CONTEXT_FIELDS:
        role, term = _matching_role(context.get(field, ""))
        if role:
            return RoleClassification(role, f"context:{field}:{term}")

    try:
        path = urlsplit(_text(source_url)).path
    except Exception:
        path = ""
    role, term = _matching_role(path)
    if role:
        return RoleClassification(role, f"source_path:{term}")

    return RoleClassification("unknown", "insufficient_evidence")
