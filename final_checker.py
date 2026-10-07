import datetime
import json
import logging
import os
import re
from urllib.parse import urlparse, unquote
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd
from rapidfuzz import fuzz
from unidecode import unidecode

from email_normalizer import (
    is_platform_support_email,
    is_system_telemetry_email,
    normalize_email_value,
)
from email_provenance import (
    dump_email_provenance_json,
    get_row_email_provenance,
    infer_email_surface,
    normalize_email_keys,
)
from source_scheduler import canonicalize_facebook_url

"""
Status semantics. Two questions are kept deliberately separate:

  1. CONTACT SAFETY    is this email tied to the intended entity and safe to use?
                       (see classify_contact_attribution)
  2. ENRICHMENT QUALITY how complete/confident was the rest of the enrichment?

- OK   : high-confidence identity, no serious conflicts; safe to auto-use.
- WARN : usable but warrants human review - missing contact/context, softer
         duplicate signals, cross-directory slug disagreement, or a third-party
         organisational inbox. No hard contradiction or unsafe condition.
- BLOCK: reserved for genuine contact-safety failures - identity contradiction,
         label/show-style entity, unusable/platform email, private-route or
         rejected-Facebook-surface provenance, hard duplicate conflict.

A weak enrichment signal alone (title mismatch, failed Facebook discovery,
directory slug disagreement, genre rarity) must never turn a safely sourced
email into BLOCK.

Export profiles (used by pipeline_runner.export_master_leads):
- studio_safe      : only OK rows with reachable email contacts.
- studio_plus      : OK/WARN rows with email, plus high-signal Unearthed rows.
- unearthed_social : Unearthed rows flagged as email-less for social scraping.
- full_dump        : keep every row regardless of status/contact.
"""

LOGGER = logging.getLogger(__name__)

FRESH_YEAR_CUTOFF = 2023
STALE_DOWNGRADE_ENABLED_DEFAULT = True

GENRE_FIXUPS = {
    "hipdut": "hip-hop",
    "uk, alt-country": "alt-country",
    "uk alt-country": "alt-country",
}

GENRE_NOISE_SUBSTRINGS = [
    "podcast",
    "radio show",
    "dj mix",
    "sessions",
    "remix",
    "radio",
    "show",
    "out now",
    "album",
    "single",
]

GENRE_LOCATION_TOKENS = {
    "uk",
    "united kingdom",
    "australia",
    "sydney",
    "melbourne",
    "perth",
    "london",
}


def _normalise_status(value: str) -> str:
    if not value:
        return ""
    return str(value).strip().upper()


def normalize_primary_genre(raw_genre: str) -> str:
    if raw_genre is None:
        return ""
    text = str(raw_genre).strip()
    if not text:
        return ""
    lower_full = text.lower()
    if lower_full in GENRE_FIXUPS:
        text = GENRE_FIXUPS[lower_full]
    tokens = re.split(r'[\/,"]+', text)
    cleaned: list[str] = []
    for token in tokens:
        if not token:
            continue
        candidate = token.strip()
        if not candidate:
            continue
        lower = candidate.lower()
        fixed = GENRE_FIXUPS.get(lower, GENRE_FIXUPS.get(candidate, candidate))
        lower_fixed = fixed.lower()
        if any(noise in lower_fixed for noise in GENRE_NOISE_SUBSTRINGS):
            continue
        if lower_fixed in GENRE_LOCATION_TOKENS:
            continue
        if lower_fixed not in cleaned:
            cleaned.append(lower_fixed)
    if not cleaned:
        return ""
    return ", ".join(cleaned[:2])


def looks_like_label_or_show(row: dict) -> bool:
    name = _safe_lower(row.get("Artist Name"))
    title = _safe_lower(row.get("Song Title"))
    label_words = [
        "recordings",
        "records",
        "sessions",
        "radio",
        "podcast",
        "sound system",
        "sound system",
        "group",
        "collective",
    ]
    return any(w in name for w in label_words) or any(w in title for w in label_words)


def _boolish(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "t"}


def _has_valid_email(row: dict) -> bool:
    for key in ("Email", "Email_All"):
        val = str(row.get(key, "") or "").strip()
        if "@" in val:
            return True
    return False


def _cell_text(value) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    try:
        return str(value or "")
    except Exception:
        return ""


def _safe_lower(value) -> str:
    return _cell_text(value).lower()


def _optional_int_flag(value) -> Optional[int]:
    """NaN-safe parse of an optional 0/1 column into int, or None when unset.

    Tolerates float-formatted integers ("0.0", 0.0) because the same column is
    read back both from ``pd.read_csv`` defaults (floats/NaN) and from
    ``dtype=str`` reads (strings).
    """
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        # Note: not via _cell_text, which reads a numeric 0 as empty.
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


NAME_CONSISTENCY_FLAG_COL = "name_consistency_flag"
NAME_CONSISTENCY_POLARITY_COL = "name_consistency_flag_polarity"
NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1 = "consistent_is_1"


def read_name_consistency_flag(row) -> Optional[int]:
    """Read ``name_consistency_flag`` in canonical semantics: 1 = consistent.

    Artifacts written before the polarity fix stored 1 = *mismatch*; artifacts
    written after it carry ``name_consistency_flag_polarity`` and store
    1 = consistent.  Absence of that marker therefore identifies a legacy
    artifact explicitly, rather than guessing from the data.

    Returns None when the value is absent, unparseable, or when a legacy value
    cannot be mapped without guessing:

    - marker present  -> the stored value is canonical, used as-is.
    - marker absent, stored 1 -> 0. Legacy truth is "mismatch"; this is also the
      safe reading if a marker were ever lost from a current artifact, because
      it only forgoes a promotion.
    - marker absent, stored 0 -> None. Ambiguous between "legacy: consistent"
      and "current: inconsistent", so it is reported unknown, not guessed.

    Net invariant: only an artifact carrying the polarity marker can drive a
    name-consistency-based promotion.
    """
    if row is None or not hasattr(row, "get"):
        return None
    value = _optional_int_flag(row.get(NAME_CONSISTENCY_FLAG_COL, None))
    if value is None:
        return None
    marker = _safe_lower(row.get(NAME_CONSISTENCY_POLARITY_COL, "")).strip()
    if marker == NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1:
        return 1 if value == 1 else 0
    if value == 1:
        return 0
    return None


ATTRIBUTION_NONE = ""
ATTRIBUTION_UNSAFE = "unsafe"
ATTRIBUTION_UNATTRIBUTED = "unattributed"
ATTRIBUTION_THIRD_PARTY = "third_party"
ATTRIBUTION_TRUSTED = "trusted"

ARTIST_IDENTITY_STATUS_COL = "Artist_Identity_Status"
IDENTITY_ASSESSMENT_JSON_COL = "Identity_Assessment_JSON"
PREFERRED_OUTREACH_EMAIL_COL = "Preferred_Outreach_Email"
ALTERNATE_EMAILS_COL = "Alternate_Emails"
PREFERRED_CONTACT_STATUS_COL = "Preferred_Contact_Status"
PREFERRED_CONTACT_REASON_COL = "Preferred_Contact_Reason"
APPROVED_CONTACT_EMAIL_COL = "Approved_Contact_Email"

_ATTRIBUTABLE_CONTACT = (ATTRIBUTION_TRUSTED, ATTRIBUTION_THIRD_PARTY)

# Provenance surfaces owned by, or directly attributable to, the entity itself.
_ATTRIBUTABLE_EMAIL_SURFACES = frozenset(
    {
        "facebook_about",
        "facebook_main",
        "website_contact_page",
        "website_homepage",
        "instagram_profile",
        "soundcloud_profile",
        "bandcamp_contact_follow",
        "bandcamp_profile",
        "bandcamp_track_follow",
        "lastfm_profile",
        "spotify_profile",
        "undiscovered_music_profile",
    }
)

# Generic organisational inboxes. Harmless on the entity's own domain, but on an
# unrelated custom domain they mean we would be emailing a label/agency/studio
# rather than the artist, so they warrant human review instead of auto-approval.
_ROLE_EMAIL_LOCAL_PARTS = frozenset(
    {
        "info",
        "contact",
        "contacts",
        "booking",
        "bookings",
        "mail",
        "email",
        "hello",
        "office",
        "management",
        "mgmt",
        "press",
        "promo",
        "enquiries",
        "inquiries",
        "general",
    }
)

_SHARED_PROFESSIONAL_ROLES = frozenset(
    {"management", "booking", "label", "pr_publicity", "team_business"}
)


def _compact_slug(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", _cell_text(value).lower())


def _fb_status_is_rejected(status) -> bool:
    """Local mirror of the shared FB_Status reject test (see pipeline_runner)."""
    return any(token in _safe_lower(status) for token in ("reject", "blocked"))


_FACEBOOK_HOST_SUFFIXES = ("facebook.com", "fb.com", "fb.me", "m.me")


def _is_facebook_host(url) -> bool:
    try:
        host = (urlparse(_cell_text(url)).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _FACEBOOK_HOST_SUFFIXES)


def _first_usable_email(row: dict) -> str:
    for key in ("Email", "Primary Email", "Email_All"):
        for candidate in re.split(r"[\s,;]+", _cell_text(row.get(key, ""))):
            normalized = normalize_email_value(candidate)
            if normalized:
                return normalized
    return ""


def _email_belongs_to_entity(email: str, artist_name) -> bool:
    """True when the email's local part or domain plainly names the entity."""
    slug = _compact_slug(artist_name)
    if not slug or "@" not in email:
        return False
    local, domain = email.split("@", 1)
    for candidate in (_compact_slug(local), _compact_slug(domain.split(".")[0])):
        if not candidate:
            continue
        if candidate == slug:
            return True
        if len(slug) >= 4 and len(candidate) >= 4:
            if slug in candidate or candidate in slug:
                return True
            if fuzz.ratio(slug, candidate) >= 85:
                return True
    return False


def _email_is_role_inbox(email: str) -> bool:
    local = email.split("@", 1)[0] if "@" in email else email
    if _compact_slug(local) in _ROLE_EMAIL_LOCAL_PARTS:
        return True
    tokens = {token for token in re.split(r"[^a-z0-9]+", local.lower()) if token}
    return bool(tokens & _ROLE_EMAIL_LOCAL_PARTS)


def classify_contact_attribution(row: dict, email: str = "") -> str:
    """Answer the contact-safety question for a row's primary email.

    This is deliberately independent of enrichment completeness: it only asks
    whether the email is usable and tied to the intended entity, never how
    confident the rest of the enrichment was.  Returns one of:

    - ``ATTRIBUTION_NONE``         no usable email on the row
    - ``ATTRIBUTION_UNSAFE``       platform/support, telemetry, private-route or
                                   rejected-Facebook-surface derived; unusable
    - ``ATTRIBUTION_UNATTRIBUTED`` usable email with no trusted provenance
    - ``ATTRIBUTION_THIRD_PARTY``  trusted surface, but an organisational inbox
                                   on a domain unrelated to the entity
    - ``ATTRIBUTION_TRUSTED``      trusted surface and a plausible direct inbox

    Reads only columns the pipeline already writes; adds no schema.
    """
    if row is None or not hasattr(row, "get"):
        return ATTRIBUTION_NONE

    email = normalize_email_value(email) or _first_usable_email(row)
    if not email:
        return ATTRIBUTION_NONE
    if is_platform_support_email(email) or is_system_telemetry_email(email):
        return ATTRIBUTION_UNSAFE

    try:
        entry = (get_row_email_provenance(row) or {}).get(email) or {}
    except Exception:
        entry = {}
    source_type = _safe_lower(entry.get("source_type", ""))
    source_url = _cell_text(entry.get("source_url", ""))
    # Derive the surface from source type + URL rather than trusting a stored
    # label: rows exist whose recorded surface disagrees with their source_type
    # and source_url (e.g. surface=facebook_main on a soundcloud.com URL).
    surface = _safe_lower(infer_email_surface(source_type=source_type, source_url=source_url))
    if not surface:
        surface = _safe_lower(entry.get("surface", ""))

    fb_applied = {
        normalize_email_value(token)
        for token in re.split(r"[\s,;]+", _cell_text(row.get("__fb_emails_applied", "")))
    }
    url_is_facebook = _is_facebook_host(source_url)
    is_facebook_sourced = (
        email in fb_applied
        or url_is_facebook
        or (source_type.startswith("facebook") and not source_url)
    )
    if url_is_facebook and not canonicalize_facebook_url(source_url):
        # Reuse the hardened canonicaliser: it rejects Messenger/m.me/messages,
        # share/plugin surfaces and other private routes outright. Scoped to
        # Facebook-family hosts so a non-Facebook URL is judged on its own terms.
        return ATTRIBUTION_UNSAFE
    if _fb_status_is_rejected(row.get("FB_Status", "")):
        # A rejected Facebook surface must not yield a usable contact. An email
        # with explicit non-Facebook provenance is unaffected, because a failed
        # Facebook discovery is an enrichment failure rather than a contact
        # safety failure - but we fail closed when provenance cannot establish
        # an independent source.
        if is_facebook_sourced or not surface:
            return ATTRIBUTION_UNSAFE

    if surface not in _ATTRIBUTABLE_EMAIL_SURFACES:
        return ATTRIBUTION_UNATTRIBUTED
    if _email_belongs_to_entity(email, row.get("Artist Name", "")):
        return ATTRIBUTION_TRUSTED
    if _email_is_role_inbox(email):
        return ATTRIBUTION_THIRD_PARTY
    return ATTRIBUTION_TRUSTED


def _email_entry_for_row(row: dict, email: str) -> dict:
    try:
        entry = dict((get_row_email_provenance(row) or {}).get(email) or {})
    except Exception:
        entry = {}
    if not entry.get("role") and normalize_email_value(row.get("Email", "")) == email:
        entry["role"] = _safe_lower(row.get("Email_Role", ""))
        entry["role_evidence"] = _cell_text(row.get("Email_Role_Evidence", ""))
    return entry


def duplicate_email_is_unsafe(row: dict, peer_rows: Iterable[dict], email: str = "") -> bool:
    """Return whether this row's repeated primary email is a conflict.

    A shared professional inbox is a valid multi-artist contact.  A repeated
    address becomes unsafe only for a row whose provenance does not identify a
    shared role and which conflicts with an artist-direct attribution belonging
    to a different artist.
    """
    email = normalize_email_value(email) or _first_usable_email(row)
    if not email:
        return False
    current_entry = _email_entry_for_row(row, email)
    current_role = _safe_lower(current_entry.get("role", ""))
    if current_role in _SHARED_PROFESSIONAL_ROLES:
        return False
    current_artist = _compact_slug(row.get("Artist Name", ""))
    current_email_matches = _email_belongs_to_entity(email, row.get("Artist Name", ""))

    for peer in peer_rows:
        if peer is row:
            continue
        peer_emails = normalize_email_keys(
            [peer.get("Email", ""), peer.get("Email_All", ""), peer.get("All Emails", "")]
        )
        if email not in peer_emails:
            continue
        peer_artist = _compact_slug(peer.get("Artist Name", ""))
        if not peer_artist or peer_artist == current_artist:
            continue
        peer_entry = _email_entry_for_row(peer, email)
        peer_role = _safe_lower(peer_entry.get("role", ""))
        if peer_role in _SHARED_PROFESSIONAL_ROLES:
            continue
        peer_artist_direct = peer_role == "artist_direct" or (
            not peer_role and _email_belongs_to_entity(email, peer.get("Artist Name", ""))
        )
        if peer_artist_direct and not current_email_matches:
            return True
    return False


def _parse_release_year(value: str) -> Optional[int]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            return dt.year
        except Exception:
            continue
    match = re.search(r"(19|20)\d{2}", text)
    if match:
        try:
            return int(match.group(0))
        except Exception:
            return None
    return None


def apply_staleness_downgrade(
    status: str,
    row: dict,
    enabled: bool = STALE_DOWNGRADE_ENABLED_DEFAULT,
    fresh_year_cutoff: int = FRESH_YEAR_CUTOFF,
) -> str:
    if not enabled:
        return status
    if status != "OK":
        return status
    release_year = _parse_release_year(row.get("Release Date", "") if isinstance(row, dict) else "")
    if release_year is None:
        return status
    if release_year < fresh_year_cutoff:
        return "WARN"
    return status


_UNRESOLVED_ORIGIN_REASONS = frozenset(
    {
        "",
        "title_not_found",
        "title_missing",
        "missing_title",
        "artist_not_found",
        "insufficient_evidence",
        "unresolved",
        "unable_to_verify",
        "verification_unavailable",
    }
)


def origin_mismatch_is_durable(row: dict) -> bool:
    """Return whether an origin mismatch is a hard contradiction.

    Origin validators also use a zero flag for unresolved or incomplete source
    evidence. Those states are review signals, not durable proof that the
    current artist/contact validation is wrong.
    """
    if _optional_int_flag(row.get("origin_match_flag", None)) != 0:
        return False
    reason = _safe_lower(_cell_text(row.get("origin_match_reason", ""))).strip()
    return reason not in _UNRESOLVED_ORIGIN_REASONS


def derive_final_status_reason(row: dict, status: str, flags: dict) -> str:
    """Derive fresh reason metadata from the current authoritative evidence."""
    status = _normalise_status(status)
    preferred_status = _normalise_status(row.get(PREFERRED_CONTACT_STATUS_COL, ""))
    preferred_reason = _cell_text(row.get(PREFERRED_CONTACT_REASON_COL, "")).strip()
    attribution = classify_contact_attribution(row)

    if status == "OK":
        if preferred_status == "OK" and preferred_reason:
            return "preferred_contact_accepted"
        return "final_evidence_accepted"

    if status == "BLOCK":
        if attribution == ATTRIBUTION_UNSAFE:
            return "unsafe_contact"
        if flags.get("dup_email_flag"):
            return "duplicate_email"
        if flags.get("dup_artist_flag"):
            return "duplicate_artist"
        if flags.get("name_flag"):
            return "identity_unresolved"
        if looks_like_label_or_show(row):
            return "non_artist_entity"
        if not _has_valid_email(row):
            return "no_attributable_contact"
        if preferred_status == "BLOCK" and preferred_reason:
            return preferred_reason
        return "authoritative_hard_block"

    if status == "WARN":
        if preferred_status == "WARN" and preferred_reason:
            return preferred_reason
        if flags.get("dir_conflict_flag"):
            return "directory_conflict_review"
        if _optional_int_flag(row.get("origin_match_flag", 1)) == 0:
            return "origin_mismatch_review"
        if attribution == ATTRIBUTION_THIRD_PARTY:
            return "professional_contact_review"
        if attribution == ATTRIBUTION_UNATTRIBUTED:
            return "contact_attribution_review"
        if flags.get("dup_email_flag") or flags.get("dup_artist_flag"):
            return "duplicate_review"
        if not _has_valid_email(row):
            return "missing_contact_review"
        return "identity_or_enrichment_review"

    return ""


def compute_final_status(
    row: dict,
    flags: dict,
    match_score: float,
) -> str:
    existing = _normalise_status(row.get("final_status", ""))

    name_flag = int(flags.get("name_flag", 0) or 0)
    dir_conflict_flag = int(flags.get("dir_conflict_flag", 0) or 0)
    dup_email_flag = int(flags.get("dup_email_flag", 0) or 0)
    dup_artist_flag = int(flags.get("dup_artist_flag", 0) or 0)
    origin_match_flag = _optional_int_flag(row.get("origin_match_flag", None))
    origin_mismatch = origin_match_flag == 0
    durable_origin_mismatch = origin_mismatch_is_durable(row)
    inherited_origin_block = existing in {"BLOCK", "BLOCKED", "BLOCKED_BY_ORIGIN"} and origin_mismatch
    # genre_outlier_flag stays available as diagnostic metadata but must not
    # affect status: genre rarity is measured within a single run, so it makes
    # classification depend on batch composition rather than contact safety.

    has_email = _has_valid_email(row)
    email_type = _safe_lower(row.get("Email_Type", ""))
    unearthed_no_emails = "unearthed_no_emails" in email_type
    labelish = looks_like_label_or_show(row)
    review_urls = _safe_lower(row.get("Review_Urls", "") or row.get("Review Urls", ""))
    review_labelish = any(token in review_urls for token in ("session", "sessions", "radio", "podcast", "dj mix", "dj set"))

    # --- Contact safety: is this email usable and tied to this entity? -------
    attribution = classify_contact_attribution(row)
    if attribution == ATTRIBUTION_UNSAFE:
        return "BLOCK"
    contact_is_attributable = has_email and attribution in _ATTRIBUTABLE_CONTACT
    artist_slug = _compact_slug(row.get("Artist Name", ""))
    identity_linked_contact = (
        has_email
        and 0 < len(artist_slug) <= 4
        and _email_belongs_to_entity(_first_usable_email(row), row.get("Artist Name", ""))
    )
    try:
        selected_meta = (get_row_email_provenance(row) or {}).get(_first_usable_email(row)) or {}
    except Exception:
        selected_meta = {}
    label_contact = _safe_lower(selected_meta.get("role", "")) == "label"
    selected_role = _safe_lower(selected_meta.get("role", "") or row.get("Email_Role", ""))
    stale_identity_block_reconsiderable = (
        existing == "BLOCK"
        and contact_is_attributable
        and selected_role in _SHARED_PROFESSIONAL_ROLES
    )

    # Duplicate flags are durable row-level evidence, unlike an inherited
    # final_status value. Preserve an existing hard block only when one of
    # those authoritative duplicate reasons is still present.
    if existing == "BLOCK" and (
        dup_email_flag
        or dup_artist_flag
        or (name_flag and not stale_identity_block_reconsiderable)
    ):
        return "BLOCK"
    if existing in {"BLOCK", "BLOCKED", "BLOCKED_BY_ORIGIN"} and not (has_email or unearthed_no_emails):
        return "BLOCK"

    fb_selected_by = _safe_lower(row.get("FB_Selected_By", ""))
    fb_match_level = _safe_lower(row.get("FB_Match_Level", ""))
    fb_name_flag = _optional_int_flag(row.get("FB_Name_Consistency_Flag", None))
    fb_review_reason = _cell_text(row.get("FB_Review_Reason", "")).strip()
    if fb_review_reason == "origin_mismatch_downgraded":
        # Legacy reconciliation wrote its own reason into the Facebook review
        # field. It is not evidence of a low-confidence Facebook match.
        fb_review_reason = ""
    fb_low_confidence = (
        fb_selected_by == "mismatch_fallback"
        or fb_match_level == "mismatch"
        or (fb_name_flag is not None and fb_name_flag == 0)
        or bool(fb_review_reason)
    )

    # Current checker artifacts carry an authoritative per-contact decision.
    # Prefer it over the legacy row-level threshold model; legacy artifacts
    # without these fields continue through the compatibility logic below.
    preferred_contact_status = _normalise_status(row.get(PREFERRED_CONTACT_STATUS_COL, ""))
    if preferred_contact_status == "BLOCK":
        return "BLOCK"
    if preferred_contact_status == "WARN":
        return "WARN"
    if preferred_contact_status == "OK":
        if dup_artist_flag or durable_origin_mismatch or fb_low_confidence:
            return "WARN"
        if labelish and not label_contact:
            return "BLOCK"
        return "OK"

    strong_identity = (
        match_score >= 0.75
        and not name_flag
        and not inherited_origin_block
        and not dir_conflict_flag
        and not dup_artist_flag
        and not labelish
    )
    if strong_identity:
        if fb_low_confidence:
            return "WARN"
        if has_email or unearthed_no_emails:
            return "WARN" if attribution == ATTRIBUTION_THIRD_PARTY else "OK"
        return "WARN"

    # --- Hard blockers: identity contradiction or unusable entity ------------
    if name_flag and match_score < 0.75 and not stale_identity_block_reconsiderable:
        return "BLOCK"
    if labelish and not (contact_is_attributable and label_contact):
        return "BLOCK"
    if review_labelish and match_score < 0.85 and not (has_email or unearthed_no_emails):
        return "BLOCK"
    # --- Enrichment quality, not contact safety ------------------------------
    # A cross-directory slug disagreement is dominated by slug formatting (
    # ".official" suffixes, host-in-path artifacts, numeric SoundCloud handles,
    # alternate handles), so it blocks only when there is no attributable
    # contact to protect. Otherwise it is a human-review signal.
    if dir_conflict_flag and not (contact_is_attributable or identity_linked_contact):
        return "BLOCK"

    if fb_low_confidence:
        return "WARN"

    if unearthed_no_emails and not dir_conflict_flag:
        return "OK"
    if dup_email_flag or dup_artist_flag:
        return "WARN"
    if not has_email:
        return "WARN"
    if inherited_origin_block or dir_conflict_flag or attribution == ATTRIBUTION_THIRD_PARTY or labelish:
        return "WARN"
    return "OK"


def filter_rows_for_export(profile: str, rows: Iterable[dict]) -> List[dict]:
    profile_key = (profile or "studio_safe").strip().lower()
    exported: List[dict] = []
    for row in rows:
        status = _normalise_status(row.get("final_status", ""))
        email_all = str(row.get("Email_All", "") or "").strip()
        has_email = "@" in email_all or "@" in str(row.get("Email", "") or "")
        source_dir = str(
            row.get("Source Directory")
            or row.get("Source_Directory")
            or row.get("Source")
            or ""
        ).strip().lower()
        unearthed_source = source_dir.startswith("job_unearthed") or "unearthed" in source_dir
        email_type = str(row.get("Email_Type", "") or "").lower()
        played_unearthed = _boolish(row.get("Played on Unearthed", ""))
        played_triplej = _boolish(row.get("Played on triple J", ""))

        if profile_key == "full_dump":
            exported.append(row)
            continue

        if profile_key == "studio_safe" or not profile_key:
            if status == "OK" and has_email:
                exported.append(row)
            continue

        if profile_key == "studio_plus":
            if status in {"OK", "WARN"} and (has_email or (unearthed_source and (played_unearthed or played_triplej))):
                exported.append(row)
            continue

        if profile_key == "unearthed_social":
            if unearthed_source and not has_email and "unearthed_no_emails" in email_type:
                exported.append(row)
            continue

        # Fallback to safest behaviour if profile is unknown.
        if status == "OK" and has_email:
            exported.append(row)
    return exported


def _clean_name(value: str) -> str:
    text = unidecode(str(value or "")).lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_spotify_id(slug: str) -> bool:
    condensed = re.sub(r"[^a-zA-Z0-9]", "", slug or "")
    return bool(re.fullmatch(r"[A-Za-z0-9]{16,}", condensed))


def _split_url_values(value) -> list[str]:
    """Return the ordered, de-duplicated URLs stored in a linkish cell."""
    if not isinstance(value, str):
        return []
    seen: set[str] = set()
    urls: list[str] = []
    # Link columns are canonically pipe/comma separated, with semicolon and
    # whitespace retained for older artifacts.
    # A separator is structural only when another URL follows it. This keeps
    # legal commas/pipes in URL paths or query strings intact.
    for token in re.split(r"(?:\s*[|,;]\s*|\s+)(?=https?://)", value.strip()):
        candidate = token.strip().strip("<>()[]{}\"'")
        if not candidate.startswith(("http://", "https://")):
            continue
        key = candidate.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        urls.append(candidate)
    return urls


def _extract_urls_from_row(row: pd.Series, keyword: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for _, value in row.items():
        for candidate in _split_url_values(value):
            if keyword not in candidate.lower():
                continue
            key = candidate.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            urls.append(candidate)
    return urls


def _extract_url_from_row(row: pd.Series, keyword: str) -> str:
    """Compatibility wrapper for callers that need only the first URL."""
    urls = _extract_urls_from_row(row, keyword)
    return urls[0] if urls else ""


def _spotify_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    slug = ""
    if "artist" in parts:
        idx = parts.index("artist")
        if idx + 1 < len(parts):
            slug = parts[idx + 1]
    if not slug and parts:
        slug = parts[-1]
    slug = unquote(slug.split("?", 1)[0])
    slug = slug.replace("-", " ").replace("_", " ").strip()
    if not slug or _looks_like_spotify_id(slug):
        return ""
    return _clean_name(slug)


def _extract_spotify_names(row: pd.Series) -> list[str]:
    return [
        name
        for url in _extract_urls_from_row(row, "spotify.com")
        if (name := _spotify_name_from_url(url))
    ]


def _extract_spotify_name(row: pd.Series) -> str:
    names = _extract_spotify_names(row)
    return names[0] if names else ""


def _bandcamp_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    candidate = ""
    if host.endswith("bandcamp.com"):
        subdomain = host.split(".")[0]
        if subdomain not in ("www", "bandcamp", ""):
            candidate = subdomain
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not candidate and parts:
        candidate = parts[0]
    candidate = unquote(candidate)
    return _clean_name(candidate)


def _extract_bandcamp_names(row: pd.Series) -> list[str]:
    return [
        name
        for url in _extract_urls_from_row(row, "bandcamp.com")
        if (name := _bandcamp_name_from_url(url))
    ]


def _extract_bandcamp_name(row: pd.Series) -> str:
    names = _extract_bandcamp_names(row)
    return names[0] if names else ""


def _soundcloud_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return ""
    candidate = unquote(parts[0])
    return _clean_name(candidate)


def _extract_soundcloud_names(row: pd.Series) -> list[str]:
    return [
        name
        for url in _extract_urls_from_row(row, "soundcloud.com")
        if (name := _soundcloud_name_from_url(url))
    ]


def _extract_soundcloud_name(row: pd.Series) -> str:
    names = _extract_soundcloud_names(row)
    return names[0] if names else ""


def _facebook_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return ""
    if parts[0].lower() in {"share", "sharer", "dialog", "plugins", "messages"}:
        candidate = ""
    elif parts[0] in ("pages",):
        candidate = parts[1] if len(parts) > 1 else ""
    elif parts[0] == "profile.php":
        candidate = ""
    else:
        candidate = parts[0]
    candidate = unquote(candidate.replace("-", " ").replace("_", " "))
    return _clean_name(candidate)


def _extract_facebook_names(row: pd.Series) -> list[str]:
    return [
        name
        for url in _extract_urls_from_row(row, "facebook.com")
        if (name := _facebook_name_from_url(url))
    ]


def _extract_facebook_name(row: pd.Series) -> str:
    names = _extract_facebook_names(row)
    return names[0] if names else ""


def _instagram_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts or parts[0].lower() in {"p", "reel", "reels", "stories", "explore"}:
        return ""
    return _clean_name(unquote(parts[0].replace("-", " ").replace("_", " ")))


def _extract_instagram_names(row: pd.Series) -> list[str]:
    return [
        name
        for url in _extract_urls_from_row(row, "instagram.com")
        if (name := _instagram_name_from_url(url))
    ]


_IDENTITY_HANDLE_PREFIXES = ("musicby", "official", "theband")
_IDENTITY_HANDLE_SUFFIXES = ("musicofficial", "officialmusic", "official", "music", "band")


def _identity_variants(name: str) -> set[str]:
    """Generate conservative handle variants without changing match thresholds."""
    cleaned = _clean_name(name)
    if not cleaned:
        return set()
    compact = cleaned.replace(" ", "")
    variants = {cleaned, compact}
    words = cleaned.split()
    while words and words[0] == "the":
        words = words[1:]
    while words and words[-1] in {"official", "music", "band"}:
        words = words[:-1]
    if words:
        variants.add("".join(words))
    frontier = [compact]
    while frontier:
        current = frontier.pop()
        candidates = [
            current[len(prefix) :]
            for prefix in _IDENTITY_HANDLE_PREFIXES
            if current.startswith(prefix)
        ]
        candidates.extend(
            current[: -len(suffix)]
            for suffix in _IDENTITY_HANDLE_SUFFIXES
            if current.endswith(suffix)
        )
        for candidate in candidates:
            if candidate and len(candidate) >= 4 and candidate not in variants:
                variants.add(candidate)
                frontier.append(candidate)
    return variants


def _identity_ratio(left: str, right: str) -> float:
    left_variants = _identity_variants(left)
    right_variants = _identity_variants(right)
    if not left_variants or not right_variants:
        return 0.0
    return max(fuzz.ratio(a, b) for a in left_variants for b in right_variants)


def _strip_platform_identity_noise(candidate: str, source: str) -> str:
    """Remove non-semantic platform-generated handle material.

    Facebook page slugs commonly append a long numeric page identifier.  It is
    diagnostic routing data, not part of the artist identity.  Short numbers
    remain meaningful (for example, ``blink 182``).
    """
    cleaned = _clean_name(candidate)
    if source == "facebook":
        cleaned = re.sub(r"(?:^|\s)\d{5,}(?=\s|$)", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _identity_candidates(row: Mapping[str, Any]) -> list[dict[str, str]]:
    series = pd.Series(dict(row))
    extractors = (
        ("spotify", "spotify.com", _spotify_name_from_url),
        ("bandcamp", "bandcamp.com", _bandcamp_name_from_url),
        ("soundcloud", "soundcloud.com", _soundcloud_name_from_url),
        ("facebook", "facebook.com", _facebook_name_from_url),
        ("instagram", "instagram.com", _instagram_name_from_url),
    )
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, host, extractor in extractors:
        for url in _extract_urls_from_row(series, host):
            candidate = _strip_platform_identity_noise(extractor(url), source)
            if not candidate:
                continue
            key = (source, url.lower().rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"source": source, "url": url, "candidate": candidate})
    return candidates


def assess_artist_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Assess every platform identity against the canonical artist.

    Candidates are not required to agree pairwise.  A common handle containing
    the canonical artist token can also be accepted as a plausible alias when
    independently corroborated by two platforms.  Unrelated candidates remain
    visible as rejected evidence without overriding stronger aligned sources.
    """
    canonical = _clean_name(row.get("Artist Name", ""))
    canonical_compact = canonical.replace(" ", "")
    candidates = _identity_candidates(row)
    cluster_sources: dict[str, set[str]] = {}
    for item in candidates:
        cluster_sources.setdefault(_compact_slug(item["candidate"]), set()).add(item["source"])

    accepted = 0
    assessments: list[dict[str, Any]] = []
    provisional: list[tuple[dict[str, str], float, str]] = []
    for item in candidates:
        score = _identity_ratio(canonical, item["candidate"]) if canonical else 0.0
        compact = _compact_slug(item["candidate"])
        canonical_words = {word for word in canonical.split() if len(word) >= 5}
        prefixed_word_alias = any(
            compact.startswith(prefix) and compact[len(prefix) :] in canonical_words
            for prefix in ("musicby", "official")
        )
        canonical_extension_alias = (
            len(canonical_compact) >= 5
            and compact.startswith(canonical_compact)
            and score >= 50
        )
        corroborated_alias = (
            len(canonical_compact) >= 4
            and canonical_compact in compact
            and len(cluster_sources.get(compact, set())) >= 2
        )
        if score >= 85:
            state = "matched"
        elif score >= 70 or prefixed_word_alias or canonical_extension_alias or corroborated_alias:
            state = "plausible_alias"
        else:
            state = "unresolved"
        if state in {"matched", "plausible_alias"}:
            accepted += 1
        provisional.append((item, score, state))

    for item, score, state in provisional:
        if state == "unresolved" and accepted:
            state = "rejected"
        assessments.append({**item, "state": state, "score": round(score / 100.0, 4)})

    if not candidates:
        overall = "unknown"
        match_score = 0.0
    elif accepted:
        overall = "matched" if any(a["state"] == "matched" for a in assessments) else "plausible_alias"
        weights = {"spotify": 1.0, "facebook": 1.0, "instagram": 1.0, "bandcamp": 0.8, "soundcloud": 0.6}
        earned = sum(weights.get(a["source"], 0.5) for a in assessments if a["state"] in {"matched", "plausible_alias"})
        possible = sum(weights.get(a["source"], 0.5) for a in assessments)
        match_score = max(a["score"] for a in assessments if a["state"] in {"matched", "plausible_alias"})
        if possible:
            match_score = max(match_score, earned / possible)
    else:
        overall = "unresolved"
        match_score = max((a["score"] for a in assessments), default=0.0)

    directory_conflict = int(
        overall == "unresolved"
        or (accepted < 2 and any(a["state"] == "rejected" for a in assessments))
    )
    return {
        "status": overall,
        "match_score": round(min(1.0, match_score), 4),
        "name_consistent": overall in {"matched", "plausible_alias"},
        "directory_conflict": directory_conflict,
        "candidates": assessments,
    }


def _canonical_url_key(value: Any) -> str:
    text = _cell_text(value).strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        host = (parsed.netloc or "").lower().removeprefix("www.")
        path = re.sub(r"/+", "/", unquote(parsed.path or "")).rstrip("/").lower()
        return f"{host}{path}"
    except Exception:
        return text.lower().split("?", 1)[0].rstrip("/")


def _source_identity_state(email: str, row: Mapping[str, Any], entry: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
    source_url = _cell_text(entry.get("source_url", ""))
    source_key = _canonical_url_key(source_url)
    if source_key:
        for candidate in identity.get("candidates", []):
            if _canonical_url_key(candidate.get("url", "")) == source_key:
                return _cell_text(candidate.get("state", "")) or "unresolved"
    surface = infer_email_surface(entry.get("source_type", ""), source_url, entry.get("surface", ""))
    if surface.startswith("website_") and _email_belongs_to_entity(email, row.get("Artist Name", "")):
        return "matched"
    return "unknown"


def _truthy(value: Any) -> bool:
    return _safe_lower(value).strip() in {"1", "true", "yes", "y", "approved"}


def assess_row_contacts(
    row: Mapping[str, Any],
    identity: Mapping[str, Any],
    peer_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate every email independently and choose one outreach default."""
    mutable_row = dict(row)
    provenance = get_row_email_provenance(mutable_row)
    emails = normalize_email_keys(
        [row.get("Email_All", ""), row.get("All Emails", ""), row.get("Email", ""), row.get("Primary Email", "")]
    )
    role_priority = {
        "artist_direct": 0,
        "management": 1,
        "team_business": 2,
        "label": 2,
        "booking": 3,
        "pr_publicity": 4,
        "unknown": 9,
    }
    status_priority = {"OK": 0, "WARN": 1, "BLOCK": 2}
    source_priority = {"matched": 0, "plausible_alias": 1, "unknown": 2, "unresolved": 3, "rejected": 4, "conflicting": 5}
    contacts: list[dict[str, Any]] = []

    for index, email in enumerate(emails):
        entry = dict(provenance.get(email) or {})
        if not entry and email == normalize_email_value(row.get("Email", "")):
            entry = _email_entry_for_row(dict(row), email)
        row_for_email = dict(row)
        row_for_email["Email"] = email
        attribution = classify_contact_attribution(row_for_email, email)
        source_identity = _source_identity_state(email, row, entry, identity)
        role = _safe_lower(entry.get("role", "") or (row.get("Email_Role", "") if index == 0 else "")) or "unknown"
        duplicate_unsafe = duplicate_email_is_unsafe(row_for_email, peer_rows, email=email)

        if attribution == ATTRIBUTION_UNSAFE:
            status, reason = "BLOCK", "unsafe_contact"
        elif duplicate_unsafe:
            status, reason = "BLOCK", "cross_artist_contact_conflict"
        elif source_identity == "conflicting":
            status, reason = "BLOCK", "source_identity_conflict"
        elif source_identity == "rejected":
            status, reason = "WARN", "source_identity_rejected_review"
        elif source_identity == "unresolved":
            status, reason = "WARN", "source_identity_unresolved"
        elif source_identity in {"matched", "plausible_alias"}:
            if attribution in _ATTRIBUTABLE_CONTACT:
                status, reason = "OK", "contact_attributable"
            else:
                status, reason = "WARN", "contact_attribution_review"
        elif _email_belongs_to_entity(email, row.get("Artist Name", "")) and attribution != ATTRIBUTION_UNATTRIBUTED:
            status, reason = "OK", "artist_identity_in_email"
        else:
            status, reason = "WARN", "contact_attribution_review"

        contacts.append(
            {
                "email": email,
                "entry": entry,
                "role": role,
                "attribution": attribution,
                "source_identity": source_identity,
                "status": status,
                "reason": reason,
                "index": index,
                "rank": (
                    status_priority[status],
                    role_priority.get(role, 8),
                    source_priority.get(source_identity, 8),
                    index,
                    email,
                ),
            }
        )

    selectable = [contact for contact in contacts if contact["status"] != "BLOCK"]
    preferred = min(selectable, key=lambda contact: contact["rank"]) if selectable else None
    approved_email = normalize_email_value(row.get(APPROVED_CONTACT_EMAIL_COL, ""))
    row_approved = any(
        _truthy(row.get(column, ""))
        for column in ("Review_Approved", "Reviewed_Approved", "Final_Status_Approved", "Approved")
    )
    for contact in contacts:
        is_preferred = bool(preferred and contact["email"] == preferred["email"])
        explicitly_approved = contact["status"] == "WARN" and (
            approved_email == contact["email"] or (row_approved and not approved_email and is_preferred)
        )
        entry = contact["entry"]
        entry.update(
            {
                "role": contact["role"],
                "source_identity_state": contact["source_identity"],
                "attribution_state": contact["attribution"],
                "validation_status": contact["status"],
                "send_eligible": "true" if contact["status"] == "OK" or explicitly_approved else "false",
                "review_reason": contact["reason"],
                "preferred": "true" if is_preferred else "false",
            }
        )
        provenance[contact["email"]] = entry

    preferred_email = preferred["email"] if preferred else ""
    return {
        "provenance": provenance,
        "preferred_email": preferred_email,
        "alternate_emails": [email for email in emails if email != preferred_email],
        "preferred_status": preferred["status"] if preferred else (
            "BLOCK"
            if contacts
            or (
                identity.get("directory_conflict")
                and identity.get("status") in {"matched", "plausible_alias"}
            )
            else "WARN"
        ),
        "preferred_reason": preferred["reason"] if preferred else ("no_safe_contact" if contacts else "no_contact"),
        "contacts": contacts,
    }


def apply_authoritative_validation(
    row: Mapping[str, Any],
    peer_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a row updated with authoritative identity/contact summaries."""
    output = dict(row)
    identity = assess_artist_identity(output)
    contacts = assess_row_contacts(output, identity, peer_rows)
    output[ARTIST_IDENTITY_STATUS_COL] = identity["status"]
    output[IDENTITY_ASSESSMENT_JSON_COL] = json.dumps(
        identity["candidates"], sort_keys=True, separators=(",", ":")
    )
    output[NAME_CONSISTENCY_FLAG_COL] = 1 if identity["name_consistent"] else 0
    output[NAME_CONSISTENCY_POLARITY_COL] = NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1
    output["directory_conflict_flag"] = identity["directory_conflict"]
    output["match_score_overall"] = identity["match_score"]
    output[PREFERRED_OUTREACH_EMAIL_COL] = contacts["preferred_email"]
    output[ALTERNATE_EMAILS_COL] = ";".join(contacts["alternate_emails"])
    output[PREFERRED_CONTACT_STATUS_COL] = contacts["preferred_status"]
    output[PREFERRED_CONTACT_REASON_COL] = contacts["preferred_reason"]
    output["Email_Provenance_JSON"] = dump_email_provenance_json(contacts["provenance"])

    if contacts["preferred_email"]:
        output["Email"] = contacts["preferred_email"]
        preferred_meta = contacts["provenance"].get(contacts["preferred_email"], {})
        for column, key in (
            ("Email_Role", "role"),
            ("Email_Role_Evidence", "role_evidence"),
            ("Email_Source_URL", "source_url"),
            ("Email_Source_Type", "source_type"),
            ("Email_Extract_Method", "extract_method"),
        ):
            output[column] = _cell_text(preferred_meta.get(key, ""))
    return output


def _pairwise_conflict(names: list[str], threshold: float) -> int:
    if len(names) < 2:
        return 0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if _identity_ratio(names[i], names[j]) < threshold:
                return 1
    return 0


def _normalise_review_url_value(value) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value or "").strip()


def _split_external_links(value) -> list[str]:
    text = _normalise_review_url_value(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _build_review_urls(row: pd.Series) -> str:
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(value) -> None:
        text = _normalise_review_url_value(value)
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)

    _add(row["Spotify_URL"] if "Spotify_URL" in row else "")
    _add(row["Facebook_URL"] if "Facebook_URL" in row else "")
    _add(row["SoundCloud Link"] if "SoundCloud Link" in row else "")
    for link in _split_external_links(row["External Links"] if "External Links" in row else ""):
        _add(link)
    _add(row["Source URL"] if "Source URL" in row else "")
    return " | ".join(candidates)


def run_final_checker(
    final_csv_path: str,
    stale_downgrade: bool = STALE_DOWNGRADE_ENABLED_DEFAULT,
    fresh_year_cutoff: int = FRESH_YEAR_CUTOFF,
) -> str:
    try:
        if not final_csv_path or not os.path.exists(final_csv_path):
            print(f"[Final Checker] Warning: file not found at {final_csv_path}")
            return final_csv_path

        df = pd.read_csv(final_csv_path)
        result_df = df.copy()
        total_rows = len(result_df)
        stale_env = os.getenv("LEAD_MACHINE_DISABLE_STALE_DOWNGRADE")
        stale_enabled = stale_downgrade
        if stale_env is not None:
            stale_enabled = not _boolish(stale_env)

        if "Primary Genre" not in result_df.columns:
            result_df["Primary Genre"] = ""
        genre_values: list[str] = []
        for raw_val in result_df.get("Primary Genre", pd.Series(dtype=str)).fillna("").astype(str):
            cleaned = normalize_primary_genre(raw_val)
            if LOGGER.isEnabledFor(logging.DEBUG) and raw_val and cleaned and len(str(raw_val)) > 40 and len(cleaned) < 30 and cleaned != raw_val:
                LOGGER.debug("[Final Checker] Simplified genre from %r to %r", raw_val[:200], cleaned)
            genre_values.append(cleaned)
        result_df["Primary Genre"] = genre_values

        artist_series = result_df.get("Artist Name", pd.Series(dtype=str)).fillna("").astype(str)
        normalized_artists = artist_series.apply(_clean_name)

        artist_counts = normalized_artists[normalized_artists != ""].value_counts().to_dict()

        normalized_genres = pd.Series(genre_values, dtype=str).str.strip().str.lower()
        genre_counts = normalized_genres[normalized_genres != ""].value_counts().to_dict()

        name_flags = []
        dup_email_flags = []
        dup_artist_flags = []
        dir_conflict_flags = []
        genre_outlier_flags = []
        match_scores = []
        statuses = []
        row_records = result_df.to_dict("records")
        validated_rows: list[dict[str, Any]] = []

        for idx, row in result_df.iterrows():
            artist_clean = normalized_artists.iloc[idx]
            row_dict = apply_authoritative_validation(row.to_dict(), row_records)
            identity_status = _safe_lower(row_dict.get(ARTIST_IDENTITY_STATUS_COL, ""))
            name_flag = 0 if identity_status in {"matched", "plausible_alias", "unknown"} else 1
            dir_conflict_flag = int(row_dict.get("directory_conflict_flag", 0) or 0)
            provenance = get_row_email_provenance(row_dict)
            dup_email_flag = int(
                any(meta.get("review_reason") == "cross_artist_contact_conflict" for meta in provenance.values())
            )

            artist_key = artist_clean
            dup_artist_flag = 1 if artist_key and artist_counts.get(artist_key, 0) > 1 else 0

            genre_key = normalized_genres.iloc[idx]
            if not genre_key or total_rows == 0:
                genre_flag = 0
            else:
                freq = genre_counts.get(genre_key, 0) / total_rows
                genre_flag = 1 if freq < 0.05 else 0

            match_score = float(row_dict.get("match_score_overall", 0) or 0)

            flags = {
                "name_flag": name_flag,
                "dir_conflict_flag": dir_conflict_flag,
                "dup_email_flag": dup_email_flag,
                "dup_artist_flag": dup_artist_flag,
                "genre_outlier_flag": genre_flag,
            }
            status = compute_final_status(row_dict, flags, match_score)
            status = apply_staleness_downgrade(status, row_dict, enabled=stale_enabled, fresh_year_cutoff=fresh_year_cutoff)
            row_dict["duplicate_email_flag"] = dup_email_flag
            row_dict["duplicate_artist_flag"] = dup_artist_flag
            row_dict["genre_outlier_flag"] = genre_flag
            row_dict["final_status"] = status
            row_dict["Final_Status_Reason"] = derive_final_status_reason(row_dict, status, flags)
            validated_rows.append(row_dict)

            name_flags.append(name_flag)
            dup_email_flags.append(dup_email_flag)
            dup_artist_flags.append(dup_artist_flag)
            dir_conflict_flags.append(dir_conflict_flag)
            genre_outlier_flags.append(genre_flag)
            match_scores.append(match_score)
            statuses.append(status)

        result_df = pd.DataFrame(validated_rows)

        # Column contract: name_consistency_flag is 1 when the artist name is
        # CONSISTENT with its directory slugs and 0 when it is not, matching the
        # column name and every reader (pipeline_runner, night_mode_fb). The
        # internal name_flag fed to compute_final_status is the inverse
        # (1 = mismatch) and is left untouched.
        result_df[NAME_CONSISTENCY_FLAG_COL] = [1 - flag for flag in name_flags]
        # Stamp the polarity so downstream readers can tell this artifact from a
        # pre-fix one without inspecting the data. See read_name_consistency_flag.
        result_df[NAME_CONSISTENCY_POLARITY_COL] = NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1
        result_df["duplicate_email_flag"] = dup_email_flags
        result_df["duplicate_artist_flag"] = dup_artist_flags
        result_df["directory_conflict_flag"] = dir_conflict_flags
        result_df["genre_outlier_flag"] = genre_outlier_flags
        result_df["match_score_overall"] = match_scores
        result_df["final_status"] = statuses
        result_df["Review_Urls"] = ""
        review_mask = result_df["final_status"].isin(["BLOCK", "WARN"])
        if review_mask.any():
            result_df.loc[review_mask, "Review_Urls"] = result_df.loc[review_mask].apply(
                _build_review_urls, axis=1
            )

        base, ext = os.path.splitext(final_csv_path)
        checked_path = f"{base}_checked{ext or '.csv'}"
        result_df.to_csv(checked_path, index=False)
        return checked_path
    except Exception as exc:
        print(f"[Final Checker] Warning: checker failed safely: {exc}")
        return final_csv_path
