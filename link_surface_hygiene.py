"""Conservative hygiene for artist-facing operational link fields.

This module deliberately uses URL semantics only.  It rejects application and
public-shell destinations while retaining artist profiles, link-hub profiles,
and ordinary official domains.  Callers that are parsing a link hub should pass
``from_link_hub=True`` so app-store and same-hub shell links are also excluded.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse


_LINK_HUB_HOSTS = {
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "lnk.bio",
    "bio.site",
    "campsite.bio",
    "flow.page",
    "solo.to",
    "withkoji.com",
    "carrd.co",
    "taplink.cc",
    "linkin.bio",
}

_SHELL_PATH_SEGMENTS = {
    "about",
    "admin",
    "analytics",
    "blog",
    "careers",
    "cookie",
    "cookies",
    "discover",
    "explore",
    "help",
    "legal",
    "login",
    "log-in",
    "marketplace",
    "press",
    "privacy",
    "product",
    "products",
    "register",
    "search",
    "share",
    "sharer",
    "sign-in",
    "sign-up",
    "signin",
    "signup",
    "support",
    "terms",
}

_PLATFORM_ROUTE_SEGMENTS = {
    "about",
    "accounts",
    "ads",
    "business",
    "careers",
    "developers",
    "download",
    "explore",
    "help",
    "home",
    "intent",
    "legal",
    "login",
    "privacy",
    "search",
    "share",
    "sharer",
    "signup",
    "support",
    "terms",
}

_LINKTREE_BRAND_HANDLES = {
    "linktr.ee",
    "linktree",
    "linktreeapp",
    "linktreeofficial",
    "linktreehq",
}

_TRACKING_HOSTS = {
    "click.linktr.ee",
    "email-link.linktr.ee",
    "links.linktr.ee",
}

_TRACKING_QUERY_KEYS = {
    "redirect",
    "redirect_url",
    "target",
    "url",
}


def _host(value: str) -> str:
    try:
        host = (urlparse(value).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _segments(value: str) -> tuple[str, ...]:
    try:
        return tuple(
            unquote(part).strip()
            for part in (urlparse(value).path or "").split("/")
            if unquote(part).strip()
        )
    except Exception:
        return ()


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _clean_handle(value: str) -> str:
    return unquote(value or "").strip().lstrip("@").casefold()


def _valid_handle(value: str) -> bool:
    handle = _clean_handle(value)
    return bool(
        handle
        and handle not in _PLATFORM_ROUTE_SEGMENTS
        and re.fullmatch(r"[a-z0-9._-]{2,100}", handle, re.I)
    )


def is_artist_link_hub_profile(value: str) -> bool:
    """Return True only for a plausible, direct artist link-hub profile."""
    host = _host(value)
    parts = _segments(value)
    if host not in _LINK_HUB_HOSTS or len(parts) != 1:
        return False
    handle = _clean_handle(parts[0])
    return bool(
        _valid_handle(handle)
        and handle not in _SHELL_PATH_SEGMENTS
        and handle not in _LINKTREE_BRAND_HANDLES
    )


def _valid_platform_artist_destination(value: str) -> Optional[bool]:
    """Return True/False for known platforms, or None for an ordinary domain."""
    host = _host(value)
    parts = _segments(value)
    if not host:
        return False
    first = parts[0] if parts else ""
    first_lower = first.casefold()

    if _host_matches(host, "instagram.com") or _host_matches(host, "threads.net"):
        return len(parts) == 1 and _valid_handle(first)
    if _host_matches(host, "twitter.com") or host == "x.com" or host.endswith(".x.com"):
        return len(parts) == 1 and _valid_handle(first)
    if _host_matches(host, "tiktok.com"):
        return len(parts) == 1 and first.startswith("@") and _valid_handle(first)
    if _host_matches(host, "soundcloud.com"):
        return len(parts) == 1 and _valid_handle(first)
    if _host_matches(host, "facebook.com") or host == "fb.me":
        if not parts or first_lower in _PLATFORM_ROUTE_SEGMENTS:
            return False
        if first_lower == "pages":
            return len(parts) >= 2 and bool(parts[1].strip())
        return len(parts) == 1 and _valid_handle(first)
    if _host_matches(host, "youtube.com"):
        if not parts or first_lower in {"watch", "shorts", "playlist", "results", "feed"}:
            return False
        if first.startswith("@"):
            return len(parts) == 1 and _valid_handle(first)
        return first_lower in {"channel", "c", "user"} and len(parts) >= 2
    if host == "youtu.be":
        return False
    if _host_matches(host, "spotify.com"):
        return len(parts) >= 2 and first_lower == "artist" and bool(parts[1])
    if _host_matches(host, "discord.com") or host == "discord.gg":
        # Invite destinations can be artist communities.  Only known Linktree
        # company promotion is rejected; identity is otherwise left intact.
        invite = parts[-1] if parts else ""
        return bool(parts) and _clean_handle(invite) not in _LINKTREE_BRAND_HANDLES
    return None


def is_artist_platform_profile(value: str) -> bool:
    """Return whether a known social/streaming URL identifies an artist surface."""
    return _valid_platform_artist_destination((value or "").strip()) is True


def is_useful_artist_link(
    value: str,
    *,
    from_link_hub: bool = False,
    source_hub_url: str = "",
    anchor_context: str = "",
    artist_name: str = "",
) -> bool:
    """Whether ``value`` belongs in an operational artist link surface."""
    raw = (value or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False

    host = _host(raw)
    parts = _segments(raw)
    lowered_parts = {_clean_handle(part) for part in parts}
    if host in _TRACKING_HOSTS or lowered_parts.intersection(_SHELL_PATH_SEGMENTS):
        return False
    if from_link_hub:
        source_host = _host(source_hub_url)
        if source_host and (host == source_host or host in _LINK_HUB_HOSTS):
            return False
    if host in _LINK_HUB_HOSTS:
        if not is_artist_link_hub_profile(raw):
            return False
        if artist_name:
            artist_key = re.sub(r"[^a-z0-9]+", "", artist_name.casefold())
            handle_key = re.sub(r"[^a-z0-9]+", "", _clean_handle(parts[0]))
            return bool(
                len(artist_key) >= 4
                and len(handle_key) >= 4
                and (artist_key in handle_key or handle_key in artist_key)
            )
        return True
    if parts and _clean_handle(parts[-1]) in _LINKTREE_BRAND_HANDLES:
        return False

    if from_link_hub:
        source_host = _host(source_hub_url)
        context = (anchor_context or "").casefold()
        if source_host == "linktr.ee" and "linktree" in context:
            return False
        if host in {"apps.apple.com", "play.google.com"}:
            return False
        query = parse_qs(parsed.query or "")
        if host.endswith("linktr.ee") or any(key.casefold() in _TRACKING_QUERY_KEYS for key in query):
            return False

    platform_result = _valid_platform_artist_destination(raw)
    if platform_result is False:
        return False
    if platform_result is True:
        return True

    # App distribution URLs and bare generic platform roots are never useful
    # artist/contact surfaces, even if they arrived from a later merge.
    if host in {"apps.apple.com", "play.google.com"}:
        return False
    return bool(parts or parsed.path in {"", "/"})
