"""Shared SoundCloud scraping/enrichment engine used by Day + Night modes.

Public contract (minimal):
- SoundCloudEngine.people_search(query, place, max_results) -> list[str]
    Returns SoundCloud handles ordered as returned by the v2 search API.
- SoundCloudEngine.fetch_profile(handle) -> dict
    Returns parsed profile payload with keys: emails[list], external_urls[list],
    website[str], display_name[str], city[str], country[str], genre[str],
    latest_track_* fields, sounds_like[str], tracks_source[str], status[str],
    reason[str], challenge_page[bool], elapsed_ms[int]. Status values match
    existing flows: 'actionable', 'non_actionable_challenge', 'blocked_403',
    'no_contacts'.
- SoundCloudEngine.find_candidates(artist, location, max_results) -> list[dict]
    Lightweight rediscovery helper returning candidate dicts with handle,
    profile_url, display_name, location, score, rank_score.

The module preserves existing environment flags:
- SC_ADAPTIVE_ABOUT_DISABLE (adaptive about-page disable)
- NIGHT_SC_DEBUG (verbose debug logging in Night Mode)
- NIGHTMODE_SC_ENGINE (Night Mode can still force alternate engines)
and mirrors the same circuit-breaker behaviour (about challenge rate, root 403).
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests
from html_fetcher import fetch_html
from bs4 import BeautifulSoup, FeatureNotFound
from requests.adapters import HTTPAdapter, Retry
from dateutil.relativedelta import relativedelta

# ---------------------------
# SoundCloud Configuration (mirrors day-mode defaults)
# ---------------------------
SC_HANDLE_RE = re.compile(r"^https?://soundcloud\.com/([a-z0-9][a-z0-9._-]{1,49})/?$", re.IGNORECASE)
SC_HANDLE_BAN = {
    "charts",
    "feed",
    "upload",
    "terms-of-use",
    "imprint",
    "transparency-reports",
    "pages",
    "you",
    "stream",
    "discover",
    "explore",
    "popular",
    "search",
}
SC_AGGREGATOR_ALLOWLIST = ("linktr.ee", "beacons.ai", "solo.to", "hypeddit.com", "toneden.io")
SC_AGGREGATOR_PREFERENCE = (
    "linktr.ee",
    "beacons.ai",
    "solo.to",
    "hypeddit.com",
    "toneden.io",
    "bandcamp.com",
    "carrd.co",
    "flow.page",
)
SC_REQUEST_TIMEOUT = (5, 10)
SC_SEARCH_USERS_API = "https://api-v2.soundcloud.com/search/users"
SC_MAX_WORKERS = 8
SC_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soundcloud_about_cache.json")
SC_CACHE_MAX_AGE_DAYS = 7
SC_DEBUG_LATEST = bool(os.getenv("SC_DEBUG_LATEST"))
SC_ADAPTIVE_ABOUT_DISABLE = bool(os.getenv("SC_ADAPTIVE_ABOUT_DISABLE"))
SC_ABOUT_CHALLENGE_WINDOW = int(os.getenv("SC_ABOUT_CHALLENGE_WINDOW", "5"))
SC_ABOUT_CHALLENGE_THRESHOLD = float(os.getenv("SC_ABOUT_CHALLENGE_THRESHOLD", "0.60"))
SC_CLIENT_ID_CANDIDATES = [
    c for c in [(os.environ.get("SC_CLIENT_ID") or "").strip()] if c
]
NIGHT_SC_DEBUG = bool(os.getenv("NIGHT_SC_DEBUG"))


def canonicalize_soundcloud_profile_url(value: str) -> str:
    """Return a canonical SoundCloud artist profile URL, or an empty string."""
    candidate = (value or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "soundcloud.com" or parsed.query:
        return ""
    canonical_candidate = f"https://soundcloud.com{parsed.path or ''}"
    match = SC_HANDLE_RE.fullmatch(canonical_candidate)
    if not match:
        return ""
    handle = match.group(1).lower()
    if handle in SC_HANDLE_BAN:
        return ""
    return f"https://soundcloud.com/{handle}"

UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
]

SC_HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}

URL_RE = re.compile(
    r"https?://(?:linktr\.ee|beacons\.ai|solo\.to|hypeddit\.com|toneden\.io|bandcamp\.com|carrd\.co|flow\.page|instagram\.com|facebook\.com|x\.com|twitter\.com|youtube\.com|tiktok\.com)[^\s\"'<)]+",
    re.IGNORECASE,
)

SC_ASSET_JS_PATTERN = re.compile(r"https://a-v2\.sndcdn\.com/assets/\d+-[a-z0-9]+\.js", re.IGNORECASE)
SC_CLIENT_ID_PATTERN = re.compile(r'client_id:"([a-zA-Z0-9]+)"')

SC_CONTACT_TEXT_SELECTORS = (
    ".profileLinks__contactText",
    ".profileLinks__body",
    ".profileLinks__content",
    ".profileLinks__description",
    ".profileLinks__text",
    ".profileLinks__cta",
    "[data-testid='profileLinksContactText']",
    "[data-testid='profile-links-contact-text']",
)

SC_CONTACT_FALLBACK_SELECTORS = (
    ".profileSidebar",
    "[class*='profileLinks']",
)

_SC_SOUNDS_PATTERNS = [
    r"\bffo\b[:\-–]\s*([^.;\n]+)",
    r"\briyl\b[:\-–]\s*([^.;\n]+)",
    r"\bfor\s+fans\s+of\b[:\-–]?\s*([^.;\n]+)",
    r"\bsounds\s+like\b[:\-–]?\s*([^.;\n]+)",
    r"\binfluences?\b[:\-–]?\s*([^.;\n]+)",
    r"\binspired\s+by\b[:\-–]?\s*([^.;\n]+)",
]

_SC_SOCIAL_DOMAINS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.me": "facebook",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "linktr.ee": "linktree",
    "linktree": "linktree",
    "withkoji.com": "linktree",
    "beacons.ai": "linktree",
    "spotify.com": "spotify",
    "bandsintown.com": "bandsintown",
    "songkick.com": "songkick",
}

_SC_WEBSITE_DENYLIST = {
    "open.spotify.com",
    "music.apple.com",
    "songkick.com",
    "www.songkick.com",
    "bandsintown.com",
    "www.bandsintown.com",
}

_RESERVED_SC = {
    "search",
    "popular",
    "charts",
    "stream",
    "you",
    "discover",
    "stations",
    "groups",
    "pro",
    "for",
    "creators",
    "repost",
    "likes",
    "home",
    "soundcloud",
    "soundcloud-scenes",
    "radio",
    "radio-indie",
}

_SC_THREAD_LOCAL = threading.local()
_SC_CLIENT_ID_LOCK = threading.Lock()
_SC_CLIENT_ID = None
_SC_HANDLE_UID_MAP: Dict[str, str] = {}
_SC_HANDLE_USEROBJ_MAP: Dict[str, dict] = {}
_SC_RUN_LOCK = threading.Lock()
_SC_RUN_STATS: Optional[Dict[str, int]] = None
_SC_ABOUT_DISABLED = False
_SC_ABOUT_DISABLE_LOGGED = False
_SC_ROOT_FORBIDDEN = False
_SC_ROOT_FORBIDDEN_LOGGED = False
_ENGINE_SESSION = None
_AGGREGATOR_BUDGET_CHECK = None


def _rand_headers():
    headers = dict(SC_HEADERS_BASE)
    headers["User-Agent"] = random.choice(UAS)
    return headers


def build_hardened_session():
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
    adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(_rand_headers())
    return session


def polite_sleep(min_ms=120, max_ms=240):
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


_PARSER_USED = None


def get_soup(html: str):
    """Prefer lxml; fallback to html.parser if lxml is unavailable."""
    global _PARSER_USED
    try:
        soup = BeautifulSoup(html or "", "lxml")
        if _PARSER_USED is None:
            _PARSER_USED = "lxml"
        return soup
    except FeatureNotFound:
        soup = BeautifulSoup(html or "", "html.parser")
        if _PARSER_USED is None:
            _PARSER_USED = "html.parser"
        return soup


def _strip_tracking(u: str) -> str:
    u = re.sub(r"[?&](?:utm_[^=&]+|fbclid|gclid|mc_cid|mc_eid)=[^&]+", "", u, flags=re.IGNORECASE)
    u = re.sub(r"[?&]$", "", u)
    return u


def normalize_external_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    try:
        parsed = urlparse(u)
        host = (parsed.hostname or "").lower()
        if host.endswith("l.soundcloud.com"):
            qs = parse_qs(parsed.query or "")
            target = qs.get("url") or qs.get("q") or []
            if target:
                candidate = unquote(target[0])
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                u = candidate
    except Exception:
        pass
    return _strip_tracking(u)


def _parse_any_date_to_iso(text: str):
    """Convert various human dates to (iso, precision)."""
    if not text:
        return "", ""
    raw = text.strip()
    if not raw:
        return "", ""
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return raw, "day"
        if re.match(r"^\d{4}-\d{2}$", raw):
            return raw + "-01", "month"
        if re.match(r"^\d{4}$", raw):
            return raw + "-01-01", "year"
    except Exception:
        return "", ""
    try:
        from dateutil import parser

        dt = parser.parse(raw)
        if dt:
            return dt.date().isoformat(), "day"
    except Exception:
        return "", ""
    return "", ""


def _norm_tokens(line: str) -> list:
    if not line:
        return []
    parts = re.split(r"[,/|•]+|\band\b|\&", line, flags=re.IGNORECASE)
    cleaned = []
    for part in parts:
        token = re.sub(r"\s+", " ", part).strip(" .;:()[]{}\"\u2013\u2014").strip()
        if token:
            cleaned.append(token)
    seen = set()
    unique = []
    for token in cleaned:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    return unique


def _sc_test_client_id(session, candidate: str) -> bool:
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": "https://soundcloud.com/soundcloud", "client_id": candidate},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        return resp.status_code == 200
    except Exception:
        return False


def _sc_scrape_client_id(session) -> str:
    sources = ["https://soundcloud.com", "https://soundcloud.com/discover"]
    for source in sources:
        try:
            resp = session.get(source, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
            resp.raise_for_status()
        except Exception:
            continue
        assets = SC_ASSET_JS_PATTERN.findall(resp.text or "")
        for asset_url in assets[:20]:
            try:
                js_resp = session.get(asset_url, timeout=SC_REQUEST_TIMEOUT)
                js_resp.raise_for_status()
                match = SC_CLIENT_ID_PATTERN.search(js_resp.text or "")
                if match:
                    return match.group(1)
            except Exception:
                continue
    return ""


def _sc_get_client_id(session) -> str:
    global _SC_CLIENT_ID
    with _SC_CLIENT_ID_LOCK:
        if _SC_CLIENT_ID:
            return _SC_CLIENT_ID
        for candidate in SC_CLIENT_ID_CANDIDATES:
            if _sc_test_client_id(session, candidate):
                _SC_CLIENT_ID = candidate
                return _SC_CLIENT_ID
        scraped = _sc_scrape_client_id(session)
        if scraped:
            _SC_CLIENT_ID = scraped
            return _SC_CLIENT_ID
    return ""


def _sc_stat_inc(key: str, n: int = 1):
    global _SC_RUN_STATS
    if _SC_RUN_STATS is None:
        return
    with _SC_RUN_LOCK:
        _SC_RUN_STATS[key] = int(_SC_RUN_STATS.get(key, 0)) + int(n)


def format_run_stats_summary(stats: Dict[str, int]) -> str:
    """
    Helper for diagnostics: build a single-line summary of run stats.
    """
    stats = stats or {}
    return (
        "handles={handles_total} "
        "actionable={actionable_written} "
        "about_attempts={about_attempts} "
        "about_challenges={about_challenges} "
        "about_disabled={about_disabled} "
        "root_403={root_403} "
        "tracks_api_401={tracks_api_401} "
        "tracks_api_403={tracks_api_403} "
        "tracks_api_blocked={tracks_api_blocked} "
        "rss_used={rss_used} "
        "api_user_fallback_used={api_user_fallback_used}"
    ).format(
        handles_total=stats.get("handles_total", 0),
        actionable_written=stats.get("actionable_written", 0),
        about_attempts=stats.get("about_attempts", 0),
        about_challenges=stats.get("about_challenges", 0),
        about_disabled=stats.get("about_disabled", 0),
        root_403=stats.get("root_403", 0),
        tracks_api_401=stats.get("tracks_api_401", 0),
        tracks_api_403=stats.get("tracks_api_403", 0),
        tracks_api_blocked=stats.get("tracks_api_blocked", 0),
        rss_used=stats.get("rss_used", 0),
        api_user_fallback_used=stats.get("api_user_fallback_used", 0),
    )


class SoundCloudAboutCache:
    def __init__(self, path: str = SC_CACHE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = None

    def _ensure_loaded(self):
        if self._data is not None:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except Exception:
            self._data = {}

    def get(self, handle: str):
        with self._lock:
            self._ensure_loaded()
            entry = self._data.get(handle)
            if not entry:
                return None
            ts = entry.get("ts") or 0
            age_days = (time.time() - ts) / 86400.0
            if age_days > SC_CACHE_MAX_AGE_DAYS:
                return None
            return entry

    def set(self, handle: str, payload: dict, etag: str = "", last_modified: str = ""):
        with self._lock:
            self._ensure_loaded()
            self._data[handle] = {
                "ts": time.time(),
                "etag": etag or "",
                "last_modified": last_modified or "",
                "data": payload,
            }
            self._persist()

    def _persist(self):
        tmp_path = self.path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp_path, self.path)
        except Exception:
            pass


SC_ABOUT_CACHE = SoundCloudAboutCache()
SC_ABOUT_CACHE_REQUIRED_KEYS = (
    "latest_track_title",
    "latest_track_release_date",
    "latest_track_tags",
    "sounds_like",
    "bio_text",
)


def _sc_norm_user_id(user_id) -> str:
    if not user_id:
        return ""
    if isinstance(user_id, int):
        return str(user_id)
    s = str(user_id).strip()
    if "soundcloud:users:" in s:
        s = s.split("soundcloud:users:")[-1]
    match = re.search("(\\d+)", s)
    return match.group(1) if match else ""


def _sc_resolve_handle_uid(session, handle: str) -> str:
    if not handle:
        return ""
    cached = _SC_HANDLE_UID_MAP.get(handle)
    if cached:
        uid = _sc_norm_user_id(cached)
        if uid and uid != cached:
            _SC_HANDLE_UID_MAP[handle] = uid
        return uid
    client_id = _sc_get_client_id(session)
    if not client_id:
        return ""
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": f"https://soundcloud.com/{handle}", "client_id": client_id},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        uid = _sc_norm_user_id(data.get("id") or data.get("urn") or "")
        if uid:
            _SC_HANDLE_UID_MAP[handle] = uid
            return uid
    except Exception:
        pass
    return ""


def _sc_track_release_iso(track: dict) -> tuple:
    if not isinstance(track, dict):
        return ("", "")
    for field in ("release_date", "display_date", "created_at"):
        raw = (track.get(field) or "").strip()
        if not raw:
            continue
        iso, precision = _parse_any_date_to_iso(raw)
        if iso:
            return (iso, precision or "day")
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return (raw[:10], "day")
    return ("", "")


def _sc_fetch_latest_track_rss(session, user_id, handle: str = "") -> dict:
    uid = _sc_norm_user_id(user_id)
    if not uid:
        return {}
    rss_url = f"https://feeds.soundcloud.com/users/soundcloud:users:{uid}/sounds.rss"
    try:
        resp = session.get(rss_url, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        resp.raise_for_status()
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        root = ET.fromstring(resp.text)
        first_item = None
        for item in root.findall(".//item"):
            first_item = item
            break
        if first_item is None:
            return {}
        title = (first_item.findtext("title") or "").strip()
        pub_date_raw = (first_item.findtext("pubDate") or "").strip()
        iso_date = ""
        precision = ""
        if pub_date_raw:
            try:
                dt = parsedate_to_datetime(pub_date_raw)
                if dt:
                    iso_date = dt.date().isoformat()
                    precision = "day"
            except Exception:
                iso_date = ""
                precision = ""
        track_url = (first_item.findtext("link") or "").strip()
        return {
            "title": title,
            "release_date": iso_date,
            "precision": precision,
            "genre": "",
            "tags": [],
            "permalink_url": track_url,
        }
    except Exception:
        return {}


def _sc_fetch_latest_track_metadata(session, client_id: str, user_id, handle: str = "") -> dict:
    uid = _sc_norm_user_id(user_id)
    if not uid:
        return {}
    rss_track = _sc_fetch_latest_track_rss(session, uid, handle)
    if rss_track and (rss_track.get("title") or rss_track.get("release_date") or rss_track.get("genre") or rss_track.get("tags")):
        rss_track["source"] = "rss"
        rss_track["permalink_url"] = rss_track.get("permalink_url") or ""
        return rss_track
    if not client_id:
        return rss_track or {}
    api_url = f"https://api-v2.soundcloud.com/users/{uid}/tracks"
    params = {"client_id": client_id, "limit": 1, "linked_partitioning": 1, "order": "published_at"}
    try:
        resp = session.get(api_url, params=params, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        if resp.status_code in (401, 403):
            if resp.status_code == 403:
                _sc_stat_inc("tracks_api_403")
            if resp.status_code == 401:
                _sc_stat_inc("tracks_api_401")
            _sc_stat_inc("tracks_api_blocked")
            return rss_track or {}
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception:
        return rss_track or {}
    collection = []
    if isinstance(payload, dict):
        collection = payload.get("collection") or []
    elif isinstance(payload, list):
        collection = payload
    for track in collection:
        if not isinstance(track, dict):
            continue
        iso_date, precision = _sc_track_release_iso(track)
        tag_tokens = _norm_tokens(track.get("tag_list") or "")
        return {
            "title": track.get("title") or "",
            "release_date": iso_date,
            "precision": precision or "day" if iso_date else "",
            "genre": track.get("genre") or "",
            "tags": tag_tokens[:8],
            "source": "api",
            "permalink_url": track.get("permalink_url") or "",
        }
    return rss_track or {}


def _sc_resolve_track_from_url(session, track_url: str, handle: str = "") -> dict:
    client_id = _sc_get_client_id(session)
    if not client_id or not track_url:
        return {}
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": track_url, "client_id": client_id},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("kind") != "track":
        return {}
    tags = _norm_tokens(data.get("tag_list") or data.get("tags") or "")
    return {"genre": data.get("genre") or "", "tags": tags[:8], "source": "resolve"}


def _resolve_country_name(value: str) -> str:
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""
    lower = raw.lower()
    overrides = {"uk": "United Kingdom", "usa": "United States", "us": "United States"}
    if lower in overrides:
        return overrides[lower]
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return raw


def _sc_fetch_api_profile(session, handle: str) -> dict:
    fallback_uid = _SC_HANDLE_UID_MAP.get(handle) or ""
    client_id = _sc_get_client_id(session)
    if not client_id:
        return {}
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": f"https://soundcloud.com/{handle}", "client_id": client_id},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if fallback_uid:
            data = {"username": handle, "id": fallback_uid}
        else:
            return {}
    uid_norm = _sc_norm_user_id(data.get("id") or data.get("urn") or fallback_uid)
    if handle and uid_norm:
        _SC_HANDLE_UID_MAP[handle] = uid_norm
    profile = {
        "display_name": data.get("full_name") or data.get("username"),
        "city": data.get("city") or "",
        "country": _resolve_country_name(data.get("country_code")),
        "genre": data.get("genre") or data.get("primary_genre") or "",
        "external_urls": [],
        "description": data.get("description") or "",
        "latest_track_title": "",
        "latest_track_release_date": "",
        "latest_track_precision": "",
        "latest_track_genre": "",
        "latest_track_tags": [],
        "latest_track_source": "",
        "latest_track_permalink": "",
        "latest_track_url": "",
        "user_genre": (data.get("genre") or "").strip(),
    }
    user_urn = data.get("urn") or ""
    user_id = uid_norm or data.get("id") or fallback_uid or ""
    user_identifier = user_id or user_urn or fallback_uid
    if user_urn:
        try:
            wp_resp = session.get(
                f"https://api-v2.soundcloud.com/users/{user_urn}/web-profiles",
                params={"client_id": client_id},
                timeout=SC_REQUEST_TIMEOUT,
                headers=_rand_headers(),
            )
            if wp_resp.status_code == 200:
                for item in wp_resp.json() or []:
                    url = item.get("url") if isinstance(item, dict) else None
                    if url:
                        profile["external_urls"].append(url)
        except Exception:
            pass
    latest_track = _sc_fetch_latest_track_metadata(session, client_id, user_identifier, handle)
    if latest_track:
        profile["latest_track_title"] = latest_track.get("title") or ""
        profile["latest_track_release_date"] = latest_track.get("release_date") or ""
        profile["latest_track_precision"] = latest_track.get("precision") or ""
        profile["latest_track_genre"] = latest_track.get("genre") or ""
        profile["latest_track_tags"] = latest_track.get("tags") or []
        profile["latest_track_source"] = latest_track.get("source") or ""
        profile["latest_track_permalink"] = latest_track.get("permalink_url") or ""
        profile["latest_track_url"] = latest_track.get("permalink_url") or ""
    return profile


def _sc_fetch_user_fallback_links(session, handle: str):
    urls, emails = set(), set()
    client_id = _sc_get_client_id(session)
    if not client_id or not handle:
        return urls, emails
    try:
        resolve_resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": f"https://soundcloud.com/{handle}", "client_id": client_id},
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resolve_resp.raise_for_status()
        data = resolve_resp.json() or {}
        uid = _sc_norm_user_id(data.get("id") or data.get("urn") or "")
    except Exception:
        return urls, emails
    if handle and uid:
        _SC_HANDLE_UID_MAP[handle] = uid

    desc = (data.get("description") or "").strip()
    if desc:
        urls.update(URL_RE.findall(desc))
        _sc_collect_emails_from_text(emails, desc)
    website = data.get("website") or ""
    if website:
        urls.add(website)

    if uid:
        try:
            user_resp = session.get(
                f"https://api-v2.soundcloud.com/users/{uid}",
                params={"client_id": client_id},
                timeout=SC_REQUEST_TIMEOUT,
                headers=_rand_headers(),
            )
            user_resp.raise_for_status()
            user_data = user_resp.json() or {}
            web_profiles = user_data.get("web_profiles") or user_data.get("website_profiles") or []
            if isinstance(web_profiles, list):
                for item in web_profiles:
                    if isinstance(item, str):
                        u = item
                        if u.startswith("http"):
                            urls.add(u)
                    elif isinstance(item, dict):
                        u = item.get("url") or ""
                        if u and u.startswith("http"):
                            urls.add(u)
            website = user_data.get("website") or ""
            if website:
                urls.add(website)
            desc2 = user_data.get("description") or ""
            if desc2 and desc2 != desc:
                urls.update(URL_RE.findall(desc2))
                _sc_collect_emails_from_text(emails, desc2)
        except Exception:
            pass
    return urls, emails


def _sc_extract_contact_text_sections(doc):
    if doc is None:
        return []
    seen = set()
    sections = []
    for selector in SC_CONTACT_TEXT_SELECTORS:
        for node in doc.select(selector):
            text = node.get_text(" ", strip=True)
            normalized = re.sub(r"\s+", " ", text).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                sections.append(normalized)
    if not sections:
        for selector in SC_CONTACT_FALLBACK_SELECTORS:
            for node in doc.select(selector):
                text = node.get_text(" ", strip=True)
                normalized = re.sub(r"\s+", " ", text).strip()
                if not normalized or normalized in seen:
                    continue
                lower = normalized.lower()
                if "@" not in normalized and not any(keyword in lower for keyword in ("book", "contact", "enquiry", "inquiry", "press", "mgmt")):
                    continue
                seen.add(normalized)
                sections.append(normalized)
    return sections


def _sc_collect_emails_from_text(bucket: set, text: str):
    if not text:
        return
    for address in extract_emails(text):
        cleaned = (address or "").strip()
        if cleaned and not cleaned.lower().endswith("@soundcloud.com"):
            bucket.add(cleaned)


def _is_aggregator_link(url: str) -> bool:
    if not url:
        return False
    cleaned = (url or "").strip().strip(" ,.;:)>]\"'")
    if not cleaned:
        return False
    if cleaned.lower().startswith("mailto:"):
        return False
    if not re.match(r"^[a-z]+://", cleaned, flags=re.IGNORECASE):
        cleaned = "https://" + cleaned.lstrip("/")
    try:
        host = (urlparse(cleaned).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == allow or host.endswith("." + allow) for allow in SC_AGGREGATOR_ALLOWLIST)


def _aggregator_budget_ok() -> Tuple[bool, str]:
    allowed = True
    reason = ""
    if _AGGREGATOR_BUDGET_CHECK:
        try:
            result = _AGGREGATOR_BUDGET_CHECK()
            if isinstance(result, tuple):
                allowed = bool(result[0])
                if len(result) > 1:
                    reason = str(result[1] or "")
            else:
                allowed = bool(result)
        except Exception:
            allowed = True
            reason = ""
    return allowed, reason


def _fetch_aggregator_emails(session: requests.Session, url: str, artist_name: str = "") -> List[str]:
    target = (url or "").strip()
    if not _is_aggregator_link(target):
        print("[Aggregator] skipped: not_allowlisted")
        return []

    allowed, reason = _aggregator_budget_ok()
    if not allowed:
        print(f"[Aggregator] skipped: {reason or 'budget_exhausted'}")
        return []

    target = (url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        target = "https://" + target.lstrip("/")
    domain = (urlparse(target).hostname or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    artist_label = artist_name or ""
    print(f"[Aggregator] detected {domain or '<unknown>'} for '{artist_label or '<unknown>'}'")
    emails: List[str] = []
    try:
        resp = session.get(target, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        status = getattr(resp, "status_code", None)
        if status and status >= 400:
            print("[Aggregator] emails_found=0")
            print("[Aggregator] no emails found")
            polite_sleep()
            return []
        html = getattr(resp, "text", "") or ""
        for mail in extract_emails(html):
            normalized = (mail or "").strip().lower()
            if normalized and normalized not in emails:
                emails.append(normalized)
        print(f"[Aggregator] emails_found={len(emails)}")
        if not emails:
            print("[Aggregator] no emails found")
    except Exception:
        print("[Aggregator] emails_found=0")
        print("[Aggregator] no emails found")
    finally:
        polite_sleep()
    return emails


def expand_for_email(session, url):
    mails = set()
    if not url:
        return sorted(mails)
    if _is_aggregator_link(url):
        return sorted(mails)
    try:
        resp = session.get(url, timeout=(6, 12), headers=_rand_headers())
        if resp.status_code >= 400:
            polite_sleep()
            return sorted(mails)
        doc = get_soup(resp.text)
        for a in doc.select('a[href^="mailto:"]'):
            href = (a.get("href") or "").strip()
            if href.startswith("mailto:"):
                mails.add(href.replace("mailto:", "").split("?", 1)[0])
    except Exception:
        pass
    polite_sleep()
    return sorted(mails)


def extract_emails(text):
    email_pattern = r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
    return list(set(re.findall(email_pattern, text or "")))


def _sc_sounds_like_from_bio(bio_text: str) -> str:
    if not bio_text:
        return ""
    text = re.sub(r"\s+", " ", bio_text).strip()
    matches = []
    for pattern in _SC_SOUNDS_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.group(1):
            matches.append(match.group(1))
    if not matches:
        return ""
    combined = ", ".join(matches)
    tokens = re.split(r"[,/|•]+|\band\b|\&", combined, flags=re.IGNORECASE)
    seen = set()
    clean = []
    for token in tokens:
        trimmed = re.sub(r"\s+", " ", token).strip(" .;:()[]{}\"")
        lowered = trimmed.lower()
        if trimmed and lowered not in seen:
            seen.add(lowered)
            clean.append(trimmed.title())
    return ", ".join(clean[:6])


def _sc_thread_session() -> requests.Session:
    global _ENGINE_SESSION
    if _ENGINE_SESSION is not None:
        return _ENGINE_SESSION
    session = getattr(_SC_THREAD_LOCAL, "session", None)
    if session is None:
        session = build_hardened_session()
        _SC_THREAD_LOCAL.session = session
    return session


def _sc_postprocess_payload(payload: dict) -> dict:
    """Normalize external_urls, promote bare emails, and pick website consistently."""
    if not isinstance(payload, dict):
        return payload or {}
    emails = set(payload.get("emails") or [])
    urls = payload.get("external_urls") or []
    norm_exts = []
    seen_norm = set()
    email_re = re.compile(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,})")

    for url in urls:
        raw = (url or "").strip()
        if not raw:
            continue
        lower = raw.lower()
        if not lower.startswith(("http://", "https://")) and email_re.fullmatch(raw):
            emails.add(raw)
            continue
        normalized = normalize_external_url(raw)
        if not normalized or normalized in seen_norm:
            continue
        seen_norm.add(normalized)
        norm_exts.append(normalized)

    def _is_social(host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in _SC_SOCIAL_DOMAINS)

    def _is_denied(host: str) -> bool:
        return any(host == d or host.endswith(d) for d in _SC_WEBSITE_DENYLIST)

    website_url = ""
    for pref in SC_AGGREGATOR_PREFERENCE:
        for url in norm_exts:
            host = (urlparse(url).hostname or "").lower()
            if not host or _is_denied(host) or _is_social(host):
                continue
            if host.endswith(pref):
                website_url = url
                break
        if website_url:
            break

    if not website_url:
        for url in norm_exts:
            host = (urlparse(url).hostname or "").lower()
            if not host or _is_social(host) or _is_denied(host):
                continue
            website_url = url
            break

    payload["external_urls"] = norm_exts
    payload["emails"] = sorted(emails)
    payload["website"] = website_url
    return payload


def extract_sc_links(session: requests.Session, handle: str) -> dict:
    global _SC_ABOUT_DISABLED, _SC_ABOUT_DISABLE_LOGGED, _SC_ROOT_FORBIDDEN, _SC_ROOT_FORBIDDEN_LOGGED
    cached = SC_ABOUT_CACHE.get(handle)
    if cached:
        cached_data = cached.get("data", {}) or {}
        exts = [u for u in (cached_data.get("external_urls") or []) if u and u.lower() != "http://firefox.com"]
        emails = cached_data.get("emails") or []
        website_cached = cached_data.get("website") or ""
        has_required = all(key in cached_data for key in SC_ABOUT_CACHE_REQUIRED_KEYS)
        if cached_data and (exts or emails) and has_required:
            cached_data["external_urls"] = exts
            cached_data["status"] = "actionable" if (exts or emails or cached_data.get("website")) else "no_contacts"
            cached_data["reason"] = "cache_hit"
            cached_data["challenge_page"] = False
            return _sc_postprocess_payload(cached_data)
        if cached_data and (exts or emails or website_cached):
            cached_data["external_urls"] = exts
            cached_data["reason"] = "cache_hit_partial"
            cached_data["status"] = "actionable" if (exts or emails or website_cached) else "no_contacts"
            cached_data["challenge_page"] = False
            return _sc_postprocess_payload(cached_data)

    external_urls, emails = set(), set()
    display_name = handle
    user_city = ""
    user_country = ""
    user_genre = ""
    t0 = time.perf_counter()
    html = ""
    profile_html = ""
    bio_text = ""
    latest_title = ""
    latest_release = ""
    latest_precision = ""
    latest_genre = ""
    latest_tags = []
    latest_source = ""
    latest_track_url = ""
    root_url = f"https://soundcloud.com/{handle}"
    about_url = f"{root_url}/about"
    contact_text_seen = set()

    def _record_contact_text(text):
        if not text:
            return
        normalized = re.sub(r"\s+", " ", text)
        normalized = (normalized or "").strip()
        if not normalized or normalized in contact_text_seen:
            return
        contact_text_seen.add(normalized)
        _sc_collect_emails_from_text(emails, normalized)

    challenge_page = False
    doc = None
    if not (SC_ADAPTIVE_ABOUT_DISABLE and _SC_ABOUT_DISABLED):
        _sc_stat_inc("about_attempts")
        try:
            resp = session.get(about_url, timeout=(6, 12), headers=_rand_headers())
            resp.raise_for_status()
            html = resp.text or ""
        except Exception:
            # Playwright fallback only on clear blocks.
            try:
                res = fetch_html(
                    about_url,
                    session=session,
                    directory="soundcloud",
                    allow_browser_fallback=True,
                    timeout_s=12,
                )
                if res.get("mode_used") == "playwright" and (res.get("html") or ""):
                    html = res.get("html") or ""
                else:
                    html = ""
            except Exception:
                html = ""
        finally:
            polite_sleep()

    if html:
        lowered = html.lower()
        if any(term in lowered for term in ("captcha", "verify you are human", "enable javascript", "cloudflare", "attention required", "enable cookies")):
            challenge_page = True
            _sc_stat_inc("about_challenges")
            if SC_ADAPTIVE_ABOUT_DISABLE:
                with _SC_RUN_LOCK:
                    attempts = int((_SC_RUN_STATS or {}).get("about_attempts", 0))
                    challenges = int((_SC_RUN_STATS or {}).get("about_challenges", 0))
                if attempts and attempts >= SC_ABOUT_CHALLENGE_WINDOW:
                    rate = challenges / attempts
                    if rate >= SC_ABOUT_CHALLENGE_THRESHOLD and not _SC_ABOUT_DISABLED:
                        _SC_ABOUT_DISABLED = True
                        _sc_stat_inc("about_disabled", 1)
                        if not _SC_ABOUT_DISABLE_LOGGED and NIGHT_SC_DEBUG:
                            print(
                                f"SoundCloud: about disabled for this run (challenge_rate={rate:.2f} attempts={attempts} challenges={challenges})"
                            )
                            _SC_ABOUT_DISABLE_LOGGED = True
        else:
            doc = get_soup(html)

    if doc:
        name_el = doc.select_one("h1, .profileHeaderInfo__userName, .profileHeaderInfo__content")
        if name_el:
            text = name_el.get_text(strip=True)
            if text:
                display_name = text
        for snippet in _sc_extract_contact_text_sections(doc):
            _record_contact_text(snippet)
        for a in doc.select(
            'a[href^="mailto:"], '
            'a[href*="instagram.com"], a[href*="facebook.com"], '
            'a[href*="linktr.ee"], a[href*="bandcamp.com"], '
            'a[href*="youtube.com"], a[href*="tiktok.com"], '
            'a[href*="twitter.com"], a[href*="x.com"], '
            'a[href*="beacons.ai"], a[href*="carrd.co"], '
            'a[href*="flow.page"], a[href*="solo.to"], '
            'a[href*="hypeddit.com"], a[href*="toneden.io"]'
        ):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            if href.startswith("mailto:"):
                emails.add(href.replace("mailto:", "").split("?", 1)[0])
            else:
                external_urls.add(href)
        for script in doc.find_all("script"):
            txt = script.string or ""
            if not txt or ("http" not in txt and "sameAs" not in txt):
                continue
            external_urls.update(URL_RE.findall(txt))
            try:
                data = json.loads(txt)
            except Exception:
                continue
            stack = [data]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for key, value in cur.items():
                        if isinstance(value, (list, tuple)) and key and key.lower() in (
                            "sameas",
                            "externalurls",
                            "externallinks",
                            "external_url",
                            "socials",
                        ):
                            for item in value:
                                if isinstance(item, str) and item.startswith("http"):
                                    external_urls.add(item)
                        elif isinstance(value, (dict, list)):
                            stack.append(value)
                        elif isinstance(value, str) and value.startswith("http"):
                            external_urls.add(value)
                elif isinstance(cur, list):
                    stack.extend(cur)
        if not external_urls:
            external_urls.update(URL_RE.findall(html))
        bio_el = (
            doc.select_one(".profileHeaderInfo__bio")
            or doc.select_one(".about__description")
            or doc.select_one("[data-testid='profile-bio']")
        )
        if bio_el:
            bio_text = bio_el.get_text(" ", strip=True)
            _record_contact_text(bio_text)
        try:
            text_blob = doc.get_text(" ", strip=True)
        except Exception:
            text_blob = ""
        _record_contact_text(text_blob)

    if not _SC_ROOT_FORBIDDEN:
        try:
            root_resp = session.get(root_url, timeout=(6, 12), headers=_rand_headers())
            if root_resp.status_code == 403:
                _SC_ROOT_FORBIDDEN = True
                if not _SC_ROOT_FORBIDDEN_LOGGED:
                    if NIGHT_SC_DEBUG:
                        print("[warn] SoundCloud root fetch 403; disabling root fetches for this run")
                    _SC_ROOT_FORBIDDEN_LOGGED = True
                _sc_stat_inc("root_403")
            else:
                root_resp.raise_for_status()
                profile_html = root_resp.text
        except Exception:
            pass
        finally:
            polite_sleep()

    if (challenge_page or _SC_ROOT_FORBIDDEN or not profile_html) and not external_urls:
        fb_urls, fb_emails = _sc_fetch_user_fallback_links(session, handle)
        if fb_urls or fb_emails:
            external_urls.update(fb_urls)
            emails.update(fb_emails)
            _sc_stat_inc("api_user_fallback_used")

    if profile_html:
        profile_doc = get_soup(profile_html)
        for snippet in _sc_extract_contact_text_sections(profile_doc):
            _record_contact_text(snippet)
        for meta in profile_doc.select(
            "meta[property='og:description'], "
            "meta[property='twitter:description'], "
            "meta[name='description'], "
            "meta[name='twitter:description']"
        ):
            _record_contact_text(meta.get("content") or "")
        try:
            text_blob = profile_doc.get_text(" ", strip=True)
        except Exception:
            text_blob = ""
        if text_blob:
            _record_contact_text(text_blob[:4000])

    aggregator_link = None
    aggregators = [u for u in external_urls if _is_aggregator_link(u)]

    def _agg_rank(u: str) -> Tuple[int, str]:
        host = (urlparse((u or "").strip()).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        pref_idx = len(SC_AGGREGATOR_PREFERENCE)
        for idx, pref_host in enumerate(SC_AGGREGATOR_PREFERENCE):
            if host == pref_host or host.endswith("." + pref_host):
                pref_idx = idx
                break
        return (pref_idx, (u or "").lower())

    if aggregators:
        aggregator_link = sorted(aggregators, key=_agg_rank)[0]

    aggregator_emails: List[str] = []
    aggregator_detected = bool(aggregator_link)
    aggregator_fetch_attempted = False
    aggregator_expanded = False  # legacy: expand_for_email executed
    if aggregator_link:
        allowed, reason = _aggregator_budget_ok()
        if not allowed:
            print(f"[Aggregator] skipped: {reason or 'budget_exhausted'}")
        else:
            aggregator_fetch_attempted = True
            aggregator_emails = _fetch_aggregator_emails(session, aggregator_link, display_name)
            for mail in aggregator_emails:
                emails.add(mail)

    # Expand a single non-allowlisted hub (bandcamp/carrd/flow.page) for mailto links.
    EXPANDABLE_DOMAINS = tuple(d for d in SC_AGGREGATOR_PREFERENCE if d not in SC_AGGREGATOR_ALLOWLIST)
    expandable_candidates = [u for u in sorted(external_urls) if not _is_aggregator_link(u)]
    for candidate in expandable_candidates:
        host = (urlparse((candidate or "").strip()).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not any(host == d or host.endswith("." + d) for d in EXPANDABLE_DOMAINS):
            continue
        mails = expand_for_email(session, candidate)
        aggregator_expanded = True
        for mail in mails:
            cleaned = (mail or "").strip().lower()
            if cleaned:
                emails.add(cleaned)
        break

    api_profile = _sc_fetch_api_profile(session, handle)
    if api_profile:
        if api_profile.get("display_name"):
            display_name = api_profile["display_name"]
        user_city = api_profile.get("city") or user_city
        user_country = api_profile.get("country") or user_country
        user_genre = api_profile.get("user_genre") or api_profile.get("genre") or user_genre
        external_urls.update(api_profile.get("external_urls") or [])
        profile_description = api_profile.get("description") or ""
        if profile_description:
            if not bio_text:
                bio_text = profile_description
            _record_contact_text(profile_description)
        latest_title = api_profile.get("latest_track_title") or latest_title
        latest_release = api_profile.get("latest_track_release_date") or latest_release
        latest_precision = api_profile.get("latest_track_precision") or latest_precision
        latest_genre = api_profile.get("latest_track_genre") or latest_genre
        latest_tags = api_profile.get("latest_track_tags") or latest_tags
        latest_source = api_profile.get("latest_track_source") or latest_source
        if not latest_track_url:
            latest_track_url = api_profile.get("latest_track_permalink") or api_profile.get("latest_track_url") or latest_track_url

    uid_hint = _SC_HANDLE_UID_MAP.get(handle)
    if uid_hint and (not latest_title or not latest_release):
        lt = _sc_fetch_latest_track_metadata(session, _sc_get_client_id(session), uid_hint, handle)
        if lt:
            if not latest_title and lt.get("title"):
                latest_title = lt.get("title")
            if not latest_release and lt.get("release_date"):
                latest_release = lt.get("release_date")
                latest_precision = latest_precision or lt.get("precision") or ""
            if not latest_tags and lt.get("tags"):
                latest_tags = lt.get("tags")
            if not latest_source and lt.get("source"):
                latest_source = lt.get("source")
            if not latest_track_url and lt.get("permalink_url"):
                latest_track_url = lt.get("permalink_url")

    tracks_source = latest_source or ("rss" if latest_title or latest_release else "")
    if tracks_source == "rss" and (not latest_genre or not latest_tags):
        rss_track_url = latest_track_url or ""
        if rss_track_url:
            resolved = _sc_resolve_track_from_url(session, rss_track_url, handle)
            if resolved:
                if not latest_genre and resolved.get("genre"):
                    latest_genre = resolved.get("genre")
                if not latest_tags and resolved.get("tags"):
                    latest_tags = resolved.get("tags")
                if not latest_source and resolved.get("source"):
                    latest_source = resolved.get("source")

    norm_exts = []
    seen_norm = set()
    email_re = re.compile(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
    for url in external_urls:
        raw = (url or "").strip()
        if not raw:
            continue
        lower = raw.lower()
        if not lower.startswith(("http://", "https://")):
            if email_re.fullmatch(raw):
                emails.add(raw)
                continue
        normalized = normalize_external_url(raw)
        if not normalized or normalized in seen_norm:
            continue
        seen_norm.add(normalized)
        norm_exts.append(normalized)

    website_url = ""
    def _is_social(host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in _SC_SOCIAL_DOMAINS)

    def _is_denied(host: str) -> bool:
        return any(host == d or host.endswith(d) for d in _SC_WEBSITE_DENYLIST)

    for pref in SC_AGGREGATOR_PREFERENCE:
        for url in norm_exts:
            host = (urlparse(url).hostname or "").lower()
            if not host or _is_denied(host) or _is_social(host):
                continue
            if host.endswith(pref):
                website_url = url
                break
        if website_url:
            break

    if not website_url:
        for url in norm_exts:
            host = (urlparse(url).hostname or "").lower()
            if not host or _is_social(host) or _is_denied(host):
                continue
            website_url = url
            break

    elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
    payload = {
        "handle": handle,
        "display_name": display_name,
        "external_urls": norm_exts,
        "emails": sorted(emails),
        "website": website_url,
        "city": user_city,
        "country": user_country,
        "genre": user_genre,
        "user_genre": user_genre,
        "elapsed_ms": elapsed_ms,
        "aggregator_detected": int(aggregator_detected),
        "aggregator_expanded": int(aggregator_expanded),
        "aggregator_fetch_attempted": int(aggregator_fetch_attempted),
        "_aggregator_tried": int(aggregator_fetch_attempted),
        "bio_text": bio_text,
        "sounds_like": _sc_sounds_like_from_bio(bio_text),
        "latest_track_title": latest_title,
        "latest_track_release_date": latest_release,
        "latest_track_precision": latest_precision,
        "latest_track_genre": latest_genre,
        "latest_track_tags": latest_tags,
        "latest_track_source": latest_source or ("rss" if latest_title or latest_release else ""),
        "latest_track_permalink": latest_track_url,
        "latest_track_url": latest_track_url,
        "tracks_source": latest_source or ("rss" if latest_title or latest_release else ""),
        "challenge_page": challenge_page,
    }
    payload = _sc_postprocess_payload(payload)
    if payload["external_urls"] or payload["emails"]:
        SC_ABOUT_CACHE.set(handle, payload)
    if challenge_page:
        payload["status"] = "non_actionable_challenge"
        payload["reason"] = "challenge_page"
    elif _SC_ROOT_FORBIDDEN:
        payload["status"] = "blocked_403"
        payload["reason"] = "root_403"
    elif payload["external_urls"] or payload["emails"] or payload["website"]:
        payload["status"] = "actionable"
        payload["reason"] = "ok"
    else:
        payload["status"] = "no_contacts"
        payload["reason"] = "no_links"
    return payload


def _sc_fetch_contact_payload(handle: str) -> dict:
    session = _sc_thread_session()
    started = time.perf_counter()
    error = ""
    try:
        data = extract_sc_links(session, handle)
    except Exception as exc:
        error = str(exc)
        data = {"emails": [], "external_urls": [], "aggregator_expanded": 0, "status": "error", "reason": error}
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    emails_len = len(data.get("emails", []) or [])
    links_len = len(data.get("external_urls", []) or [])
    website = 1 if data.get("website") else 0
    elapsed = data.get("elapsed_ms", elapsed_ms)
    site_flag = int(data.get("aggregator_expanded", data.get("_aggregator_tried", 0)))
    tracks_source = data.get("latest_track_source") or "none"
    return {
        "data": data,
        "elapsed_ms": elapsed,
        "links": links_len,
        "emails": emails_len,
        "site": site_flag or website,
        "error": error,
        "status": data.get("status"),
        "reason": data.get("reason", error),
        "tracks_source": tracks_source,
    }


def _sc_handle_ok(h: str) -> bool:
    if not h or "/" in h or h.startswith("_"):
        return False
    h = h.strip().lower()
    if h in _RESERVED_SC:
        return False
    return bool(re.match(r"^[a-z0-9][a-z0-9\-_.]{2,}$", h))


def _sc_location_matches_filter(location_text: str, place_filter: str) -> bool:
    if not place_filter:
        return True
    if not location_text:
        return True
    return place_filter.lower() in (location_text or "").lower()


def sc_fetch_people_handles_v2(query: Optional[str], place: Optional[str], client_id: str, session, logger=None, max_results: int = 50) -> list:
    if logger is None:
        class _PrintLogger:
            def info(self, msg, *args, **kwargs):
                try:
                    print(msg % args if args else msg)
                except Exception:
                    print(msg)

            def warning(self, msg, *args, **kwargs):
                try:
                    print(msg % args if args else msg)
                except Exception:
                    print(msg)

            def error(self, msg, *args, **kwargs):
                try:
                    print(msg % args if args else msg)
                except Exception:
                    print(msg)

        logger = _PrintLogger()
    handles: list = []
    if not query:
        logger.warning("SoundCloud: v2 people search called without query; returning empty handle list")
        return handles
    base_url = SC_SEARCH_USERS_API
    offset = 0
    page_size = min(50, max_results if max_results > 0 else 50)
    params_base = {"q": query, "client_id": client_id, "limit": page_size, "linked_partitioning": 1}
    if place:
        params_base["filter.place"] = place
        params_base["facet"] = "place"
    logger.info(
        "SoundCloud: v2 people search API -> query='%s' place='%s' max_results=%d",
        query,
        place,
        max_results,
    )
    while True:
        params = dict(params_base)
        params["offset"] = offset
        try:
            resp = session.get(base_url, params=params, timeout=(6, 12), headers=_rand_headers())
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break
        collection = data.get("collection") or []
        for item in collection:
            if not isinstance(item, dict):
                continue
            handle = (item.get("permalink") or item.get("username") or "").strip()
            uid_raw = item.get("id") or item.get("urn") or ""
            if not _sc_handle_ok(handle):
                continue
            if uid_raw:
                _SC_HANDLE_UID_MAP[handle] = uid_raw
            _SC_HANDLE_USEROBJ_MAP[handle] = item
            if place:
                place_clean = place.strip().lower()
                city = (item.get("city") or "").strip()
                country = (item.get("country") or item.get("country_code") or item.get("country_name") or "").strip()
                location_text = " ".join(part for part in (city, country) if part).lower()
                if place_clean and place_clean not in location_text:
                    continue
            handles.append(handle)
            if max_results and len(handles) >= max_results:
                break
        if max_results and len(handles) >= max_results:
            break
        next_href = data.get("next_href")
        if not next_href:
            break
        try:
            parsed_next = urlparse(next_href)
            qs_next = parse_qs(parsed_next.query or "")
            offset = int(qs_next.get("offset", [offset + page_size])[0])
        except Exception:
            offset += page_size
        polite_sleep()
    deduped = []
    seen = set()
    for h in handles:
        if h in seen:
            continue
        seen.add(h)
        deduped.append(h)
    logger.info("SoundCloud: v2 people search API -> %d unique handles (query='%s' place='%s')", len(deduped), query, place)
    return deduped


def _people_search_candidates(query: str, place: str, max_results: int, session) -> List[Dict[str, str]]:
    client_id = _sc_get_client_id(session)
    raw_handles = sc_fetch_people_handles_v2(query=query, place=place, client_id=client_id, session=session, logger=None, max_results=max_results)
    candidates: List[Dict[str, str]] = []
    for h in raw_handles:
        obj = _SC_HANDLE_USEROBJ_MAP.get(h) or {}
        display = obj.get("full_name") or obj.get("username") or h
        location_text = " ".join(part for part in ((obj.get("city") or ""), (obj.get("country") or obj.get("country_code") or "")) if part)
        candidates.append({
            "handle": h,
            "profile_url": f"https://soundcloud.com/{h}",
            "display_name": display,
            "location": location_text,
            "context": obj.get("description") or "",
        })
    return candidates


def _score_candidate(artist: str, display: str, handle: str, location_hint: str = "", candidate_location: str = "") -> float:
    artist_norm = re.sub(r"\s+", " ", artist or "").strip().lower()
    display_norm = re.sub(r"\s+", " ", display or "").strip().lower()
    handle_norm = (handle or "").strip().lower()
    score = 0.0
    if artist_norm and display_norm:
        if artist_norm == display_norm:
            score += 1.0
        elif artist_norm in display_norm or display_norm in artist_norm:
            score += 0.7
    if artist_norm and handle_norm:
        if artist_norm == handle_norm:
            score += 0.8
        elif artist_norm.replace(" ", "") == handle_norm.replace("-", ""):
            score += 0.6
    if location_hint and candidate_location and location_hint.lower() in candidate_location.lower():
        score += 0.2
    return min(score, 1.2)


class SoundCloudEngine:
    def __init__(self, session: Optional[requests.Session] = None, debug: bool = False):
        self.session = session or build_hardened_session()
        self.debug = debug or NIGHT_SC_DEBUG
        self.reset_run_stats()

    # Stats helpers -----------------------------------------------------
    @property
    def run_stats(self) -> Dict[str, int]:
        return _SC_RUN_STATS or {}

    def get_run_flags(self) -> Dict[str, int]:
        """
        Read-only run-state snapshot for logging; does not mutate engine behaviour.
        """
        stats = self.run_stats or {}
        return {
            "root_fetch_disabled": int(bool(_SC_ROOT_FORBIDDEN)),
            "about_disabled": int(bool(_SC_ABOUT_DISABLED)),
            "tracks_api_blocked": int(
                bool(
                    stats.get("tracks_api_blocked", 0)
                    or stats.get("tracks_api_403", 0)
                    or stats.get("tracks_api_401", 0)
                )
            ),
            "used_user_api": int(bool(stats.get("api_user_fallback_used", 0))),
            "used_rss": int(bool(stats.get("rss_used", 0))),
        }

    def reset_run_stats(self):
        global _SC_RUN_STATS, _SC_ABOUT_DISABLED, _SC_ABOUT_DISABLE_LOGGED, _SC_ROOT_FORBIDDEN, _SC_ROOT_FORBIDDEN_LOGGED, _ENGINE_SESSION
        _SC_RUN_STATS = {
            "handles_total": 0,
            "actionable_written": 0,
            "about_attempts": 0,
            "about_challenges": 0,
            "about_disabled": 0,
            "root_403": 0,
            "tracks_api_blocked": 0,
            "tracks_api_403": 0,
            "tracks_api_401": 0,
            "api_user_fallback_used": 0,
            "rss_used": 0,
        }
        _SC_ABOUT_DISABLED = False
        _SC_ABOUT_DISABLE_LOGGED = False
        _SC_ROOT_FORBIDDEN = False
        _SC_ROOT_FORBIDDEN_LOGGED = False
        _ENGINE_SESSION = self.session

    def stat_inc(self, key: str, n: int = 1):
        _sc_stat_inc(key, n)

    # Public API --------------------------------------------------------
    def search_people_v2(
        self,
        query: str,
        place: Optional[str],
        max_results: int = 50,
        logger=None,
    ) -> List[str]:
        """
        Light wrapper around the v2 people search API used by the seed scraper.
        Exposed so live enrichment can share the same pathway.
        """
        if not query:
            return []
        client_id = _sc_get_client_id(self.session)
        return sc_fetch_people_handles_v2(
            query=query,
            place=place,
            client_id=client_id,
            session=self.session,
            logger=logger,
            max_results=max_results,
        )

    def people_search_candidates_v2(
        self, query: str, place: Optional[str], max_results: int = 50
    ) -> List[Dict[str, str]]:
        """Return scored candidate dicts for v2 people search."""
        return _people_search_candidates(query, place or "", max_results, self.session)

    def people_search(self, query: str, place: Optional[str], max_results: int) -> List[str]:
        if not query:
            return []
        client_id = _sc_get_client_id(self.session)
        return sc_fetch_people_handles_v2(query=query, place=place, client_id=client_id, session=self.session, logger=None, max_results=max_results)

    def fetch_profile(self, handle: str) -> dict:
        if not handle:
            return {"status": "no_handle", "reason": "missing_handle", "emails": [], "external_urls": []}
        result = _sc_fetch_contact_payload(handle)
        data = result.get("data") or {}
        data.setdefault("status", result.get("status") or ("actionable" if data.get("emails") or data.get("external_urls") or data.get("website") else "no_contacts"))
        data.setdefault("reason", result.get("reason") or "")
        data.setdefault("elapsed_ms", result.get("elapsed_ms", 0))
        data["fetches"] = 1
        return data

    def find_candidates(self, artist: str, location: Optional[str], max_results: int = 5) -> List[Dict[str, str]]:
        if not artist:
            return []
        candidates = _people_search_candidates(artist, location or "", max_results * 2, self.session)
        scored: List[Dict[str, str]] = []
        for cand in candidates:
            score = _score_candidate(artist, cand.get("display_name") or "", cand.get("handle") or "", location or "", cand.get("location") or "")
            cand["score"] = score
            cand["rank_score"] = score + (0.05 if location and (location.lower() in (cand.get("location") or "").lower()) else 0)
            scored.append(cand)
        scored.sort(key=lambda c: (c.get("score", 0), c.get("rank_score", 0)), reverse=True)
        return scored[:max_results]


__all__ = ["SoundCloudEngine", "sc_fetch_people_handles_v2", "extract_sc_links"]


if __name__ == "__main__":
    engine = SoundCloudEngine(debug=bool(os.getenv("NIGHT_SC_DEBUG")))
    sample = ["pesolife", "ellurr", "georgeriley-music"]
    for h in sample:
        print(f"[demo] fetching {h}")
        data = engine.fetch_profile(h)
        print(
            f"status={data.get('status')} emails={len(data.get('emails', []))} "
            f"links={len(data.get('external_urls', []))} site={bool(data.get('website'))} "
            f"tracks={data.get('tracks_source')}"
        )
        if h == "ellurr":
            print("ellurr emails sample:", [e for e in data.get("emails", []) if "ellur" in e])
        if h == "georgeriley-music":
            print("georgeriley-music website:", data.get("website"))
