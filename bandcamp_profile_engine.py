"""Reusable, profile-only Bandcamp acquisition and parsing.

This module deliberately contains no discovery, CSV, GUI, MusicBrainz, or
Spotify concerns.  Callers supply an optional browser fetcher when their
existing execution path already owns one.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dparser
from requests.adapters import HTTPAdapter
from soundcloud_engine import canonicalize_soundcloud_profile_url
from urllib3.util.retry import Retry


PROFILE_ACCEPTED = "accepted"
PROFILE_CHALLENGE_UNAVAILABLE = "challenge_unavailable"
PROFILE_ERROR = "error"

_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
)
_THREAD_LOCAL = threading.local()
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)
_SOCIAL_HOSTS_PATTERN = (
    r"(?:linktr\.ee|beacons\.ai|solo\.to|hypeddit\.com|toneden\.io|"
    r"carrd\.co|flow\.page|instagram\.com|facebook\.com|x\.com|"
    r"twitter\.com|youtube\.com|tiktok\.com|spotify\.com|"
    r"soundcloud\.com|bandsintown\.com|songkick\.com)"
)
_SOCIAL_TEXT_RE = re.compile(
    rf"((?:https?://|www\.)?{_SOCIAL_HOSTS_PATTERN}[^\s\"'<)]+)", re.I
)
_HANDLE_HINTS = (
    (re.compile(r"(?:instagram|ig)\s*(?::|\-|@)\s*@?([a-z0-9._]{3,})", re.I),
     "https://instagram.com/{handle}"),
    (re.compile(r"(?:twitter|x)\s*(?::|\-|@)\s*@?([a-z0-9_]{3,})", re.I),
     "https://twitter.com/{handle}"),
    (re.compile(r"(?:tiktok)\s*(?::|\-|@)\s*@?([a-z0-9._]{3,})", re.I),
     "https://www.tiktok.com/@{handle}"),
)
_ASSET_EXTENSIONS = frozenset({
    ".avif", ".bmp", ".css", ".eot", ".gif", ".ico", ".jpeg", ".jpg",
    ".js", ".map", ".mjs", ".otf", ".png", ".svg", ".tif", ".tiff",
    ".ttf", ".webp", ".woff", ".woff2",
})
_ASSET_HOST_SUFFIXES = ("bcbits.com", "bandcamp.com", "bandcampcdn.com")
_ASSET_PATH_MARKERS = ("/assets/", "/img/", "/image/", "/images/", "/static/")
_GENERIC_HANDLE_TOKENS = frozenset({
    "about", "account", "accounts", "artist", "artists", "bio", "contact",
    "discography", "event", "events", "facebook", "follow", "help", "home",
    "instagram", "legal", "link", "links", "login", "menu", "message",
    "music", "news", "official", "privacy", "profile", "reel", "reels",
    "share", "shop", "shows", "signup", "support", "terms", "tiktok",
    "tour", "twitter", "website", "x",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep",
    "sept", "september", "oct", "october", "nov", "november", "dec",
    "december",
})
_INSTAGRAM_ROUTE_TOKENS = frozenset({"accounts", "explore", "p", "reel", "reels", "stories"})
_TWITTER_ROUTE_TOKENS = frozenset({"compose", "hashtag", "home", "i", "intent", "search", "share"})
_FACEBOOK_ROUTE_TOKENS = frozenset({
    "events", "groups", "help", "login", "marketplace", "reel", "reels",
    "share", "sharer", "stories", "watch",
})
_LINK_HUB_ROUTE_TOKENS = frozenset({
    "about", "admin", "blog", "features", "help", "legal", "login",
    "marketplace", "pricing", "privacy", "s", "signup", "terms",
})
_LINK_HUB_HOST_SUFFIXES = (
    "beacons.ai", "carrd.co", "flow.page", "hypeddit.com", "linktr.ee",
    "lnk.to", "solo.to", "toneden.io", "withkoji.com",
)
_SOCIAL_HOST_SUFFIXES = (
    "bandsintown.com", "facebook.com", "fb.me", "instagram.com", "spotify.com",
    "songkick.com", "tiktok.com", "twitter.com", "x.com", "youtube.com", "youtu.be",
    *_LINK_HUB_HOST_SUFFIXES,
)
_NON_WEBSITE_HOST_SUFFIXES = (
    "bandsintown.com", "deezer.com", "discogs.com", "last.fm", "lastfm.com",
    "music.apple.com", "musicbrainz.org", "open.spotify.com", "rateyourmusic.com",
    "songkick.com", "soundcloud.com", "spotify.com",
)
_RELEASE_PATTERNS = (
    r"\breleased\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
    r"\breleased\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+(\d{4})",
)
_SOUNDS_PATTERNS = (
    r"\bffo\b[:\-–]\s*([^.;\n]+)",
    r"\briyl\b[:\-–]\s*([^.;\n]+)",
    r"\bfor\s+fans\s+of\b[:\-–]?\s*([^.;\n]+)",
    r"\bsounds\s+like\b[:\-–]?\s*([^.;\n]+)",
    r"\binfluences?\b[:\-–]?\s*([^.;\n]+)",
    r"\binspired\s+by\b[:\-–]?\s*([^.;\n]+)",
)
_COUNTRY_CANON = {
    "the netherlands": "netherlands",
    "holland": "netherlands",
    "u.k.": "united kingdom",
    "u.s.a.": "usa",
}
_SOFT_BLOCK_PATTERNS = (
    "enable javascript",
    "verify you are human",
    "access denied",
)


@dataclass(frozen=True)
class BandcampProfileResult:
    status: str
    canonical_url: str
    profile: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    http_status: Optional[int] = None
    browser_used: bool = False
    identity_evidence: Dict[str, Any] = field(default_factory=dict)


def canonicalize_bandcamp_profile_url(value: str) -> str:
    if not value:
        return ""
    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate.lstrip("/")
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "bandcamp.com" or not host.endswith(".bandcamp.com"):
        return ""
    return f"https://{host}/"


def bandcamp_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Referer": "https://bandcamp.com/",
        "Connection": "keep-alive",
    }


def build_bandcamp_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=False,
    )
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(bandcamp_headers())
    return session


def bandcamp_thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = build_bandcamp_session()
        _THREAD_LOCAL.session = session
    return session


def bandcamp_challenge_reason(html_text: str) -> str:
    if not html_text:
        return ""
    lowered = html_text.lower()
    if any(pattern in lowered for pattern in _SOFT_BLOCK_PATTERNS):
        return "recognized_soft_block"
    try:
        title = BeautifulSoup(html_text, "html.parser").find("title")
        title_text = title.get_text(" ", strip=True) if title else ""
    except Exception:
        title_text = ""
    normalized = re.sub(r"[^a-z0-9]+", " ", title_text.lower()).strip()
    return "client_challenge_title" if normalized == "client challenge" else ""


def _parse_any_date_to_iso(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    clean = " ".join(text.split())
    try:
        parsed = dparser.parse(
            clean, fuzzy=True, dayfirst=False,
            default=datetime.datetime(1900, 1, 1),
        )
        if 2000 <= parsed.year <= datetime.datetime.now().year + 1:
            return parsed.strftime("%Y-%m-%d"), "day"
    except Exception:
        pass
    month_match = re.search(r"\b([A-Za-z]+)\s+(\d{4})\b", clean)
    if month_match:
        try:
            parsed = dparser.parse(
                f"01 {month_match.group(1)} {month_match.group(2)}",
                fuzzy=True, dayfirst=True,
            )
            return parsed.strftime("%Y-%m-%d"), "month"
        except Exception:
            pass
    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", clean)
    if year_match:
        year = int(year_match.group(1))
        if 2000 <= year <= datetime.datetime.now().year + 1:
            return f"{year:04d}-01-01", "year"
    return None, None


def _date_from_json_ld(soup: BeautifulSoup):
    for script in soup.find_all("script", type=lambda value: value and "ld+json" in value):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            for key in ("datePublished", "uploadDate", "dateCreated"):
                raw = obj.get(key)
                if isinstance(raw, str) and raw.strip():
                    iso, precision = _parse_any_date_to_iso(raw)
                    if iso:
                        return iso, precision, raw
    return None, None, None


def _date_from_tralbum_attr(soup: BeautifulSoup):
    for node in soup.find_all(attrs={"data-tralbum": True}):
        try:
            data = json.loads(node.get("data-tralbum") or "")
        except Exception:
            continue
        blocks = [data] if isinstance(data, dict) else []
        if blocks and isinstance(data.get("current"), dict):
            blocks.append(data["current"])
        if blocks and isinstance(data.get("trackinfo"), list):
            blocks.extend(item for item in data["trackinfo"] if isinstance(item, dict))
        for block in blocks:
            for key in ("release_date", "publish_date", "album_release_date", "date"):
                raw = block.get(key)
                if isinstance(raw, str) and raw.strip():
                    iso, precision = _parse_any_date_to_iso(raw)
                    if iso:
                        return iso, precision, raw
    return None, None, None


def _date_from_tralbum_data(soup: BeautifulSoup):
    marker = re.compile(r"var\s+TralbumData\s*=", re.I)
    for script in soup.find_all("script"):
        text = script.string or ""
        match = marker.search(text)
        if not match:
            continue
        remainder = text[match.end():]
        start = remainder.find("{")
        if start < 0:
            continue
        depth = 0
        end = None
        for index, character in enumerate(remainder[start:]):
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = start + index + 1
                    break
        try:
            data = json.loads(remainder[start:end]) if end is not None else {}
        except Exception:
            continue
        blocks = [data] if isinstance(data, dict) else []
        if blocks and isinstance(data.get("current"), dict):
            blocks.append(data["current"])
        if blocks and isinstance(data.get("trackinfo"), list):
            blocks.extend(item for item in data["trackinfo"] if isinstance(item, dict))
        for block in blocks:
            for key in ("release_date", "publish_date", "date", "album_release_date"):
                raw = block.get(key)
                if isinstance(raw, str) and raw.strip():
                    iso, precision = _parse_any_date_to_iso(raw)
                    if iso:
                        return iso, precision, raw
    return None, None, None


def _date_from_meta(soup: BeautifulSoup):
    selectors = (
        'meta[itemprop="datePublished"], meta[itemprop="dateCreated"], '
        'meta[name="date"], meta[property="music:release_date"]'
    )
    for meta in soup.select(selectors):
        raw = (meta.get("content") or meta.get("value") or "").strip()
        iso, precision = _parse_any_date_to_iso(raw)
        if iso:
            return iso, precision, raw
    description = soup.select_one('meta[property="og:description"], meta[name="description"]')
    raw = (description.get("content") or "").strip() if description else ""
    if "released" in raw.lower():
        iso, precision = _parse_any_date_to_iso(raw)
        if iso:
            return iso, precision, raw
    return None, None, None


def _date_from_time(soup: BeautifulSoup):
    for element in soup.find_all("time"):
        for raw in ((element.get("datetime") or "").strip(), element.get_text(" ", strip=True)):
            iso, precision = _parse_any_date_to_iso(raw)
            if iso:
                return iso, precision, raw
    return None, None, None


def _date_from_text(soup: BeautifulSoup):
    containers = soup.select(".tralbum-credits, .tralbumData, #trackInfoInner, #bio-container")
    text = " ".join(node.get_text(" ", strip=True) for node in containers) or soup.get_text(" ", strip=True)
    for pattern in _RELEASE_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            raw = match.group(0)
            iso, precision = _parse_any_date_to_iso(raw)
            if iso:
                return iso, precision, raw
    return None, None, None


def bandcamp_extract_release_date(html_text: str) -> Dict[str, Optional[str]]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for extractor in (
        _date_from_json_ld, _date_from_tralbum_attr, _date_from_tralbum_data,
        _date_from_meta, _date_from_time, _date_from_text,
    ):
        try:
            iso, precision, raw = extractor(soup)
        except Exception:
            continue
        if iso:
            return {"date_iso": iso, "precision": precision, "raw": raw}
    return {"date_iso": None, "precision": None, "raw": None}


def _tralbum_payloads(soup: BeautifulSoup):
    """Yield decoded Bandcamp tralbum payloads without interpreting titles."""
    for node in soup.find_all(attrs={"data-tralbum": True}):
        try:
            data = json.loads(node.get("data-tralbum") or "")
        except Exception:
            continue
        if isinstance(data, dict):
            yield data

    marker = re.compile(r"var\s+TralbumData\s*=", re.I)
    for script in soup.find_all("script"):
        text = script.string or ""
        match = marker.search(text)
        if not match:
            continue
        remainder = text[match.end():]
        start = remainder.find("{")
        if start < 0:
            continue
        depth = 0
        end = None
        in_string = False
        escaped = False
        for index, character in enumerate(remainder[start:]):
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = start + index + 1
                    break
        try:
            data = json.loads(remainder[start:end]) if end is not None else {}
        except Exception:
            continue
        if isinstance(data, dict):
            yield data


def bandcamp_extract_track_title(html_text: str) -> str:
    """Return a demonstrable track title, never a bare album/release title.

    Bandcamp's ``current.title`` and page heading can name an album or EP, so
    neither is accepted by itself. A title must come from the release's
    structured ``trackinfo`` list, a MusicRecording JSON-LD object, or a
    concrete track-list row. The first stable listed track is deterministic.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")
    for payload in _tralbum_payloads(soup):
        trackinfo = payload.get("trackinfo")
        if not isinstance(trackinfo, list):
            current = payload.get("current")
            trackinfo = current.get("trackinfo") if isinstance(current, dict) else None
        if isinstance(trackinfo, list):
            for track in trackinfo:
                if not isinstance(track, dict):
                    continue
                title = str(track.get("title") or "").strip()
                if title:
                    return title

    for script in soup.find_all("script", type=lambda value: value and "ld+json" in value):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        pending = list(data) if isinstance(data, list) else [data]
        while pending:
            item = pending.pop(0)
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = {str(value).casefold() for value in item_type} if isinstance(item_type, list) else {str(item_type).casefold()}
            if "musicrecording" in types:
                title = str(item.get("name") or "").strip()
                if title:
                    return title
            for key in ("track", "tracks", "itemListElement"):
                child = item.get(key)
                if isinstance(child, list):
                    pending.extend(child)
                elif isinstance(child, dict):
                    pending.append(child)

    selectors = (
        "#track_table tr.track_row_view .track-title",
        "#track_table .track-title",
        "tr.track_row_view [itemprop='name']",
        "a[itemprop='item'] [itemprop='name']",
    )
    for selector in selectors:
        for node in soup.select(selector):
            title = node.get_text(" ", strip=True)
            title = re.sub(r"^\s*\d+[.)]?\s*", "", title).strip()
            if title:
                return title
    return ""


def _norm_tokens(line: str):
    parts = re.split(r"[,/|•]+|\band\b|\&", line or "", flags=re.I)
    output = []
    seen = set()
    for part in parts:
        token = re.sub(r"\s+", " ", part).strip(" .;:()[]{}\"\u2013\u2014").strip()
        if token and token.lower() not in seen:
            seen.add(token.lower())
            output.append(token)
    return output


def bandcamp_extract_genres(soup: BeautifulSoup):
    tags = set()
    for anchor in soup.select(".tralbum-tags a, a.tag, #tags a"):
        text = anchor.get_text(" ", strip=True)
        if text:
            tags.add(text.lower())
    keywords = soup.select_one('meta[name="keywords"]')
    if keywords and keywords.get("content"):
        tags.update(token.lower() for token in _norm_tokens(keywords["content"]) if token)
    return list(tags)


def bandcamp_extract_sounds_like(soup: BeautifulSoup) -> str:
    blocks = [
        node.get_text(" ", strip=True)
        for node in soup.select("#bio-container, .tralbum-credits, .tralbumData, #trackInfoInner")
    ]
    description = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if description and description.get("content"):
        blocks.append(description["content"])
    text = re.sub(r"\s+", " ", " \n".join(filter(None, blocks))).strip()
    for pattern in _SOUNDS_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match and match.group(1):
            tokens = _norm_tokens(match.group(1))
            if tokens:
                return ", ".join(token.title() for token in tokens[:5])
    return ""


def _canonical_location(location: str) -> str:
    location = (location or "").strip()
    parts = [part.strip() for part in location.split(",")]
    if len(parts) < 2:
        return location
    city = " ".join(word.capitalize() if word.isalpha() else word for word in re.split(r"\s+", parts[0]))
    country = _COUNTRY_CANON.get(", ".join(parts[1:]).lower(), ", ".join(parts[1:]).lower())
    return f"{city}, {' '.join(word.capitalize() for word in country.split())}"


def _normalize_external_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlparse(value)
        if (parsed.hostname or "").lower().endswith("l.soundcloud.com"):
            target = parse_qs(parsed.query or "").get("url") or parse_qs(parsed.query or "").get("q")
            if target:
                value = unquote(target[0])
    except Exception:
        pass
    value = re.sub(r"[?&](?:utm_[^=&]+|fbclid|gclid|mc_cid|mc_eid)=[^&]+", "", value, flags=re.I)
    return re.sub(r"[?&]$", "", value)


def _host_matches(host: str, domain: str) -> bool:
    host = (host or "").lower().split(":", 1)[0]
    domain = (domain or "").lower()
    return host == domain or host.endswith("." + domain)


def _url_parts(value: str) -> Tuple[str, Tuple[str, ...]]:
    try:
        parsed = urlparse(value)
    except Exception:
        return "", ()
    host = (parsed.hostname or "").lower()
    segments = tuple(unquote(part).strip() for part in (parsed.path or "").split("/") if part.strip())
    return host, segments


def _is_asset_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return True
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not host:
        return True
    if any(_host_matches(host, suffix) for suffix in _ASSET_HOST_SUFFIXES):
        return True
    if any(marker in path for marker in _ASSET_PATH_MARKERS):
        return True
    filename = path.rsplit("/", 1)[-1]
    extension = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    return extension in _ASSET_EXTENSIONS


def _valid_handle(handle: str, *, inferred: bool) -> bool:
    clean = unquote(handle or "").strip().lstrip("@").lower()
    if not clean or clean in _GENERIC_HANDLE_TOKENS:
        return False
    if not re.fullmatch(r"[a-z0-9._-]{2,64}", clean, re.I):
        return False
    if inferred and (
        re.search(r"\.(?:com|net|org|co|io|fm|tv|band|music)$", clean)
        or clean.startswith(("www.", "http"))
    ):
        return False
    return True


def _social_candidate(value: str, *, inferred: bool = False) -> Tuple[str, str]:
    """Return a validated Bandcamp outbound social/profile candidate."""
    host, segments = _url_parts(value)
    if not host:
        return "", ""
    first = segments[0] if segments else ""
    first_lower = first.lower()

    if _host_matches(host, "instagram.com"):
        if len(segments) != 1 or first_lower in _INSTAGRAM_ROUTE_TOKENS or not _valid_handle(first, inferred=inferred):
            return "", ""
        return "instagram", value
    if _host_matches(host, "twitter.com") or _host_matches(host, "x.com"):
        if len(segments) != 1 or first_lower in _TWITTER_ROUTE_TOKENS or not _valid_handle(first, inferred=inferred):
            return "", ""
        return "twitter", value
    if _host_matches(host, "facebook.com") or host == "fb.me":
        if len(segments) != 1 or first_lower in _FACEBOOK_ROUTE_TOKENS or not _valid_handle(first, inferred=inferred):
            return "", ""
        return "facebook", value
    if _host_matches(host, "tiktok.com"):
        handle = first.lstrip("@")
        if len(segments) != 1 or not first.startswith("@") or not _valid_handle(handle, inferred=inferred):
            return "", ""
        return "tiktok", value
    if _host_matches(host, "youtube.com") or host == "youtu.be":
        if host == "youtu.be" or not segments:
            return "", ""
        if first_lower in {"watch", "shorts", "playlist", "results", "feed"}:
            return "", ""
        if first.startswith("@") or first_lower in {"channel", "c", "user"}:
            return "youtube", value
        return "", ""
    if any(_host_matches(host, suffix) for suffix in _LINK_HUB_HOST_SUFFIXES):
        if len(segments) != 1 or first_lower in _LINK_HUB_ROUTE_TOKENS or not _valid_handle(first, inferred=False):
            return "", ""
        return "linktree", value
    if _host_matches(host, "spotify.com"):
        if len(segments) >= 2 and first_lower == "artist":
            return "spotify", value
        return "", ""
    if _host_matches(host, "soundcloud.com"):
        canonical = canonicalize_soundcloud_profile_url(value)
        return ("soundcloud", canonical) if canonical else ("", "")
    if _host_matches(host, "bandsintown.com"):
        if segments and first_lower in {"a", "artist", "artists"}:
            return "bandsintown", value
        return "", ""
    if _host_matches(host, "songkick.com"):
        if len(segments) >= 2 and first_lower == "artists":
            return "songkick", value
        return "", ""
    return "", ""


def _credible_website(value: str) -> bool:
    if _is_asset_url(value):
        return False
    host, _ = _url_parts(value)
    if not host or any(
        _host_matches(host, suffix)
        for suffix in (*_NON_WEBSITE_HOST_SUFFIXES, *_SOCIAL_HOST_SUFFIXES)
    ):
        return False
    social_kind, _ = _social_candidate(value)
    return not social_kind


def _artist_name(soup: BeautifulSoup) -> str:
    site = soup.find("meta", attrs={"property": "og:site_name"})
    if site and site.get("content") and site["content"].strip():
        return site["content"].strip()
    title = soup.find("meta", attrs={"property": "og:title"})
    if title and title.get("content"):
        left = title["content"].strip().split("·")[0].strip()
        if left:
            return left
    for selector in (".band-name", "h1.band-name", "h1.title", "#name-section h1", "header h1", "h2.band-name"):
        element = soup.select_one(selector)
        if element and element.get_text(" ", strip=True):
            return element.get_text(" ", strip=True)
    header = soup.select_one("header a[href]")
    return header.get_text(" ", strip=True) if header else ""


def parse_bandcamp_profile_html(
    profile_url: str,
    html_text: str,
    seed_primary_genre: str = "",
    *,
    release_fetcher: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Parse one artist profile using the former GUI parser behavior."""
    canonical_url = canonicalize_bandcamp_profile_url(profile_url) or profile_url
    if not html_text or bandcamp_challenge_reason(html_text):
        return {}
    soup = BeautifulSoup(html_text, "html.parser")
    genres = bandcamp_extract_genres(soup)
    primary_genre = (seed_primary_genre or (genres[0] if genres else "")).strip()
    if not genres and primary_genre:
        genres = [primary_genre]
    profile: Dict[str, Any] = {
        "artist_name": _artist_name(soup),
        "profile_url": canonical_url,
        "location": "",
        "website": "",
        "email": "",
        "emails": [],
        "socials": {key: "" for key in (
            "instagram", "twitter", "facebook", "tiktok", "youtube", "linktree",
            "spotify", "soundcloud", "bandsintown", "songkick",
        )},
        "genres": genres,
        "latest_release_title": "",
        "latest_release_date": "",
        "latest_release_precision": "",
        "latest_track_title": "",
        "latest_track_date": "",
        "sounds_like": bandcamp_extract_sounds_like(soup),
        "primary_genre": primary_genre,
        "source_tag": "",
    }
    release = bandcamp_extract_release_date(html_text)
    if release.get("date_iso"):
        profile["latest_release_date"] = release["date_iso"]
        profile["latest_release_precision"] = release.get("precision") or ""
    location = soup.find(class_=re.compile("location", re.I))
    if location:
        profile["location"] = location.get_text(" ", strip=True)
    profile["location"] = _canonical_location(profile["location"])
    collected_links = []
    seen_links = set()
    explicit_socials = set()

    def record_email(value: str) -> None:
        clean = (value or "").strip()
        if clean and clean not in profile["emails"]:
            profile["emails"].append(clean)
            if not profile["email"]:
                profile["email"] = clean

    def consume(candidate: str, *, inferred: bool = False) -> None:
        candidate = (candidate or "").strip().strip("()[]{}<>.,; ")
        if not candidate:
            return
        if candidate.lower().startswith("mailto:"):
            record_email(candidate.split(":", 1)[1].split("?", 1)[0])
            return
        candidate = candidate.split("#", 1)[0]
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif candidate.startswith("/"):
            candidate = urljoin(canonical_url, candidate)
        elif candidate.startswith("www."):
            candidate = "https://" + candidate
        elif not urlparse(candidate).scheme:
            candidate = "https://" + candidate
        normalized = _normalize_external_url(candidate)
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()
        if not parsed.scheme.startswith("http") or not host or host.endswith("bandcamp.com"):
            return
        if _is_asset_url(normalized):
            return
        social_kind, social_url = _social_candidate(normalized, inferred=inferred)
        if social_kind:
            if inferred and social_kind in explicit_socials:
                return
            if not inferred:
                explicit_socials.add(social_kind)
            if not profile["socials"].get(social_kind) or not inferred:
                profile["socials"][social_kind] = social_url
            return
        if inferred:
            return
        if _credible_website(normalized) and not profile["website"]:
            profile["website"] = normalized

    for anchor in soup.find_all("a", href=True):
        consume(anchor["href"])
    texts = []
    for selector in (
        "#bio-container", "#bio-text", ".bio-container", ".bio-text", ".bio",
        ".band-bio", ".profile-bio", "#rightColumn", "#right-column",
        ".rightColumn", ".tralbum-about", ".tralbumData",
    ):
        texts.extend(node.get_text(" ", strip=True) for node in soup.select(selector))
    for block in filter(None, texts):
        for email in _EMAIL_RE.findall(block):
            record_email(email)
        for candidate in _SOCIAL_TEXT_RE.findall(block):
            consume(candidate)
        for pattern, template in _HANDLE_HINTS:
            for handle in pattern.findall(block):
                handle = handle.strip().lstrip("@").strip(".,/ ")
                if handle:
                    consume(template.format(handle=handle), inferred=True)
    for value in [profile["website"], *profile["socials"].values()]:
        if value and value not in seen_links:
            seen_links.add(value)
            collected_links.append(value)
    profile["all_social_links"] = collected_links

    release_item = soup.find("li", class_=re.compile("music-grid-item", re.I))
    if release_item:
        title = release_item.find(class_=re.compile("title", re.I))
        if title:
            profile["latest_release_title"] = title.get_text(strip=True)
        selected_release_date = ""
        date = release_item.find(class_=re.compile("release", re.I))
        if date and not profile["latest_release_date"]:
            raw = date.get_text(strip=True)
            iso, precision = _parse_any_date_to_iso(raw)
            profile["latest_release_date"] = iso or raw
            profile["latest_release_precision"] = precision or ""
            selected_release_date = iso or ""
        elif date:
            raw = date.get_text(strip=True)
            selected_release_date = _parse_any_date_to_iso(raw)[0] or ""
        anchor = release_item.find("a", href=True)
        if release_fetcher and anchor:
            release_url = urljoin(canonical_url, anchor.get("href", ""))
            if release_url and release_url != canonical_url:
                release_html = release_fetcher(release_url)
                if release_html and not bandcamp_challenge_reason(release_html):
                    release = bandcamp_extract_release_date(release_html)
                    if release.get("date_iso"):
                        profile["latest_release_date"] = release["date_iso"]
                        profile["latest_release_precision"] = release.get("precision") or profile["latest_release_precision"]
                    track_title = bandcamp_extract_track_title(release_html)
                    track_date = release.get("date_iso") or selected_release_date
                    if track_title and track_date:
                        profile["latest_track_title"] = track_title
                        profile["latest_track_date"] = track_date
                    if not profile["latest_release_title"]:
                        release_soup = BeautifulSoup(release_html, "html.parser")
                        title = release_soup.select_one("h2.trackTitle") or release_soup.select_one(".trackTitle") or release_soup.select_one("h1")
                        if title:
                            profile["latest_release_title"] = title.get_text(" ", strip=True)
    if not profile["latest_release_title"]:
        title = soup.find(class_=re.compile("trackTitle", re.I))
        if title:
            profile["latest_release_title"] = title.get_text(strip=True)
    if not profile["latest_release_date"]:
        date = soup.find(class_=re.compile("release-date", re.I))
        if date:
            raw = date.get_text(strip=True)
            iso, precision = _parse_any_date_to_iso(raw)
            profile["latest_release_date"] = iso or raw
            profile["latest_release_precision"] = precision or profile["latest_release_precision"]
    if not profile["latest_release_date"]:
        credits = soup.find("div", class_=re.compile(r"tralbum-credits", re.I))
        if credits:
            raw = credits.get_text(" ", strip=True)
            match = re.search(r"released\s+(.+)", raw, re.I)
            raw = match.group(1).strip() if match else raw.strip()
            iso, precision = _parse_any_date_to_iso(raw)
            profile["latest_release_date"] = iso or raw
            profile["latest_release_precision"] = precision or profile["latest_release_precision"]
    if not profile["latest_release_date"]:
        profile["latest_release_date"] = "not present"
    return profile


def _identity_evidence(html_text: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    values = []
    for node in soup.select("[itemprop='byArtist']"):
        name_node = node.select_one("[itemprop='name']")
        value = (name_node or node).get_text(" ", strip=True)
        value = re.sub(r"^\s*by\s+", "", value, flags=re.I).strip()
        if value:
            values.append(value)
    if profile.get("artist_name") and profile["artist_name"] not in values:
        values.append(profile["artist_name"])
    return {"artist_names": tuple(values), "page_artist": values[0] if values else ""}


def fetch_bandcamp_profile(
    profile_url: str,
    *,
    session: Optional[requests.Session] = None,
    browser_fetcher: Optional[Callable[[str], str]] = None,
    browser_on_empty: bool = True,
    seed_primary_genre: str = "",
    timeout: Tuple[float, float] = (6, 15),
) -> BandcampProfileResult:
    """Acquire and parse one known Bandcamp artist profile.

    A browser callback is attempted at most once after a challenge, or after
    an empty/error response when ``browser_on_empty`` is enabled. Challenge
    pages remain neutral and are never treated as an identity contradiction.
    """
    canonical_url = canonicalize_bandcamp_profile_url(profile_url)
    if not canonical_url:
        return BandcampProfileResult(PROFILE_ERROR, "", reason="invalid_profile_url")
    active_session = session or bandcamp_thread_session()
    html_text = ""
    status = None
    request_reason = ""
    try:
        response = active_session.get(canonical_url, timeout=timeout, headers=bandcamp_headers())
        status = getattr(response, "status_code", None)
        response.raise_for_status()
        html_text = getattr(response, "text", "") or ""
    except Exception as exc:
        request_reason = f"network_error:{type(exc).__name__}"

    challenge = bandcamp_challenge_reason(html_text)
    http_challenge = bool(challenge)
    browser_used = False
    if browser_fetcher and (challenge or (browser_on_empty and not html_text)):
        browser_used = True
        try:
            browser_html = browser_fetcher(canonical_url) or ""
        except Exception:
            browser_html = ""
        if browser_html:
            html_text = browser_html
            challenge = bandcamp_challenge_reason(html_text)

    if challenge:
        return BandcampProfileResult(
            PROFILE_CHALLENGE_UNAVAILABLE, canonical_url, reason=challenge,
            http_status=status, browser_used=browser_used,
        )
    if not html_text:
        return BandcampProfileResult(
            PROFILE_ERROR, canonical_url, reason=request_reason or "profile_unavailable",
            http_status=status, browser_used=browser_used,
        )

    def release_fetcher(url: str) -> str:
        try:
            response = active_session.get(url, timeout=timeout, headers=bandcamp_headers())
            response.raise_for_status()
            release_html = getattr(response, "text", "") or ""
        except Exception:
            release_html = ""
        if browser_fetcher and bandcamp_challenge_reason(release_html):
            try:
                browser_html = browser_fetcher(url) or ""
            except Exception:
                browser_html = ""
            if browser_html and not bandcamp_challenge_reason(browser_html):
                return browser_html
        return release_html

    preferred_release_url = ""
    try:
        requested = urlparse(profile_url if "://" in profile_url else f"https://{profile_url}")
        requested_host = (requested.hostname or "").lower()
        canonical_host = (urlparse(canonical_url).hostname or "").lower()
        requested_path = re.sub(r"/+", "/", requested.path or "")
        if requested_host == canonical_host and re.fullmatch(r"/(?:track|album)/[^/]+/?", requested_path, re.I):
            preferred_release_url = f"https://{canonical_host}{requested_path.rstrip('/')}"
    except Exception:
        preferred_release_url = ""

    profile = parse_bandcamp_profile_html(
        canonical_url, html_text, seed_primary_genre,
        release_fetcher=release_fetcher,
    )
    if profile and preferred_release_url:
        release_html = release_fetcher(preferred_release_url)
        if release_html and not bandcamp_challenge_reason(release_html):
            track_title = bandcamp_extract_track_title(release_html)
            release = bandcamp_extract_release_date(release_html)
            if track_title and release.get("date_iso"):
                profile["latest_track_title"] = track_title
                profile["latest_track_date"] = release["date_iso"]
    if not profile:
        return BandcampProfileResult(
            PROFILE_ERROR, canonical_url, reason="profile_parse_failed",
            http_status=status, browser_used=browser_used,
        )
    identity_evidence = _identity_evidence(html_text, profile)
    if browser_used and http_challenge and not identity_evidence.get("page_artist"):
        return BandcampProfileResult(
            PROFILE_CHALLENGE_UNAVAILABLE,
            canonical_url,
            reason="browser_profile_unavailable",
            http_status=status,
            browser_used=True,
        )
    return BandcampProfileResult(
        PROFILE_ACCEPTED, canonical_url, profile=profile,
        http_status=status, browser_used=browser_used,
        identity_evidence=identity_evidence,
    )
