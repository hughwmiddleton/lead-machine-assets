"""Normalized, provenance-preserving identity signals for pairwise comparison."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Tuple
from urllib.parse import parse_qs, urlsplit

from .adapters import source_occurrence_from_row
from .contracts import LeadRecord, SourceOccurrence
from .identity import canonicalize_url, normalize_text, select_identity_evidence

IDENTITY_EVIDENCE_SCHEMA_VERSION = "lead-engine-identity-evidence/v1"


@dataclass(frozen=True)
class IdentitySignal:
    """One normalized identity fact derived from one source occurrence."""

    family: str
    kind: str
    value: str
    raw_value: str
    source_occurrence_id: str
    strength: str
    independence_key: str
    schema_version: str = IDENTITY_EVIDENCE_SCHEMA_VERSION


@dataclass(frozen=True)
class IdentityProfile:
    """Identity-relevant signals for one occurrence; not an entity record."""

    source_occurrence: SourceOccurrence
    signals: Tuple[IdentitySignal, ...]
    schema_version: str = IDENTITY_EVIDENCE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_ALIASES = {
    "artist_name": ("Artist Name", "Artist", "Band Name"),
    "location": ("Location", "City"),
    "spotify_id": ("Spotify Artist ID", "Spotify_Artist_ID", "spotify_artist_id"),
    "spotify_url": ("Spotify URL", "Spotify_URL", "Spotify Link"),
    "bandcamp_url": ("Bandcamp URL", "Bandcamp_URL", "Bandcamp Link"),
    "soundcloud_url": ("SoundCloud URL", "SoundCloud_URL", "SoundCloud Link"),
    "lastfm_url": ("LastFM URL", "Last.fm URL", "LastFM_URL", "lastfm_url"),
    "source_url": ("Source URL", "Source_URL", "Profile URL", "profile_url", "artist_url"),
    "instagram_url": ("Instagram URL", "Instagram_URL", "Instagram"),
    "instagram_handle": ("Instagram Handle", "Instagram_Handle"),
    "facebook_url": ("Facebook URL", "Facebook_URL", "Facebook"),
    "external_links": ("External Links", "External_Links", "Social Link", "Social_Link"),
    "website": ("Website", "Official Website", "Spotify Website URL", "Spotify_Website_URL"),
    "domain_role": ("Domain Role", "Domain_Role"),
    "email": ("Primary Email", "Primary_Email", "Email", "E-mail"),
    "contact_role": ("Contact Role", "Contact_Role"),
    "contact_type": ("Contact Type", "Contact_Type"),
}

_LINK_HUB_HOSTS = {
    "beacons.ai",
    "bio.site",
    "campsite.bio",
    "linktr.ee",
    "lnk.bio",
    "solo.to",
    "taplink.cc",
}


def _header_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value, comparison=True).lstrip("\ufeff"))


class _Row:
    def __init__(self, row: Mapping[object, object]):
        self.values = {_header_key(key): value for key, value in row.items()}

    def get(self, name: str) -> str:
        for alias in _ALIASES[name]:
            value = self.values.get(_header_key(alias))
            if value is None:
                continue
            if isinstance(value, float) and math.isnan(value):
                continue
            value_type = type(value)
            if value_type.__name__ in {"NAType", "NaTType"} and value_type.__module__.startswith("pandas"):
                continue
            text = str(value)
            if text.strip():
                return text
        return ""


def _signal(
    occurrence_id: str,
    family: str,
    kind: str,
    value: str,
    raw_value: str,
    strength: str,
) -> IdentitySignal:
    return IdentitySignal(
        family=family,
        kind=kind,
        value=value,
        raw_value=raw_value,
        source_occurrence_id=occurrence_id,
        strength=strength,
        independence_key=f"{family}:{kind}",
    )


def _provider_signal(occurrence_id: str, source_type: str, raw_value: str, native_id: str = ""):
    if not raw_value and not native_id:
        return None
    evidence = select_identity_evidence(
        source_type=source_type,
        source_native_id=native_id,
        source_url=raw_value,
    )
    provider_kinds = {
        "spotify_artist_id",
        "bandcamp_artist_host",
        "bandcamp_profile_url",
        "soundcloud_handle",
        "unearthed_artist_slug",
        "lastfm_artist_path",
        "source_native_id",
    }
    if evidence.kind not in provider_kinds or evidence.strength != "strong":
        return None
    kind = f"{source_type}_source_native_id" if evidence.kind == "source_native_id" else evidence.kind
    return _signal(occurrence_id, "provider", kind, evidence.value, native_id or raw_value, "strong")


def _instagram_handle(raw: str) -> str:
    text = raw.strip().lstrip("@").casefold()
    if text and "/" not in text and "." not in text and " " not in text:
        return text
    canonical = canonicalize_url(raw)
    if not canonical:
        return ""
    parsed = urlsplit(canonical)
    if (parsed.hostname or "").removeprefix("www.") != "instagram.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0].casefold() in {"explore", "p", "reel", "stories"}:
        return ""
    return parts[0].lstrip("@").casefold()


def _facebook_profile(raw: str) -> str:
    canonical = canonicalize_url(raw)
    if not canonical:
        return ""
    parsed = urlsplit(canonical)
    host = (parsed.hostname or "").removeprefix("www.")
    if host not in {"facebook.com", "fb.com"}:
        return ""
    path = parsed.path.rstrip("/")
    if path.casefold() == "/profile.php":
        profile_id = (parse_qs(parsed.query).get("id") or [""])[0]
        return f"id:{profile_id}" if profile_id.isdigit() else ""
    parts = [part for part in path.split("/") if part]
    excluded = {"events", "groups", "login", "plugins", "share", "sharer", "watch"}
    if not parts or parts[0].casefold() in excluded:
        return ""
    return parts[0].casefold()


def _domain(raw: str) -> str:
    canonical = canonicalize_url(raw)
    if not canonical:
        return ""
    return (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")


def _external_link_tokens(raw: str) -> tuple[str, ...]:
    return tuple(token.strip() for token in re.split(r"[|;,\s]+", raw) if token.strip())


def identity_profile_from_row(row: Mapping[object, object]) -> IdentityProfile:
    """Extract a deterministic, de-duplicated identity profile from a legacy row."""
    occurrence = source_occurrence_from_row(row)
    view = _Row(row)
    occurrence_id = occurrence.source_occurrence_id
    signals: dict[tuple[str, str, str], IdentitySignal] = {}

    def add(signal: IdentitySignal | None) -> None:
        if signal is not None and signal.value:
            signals.setdefault((signal.family, signal.kind, signal.value), signal)

    if occurrence.identity_strength == "strong":
        add(_provider_signal(occurrence_id, occurrence.source_type, occurrence.source_url, occurrence.source_native_id))
    spotify_id = view.get("spotify_id")
    add(_provider_signal(occurrence_id, "spotify", view.get("spotify_url"), spotify_id))
    add(_provider_signal(occurrence_id, "bandcamp", view.get("bandcamp_url")))
    add(_provider_signal(occurrence_id, "soundcloud", view.get("soundcloud_url")))
    add(_provider_signal(occurrence_id, "lastfm", view.get("lastfm_url")))
    source_url = view.get("source_url")
    for provider in ("triple_j_unearthed", "bandcamp", "soundcloud", "lastfm", "spotify"):
        add(_provider_signal(occurrence_id, provider, source_url, spotify_id if provider == "spotify" else ""))

    instagram_values = [view.get("instagram_handle"), view.get("instagram_url")]
    facebook_values = [view.get("facebook_url")]
    for token in _external_link_tokens(view.get("external_links")):
        instagram_values.append(token)
        facebook_values.append(token)
    for raw in instagram_values:
        handle = _instagram_handle(raw)
        if handle:
            add(_signal(occurrence_id, "social", "instagram_handle", handle, raw, "strong"))
    for raw in facebook_values:
        profile = _facebook_profile(raw)
        if profile:
            add(_signal(occurrence_id, "social", "facebook_profile", profile, raw, "strong"))

    website_raw = view.get("website")
    website_domain = _domain(website_raw)
    if website_domain and website_domain not in _LINK_HUB_HOSTS:
        domain_role = normalize_text(view.get("domain_role"), comparison=True).replace(" ", "_")
        artist_controlled = domain_role in {"artist", "artist_controlled", "official_artist"}
        kind = "artist_domain" if artist_controlled else "website_domain"
        strength = "strong" if artist_controlled else "corroborative"
        add(_signal(occurrence_id, "website", kind, website_domain, website_raw, strength))

    email_raw = view.get("email")
    email = normalize_text(email_raw, comparison=True)
    if email and "@" in email and " " not in email:
        contact_role = normalize_text(view.get("contact_role"), comparison=True).replace(" ", "_")
        contact_type = normalize_text(view.get("contact_type"), comparison=True).replace(" ", "_")
        is_direct = contact_role in {"artist", "self"} and contact_type in {"direct", "artist_direct"}
        kind = "direct_email" if is_direct else "shared_or_unclassified_email"
        strength = "strong" if is_direct else "weak"
        add(_signal(occurrence_id, "contact", kind, email, email_raw, strength))

    artist_raw = view.get("artist_name")
    artist_name = normalize_text(artist_raw, comparison=True)
    if artist_name:
        add(_signal(occurrence_id, "context", "artist_name", artist_name, artist_raw, "contextual"))
    location_raw = view.get("location")
    location = normalize_text(location_raw, comparison=True)
    if location:
        add(_signal(occurrence_id, "context", "location", location, location_raw, "contextual"))

    ordered = tuple(sorted(signals.values(), key=lambda item: (item.family, item.kind, item.value, item.raw_value)))
    return IdentityProfile(source_occurrence=occurrence, signals=ordered)


def identity_profile_from_contracts(
    source_occurrence: SourceOccurrence,
    lead_record: LeadRecord,
) -> IdentityProfile:
    """Extract signals from established contracts without creating a new record model."""
    if source_occurrence.source_occurrence_id not in lead_record.source_occurrence_ids:
        raise ValueError("LeadRecord does not reference the supplied SourceOccurrence")
    row = {
        "Artist Name": source_occurrence.artist_name_raw or lead_record.artist_name_normalized,
        "Location": source_occurrence.location_raw or lead_record.location_normalized,
        "Source Directory": source_occurrence.source_directory or source_occurrence.source_type,
        "Source URL": source_occurrence.source_url,
        "Source Native ID": source_occurrence.source_native_id,
        "Email": lead_record.email,
        "Spotify Artist ID": lead_record.spotify_artist_id,
        "Spotify_URL": lead_record.spotify_url,
        "Bandcamp_URL": lead_record.bandcamp_url,
        "SoundCloud_URL": lead_record.soundcloud_url,
        "LastFM_URL": lead_record.lastfm_url,
    }
    evidence_fields = {
        "artist_name": "Artist Name",
        "location": "Location",
        "email": "Email",
        "spotify_artist_id": "Spotify Artist ID",
        "instagram_url": "Instagram_URL",
        "instagram_handle": "Instagram_Handle",
        "facebook_url": "Facebook_URL",
        "website": "Website",
        "domain_role": "Domain_Role",
        "contact_role": "Contact_Role",
        "contact_type": "Contact_Type",
    }
    for evidence in lead_record.provenance:
        target = evidence_fields.get(evidence.fact_name)
        if target and not str(row.get(target, "") or "").strip() and evidence.value is not None:
            row[target] = evidence.value
    profile = identity_profile_from_row(row)
    if profile.source_occurrence.source_occurrence_id != source_occurrence.source_occurrence_id:
        raise ValueError("contract evidence would change established source_occurrence_id")
    return replace(profile, source_occurrence=source_occurrence)
