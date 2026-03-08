#!/usr/bin/env python3
"""Spotify CSV post-processor that can reuse directory CSVs or perform live lookups."""

from __future__ import annotations

import datetime
import difflib
import importlib.util
import json
import math
import os
import re
import random
import threading
import time
import unicodedata
import urllib.parse
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from soundcloud_engine import SoundCloudEngine
import soundcloud_engine as sc_engine
import pandas as pd
import requests
from bs4 import BeautifulSoup
from source_scheduler import (
    SourceDiversityScheduler,
    SourceResult,
    SourceSpec,
    TimedRetry,
    canonicalize_facebook_url,
    ensure_canonical_facebook_url,
    promote_facebook_url,
)
from html_fetcher import fetch_html, _detect_soft_block
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from rapidfuzz import fuzz
from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, pyqtSignal
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, parse_qs, unquote
from unidecode import unidecode
from email_normalizer import normalize_email_value, normalize_obfuscated_email_patterns
from email_provenance import _set_email_with_provenance

from facebook_enrich import (
    FbCandidate,
    detect_corporate_token,
    extract_fb_category,
    classify_corporate_signals,
    has_corporate_token,
    is_noisy_fb_text_block,
    is_fb_creator_category,
    looks_like_music_fallback,
    clean_fb_category_text,
    MUSIC_CATEGORY_KEYWORDS,
    FB_MUSIC_CATEGORY_TOKENS,
    MUSIC_TOKENS,
    is_music_like_category,
    normalize_role_text,
    normalize_fb_name,
    score_fb_candidate,
    is_music_page,
    _corporate_hit,
    _looks_corporate,
    _looks_music_related,
    is_junk_fb_candidate_url,
    fb_reason_code_split,
    fb_is_allowed_profile_candidate_url,
    _fb_extract_candidates_from_search_dom,
)
from night_mode_fb import (
    classify_explicit_fb_intake,
    _extract_emails_from_html,
    _is_fb_login_or_security_url,
    _looks_like_fb_warning_or_block,
    _merge_email_all,
    _normalise_fb_url,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LIVE_SEARCH_MAX_ATTEMPTS = 50  # 0 = no limit
MAX_LINK_HUB_HOPS_PER_ROW = 1
HTTP_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/117.0.0.0 Safari/537.36"
)
MIN_BC_CONFIDENCE = 0.92
MIN_LF_CONFIDENCE = 0.9
STRICT_MATCHING = True
MATCH_THRESHOLD = 0.7
LIVE_LOOKUP_BCLF_BASE_SCORE = 1.0
LIVE_LOOKUP_BCLF_MIN_ATTEMPTS = 2
LIVE_LOOKUP_BCLF_MAX_BONUS = 0.35
LIVE_LOOKUP_BCLF_MAX_COOLDOWN_PENALTY = 0.45
FESTIVAL_EXPANSION_DISCOVERY_TIER = "festival_expansion"
FESTIVAL_EXPANSION_ORIGIN_BANDCAMP = "bandcamp"
FESTIVAL_EXPANSION_MAX_RELATED_ARTISTS = 3

# Last.fm live search resilience (T0X2)
LASTFM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.last.fm/",
}
LF_SEARCH_RETRY_MAX = 3
LF_BACKOFF_BASE = 0.7
LF_BACKOFF_MAX = 6.0
LF_PACING_DELAY_S = 0.25
LF_UNHEALTHY_PACING_MIN_S = 2.0
LF_UNHEALTHY_PACING_MAX_S = 4.0
LF_COOLDOWN_CONSEC_406 = 4
LF_COOLDOWN_MIN_S = 12.0
LF_COOLDOWN_STEP_S = 6.0
LF_COOLDOWN_MAX_S = 60.0


def _lf_sleep(unhealthy: bool = False) -> None:
    """Jittered pacing for Last.fm searches; longer when Last.fm seems unhappy."""
    if unhealthy:
        delay = LF_UNHEALTHY_PACING_MIN_S + random.uniform(0, LF_UNHEALTHY_PACING_MAX_S - LF_UNHEALTHY_PACING_MIN_S)
    else:
        delay = LF_PACING_DELAY_S + random.uniform(0, LF_PACING_DELAY_S)
    time.sleep(delay)

# Bandcamp live-search resilience (T0X1)
BC_SEARCH_RETRY_MAX = 3
BC_BACKOFF_BASE = 0.6
BC_BACKOFF_MAX = 4.0
BC_QUERY_GAP_MIN = 0.2
BC_QUERY_GAP_MAX = 0.6
BC_ENABLE_SEARCH_ENDPOINT = False
BC_BREAKER_CONSEC_403 = 5
BC_BREAKER_RATE_THRESHOLD = 0.8
BC_BREAKER_MIN_ATTEMPTS = 10
BC_FALLBACK_MAX_PER_RUN = 12
BC_FALLBACK_MAX_SLUGS = 4
BC_DISCOVER_MAX_FETCHES = 25

ENABLE_FACEBOOK_ENRICHMENT = True
FACEBOOK_SEARCH_URL = "https://www.facebook.com/search/pages/"
FACEBOOK_CATEGORY_KEYWORDS = ("musician", "band", "artist", "music")
FACEBOOK_SEARCH_WAIT_SECONDS = 10
ENABLE_LOCALE_BIAS = False
LOCALE_COUNTRY_HINT: Optional[str] = None  # Optional hint (e.g. "BR") to gently bias ties; never relaxes safety.

UNEARTHED_CSV = "unearthed_output.csv"
BANDCAMP_CSV = "bandcamp_output.csv"
SOUNDCLOUD_CSV = "soundcloud_output.csv"
LASTFM_CSV = "lastfm_output.csv"

DIRECTORY_FILES = {
    "bandcamp": BANDCAMP_CSV,
    "soundcloud": SOUNDCLOUD_CSV,
    "lastfm": LASTFM_CSV,
    "unearthed": UNEARTHED_CSV,
}

EMPTY_FIELD_MARKERS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "not available",
    "not_present",
    "not present",
    "notprovided",
    "tbd",
    "unknown",
    "unavailable",
    "-",
}

NOISE_HOSTS = {
    "cbsinteractive.com",
    "careers.paramount.com",
    "firefox.com",
    "apple.com",
    "google.com",
    "enable-javascript.com",
    "microsoft.com",
}

GENERIC_SOCIAL_ROOT_HOSTS = {
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    "bandcamp.com",
    "last.fm",
    "lastfm.com",
    "open.spotify.com",
    "spotify.com",
}

NOISE_PATH_KEYWORDS = (
    "/img/",
    "/image/",
    "/images/",
    "/static/",
    "/assets/",
    "/uploads/",
    "enable-javascript",
    "trust-center",
    "privacy",
)

NOISE_FILE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}

GENERIC_SOCIAL_HANDLES = {
    "instagram.com": {"last_fm", "last.fm"},
}

_SC_HEALTHCHECK_LOGGED = False
_T007_SC_HELPER = None
_T007_SC_HELPER_LOADED = False
_SC_CHALLENGE_TOKENS = (
    "enable cookies",
    "please enable cookies",
    "before we continue",
    "attention required",
    "captcha",
    "check your browser",
    "are you a human",
)
SC_RSS_ONLY_CONSEC_CHALLENGES = int(os.getenv("SC_RSS_ONLY_CONSEC_CHALLENGES", "2"))
SC_RSS_ONLY_CONSEC_403 = int(os.getenv("SC_RSS_ONLY_CONSEC_403", "3"))
SC_RSS_ONLY_COOLDOWN_SECONDS = int(os.getenv("SC_RSS_ONLY_COOLDOWN_SECONDS", "300"))
SC_RSS_ONLY_COOLDOWN_ROWS = int(os.getenv("SC_RSS_ONLY_COOLDOWN_ROWS", "15"))
SC_RSS_ONLY_SUCCESS_RESET = int(os.getenv("SC_RSS_ONLY_SUCCESS_RESET", "2"))
SC_CHALLENGE_ACTIVE_SECONDS = int(os.getenv("SC_CHALLENGE_ACTIVE_SECONDS", "300"))
SC_BREAKER_MIN_ROWS = int(os.getenv("SC_BREAKER_MIN_ROWS", "12"))
SC_RSS_FAIL_BREAKER_THRESHOLD = int(os.getenv("SC_RSS_FAIL_BREAKER_THRESHOLD", "4"))
SC_RSS_BREAKER_COOLDOWN_SECONDS = int(os.getenv("SC_RSS_BREAKER_COOLDOWN_SECONDS", "120"))
SC_COOLDOWN_ROW_RETRY_MAX = int(os.getenv("SC_COOLDOWN_ROW_RETRY_MAX", "2"))
SC_COOLDOWN_ROW_RETRY_JITTER_S = float(os.getenv("SC_COOLDOWN_ROW_RETRY_JITTER_S", "0.15"))
SC_ALLOW_FALLBACK_ON_TRACKS_401_403 = int(os.getenv("SC_ALLOW_FALLBACK_ON_TRACKS_401_403", "1") or "1")
NIGHT_SC_BUDGET_SECONDS_DEFAULT = 6
NIGHT_SC_MAX_FETCHES_DEFAULT = 3
_NIGHT_SC_PIPELINE_LOGGED = False
_SC_TRACKS_API_FALLBACK_LOGGED = False


def _sc_allow_fallback_on_tracks_api_block() -> bool:
    """
    Runtime check for allowing HTML/about fallback when the tracks API is blocked.
    Reads the env var each time so operators can toggle without reloads.
    """
    try:
        return int(os.getenv("SC_ALLOW_FALLBACK_ON_TRACKS_401_403", str(SC_ALLOW_FALLBACK_ON_TRACKS_401_403)) or "1") != 0
    except Exception:
        return True


def _sc_classify_rss_reason(reason: str) -> str:
    """
    Map a granular RSS failure reason to a coarse category used by the breaker.
    - blocked: signals rate-limit/403/challenge/engine instability
    - nofeed: profile has no RSS feed or feed is empty/private/missing
    - other: everything else (treated as non-blocking)
    """
    r = (reason or "").strip().lower()
    if not r:
        return "other"
    blocked_tokens = (
        "blocked",
        "api_403",
        "root_403",
        "tracks_api_blocked",
        "challenge",
        "captcha",
        "engine_unstable",
        "rate_limit",
        "429",
        "403",
        "timeout",
        "ssl",
        "connection",
    )
    nofeed_tokens = (
        "rss_unavailable",
        "rss_empty",
        "no_feed",
        "nofeed",
        "missing_handle",
        "no_uid",
        "private",
        "not_found",
        "404",
    )
    if any(tok in r for tok in blocked_tokens):
        return "blocked"
    if any(tok in r for tok in nofeed_tokens):
        return "nofeed"
    return "other"


@dataclass
class _NightSCAttempt:
    handle: str = ""
    profile_url: str = ""
    confidence: float = 0.0
    http_status: Optional[int] = None
    fetches: int = 0
    start_time: float = field(default_factory=time.time)
    reason: str = ""
    status: str = ""
    saw_403: bool = False
    challenge: bool = False
    budget_exceeded: bool = False
    cached_snapshot: Optional[Dict[str, Any]] = None
    cached_payload: Optional[Any] = None
    match_score: float = 0.0
    finalized: bool = False
    candidate_source: str = "none"
    profile_source: str = "none"

    max_seconds: float = NIGHT_SC_BUDGET_SECONDS_DEFAULT
    max_fetches: int = NIGHT_SC_MAX_FETCHES_DEFAULT

    def elapsed_ms(self) -> int:
        return int((time.time() - self.start_time) * 1000)

    def budget_ok(self) -> bool:
        if self.budget_exceeded:
            return False
        now = time.time()
        if self.max_seconds and (now - self.start_time) > self.max_seconds:
            self.budget_exceeded = True
            self.reason = self.reason or "budget_exceeded"
            return False
        if self.max_fetches is not None and self.fetches >= self.max_fetches:
            self.budget_exceeded = True
            self.reason = self.reason or "budget_exceeded"
            return False
        return True

    def note_fetch(self) -> bool:
        if not self.budget_ok():
            return False
        self.fetches += 1
        if self.max_fetches is not None and self.fetches > self.max_fetches:
            self.budget_exceeded = True
            self.reason = self.reason or "budget_exceeded"
            return False
        return True


def _night_sc_budget_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("NIGHT_SC_BUDGET_SECONDS", NIGHT_SC_BUDGET_SECONDS_DEFAULT)))
    except Exception:
        return float(NIGHT_SC_BUDGET_SECONDS_DEFAULT)


def _night_sc_max_fetches() -> int:
    try:
        return max(0, int(os.environ.get("NIGHT_SC_MAX_FETCHES", NIGHT_SC_MAX_FETCHES_DEFAULT)))
    except Exception:
        return NIGHT_SC_MAX_FETCHES_DEFAULT


def _night_sc_cache_keys(handle: str = "", profile_url: str = "") -> List[str]:
    keys: List[str] = []
    handle_key = (handle or "").strip().lower()
    if handle_key:
        keys.append(f"handle::{handle_key}")
    url_norm = _normalise_url(profile_url or "")
    if url_norm:
        parsed = urllib.parse.urlparse(url_norm)
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            url_norm = f"https://soundcloud.com/{parts[0]}"
        keys.append(f"url::{url_norm}")
    return keys


def _snapshot_payload(payload: Optional[Any]) -> Optional[Dict[str, Any]]:
    if payload is None:
        return None
    return {
        "socials": sorted(payload.socials),
        "websites": sorted(payload.websites),
        "emails": sorted(payload.emails),
        "link_hubs": sorted(payload.link_hubs),
        "source_dir": payload.source_dir,
        "source_url": payload.source_url,
        "source_detail": payload.source_detail,
        "match_score": getattr(payload, "match_score", 0.0),
        "candidate_name": getattr(payload, "candidate_name", ""),
    }


def _payload_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> Optional[Any]:
    if not snapshot:
        return None
    return EnrichmentPayload(
        socials=set(snapshot.get("socials") or []),
        websites=set(snapshot.get("websites") or []),
        emails=set(snapshot.get("emails") or []),
        link_hubs=set(snapshot.get("link_hubs") or []),
        source_dir=snapshot.get("source_dir") or "soundcloud",
        source_url=snapshot.get("source_url") or "",
        source_detail=snapshot.get("source_detail") or _format_source_display("soundcloud_live"),
        match_score=float(snapshot.get("match_score") or 0.0),
        candidate_name=snapshot.get("candidate_name") or "",
    )


def _sc_is_blocked(status_code, html) -> bool:
    if status_code == 403:
        return True
    src = (html or "").lower()
    return any(tok in src for tok in (
        "access denied",
        "error 403",
        "forbidden",
        "cloudflare",
        "attention required",
        "please enable cookies",
    ))


def _sc_is_challenge_page(html: str) -> bool:
    if not html:
        return False
    if len(html.strip()) < 200:
        return False
    lower = html.lower()
    return any(token in lower for token in _SC_CHALLENGE_TOKENS)


DIRECTORY_FIELD_MAP: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "bandcamp": {
        "primary_genre": (
            "Primary Genre",
            "primary_genre",
            "Genres",
            "Genre",
            "seed_genre",
        ),
        "release_date": (
            "Latest Release Date",
            "Release Date",
            "latest_release_date",
            "release_date",
        ),
        "location": (
            "Location",
            "location",
            "api_location",
        ),
        "email": (
            "Email",
            "Emails",
            "emails",
        ),
    },
    "soundcloud": {
        "primary_genre": (
            "Primary Genre",
            "primary_genre",
            "Genres",
        ),
        "release_date": (
            "Latest Release Date",
            "Release Date",
            "latest_release_date",
            "release_date",
        ),
        "location": (
            "Location",
            "location",
            "city",
            "country",
        ),
        "email": (
            "Email",
            "Emails",
            "emails",
        ),
    },
    "lastfm": {
        "primary_genre": (
            "Primary Genre",
            "primary_genre",
            "Genre",
            "Genres",
        ),
        "release_date": (
            "Release Date",
            "latest_release_date",
            "Latest Release Date",
        ),
        "location": (
            "Location",
            "location",
        ),
        "email": (
            "Email",
            "Emails",
            "emails",
        ),
    },
    "unearthed": {
        "primary_genre": (
            "Primary Genre",
            "Genre",
        ),
        "release_date": (
            "Release Date",
            "latest_release_date",
        ),
        "location": (
            "Location",
            "location",
        ),
        "email": (
            "Email",
            "Emails",
            "emails",
        ),
    },
}

LINK_HUB_HOSTS = {
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "lnk.bio",
    "bio.site",
    "flow.page",
    "solo.to",
    "withkoji.com",
    "carrd.co",
    "taplink.cc",
    "linkin.bio",
}

_EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SOCIAL_PRIORITY = [
    "facebook.com",
    "m.facebook.com",
    "fb.me",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "linktr.ee",
    "beacons.ai",
]

SOCIAL_HOST_WHITELIST = tuple(SOCIAL_PRIORITY)

SOCIAL_DOMAINS = {
    "facebook": ("facebook.com", "m.facebook.com"),
    "instagram": ("instagram.com", "www.instagram.com"),
    "x": ("x.com", "twitter.com", "mobile.twitter.com"),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "linktree": ("linktr.ee",),
    "beacons": ("beacons.ai",),
}

PLATFORM_HOSTS = {
    "bandcamp": (
        "bandcamp.com",
        "f4.bcbits.com",
        "get.bandcamp.help",
        "help.bandcamp.com",
        "bandcamp.help",
    ),
    "soundcloud": ("soundcloud.com",),
    "lastfm": ("last.fm", "lastfm.freetls.fastly.net"),
}

JUNK_WEBSITE_HOSTS = {
    "creativecommons.org",
    "get.bandcamp.help",
    "help.bandcamp.com",
    "bandcamp.help",
    "firefox.com",
    "apple.com",
    "google.com",
    "enable-javascript.com",
    "microsoft.com",
}

JUNK_WEBSITE_PATH_KEYWORDS = (
    "license",
    "licenses",
    "privacy",
    "terms",
    "cookie",
    "cookies",
    "help",
    "support",
    "faq",
    "press",
    "about-us",
    "contact-spotify-support",
)

PATH_NOISE = (
    "/help",
    "/support",
    "/legal",
    "/terms",
    "/privacy",
    "/about",
    "/contact",
    "/jobs",
)

LASTFM_BRAND_KEYWORDS = (
    "lastfm",
    "last.fm",
)

PROFILE_URL_CANDIDATES = (
    "Source URL",
    "Profile URL",
    "Bandcamp_URL",
    "Profile",
    "Artist URL",
    "Artist Link",
    "URL",
    "Bandcamp URL",
    "SoundCloud Link",
    "SoundCloud URL",
    "Last.fm URL",
    "LastFM URL",
    "Unearthed URL",
)

TRACK_NAME_COLUMNS = (
    "Song Title",
    "Song",
    "Track Name",
    "Track",
    "Track Title",
    "Latest Release",
    "Latest Release Title",
    "Latest Release Name",
    "Release Title",
)

SEED_TRACK_COLUMNS = (
    "Song Title",
    "Track Name",
    "Track",
)

TRACK_TOKEN_FIELD = "__track_tokens"

SEED_LINK_COLUMNS = (
    "Social Link",
    "External Links",
    "SoundCloud Link",
    "SoundCloud URL",
    "Bandcamp Link",
    "Bandcamp URL",
    "Bandcamp_URL",
    "Last.fm URL",
    "LastFM URL",
    "Source URL",
    "Profile URL",
)

DIRECTORY_SOCIAL_COLUMNS = (
    "Social Link",
    "Instagram",
    "Instagram URL",
    "Instagram_URL",
    "instagram_url",
    "Facebook",
    "Facebook URL",
    "Facebook_URL",
    "facebook_url",
    "Twitter",
    "Twitter URL",
    "Twitter_URL",
    "twitter_url",
    "X",
    "TikTok",
    "TikTok URL",
    "TikTok_URL",
    "Youtube",
    "YouTube",
    "YouTube URL",
    "YouTube_URL",
    "Threads",
    "LinkedIn",
)

INSTAGRAM_URL_CANDIDATES = (
    "Instagram",
    "Instagram URL",
    "Instagram_URL",
    "instagram_url",
    "Social Link",
    "External Links",
)

DIRECTORY_WEBSITE_COLUMNS = (
    "External Links",
    "Website",
    "Websites",
    "Linktree",
    "Link Tree",
    "Linktr.ee",
    "Bandcamp_URL",
    "Bandcamp Link",
    "SoundCloud Link",
    "SoundCloud URL",
    "Profile URL",
    "Profile",
    "URL",
    "url",
)

SOURCE_PRIORITY = {
    "bandcamp": 0,
    "soundcloud": 1,
    "lastfm": 2,
    "unearthed": 3,
    "bandcamp_live": 0,
    "soundcloud_live": 1,
    "lastfm_live": 2,
    "live_search": 5,
}

SOURCE_BASE_NAMES = {
    "bandcamp": "Bandcamp",
    "soundcloud": "SoundCloud",
    "lastfm": "Last.fm",
    "unearthed": "Triple J Unearthed",
    "live_search": "Live search",
}

MAX_WEBSITES = 2
WEBSITE_EMAIL_MAX_PAGES = max(1, int(os.getenv("WEBSITE_EMAIL_MAX_PAGES", "2") or 2))
WEBSITE_EMAIL_TIMEOUT = float(os.getenv("WEBSITE_EMAIL_TIMEOUT", "8") or 8)
WEBSITE_EMAIL_MAX_BYTES = max(1024, int(os.getenv("WEBSITE_EMAIL_MAX_BYTES", str(1_500_000)) or 1_500_000))
WEBSITE_EMAIL_KEYWORDS = (
    "contact",
    "about",
    "booking",
    "bookings",
    "press",
    "management",
    "manager",
    "mgmt",
)
WEBSITE_EMAIL_PATH_KEYWORDS = (
    "/contact",
    "/about",
    "/book",
    "/booking",
    "/press",
    "/management",
    "/team",
)
WEBSITE_EMAIL_SHALLOW_PATHS = (
    "/contact",
    "/press",
    "/media",
    "/epk",
    "/management",
    "/manager",
    "/booking",
    "/bookings",
    "/team",
    "/about",
)
WEBSITE_EMAIL_JUNK_KEYWORDS = ("login", "logout", "cart", "privacy", "terms")
WEBSITE_EMAIL_OPTIONAL_FIELDS = (
    "Website",
    "Websites",
    "Website URL",
)
HEAVY_ENRICHER_CONFIDENCE_THRESHOLD = 0.30
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MULTI_VALUE_SEPARATOR = ", "
FACEBOOK_HELPERS_PATH = os.path.join(BASE_DIR, "Lead Machine (Final Update 5).py")
ENRICHER_FB_PROFILE = os.path.join(os.path.expanduser("~"), "LeadMachine", "fb_enricher_profile")
_SC_SHARED_ENGINE = SoundCloudEngine(debug=bool(os.getenv("NIGHT_SC_DEBUG")))


def _night_sc_engine_enabled(night_mode: bool) -> bool:
    if not night_mode:
        return False
    mode = (os.getenv("NIGHTMODE_SC_ENGINE", "shared") or "shared").strip().lower()
    return mode not in {"legacy", "current", "off"}


def _sc_get_rss_used_total() -> int:
    try:
        stats_candidates = []
        try:
            stats_candidates.append(getattr(SoundCloudEngine, "_SC_RUN_STATS", None))
        except Exception:
            stats_candidates.append(None)
        try:
            stats_candidates.append(getattr(_SC_SHARED_ENGINE, "_SC_RUN_STATS", None))
        except Exception:
            stats_candidates.append(None)
        try:
            stats_candidates.append(getattr(_SC_SHARED_ENGINE, "run_stats", None))
        except Exception:
            stats_candidates.append(None)
        for stats in stats_candidates:
            try:
                if callable(stats):
                    stats = stats()
                if stats is not None:
                    return int((stats or {}).get("rss_used", 0) or 0)
            except Exception:
                continue
        # Last resort: derive from run flags if available.
        try:
            flags = _SC_SHARED_ENGINE.get_run_flags()
            return 1 if int(flags.get("used_rss", 0)) else 0
        except Exception:
            return 0
    except Exception:
        return 0


def _get_t007_sc_helper():
    """
    Load the T007 SoundCloud helper once to avoid repeated execution of the large helper module.
    Returns the cached helper (or None) on subsequent calls.
    """
    global _T007_SC_HELPER, _T007_SC_HELPER_LOADED
    if _T007_SC_HELPER_LOADED:
        return _T007_SC_HELPER
    _T007_SC_HELPER_LOADED = True
    try:
        spec = importlib.util.spec_from_file_location(
            "lead_machine_final_update_5_sc", os.path.join(BASE_DIR, "Lead Machine (Final Update 5).py")
        )
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _T007_SC_HELPER = getattr(module, "_sc_fetch_contact_payload", None)
    except Exception:
        _T007_SC_HELPER = None
    return _T007_SC_HELPER


_FB_DRIVER = None
_FB_DRIVER_LOCK = threading.Lock()
setup_facebook_driver = None
fb_scrape_emails_from_page = None
fb_find_page_and_emails_by_name = None
if os.path.exists(FACEBOOK_HELPERS_PATH):
    try:
        spec = importlib.util.spec_from_file_location(
            "lead_machine_final_update_5", FACEBOOK_HELPERS_PATH
        )
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setup_facebook_driver = getattr(module, "setup_facebook_driver", None)
            fb_scrape_emails_from_page = getattr(module, "fb_scrape_emails_from_page", None)
            fb_find_page_and_emails_by_name = getattr(module, "fb_find_page_and_emails_by_name", None)
    except Exception:
        setup_facebook_driver = None
        fb_scrape_emails_from_page = None
        fb_find_page_and_emails_by_name = None


def normalize_external_url(u: str) -> str:
    """
    Lightweight URL normalizer for Facebook enrichment to unwrap redirectors and strip noise.
    """
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
        if host.endswith("l.facebook.com") or host.endswith("lm.facebook.com"):
            qs = parse_qs(parsed.query or "")
            target = (qs.get("u") or qs.get("url") or [""])[0]
            if target:
                target = unquote(target)
                if target.startswith("//"):
                    target = "https:" + target
                u = target
        u = u.rstrip("/")
    except Exception:
        return u.rstrip("/")
    return u


def enricher_fb_profile_has_cookies() -> bool:
    return os.path.exists(os.path.join(ENRICHER_FB_PROFILE, "Default", "Cookies"))


def persistent_fb_driver():
    chrome_options = Options()
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.page_load_strategy = "eager"
    chrome_options.add_argument(f"--user-data-dir={ENRICHER_FB_PROFILE}")
    chrome_options.add_argument("--profile-directory=Default")
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    service_path = ChromeDriverManager().install()
    driver = webdriver.Chrome(service=ChromeService(service_path), options=chrome_options)
    try:
        driver.set_page_load_timeout(35)
    except Exception:
        pass
    return driver


def _get_enricher_facebook_driver():
    """
    Lazily initialize and return a shared Selenium Chrome driver for Facebook enrichment.
    Uses a persistent user-data-dir so login persists across runs.
    """
    global _FB_DRIVER
    with _FB_DRIVER_LOCK:
        if _FB_DRIVER is not None:
            return _FB_DRIVER
        os.makedirs(ENRICHER_FB_PROFILE, exist_ok=True)
        _FB_DRIVER = persistent_fb_driver()
        return _FB_DRIVER


def _cleanup_enricher_facebook_driver():
    """Safely close the Enricher's shared Facebook driver, if one was created."""
    global _FB_DRIVER
    with _FB_DRIVER_LOCK:
        if _FB_DRIVER is not None:
            try:
                _FB_DRIVER.quit()
            except Exception:
                pass
            _FB_DRIVER = None


def _is_music_related_facebook_candidate(
    artist_name: str,
    candidate_name: str,
    category_text: str = "",
    description_text: str = "",
) -> bool:
    import re

    # Normalise
    artist = artist_name.lower().strip()
    cand = candidate_name.lower().strip()
    cat = (category_text or "").lower()
    desc = (description_text or "").lower()
    combined = cat + " " + desc

    # Stopwords
    stop = set(["the", "and", "music", "band", "artist", "official"])

    tokens = [re.sub(r"[^a-z0-9]", "", t) for t in artist.split()]
    tokens = [t for t in tokens if t and t not in stop]

    # Must match at least one token
    if not any(t in cand for t in tokens):
        return False

    # Whitelist (MUST appear)
    whitelist = [
        "musician", "band", "artist", "singer", "songwriter",
        "rapper", "dj", "producer", "music", "record label",
        "recording studio"
    ]
    if not any(w in combined for w in whitelist):
        return False

    # Blacklist (MUST NOT appear)
    blacklist = [
        "church", "chapel", "ministries", "worship",
        "park", "city", "council", "tourism",
        "school", "college", "university",
        "restaurant", "cafe", "bar", "pub"
    ]
    if any(b in cand or b in combined for b in blacklist):
        return False

    return True


@dataclass
class EnrichmentPayload:
    socials: Set[str] = field(default_factory=set)
    websites: Set[str] = field(default_factory=set)
    emails: Set[str] = field(default_factory=set)
    link_hubs: Set[str] = field(default_factory=set)
    source_dir: Optional[str] = None
    source_url: Optional[str] = None
    source_detail: Optional[str] = None
    match_score: float = 0.0
    candidate_name: str = ""
    related_artists: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.candidate_name:
            parts.append(self.candidate_name)
        if self.source_detail:
            parts.append(self.source_detail)
        if self.source_url:
            parts.append(self.source_url)
        return " | ".join(part for part in parts if part) or (self.source_dir or "")


def _format_outcome_suffix(
    fetch_ok: Optional[bool] = None,
    actionable: Optional[bool] = None,
    http_status: Optional[int] = None,
) -> str:
    parts = []
    if fetch_ok is not None:
        parts.append(f"fetch_ok={fetch_ok}")
    if http_status is not None:
        parts.append(f"http={http_status}")
    if actionable is not None:
        parts.append(f"actionable={actionable}")
    return f" | {' '.join(parts)}" if parts else ""


def _payload_actionable(payload: Optional[EnrichmentPayload]) -> Optional[bool]:
    if payload is None:
        return None
    has_fields = bool(payload.socials or payload.websites or payload.emails or payload.link_hubs)
    return has_fields


def _festival_expansion_raw_path(output_csv_path: str) -> str:
    base, ext = os.path.splitext(output_csv_path)
    return f"{base}_festival_expansion_raw{ext or '.csv'}"


def _clean_bandcamp_related_artist_name(value: str) -> str:
    text = " ".join((value or "").replace("\xa0", " ").split()).strip()
    if not text:
        return ""
    text = re.sub(r"^(?:by|from)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", text)
    if len(text) > 80:
        return ""
    if text.lower() in {"view all", "more", "recommended by", "fans also like"}:
        return ""
    if re.search(r"https?://|bandcamp\.com", text, flags=re.IGNORECASE):
        return ""
    if not re.search(r"[A-Za-z]", text):
        return ""
    return text.strip(" -|,.;:")


def _add_bandcamp_related_name(
    results: List[str],
    seen: Set[str],
    candidate_text: str,
) -> None:
    cleaned = _clean_bandcamp_related_artist_name(candidate_text)
    if not cleaned:
        return
    key = normalise_artist_name(cleaned)
    if not key or key in seen:
        return
    seen.add(key)
    results.append(cleaned)


def _extract_bandcamp_related_artist_names(
    soup: Optional[BeautifulSoup],
    profile_url: str,
    limit: int = FESTIVAL_EXPANSION_MAX_RELATED_ARTISTS,
) -> List[str]:
    if soup is None or limit <= 0:
        return []

    results: List[str] = []
    seen: Set[str] = set()
    profile_host = urlparse(profile_url or "").netloc.lower().lstrip("www.")
    selector_candidates = (
        ".recommended-album .item-artist",
        ".recommended-grid-container .item-artist",
        ".recommended-grid-container .itemsubtext",
        ".related-artists .item-artist",
        ".fans-also-like .item-artist",
        ".recommended-by .item-artist",
    )

    for selector in selector_candidates:
        for node in soup.select(selector):
            text = node.get_text(" ", strip=True)
            _add_bandcamp_related_name(results, seen, text)
            if len(results) >= limit:
                return results[:limit]

    hint_tokens = ("recommended", "fans also like", "also like", "related artists")
    for container in soup.select("[class],[id]"):
        attrs = " ".join(
            [
                " ".join(container.get("class", [])),
                str(container.get("id") or ""),
            ]
        ).lower()
        if not any(token in attrs for token in ("recommended", "related", "also-like", "also_like", "fans")):
            continue
        text_preview = " ".join(container.stripped_strings).lower()
        if not any(token in text_preview for token in hint_tokens):
            continue
        for anchor in container.select("a[href]"):
            href = urljoin(profile_url, anchor.get("href") or "")
            host = urlparse(href).netloc.lower().lstrip("www.")
            if not host or not host.endswith("bandcamp.com"):
                continue
            if profile_host and host == profile_host:
                continue
            text = anchor.get_text(" ", strip=True) or anchor.get("title") or ""
            _add_bandcamp_related_name(results, seen, text)
            if len(results) >= limit:
                return results[:limit]

    return results[:limit]


@dataclass
class DirectoryIndex:
    source: str
    rows_by_artist: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    rows_by_track: Dict[Tuple[str, str], List[Dict[str, Any]]] = field(default_factory=dict)
    rows_by_profile_url: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def add_row(
        self,
        artist_key: str,
        row_dict: Dict[str, Any],
        track_tokens: Iterable[str],
    ) -> None:
        if not artist_key:
            return
        self.rows_by_artist.setdefault(artist_key, []).append(row_dict)
        tokens = tuple(token for token in track_tokens if token)
        if tokens:
            row_dict[TRACK_TOKEN_FIELD] = tokens
            for token in tokens:
                self.rows_by_track.setdefault((artist_key, token), []).append(row_dict)
        profile_url = _extract_profile_url(row_dict)
        if profile_url:
            self.rows_by_profile_url.setdefault(profile_url, []).append(row_dict)

    def lookup_artist(self, artist_key: str) -> List[Dict[str, Any]]:
        if not artist_key:
            return []
        return list(self.rows_by_artist.get(artist_key, []))

    def lookup_track(self, artist_key: str, track_key: str) -> List[Dict[str, Any]]:
        if not artist_key or not track_key:
            return []
        return list(self.rows_by_track.get((artist_key, track_key), []))

    def lookup_profile_url(self, url: str) -> List[Dict[str, Any]]:
        normalised = _normalise_url(url)
        if not normalised:
            return []
        return list(self.rows_by_profile_url.get(normalised, []))

    def unique_artist_count(self) -> int:
        return len(self.rows_by_artist)


@dataclass
class WebsiteFetchResult:
    url: str
    final_url: str
    status: Optional[int]
    content_type: str
    html: str
    is_html: bool


@dataclass(frozen=True)
class HeavyEnricherGateDecision:
    allowed: bool
    score: float
    threshold: float
    reasons: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def extract_domain(url: str) -> str:
    """
    Return a normalised domain string from a URL, e.g.
    'https://www.myband.com/about' -> 'myband.com'.
    If url is empty/invalid, return ''.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        url = url.split(",")[0].strip()
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def build_search_query(artist_name: str, song_title: str | None) -> str:
    artist_name = artist_name or ""
    song_title = song_title or ""
    artist_name = artist_name.strip()
    song_title = song_title.strip()
    if artist_name and song_title:
        return f'"{artist_name}" "{song_title}"'
    return artist_name


def _sanitize_lastfm_track_title(title: str) -> str:
    """Clean and shorten track titles to avoid Last.fm/WAF rejection.

    Steps:
    1) Trim and collapse whitespace.
    2) Split on common joiners (with surrounding whitespace) and keep first segment.
    3) Replace heavy/fancy punctuation with simple spaces; strip quotes/brackets.
    4) Hard cap to 60 chars; drop results that are too short or non-alphanumeric.
    """

    if not isinstance(title, str):
        return ""

    working = title.strip()
    if not working:
        return ""

    # Split on obvious multi-part separators before other replacements.
    split_pattern = r"\s*(?:\|\s*|/\s*|-\s+|—\s+|–\s+|:\s*|;\s*)"
    parts = re.split(split_pattern, working, maxsplit=1)
    working = parts[0]

    # Replace ellipsis variants and heavyweight separators with spaces.
    working = working.replace("…", " ")
    working = re.sub(r"\.\.\.+", " ", working)
    working = re.sub(r"[|/•—–:;]", " ", working)

    # Remove quotes and brackets that add noise but little value to search.
    working = re.sub(r"[\(\)\[\]{}<>\"']", "", working)

    # Collapse whitespace and cap length.
    working = " ".join(working.split())
    if len(working) > 60:
        working = working[:60]

    # Junk guard: skip numeric-heavy or very short fragments.
    alpha_ct = len(re.findall(r"[A-Za-z]", working))
    digit_ct = len(re.findall(r"[0-9]", working))
    if alpha_ct < 4:
        return ""
    if digit_ct >= alpha_ct and digit_ct >= 3:
        return ""

    # Guard against junk results (empty, too short, or no alphanumerics).
    if len(working) < 2 or not re.search(r"[A-Za-z0-9]", working):
        return ""

    return working


def build_bandcamp_queries(artist_name: str, track_title: Optional[str] = None) -> List[str]:
    artist = (artist_name or "").strip()
    track = (track_title or "").strip()

    queries: List[str] = []
    if track:
        queries.append(f'"{artist}" "{track}"')
        queries.append(f'{artist} "{track}"')
    queries.append(artist)

    seen = set()
    unique_queries: List[str] = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            unique_queries.append(q)
    return unique_queries


def normalize_text(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = unidecode(s).lower().strip()
    for bad in (" the ", "the ", " the"):
        s = s.replace(bad, " ")
    return " ".join(s.split())


def compute_match_score(
    seed_artist: str,
    seed_title: str,
    cand_artist: str,
    cand_title: str,
    spotify_domain: str,
    candidate_domain: str,
) -> float:
    """
    Returns a score between 0.0 and 1.0 indicating confidence that
    this candidate matches the seed Spotify artist/track.
    """
    seed_artist_n = normalize_text(seed_artist)
    seed_title_n = normalize_text(seed_title)
    cand_artist_n = normalize_text(cand_artist)
    cand_title_n = normalize_text(cand_title)

    artist_score = fuzz.ratio(seed_artist_n, cand_artist_n) if seed_artist_n and cand_artist_n else 0
    title_score = fuzz.ratio(seed_title_n, cand_title_n) if seed_title_n and cand_title_n else 0

    score = 0.0
    if artist_score >= 90:
        score += 0.5
    elif artist_score >= 80:
        score += 0.35
    elif artist_score >= 70:
        score += 0.2

    if title_score >= 90:
        score += 0.3
    elif title_score >= 80:
        score += 0.2
    elif title_score >= 70:
        score += 0.1

    spotify_domain = (spotify_domain or "").lower()
    candidate_domain = (candidate_domain or "").lower()
    if spotify_domain and candidate_domain:
        if spotify_domain == candidate_domain or candidate_domain.endswith("." + spotify_domain):
            score += 0.2

    return max(0.0, min(score, 1.0))


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
        }
    )
    return session


def _fb_status_is_rejected(status: str) -> bool:
    """Return True when FB_Status denotes a rejected/mismatched/blocked candidate."""
    status_norm = (status or "").lower()
    return any(tok in status_norm for tok in ("reject", "mismatch", "blocked"))


# ---------------------------------------------------------------------------
# Bandcamp-specific HTTP profile (polite, reusable)
# ---------------------------------------------------------------------------
_BC_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


def _build_bandcamp_session() -> requests.Session:
    session = requests.Session()
    ua = random.choice(_BC_UAS)
    session.headers.update(
        {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://bandcamp.com/",
            "Connection": "keep-alive",
        }
    )
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def cell_to_str(value) -> str:
    """
    Safely convert a CSV cell value into a clean string.
    Handles None, NaN, float, int, str, etc. without raising.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        try:
            if math.isnan(value):
                return ""
        except Exception:
            pass
    try:
        return str(value).strip()
    except Exception:
        try:
            return f"{value}".strip()
        except Exception:
            return ""


def _row_has_email(row) -> bool:
    """
    Return True when the row already has any email populated.
    Looks at Email and Email_All style fields and ignores Facebook URLs.
    """
    if row is None:
        return False

    def _get(key: str) -> str:
        try:
            value = row.get(key, "")
        except AttributeError:
            try:
                value = row[key]
            except Exception:
                value = ""
        return (str(value) or "").strip()

    email_primary = _get("Email")
    email_all = _get("Email_All") or _get("Email All")
    return bool(email_primary or email_all)


def _row_email_summary_snapshot(df: pd.DataFrame, row_idx) -> Dict[str, str]:
    """Capture the row email fields used by scheduler email accounting."""
    snapshot: Dict[str, str] = {}
    for col in ("Email", "Email_All"):
        if col in df.columns:
            snapshot[col] = cell_to_str(df.at[row_idx, col])
        else:
            snapshot[col] = ""
    return snapshot


def _canonicalize_fb_url(raw: str) -> str:
    """Normalize and validate a Facebook URL candidate."""
    if not raw:
        return ""
    cleaned = normalize_external_url(cell_to_str(raw))
    normalised = _normalise_fb_url(cleaned or raw)
    if normalised and "facebook.com" in normalised.lower():
        return normalised
    return ""


def _get_canonical_fb_url(row) -> str:
    """
    Return the preferred Facebook URL for a row.
    Preference order: facebook_url -> Facebook_URL -> Facebook URL.
    """
    if row is None:
        return ""

    def _get(key: str) -> str:
        try:
            value = row.get(key, "")
        except AttributeError:
            try:
                value = row[key]
            except Exception:
                value = ""
        return (str(value) or "").strip()

    for key in ("facebook_url", "Facebook_URL", "Facebook URL"):
        raw = _get(key)
        normalised = _canonicalize_fb_url(raw)
        if normalised:
            return normalised
    promoted = promote_facebook_url(row, set_row=False)
    return _canonicalize_fb_url(promoted)


_INSTAGRAM_ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
    "www.instagr.am",
}
_INSTAGRAM_REJECT_SEGMENTS = {
    "accounts",
    "explore",
    "p",
    "reel",
    "reels",
    "stories",
}
_INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _canonicalize_instagram_profile_url(raw: str) -> str:
    """Normalize an Instagram profile URL to the canonical profile root."""
    if not raw:
        return ""
    candidate = cell_to_str(raw)
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")
    try:
        parsed = urllib.parse.urlparse(candidate)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if host not in _INSTAGRAM_ALLOWED_HOSTS:
        return ""
    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    if len(segments) != 1:
        return ""
    handle = segments[0].strip()
    if not handle or handle.lower() in _INSTAGRAM_REJECT_SEGMENTS:
        return ""
    if not _INSTAGRAM_HANDLE_RE.fullmatch(handle):
        return ""
    return f"https://www.instagram.com/{handle}/"


def _get_canonical_instagram_url(row) -> str:
    """Return the first canonical Instagram profile URL found on the row."""
    if row is None:
        return ""
    for key in INSTAGRAM_URL_CANDIDATES:
        try:
            value = row.get(key, "")
        except AttributeError:
            try:
                value = row[key]
            except Exception:
                value = ""
        for token in _split_multi_value(value):
            normalised = _canonicalize_instagram_profile_url(token)
            if normalised:
                return normalised
    return ""


def _fetch_instagram_profile_html(session: requests.Session, url: str) -> Tuple[str, Optional[int]]:
    """Fetch a single Instagram profile page with the worker session and no retries."""
    if not session or not url:
        return ("", None)
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False)
    except Exception:
        return ("", None)
    status = getattr(resp, "status_code", None)
    if status != 200:
        return ("", status)
    return (getattr(resp, "text", "") or "", status)


def _row_has_facebook_or_email(row) -> bool:
    """
    Legacy helper retained for compatibility.
    Now only considers existing emails so rows with prefilled Facebook URLs
    are still eligible for FB email extraction.
    """
    return _row_has_email(row)


_FB_CONTACT_SURFACE_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
}

_FB_CONTACT_SURFACE_PRIORITIES = (
    ("about", ("sk=about", "about_profile_transparency", "/about", "about")),
    ("contact", ("contact_and_basic_info", "/contact", "contact")),
    ("info", ("/info", "info")),
)


def _normalise_fb_surface_url(url: str, base_url: str = "") -> str:
    """Normalize a Facebook profile/contact surface URL while preserving query strings."""
    raw = cell_to_str(url)
    if not raw:
        return ""

    lowered = raw.lower()
    if lowered.startswith("#") or lowered.startswith("javascript:") or lowered.startswith("mailto:") or lowered.startswith("tel:"):
        return ""

    resolved = normalize_external_url(raw) or raw
    try:
        if base_url:
            resolved = urllib.parse.urljoin(base_url, resolved)
        elif resolved.startswith("//"):
            resolved = "https:" + resolved
        elif resolved.startswith("/"):
            resolved = "https://www.facebook.com" + resolved
    except Exception:
        return ""

    try:
        parsed = urllib.parse.urlparse(resolved)
    except Exception:
        return ""

    scheme = (parsed.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        return ""

    host = (parsed.netloc or "").lower()
    if host not in _FB_CONTACT_SURFACE_HOSTS:
        return ""
    if host in {"facebook.com", "web.facebook.com", "m.facebook.com", "touch.facebook.com"}:
        host = "www.facebook.com"

    candidate = urllib.parse.urlunparse((scheme, host, parsed.path or "", "", parsed.query or "", ""))
    canonical = _normalise_fb_url(candidate)
    if not canonical:
        return ""

    if urllib.parse.urlparse(canonical).path.lower() == "/profile.php":
        return canonical

    canonical_parsed = urllib.parse.urlparse(canonical)
    return urllib.parse.urlunparse(
        (
            canonical_parsed.scheme or scheme,
            canonical_parsed.netloc or host,
            canonical_parsed.path,
            "",
            parsed.query or "",
            "",
        )
    )


def _select_fb_contact_surface_url(base_url: str, html: str) -> Optional[str]:
    """Return the best on-domain Facebook contact/about/info surface from page HTML."""
    if not base_url or not html:
        return None

    base_norm = _normalise_fb_surface_url(base_url)
    if not base_norm:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    candidates: list[tuple[int, int, int, str]] = []
    seen: set[str] = set()

    for index, anchor in enumerate(soup.find_all("a", href=True)):
        href = cell_to_str(anchor.get("href"))
        if not href:
            continue
        absolute = _normalise_fb_surface_url(href, base_url=base_norm)
        if not absolute or absolute == base_norm or absolute in seen:
            continue

        text = " ".join(anchor.get_text(" ", strip=True).lower().split())
        href_lower = href.lower()
        absolute_lower = absolute.lower()

        matched_rank = None
        matched_token_rank = None
        for priority_rank, (bucket, tokens) in enumerate(_FB_CONTACT_SURFACE_PRIORITIES):
            text_matches = bucket in text
            token_rank = next(
                (token_rank for token_rank, token in enumerate(tokens) if token in href_lower or token in absolute_lower),
                None,
            )
            if text_matches or token_rank is not None:
                matched_rank = priority_rank
                matched_token_rank = token_rank if token_rank is not None else len(tokens)
                break

        if matched_rank is None:
            continue

        seen.add(absolute)
        candidates.append((matched_rank, matched_token_rank or 0, index, absolute))

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][3]


def _extract_fb_emails_bounded(fb_driver, fb_url: str, log_fn=None) -> tuple[list[str], str, str]:
    """
    Visit at most two Facebook pages (main + about/info/contact) to extract emails.
    Returns (emails, resolved_url, status_reason).
    """
    emails: list[str] = []
    resolved_url = fb_url or ""
    last_reason = ""
    if not fb_driver or not fb_url:
        return (emails, resolved_url, "no_fb_url")

    budget = 2
    visited: set[str] = set()

    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    def _fetch(target: str) -> tuple[list[str], str, str]:
        nonlocal budget, last_reason, resolved_url
        if budget <= 0 or not target:
            return ([], resolved_url, "")
        target_fetch = _normalise_fb_surface_url(target) or _normalise_fb_url(normalize_external_url(target))
        if not target_fetch or target_fetch in visited:
            return ([], resolved_url, "")
        visited.add(target_fetch)
        budget -= 1
        try:
            fb_driver.get(target_fetch)
            current_url = getattr(fb_driver, "current_url", "") or target_fetch
            resolved_url = _normalise_fb_surface_url(current_url) or _normalise_fb_url(normalize_external_url(current_url) or current_url) or current_url
            if _is_fb_login_or_security_url(current_url):
                last_reason = "login_wall"
                _log("[FB Enrich] Facebook login/checkpoint detected; skipping.")
                return ([], resolved_url, "")
            html = getattr(fb_driver, "page_source", "") or ""
            warning = _looks_like_fb_warning_or_block(html, current_url)
            if warning:
                last_reason = warning
                _log(f"[FB Enrich] Warning/block page detected ({warning}); skipping row.")
                return ([], resolved_url, html)
            found, _ = _extract_emails_from_html(html)
            return (found, resolved_url, html)
        except Exception as exc:  # pragma: no cover - defensive
            last_reason = "fetch_error"
            _log(f"[FB Enrich] Error fetching FB page '{target_fetch}': {exc}")
            return ([], target_fetch, "")

    main_target = _normalise_fb_surface_url(fb_url) or _normalise_fb_url(normalize_external_url(fb_url))
    if main_target:
        _log(f"[FB Enrich] Visiting {main_target}")
    main_emails, resolved, main_html = _fetch(fb_url)
    if main_emails:
        return (main_emails, resolved, last_reason)

    selected_url = _select_fb_contact_surface_url(resolved or fb_url, main_html)
    if selected_url:
        _log(f"[FB Enrich] Visiting contact/about page: {selected_url}")
        more_emails, resolved, _ = _fetch(selected_url)
        if more_emails:
            return (more_emails, resolved, last_reason)
    else:
        _log("[FB Enrich] No contact/about link found")
        if budget > 0:
            parsed = urllib.parse.urlparse(resolved or fb_url)
            base_path = (parsed.path or "").rstrip("/") or "/"
            fallback_url = urllib.parse.urlunparse(
                parsed._replace(path=base_path + "/about", query="", fragment="")
            )
            _log(f"[FB Enrich] Visiting contact/about page: {fallback_url}")
            more_emails, resolved, _ = _fetch(fallback_url)
            if more_emails:
                return (more_emails, resolved, last_reason)

    return ([], resolved or fb_url, last_reason)


def _safe_log(logger, message: str, *args) -> None:
    if not logger:
        return
    try:
        if hasattr(logger, "info"):
            logger.info(message, *args)
            return
        if callable(logger):
            formatted = message % args if args else message
            logger(str(formatted))
    except Exception:
        try:
            print(message % args if args else message)
        except Exception:
            pass


def _append_link(existing: str, new: str) -> str:
    existing_str = cell_to_str(existing)
    new_str = cell_to_str(new)
    if not new_str:
        return existing_str
    parts = [part.strip() for part in existing_str.split(",") if part.strip()]
    if new_str not in parts:
        parts.append(new_str)
    return ", ".join(parts)


def _normalise_facebook_name(value: str) -> str:
    cleaned = cell_to_str(value).lower()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("official", "")
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned

_FB_ALLOWED_CATEGORY_TOKENS = ("musician", "band", "artist", "music", "singer", "dj", "producer", "songwriter")
_FB_BLOCKED_CATEGORY_TOKENS = (
    "church",
    "chapel",
    "ministries",
    "ministry",
    "worship",
    "park",
    "city",
    "council",
    "tourism",
    "school",
    "college",
    "university",
    "restaurant",
    "cafe",
    "bar",
    "pub",
    "spa",
    "resort",
    "hotel",
    "boutique",
    "store",
    "shop",
    "market",
    "mart",
)

def _facebook_score_candidate(artist_norm: str, page_name_norm: str, username_norm: str, category_norm: str) -> float:
    if not artist_norm:
        return 0.0
    category_norm = normalize_fb_name(category_norm)
    if has_corporate_token(page_name_norm) or has_corporate_token(username_norm) or has_corporate_token(category_norm):
        return 0.0
    score = 0.0
    if page_name_norm == artist_norm:
        score += 1.0
    elif page_name_norm.startswith(artist_norm):
        score += 0.7
    elif artist_norm in page_name_norm:
        score += 0.4

    if username_norm == artist_norm:
        score += 1.0
    elif username_norm.startswith(artist_norm):
        score += 0.7

    if category_norm:
        if any(bad in category_norm for bad in _FB_BLOCKED_CATEGORY_TOKENS):
            return 0.0
        if any(good in category_norm for good in _FB_ALLOWED_CATEGORY_TOKENS):
            score += 0.5
    return score



def _facebook_candidate_score(
    target_name: str, location: str, candidate_name: str, context_text: str
) -> float:
    target_norm = _normalise_facebook_name(cell_to_str(target_name))
    candidate_norm = _normalise_facebook_name(cell_to_str(candidate_name) or cell_to_str(context_text))
    if not candidate_norm or not target_norm:
        return 0.0
    score = 0.0
    if candidate_norm == target_norm:
        score += 60.0
    elif target_norm in candidate_norm or candidate_norm in target_norm:
        score += 40.0
    target_tokens = set(target_norm.split())
    candidate_tokens = set(candidate_norm.split())
    if target_tokens:
        overlap = len(target_tokens & candidate_tokens)
        score += overlap * 5.0
    context_norm = _normalise_facebook_name(context_text)
    if context_norm:
        score += len(target_tokens & set(context_norm.split())) * 2.0
    category_lower = cell_to_str(context_text).lower()
    if any(keyword in category_lower for keyword in FACEBOOK_CATEGORY_KEYWORDS):
        score += 8.0
    if location:
        loc_norm = _normalise_facebook_name(cell_to_str(location))
        if loc_norm and (loc_norm in candidate_norm or loc_norm in category_lower):
            score += 5.0
    return score


def _fb_scoring_sanity_tests():
    """
    Lightweight sanity checks for FB scoring without live requests.
    """
    def score(artist, name, category):
        final, _, _ = score_fb_candidate(artist, name, name, category or "")
        return final

    scenarios = [
        ("Aneya", "Aneya Music", "Musician/band", "Aneya Care Spa", "Health spa"),
        ("Ṣẹwà", "Ṣẹwà", "Musician/band", "Sewa Sandhu - Real Estate", "Real Estate Agent"),
        ("Salle", "Salle", "Musician", "Salle Sells Retro Vintage", "Clothing store"),
        ("The.wav", "The.wav", "Musician/band", "The Wave Resort", "Hotel/Resort"),
    ]
    for artist, good_name, good_cat, bad_name, bad_cat in scenarios:
        good_score = score(artist, good_name, good_cat)
        bad_score = score(artist, bad_name, bad_cat)
        assert good_score > bad_score, f"Expected music page to outrank business for {artist}"
    print("[FB Enrich] Sanity tests passed.")


@dataclass
class FacebookSearchClient:
    driver: Any
    logger: Any
    user_data_dir: Optional[str] = None
    cookies_path: Optional[str] = None
    _login_checked: bool = False

    def _is_logged_in(self) -> bool:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import TimeoutException
        except Exception:
            return False
        try:
            WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "a[aria-label*='profile'], a[aria-label*='account'], a[href*='profile.php']")
                )
            )
            return True
        except TimeoutException:
            return False
        except Exception:
            return False

    def ensure_facebook_logged_in(self) -> bool:
        """Ensure driver has an authenticated FB session."""
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import TimeoutException
        except Exception as exc:
            _safe_log(self.logger, "[FB Enrich] Selenium imports unavailable: %s", exc)
            return False
        try:
            self.driver.get("https://www.facebook.com/")
        except Exception as exc:
            _safe_log(self.logger, "[FB Enrich] Could not open facebook.com: %s", exc)
            return False

        if self._is_logged_in():
            return True

        # Try loading cookies if provided
        if self.cookies_path and os.path.exists(self.cookies_path):
            try:
                with open(self.cookies_path, "r", encoding="utf-8") as fh:
                    cookies = json.load(fh) or []
                for cookie in cookies:
                    try:
                        self.driver.add_cookie(cookie)
                    except Exception:
                        continue
                self.driver.get("https://www.facebook.com/")
                if self._is_logged_in():
                    return True
            except Exception as exc:
                _safe_log(self.logger, "[FB Enrich] Failed to load FB cookies: %s", exc)

        # One-time manual login prompt
        if not self._login_checked:
            self._login_checked = True
            _safe_log(
                self.logger,
                "[FB Enrich] Facebook not logged in. Please log in within 30s in the opened browser...",
            )
            try:
                self.driver.get("https://www.facebook.com/")
            except Exception as exc:
                _safe_log(self.logger, "[FB Enrich] Failed to open Facebook homepage: %s", exc)
                return False
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "a[aria-label*='profile'], a[aria-label*='account'], a[href*='profile.php']")
                    )
                )
                _safe_log(self.logger, "[FB Enrich] Facebook login detected; continuing enrichment.")
                return True
            except TimeoutException:
                _safe_log(self.logger, "[FB Enrich] Login window timed out.")
                return False
        return False

    def find_best_page_url(
        self,
        artist_name: str,
        location: Optional[str] = None,
        *,
        require_strong_candidate: bool = False,
    ) -> Optional[str]:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import TimeoutException, WebDriverException
        except Exception as exc:
            _safe_log(self.logger, "[FB Enrich] Selenium imports unavailable: %s", exc)
            return None
        if not self.ensure_facebook_logged_in():
            _safe_log(self.logger, "[FB Enrich] Facebook login not available, skipping.")
            return None
        query_parts = [cell_to_str(artist_name)]
        if location:
            query_parts.append(cell_to_str(location))
        query = " ".join(part for part in query_parts if part)
        if not query:
            return None
        search_url = f"{FACEBOOK_SEARCH_URL}?q={urllib.parse.quote_plus(query)}"
        _safe_log(self.logger, "[FB Enrich] Selenium FB search URL: %s", search_url)
        try:
            self.driver.get(search_url)
        except WebDriverException as exc:
            _safe_log(
                self.logger,
                "[FB Enrich] WebDriver error while navigating FB for '%s': %s",
                artist_name,
                exc,
            )
            return None
        try:
            WebDriverWait(self.driver, FACEBOOK_SEARCH_WAIT_SECONDS).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='facebook.com']"))
            )
        except TimeoutException:
            _safe_log(self.logger, "[FB Enrich] No FB results rendered for query '%s'", query)
            return None
        try:
            page_html = self.driver.page_source or ""
        except Exception:
            page_html = ""
        if not page_html:
            return None
        try:
            soup = BeautifulSoup(page_html, "html.parser")
        except Exception as exc:
            _safe_log(
                self.logger,
                "[FB Enrich] Failed to parse FB search DOM for '%s': %s",
                artist_name,
                exc,
            )
            return None
        candidates = _fb_extract_candidates_from_search_dom(
            page_html,
            logger=self.logger,
            debug=os.getenv("FB_DEBUG_DOM_GATE") == "1",
            search_name=artist_name,
        )
        dropped_business = 0
        gate_before = len(candidates)
        gate_reject_count = 0
        gate_reject_samples: List[str] = []
        gate_debug = os.getenv("FB_DEBUG_CAND_GATE") == "1"
        seen_urls: Set[str] = {
            _normalise_url((getattr(c, "url", "") or "").split("?", 1)[0])
            for c in candidates
            if getattr(c, "url", "")
        }

        if not candidates:
            if dropped_business:
                _safe_log(
                    self.logger,
                    "[FB Enrich] No non-junk FB candidates for '%s' after dropping %s junk business UI hits.",
                    artist_name,
                    dropped_business,
                )
            slug = normalize_fb_name(artist_name).replace(" ", "")
            if slug and len(slug) >= 4:
                fallback_url = f"https://www.facebook.com/{urllib.parse.quote(slug)}"
                fallback_key = _normalise_url(fallback_url)
                gate_before += 1
                if fallback_key not in seen_urls and fb_is_allowed_profile_candidate_url(fallback_url):
                    candidates.append(
                        FbCandidate(
                            name=artist_name,
                            url=fallback_url,
                            category="",
                        )
                    )
                    seen_urls.add(fallback_key)
                    _safe_log(
                        self.logger,
                        "[FB Enrich] No FB search candidates for '%s'; trying slug fallback '%s'.",
                        artist_name,
                        fallback_url,
                    )
                elif gate_debug and fallback_key not in seen_urls:
                    gate_reject_count += 1
                    if len(gate_reject_samples) < 5:
                        gate_reject_samples.append(fallback_url)
            if not candidates:
                _safe_log(
                    self.logger,
                    "[FB Enrich] No safe Facebook page candidates for '%s'",
                    artist_name,
                )
                return None

        if gate_debug:
            # Quick sanity: grep FB gate logs for /watch, /reel, /events/, notif_id to confirm junk candidates stop earlier.
            _safe_log(
                self.logger,
                "[FB Enrich][Gate] candidates before=%s after=%s rejected=%s",
                gate_before,
                len(candidates),
                gate_reject_count,
            )
            for sample in gate_reject_samples:
                _safe_log(self.logger, "[FB Enrich][Gate] rejected url=%r", sample)

        strong_music_candidates: List[Tuple[float, float, float, bool, bool, FbCandidate]] = []
        fallback_candidates: List[Tuple[float, float, float, bool, bool, FbCandidate]] = []
        generic_candidates: List[Tuple[float, float, float, bool, bool, FbCandidate]] = []
        corporate_tokens = [
            "ltd",
            "pty",
            "pty ltd",
            "inc",
            "corp",
            "company",
            "co.",
            "store",
            "shop",
            "shoppe",
            "boutique",
            "market",
            "resort",
            "hotel",
            "hostel",
            "motel",
            "lodge",
            "gallery",
            "galleria",
            "guest house",
            "guesthouse",
            "real estate",
            "realestate",
            "estate agent",
            "estateagency",
            "spa",
            "salon",
            "barber",
            "restaurant",
            "cafe",
            "coffee shop",
            "coffeehouse",
            "coffee",
            "bar",
            "pub",
            "farm",
            "farms",
            "beauty",
            "hair",
            "lash",
            "lashes",
            "makeup",
            "nails",
            "clinic",
            "brand",
            "journalist",
            "agency",
            "market",
            "grocer",
            "butcher",
            "bakery",
            "op shop",
            "thrift",
            "mart",
            "properties",
            "agency",
            "travel",
            "construction",
            "hospital",
            "club",
            "school",
            "college",
            "university",
            "academy",
            "church",
            "ministry",
            "ministries",
            "temple",
            "mosque",
            "foundation",
            "ngo",
            "association",
            "society",
            "pvt",
            "limited",
            "s.a.",
            "s.r.l",
        ]
        music_category_tokens = [
            "musician",
            "band",
            "artist",
            "recording artist",
            "music",
            "singer",
            "songwriter",
            "record label",
            "musical artist",
            "music production",
            "recording studio",
            "music producer",
            "producer",
        ]
        music_link_tokens = [
            "spotify.com",
            "open.spotify.com",
            "bandcamp.com",
            "soundcloud.com",
            "music.apple.com",
            "deezer.com",
            "tidal.com",
            "youtube.com",
            "youtu.be",
            "linktr.ee",
            "distrokid",
            "tunecore",
            "artist.to",
            "songwhip",
        ]
        music_text_tokens = [
            "single",
            "ep",
            "album",
            "track",
            "new song",
            "stream now",
            "listen now",
            "out now",
            "tour",
            "gig",
            "live show",
            "producer",
            "mixing",
            "mastering",
            "recording",
            "studio",
            "band",
            "musician",
            "songwriter",
        ]
        non_music_artist_tokens = [
            "makeup",
            "cosmetic",
            "hair",
            "nail",
            "lashes",
            "lash",
            "brow",
            "tattoo",
            "piercing",
            "barber",
            "beauty",
            "jewelry",
            "clinic",
            "dentist",
            "lawyer",
            "shop",
            "store",
        ]

        def _has_music_category(category: str | None) -> bool:
            return is_music_like_category(category or "", logger=self.logger, debug_logging_enabled=True)

        def _has_press_token(name: str, url: str, category: str | None) -> bool:
            text = f"{name} {url} {category or ''}".lower()
            return any(tok in text for tok in ("news", "magazine", "press", "blog", "journal", "media", "publisher"))

        def _has_music_signals(
            category: str | None,
            page_text: str,
            outbound_links: List[str],
            page_url: str,
            page_html: str | None = None,
        ) -> bool:
            combined_text = " ".join(part for part in (category or "", page_text or "") if part).lower()
            if "artist" in combined_text:
                for bad in non_music_artist_tokens:
                    if bad in combined_text:
                        break
                else:
                    if self.logger:
                        try:
                            if hasattr(self.logger, "debug"):
                                self.logger.debug(
                                    "[FB Enrich] Treating FB page '%s' as music based on 'artist' token.", page_url
                                )
                            else:
                                _safe_log(
                                    self.logger,
                                    "[FB Enrich] Treating FB page '%s' as music based on 'artist' token.",
                                    page_url,
                                )
                        except Exception:
                            pass
                    return True
            html_lc = (page_html or "").lower()
            if html_lc and "artist" in html_lc and not any(bad in html_lc for bad in non_music_artist_tokens):
                if self.logger:
                    try:
                        if hasattr(self.logger, "debug"):
                            self.logger.debug(
                                "[FB Enrich] Treating FB page '%s' as music based on 'artist' token (HTML).", page_url
                            )
                        else:
                            _safe_log(
                                self.logger,
                                "[FB Enrich] Treating FB page '%s' as music based on 'artist' token (HTML).",
                                page_url,
                            )
                    except Exception:
                        pass
                return True
            text = (page_text or "").lower()
            for link in outbound_links or []:
                l = (link or "").lower()
                if any(tok in l for tok in music_link_tokens):
                    return True
            return any(tok in text for tok in music_text_tokens)

        def _is_music_page_final(
            name: str, url: str, category: str | None, page_text: str, outbound_links: List[str], page_html: str | None
        ) -> bool:
            combined = f"{name} {url} {category or ''}".lower()
            if any(tok in combined for tok in corporate_tokens):
                return False
            if _has_press_token(name, url, category) and not _has_music_category(category):
                return False
            if not _has_music_category(category):
                return False
            if not _has_music_signals(category, page_text, outbound_links, url, page_html):
                return False
            return True
        for cand in candidates:
            name_lc = (cand.name or "").lower()
            url_lc = (cand.url or "").lower()
            category_lc = (cand.category or "").lower()
            cand_name_norm = normalize_fb_name(cand.name or "")
            artist_norm = normalize_fb_name(artist_name)
            try:
                cand_username_norm = normalize_fb_name(urllib.parse.urlparse(cand.url or "").path.strip("/").split("/")[0])
            except Exception:
                cand_username_norm = ""

            # First, reuse the shared helper for robust corporate detection.
            corp_hit_shared, corp_token_shared, corp_field_shared = _corporate_hit(name_lc, url_lc, category_lc)
            if corp_hit_shared:
                _safe_log(
                    self.logger,
                    "[FB Enrich] Rejecting FB candidate '%s' (%s) for '%s' due to corporate token '%s' in %s.",
                    cand.name or cand.url,
                    cand.url,
                    artist_name,
                    corp_token_shared or "<unknown>",
                    corp_field_shared or "name/url/category",
                )
                continue

            rejected = False
            for token in corporate_tokens:
                if token in name_lc:
                    _safe_log(
                        self.logger,
                        "[FB Enrich] Rejecting FB candidate '%s' (%s) for '%s' due to corporate token '%s' in name.",
                        cand.name or cand.url,
                        cand.url,
                        artist_name,
                        token,
                    )
                    rejected = True
                    break
                if token in url_lc:
                    _safe_log(
                        self.logger,
                        "[FB Enrich] Rejecting FB candidate '%s' (%s) for '%s' due to corporate token '%s' in url.",
                        cand.name or cand.url,
                        cand.url,
                        artist_name,
                        token,
                    )
                    rejected = True
                    break
                if token in category_lc:
                    _safe_log(
                        self.logger,
                        "[FB Enrich] Rejecting FB candidate '%s' (%s) for '%s' due to corporate token '%s' in category.",
                        cand.name or cand.url,
                        cand.url,
                        artist_name,
                        token,
                    )
                    rejected = True
                    break
            if rejected:
                continue

            scored = score_fb_candidate(artist_name, cand.name, cand.url, cand.category)
            if scored is None:
                continue
            final_score, name_score, cat_boost = scored
            contains_music_token = any(tok in name_lc or tok in url_lc or tok in category_lc for tok in MUSIC_TOKENS)
            strong_cat_tokens = ("musician", "band", "artist", "singer", "songwriter", "music", "recording artist")
            category_has_strong = any(tok in category_lc for tok in strong_cat_tokens)
            music_flag = category_has_strong or is_music_page(name_lc, url_lc, category_lc)
            if not music_flag:
                exact_match = (cand_name_norm and artist_norm and cand_name_norm == artist_norm) or (
                    cand_username_norm and artist_norm and cand_username_norm == artist_norm
                )
                if exact_match:
                    music_flag = True
            music_cat_bonus = 0.5 if any(tok in category_lc for tok in MUSIC_TOKENS) else 0.0
            final_score += music_cat_bonus
            _safe_log(
                self.logger,
                "-> '%s' (cat='%s', name_score=%.2f, cat_boost=%.2f, final=%.2f, music=%s, corporate=%s)",
                cand.name or cand.url,
                cand.category or "<none>",
                name_score,
                cat_boost + music_cat_bonus,
                final_score,
                music_flag,
                False,
            )
            if music_flag and category_has_strong:
                strong_music_candidates.append((final_score, name_score, cat_boost, music_flag, False, cand))
            elif music_flag or (
                not category_has_strong
                and "profile.php" not in url_lc
                and (
                    name_score >= 0.1
                    or (artist_norm and artist_norm.split() and artist_norm.split()[0] in (name_lc or ""))
                    or (artist_norm and artist_norm.split() and artist_norm.split()[0] in (url_lc or ""))
                )
            ):
                fallback_candidates.append((max(final_score, 1.0), name_score, cat_boost, True, False, cand))
            else:
                generic_candidates.append((max(final_score, 1.0), name_score, cat_boost, True, False, cand))

        best_entry: Optional[Tuple[float, float, float, bool, bool, FbCandidate]] = None
        using_fallback = False
        using_generic = False
        if strong_music_candidates:
            best_entry = max(strong_music_candidates, key=lambda item: item[0])
        elif fallback_candidates:
            best_entry = max(fallback_candidates, key=lambda item: item[0])
            using_fallback = True
        elif generic_candidates:
            best_entry = max(generic_candidates, key=lambda item: item[0])
            using_generic = True

        MIN_FINAL_SCORE = 1.0
        if not best_entry or best_entry[0] < MIN_FINAL_SCORE or not best_entry[3]:
            _safe_log(self.logger, "[FB Enrich] No high-confidence Facebook match for '%s'.", artist_name)
            return None

        if using_fallback and best_entry:
            _, base_score, _, _, _, cand = best_entry
            _safe_log(
                self.logger,
                "[FB Enrich] Trying uncertain music FB candidate '%s' for '%s' (category='%s', base_score=%.2f).",
                cand.name or cand.url,
                artist_name,
                cand.category or "<none>",
                base_score,
            )
        elif using_generic and best_entry:
            _, base_score, _, _, _, cand = best_entry
            _safe_log(
                self.logger,
                "[FB Enrich] Trying very loose FB candidate '%s' for '%s' (category='%s', base_score=%.2f).",
                cand.name or cand.url,
                artist_name,
                cand.category or "<none>",
                base_score,
            )

        best_score, best_name_score, best_cat_boost, best_is_music, best_is_corp, best_candidate = best_entry

        # Second-layer validation: fetch page category and reject late if corporate or not music.
        page_music = False
        confirmed_logged = False
        best_name_norm = normalize_fb_name(best_candidate.name or "")
        artist_norm = normalize_fb_name(artist_name)
        try:
            path_slug = urllib.parse.urlparse(best_candidate.url or "").path.strip("/").split("/")[0]
        except Exception:
            path_slug = ""
        best_username_norm = normalize_fb_name(path_slug)
        try:
            self.driver.get(best_candidate.url)
            WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            page_html = self.driver.page_source or ""
            page_category_text = None
            page_text_blocks: List[str] = []
            raw_html_lc = (page_html or "").lower()
            outbound_links: List[str] = []
            try:
                soup = BeautifulSoup(page_html, "html.parser")
                seen_blocks: Set[str] = set()

                def _add_block(val: str) -> Optional[str]:
                    val = (val or "").strip()
                    if not val or len(val) > 160:
                        return None
                    if is_noisy_fb_text_block(val):
                        return None
                    if val in seen_blocks:
                        return None
                    seen_blocks.add(val)
                    page_text_blocks.append(val)
                    return val

                meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
                    "meta", attrs={"property": "og:description"}
                )
                if meta_desc:
                    cleaned_meta = _add_block(meta_desc.get("content") or "")
                    if cleaned_meta and not page_category_text:
                        page_category_text = cleaned_meta
                # Scan visible spans/divs for music-y labels.
                MAX_FB_TEXT_BLOCKS = 80
                for tag in soup.find_all(["span", "div"]):
                    val = _add_block(tag.get_text(" ", strip=True))
                    if not val:
                        continue
                    low = val.lower()
                    if not page_category_text and ("/" in val or any(tok in low for tok in MUSIC_TOKENS)):
                        page_category_text = val
                    if len(page_text_blocks) >= MAX_FB_TEXT_BLOCKS:
                        break
                for tag in soup.find_all("a", href=True):
                    href_val = (tag.get("href") or "").strip()
                    if href_val.startswith("http"):
                        outbound_links.append(href_val)
                page_category_text = clean_fb_category_text(page_category_text) if page_category_text else None
            except Exception:
                page_category_text = None
            if not page_category_text and raw_html_lc and "artist" in raw_html_lc:
                if not any(bad in raw_html_lc for bad in non_music_artist_tokens):
                    page_category_text = "Artist"
            page_text_combined = " ".join(page_text_blocks)
            sig_page = classify_corporate_signals(
                best_candidate.url, best_candidate.name, page_category_text or "", page_text_combined
            )
            if sig_page.has_hard and not sig_page.has_artist:
                _safe_log(
                    self.logger,
                    "[FB Enrich] Rejecting FB page '%s' for '%s' after scrape due to HARD corporate category '%s'.",
                    best_candidate.url,
                    artist_name,
                    page_category_text or "<none>",
                )
                return None
            category_non_music = bool(page_category_text and not sig_page.has_artist)
            if page_category_text and sig_page.has_artist:
                _safe_log(
                    self.logger,
                    "[FB Enrich] Confirmed music page for '%s' with FB category '%s'.",
                    artist_name,
                    page_category_text,
                )
                confirmed_logged = True
            has_reliable_category = any(
                (
                    cat
                    and any(tok in normalize_role_text(cat) for tok in FB_MUSIC_CATEGORY_TOKENS)
                )
                for cat in (page_category_text, best_candidate.category)
            )
            page_music = _is_music_page_final(
                best_candidate.name or "",
                best_candidate.url or "",
                page_category_text or best_candidate.category,
                page_text_combined,
                outbound_links,
                page_html,
            )
            if not page_music:
                page_music = sig_page.has_artist or is_music_page(
                    best_candidate.name or "",
                    best_candidate.url or "",
                    page_category_text or "",
                )
            if not page_music and not has_reliable_category and not category_non_music:
                if looks_like_music_fallback(page_text_blocks, artist_name):
                    page_music = True
                    _safe_log(
                        self.logger,
                        "[FB Enrich] Falling back to text-based music detection for '%s' (no FB category; matched name+music tokens)",
                        artist_name,
                    )
                    if not confirmed_logged:
                        _safe_log(
                            self.logger,
                            "[FB Enrich] Confirmed music page for '%s' with FB category '%s'.",
                            artist_name,
                            page_category_text or "<none>",
                        )
                        confirmed_logged = True
            if not page_music:
                if category_non_music and page_category_text:
                    _safe_log(
                        self.logger,
                        "[FB Enrich] Rejecting FB page '%s' for '%s' after scrape: category '%s' not music-related.",
                        best_candidate.url,
                        artist_name,
                        page_category_text or "<none>",
                    )
                else:
                    _safe_log(
                        self.logger,
                        "[FB Enrich] Rejecting FB page '%s' for '%s' after scrape: no music signals found.",
                        best_candidate.url,
                        artist_name,
                    )
                return None
        except Exception as exc:
            _safe_log(
                self.logger,
                "[FB Enrich] Failed to parse FB page '%s' for '%s': %s",
                best_candidate.url,
                artist_name,
                exc,
            )

        # If we still have no music signal after page scrape, reject.
        if not page_music:
            _safe_log(
                self.logger,
                "[FB Enrich] Rejecting FB page '%s' for '%s' after scrape: no music signals found (final gate).",
                best_candidate.url,
                artist_name,
            )
            return None

        if require_strong_candidate:
            is_strong, strong_reason = _facebook_candidate_is_strong(
                artist_name,
                best_candidate,
                page_html,
                page_category_text,
                page_text_blocks,
                outbound_links,
                logger=self.logger,
            )
            if not is_strong:
                _safe_log(
                    self.logger,
                    "[FB Discover] Rejected candidate for '%s' - weak candidate: %s",
                    artist_name,
                    strong_reason,
                )
                return None

        _safe_log(
            self.logger,
            "[FB Enrich] Best FB candidate for '%s' -> '%s' (final_score=%.2f, name_score=%.2f, cat_boost=%.2f, music=%s, corporate=%s, category='%s')",
            artist_name,
            best_candidate.name or best_candidate.url,
            best_score,
            best_name_score,
            best_cat_boost,
            best_is_music,
            best_is_corp,
            best_candidate.category or "<none>",
        )
        return best_candidate.url


def _build_facebook_search_client(logger) -> Tuple[Optional["FacebookSearchClient"], Optional[Any]]:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
    except Exception as exc:
        _safe_log(logger, "[FB Enrich] Selenium not available; skipping FB enrichment: %s", exc)
        return None, None
    try:
        from webdriver_manager.chrome import ChromeDriverManager
    except Exception:
        ChromeDriverManager = None
    try:
        user_data_dir = os.environ.get("FACEBOOK_USER_DATA_DIR") or ""
        cookies_path = os.path.join(BASE_DIR, "fb_cookies.json")
        options = webdriver.ChromeOptions()
        # Facebook enrichment should use a visible browser for manual login.
        # Remove any inherited headless flags defensively.
        for arg in list(options.arguments):
            if "headless" in arg:
                try:
                    options.arguments.remove(arg)
                except Exception:
                    pass
        options.add_experimental_option("detach", True)
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if user_data_dir:
            options.add_argument(f"--user-data-dir={user_data_dir}")
            options.add_argument("--profile-directory=Default")
        service = None
        if ChromeDriverManager:
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)
        if cookies_path and os.path.exists(cookies_path):
            try:
                driver.get("https://www.facebook.com/")
                with open(cookies_path, "r", encoding="utf-8") as fh:
                    cookies = json.load(fh) or []
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        continue
            except Exception as exc:
                _safe_log(logger, "[FB Enrich] Failed to preload FB cookies: %s", exc)
        client = FacebookSearchClient(
            driver=driver,
            logger=logger,
            user_data_dir=user_data_dir or None,
            cookies_path=cookies_path if os.path.exists(cookies_path) else None,
        )
        return client, driver
    except Exception as exc:
        _safe_log(logger, "[FB Enrich] Failed to start Selenium driver: %s", exc)
        return None, None


def _facebook_candidate_has_personal_profile_phrase(
    artist_name: str,
    candidate_name: str,
    page_html: str,
    page_text_blocks: List[str],
) -> bool:
    raw_blob = " ".join(part for part in [page_html, " ".join(page_text_blocks or [])] if part)
    raw_blob = " ".join(raw_blob.split()).lower()
    normalized_blob = normalize_fb_name(raw_blob)
    names_to_check = []
    for raw_name in (artist_name, candidate_name):
        name = cell_to_str(raw_name)
        if not name:
            continue
        names_to_check.append(name)
    for name in names_to_check:
        name_phrase = " ".join(name.split()).lower()
        if name_phrase and f"{name_phrase} is on facebook" in raw_blob:
            return True
        name_norm = normalize_fb_name(name)
        if name_norm and f"{name_norm} is on facebook" in normalized_blob:
            return True
    return False


def _facebook_candidate_is_strong(
    artist_name: str,
    candidate: FbCandidate,
    page_html: str,
    page_category_text,
    page_text_blocks,
    outbound_links,
    logger=None,
) -> Tuple[bool, str]:
    candidate_name = cell_to_str(getattr(candidate, "name", ""))
    candidate_url = cell_to_str(getattr(candidate, "url", ""))
    page_html = page_html or ""
    page_text_blocks = list(page_text_blocks or [])
    outbound_links = list(outbound_links or [])

    if _facebook_candidate_has_personal_profile_phrase(
        artist_name,
        candidate_name,
        page_html,
        page_text_blocks,
    ):
        return False, "personal_profile_phrase"

    category_values: List[str] = []
    for raw_value in (
        page_category_text,
        getattr(candidate, "category", ""),
        getattr(candidate, "descriptor", ""),
        getattr(candidate, "secondary_text", ""),
    ):
        value = clean_fb_category_text(cell_to_str(raw_value))
        if value:
            category_values.append(value)
    for token in getattr(candidate, "category_tokens", []) or []:
        value = clean_fb_category_text(cell_to_str(token))
        if value:
            category_values.append(value)

    for category_value in category_values:
        if is_music_like_category(category_value, logger=logger, debug_logging_enabled=True):
            return True, "music_category"

    strong_music_link_tokens = (
        "spotify.com",
        "open.spotify.com",
        "bandcamp.com",
        "soundcloud.com",
        "music.apple.com",
    )
    for link in outbound_links:
        link_l = cell_to_str(link).lower()
        if any(token in link_l for token in strong_music_link_tokens):
            return True, "music_platform_link"

    if candidate_url and page_html:
        contact_surface = _select_fb_contact_surface_url(candidate_url, page_html)
        if contact_surface:
            return True, "page_structure_surface"

    structure_blob = " ".join(page_text_blocks).lower()
    if any(token in structure_blob for token in ("official page", "contact info", "contact us", "email us")):
        return True, "page_structure_text"

    artist_norm = normalize_fb_name(artist_name).replace(" ", "")
    if artist_norm and len(artist_norm) < 4:
        return False, "short_name_without_strong_signal"

    return False, "slug_or_name_only_match"


def facebook_find_best_page(
    artist_name: str,
    location: str,
    fb_client,
    logger,
    *,
    require_strong_candidate: bool = False,
) -> Optional[str]:
    artist_name = cell_to_str(artist_name)
    location = cell_to_str(location)
    if not fb_client or not hasattr(fb_client, "find_best_page_url"):
        _safe_log(logger, "[FB Enrich] No Facebook search client available; skipping '%s'.", artist_name)
        return None
    try:
        return fb_client.find_best_page_url(
            artist_name,
            location,
            require_strong_candidate=require_strong_candidate,
        )
    except Exception as exc:
        _safe_log(logger, "[FB Enrich] Facebook search client error for '%s': %s", artist_name, exc)
        return None


def _discover_facebook_url_bounded(fb_driver, artist_name: str, location: str, logger) -> str:
    """Attempt one bounded daytime Facebook discovery using the existing driver."""
    if not fb_driver:
        return ""
    artist_name = cell_to_str(artist_name)
    location = cell_to_str(location)
    if not artist_name:
        return ""
    try:
        fb_client = FacebookSearchClient(driver=fb_driver, logger=logger)
    except Exception:
        return ""
    fb_url = facebook_find_best_page(
        artist_name,
        location,
        fb_client,
        logger,
        require_strong_candidate=True,
    )
    canonical_fb_url = canonicalize_facebook_url(fb_url)
    if not canonical_fb_url:
        return ""
    canonical_fb_url = _canonicalize_fb_url(canonical_fb_url)
    if not canonical_fb_url:
        return ""
    if not fb_is_allowed_profile_candidate_url(canonical_fb_url):
        return ""
    return canonical_fb_url


def enrich_row_with_facebook(*args, **kwargs) -> None:
    raise RuntimeError(
        "enrich_row_with_facebook() is deprecated. "
        "Use _enrich_row_facebook instead."
    )


def _format_source_display(source_key: Optional[str]) -> str:
    if not source_key:
        return ""
    key = source_key.strip().lower()
    live_suffix = False
    if key.endswith("_live"):
        live_suffix = True
        key = key[:-5]
    base_name = SOURCE_BASE_NAMES.get(key, key.title())
    if live_suffix or key == "live_search":
        return f"{base_name} (live search)"
    return base_name


def _canonical_source_key(label: Optional[str]) -> str:
    if not label:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "", label.strip().lower())
    if not normalized:
        return ""
    priority_keys = list(SOURCE_PRIORITY.keys())
    live_keys = [k for k in priority_keys if k.endswith("_live") or k == "live_search"]
    base_keys = [k for k in priority_keys if k not in live_keys]
    for key in live_keys + base_keys:
        key_norm = re.sub(r"[^a-z0-9]+", "", key.lower())
        if key_norm and key_norm in normalized:
            return key
    return normalized


def _read_csv_flexible(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    last_exc = None
    for kwargs in ({"sep": None, "engine": "python"}, {}):
        try:
            df = pd.read_csv(path, **kwargs)
            return _sanitize_dataframe(df)
        except Exception as exc:
            last_exc = exc
    print(f"[Enricher] Failed to read CSV {path}: {last_exc}")
    return None


def _ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return df
    try:
        new_columns = []
        updated = False
        for col in df.columns:
            if isinstance(col, str):
                sanitized = col.lstrip("\ufeff")
                new_columns.append(sanitized)
                if sanitized != col:
                    updated = True
            else:
                new_columns.append(col)
        if updated:
            df.columns = new_columns
    except Exception:
        pass
    return df


def _split_multi_value(value, delimiter: Optional[str] = None) -> Iterable[str]:
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return []
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if delimiter:
        parts = text.split(delimiter)
    else:
        parts = re.split(r"[|;,\n]", text)
    return [part.strip() for part in parts if part.strip()]


def _is_noise_url(raw_url: str) -> bool:
    if not raw_url:
        return True
    url = raw_url.strip()
    if not url:
        return True
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return True
    host = (parsed.netloc or "").lower()
    if not host:
        return True
    if host.startswith("www."):
        host = host[4:]
    host = host.split(":", 1)[0]
    path = (parsed.path or "").lower()
    if host in NOISE_HOSTS:
        return True
    if host in GENERIC_SOCIAL_ROOT_HOSTS and not path.strip("/"):
        return True
    for keyword in NOISE_PATH_KEYWORDS:
        if keyword in path:
            return True
    _, ext = os.path.splitext(path)
    if ext and ext in NOISE_FILE_EXTENSIONS:
        return True
    segments = [segment for segment in path.split("/") if segment]
    if segments:
        first_segment = segments[0].lower()
        handles = GENERIC_SOCIAL_HANDLES.get(host) or GENERIC_SOCIAL_HANDLES.get("www." + host, set())
        if handles and first_segment in handles:
            return True
    return False


def _normalise_url(value: str) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "")
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunparse((parsed.scheme, netloc, path, "", parsed.query, ""))


def _canonicalise_bandcamp_url(value: str) -> str:
    """
    Bandcamp-only canonicalizer: keep scheme+host+path, drop query/fragment, force https.
    Non-Bandcamp URLs are returned unchanged.
    """
    if not value:
        return ""
    raw_value = value.strip()
    if not raw_value:
        return ""
    value = raw_value
    # Ensure we have a scheme so urlsplit works consistently.
    if value.startswith("//"):
        value = "https:" + value
    if "://" not in value:
        value = f"https://{value.lstrip('/')}"
    try:
        parsed = urllib.parse.urlsplit(value)
    except Exception:
        return raw_value

    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Only canonicalise Bandcamp hosts; otherwise, return the original value untouched.
    if not netloc.endswith("bandcamp.com"):
        return raw_value

    path = re.sub(r"/+", "/", parsed.path or "")
    if path == "/":
        path = ""

    # Force https and strip query/fragment.
    return urllib.parse.urlunsplit(("https", netloc, path, "", ""))


def _split_pipe_cell(value, is_email: bool = False) -> Set[str]:
    if value is None:
        return set()
    if isinstance(value, float) and math.isnan(value):
        return set()
    if not isinstance(value, str):
        value = str(value)
    values = set()
    normalized = value.replace("|", "\n").replace(", ", "\n")
    for part in normalized.split("\n"):
        part = part.strip()
        if not part:
            continue
        if is_email:
            values.add(part.lower())
        else:
            normalised = _normalise_url(part)
            if normalised and not _is_noise_url(normalised):
                values.add(normalised)
    return values


def _social_sort_key(url: str) -> Tuple[int, str]:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        host = ""
    for idx, domain in enumerate(SOCIAL_PRIORITY):
        if host.endswith(domain):
            return (idx, url)
    return (len(SOCIAL_PRIORITY), url)


def _website_sort_key(url: str) -> Tuple[int, str]:
    host = _host(url)
    is_hub = 0 if host in LINK_HUB_HOSTS else 1
    return (is_hub, url)


def _prioritise_facebook_first(urls: Iterable[str]) -> List[str]:
    facebook: List[str] = []
    non_facebook: List[str] = []
    for url in urls:
        lowered = url.lower()
        if "facebook.com" in lowered or "fb.me" in lowered:
            facebook.append(url)
        else:
            non_facebook.append(url)
    return facebook + non_facebook


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _source_for_url(url: str) -> Optional[str]:
    host = _host(url)
    if not host:
        return None
    for source, domains in PLATFORM_HOSTS.items():
        if any(host.endswith(domain) for domain in domains):
            return source
    return None


def _extract_profile_url(row: dict) -> Optional[str]:
    for column in PROFILE_URL_CANDIDATES:
        value = row.get(column)
        if not isinstance(value, str):
            continue
        normalised = _normalise_url(value)
        if normalised:
            return normalised
    return None


def _extract_directory_track_title(row: dict) -> str:
    for column in TRACK_NAME_COLUMNS:
        value = row.get(column)
        cleaned = _clean_cell(value)
        if cleaned:
            return cleaned
    return ""


def _extract_directory_fields(
    row: dict,
    source: Optional[str] = None,
) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    socials: Set[str] = set()
    websites: Set[str] = set()
    emails: Set[str] = set()
    link_hubs: Set[str] = set()
    for column in DIRECTORY_SOCIAL_COLUMNS:
        value = row.get(column)
        for item in _split_multi_value(value):
            normalised = _normalise_url(item)
            if not normalised or _is_noise_url(normalised):
                continue
            host = _host(normalised)
            if host in LINK_HUB_HOSTS:
                link_hubs.add(normalised)
                websites.add(normalised)
            elif any(host.endswith(dom) for dom in SOCIAL_HOST_WHITELIST):
                socials.add(normalised)
            else:
                websites.add(normalised)
    for column in DIRECTORY_WEBSITE_COLUMNS:
        value = row.get(column)
        for item in _split_multi_value(value):
            normalised = _normalise_url(item)
            if not normalised or _is_noise_url(normalised):
                continue
            host = _host(normalised)
            if host in LINK_HUB_HOSTS:
                link_hubs.add(normalised)
                websites.add(normalised)
            elif any(host.endswith(dom) for dom in SOCIAL_HOST_WHITELIST):
                socials.add(normalised)
            else:
                websites.add(normalised)
    email_values: List[Any] = []
    if source and source in DIRECTORY_FIELD_MAP:
        email_columns = DIRECTORY_FIELD_MAP[source].get("email", ())
        for column in email_columns:
            email_values.append(row.get(column))
    else:
        email_values.extend(
            [row.get("Email"), row.get("Emails"), row.get("email"), row.get("emails")]
        )
    for email_value in email_values:
        for email in _split_multi_value(email_value):
            cleaned = email.strip().lower()
            if cleaned:
                emails.add(cleaned)
    return socials, websites, emails, link_hubs


def _website_domains_match(left_url: str, right_url: str) -> bool:
    left_domain = extract_domain(left_url)
    right_domain = extract_domain(right_url)
    return bool(left_domain and right_domain and left_domain == right_domain)


def _mailto_emails_from_soup(soup: Optional[BeautifulSoup]) -> Tuple[List[str], bool]:
    if soup is None:
        return ([], False)
    emails: List[str] = []
    used_mailto = False
    for anchor in soup.select('a[href^="mailto:"]'):
        href = cell_to_str(anchor.get("href"))
        if not href:
            continue
        addr = href.split("mailto:", 1)[-1].split("?", 1)[0].strip()
        cleaned = normalize_email_value(addr)
        if cleaned and cleaned not in emails:
            emails.append(cleaned)
            used_mailto = True
    return (emails, used_mailto)


def _extract_website_emails_from_html(html: str) -> Tuple[List[str], bool]:
    if not html:
        return ([], False)
    try:
        emails, used_mailto = _extract_emails_from_html(html)
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            soup = None
        emails, used_mailto = _mailto_emails_from_soup(soup)
    return (emails, used_mailto)


def _discover_website_contact_candidates(base_url: str, html: str) -> List[str]:
    if not base_url or not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    candidates: List[Tuple[int, int, str]] = []
    seen: Set[str] = set()
    base_norm = _normalise_url(base_url) or base_url

    for anchor in soup.find_all("a", href=True):
        href_raw = cell_to_str(anchor.get("href"))
        if not href_raw:
            continue
        href_lower = href_raw.lower()
        if href_lower.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = _normalise_url(urllib.parse.urljoin(base_norm, href_raw))
        if not absolute or absolute == base_norm:
            continue
        if not _website_domains_match(base_norm, absolute):
            continue
        parsed = urllib.parse.urlparse(absolute)
        query_lower = (parsed.query or "").lower()
        if len(query_lower) > 80 or "utm_" in query_lower or "fbclid=" in query_lower or "gclid=" in query_lower:
            continue

        text_lower = " ".join(anchor.get_text(" ", strip=True).lower().split())
        path_lower = parsed.path.lower()
        score = 0
        strong_match = False
        for keyword in WEBSITE_EMAIL_KEYWORDS:
            if keyword in text_lower:
                score += 6
                strong_match = True
            if keyword in href_lower:
                score += 5
                strong_match = True
        for keyword in WEBSITE_EMAIL_PATH_KEYWORDS:
            if keyword in path_lower or keyword in href_lower:
                score += 7
                strong_match = True
        if not strong_match:
            continue
        junk_hit = any(keyword in text_lower or keyword in path_lower for keyword in WEBSITE_EMAIL_JUNK_KEYWORDS)
        if junk_hit and score < 10:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        path_length = len(parsed.path or "/")
        candidates.append((-score, path_length, absolute))

    candidates.sort()
    return [url for _, _, url in candidates]


def _build_website_shallow_candidates(base_url: str, current_url: str = "") -> List[str]:
    base_norm = _normalise_url(base_url)
    current_norm = _normalise_url(current_url)
    if current_norm and (not base_norm or not _website_domains_match(base_norm, current_norm)):
        current_norm = None
    root_source = current_norm or base_norm
    if not root_source:
        return []
    parsed = urllib.parse.urlparse(root_source)
    if not parsed.scheme or not parsed.netloc:
        return []

    root_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
    seen: Set[str] = set()
    skip = {url for url in (base_norm, current_norm) if url}
    candidates: List[str] = []
    for path in WEBSITE_EMAIL_SHALLOW_PATHS:
        candidate = _normalise_url(urllib.parse.urljoin(root_url, path))
        if not candidate or candidate in seen or candidate in skip:
            continue
        if not _website_domains_match(root_url, candidate):
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def _website_fetch_result_is_same_domain(result: WebsiteFetchResult, base_url: str) -> bool:
    final_url = _normalise_url(result.final_url or result.url or "")
    base_norm = _normalise_url(base_url or "")
    if not final_url or not base_norm:
        return False
    return _website_domains_match(base_norm, final_url)


def _website_cache_key(url: str) -> str:
    return extract_domain(_normalise_url(url) or url)


def _is_platform_host(host: str) -> bool:
    if not host:
        return False
    return any(host.endswith(domain) for domains in PLATFORM_HOSTS.values() for domain in domains)


def _is_website_enrich_candidate_url(url: str, *, allow_platform: bool = False) -> bool:
    normalised = _normalise_url(url)
    if not normalised or _is_noise_url(normalised):
        return False
    host = _host(normalised)
    if not host:
        return False
    path_lower = urllib.parse.urlparse(normalised).path.lower()
    if host in LINK_HUB_HOSTS:
        return False
    if any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST):
        return False
    if not allow_platform and _is_platform_host(host):
        return False
    if host in JUNK_WEBSITE_HOSTS:
        return False
    if any(keyword in path_lower for keyword in JUNK_WEBSITE_PATH_KEYWORDS):
        return False
    return True


def _collect_website_enrich_candidate_urls(row: Any) -> List[str]:
    if row is None:
        return []

    candidates: List[str] = []
    seen_urls: Set[str] = set()
    seen_domains: Set[str] = set()

    def _add_value(value: Any, *, allow_platform: bool = False) -> None:
        for token in _split_multi_value(value):
            normalised = _normalise_url(token)
            if not normalised:
                continue
            domain = _website_cache_key(normalised)
            if not domain or normalised in seen_urls or domain in seen_domains:
                continue
            if not _is_website_enrich_candidate_url(normalised, allow_platform=allow_platform):
                continue
            seen_urls.add(normalised)
            seen_domains.add(domain)
            candidates.append(normalised)

    _add_value(row.get("Spotify_Website_URL", ""))

    bandcamp_raw = _coerce_directory_value(row.get("Bandcamp_URL", "")) if hasattr(row, "get") else ""
    bandcamp_norm = _canonicalise_bandcamp_url(bandcamp_raw) if bandcamp_raw else ""
    if bandcamp_norm:
        bandcamp_host = _host(bandcamp_norm)
        _add_value(bandcamp_norm, allow_platform=bool(bandcamp_host.endswith("bandcamp.com")))

    _add_value(row.get("External Links", ""))

    for field in WEBSITE_EMAIL_OPTIONAL_FIELDS:
        if hasattr(row, "__contains__") and field not in row:
            continue
        _add_value(row.get(field, ""))

    return candidates


def _fetch_website_html_bounded(
    session: requests.Session,
    url: str,
    *,
    timeout_s: float = WEBSITE_EMAIL_TIMEOUT,
    max_bytes: int = WEBSITE_EMAIL_MAX_BYTES,
) -> WebsiteFetchResult:
    final_url = url
    status = None
    content_type = ""
    try:
        response = session.get(url, timeout=timeout_s, allow_redirects=True, stream=True)
        status = getattr(response, "status_code", None)
        final_url = getattr(response, "url", url) or url
        content_type = cell_to_str(response.headers.get("Content-Type", "")).lower()
        is_html = "html" in content_type or "xhtml" in content_type
        if not is_html:
            try:
                response.close()
            except Exception:
                pass
            return WebsiteFetchResult(
                url=url,
                final_url=final_url,
                status=status,
                content_type=content_type,
                html="",
                is_html=False,
            )
        chunks: List[bytes] = []
        bytes_read = 0
        for chunk in response.iter_content(chunk_size=16_384, decode_unicode=False):
            if not chunk:
                continue
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                try:
                    response.close()
                except Exception:
                    pass
                return WebsiteFetchResult(
                    url=url,
                    final_url=final_url,
                    status=status,
                    content_type=content_type,
                    html="",
                    is_html=False,
                )
            chunks.append(chunk)
        encoding = getattr(response, "encoding", None) or "utf-8"
        html = b"".join(chunks).decode(encoding, errors="replace")
        return WebsiteFetchResult(
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            html=html,
            is_html=True,
        )
    except Exception:
        return WebsiteFetchResult(
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            html="",
            is_html=False,
        )


def _coerce_directory_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        parts = [_clean_cell(part) for part in value]
        value = MULTI_VALUE_SEPARATOR.join(part for part in parts if part)
    text = _clean_cell(value)
    if not text:
        return ""
    if text.lower() in EMPTY_FIELD_MARKERS:
        return ""
    return text


def _normalise_genre_value(value: str) -> str:
    if not value:
        return ""
    for delimiter in (";", "|"):
        if delimiter in value:
            value = value.split(delimiter, 1)[0]
            break
    return value.strip()


def _iter_directory_field_values(
    row: Dict[str, Any],
    source: str,
    field_key: str,
) -> Iterable[str]:
    mapping = DIRECTORY_FIELD_MAP.get(source, {})
    columns = mapping.get(field_key, ())
    for column in columns:
        if column not in row:
            continue
        value = _coerce_directory_value(row.get(column))
        if not value:
            continue
        if field_key == "primary_genre":
            value = _normalise_genre_value(value)
        if value:
            yield value


def _first_directory_value(
    matches: Iterable[Tuple[str, Dict[str, Any]]],
    field_key: str,
) -> str:
    for source, row in matches:
        for value in _iter_directory_field_values(row, source, field_key):
            if value:
                return value
    return ""


def _parse_release_date(value: str) -> Optional[datetime.date]:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in EMPTY_FIELD_MARKERS:
        return None
    parsed = None
    for dayfirst in (False, True):
        try:
            parsed_ts = pd.to_datetime(cleaned, errors="coerce", dayfirst=dayfirst)
        except Exception:
            parsed_ts = None
        if parsed_ts is not None and not pd.isna(parsed_ts):
            if isinstance(parsed_ts, datetime.datetime):
                parsed = parsed_ts.date()
            else:
                try:
                    parsed = parsed_ts.to_pydatetime().date()
                except Exception:
                    parsed = None
            if parsed:
                return parsed
    if re.fullmatch(r"\d{4}-\d{2}", cleaned):
        year, month = map(int, cleaned.split("-"))
        month = min(max(month, 1), 12)
        return datetime.date(year, month, 1)
    if re.fullmatch(r"\d{4}", cleaned):
        return datetime.date(int(cleaned), 1, 1)
    return None


_EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}")


def _extract_emails_from_html_text(html: str) -> Set[str]:
    """
    Lightweight full-document email scan with basic obfuscation normalisation.
    Only used for Bandcamp fallback so we keep it conservative.
    """
    emails: Set[str] = set()
    if not html:
        return emails
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = html
    if not text:
        return emails

    def _normalise_obfuscations(blob: str) -> str:
        cleaned = blob
        cleaned = re.sub(r"\\s*\\[\\s*at\\s*\\]\\s*", "@", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s*\\(\\s*at\\s*\\)\\s*", "@", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s+at\\s+", "@", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s*\\[\\s*dot\\s*\\]\\s*", ".", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s*\\(\\s*dot\\s*\\)\\s*", ".", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s+dot\\s+", ".", cleaned, flags=re.IGNORECASE)
        return cleaned

    from email_normalizer import normalize_obfuscated_email_patterns  # local import to avoid heavyweight cycles
    try:
        normalised_text, replacements = normalize_obfuscated_email_patterns(text)
        if replacements:
            try:
                from pipeline_runner import increment_pattern_emails

                increment_pattern_emails(replacements)
            except Exception:
                pass
    except Exception:
        normalised_text = text
        replacements = 0
    # Preserve legacy dot normalization to keep backwards-compatible coverage.
    normalised_text = re.sub(r"\s*\[\s*dot\s*\]\s*", ".", normalised_text, flags=re.IGNORECASE)
    normalised_text = re.sub(r"\s*\(\s*dot\s*\)\s*", ".", normalised_text, flags=re.IGNORECASE)
    normalised_text = re.sub(r"\s+dot\s+", ".", normalised_text, flags=re.IGNORECASE)
    for match in _EMAIL_REGEX.findall(normalised_text):
        candidate = match.strip().lower()
        if not candidate or candidate.count("@") != 1:
            continue
        local, _, domain = candidate.partition("@")
        if len(local) < 2 or len(domain) < 4 or "." not in domain:
            continue
        emails.add(candidate)
    return emails


def _bandcamp_pick_internal_follow_url(soup: BeautifulSoup, profile_url: str) -> Optional[str]:
    """Pick a single /contact or /about URL on the same host."""
    if not soup or not profile_url:
        return None
    try:
        host = urllib.parse.urlparse(profile_url).netloc.lower()
    except Exception:
        return None
    candidates: List[Tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urllib.parse.urljoin(profile_url, href)
        try:
            parsed = urllib.parse.urlparse(absolute)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != host:
            continue
        path = (parsed.path or "").lower()
        if path.startswith("/contact"):
            candidates.append((0, absolute))
        elif path.startswith("/about"):
            candidates.append((1, absolute))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _bandcamp_pick_first_track_url(soup: BeautifulSoup, profile_url: str) -> Optional[str]:
    """Return first same-host track URL (one-time fallback)."""
    if not soup or not profile_url:
        return None
    try:
        host = urllib.parse.urlparse(profile_url).netloc.lower()
    except Exception:
        return None
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urllib.parse.urljoin(profile_url, href)
        try:
            parsed = urllib.parse.urlparse(absolute)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != host:
            continue
        if "/track/" in (parsed.path or "").lower():
            return absolute
    return None


def _extract_links_from_profile(
    html: str,
    source_dir: str,
    profile_url: str,
) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    socials: Set[str] = set()
    websites: Set[str] = set()
    emails: Set[str] = set()
    link_hubs: Set[str] = set()
    if not html:
        return socials, websites, emails, link_hubs
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        lower = href.lower()
        if lower.startswith("mailto:"):
            addr = href.split("mailto:", 1)[1].split("?", 1)[0].strip().lower()
            if addr:
                emails.add(addr)
            continue
        if lower.startswith("#") or lower.startswith("javascript:"):
            continue
        absolute = urllib.parse.urljoin(profile_url, href)
        normalised = _normalise_url(absolute)
        if not normalised or _is_noise_url(normalised):
            continue
        parsed = urllib.parse.urlparse(normalised)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.netloc.lower()
        path_lower = parsed.path.lower()
        if source_dir == "bandcamp" and host.endswith("bandcamp.com"):
            continue
        if source_dir == "soundcloud" and host.endswith("soundcloud.com"):
            continue
        if source_dir == "lastfm" and (
            host.endswith("last.fm") or host.endswith("lastfm.com") or "lastfm" in host
        ):
            continue
        if source_dir == "lastfm" and any(keyword in lower for keyword in LASTFM_BRAND_KEYWORDS):
            continue
        if any(fragment in path_lower for fragment in PATH_NOISE):
            continue
        if host in LINK_HUB_HOSTS:
            link_hubs.add(normalised)
            websites.add(normalised)
        elif any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST):
            socials.add(normalised)
        else:
            if host in JUNK_WEBSITE_HOSTS:
                continue
            if any(keyword in path_lower for keyword in JUNK_WEBSITE_PATH_KEYWORDS):
                continue
            websites.add(normalised)
    return socials, websites, emails, link_hubs


def _extract_emails_from_html_text(html: str) -> Set[str]:
    emails: Set[str] = set()
    if not html:
        return emails
    text = ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
    except Exception:
        if isinstance(html, str):
            text = html
    if not text:
        return emails
    normalized = text
    try:
        from email_normalizer import normalize_obfuscated_email_patterns

        normalized, replacements = normalize_obfuscated_email_patterns(normalized)
        if replacements:
            try:
                from pipeline_runner import increment_pattern_emails

                increment_pattern_emails(replacements)
            except Exception:
                pass
    except Exception:
        replacements = 0
    for pattern in (
        r"\\s*\\[\\s*dot\\s*\\]\\s*",
        r"\\s*\\(\\s*dot\\s*\\)\\s*",
        r"\\s+dot\\s+",
    ):
        normalized = re.sub(pattern, ".", normalized, flags=re.IGNORECASE)
    for match in _EMAIL_REGEX.findall(normalized):
        cleaned = match.strip().lower()
        if not cleaned:
            continue
        if cleaned.count("@") != 1:
            continue
        local, _, domain = cleaned.partition("@")
        if len(local) < 2 or len(domain) < 4 or "." not in domain:
            continue
        if ".." in cleaned:
            cleaned = cleaned.replace("..", ".")
        if cleaned:
            emails.add(cleaned)
    return emails


def _bandcamp_pick_internal_follow_url(
    soup: Optional[BeautifulSoup], profile_url: str
) -> Optional[str]:
    if not soup or not profile_url:
        return None
    try:
        profile_host = urllib.parse.urlparse(profile_url).netloc.lower()
    except Exception:
        profile_host = ""
    if not profile_host:
        return None
    anchors = soup.find_all("a", href=True)
    for target_path in ("/contact", "/about"):
        for anchor in anchors:
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            absolute = urllib.parse.urljoin(profile_url, href)
            try:
                parsed = urllib.parse.urlparse(absolute)
            except Exception:
                continue
            if parsed.scheme not in ("http", "https"):
                continue
            if parsed.netloc.lower() != profile_host:
                continue
            path = re.sub(r"/+$", "", parsed.path or "/") or "/"
            if path.endswith(target_path):
                absolute_clean, _ = urllib.parse.urldefrag(absolute)
                return absolute_clean or absolute
    return None


def _bandcamp_pick_first_track_url(
    soup: Optional[BeautifulSoup], profile_url: str
) -> Optional[str]:
    if not soup or not profile_url:
        return None
    try:
        profile_host = urllib.parse.urlparse(profile_url).netloc.lower()
    except Exception:
        profile_host = ""
    if not profile_host:
        return None
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urllib.parse.urljoin(profile_url, href)
        try:
            parsed = urllib.parse.urlparse(absolute)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != profile_host:
            continue
        path = parsed.path.lower()
        if "/track/" in path:
            absolute_clean, _ = urllib.parse.urldefrag(absolute)
            return absolute_clean or absolute
    return None


def _scrape_link_hub_socials(session: requests.Session, hub_url: str) -> Set[str]:
    socials: Set[str] = set()
    if not hub_url:
        return socials
    print(f"[Enricher] Scraping link hub: {hub_url}")
    try:
        resp = session.get(hub_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[Enricher] Link hub fetch failed {hub_url}: {exc}")
        return socials
    soup = BeautifulSoup(resp.text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        absolute = urllib.parse.urljoin(hub_url, href)
        normalised = _normalise_url(absolute)
        if not normalised or _is_noise_url(normalised):
            continue
        host = _host(normalised)
        if any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST):
            socials.add(normalised)
    return socials


def _resolve_csv_path(filename: str, seed_dir: str) -> Optional[str]:
    if not filename:
        return None
    candidates = []
    if os.path.isabs(filename):
        candidates.append(filename)
    else:
        candidates.append(os.path.join(seed_dir, filename))
        candidates.append(os.path.join(BASE_DIR, filename))
        candidates.append(os.path.abspath(filename))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _norm_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = name.strip().lower()
    if not name:
        return ""
    return " ".join(name.split())

def normalize_name(name: str) -> str:
    """
    Normalise a name for comparison:
    - lowercase
    - strip leading/trailing whitespace
    - collapse internal whitespace
    - strip common punctuation
    - remove accents
    """
    if not isinstance(name, str):
        return ""
    cleaned = unicodedata.normalize("NFKD", name)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.lower()
    cleaned = re.sub(r"[.,!?:;\'\"\\-_/]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def normalise_artist_name(name: str) -> str:
    base = _norm_name(name)
    if not base:
        return ""
    for suffix in (" official", " - topic"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            base = base.strip()
    base = re.sub(r"^(the|a)\s+", "", base)
    base = re.sub(r"[^\w\s]", "", base)
    base = re.sub(r"\s+", " ", base)
    base = unicodedata.normalize("NFKD", base)
    base = "".join(ch for ch in base if not unicodedata.combining(ch))
    return base.strip()


def _normalise_for_soundcloud(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch.isspace())
    return " ".join(cleaned.split())


_SC_LABEL_PODCAST_KEYWORDS = {
    "records",
    "recordings",
    "label",
    "radio",
    "podcast",
    "station",
    "network",
}
_SC_CONFIDENCE_ACCEPT = 0.68
_SC_CONFIDENCE_MIN = 0.45
_SC_CLIENT_ID_CACHE = ""
_SC_ASSET_JS_PATTERN = re.compile(r"https://a-v2\.sndcdn\.com/assets/\d+-[a-z0-9]+\.js", re.IGNORECASE)
_SC_CLIENT_ID_PATTERN = re.compile(r'client_id:"([a-zA-Z0-9]+)"')
_SC_GENERIC_TOKENS = {"ix", "dj", "mc"}
MIN_SC_CONFIDENCE = 0.95
_LOCALE_FALLBACK_KEYWORDS = {"brazil", "brasil", "sao paulo", "rio de janeiro"}


def _sc_handle_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        segment = parsed.path.strip("/").split("/")[0]
        return segment.lower()
    except Exception:
        return ""


def _sc_handle_from_profile_url(url: str) -> Optional[str]:
    """
    Extract a SoundCloud handle from a profile URL, ignoring query/fragment noise.
    Returns None when the URL is not a SoundCloud profile.
    """
    if not url:
        return None
    try:
        normalised = _normalise_url(url)
        if not normalised:
            return None
        parsed = urllib.parse.urlparse(normalised)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host != "soundcloud.com":
            return None
        path = parsed.path.split("?", 1)[0]
        handle = path.strip("/").split("/")[0]
        return handle.lower() or None
    except Exception:
        return None


def _sc_normalise_text(value: str) -> str:
    cleaned = _normalise_for_soundcloud(value or "")
    return cleaned.lower().strip()


def _sc_strip_basic(value: str) -> str:
    """Lowercase, trim, and drop simple punctuation for strict short-name checks."""
    text = (value or "").lower().strip()
    return re.sub(r"[\\.,!?\\'\\\"]+", "", text)


def _locale_bias_amount(text: str) -> float:
    """
    Optional locale-aware nudge for tie-breaking between already plausible candidates.
    Never used to relax safety thresholds; consumers should keep using base scores for acceptance checks.
    """
    if not ENABLE_LOCALE_BIAS:
        return 0.0
    haystack = (text or "").lower()
    hint = (LOCALE_COUNTRY_HINT or "").strip().lower()
    keywords = set()
    if hint:
        keywords.add(hint)
        if hint in {"br", "bra", "brazil", "brasil"}:
            keywords |= _LOCALE_FALLBACK_KEYWORDS
    else:
        keywords |= set()
    if any(keyword and keyword in haystack for keyword in keywords):
        return 0.03
    return 0.0


def _locale_rank_score(base_score: float, *texts: str) -> float:
    bias = _locale_bias_amount(" ".join([t for t in texts if t]))
    if bias <= 0.0:
        return base_score
    return min(1.0, base_score + bias)


def _bandcamp_confidence(artist_name: str, display_name: str, profile_url: str, song_title: str = "") -> float:
    """
    Lightweight Bandcamp confidence:
    - Name similarity baseline
    - Boost when subdomain closely matches artist name
    - Optional boost when song title overlaps search context
    - Small penalty for label/store/festival-like tokens
    """
    artist_norm = normalize_name(artist_name)
    disp_norm = normalize_name(display_name or "")
    if not artist_norm or not disp_norm:
        return 0.0
    score = difflib.SequenceMatcher(None, artist_norm, disp_norm).ratio()
    # Subdomain boost when it closely matches the artist.
    try:
        parsed = urllib.parse.urlparse(profile_url or "")
        host = (parsed.netloc or "").split(".")[0].lower()
        host_norm = normalize_name(host)
        if host_norm and artist_norm and (host_norm == artist_norm or artist_norm in host_norm or host_norm in artist_norm):
            score = max(score, score + 0.08)
    except Exception:
        pass
    # Song-title boost if provided and appears in display text.
    song_norm = normalize_name(song_title or "")
    if song_norm and song_norm in disp_norm:
        score += 0.03
    penalty_tokens = {"records", "recordings", "label", "store", "festival", "shop"}
    if any(tok in disp_norm for tok in penalty_tokens):
        score -= 0.1
    return max(0.0, min(score, 1.0))


def _bc_slug_candidates(artist_name: str) -> List[str]:
    """
    Conservative slug guesses for https://{slug}.bandcamp.com
    - lower, accent-free, alnum only
    - space joiners: none or hyphen
    - drop leading 'the ' variant
    """
    norm = normalize_name(artist_name)
    if not norm:
        return []
    words = [re.sub(r"[^a-z0-9]+", "", w) for w in norm.split() if re.sub(r"[^a-z0-9]+", "", w)]
    if not words:
        return []
    variants = []
    def _push(parts):
        slug = "".join(parts)
        if slug and slug not in variants:
            variants.append(slug)
        hy = "-".join(parts)
        if hy and hy not in variants:
            variants.append(hy)
    _push(words)
    if words[0] == "the" and len(words) > 1:
        _push(words[1:])
    return variants[:BC_FALLBACK_MAX_SLUGS]


def _lastfm_confidence(artist_name: str, candidate_name: str) -> float:
    artist_norm = normalize_name(artist_name)
    cand_norm = normalize_name(candidate_name)
    if not artist_norm or not cand_norm:
        return 0.0
    score = difflib.SequenceMatcher(None, artist_norm, cand_norm).ratio()
    if artist_norm == cand_norm:
        score = max(score, 0.98)
    return max(0.0, min(score, 1.0))


def _sc_extract_client_id_from_js(text: str) -> str:
    if not text:
        return ""
    match = re.search(r'"client_id":"([a-zA-Z0-9]+)"', text)
    if match:
        return match.group(1)
    match = re.search(r"client_id=([a-zA-Z0-9]+)", text)
    if match:
        return match.group(1)
    return ""


def _sc_get_client_id(session: requests.Session) -> str:
    global _SC_CLIENT_ID_CACHE
    if _SC_CLIENT_ID_CACHE:
        return _SC_CLIENT_ID_CACHE
    scraped = _sc_scrape_client_id(session)
    if scraped:
        _SC_CLIENT_ID_CACHE = scraped
        return _SC_CLIENT_ID_CACHE
    return ""


def _sc_test_client_id(session: requests.Session, candidate: str) -> bool:
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={"url": "https://soundcloud.com/soundcloud", "client_id": candidate},
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        return resp.status_code == 200
    except Exception:
        return False


def _sc_scrape_client_id(session: requests.Session) -> str:
    sources = [
        "https://soundcloud.com/discover",
        "https://soundcloud.com",
    ]
    for source in sources:
        try:
            resp = session.get(source, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except Exception:
            continue
        cid = _sc_extract_client_id_from_js(resp.text or "")
        if cid and _sc_test_client_id(session, cid):
            return cid
        assets = _SC_ASSET_JS_PATTERN.findall(resp.text or "")
        for asset_url in assets[:10]:
            try:
                js_resp = session.get(asset_url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
                js_resp.raise_for_status()
                match = _SC_CLIENT_ID_PATTERN.search(js_resp.text or "")
                if match:
                    candidate = match.group(1)
                    if _sc_test_client_id(session, candidate):
                        return candidate
            except Exception:
                continue
    return ""


def _sc_location_match(hint: str, candidate: str) -> bool:
    if not hint or not candidate:
        return False
    hint_norm = _clean_cell(hint).lower()
    cand_norm = _clean_cell(candidate).lower()
    if not hint_norm or not cand_norm:
        return False
    if hint_norm in cand_norm:
        return True
    hint_parts = [part.strip() for part in hint_norm.split(",") if part.strip()]
    return all(part in cand_norm for part in hint_parts)


def _sc_score_candidate(
    artist_name: str,
    candidate_name: str,
    handle: str,
    location_hint: str = "",
    candidate_location: str = "",
    genre_hint: str = "",
) -> float:
    """
    Lightweight confidence score for SoundCloud candidates:
    - anchor on cleaned display name + handle similarity to artist name
    - boost on location/genre hints when available
    - penalise label/podcast-like handles to de-prioritise obvious mismatches
    """
    artist_norm = _sc_normalise_text(artist_name)
    cand_norm = _sc_normalise_text(candidate_name or handle)
    handle_norm = _sc_normalise_text(handle)
    if not artist_norm or not cand_norm:
        return 0.0
    artist_norm_basic = _sc_strip_basic(artist_norm)
    cand_norm_basic = _sc_strip_basic(cand_norm)
    handle_norm_basic = _sc_strip_basic(handle_norm)
    ratio = difflib.SequenceMatcher(None, artist_norm, cand_norm).ratio()
    score = ratio
    if artist_norm == cand_norm:
        score = max(score, 0.95)
    if handle_norm and (artist_norm == handle_norm or artist_norm in handle_norm):
        score = max(score, 0.92)
    if handle_norm and handle_norm.replace("_", " ") == artist_norm:
        score = max(score, 0.9)
    if artist_norm and cand_norm.startswith(artist_norm):
        score = max(score, 0.85)
    if artist_norm and handle_norm.startswith(artist_norm.split()[0]):
        score = max(score, score + 0.05)
    if location_hint and _sc_location_match(location_hint, candidate_location):
        score += 0.08
    if genre_hint:
        genre_norm = _sc_normalise_text(genre_hint)
        if genre_norm and genre_norm in _sc_normalise_text(candidate_name):
            score += 0.05
    if any(keyword in handle_norm for keyword in _SC_LABEL_PODCAST_KEYWORDS):
        score -= 0.25
    if any(keyword in cand_norm for keyword in _SC_LABEL_PODCAST_KEYWORDS):
        score -= 0.15
    # Generic/short-name penalty unless exact basic match.
    if artist_norm_basic in _SC_GENERIC_TOKENS or len(artist_norm_basic) <= 3:
        if not (artist_norm_basic and artist_norm_basic == cand_norm_basic == handle_norm_basic):
            score -= 0.15
    # Penalise digits in candidate when artist name has none.
    if artist_norm_basic and not any(ch.isdigit() for ch in artist_norm_basic):
        if any(ch.isdigit() for ch in cand_norm_basic) or any(ch.isdigit() for ch in handle_norm_basic):
            score -= 0.2
    return max(0.0, min(score, 1.0))


def _build_soundcloud_queries(base_query: str, track_hint: str = "", location_hint: str = "") -> List[str]:
    """
    Build a small set of progressively broader SoundCloud search strings.
    - base: artist name
    - optional: artist + track, artist + location
    """
    queries: List[str] = []

    def _add(candidate: str):
        candidate = (candidate or "").strip()
        if candidate and candidate not in queries:
            queries.append(candidate)

    _add(base_query)
    if track_hint:
        _add(f"{base_query} {track_hint}")
    if location_hint:
        _add(f"{base_query} {location_hint}")
    return queries[:4]

def _clean_soundcloud_query(name: str) -> str:
    """
    Prepare a SoundCloud search query once per artist to avoid duplicate logical searches.
    """
    query = cell_to_str(name)
    query = query.strip()
    if len(query) > 1 and query.endswith("."):
        query = query[:-1].strip()
    query = re.sub(r"\s+", " ", query)
    return query


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def normalise_track_title(value: Any) -> str:
    text = _clean_cell(value)
    if not text:
        return ""
    lowered = text.lower()
    lowered = lowered.replace("’", "'")
    lowered = re.sub(r"\s+-\s+topic$", "", lowered)
    lowered = re.sub(r"\bofficial\b", "", lowered)
    lowered = re.sub(r"\(.*?\)", " ", lowered)
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def _compute_row_track_tokens(row: Dict[str, Any]) -> Tuple[str, ...]:
    tokens: List[str] = []
    seen: Set[str] = set()
    for column in TRACK_NAME_COLUMNS:
        token = normalise_track_title(row.get(column))
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


def _row_track_tokens(row: Dict[str, Any]) -> Tuple[str, ...]:
    tokens = row.get(TRACK_TOKEN_FIELD)
    if isinstance(tokens, (list, tuple, set)):
        return tuple(token for token in tokens if token)
    computed = _compute_row_track_tokens(row)
    if computed:
        row[TRACK_TOKEN_FIELD] = computed
    return computed


def _extract_seed_track_text(row: pd.Series) -> str:
    for column in SEED_TRACK_COLUMNS:
        value = _clean_cell(row.get(column))
        if value:
            return value
    return ""


def _extract_seed_track_key(row: pd.Series) -> str:
    for column in SEED_TRACK_COLUMNS:
        value = _clean_cell(row.get(column))
        token = normalise_track_title(value)
        if token:
            return token
    return ""


def _iter_seed_links(row: pd.Series) -> Iterable[str]:
    if row is None:
        return []
    for column in SEED_LINK_COLUMNS:
        if column not in row:
            continue
        value = row.get(column)
        for token in _split_multi_value(value):
            normalised = _normalise_url(token)
            if normalised:
                yield normalised


def _extract_seed_links_by_source(row: pd.Series) -> Dict[str, Set[str]]:
    mapping: Dict[str, Set[str]] = {}
    for url in _iter_seed_links(row):
        source = _source_for_url(url)
        if not source:
            continue
        mapping.setdefault(source, set()).add(url)
    return mapping


def dedupe_pre_enrich(df: pd.DataFrame) -> pd.DataFrame:
    """
    Removes duplicate artist rows before enrichment based on
    composite key: Artist Name + Song Title + Source Directory (+ Email if present).
    Normalises text before comparison.
    Logs how many duplicates were removed.
    Returns the cleaned DataFrame.
    """
    if df is None:
        return df

    total_before = len(df.index)

    def _log_dedupe(removed: int, kept: int) -> str:
        message = f"[Deduper] Removed {removed} duplicate rows (kept {kept} unique rows)"
        try:
            dedupe_pre_enrich._last_log_message = message  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            print(message)
        except Exception:
            pass
        return message

    if total_before == 0:
        _log_dedupe(0, 0)
        return df

    def _song_key(row: pd.Series) -> str:
        token = _extract_seed_track_key(row)
        if token:
            return token
        for column in TRACK_NAME_COLUMNS:
            if column not in row:
                continue
            candidate = normalise_track_title(row.get(column))
            if candidate:
                return candidate
        return ""

    def _contact_key(row: pd.Series) -> str:
        email = _clean_cell(row.get("Email")).lower()
        if email:
            return email
        for column in ("External Links", "Spotify_URL", "SoundCloud Link"):
            if column not in row:
                continue
            raw = _clean_cell(row.get(column))
            if not raw:
                continue
            for token in _split_multi_value(raw):
                normalised = _normalise_url(token) or token.strip().lower()
                if normalised:
                    return normalised
        return ""

    def _contact_rank(row: pd.Series) -> Tuple[int, str]:
        """
        Lower ranks are better. Prefers rows with an email, then rows that carry a
        Facebook social link (so we keep the row that can produce an email during FB
        enrichment instead of dropping it during dedupe), then other social links by
        SOCIAL_PRIORITY order. Rows with no contact info get the worst score.
        """
        email = _clean_cell(row.get("Email"))
        if email:
            return (0, email)

        social_raw = _clean_cell(row.get("Social Link"))
        if social_raw:
            social = _normalise_url(social_raw) or social_raw.strip().lower()
            host = urllib.parse.urlparse(social).netloc.lower() if social else ""
            if "facebook.com" in host or host.endswith("fb.me"):
                return (1, social)
            social_rank, _ = _social_sort_key(social)
            return (2 + social_rank, social)

        external_raw = _clean_cell(row.get("External Links"))
        if external_raw:
            external = _normalise_url(external_raw) or external_raw.strip().lower()
            external_rank, _ = _social_sort_key(external)
            return (20 + external_rank, external)

        return (999, "")

    seen: Set[Tuple[str, str, str, str]] = set()
    keep_indices: List[Any] = []
    best_contact_for_key: Dict[
        Tuple[str, str, str, str], Tuple[Any, bool, Tuple[int, str]]
    ] = {}
    for idx, row in df.iterrows():
        artist_key = normalise_artist_name(_clean_cell(row.get("Artist Name")))
        if not artist_key:
            keep_indices.append(idx)
            continue
        track_key = _song_key(row)
        track_key = unidecode(track_key) if track_key else ""
        source_raw = _clean_cell(row.get("Source Directory"))
        source_key = _canonical_source_key(source_raw) or _norm_name(source_raw)
        contact = _contact_key(row)
        composite = (artist_key, track_key, source_key, contact)
        has_email = bool(_clean_cell(row.get("Email")))
        contact_rank = _contact_rank(row)
        if composite in seen:
            _, existing_has_email, existing_rank = best_contact_for_key.get(
                composite, (None, False, (999, ""))
            )
            # Prefer the row that carries an email if the first kept row lacked one.
            should_replace = False
            if has_email and not existing_has_email:
                should_replace = True
            elif has_email == existing_has_email and contact_rank < existing_rank:
                should_replace = True

            if should_replace:
                previous_idx, _, _ = best_contact_for_key[composite]
                try:
                    keep_indices.remove(previous_idx)
                except ValueError:
                    pass
                keep_indices.append(idx)
                best_contact_for_key[composite] = (idx, has_email, contact_rank)
            continue
        seen.add(composite)
        best_contact_for_key[composite] = (idx, has_email, contact_rank)
        keep_indices.append(idx)

    deduped_df = df.loc[keep_indices].copy()
    deduped_df.reset_index(drop=True, inplace=True)
    removed = total_before - len(deduped_df.index)
    _log_dedupe(removed, len(deduped_df.index))
    return deduped_df


EMAIL_COLUMNS_REQUIRED: Tuple[str, ...] = ("Email", "Email_All")
EMAIL_COLUMNS_PROVENANCE: Tuple[str, ...] = (
    "Email_Type",
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
)


def _ensure_email_columns(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """
    Guarantee email columns exist with string dtype to prevent KeyError during enrichment.
    Operates in-place and returns the same dataframe for convenience.
    """
    if df is None:
        return df
    for column in EMAIL_COLUMNS_REQUIRED + EMAIL_COLUMNS_PROVENANCE:
        if column not in df.columns:
            df[column] = ""
        else:
            df[column] = df[column].fillna("").astype(str)
    return df


def _apply_fb_promotion_df(df: pd.DataFrame, log_fn: Optional[Callable[[str], None]] = None) -> pd.DataFrame:
    """Promote Facebook URLs from generic link fields into facebook_url/Facebook_URL."""
    if df is None or df.empty:
        return df
    if "facebook_url" not in df.columns:
        df["facebook_url"] = ""
    if "Facebook_URL" not in df.columns:
        df["Facebook_URL"] = ""
    if "Facebook URL" not in df.columns:
        df["Facebook URL"] = ""
    populated = 0
    canonical_from_alias = 0
    canonical_from_links = 0
    for idx in df.index:
        new_url, source = ensure_canonical_facebook_url(df.loc[idx], set_row=False)
        if not new_url:
            continue
        wrote = False
        current_canonical_raw = _coerce_directory_value(df.loc[idx, "Facebook_URL"])
        current_canonical = canonicalize_facebook_url(current_canonical_raw)
        if current_canonical and current_canonical_raw != current_canonical:
            df.loc[idx, "Facebook_URL"] = current_canonical
        elif not current_canonical:
            df.loc[idx, "Facebook_URL"] = new_url
            wrote = True
            if source in {"Social Link", "External Links", "Website", "Websites", "Website URL"}:
                canonical_from_links += 1
            elif source and source != "Facebook_URL":
                canonical_from_alias += 1
        if not canonicalize_facebook_url(df.loc[idx, "facebook_url"]):
            df.loc[idx, "facebook_url"] = new_url
            wrote = True
        if "Facebook URL" in df.columns and not canonicalize_facebook_url(df.loc[idx, "Facebook URL"]):
            df.loc[idx, "Facebook URL"] = new_url
            wrote = True
        if wrote:
            populated += 1
    if log_fn and populated:
        _safe_log(log_fn, "[FB Promotion] facebook_url populated for %s rows", populated)
    if log_fn and canonical_from_alias:
        _safe_log(log_fn, "[FB Promotion] canonical Facebook_URL backfilled from alias fields for %s rows", canonical_from_alias)
    if log_fn and canonical_from_links:
        _safe_log(log_fn, "[FB Promotion] canonical Facebook_URL backfilled from Social Link / External Links for %s rows", canonical_from_links)
    return df


def _dedupe_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        marker = id(row)
        if marker in seen_ids:
            continue
        seen_ids.add(marker)
        deduped.append(row)
    return deduped


def _load_directory_csv(path: str, source_name: str) -> DirectoryIndex:
    index = DirectoryIndex(source=source_name)
    if not path or not os.path.exists(path):
        return index
    df = _read_csv_flexible(path)
    if df is None or df.empty:
        print(f"[Enricher] {source_name} CSV empty or unreadable: {path}")
        return index
    if "Artist Name" not in df.columns:
        print(f"[Enricher] CSV for {source_name} missing 'Artist Name': {path}")
        return index
    for _, row in df.iterrows():
        artist = _clean_cell(row.get("Artist Name"))
        if not artist:
            continue
        key = normalise_artist_name(artist)
        row_dict = row.to_dict()
        tokens = _compute_row_track_tokens(row_dict)
        index.add_row(key, row_dict, tokens)
    print(
        f"[Enricher] Loaded {index.unique_artist_count()} artists for {source_name} from {path}"
    )
    return index


# ---------------------------------------------------------------------------
# Worker implementation
# ---------------------------------------------------------------------------
class CrossDirectoryEnricherWorker(QThread):
    progress = pyqtSignal(int)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(
        self,
        seed_csv_path: str,
        output_csv_path: str,
        bandcamp_csv_path: str = "",
        soundcloud_csv_path: str = "",
        unearthed_csv_path: str = "",
        lastfm_csv_path: str = "",
        enable_live_search: bool = True,
        max_live_searches: int = LIVE_SEARCH_MAX_ATTEMPTS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Night Mode toggle; default off so daytime behaviour is unchanged.
        self.night_mode: bool = False
        self.seed_csv_path = seed_csv_path
        self.output_csv_path = output_csv_path
        self.bandcamp_csv_path = bandcamp_csv_path
        self.soundcloud_csv_path = soundcloud_csv_path
        self.unearthed_csv_path = unearthed_csv_path
        self.lastfm_csv_path = lastfm_csv_path
        self.enable_live_search = enable_live_search
        self.max_live_searches = max_live_searches
        self.session = _build_session()
        self._bc_session = _build_bandcamp_session()
        self.live_search_attempts = 0
        self._notified_limit = False
        self._sc_live_enrich_disabled: bool = False
        self._sc_live_enrich_disabled_reason: str = ""
        self.total_rows = 0
        self._live_context: Dict[str, Any] = {}
        self._row_enrichment_state: Dict[str, str] = {}
        self._fb_discovery_attempted_rows: Set[Any] = set()
        self._night_sc_cache: Dict[str, Dict[str, Any]] = {}
        self._active_night_sc_attempt: Optional[_NightSCAttempt] = None
        self._night_sc_challenge_streak: int = 0  # consecutive challenge detections
        self._night_sc_breaker_tripped: bool = False
        self._sc_live_disabled_until: float = 0.0  # soft cooldown window for live SC
        self._sc_rss_only_mode: bool = False
        self._sc_html_challenge_count: int = 0  # total challenges this run
        self._sc_rss_fail_streak: int = 0
        self._sc_rss_fail_streak_blocked: int = 0
        self._sc_rss_fail_streak_nofeed: int = 0
        self._sc_rss_fail_counts: Counter = Counter()
        self._sc_rss_fail_last_reasons = deque(maxlen=5)
        self._sc_consecutive_403: int = 0
        self._sc_rss_only_logged: bool = False
        self._sc_rss_only_entered_at: float = 0.0
        self._sc_rss_only_rows: int = 0
        self._sc_rss_successes: int = 0
        self._sc_rss_only_entries_consecutive_403: int = 0
        self._sc_rss_only_engine_fetch_skips: int = 0
        self._sc_rows_seen: int = 0
        self._sc_last_challenge_at: float = 0.0
        # Bandcamp run-state
        self._bc_search_attempts: int = 0
        self._bc_total_403: int = 0
        self._bc_consecutive_403: int = 0
        self._bc_search_breaker_tripped: bool = False
        self._bc_search_breaker_reason: str = ""
        self._bc_fallback_used: int = 0
        self._bc_fallback_matches: int = 0
        self._bc_matches: int = 0
        self._bc_no_match: int = 0
        self._bc_breaker_logged: bool = False
        self._last_bc_row_stats: Dict[str, Any] = {}
        self._bc_discover_cache: Dict[str, List[Tuple[str, str]]] = {}
        self._bc_discover_fetches: int = 0
        # Last.fm health state (endpoint-specific)
        self._lf_search_consecutive_406: int = 0
        self._lf_profile_consecutive_406: int = 0
        self._lf_search_cooldown_until: float = 0.0  # monotonic timestamp
        self._lf_profile_cooldown_until: float = 0.0  # monotonic timestamp
        self._lf_search_cooldown_logged: bool = False
        self._lf_profile_cooldown_logged: bool = False
        self._lf_search_cooldown_skip_logged: bool = False
        self._lf_profile_cooldown_skip_logged: bool = False
        self._lf_search_skipped_cooldown: int = 0
        self._lf_profile_skipped_cooldown: int = 0
        # Last.fm run-scoped caches (reset every run)
        self._lf_profile_url_cache: Dict[str, str] = {}
        self._lf_search_result_cache: Dict[str, Optional[str]] = {}
        self._lf_canonical_url_cache: Dict[str, str] = {}
        self._last_final_url: Optional[str] = None
        self._last_resolved_profile_url: Optional[str] = None
        self._live_lookup_bclf_stats: Dict[str, Dict[str, int]] = {}
        self._live_lookup_bclf_adaptive_enabled: bool = False
        self._reset_live_lookup_bclf_stats()
        self._directory_indexes: Dict[str, DirectoryIndex] = {}
        self._domain_email_reuse_index: Dict[str, Dict[str, str]] = {}
        self._domain_email_reuse_rows: Set[Any] = set()
        self._domain_email_reuse_count: int = 0
        self._website_email_cache: Dict[str, Dict[str, Any]] = {}
        self._festival_expansion_rows: List[Dict[str, Any]] = []
        self._festival_expansion_existing_keys: Set[str] = set()
        self._festival_expansion_staged_keys: Set[str] = set()
        self._festival_expansion_raw_csv_path: str = _festival_expansion_raw_path(output_csv_path)
        # Share live-search budget with SoundCloud aggregator fetches.
        try:
            def _agg_budget_check():
                if self.max_live_searches is None or self.max_live_searches <= 0:
                    return (True, "")
                if self.live_search_attempts < self.max_live_searches:
                    return (True, "")
                return (False, "max_live")
            sc_engine._AGGREGATOR_BUDGET_CHECK = _agg_budget_check
        except Exception:
            pass

    def run(self) -> None:
        try:
            self._run_impl()
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Error: {exc}")
            self.finished.emit("")
        finally:
            try:
                self.session.close()
            except Exception:
                pass

    def _run_impl(self) -> None:
        fb_driver = None
        try:
            from pipeline_runner import reset_email_summary_counts

            reset_email_summary_counts()
        except Exception:
            pass
        self._festival_expansion_rows = []
        self._festival_expansion_existing_keys = set()
        self._festival_expansion_staged_keys = set()
        self._festival_expansion_raw_csv_path = _festival_expansion_raw_path(self.output_csv_path)
        try:
            if self._festival_expansion_raw_csv_path and os.path.exists(self._festival_expansion_raw_csv_path):
                os.remove(self._festival_expansion_raw_csv_path)
        except Exception:
            pass
        # Reset SoundCloud live-enrich fail-fast flag for each run.
        self._sc_live_enrich_disabled = False
        self._sc_live_enrich_disabled_reason = ""
        self._sc_rss_only_mode = False
        self._sc_html_challenge_count = 0
        self._sc_rss_fail_streak = 0
        self._sc_rss_fail_streak_blocked = 0
        self._sc_rss_fail_streak_nofeed = 0
        self._sc_rss_fail_counts = Counter()
        self._sc_rss_fail_last_reasons = deque(maxlen=5)
        self._sc_live_disabled_until = 0.0
        self._sc_rss_only_logged = False
        self._sc_rss_only_entered_at = 0.0
        self._sc_rss_only_rows = 0
        self._sc_rss_successes = 0
        self._sc_rows_seen = 0
        self._sc_last_challenge_at = 0.0
        # Last.fm run-scoped state reset each run
        self._lf_search_skipped_cooldown = 0
        self._lf_profile_skipped_cooldown = 0
        self._lf_search_cooldown_skip_logged = False
        self._lf_profile_cooldown_skip_logged = False
        self._lf_profile_url_cache = {}
        self._lf_search_result_cache = {}
        self._lf_canonical_url_cache = {}
        self._last_final_url = None
        self._last_resolved_profile_url = None
        self._fb_discovery_attempted_rows = set()
        self._domain_email_reuse_index = {}
        self._domain_email_reuse_rows = set()
        self._domain_email_reuse_count = 0
        self._website_email_cache = {}
        # Bandcamp discover per-run state
        self._bc_discover_cache = {}
        self._bc_discover_fetches = 0
        try:
            if ENABLE_FACEBOOK_ENRICHMENT:
                try:
                    if not enricher_fb_profile_has_cookies():
                        driver = None
                        try:
                            driver = persistent_fb_driver()
                            try:
                                driver.get("https://www.facebook.com/")
                            except Exception:
                                pass
                            message = "[FB Enrich] Please manually log into Facebook in the opened window."
                            _safe_log(self.log_message.emit, message)
                            try:
                                input("Press ENTER once logged in…")
                            except EOFError:
                                pass
                            except Exception:
                                pass
                        finally:
                            if driver:
                                try:
                                    driver.quit()
                                except Exception:
                                    pass
                    fb_driver = _get_enricher_facebook_driver()
                except Exception as exc:
                    _safe_log(
                        self.log_message.emit,
                        "[FB Enrich] Failed to start Facebook driver: %s",
                        exc,
                    )
                    fb_driver = None
            if not os.path.exists(self.seed_csv_path):
                self.log_message.emit(f"[Enricher] Seed CSV not found: {self.seed_csv_path}")
                self.finished.emit("")
                return
            self.log_message.emit(f"[Enricher] Loading seed CSV: {self.seed_csv_path}")
            seed_df = _read_csv_flexible(self.seed_csv_path)
            if seed_df is None:
                self.log_message.emit("[Enricher] Failed to read Spotify CSV; aborting.")
                self.finished.emit("")
                return
            if "Artist Name" not in seed_df.columns:
                self.log_message.emit("[Enricher] Seed CSV missing 'Artist Name'; aborting.")
                self.finished.emit("")
                return
            seed_df = dedupe_pre_enrich(seed_df)
            dedupe_message = getattr(dedupe_pre_enrich, "_last_log_message", "")
            if dedupe_message:
                self.log_message.emit(dedupe_message)
            self._festival_expansion_existing_keys = {
                normalise_artist_name(_clean_cell(name))
                for name in seed_df.get("Artist Name", pd.Series(dtype=str)).tolist()
                if normalise_artist_name(_clean_cell(name))
            }
            seed_df = _ensure_email_columns(seed_df)
            self.log_message.emit(
                "[Schema] ensured email columns: Email, Email_All, Email_Type, Email_Source_URL, Email_Source_Type, Email_Extract_Method"
            )
            seed_df = _apply_fb_promotion_df(seed_df, log_fn=self.log_message.emit)
            total = len(seed_df.index)
            self.total_rows = total
            if getattr(self, "night_mode", False):
                # Reset per-run SoundCloud cache for deterministic Night Mode attempts.
                self._night_sc_cache = {}
            if total == 0:
                seed_df = self._ensure_bandcamp_output_columns(seed_df)
                _ensure_parent_dir(self.output_csv_path)
                try:
                    seed_df.head(0).to_csv(self.output_csv_path, index=False, encoding="utf-8-sig")
                except Exception:
                    # Defensive fallback: write truly empty file if header write fails.
                    pd.DataFrame().to_csv(self.output_csv_path, index=False, encoding="utf-8-sig")
                self.log_message.emit(
                    f"[Enricher] Seed CSV has no rows; wrote empty output with headers -> {self.output_csv_path}"
                )
                self.finished.emit(self.output_csv_path)
                return
            required_columns = [
                "Social Link",
                "External Links",
                "Email",
                "Source Directory",
                "Source URL",
                "Location",
                "Primary Genre",
                "Release Date",
                "Facebook_URL",
                "Bandcamp_URL",
            ]
            provenance_columns = ("Email_Source_URL", "Email_Source_Type", "Email_Extract_Method")
            # Bandcamp status columns (diagnostic)
            bc_diag_columns = ("BC_Status", "BC_Mode", "BC_Attempts", "BC_403_Count")
            for bc_col in bc_diag_columns:
                if bc_col not in seed_df.columns:
                    seed_df[bc_col] = ""
                seed_df[bc_col] = seed_df[bc_col].fillna("").astype(str)
            match_score_column = "Match_Score"
            for column in required_columns:
                if column not in seed_df.columns:
                    seed_df[column] = ""
                seed_df[column] = seed_df[column].fillna("").astype(str)
            for column in provenance_columns:
                if column not in seed_df.columns:
                    seed_df[column] = ""
                seed_df[column] = seed_df[column].fillna("").astype(str)
            # Canonical Bandcamp column + backfill from existing profile URLs (Bandcamp seeds).
            bandcamp_url_col = "Bandcamp_URL"
            profile_url_col = "Profile URL"
            seed_df[bandcamp_url_col] = seed_df[bandcamp_url_col].apply(_canonicalise_bandcamp_url)
            if profile_url_col in seed_df.columns:
                profile_canon = seed_df[profile_url_col].fillna("").astype(str).apply(_canonicalise_bandcamp_url)
                profile_is_bandcamp = profile_canon.str.contains("bandcamp.com", case=False, na=False)
                needs_bandcamp = seed_df[bandcamp_url_col].fillna("").astype(str).str.strip() == ""
                copy_mask = profile_is_bandcamp & needs_bandcamp
                if copy_mask.any():
                    seed_df.loc[copy_mask, bandcamp_url_col] = profile_canon[copy_mask]
            if getattr(self, "night_mode", False):
                for column in ("SC_Status", "SC_Reason", "SC_Fetches", "SC_ms"):
                    if column not in seed_df.columns:
                        seed_df[column] = ""
                    seed_df[column] = seed_df[column].fillna("")
            # Keep optional link fields as strings to avoid dtype warnings when updating.
            for column in ("SoundCloud Link",):
                if column in seed_df.columns:
                    seed_df[column] = seed_df[column].fillna("").astype(str)
            if match_score_column not in seed_df.columns:
                seed_df[match_score_column] = 0.0
            seed_df[match_score_column] = (
                pd.to_numeric(seed_df[match_score_column], errors="coerce")
                .fillna(0.0)
                .clip(lower=0.0, upper=1.0)
            )
            directory_indexes: Dict[str, DirectoryIndex] = {}
            if self.unearthed_csv_path:
                directory_indexes["unearthed"] = _load_directory_csv(self.unearthed_csv_path, "Unearthed")
            if self.bandcamp_csv_path:
                directory_indexes["bandcamp"] = _load_directory_csv(self.bandcamp_csv_path, "Bandcamp")
            if self.soundcloud_csv_path:
                directory_indexes["soundcloud"] = _load_directory_csv(self.soundcloud_csv_path, "SoundCloud")
            if self.lastfm_csv_path:
                directory_indexes["lastfm"] = _load_directory_csv(self.lastfm_csv_path, "Last.fm")
            self._directory_indexes = directory_indexes
            self.log_message.emit(f"[Enricher] Starting enrichment for {total} rows...")
            self.log_message.emit(
                f"[Enricher] Live search enabled={self.enable_live_search} max={self.max_live_searches}"
            )
            if self.soundcloud_csv_path:
                self.log_message.emit(
                    f"[Enricher] SoundCloud directory path set -> {self.soundcloud_csv_path}"
                )
            priority = ["bandcamp", "soundcloud", "lastfm", "unearthed"]
            _enrichment_mode = os.getenv("ENRICHMENT_MODE", "row_linear")
            self.log_message.emit(f"[Enricher] mode={_enrichment_mode}")
            if _enrichment_mode == "source_phased":
                self._run_source_phased(seed_df, directory_indexes, priority, fb_driver, total)
            else:
                for position, row_idx in enumerate(seed_df.index, start=1):
                    ctx = self._build_row_context(seed_df, row_idx, position, total)
                    if not ctx:
                        self._update_progress(position, total)
                        continue
                    if self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                        self._update_progress(position, total)
                        continue
                    self._init_row_enrichment_state()
                    enriched = self._enrich_row_directories(seed_df, row_idx, directory_indexes, priority, ctx)
                    if self.enable_live_search:
                        sc_enriched, skip_rest = self._enrich_row_sc_live(seed_df, row_idx, ctx)
                        enriched |= sc_enriched
                        if skip_rest:
                            self._update_progress(position, total)
                            continue
                        ll_enriched, skip_rest = self._enrich_row_live_lookup(seed_df, row_idx, ctx)
                        enriched |= ll_enriched
                        if skip_rest:
                            self._update_progress(position, total)
                            continue
                    enriched |= self._enrich_row_instagram_email(seed_df, row_idx, ctx)
                    enriched |= self._enrich_row_website_email(seed_df, row_idx, ctx)
                    if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:
                        enriched |= self._enrich_row_facebook(seed_df, row_idx, fb_driver, ctx)
                    if not enriched:
                        self.log_message.emit(
                            f"[Enricher] Row {position}/{total}: no enrichment for {ctx['artist']!r}."
                        )
                    self._update_progress(position, total)
            # Bandcamp per-run summary (low noise)
            if self._bc_search_attempts:
                summary_parts = [
                    f"attempts={self._bc_search_attempts}",
                    f"matches={self._bc_matches}",
                    f"no_match={self._bc_no_match}",
                    f"403s={self._bc_total_403}",
                ]
                if self._bc_fallback_used:
                    summary_parts.append(f"fallback_used={self._bc_fallback_used}")
                    summary_parts.append(f"fallback_matches={self._bc_fallback_matches}")
                if self._bc_search_breaker_tripped:
                    summary_parts.append(f"breaker=1 reason={self._bc_search_breaker_reason or 'unknown'}")
                self.log_message.emit(f"[Enricher] Bandcamp summary: " + " ".join(summary_parts))
            # SoundCloud RSS-only summary (low noise)
            if getattr(self, "_sc_rss_only_entries_consecutive_403", 0) or getattr(self, "_sc_rss_only_engine_fetch_skips", 0):
                sc_summary_parts = []
                if getattr(self, "_sc_rss_only_entries_consecutive_403", 0):
                    sc_summary_parts.append(f"rss_only_entries_consecutive_403={self._sc_rss_only_entries_consecutive_403}")
                if getattr(self, "_sc_rss_only_engine_fetch_skips", 0):
                    sc_summary_parts.append(f"engine_fetch_skips={self._sc_rss_only_engine_fetch_skips}")
                if sc_summary_parts:
                    self.log_message.emit("[Enricher] SoundCloud summary: " + " ".join(sc_summary_parts))
            self.log_message.emit(
                f"[Enricher] Domain email reuse summary: indexed_domains={len(self._domain_email_reuse_index)} "
                f"rows_reused={self._domain_email_reuse_count}"
            )
            self._write_festival_expansion_sidecar()
            seed_df = self._ensure_bandcamp_output_columns(seed_df)
            _ensure_parent_dir(self.output_csv_path)
            try:
                seed_df.to_csv(self.output_csv_path, index=False, encoding="utf-8-sig")
            except Exception as exc:
                self.log_message.emit(f"[Enricher] Failed to write output CSV: {exc}")
                self.finished.emit("")
                return
            self.log_message.emit(f"[Enricher] Enriched CSV written to {self.output_csv_path}")
            self.finished.emit(self.output_csv_path)
        finally:
            _cleanup_enricher_facebook_driver()

    def _init_row_enrichment_state(self) -> None:
        self._row_enrichment_state = {
            "soundcloud": "pending",
            "bandcamp": "pending",
            "lastfm": "pending",
            "instagram": "pending",
            "website": "pending",
            "facebook": "pending",
        }
        self._sc_blocked_for_row = False
        self._last_bc_row_stats = {}

    def _index_domain_email_reuse(
        self,
        spotify_domain: str,
        *,
        email: str,
        email_all: str = "",
        source_url: str = "",
        source_type: str = "",
        extract_method: str = "",
        email_type: str = "",
        source_label: str = "",
    ) -> bool:
        domain_norm = _clean_cell(spotify_domain).lower()
        if not domain_norm or domain_norm in self._domain_email_reuse_index:
            return False

        source_label_norm = _clean_cell(source_label).lower()
        if source_label_norm.startswith("seed directory"):
            return False

        try:
            from email_normalizer import normalize_email_value
        except Exception:
            normalize_email_value = lambda value: _clean_cell(value).lower()
        try:
            from pipeline_runner import normalize_emails
        except Exception:
            normalize_emails = lambda value: [normalize_email_value(value)] if normalize_email_value(value) else []

        email_norm = normalize_email_value(email)
        if not email_norm or email_norm.split("@", 1)[1] != domain_norm:
            return False

        email_all_norm = ";".join(normalize_emails(email_all)) if email_all else ""
        if not email_all_norm:
            email_all_norm = email_norm

        self._domain_email_reuse_index[domain_norm] = {
            "email": email_norm,
            "email_all": email_all_norm,
            "source_url": _clean_cell(source_url),
            "source_type": _clean_cell(source_type),
            "extract_method": _clean_cell(extract_method) or "regex",
            "email_type": _clean_cell(email_type),
        }
        return True

    def _index_domain_email_reuse_from_row(self, df: pd.DataFrame, row_idx, spotify_domain: str, source_label: str = "") -> bool:
        if df is None or row_idx not in df.index:
            return False
        email_value = _coerce_directory_value(df.at[row_idx, "Email"]) if "Email" in df.columns else ""
        if not email_value:
            return False
        email_all_value = _coerce_directory_value(df.at[row_idx, "Email_All"]) if "Email_All" in df.columns else ""
        source_url = _coerce_directory_value(df.at[row_idx, "Email_Source_URL"]) if "Email_Source_URL" in df.columns else ""
        source_type = _coerce_directory_value(df.at[row_idx, "Email_Source_Type"]) if "Email_Source_Type" in df.columns else ""
        extract_method = _coerce_directory_value(df.at[row_idx, "Email_Extract_Method"]) if "Email_Extract_Method" in df.columns else ""
        email_type = _coerce_directory_value(df.at[row_idx, "Email_Type"]) if "Email_Type" in df.columns else ""
        row_source_label = _coerce_directory_value(df.at[row_idx, "Email Source"]) if "Email Source" in df.columns else ""
        return self._index_domain_email_reuse(
            spotify_domain,
            email=email_value,
            email_all=email_all_value,
            source_url=source_url,
            source_type=source_type,
            extract_method=extract_method,
            email_type=email_type,
            source_label=source_label or row_source_label,
        )

    def _maybe_apply_domain_email_reuse(self, df: pd.DataFrame, row_idx, ctx: Optional[Dict[str, Any]]) -> bool:
        if df is None or row_idx not in df.index or row_idx in self._domain_email_reuse_rows:
            return False
        row = df.loc[row_idx]
        if _row_has_email(row):
            return False
        domain_norm = _clean_cell((ctx or {}).get("spotify_domain", "")).lower()
        if not domain_norm:
            return False
        entry = self._domain_email_reuse_index.get(domain_norm)
        if not entry:
            return False

        email_before = _row_email_summary_snapshot(df, row_idx)
        _set_email_with_provenance(
            (df, row_idx),
            entry.get("email", ""),
            entry.get("source_url", ""),
            entry.get("source_type", ""),
            entry.get("extract_method", "regex") or "regex",
        )
        try:
            from pipeline_runner import _set_email_all, record_email_summary_row_change
        except Exception:
            _set_email_all = None  # type: ignore[assignment]
            record_email_summary_row_change = None  # type: ignore[assignment]

        if callable(_set_email_all):
            _set_email_all(
                df,
                row_idx,
                entry.get("email_all") or entry.get("email", ""),
                source="domain_reuse",
                logger=self.log_message.emit,
            )
        if entry.get("email_type") and not _coerce_directory_value(df.at[row_idx, "Email_Type"]):
            df.at[row_idx, "Email_Type"] = entry.get("email_type", "")
        if callable(record_email_summary_row_change):
            record_email_summary_row_change(email_before, _row_email_summary_snapshot(df, row_idx))
        self._domain_email_reuse_rows.add(row_idx)
        self._domain_email_reuse_count += 1
        return True

    # ------------------------------------------------------------------
    # Extracted per-row helpers (used by both row-linear and source-phased)
    # ------------------------------------------------------------------

    def _build_row_signal_snapshot(
        self,
        row: Any,
        *,
        spotify_domain: str = "",
        seed_links_by_source: Optional[Dict[str, Set[str]]] = None,
    ) -> Dict[str, Any]:
        if row is None:
            return {
                "spotify_domain": _clean_cell(spotify_domain),
                "seed_links_by_source": {},
                "website_candidates": (),
                "soundcloud_link": "",
                "canonical_fb_url": "",
                "source_url": "",
                "source_url_source": "",
                "match_score": 0.0,
                "signal_sources": (),
            }

        source_url_raw = _clean_cell(row.get("Source URL")) if hasattr(row, "get") else ""
        source_url = _normalise_url(source_url_raw) or source_url_raw
        source_url_source = _source_for_url(source_url) or ""
        source_host = _host(source_url)
        if (
            not source_url_source
            and source_host
            and any(source_host.endswith(domain) for domain in SOCIAL_DOMAINS.get("facebook", ()))
        ):
            source_url_source = "facebook"
        website_candidates = tuple(_collect_website_enrich_candidate_urls(row))
        soundcloud_raw = _clean_cell(row.get("SoundCloud Link")) if hasattr(row, "get") else ""
        if not soundcloud_raw and hasattr(row, "get"):
            soundcloud_raw = _clean_cell(row.get("SoundCloud URL"))
        soundcloud_link = _normalise_url(soundcloud_raw) or soundcloud_raw
        canonical_fb_url = _get_canonical_fb_url(row)
        links_by_source = seed_links_by_source or _extract_seed_links_by_source(row)
        signal_sources: Set[str] = {source for source, urls in links_by_source.items() if urls}
        if canonical_fb_url:
            signal_sources.add("facebook")
        if website_candidates:
            signal_sources.add("website")
        if soundcloud_link:
            signal_sources.add("soundcloud")
        if source_url_source:
            signal_sources.add(f"source:{source_url_source}")
        elif source_url:
            signal_sources.add("source_url")
        if spotify_domain:
            signal_sources.add("spotify_domain")
        social_raw = _clean_cell(row.get("Social Link")) if hasattr(row, "get") else ""
        external_raw = _clean_cell(row.get("External Links")) if hasattr(row, "get") else ""
        if social_raw:
            signal_sources.add("social_field")
        if external_raw:
            signal_sources.add("external_field")
        try:
            match_score = float(_clean_cell(row.get("Match_Score"))) if hasattr(row, "get") else 0.0
            if math.isnan(match_score):
                match_score = 0.0
        except Exception:
            match_score = 0.0
        return {
            "spotify_domain": _clean_cell(spotify_domain),
            "seed_links_by_source": links_by_source,
            "website_candidates": website_candidates,
            "soundcloud_link": soundcloud_link,
            "canonical_fb_url": canonical_fb_url,
            "source_url": source_url,
            "source_url_source": source_url_source,
            "match_score": max(0.0, min(match_score, 1.0)),
            "signal_sources": tuple(sorted(signal_sources)),
        }

    def _compute_row_confidence(
        self,
        row: Optional[Any],
        ctx: Optional[Dict[str, Any]],
        target: str,
    ) -> HeavyEnricherGateDecision:
        target_key = _clean_cell(target).strip().lower()
        live_ctx = getattr(self, "_live_context", {}) or {}
        working_ctx = ctx or live_ctx
        signal_snapshot = dict(working_ctx.get("signal_snapshot") or {})
        if not signal_snapshot and row is not None:
            signal_snapshot = self._build_row_signal_snapshot(
                row,
                spotify_domain=_clean_cell(working_ctx.get("spotify_domain", "")),
                seed_links_by_source=working_ctx.get("seed_links_by_source"),
            )
        if not signal_snapshot:
            return HeavyEnricherGateDecision(
                allowed=True,
                score=HEAVY_ENRICHER_CONFIDENCE_THRESHOLD,
                threshold=HEAVY_ENRICHER_CONFIDENCE_THRESHOLD,
                reasons=("missing_signal_snapshot",),
            )

        score = 0.0
        reasons: List[str] = []

        def _add(points: float, reason: str) -> None:
            nonlocal score
            if not reason or reason in reasons:
                return
            score += float(points)
            reasons.append(reason)

        website_candidates = tuple(signal_snapshot.get("website_candidates") or ())
        seed_links_by_source = signal_snapshot.get("seed_links_by_source") or {}
        spotify_domain = _clean_cell(signal_snapshot.get("spotify_domain", ""))
        source_url = _clean_cell(signal_snapshot.get("source_url", ""))
        source_url_source = _clean_cell(signal_snapshot.get("source_url_source", "")).lower()
        canonical_fb_url = _clean_cell(signal_snapshot.get("canonical_fb_url", ""))
        soundcloud_link = _clean_cell(signal_snapshot.get("soundcloud_link", ""))
        match_score = float(signal_snapshot.get("match_score") or 0.0)
        signal_sources = tuple(signal_snapshot.get("signal_sources") or ())
        signal_count = len(set(signal_sources))
        artist_name = _clean_cell(working_ctx.get("artist", ""))
        if not artist_name and row is not None and hasattr(row, "get"):
            artist_name = _clean_cell(row.get("Artist Name"))
        artist_tokens = [token for token in re.split(r"\s+", artist_name) if token]
        artist_alnum_len = sum(1 for ch in artist_name if ch.isalnum())

        if target_key == "website":
            if website_candidates:
                _add(0.8, "website_url")
            if source_url and any(_website_domains_match(source_url, candidate) for candidate in website_candidates):
                _add(0.45, "source_url_domain_match")
            spotify_match_url = f"https://{spotify_domain}" if spotify_domain else ""
            if spotify_match_url and (
                any(_website_domains_match(candidate, spotify_match_url) for candidate in website_candidates)
                or (source_url and _website_domains_match(source_url, spotify_match_url))
            ):
                _add(0.45, "spotify_domain_match")
        elif target_key == "facebook":
            if canonical_fb_url:
                _add(0.8, "explicit_facebook_url")
            elif source_url_source == "facebook":
                _add(0.55, "facebook_source_url")
        elif target_key == "soundcloud":
            if soundcloud_link or seed_links_by_source.get("soundcloud"):
                _add(0.8, "explicit_soundcloud_link")
            elif source_url_source == "soundcloud":
                _add(0.55, "soundcloud_source_url")
        elif target_key == "lastfm":
            if seed_links_by_source.get("lastfm"):
                _add(0.8, "explicit_lastfm_url")
            elif source_url_source == "lastfm":
                _add(0.55, "lastfm_source_url")

        if match_score >= 0.7:
            _add(0.6, "strong_match_score")
        elif match_score >= 0.45:
            _add(0.35, "match_score")

        if signal_count == 0 and len(artist_tokens) >= 2 and artist_alnum_len >= 6:
            _add(0.6, "descriptive_artist_name")

        if signal_count >= 3:
            _add(0.55, "multiple_link_clues")
        elif signal_count >= 2:
            _add(0.3, "some_link_clues")

        if spotify_domain:
            _add(0.15, "spotify_domain")
        if target_key != "website" and website_candidates:
            _add(0.2, "website_clue")

        threshold = HEAVY_ENRICHER_CONFIDENCE_THRESHOLD
        final_score = round(max(0.0, min(score, 1.0)), 2)
        return HeavyEnricherGateDecision(
            allowed=final_score >= threshold,
            score=final_score,
            threshold=threshold,
            reasons=tuple(reasons),
        )

    def _row_has_website_domain_hard_allow(
        self,
        row: Optional[Any],
        ctx: Optional[Dict[str, Any]],
    ) -> bool:
        def _valid_website_candidates(values: Iterable[Any]) -> bool:
            for value in values or ():
                normalised = _normalise_url(_clean_cell(value))
                if not normalised:
                    continue
                domain = _website_cache_key(normalised)
                host = _host(normalised)
                if not domain or not host:
                    continue
                allow_platform = bool(host.endswith("bandcamp.com"))
                if _is_website_enrich_candidate_url(normalised, allow_platform=allow_platform):
                    return True
            return False

        live_row = row
        if live_row is None:
            live_df = getattr(self, "_live_seed_df", None)
            live_row_idx = getattr(self, "_live_row_idx", None)
            if live_df is not None and live_row_idx in getattr(live_df, "index", ()):
                try:
                    live_row = live_df.loc[live_row_idx]
                except Exception:
                    live_row = None
        if live_row is not None and _valid_website_candidates(_collect_website_enrich_candidate_urls(live_row)):
            return True

        working_ctx = ctx or getattr(self, "_live_context", {}) or {}
        signal_snapshot = dict(working_ctx.get("signal_snapshot") or {})
        if _valid_website_candidates(signal_snapshot.get("website_candidates") or ()):
            return True
        return False

    def _row_allows_heavy_enricher(
        self,
        row: Optional[Any],
        ctx: Optional[Dict[str, Any]],
        target: str,
    ) -> HeavyEnricherGateDecision:
        threshold = HEAVY_ENRICHER_CONFIDENCE_THRESHOLD
        if self._row_has_website_domain_hard_allow(row, ctx):
            return HeavyEnricherGateDecision(
                allowed=True,
                score=1.0,
                threshold=threshold,
                reasons=("website_domain",),
            )
        return self._compute_row_confidence(row, ctx, target)

    def _log_low_confidence_skip(
        self,
        target_label: str,
        artist: str,
        decision: HeavyEnricherGateDecision,
    ) -> None:
        reasons = ",".join(decision.reasons) if decision.reasons else "none"
        self.log_message.emit(
            f"[Enricher] skipping {target_label} heavy enrichment for '{artist}' due to low row confidence "
            f"(score={decision.score:.2f} threshold={decision.threshold:.2f} reasons={reasons})"
        )

    def _build_row_context(self, seed_df, row_idx, position, total):
        """Build per-row context dict and set self._live_context.

        Returns a dict with row metadata, or None if the artist name is invalid.
        Does NOT call _init_row_enrichment_state — callers must do that.
        """
        row = seed_df.loc[row_idx]
        self._live_seed_df = seed_df
        self._live_row_idx = row_idx
        had_email_from_seed = _row_has_email(row)
        artist = _clean_cell(row.get("Artist Name"))
        key = normalise_artist_name(artist)
        if not key:
            self.log_message.emit(
                f"[Enricher] Row {position}/{total}: invalid artist name; skipping."
            )
            return None
        track_key = _extract_seed_track_key(row)
        seed_song_title = _extract_seed_track_text(row)
        spotify_domain = extract_domain(_clean_cell(row.get("Spotify_Website_URL", "")))
        seed_links_by_source = _extract_seed_links_by_source(row)
        signal_snapshot = self._build_row_signal_snapshot(
            row,
            spotify_domain=spotify_domain,
            seed_links_by_source=seed_links_by_source,
        )
        self._live_context = {
            "artist": artist,
            "location": _clean_cell(row.get("Location")),
            "track": track_key,
            "genre": _coerce_directory_value(row.get("Primary Genre")) if "Primary Genre" in row else "",
            "song_title": seed_song_title,
            "spotify_domain": spotify_domain,
            "spotify_id": _clean_cell(row.get("Spotify_Artist_ID")),
            "seed_lastfm_urls": seed_links_by_source.get("lastfm", set()),
            "signal_snapshot": signal_snapshot,
        }
        spotify_id = self._live_context.get("spotify_id", "")
        return {
            "artist": artist,
            "key": key,
            "track_key": track_key,
            "spotify_domain": spotify_domain,
            "spotify_id": spotify_id,
            "seed_links_by_source": seed_links_by_source,
            "had_email_from_seed": had_email_from_seed,
            "position": position,
            "total": total,
            "signal_snapshot": signal_snapshot,
        }

    def _enrich_row_directories(self, seed_df, row_idx, directory_indexes, priority, ctx):
        """Directory matching for a single row. Returns True if any enrichment applied."""
        artist = ctx["artist"]
        key = ctx["key"]
        track_key = ctx["track_key"]
        spotify_id = ctx["spotify_id"]
        seed_links_by_source = ctx["seed_links_by_source"]
        position = ctx["position"]
        total = ctx["total"]
        enriched = False
        matches_used: List[Tuple[str, Dict[str, Any]]] = []
        sources_logged: List[str] = []
        for source in priority:
            directory_index = directory_indexes.get(source)
            if not directory_index:
                continue
            url_candidates = list(seed_links_by_source.get(source, ()))
            matches = self._find_directory_matches(
                directory_index, key, track_key, url_candidates
            )
            if not matches:
                continue
            payload, best_row = self._payload_from_directory_matches(
                matches, source
            )
            if not payload:
                continue
            applied = self._apply_payload_guarded(
                seed_df, row_idx, payload, artist, spotify_id=spotify_id
            )
            if applied:
                enriched = True
                if best_row:
                    matches_used.append((source, best_row))
                if source not in sources_logged:
                    sources_logged.append(source)
        metadata_updated = self._apply_structured_fields(seed_df, row_idx, matches_used)
        if metadata_updated:
            enriched = True
            for source, _ in matches_used:
                if source not in sources_logged:
                    sources_logged.append(source)
        if enriched and sources_logged:
            display_sources = ", ".join(
                filter(
                    None,
                    [_format_source_display(src) or src.title() for src in sources_logged],
                )
            )
            if display_sources:
                self.log_message.emit(
                    f"[Enricher] Row {position}/{total}: matched {artist!r} via {display_sources}."
                )
        return enriched

    def _enrich_row_sc_live(self, seed_df, row_idx, ctx):
        """Dedicated SoundCloud live check for a single row.

        Returns (enriched: bool, skip_rest: bool).
        skip_rest=True means the SC blocked flag fired and the caller should
        skip remaining enrichment for this row.
        """
        artist = ctx["artist"]
        spotify_id = ctx["spotify_id"]
        decision = self._row_allows_heavy_enricher(seed_df.loc[row_idx], ctx, "soundcloud")
        if not decision.allowed:
            self._log_low_confidence_skip("soundcloud", artist, decision)
            self._set_platform_state("soundcloud", "skipped")
            return (False, False)
        if not _coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"]):
            if getattr(self, "_sc_live_enrich_disabled", False):
                reason = self._sc_live_enrich_disabled_reason or "first_challenge_page"
                self.log_message.emit(
                    f"[Enricher][SC] Live enrichment disabled (reason={reason}); skipping live SC check for '{artist}'."
                )
            else:
                self.log_message.emit(
                    f"[Enricher] SoundCloud live check for '{artist}' (current SC link missing)."
                )
                if getattr(self, "night_mode", False):
                    sc_applied = self._night_sc_attempt_row(seed_df, row_idx, artist, spotify_id=spotify_id)
                    if "SC_Status" in seed_df.columns or "SC_Reason" in seed_df.columns:
                        sc_status = _coerce_directory_value(seed_df.at[row_idx, "SC_Status"]) if "SC_Status" in seed_df.columns else ""
                        sc_reason = _coerce_directory_value(seed_df.at[row_idx, "SC_Reason"]) if "SC_Reason" in seed_df.columns else ""
                        self._note_sc_challenge(sc_status, sc_reason)
                    if sc_applied:
                        return (True, False)
                else:
                    sc_payload = self._live_search_soundcloud(artist)
                    if self._mark_sc_blocked_row(seed_df, row_idx):
                        return (False, True)
                    if sc_payload:
                        applied = self._apply_payload_guarded(
                            seed_df, row_idx, sc_payload, artist, spotify_id=spotify_id
                        )
                        if applied:
                            return (True, False)
        return (False, False)

    def _enrich_row_live_lookup(self, seed_df, row_idx, ctx, *, skip_lastfm: bool = False):
        """General live lookup (BC + SC + LF) for a single row.

        Returns (enriched: bool, skip_rest: bool).
        skip_rest=True means SC blocked and the caller should skip remaining
        enrichment for this row.
        """
        artist = ctx["artist"]
        spotify_id = ctx["spotify_id"]
        enriched = False
        bandcamp_url_present = False
        if "Bandcamp_URL" in seed_df.columns:
            bandcamp_url_present = bool(_coerce_directory_value(seed_df.at[row_idx, "Bandcamp_URL"]))
        skip_soundcloud = bool(_coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"]))
        if getattr(self, "_sc_live_enrich_disabled", False):
            skip_soundcloud = True
        if skip_lastfm and getattr(self, "_live_lookup_bclf_adaptive_enabled", False):
            self._record_live_lookup_bclf_cooldown("lastfm")
        payload = self._live_lookup(
            artist,
            skip_soundcloud=skip_soundcloud,
            skip_bandcamp=bandcamp_url_present,
            skip_lastfm=skip_lastfm,
        )
        if not getattr(self, "night_mode", False) and self._mark_sc_blocked_row(seed_df, row_idx):
            return (False, True)
        if payload:
            applied = self._apply_payload_guarded(
                seed_df, row_idx, payload, artist, spotify_id=spotify_id
            )
            if applied:
                enriched = True
            if getattr(self, "_live_lookup_bclf_adaptive_enabled", False):
                self._record_live_lookup_bclf_applied_winner(payload, applied=applied)
        # Persist Bandcamp diagnostics (per-row)
        bc_stats = getattr(self, "_last_bc_row_stats", {}) or {}
        if bc_stats:
            if "BC_Status" in seed_df.columns:
                seed_df.at[row_idx, "BC_Status"] = bc_stats.get("status", "")
            if "BC_Mode" in seed_df.columns:
                seed_df.at[row_idx, "BC_Mode"] = bc_stats.get("mode", "")
            if "BC_Attempts" in seed_df.columns:
                seed_df.at[row_idx, "BC_Attempts"] = bc_stats.get("attempts", "")
            if "BC_403_Count" in seed_df.columns:
                seed_df.at[row_idx, "BC_403_Count"] = bc_stats.get("http_403", "")
        return (enriched, False)

    def _enrich_row_instagram_email(self, seed_df, row_idx, ctx):
        """Extract email from the canonical Instagram profile HTML in a single fetch."""
        row = seed_df.loc[row_idx]
        if _row_has_email(row):
            self._set_platform_state("instagram", "skipped")
            return False
        ig_url = _get_canonical_instagram_url(row)
        if not ig_url:
            self._set_platform_state("instagram", "skipped")
            return False

        email_before = _row_email_summary_snapshot(seed_df, row_idx)
        self.log_message.emit(f"[IG Email] Visiting {ig_url}")
        html, status = _fetch_instagram_profile_html(self.session, ig_url)
        if status != 200 or not html:
            self.log_message.emit("[IG Email] No email found")
            self._set_platform_state("instagram", "skipped")
            return False

        soup = BeautifulSoup(html, "html.parser")
        ig_emails, _ = _extract_emails_from_html(html, soup=soup)

        meta_emails: List[str] = []
        for meta_tag in soup.select('meta[property="og:description"], meta[name="description"]'):
            meta_content = cell_to_str(meta_tag.get("content"))
            if not meta_content:
                continue
            extracted_meta_emails, _ = _extract_emails_from_html(meta_content)
            meta_emails.extend(extracted_meta_emails)

        all_ig_emails = list(dict.fromkeys([*ig_emails, *meta_emails]))
        if not all_ig_emails:
            self.log_message.emit("[IG Email] No email found")
            self._set_platform_state("instagram", "skipped")
            return False

        found_email = all_ig_emails[0]
        self.log_message.emit(f"[IG Email] Found email: {found_email}")
        if not cell_to_str(seed_df.at[row_idx, "Email"]):
            seed_df.at[row_idx, "Email"] = found_email
        seed_df.at[row_idx, "Email_All"] = _merge_email_all(seed_df.at[row_idx, "Email_All"], all_ig_emails)
        seed_df.at[row_idx, "Email_Type"] = "ig_enrich"
        if not cell_to_str(seed_df.at[row_idx, "Email_Source_URL"]):
            seed_df.at[row_idx, "Email_Source_URL"] = ig_url
        if not cell_to_str(seed_df.at[row_idx, "Email_Source_Type"]):
            seed_df.at[row_idx, "Email_Source_Type"] = "instagram_enrich"
        if not cell_to_str(seed_df.at[row_idx, "Email_Extract_Method"]):
            seed_df.at[row_idx, "Email_Extract_Method"] = "regex"
        try:
            from pipeline_runner import record_email_summary_row_change

            record_email_summary_row_change(
                email_before,
                _row_email_summary_snapshot(seed_df, row_idx),
            )
        except Exception:
            pass
        self._index_domain_email_reuse_from_row(
            seed_df,
            row_idx,
            _clean_cell(ctx.get("spotify_domain", "")),
        )
        self._set_platform_state("instagram", "matched")
        return True

    def _enrich_row_website_email(self, seed_df, row_idx, ctx):
        """Fetch a bounded set of same-domain contact pages from canonical row website fields."""
        try:
            from pipeline_runner import normalize_emails as _normalize_emails
        except Exception:
            _normalize_emails = lambda value: [normalize_email_value(value)] if normalize_email_value(value) else []
        row = seed_df.loc[row_idx]
        if _row_has_email(row):
            self._set_platform_state("website", "skipped")
            return False

        website_candidates = _collect_website_enrich_candidate_urls(row)
        if not website_candidates:
            self._set_platform_state("website", "skipped")
            return False

        artist = ctx.get("artist") or _clean_cell(row.get("Artist Name")) or "<unknown>"
        decision = self._row_allows_heavy_enricher(row, ctx, "website")
        if not decision.allowed:
            self._log_low_confidence_skip("website", artist, decision)
            self._set_platform_state("website", "skipped")
            return False
        email_before = _row_email_summary_snapshot(seed_df, row_idx)
        pages_fetched = 0
        selected_emails: List[str] = []
        selected_source_url = ""
        selected_extract_method = "regex"
        selected_domain = ""
        fetched_uncached_domain = False

        for website_url in website_candidates:
            website_domain = _website_cache_key(website_url)
            if not website_domain:
                continue

            cached_result = self._website_email_cache.get(website_domain)
            if cached_result is not None:
                cached_emails = list(cached_result.get("emails") or [])
                self.log_message.emit(
                    f"[Web] cache hit domain={website_domain} status={cached_result.get('status', 'miss')} artist='{artist}'"
                )
                if cached_emails:
                    selected_emails = _normalize_emails(";".join(cached_emails))
                    selected_source_url = cell_to_str(cached_result.get("source_url"))
                    selected_extract_method = cell_to_str(cached_result.get("extract_method")) or "regex"
                    selected_domain = website_domain
                    break
                continue

            if fetched_uncached_domain:
                self.log_message.emit(
                    f"[Web] skipping uncached candidate domain={website_domain} artist='{artist}' after prior website fetch"
                )
                continue

            fetched_uncached_domain = True
            max_fetches = max(1, WEBSITE_EMAIL_MAX_PAGES)
            domain_fetches_used = 0
            emails_found: List[str] = []
            source_url = ""
            extract_method = "regex"

            def _fetch(target_url: str) -> WebsiteFetchResult:
                nonlocal domain_fetches_used, pages_fetched
                domain_fetches_used += 1
                pages_fetched += 1
                return _fetch_website_html_bounded(
                    self.session,
                    target_url,
                    timeout_s=WEBSITE_EMAIL_TIMEOUT,
                    max_bytes=WEBSITE_EMAIL_MAX_BYTES,
                )

            homepage = _fetch(website_url)
            homepage_url = homepage.final_url or website_url
            homepage_ok = bool(
                homepage.is_html
                and homepage.html
                and _website_fetch_result_is_same_domain(homepage, website_url)
            )
            self.log_message.emit(
                f"[Web] homepage fetched ok={homepage_ok} artist='{artist}' url={homepage_url}"
            )

            if homepage_ok:
                page_emails, used_mailto = _extract_website_emails_from_html(homepage.html)
                if page_emails:
                    emails_found = page_emails
                    source_url = homepage_url
                    extract_method = "mailto" if used_mailto else "regex"

            remaining_budget = max(0, max_fetches - domain_fetches_used)
            candidates: List[str] = []
            if not emails_found and remaining_budget > 0 and homepage_ok:
                candidates = _discover_website_contact_candidates(homepage_url, homepage.html)
                self.log_message.emit(
                    f"[Web] found contact candidates={len(candidates)} using top={min(len(candidates), remaining_budget)} artist='{artist}'"
                )
                for candidate_url in candidates[:remaining_budget]:
                    result = _fetch(candidate_url)
                    if not result.is_html or not result.html:
                        continue
                    if not _website_fetch_result_is_same_domain(result, website_url):
                        continue
                    page_emails, used_mailto = _extract_website_emails_from_html(result.html)
                    if not page_emails:
                        continue
                    if not source_url:
                        source_url = result.final_url or candidate_url
                        extract_method = "mailto" if used_mailto else "regex"
                    emails_found = list(dict.fromkeys([*emails_found, *page_emails]))

            remaining_budget = max(0, max_fetches - domain_fetches_used)
            if not emails_found and remaining_budget > 0:
                shallow_candidates = _build_website_shallow_candidates(website_url, homepage_url)
                if candidates:
                    seen_candidates = set(candidates)
                    shallow_candidates = [url for url in shallow_candidates if url not in seen_candidates]
                shallow_to_try = shallow_candidates[:remaining_budget]
                self.log_message.emit(
                    f"[Web] shallow sweep paths_considered={len(shallow_candidates)} using_top={len(shallow_to_try)}"
                )
                shallow_fetches = 0
                shallow_emails_found = 0
                for candidate_url in shallow_to_try:
                    result = _fetch(candidate_url)
                    shallow_fetches += 1
                    if not result.is_html or not result.html:
                        continue
                    if not _website_fetch_result_is_same_domain(result, website_url):
                        continue
                    page_emails, used_mailto = _extract_website_emails_from_html(result.html)
                    if not page_emails:
                        continue
                    if not source_url:
                        source_url = result.final_url or candidate_url
                        extract_method = "mailto" if used_mailto else "regex"
                    emails_found = list(dict.fromkeys([*emails_found, *page_emails]))
                    shallow_emails_found = len(page_emails)
                    matched_path = urllib.parse.urlparse(result.final_url or candidate_url).path or "/"
                    self.log_message.emit(f"[Web] shallow sweep matched path={matched_path}")
                    break
                self.log_message.emit(
                    f"[Web] shallow sweep fetched={shallow_fetches} emails_found={shallow_emails_found}"
                )

            normalized_emails = _normalize_emails(";".join(emails_found))
            cache_entry = {
                "status": "hit" if normalized_emails else "miss",
                "emails": list(normalized_emails),
                "source_url": source_url,
                "extract_method": extract_method if normalized_emails else "",
            }
            self._website_email_cache[website_domain] = cache_entry
            if normalized_emails:
                selected_emails = normalized_emails
                selected_source_url = source_url
                selected_extract_method = extract_method
                selected_domain = website_domain
                break

        normalized_emails = list(selected_emails)
        self.log_message.emit(
            f"[Web] emails_found={len(normalized_emails)} pages_fetched={pages_fetched} artist='{artist}'"
        )
        if not normalized_emails:
            self._set_platform_state("website", "skipped")
            return False

        primary_email = normalized_emails[0]
        if not cell_to_str(seed_df.at[row_idx, "Email"]):
            _set_email_with_provenance(
                (seed_df, row_idx),
                primary_email,
                source_url=selected_source_url,
                source_type="website_enrich",
                method=selected_extract_method,
            )
        try:
            from pipeline_runner import _set_email_all, record_email_summary_row_change

            _set_email_all(
                seed_df,
                row_idx,
                normalized_emails,
                source="website_enrich",
                logger=self.log_message.emit,
            )
            record_email_summary_row_change(
                email_before,
                _row_email_summary_snapshot(seed_df, row_idx),
            )
        except Exception:
            seed_df.at[row_idx, "Email_All"] = ";".join(normalized_emails)
        if "Email_Type" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Email_Type"]):
            seed_df.at[row_idx, "Email_Type"] = "website_enrich"
        if "Email_Source_URL" in seed_df.columns and selected_source_url and not cell_to_str(seed_df.at[row_idx, "Email_Source_URL"]):
            seed_df.at[row_idx, "Email_Source_URL"] = selected_source_url
        if "Email_Source_Type" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Email_Source_Type"]):
            seed_df.at[row_idx, "Email_Source_Type"] = "website_enrich"
        if "Email_Extract_Method" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Email_Extract_Method"]):
            seed_df.at[row_idx, "Email_Extract_Method"] = selected_extract_method
        self._index_domain_email_reuse_from_row(
            seed_df,
            row_idx,
            selected_domain or _clean_cell(ctx.get("spotify_domain", "")),
        )
        self._set_platform_state("website", "matched")
        return True

    def _enrich_row_facebook(self, seed_df, row_idx, fb_driver, ctx):
        """Facebook enrichment for a single row. Returns True if enrichment applied."""
        artist = ctx["artist"]
        position = ctx["position"]
        total = ctx["total"]
        had_email_from_seed = ctx.get("had_email_from_seed") or ctx.get("had_fb_or_email_from_seed")
        email_before = _row_email_summary_snapshot(seed_df, row_idx)
        fb_attempted = False
        fb_matched = False
        if not self._platform_attempt_allowed("facebook", artist, "Facebook Enrich"):
            fb_attempted = False
        else:
            fb_attempted = True
            has_email_after_directories = _row_has_email(seed_df.loc[row_idx])
            if had_email_from_seed or has_email_after_directories:
                self.log_message.emit(
                    f"[FB Enrich] Skipping Facebook enrichment for '{artist}' (already has email from seed or directory enrichment)."
                )
            else:
                for col in ("facebook_url", "Facebook_URL", "Facebook URL"):
                    if col not in seed_df.columns:
                        seed_df[col] = ""
                row = seed_df.loc[row_idx]
                promoted_fb = promote_facebook_url(row, set_row=False)
                promoted_norm = _canonicalize_fb_url(promoted_fb)
                if promoted_norm:
                    if not cell_to_str(seed_df.at[row_idx, "facebook_url"]):
                        seed_df.at[row_idx, "facebook_url"] = promoted_norm
                    if "Facebook_URL" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Facebook_URL"]):
                        seed_df.at[row_idx, "Facebook_URL"] = promoted_norm
                    elif "Facebook URL" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Facebook URL"]):
                        seed_df.at[row_idx, "Facebook URL"] = promoted_norm
                fb_url_val = _get_canonical_fb_url(seed_df.loc[row_idx])
                existing_fb_links: List[str] = []
                if fb_url_val:
                    parts = [part.strip() for part in str(fb_url_val).split(",") if part.strip()]
                    for part in parts:
                        if "facebook.com" in part.lower():
                            normalised = _normalise_fb_url(normalize_external_url(part))
                            if normalised:
                                existing_fb_links.append(normalised)
                decision = self._row_allows_heavy_enricher(seed_df.loc[row_idx], ctx, "facebook")
                if not decision.allowed:
                    self._log_low_confidence_skip("fb", artist, decision)
                    self._set_platform_state("facebook", "skipped")
                    return False

                if not existing_fb_links:
                    if row_idx in getattr(self, "_fb_discovery_attempted_rows", set()):
                        self.log_message.emit(
                            f"[FB Discover] Skipping discovery for '{artist}' (already attempted this run)"
                        )
                        if "FB_Status" not in seed_df.columns:
                            seed_df["FB_Status"] = ""
                        seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or "no_fb_url"
                    else:
                        intake = classify_explicit_fb_intake(seed_df.loc[row_idx].to_dict())
                        source_summary = ",".join(intake.source_fields[:2]) if intake.source_fields else "<none>"
                        sample = ""
                        if intake.accepted_urls:
                            sample = intake.accepted_urls[0]
                        elif intake.rejected_invalid:
                            sample = intake.rejected_invalid[0]
                        elif intake.rejected_guard:
                            sample = intake.rejected_guard[0]
                        location = cell_to_str(seed_df.at[row_idx, "Location"]) if "Location" in seed_df.columns else ""
                        self.log_message.emit(
                            f"[FB Discover] No explicit facebook url for '{artist}'; attempting bounded discovery "
                            f"(explicit FB intake outcome='{intake.outcome}' source='{source_summary}' sample='{sample}')."
                        )
                        self._fb_discovery_attempted_rows.add(row_idx)
                        discovered_fb_url = _discover_facebook_url_bounded(
                            fb_driver, artist, location, self.log_message.emit
                        )
                        if discovered_fb_url:
                            self.log_message.emit(
                                f"[FB Discover] Candidate accepted for '{artist}': {discovered_fb_url}"
                            )
                            for col in ("facebook_url", "Facebook_URL", "Facebook URL"):
                                if col in seed_df.columns and not cell_to_str(seed_df.at[row_idx, col]):
                                    seed_df.at[row_idx, col] = discovered_fb_url
                            self.log_message.emit(
                                f"[FB Discover] Canonical facebook_url populated via discovery for '{artist}'"
                            )
                            existing_fb_links = [discovered_fb_url]
                        else:
                            self.log_message.emit(f"[FB Discover] No safe candidate found for '{artist}'")
                            self.log_message.emit(
                                f"[FB Discover] Discovery failed for '{artist}'; locking FB discovery for this run"
                            )
                            if "FB_Status" not in seed_df.columns:
                                seed_df["FB_Status"] = ""
                            seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or "no_fb_url"
                if existing_fb_links:
                    fb_emails: List[str] = []
                    page_url_used = ""
                    fb_status_reason = ""

                    try:
                        for candidate in existing_fb_links:
                            fb_emails, resolved_url, fb_status_reason = _extract_fb_emails_bounded(
                                fb_driver, candidate, log_fn=self.log_message.emit
                            )
                            page_url_used = resolved_url or candidate
                            if fb_emails:
                                break
                            if fb_status_reason in {"login_wall", "warning_interstitial", "checkpoint"}:
                                break
                    except Exception as exc:  # pragma: no cover - defensive
                        self.log_message.emit(
                            f"[FB Enrich] Error enriching row {position}/{total} ({artist}): {exc}"
                        )
                    if "FB_Status" not in seed_df.columns:
                        seed_df["FB_Status"] = ""
                    if fb_emails:
                        fb_status_val = str(seed_df.at[row_idx, "FB_Status"] or "")
                        if _fb_status_is_rejected(fb_status_val):
                            artist_label = cell_to_str(seed_df.at[row_idx, "Artist Name"]) or "<unknown>"
                            page_label = page_url_used or (existing_fb_links[0] if existing_fb_links else "<unknown>")
                            self.log_message.emit(
                                f"[FB Guard] Discarding emails from rejected FB page '{page_label}' for '{artist_label}' (reason={fb_status_val})"
                            )
                        else:
                            current_email = cell_to_str(seed_df.at[row_idx, "Email"])
                            if not current_email:
                                seed_df.at[row_idx, "Email"] = fb_emails[0]
                            if page_url_used and not cell_to_str(seed_df.at[row_idx, "Social Link"]):
                                seed_df.at[row_idx, "Social Link"] = page_url_used
                            if page_url_used and "facebook_url" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "facebook_url"]):
                                seed_df.at[row_idx, "facebook_url"] = page_url_used
                            if page_url_used and not cell_to_str(seed_df.at[row_idx, "Facebook_URL"]):
                                seed_df.at[row_idx, "Facebook_URL"] = page_url_used
                            seed_df.at[row_idx, "Email_All"] = _merge_email_all(
                                seed_df.at[row_idx, "Email_All"], fb_emails
                            )
                            seed_df.at[row_idx, "Email_Type"] = "fb_enrich"
                            if not cell_to_str(seed_df.at[row_idx, "Email_Source_URL"]):
                                seed_df.at[row_idx, "Email_Source_URL"] = page_url_used or ""
                            if not cell_to_str(seed_df.at[row_idx, "Email_Source_Type"]):
                                seed_df.at[row_idx, "Email_Source_Type"] = "facebook_enrich"
                            if not cell_to_str(seed_df.at[row_idx, "Email_Extract_Method"]):
                                seed_df.at[row_idx, "Email_Extract_Method"] = "regex"
                            seed_df.at[row_idx, "__fb_emails_applied"] = ";".join(
                                sorted({e.strip().lower() for e in fb_emails if e})
                            )
                            seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or "found_email"
                            try:
                                from pipeline_runner import record_email_summary_row_change

                                record_email_summary_row_change(
                                    email_before,
                                    _row_email_summary_snapshot(seed_df, row_idx),
                                )
                            except Exception:
                                pass
                            self._index_domain_email_reuse_from_row(
                                seed_df,
                                row_idx,
                                _clean_cell(ctx.get("spotify_domain", "")),
                            )
                            fb_matched = True
                    else:
                        fallback_status = fb_status_reason or "no_email_on_page"
                        seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or fallback_status
        if fb_attempted and not fb_matched:
            self._set_platform_state("facebook", "skipped")
        elif fb_matched:
            self._set_platform_state("facebook", "matched")
        return fb_matched


    # ------------------------------------------------------------------
    # Source-phased orchestration (ENRICHMENT_MODE=source_phased)
    # ------------------------------------------------------------------

    def _sc_row_retry_after(self, base_until: Optional[float], *, retry_count: int, ordinal: int) -> float:
        """
        Return a small staggered retry timestamp so deferred rows do not all
        re-enter immediately when the breaker window lifts.
        """
        base = float(base_until or time.time())
        retry_idx = max(0, int(retry_count))
        slot = max(0, int(ordinal)) % 5
        stagger = SC_COOLDOWN_ROW_RETRY_JITTER_S * float(slot + retry_idx + 1)
        return base + stagger

    def _sc_defer_row_for_cooldown(
        self,
        seed_df,
        row_idx,
        *,
        position: int,
        total: int,
        deferred_rows: Dict[Any, Dict[str, Any]],
        retry_count: int = 0,
    ) -> None:
        cooldown_end = float(getattr(self, "_sc_live_disabled_until", 0.0) or time.time())
        retry_after = self._sc_row_retry_after(
            cooldown_end,
            retry_count=retry_count,
            ordinal=len(deferred_rows),
        )
        state = deferred_rows.get(row_idx)
        if state:
            state["retry_after"] = max(float(state.get("retry_after", 0.0) or 0.0), retry_after)
            state["cooldown_end"] = max(float(state.get("cooldown_end", 0.0) or 0.0), cooldown_end)
            state["position"] = position
            state["total"] = total
            state["retry_later"] = True
            state["row_attempted"] = False
        else:
            deferred_rows[row_idx] = {
                "row_idx": row_idx,
                "position": position,
                "total": total,
                "retry_count": max(0, int(retry_count)),
                "retry_after": retry_after,
                "cooldown_end": cooldown_end,
                "retry_later": True,
                "row_attempted": False,
            }
        artist = ""
        try:
            artist = _clean_cell(seed_df.at[row_idx, "Artist Name"])
        except Exception:
            artist = ""
        expires = int(max(1.0, cooldown_end - time.time()))
        retry_after_s = int(max(1.0, retry_after - time.time()))
        try:
            self.log_message.emit(
                f"[Enricher][SC] cooldown active; deferring row {position}/{total} "
                f"'{artist or '<unknown>'}' expires_in={expires}s retry_after={retry_after_s}s "
                f"retry_count={max(0, int(retry_count))}"
            )
        except Exception:
            pass
        self._set_platform_state("soundcloud", "skipped")
        try:
            self._write_sc_status_columns(
                seed_df,
                row_idx,
                "retry_later",
                "cooldown_active",
                0,
                0,
            )
        except Exception:
            pass

    def _retry_deferred_soundcloud_rows(
        self,
        seed_df,
        total: int,
        deferred_rows: Dict[Any, Dict[str, Any]],
        *,
        phase_label: str,
    ) -> Dict[Any, Dict[str, Any]]:
        if not deferred_rows:
            return {}
        retried = 0
        enriched = 0
        carried = 0
        exhausted = 0
        remaining: Dict[Any, Dict[str, Any]] = {}
        try:
            self.log_message.emit(
                f"[Enricher][SC Retry] Starting {phase_label} (queued={len(deferred_rows)})"
            )
        except Exception:
            pass
        for row_idx, state in sorted(
            deferred_rows.items(),
            key=lambda item: (
                float(item[1].get("retry_after", 0.0) or 0.0),
                int(item[1].get("position", 0) or 0),
            ),
        ):
            retry_count = max(0, int(state.get("retry_count", 0) or 0))
            position = int(state.get("position", 0) or 0)
            now = time.time()
            retry_after = float(state.get("retry_after", 0.0) or 0.0)
            if retry_after and now < retry_after:
                carried += 1
                remaining[row_idx] = state
                continue
            try:
                if _coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"]):
                    continue
            except Exception:
                pass
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                self._set_platform_state("soundcloud", "skipped")
                continue
            if self._sc_in_live_cooldown():
                self._sc_defer_row_for_cooldown(
                    seed_df,
                    row_idx,
                    position=position,
                    total=total,
                    deferred_rows=remaining,
                    retry_count=retry_count,
                )
                carried += 1
                continue
            if retry_count >= SC_COOLDOWN_ROW_RETRY_MAX:
                exhausted += 1
                continue
            self._init_row_enrichment_state()
            retried += 1
            sc_enriched, _ = self._enrich_row_sc_live(seed_df, row_idx, ctx)
            if sc_enriched:
                enriched += 1
                continue
            if self._sc_in_live_cooldown() and (retry_count + 1) < SC_COOLDOWN_ROW_RETRY_MAX:
                self._sc_defer_row_for_cooldown(
                    seed_df,
                    row_idx,
                    position=position,
                    total=total,
                    deferred_rows=remaining,
                    retry_count=retry_count + 1,
                )
                carried += 1
            elif self._sc_in_live_cooldown():
                exhausted += 1
        try:
            self.log_message.emit(
                f"[Enricher][SC Retry] Completed {phase_label} "
                f"(retried={retried}, enriched={enriched}, deferred={carried}, exhausted={exhausted})"
            )
        except Exception:
            pass
        return remaining

    def _run_source_phased(self, seed_df, directory_indexes, priority, fb_driver, total):
        """Run enrichment in source-phased mode: one source across all rows at a time."""
        use_scheduler = (
            os.getenv("SOURCE_DIVERSITY_SCHEDULER", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        if use_scheduler:
            self.log_message.emit("[Enricher] Source diversity scheduler=ON (round-robin)")
        # Phase 0: Directory matching (fast, no network)
        self._phase_directory_matching(seed_df, directory_indexes, priority, total)
        if use_scheduler:
            # Keep IG extraction outside the scheduler as a bounded single-page pass.
            self._phase_instagram_email(seed_df, total)
            self._phase_website_email(seed_df, total)
            self._run_interleaved_sources(seed_df, fb_driver, total)
            return
        sc_deferred_rows: Dict[Any, Dict[str, Any]] = {}
        # Phase 1: Dedicated SoundCloud live check
        if self.enable_live_search:
            sc_deferred_rows = self._phase_soundcloud(seed_df, total)
        # Phase 2: General live lookup (BC + LF; SC mostly skipped since Phase 1 populated it)
        if self.enable_live_search:
            self._phase_live_lookup(seed_df, total)
        # Phase 3: Instagram profile HTML email extraction (single fetch only)
        self._phase_instagram_email(seed_df, total)
        # Phase 4: bounded website contact crawl from canonical website field.
        self._phase_website_email(seed_df, total)
        if self.enable_live_search and sc_deferred_rows:
            sc_deferred_rows = self._retry_deferred_soundcloud_rows(
                seed_df,
                total,
                sc_deferred_rows,
                phase_label="post_website",
            )
        # Refresh Facebook promotion after live/directory phases so newly discovered FB links are usable.
        seed_df = _apply_fb_promotion_df(seed_df, log_fn=self.log_message.emit)
        # Phase 5: Facebook
        if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:
            self._phase_facebook(seed_df, fb_driver, total)
            if self.enable_live_search and sc_deferred_rows:
                self._retry_deferred_soundcloud_rows(
                    seed_df,
                    total,
                    sc_deferred_rows,
                    phase_label="final_window",
                )

    def _festival_seed_priority_tier(self, row: Any) -> int:
        if row is None:
            return 2
        try:
            priority = cell_to_str(row.get("Seed Priority", "")).strip().lower()
        except Exception:
            priority = ""
        if priority == "festival_high":
            return 0
        if priority == "festival":
            return 1
        return 2

    def _ordered_interleaved_row_ids(self, seed_df: pd.DataFrame) -> List[Any]:
        rows = list(seed_df.index)
        return sorted(rows, key=lambda row_idx: self._festival_seed_priority_tier(seed_df.loc[row_idx]))

    def _run_interleaved_sources(self, seed_df, fb_driver, total):
        """Interleave SC, LF (live lookup), and FB across rows to avoid bursts."""

        seed_df = _apply_fb_promotion_df(seed_df, log_fn=self.log_message.emit)
        rows = self._ordered_interleaved_row_ids(seed_df)
        position_by_row = {row_idx: pos for pos, row_idx in enumerate(seed_df.index, start=1)}
        priority_summary = {"festival_high": 0, "festival": 0, "normal": 0}
        for row_idx in rows:
            tier = self._festival_seed_priority_tier(seed_df.loc[row_idx])
            if tier == 0:
                priority_summary["festival_high"] += 1
            elif tier == 1:
                priority_summary["festival"] += 1
            else:
                priority_summary["normal"] += 1
        if priority_summary["festival_high"] or priority_summary["festival"]:
            self.log_message.emit(
                f"[FestivalPriority] scheduler rows: festival_high={priority_summary['festival_high']} "
                f"festival={priority_summary['festival']} normal={priority_summary['normal']}"
            )

        def _row_label(row_idx: int) -> str:
            pos = position_by_row.get(row_idx, row_idx)
            return f"{pos}/{total}"

        sources: List[SourceSpec] = []

        def _sc_timed_retry(row_idx: int, retry_count: int) -> Optional[TimedRetry]:
            retry_idx = max(0, int(retry_count))
            if retry_idx >= SC_COOLDOWN_ROW_RETRY_MAX:
                return None
            return TimedRetry(
                ready_at=self._sc_row_retry_after(
                    getattr(self, "_sc_live_disabled_until", 0.0),
                    retry_count=retry_idx,
                    ordinal=position_by_row.get(row_idx, 0),
                ),
                max_attempts=SC_COOLDOWN_ROW_RETRY_MAX,
            )

        def row_getter(rid):
            row_data = seed_df.loc[rid]
            if rid not in getattr(self, "_fb_discovery_attempted_rows", set()):
                return row_data
            try:
                row_copy = row_data.copy()
            except Exception:
                row_copy = dict(row_data) if hasattr(row_data, "items") else row_data
            try:
                row_copy["__fb_discovery_attempted_this_run"] = "1"
            except Exception:
                pass
            return row_copy

        if self.enable_live_search:

            def sc_available() -> Tuple[bool, Optional[str]]:
                in_cd = self._sc_in_live_cooldown()
                return (not in_cd, "cooldown" if in_cd else None)

            def sc_run(row_idx: int, retry_count: int = 0) -> SourceResult:
                ctx = self._build_row_context(seed_df, row_idx, position_by_row[row_idx], total)
                if not ctx:
                    return SourceResult()
                if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                    return SourceResult()
                self._init_row_enrichment_state()
                sc_enriched, sc_retry_later = self._enrich_row_sc_live(seed_df, row_idx, ctx)
                timed_retry = None
                if not sc_enriched and self._sc_in_live_cooldown():
                    timed_retry = _sc_timed_retry(row_idx, max(0, int(retry_count)) + 1)
                    sc_retry_later = True
                return SourceResult(
                    attempted=True,
                    enriched=bool(sc_enriched),
                    retry_later=bool(sc_retry_later),
                    timed_retry=timed_retry,
                )

            sources.append(
                SourceSpec(
                    name="SC",
                    rows=rows,
                    run_row=lambda row_idx: sc_run(row_idx, 0),
                    is_available=sc_available,
                    row_getter=row_getter,
                    retry_now=time.time,
                    unavailable_retry=lambda row_idx, reason, retry_count: (
                        _sc_timed_retry(row_idx, retry_count) if reason == "cooldown" else None
                    ),
                    run_row_retry=sc_run,
                )
            )

            def lf_available() -> Tuple[bool, Optional[str]]:
                in_cd = self._lf_endpoint_in_cooldown("search") and self._lf_endpoint_in_cooldown("profile")
                return (not in_cd, "cooldown" if in_cd else None)

            def lf_run(row_idx: int) -> SourceResult:
                ctx = self._build_row_context(seed_df, row_idx, position_by_row[row_idx], total)
                if not ctx:
                    return SourceResult()
                if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                    return SourceResult()
                self._init_row_enrichment_state()
                if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
                    if not self._notified_limit:
                        self.log_message.emit(
                            "[Scheduler] live search limit reached; skipping remaining live lookups."
                        )
                        self._notified_limit = True
                    return SourceResult()
                ll_enriched, ll_retry_later = self._enrich_row_live_lookup(seed_df, row_idx, ctx)
                return SourceResult(
                    attempted=True,
                    enriched=bool(ll_enriched),
                    retry_later=bool(ll_retry_later),
                )

            sources.append(
                SourceSpec(
                    name="LF",
                    rows=rows,
                    run_row=lf_run,
                    is_available=lf_available,
                    row_getter=row_getter,
                )
            )

        if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:

            def fb_available() -> Tuple[bool, Optional[str]]:
                return (True, None)

            def fb_run(row_idx: int) -> SourceResult:
                ctx = self._build_row_context(seed_df, row_idx, position_by_row[row_idx], total)
                if not ctx:
                    return SourceResult()
                if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                    return SourceResult()
                self._init_row_enrichment_state()
                fb_enriched = self._enrich_row_facebook(seed_df, row_idx, fb_driver, ctx)
                fb_status = ""
                if "FB_Status" in seed_df.columns:
                    fb_status = cell_to_str(seed_df.at[row_idx, "FB_Status"]).strip().lower()
                fb_retry_later = fb_status in {"login_wall", "warning_interstitial", "checkpoint", "fetch_error"}
                return SourceResult(
                    attempted=True,
                    enriched=bool(fb_enriched),
                    retry_later=fb_retry_later,
                )

            sources.append(
                SourceSpec(
                    name="FB",
                    rows=rows,
                    run_row=fb_run,
                    is_available=fb_available,
                    row_getter=row_getter,
                )
            )

        if not sources:
            return

        try:
            from pipeline_runner import has_contact_email_for_short_circuit
        except Exception:
            has_contact_email_for_short_circuit = None  # type: ignore

        short_circuit_fn = (
            (lambda row: has_contact_email_for_short_circuit(row)) if has_contact_email_for_short_circuit else None
        )

        scheduler = SourceDiversityScheduler(
            sources,
            row_label=_row_label,
            log_fn=self.log_message.emit,
            short_circuit_fn=short_circuit_fn,
        )
        summary = scheduler.run()
        for source_name in ("SC", "LF", "FB"):
            if source_name in summary:
                stats = summary[source_name]
                self.log_message.emit(
                    f"[Scheduler][Summary] {source_name} attempted={stats['attempted']} "
                    f"enriched={stats['enriched']} skipped_cooldown={stats['skipped_cooldown']} "
                    f"skipped_opportunity={stats['skipped_opportunity']}"
                )
        try:
            from pipeline_runner import get_email_summary_counts

            email_summary = get_email_summary_counts()
            self.log_message.emit(
                f"[Scheduler][Summary] emails_found={email_summary.get('emails_found', 0)} "
                f"pattern_emails={email_summary.get('pattern_emails', 0)}"
            )
        except Exception:
            pass

    def _phase_directory_matching(self, seed_df, directory_indexes, priority, total):
        self.log_message.emit("[Enricher][Directory Phase] Starting...")
        enriched_count = 0
        for position, row_idx in enumerate(seed_df.index, start=1):
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                self._update_progress(position, total)
                continue
            if self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                self._update_progress(position, total)
                continue
            self._init_row_enrichment_state()
            if self._enrich_row_directories(seed_df, row_idx, directory_indexes, priority, ctx):
                enriched_count += 1
            self._update_progress(position, total)
        self.log_message.emit(f"[Enricher][Directory Phase] Completed {total} rows (enriched={enriched_count})")

    def _phase_soundcloud(self, seed_df, total):
        self.log_message.emit("[Enricher][SC Phase] Starting...")
        enriched_count = 0
        skipped_cooldown = 0
        skipped_disabled = 0
        processed_rows = 0
        cooldown_remaining_hint = 0
        deferred_rows: Dict[Any, Dict[str, Any]] = {}
        stopped_max_live = False
        for position, row_idx in enumerate(seed_df.index, start=1):
            if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
                stopped_max_live = True
                break
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            processed_rows += 1
            if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                self._set_platform_state("soundcloud", "skipped")
                continue
            if self._sc_in_live_cooldown():
                skipped_cooldown += 1
                try:
                    cooldown_remaining_hint = max(
                        cooldown_remaining_hint,
                        int(max(0.0, (getattr(self, "_sc_live_disabled_until", 0.0) or 0.0) - time.time())),
                    )
                except Exception:
                    pass
                self._sc_defer_row_for_cooldown(
                    seed_df,
                    row_idx,
                    position=position,
                    total=total,
                    deferred_rows=deferred_rows,
                    retry_count=0,
                )
                continue
            self._init_row_enrichment_state()
            if not _coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"]):
                if getattr(self, "_sc_live_enrich_disabled", False):
                    skipped_disabled += 1
                else:
                    sc_enriched, _ = self._enrich_row_sc_live(seed_df, row_idx, ctx)
                    if sc_enriched:
                        enriched_count += 1
        if stopped_max_live:
            self.log_message.emit("[Enricher][SC Phase] Stopped early: max_live")
        if skipped_cooldown:
            try:
                cooldown_remaining_hint = max(
                    cooldown_remaining_hint,
                    int(max(0.0, (getattr(self, "_sc_live_disabled_until", 0.0) or 0.0) - time.time())),
                )
            except Exception:
                pass
            try:
                cooldown_snapshot = self._sc_fail_stats_snapshot()
            except Exception:
                cooldown_snapshot = ""
            try:
                self.log_message.emit(
                    "[Enricher][SC Phase] cooldown_skip_summary: "
                    f"skipped_cooldown={skipped_cooldown} "
                    f"cooldown_s_remaining={cooldown_remaining_hint} "
                    f"counts={cooldown_snapshot}"
                )
            except Exception:
                pass
            try:
                self.log_message.emit(
                    "[Enricher][SC Phase] cooldown_summary: %s" % (cooldown_snapshot,)
                )
            except Exception:
                pass
        self.log_message.emit(
            f"[Enricher][SC Phase] Completed {processed_rows} rows "
            f"(enriched={enriched_count}, skipped_cooldown={skipped_cooldown}, "
            f"skipped_disabled={skipped_disabled}, deferred={len(deferred_rows)})"
        )
        return deferred_rows

    def _phase_instagram_email(self, seed_df, total):
        for position, row_idx in enumerate(seed_df.index, start=1):
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                self._set_platform_state("instagram", "skipped")
                continue
            self._init_row_enrichment_state()
            self._enrich_row_instagram_email(seed_df, row_idx, ctx)

    def _phase_website_email(self, seed_df, total):
        for position, row_idx in enumerate(seed_df.index, start=1):
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                self._set_platform_state("website", "skipped")
                continue
            self._init_row_enrichment_state()
            self._enrich_row_website_email(seed_df, row_idx, ctx)

    def _phase_live_lookup(self, seed_df, total):
        self.log_message.emit("[Enricher][LF Phase] Starting...")
        self._reset_live_lookup_bclf_stats()
        self._live_lookup_bclf_adaptive_enabled = True
        try:
            enriched_count = 0
            processed_rows = 0
            skipped_search_cooldown = 0
            skipped_profile_cooldown = 0
            stopped_max_live = False
            for position, row_idx in enumerate(seed_df.index, start=1):
                ctx = self._build_row_context(seed_df, row_idx, position, total)
                if not ctx:
                    continue
                processed_rows += 1
                if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                    self._set_platform_state("lastfm", "skipped")
                    continue
                self._init_row_enrichment_state()
                lf_skip_search = False
                if self._lf_endpoint_in_cooldown("search"):
                    artist = ctx.get("artist", "")
                    artist_key = normalise_artist_name(artist)
                    seed_urls = set(ctx.get("seed_lastfm_urls") or [])
                    cached_profile = (
                        artist_key
                        and (
                            artist_key in self._lf_profile_url_cache
                            or (
                                artist_key in self._lf_search_result_cache
                                and self._lf_search_result_cache.get(artist_key)
                            )
                        )
                    )
                    has_profile_first = bool(seed_urls or cached_profile)
                    if not has_profile_first:
                        lf_skip_search = True
                        if not self._lf_search_cooldown_skip_logged:
                            cooldown = self._lf_endpoint_cooldown_remaining("search")
                            try:
                                self.log_message.emit(
                                    f"[Enricher][LF] search cooldown active; skipping row '{artist}' expires_in={cooldown}s"
                                )
                            except Exception:
                                pass
                            self._lf_search_cooldown_skip_logged = True
                    self._lf_search_skipped_cooldown += 1
                    self._set_platform_state("lastfm", "skipped")
                if (not lf_skip_search) and self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
                    stopped_max_live = True
                    break
                ll_enriched, _ = self._enrich_row_live_lookup(seed_df, row_idx, ctx, skip_lastfm=lf_skip_search)
                if ll_enriched:
                    enriched_count += 1
                skipped_search_cooldown += getattr(self, "_lf_search_skipped_cooldown", 0)
                skipped_profile_cooldown += getattr(self, "_lf_profile_skipped_cooldown", 0)
                self._lf_search_skipped_cooldown = 0
                self._lf_profile_skipped_cooldown = 0
            if stopped_max_live:
                self.log_message.emit("[Enricher][LF Phase] Stopped early: max_live")
            try:
                self.log_message.emit(
                    f"[Enricher][LF Phase] search_skip_summary: search_skipped={skipped_search_cooldown}"
                )
            except Exception:
                pass
            self.log_message.emit(
                f"[Enricher][LF Phase] Completed {processed_rows} rows (enriched={enriched_count}, "
                f"search_skipped={skipped_search_cooldown}, profile_skipped={skipped_profile_cooldown})"
            )
        finally:
            self._live_lookup_bclf_adaptive_enabled = False

    def _phase_facebook(self, seed_df, fb_driver, total):
        self.log_message.emit("[Enricher][FB Phase] Starting...")
        enriched_count = 0
        skipped_count = 0
        processed_rows = 0
        stop_reason = ""
        for position, row_idx in enumerate(seed_df.index, start=1):
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            processed_rows += 1
            if row_idx in self._domain_email_reuse_rows or self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                self._set_platform_state("facebook", "skipped")
                skipped_count += 1
                self._update_progress(position, total)
                continue
            self._init_row_enrichment_state()
            if self._enrich_row_facebook(seed_df, row_idx, fb_driver, ctx):
                enriched_count += 1
            else:
                skipped_count += 1
            self._update_progress(position, total)
        if stop_reason:
            self.log_message.emit(f"[Enricher][FB Phase] Stopped early: {stop_reason}")
        self.log_message.emit(
            f"[Enricher][FB Phase] Completed {processed_rows} rows (enriched={enriched_count}, skipped={skipped_count})"
        )

    def _ensure_bandcamp_output_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Guarantee Bandcamp output + diagnostic columns exist and are string typed.
        Also canonicalise and backfill Bandcamp_URL from Profile URL when applicable.
        """
        bc_cols = ["Bandcamp_URL", "BC_Status", "BC_Mode", "BC_Attempts", "BC_403_Count"]
        for col in bc_cols:
            if col not in df.columns:
                df[col] = ""
            df[col] = df[col].fillna("").astype(str)
        df["Bandcamp_URL"] = df["Bandcamp_URL"].apply(_canonicalise_bandcamp_url)
        if "Profile URL" in df.columns:
            profile_canon = df["Profile URL"].fillna("").astype(str).apply(_canonicalise_bandcamp_url)
            needs_bandcamp = df["Bandcamp_URL"].fillna("").astype(str).str.strip() == ""
            profile_is_bandcamp = profile_canon.str.contains("bandcamp.com", case=False, na=False)
            mask = needs_bandcamp & profile_is_bandcamp
            if mask.any():
                df.loc[mask, "Bandcamp_URL"] = profile_canon[mask]
        return df

    # ---------------- Bandcamp resilience helpers ----------------
    def _bc_reset_row_stats(self) -> None:
        self._last_bc_row_stats = {
            "status": "pending",
            "mode": "",
            "attempts": 0,
            "http_403": 0,
        }

    def _reset_live_lookup_bclf_stats(self) -> None:
        self._live_lookup_bclf_stats = {
            "bandcamp": {"attempts": 0, "enriched": 0, "cooldown": 0},
            "lastfm": {"attempts": 0, "enriched": 0, "cooldown": 0},
        }

    def _record_live_lookup_bclf_attempt(self, source: str) -> None:
        if source not in {"bandcamp", "lastfm"}:
            return
        stats = getattr(self, "_live_lookup_bclf_stats", None)
        if not isinstance(stats, dict) or source not in stats:
            self._reset_live_lookup_bclf_stats()
            stats = self._live_lookup_bclf_stats
        stats[source]["attempts"] += 1

    def _record_live_lookup_bclf_cooldown(self, source: str) -> None:
        if source not in {"bandcamp", "lastfm"}:
            return
        stats = getattr(self, "_live_lookup_bclf_stats", None)
        if not isinstance(stats, dict) or source not in stats:
            self._reset_live_lookup_bclf_stats()
            stats = self._live_lookup_bclf_stats
        stats[source]["cooldown"] += 1

    def _record_live_lookup_bclf_applied_winner(
        self,
        payload: Optional[EnrichmentPayload],
        *,
        applied: bool,
    ) -> None:
        if not applied or not payload:
            return
        source = (getattr(payload, "source_dir", "") or "").strip().lower()
        if source not in {"bandcamp", "lastfm"}:
            return
        stats = getattr(self, "_live_lookup_bclf_stats", None)
        if not isinstance(stats, dict) or source not in stats:
            self._reset_live_lookup_bclf_stats()
            stats = self._live_lookup_bclf_stats
        stats[source]["enriched"] += 1

    def _live_lookup_bclf_priority_score(self, source: str) -> float:
        stats = (getattr(self, "_live_lookup_bclf_stats", {}) or {}).get(source, {})
        attempts = int(stats.get("attempts", 0) or 0)
        enriched = int(stats.get("enriched", 0) or 0)
        cooldown = int(stats.get("cooldown", 0) or 0)
        success_bonus = 0.0
        if attempts >= LIVE_LOOKUP_BCLF_MIN_ATTEMPTS:
            success_rate = enriched / attempts if attempts else 0.0
            success_bonus = min(
                LIVE_LOOKUP_BCLF_MAX_BONUS,
                max(0.0, success_rate * LIVE_LOOKUP_BCLF_MAX_BONUS),
            )
        cooldown_penalty = 0.0
        if cooldown > 0:
            denom = attempts or cooldown or 1
            cooldown_rate = cooldown / denom if denom else 0.0
            cooldown_penalty = min(
                LIVE_LOOKUP_BCLF_MAX_COOLDOWN_PENALTY,
                max(0.0, cooldown_rate * LIVE_LOOKUP_BCLF_MAX_COOLDOWN_PENALTY),
            )
        return LIVE_LOOKUP_BCLF_BASE_SCORE + success_bonus - cooldown_penalty

    def _live_lookup_bclf_order(self) -> Tuple[str, str]:
        default_order = ("bandcamp", "lastfm")
        stats = getattr(self, "_live_lookup_bclf_stats", {}) or {}
        bandcamp_attempts = int((stats.get("bandcamp") or {}).get("attempts", 0) or 0)
        lastfm_attempts = int((stats.get("lastfm") or {}).get("attempts", 0) or 0)
        if min(bandcamp_attempts, lastfm_attempts) < LIVE_LOOKUP_BCLF_MIN_ATTEMPTS:
            return default_order
        bandcamp_score = self._live_lookup_bclf_priority_score("bandcamp")
        lastfm_score = self._live_lookup_bclf_priority_score("lastfm")
        if lastfm_score > bandcamp_score:
            return ("lastfm", "bandcamp")
        return default_order

    def _bc_record_attempt(self, status_code: Optional[int]) -> None:
        self._bc_search_attempts += 1
        if status_code == 403:
            self._bc_total_403 += 1
            self._bc_consecutive_403 += 1
            self._last_bc_row_stats["http_403"] = self._last_bc_row_stats.get("http_403", 0) + 1
        else:
            self._bc_consecutive_403 = 0
        self._last_bc_row_stats["attempts"] = self._last_bc_row_stats.get("attempts", 0) + 1
        # Breaker logic: consecutive 403s OR high 403 rate after enough attempts.
        if not self._bc_search_breaker_tripped:
            if self._bc_consecutive_403 >= BC_BREAKER_CONSEC_403:
                self._bc_search_breaker_tripped = True
                self._bc_search_breaker_reason = f"consecutive_403>={BC_BREAKER_CONSEC_403}"
            elif (
                self._bc_search_attempts >= BC_BREAKER_MIN_ATTEMPTS
                and self._bc_total_403 / max(1, self._bc_search_attempts) >= BC_BREAKER_RATE_THRESHOLD
            ):
                self._bc_search_breaker_tripped = True
                self._bc_search_breaker_reason = f"403_rate>={BC_BREAKER_RATE_THRESHOLD:.2f}"

    def _bc_should_skip_search(self) -> bool:
        return self._bc_search_breaker_tripped

    def _bc_gap(self) -> None:
        time.sleep(random.uniform(BC_QUERY_GAP_MIN, BC_QUERY_GAP_MAX))

    def _bc_backoff_sleep(self, attempt: int) -> None:
        delay = min(BC_BACKOFF_MAX, BC_BACKOFF_BASE * (2 ** max(0, attempt - 1)))
        jitter = random.uniform(0, 0.35)
        time.sleep(delay + jitter)

    def _bc_http_get(
        self,
        url: str,
        label: str = "Bandcamp search",
        count_breaker: bool = True,
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Perform a polite GET with bounded retries for Bandcamp endpoints.
        Returns (html, status_code).
        """
        attempt = 0
        last_status = None
        while attempt < BC_SEARCH_RETRY_MAX:
            attempt += 1
            try:
                resp = self._bc_session.get(url, timeout=HTTP_TIMEOUT)
                last_status = getattr(resp, "status_code", None)
                if count_breaker:
                    self._bc_record_attempt(last_status)
                if last_status == 200:
                    return resp.text, last_status
                if last_status in (403, 429) or (last_status and last_status >= 500):
                    if attempt < BC_SEARCH_RETRY_MAX:
                        self._bc_backoff_sleep(attempt)
                        continue
                    return None, last_status
                # Other non-OK -> no retry to avoid noise
                return None, last_status
            except requests.RequestException:
                if count_breaker:
                    self._bc_record_attempt(None)
                if attempt < BC_SEARCH_RETRY_MAX:
                    self._bc_backoff_sleep(attempt)
                    continue
                return None, None
        return None, last_status

    def _bc_discover_http_get(self, url: str, label: str = "Bandcamp discover") -> Tuple[Optional[str], Optional[int]]:
        """
        Fetch Bandcamp Discover HTML without touching the search breaker counters.
        Bounded by BC_DISCOVER_MAX_FETCHES for the entire run.
        """
        if self._bc_discover_fetches >= BC_DISCOVER_MAX_FETCHES:
            return None, None
        self._bc_discover_fetches += 1
        try:
            resp = self._bc_session.get(url, timeout=HTTP_TIMEOUT)
            status = getattr(resp, "status_code", None)
            if status != 200:
                return None, status
            return resp.text, status
        except requests.RequestException:
            return None, None

    @staticmethod
    def _bc_slugify(value: str) -> str:
        if not value:
            return ""
        norm = normalize_name(value)
        slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
        return slug

    def _bc_location_slug(self, location: str) -> str:
        clean_loc = _clean_cell(location)
        if not clean_loc:
            return ""
        parts = [p.strip() for p in clean_loc.split(",") if p.strip()]
        coarse = parts[-1] if parts else clean_loc
        slug = self._bc_slugify(coarse)
        if not slug:
            slug = self._bc_slugify(clean_loc)
        return slug

    def _bc_build_discover_urls(self, primary_genre: str, location: str) -> List[str]:
        genre_slug = self._bc_slugify(primary_genre)
        loc_slug = self._bc_location_slug(location)
        urls: List[str] = []
        if genre_slug and loc_slug:
            urls.append(f"https://bandcamp.com/discover/{loc_slug}+{genre_slug}?s=new")
        if genre_slug:
            urls.append(f"https://bandcamp.com/discover/{genre_slug}?s=new")
        return urls[:2]

    @staticmethod
    def _bc_extract_discover_candidates(html: str) -> List[Tuple[str, str]]:
        """
        Parse discover HTML and return (display_name, profile_url) tuples.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        candidates: List[Tuple[str, str]] = []
        seen_hosts: Set[str] = set()
        # Prefer structured tiles when present.
        tiles = soup.select("li.results-grid-item, div.results-grid-item, li[data-band-id], li[data-band-url]")
        if not tiles:
            tiles = soup.select("li, div")
        for tile in tiles:
            if len(candidates) >= 30:
                break
            # Use explicit attrs first.
            band_url = tile.get("data-band-url") or tile.get("data-band-url-href")
            display = tile.get("data-band-name") or ""
            link_el = None
            if not band_url:
                link_el = tile.select_one("a[href*='bandcamp.com']")
                if link_el:
                    band_url = link_el.get("href") or ""
            if not band_url:
                continue
            band_url = band_url.strip()
            if not band_url:
                continue
            canon = _canonicalise_bandcamp_url(band_url)
            if not canon or "bandcamp.com" not in canon:
                continue
            try:
                parsed = urllib.parse.urlsplit(canon)
                host = parsed.netloc
            except Exception:
                host = ""
            if not host or host in seen_hosts:
                continue
            seen_hosts.add(host)
            profile_url = f"https://{host}/"
            if not display:
                if tile:
                    heading = tile.select_one(".heading, .result-info, .title")
                    if heading:
                        display = heading.get_text(" ", strip=True)
                if not display and link_el:
                    display = link_el.get_text(" ", strip=True)
                if not display:
                    display = host.split(".")[0]
            candidates.append((display, profile_url))
        if not candidates:
            # Fallback: crawl anchors to avoid empty result when markup changes.
            anchors = soup.select("a[href*='bandcamp.com']")
            for anchor in anchors:
                if len(candidates) >= 30:
                    break
                href = anchor.get("href") or ""
                canon = _canonicalise_bandcamp_url(href)
                if not canon or "bandcamp.com" not in canon:
                    continue
                try:
                    parsed = urllib.parse.urlsplit(canon)
                    host = parsed.netloc
                except Exception:
                    host = ""
                if not host or host in seen_hosts:
                    continue
                seen_hosts.add(host)
                profile_url = f"https://{host}/"
                display = anchor.get("data-band-name") or anchor.get_text(" ", strip=True) or host.split(".")[0]
                candidates.append((display, profile_url))
        return candidates[:30]

    def _bc_discover_enrich(
        self,
        artist_name: str,
        song_title: str,
        location: str,
        primary_genre: str,
    ) -> Tuple[Optional[EnrichmentPayload], bool]:
        urls = self._bc_build_discover_urls(primary_genre, location)
        if not urls:
            return None, False
        saw_403 = False
        for idx, url in enumerate(urls):
            if idx > 0:
                self._bc_gap()
            cached = self._bc_discover_cache.get(url)
            if cached is None:
                if self._bc_discover_fetches >= BC_DISCOVER_MAX_FETCHES:
                    break
                html, status = self._bc_discover_http_get(url, label="Bandcamp discover")
                if status == 403:
                    # Treat discover 403 as non-fatal; continue to next url.
                    self._bc_discover_cache[url] = []
                    saw_403 = True
                    continue
                if not html:
                    self._bc_discover_cache[url] = []
                    continue
                cached = self._bc_extract_discover_candidates(html)
                self._bc_discover_cache[url] = cached
            candidates = cached or []
            for display_name, profile_url in candidates:
                confidence = _bandcamp_confidence(artist_name, display_name, profile_url, song_title=song_title)
                if confidence < MIN_BC_CONFIDENCE:
                    continue
                payload = self._fetch_profile_and_build(profile_url, "bandcamp", confidence=confidence)
                if payload:
                    payload.match_score = self._compute_match_score_for_candidate(
                        display_name or profile_url,
                        song_title,
                        extract_domain(profile_url),
                    )
                    payload.candidate_name = display_name or ""
                    return payload, saw_403
        return None, saw_403

    def _make_minimal_payload_for_url(
        self,
        profile_url: str,
        display_name: str,
        confidence: float,
        song_title: str = "",
    ) -> EnrichmentPayload:
        payload = EnrichmentPayload(
            socials=set(),
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir="bandcamp_directory",
            source_url=_canonicalise_bandcamp_url(profile_url),
            source_detail="Bandcamp Directory",
            match_score=self._compute_match_score_for_candidate(
                display_name or profile_url,
                song_title,
                extract_domain(profile_url),
            ),
            candidate_name=display_name or "",
        )
        payload.match_score = payload.match_score or 0.0
        # carry confidence into payload even though we didn't fetch page
        payload.confidence = confidence  # type: ignore[attr-defined]
        return payload

    def _bc_directory_fallback(self, artist_name: str, song_title: str) -> Optional[EnrichmentPayload]:
        """
        Offline-first Bandcamp enrich using the loaded Bandcamp directory index.
        Avoids hitting bandcamp.com when discover/search are blocked.
        """
        debug_attempts = bool(os.getenv("BC_DEBUG_ATTEMPTS"))
        index = getattr(self, "_directory_indexes", {}).get("bandcamp") if hasattr(self, "_directory_indexes") else None
        if not index:
            if debug_attempts:
                self.log_message.emit(
                    "[BC Debug] directory_fallback: index_missing "
                    f"bandcamp_csv_path_set={bool(getattr(self, 'bandcamp_csv_path', ''))}"
                )
            return None
        key = normalise_artist_name(artist_name)
        candidates = _dedupe_rows(index.lookup_artist(key))
        shortlist_used = False
        if not candidates:
            shortlist: List[Dict[str, Any]] = []
            for idx, (artist_key, rows) in enumerate(index.rows_by_artist.items()):
                if idx >= 1000:
                    break
                if not artist_key:
                    continue
                if key and (key in artist_key or artist_key in key):
                    shortlist.extend(rows)
                else:
                    try:
                        if key and difflib.SequenceMatcher(None, key, artist_key).ratio() >= 0.82:
                            shortlist.extend(rows)
                    except Exception:
                        pass
                if len(shortlist) >= 200:
                    break
            candidates = shortlist[:200]
            shortlist_used = True
        if debug_attempts:
            try:
                self.log_message.emit(
                    "[BC Debug] directory_fallback: key='%s' candidates=%d shortlist_used=%s index_artists=%d"
                    % (
                        key,
                        len(candidates),
                        shortlist_used,
                        index.unique_artist_count() if index else 0,
                    )
                )
            except Exception:
                pass
        best_payload: Optional[EnrichmentPayload] = None
        best_conf = 0.0
        best_match = 0.0
        url_only_logged = False
        for row in candidates:
            profile_url = _canonicalise_bandcamp_url(_extract_profile_url(row) or "")
            if not profile_url or "bandcamp.com" not in profile_url:
                continue
            display_name = _clean_cell(
                row.get("Artist Name")
                or row.get("artist")
                or row.get("Name")
                or ""
            )
            confidence = _bandcamp_confidence(artist_name, display_name or profile_url, profile_url, song_title=song_title)
            if confidence < MIN_BC_CONFIDENCE:
                continue
            payload = self._fetch_profile_and_build(profile_url, "bandcamp", confidence=confidence)
            if not payload:
                # Fall back to URL-only payload when profile fetch is blocked.
                if not url_only_logged:
                    try:
                        self.log_message.emit(
                            f"[Enricher] Bandcamp: directory candidate matched but profile fetch blocked; emitting URL-only payload for '{artist_name}' -> {profile_url}"
                        )
                    except Exception:
                        pass
                    url_only_logged = True
                payload = self._make_minimal_payload_for_url(profile_url, display_name, confidence, song_title=song_title)
                if not payload or not payload.source_url:
                    continue
            payload.match_score = self._compute_match_score_for_candidate(
                display_name or profile_url,
                song_title,
                extract_domain(profile_url),
            )
            payload.candidate_name = display_name or ""
            if confidence > best_conf or (abs(confidence - best_conf) <= 0.005 and payload.match_score > best_match):
                best_payload = payload
                best_conf = confidence
                best_match = payload.match_score
        return best_payload

    def _flag_sc_blocked(self, status_code: Optional[int] = None, html: str = "") -> None:
        global _SC_HEALTHCHECK_LOGGED
        self._sc_blocked_for_row = True
        if not _SC_HEALTHCHECK_LOGGED:
            suffix = _format_outcome_suffix(
                fetch_ok=False,
                actionable=False,
                http_status=status_code if status_code is not None else 403,
            )
            self.log_message.emit(
                "[Night SC] Healthcheck: blocked_403 detected (SoundCloud returned 403/blocked page). "
                f"Marking SC rows as blocked_403.{suffix}"
            )
            _SC_HEALTHCHECK_LOGGED = True
        self._set_platform_state("soundcloud", "skipped")

    def _mark_sc_blocked_row(self, df: pd.DataFrame, row_idx) -> bool:
        if not getattr(self, "_sc_blocked_for_row", False):
            return False
        field = None
        if "SC_Status" in df.columns:
            field = "SC_Status"
        elif "final_status" in df.columns:
            field = "final_status"
        if field:
            df.at[row_idx, field] = "blocked_403"
        # Even if no writable field is present, treat the row as handled so callers short-circuit.
        return True

    def _set_platform_state(self, platform: str, status: str) -> None:
        if not hasattr(self, "_row_enrichment_state"):
            self._row_enrichment_state = {}
        self._row_enrichment_state[platform] = status

    def _sc_in_live_cooldown(self, now: Optional[float] = None) -> bool:
        """
        Return True if SoundCloud live enrichment is temporarily paused by the breaker.
        Automatically clears the cooldown once it has elapsed.
        """
        now = time.time() if now is None else now
        disabled_until = getattr(self, "_sc_live_disabled_until", 0.0) or 0.0
        if disabled_until and now >= disabled_until:
            self._sc_live_disabled_until = 0.0
            # Reset streak so the next failure sequence must rebuild the breaker.
            self._sc_rss_fail_streak = 0
            self._sc_rss_fail_streak_blocked = 0
            self._sc_rss_fail_streak_nofeed = 0
            self._sc_rss_fail_counts = Counter()
            if hasattr(self, "_sc_rss_fail_last_reasons"):
                self._sc_rss_fail_last_reasons.clear()
            self._sc_reset_403_streak()
            return False
        return bool(disabled_until and now < disabled_until)

    def _disable_sc_live_enrich(self, reason: str = "first_challenge_page") -> None:
        if getattr(self, "_sc_live_enrich_disabled", False):
            return
        self._sc_live_enrich_disabled = True
        self._sc_live_enrich_disabled_reason = reason or "first_challenge_page"
        try:
            if self._sc_live_enrich_disabled_reason == "breaker_tripped":
                self.log_message.emit(
                    "[Enricher][SC] Circuit breaker tripped; disabling SC live enrichment for remainder of enrichment run (cache-only still allowed)."
                )
            else:
                self.log_message.emit(
                    "[Enricher][SC] First challenge page detected; disabling SC live enrichment for remainder of enrichment run (cache-only still allowed)."
                )
        except Exception:
            pass

    def _sc_enter_rss_only_mode(self, reason: str = "html_challenges", row_idx: Optional[int] = None) -> bool:
        if getattr(self, "_sc_rss_only_mode", False):
            return False
        self._sc_rss_only_mode = True
        self._sc_rss_only_entered_at = time.time()
        self._sc_rss_only_rows = 0
        self._sc_rss_successes = 0
        try:
            if not getattr(self, "_sc_rss_only_logged", False):
                row_label = row_idx if row_idx is not None else "<unknown>"
                self.log_message.emit(
                    f"[Night SC] Entering RSS-only mode (reason={reason} row={row_label} "
                    f"challenges={int(getattr(self, '_sc_html_challenge_count', 0))} "
                    f"html_streak={int(getattr(self, '_night_sc_challenge_streak', 0))} "
                    f"api_403_streak={int(getattr(self, '_sc_consecutive_403', 0))} "
                    f"threshold_403={int(SC_RSS_ONLY_CONSEC_403)})"
                )
                self._sc_rss_only_logged = True
        except Exception:
            pass
        return True

    def _sc_exit_rss_only_mode(self, reason: str = "cooldown_elapsed", row_idx: Optional[int] = None) -> None:
        if not getattr(self, "_sc_rss_only_mode", False):
            return
        self._sc_rss_only_mode = False
        self._sc_rss_only_logged = False
        try:
            self.log_message.emit(
                "[Night SC] Exiting RSS-only mode (reason=%s row=%s rss_successes=%d rows_since=%d)"
                % (
                    reason,
                    row_idx if row_idx is not None else "<unknown>",
                    int(getattr(self, "_sc_rss_successes", 0)),
                    int(getattr(self, "_sc_rss_only_rows", 0)),
                )
            )
        except Exception:
            pass
        self._sc_rss_only_entered_at = 0.0
        self._sc_rss_only_rows = 0
        self._sc_rss_successes = 0
        self._night_sc_challenge_streak = 0
        self._sc_rss_fail_streak = 0
        self._sc_rss_fail_streak_blocked = 0
        self._sc_rss_fail_streak_nofeed = 0
        self._sc_rss_fail_counts = Counter()
        self._sc_rss_fail_last_reasons.clear()
        self._sc_reset_403_streak()

    def _sc_maybe_exit_rss_only(self, row_idx: Optional[int] = None) -> None:
        if not getattr(self, "_sc_rss_only_mode", False):
            return
        elapsed = time.time() - (getattr(self, "_sc_rss_only_entered_at", 0.0) or 0.0)
        rows = getattr(self, "_sc_rss_only_rows", 0)
        successes = getattr(self, "_sc_rss_successes", 0)
        if (
            elapsed >= SC_RSS_ONLY_COOLDOWN_SECONDS
            or rows >= SC_RSS_ONLY_COOLDOWN_ROWS
            or successes >= SC_RSS_ONLY_SUCCESS_RESET
        ):
            self._sc_exit_rss_only_mode(
                reason="cooldown_elapsed" if elapsed >= SC_RSS_ONLY_COOLDOWN_SECONDS else "rss_success_reset",
                row_idx=row_idx,
            )

    def _sc_fail_stats_snapshot(self) -> str:
        counts_summary = dict(getattr(self, "_sc_rss_fail_counts", Counter()).most_common(4))
        last_reasons = list(getattr(self, "_sc_rss_fail_last_reasons", []))
        return (
            f"blocked_streak={getattr(self, '_sc_rss_fail_streak_blocked', 0)} "
            f"nofeed_streak={getattr(self, '_sc_rss_fail_streak_nofeed', 0)} "
            f"counts={counts_summary} last={last_reasons}"
        )

    def _sc_reset_403_streak(self) -> None:
        """Reset consecutive 403 counter after a successful or non-403 outcome."""
        self._sc_consecutive_403 = 0

    def _sc_record_403(self, row_idx: Optional[int] = None, source: str = "unknown") -> None:
        """
        Track consecutive 403/blocked responses from SC engine/profile fetches.
        Enter RSS-only mode when the streak reaches the configured threshold.
        """
        if getattr(self, "_sc_rss_only_mode", False):
            return
        self._sc_consecutive_403 += 1
        if getattr(self, "_sc_consecutive_403", 0) >= SC_RSS_ONLY_CONSEC_403:
            entered = self._sc_enter_rss_only_mode(
                reason="consecutive_403",
                row_idx=row_idx,
            )
            if entered:
                self._sc_rss_only_entries_consecutive_403 += 1
    def _sc_record_html_challenge(self) -> None:
        try:
            now = time.time()
            if self._sc_last_challenge_at and (now - self._sc_last_challenge_at) > SC_CHALLENGE_ACTIVE_SECONDS:
                self._night_sc_challenge_streak = 0
            self._sc_last_challenge_at = now
            self._sc_html_challenge_count += 1
            self._night_sc_challenge_streak += 1
            if getattr(self, "_night_sc_challenge_streak", 0) >= SC_RSS_ONLY_CONSEC_CHALLENGES:
                try:
                    if not getattr(self, "_sc_rss_only_mode", False):
                        self.log_message.emit(
                            "[Night SC] forcing rss_only due to html challenges=%d" % int(self._sc_html_challenge_count)
                        )
                except Exception:
                    pass
                self._sc_enter_rss_only_mode(reason="consecutive_challenges")
        except Exception:
            pass

    def _sc_record_rss_result(self, success: bool, reason: str = "", row_idx: Optional[int] = None) -> None:
        try:
            if not hasattr(self, "_sc_rss_fail_last_reasons"):
                self._sc_rss_fail_last_reasons = deque(maxlen=5)
            reason_norm = (reason or "unknown").strip().lower()
            if success:
                self._sc_rss_fail_streak = 0
                self._sc_rss_fail_streak_blocked = 0
                self._sc_rss_fail_streak_nofeed = 0
                self._sc_rss_fail_counts = Counter()
                self._sc_rss_fail_last_reasons.clear()
                self._sc_rss_successes += 1
                self._sc_maybe_exit_rss_only(row_idx=row_idx)
                return
            else:
                self._sc_rss_fail_streak += 1
                self._sc_rss_fail_last_reasons.append(reason_norm)
                self._sc_rss_fail_counts[reason_norm] += 1
                category = _sc_classify_rss_reason(reason_norm)
                self._sc_rss_fail_counts[category] += 1
                if category == "blocked":
                    self._sc_rss_fail_streak_blocked += 1
                else:
                    self._sc_rss_fail_streak_blocked = 0
                if category == "nofeed":
                    self._sc_rss_fail_streak_nofeed += 1
                else:
                    self._sc_rss_fail_streak_nofeed = 0
                min_rows_ok = getattr(self, "_sc_rows_seen", 0) >= SC_BREAKER_MIN_ROWS
                if (
                    self._sc_rss_fail_streak_blocked >= SC_RSS_FAIL_BREAKER_THRESHOLD
                    and min_rows_ok
                ):
                    self._night_sc_breaker_tripped = True
                    now = time.time()
                    cooldown_s = SC_RSS_BREAKER_COOLDOWN_SECONDS
                    if getattr(self, "_sc_live_disabled_until", 0.0) and self._sc_live_disabled_until > now:
                        # Extend, but cap to one additional window.
                        self._sc_live_disabled_until = min(self._sc_live_disabled_until + cooldown_s, now + cooldown_s * 2)
                    else:
                        self._sc_live_disabled_until = now + cooldown_s
                    try:
                        cooldown_left = int(max(1.0, self._sc_live_disabled_until - now))
                        snapshot = self._sc_fail_stats_snapshot()
                        message = (
                            f"[Night SC] Circuit breaker: rows_seen={getattr(self, '_sc_rows_seen', 0)} "
                            f"row={row_idx if row_idx is not None else '<unknown>'} -> entering cooldown for {cooldown_left}s "
                            f"(live SC paused; RSS/cache continue) {snapshot}"
                        )
                        self.log_message.emit(message)
                    except Exception:
                        pass
        except Exception:
            pass

    def _note_sc_challenge(self, status: str = "", reason: str = "", challenge_flag: bool = False) -> None:
        status_l = (status or "").strip().lower()
        reason_l = (reason or "").strip().lower()
        if status_l == "non_actionable_challenge" or reason_l == "challenge_page" or challenge_flag:
            self._sc_record_html_challenge()
            return

    def _platform_attempt_allowed(self, platform: str, artist_name: str, label: str) -> bool:
        state = getattr(self, "_row_enrichment_state", {}).get(platform)
        if state in {"matched", "skipped"}:
            prefix = "[FB Enrich]" if platform == "facebook" else "[Enricher]"
            self.log_message.emit(f"{prefix} {label}: skipping '{artist_name}' (already attempted).")
            return False
        return True

    # -----------------------------
    # Last.fm health helpers (soft-degrade)
    # -----------------------------
    def _lf_now(self) -> float:
        return time.monotonic()

    # ---------- Last.fm cooldown helpers (endpoint-specific) ----------
    def _lf_endpoint_in_cooldown(self, endpoint: str, now: Optional[float] = None) -> bool:
        now = self._lf_now() if now is None else now
        until = self._lf_search_cooldown_until if endpoint == "search" else self._lf_profile_cooldown_until
        return bool(until and now < until)

    def _lf_clear_endpoint_cooldown(self, endpoint: str) -> None:
        if endpoint == "search":
            if self._lf_search_cooldown_until:
                self._lf_search_cooldown_until = 0.0
            self._lf_search_cooldown_logged = False
            self._lf_search_cooldown_skip_logged = False
            self._lf_search_consecutive_406 = 0
        else:
            if self._lf_profile_cooldown_until:
                self._lf_profile_cooldown_until = 0.0
            self._lf_profile_cooldown_logged = False
            self._lf_profile_cooldown_skip_logged = False
            self._lf_profile_consecutive_406 = 0

    def _lf_endpoint_cooldown_remaining(self, endpoint: str, now: Optional[float] = None) -> int:
        now = self._lf_now() if now is None else now
        until = self._lf_search_cooldown_until if endpoint == "search" else self._lf_profile_cooldown_until
        return int(max(0.0, (until or 0.0) - now))

    def _lf_set_endpoint_cooldown(
        self, endpoint: str, seconds: Optional[int] = None, reason: str = "", consec: Optional[int] = None
    ) -> None:
        now = self._lf_now()
        consec_val = (
            consec
            if consec is not None
            else (self._lf_search_consecutive_406 if endpoint == "search" else self._lf_profile_consecutive_406)
        )
        if seconds is not None:
            duration = float(seconds)
        else:
            base = LF_COOLDOWN_MIN_S + max(0, consec_val - LF_COOLDOWN_CONSEC_406) * LF_COOLDOWN_STEP_S
            duration = min(LF_COOLDOWN_MAX_S, max(LF_COOLDOWN_MIN_S, base))
        current_until = self._lf_search_cooldown_until if endpoint == "search" else self._lf_profile_cooldown_until
        new_until = now + duration
        if current_until and current_until > now:
            new_until = min(now + LF_COOLDOWN_MAX_S, max(current_until, new_until))
        entering = not self._lf_endpoint_in_cooldown(endpoint, now)
        if endpoint == "search":
            self._lf_search_cooldown_until = new_until
        else:
            self._lf_profile_cooldown_until = new_until
        if entering:
            cooldown = int(max(1.0, new_until - now))
            try:
                if endpoint == "search" and not self._lf_search_cooldown_logged:
                    self.log_message.emit(
                        f"[Enricher][LF] Entering soft cooldown (search) after consecutive 406s={consec_val}; cooldown_s={cooldown}; until={new_until:.2f}"
                    )
                    self._lf_search_cooldown_logged = True
                if endpoint == "profile" and not self._lf_profile_cooldown_logged:
                    self.log_message.emit(
                        f"[Enricher][LF] Entering soft cooldown (profile) after consecutive 406s={consec_val}; cooldown_s={cooldown}; until={new_until:.2f}"
                    )
                    self._lf_profile_cooldown_logged = True
            except Exception:
                pass
            if endpoint == "search":
                self._lf_search_cooldown_skip_logged = False
            else:
                self._lf_profile_cooldown_skip_logged = False

    def _lf_mark_406(self, endpoint: str) -> None:
        if endpoint == "search":
            self._lf_search_consecutive_406 += 1
            if self._lf_search_consecutive_406 >= LF_COOLDOWN_CONSEC_406:
                self._lf_set_endpoint_cooldown("search")
        else:
            self._lf_profile_consecutive_406 += 1
            if self._lf_profile_consecutive_406 >= LF_COOLDOWN_CONSEC_406:
                self._lf_set_endpoint_cooldown("profile")

    def _lf_mark_success(self, endpoint: str) -> None:
        self._lf_clear_endpoint_cooldown(endpoint)

    def _compute_match_score_for_candidate(
        self,
        cand_artist: str,
        cand_title: str = "",
        candidate_domain: str = "",
    ) -> float:
        seed_artist = _clean_cell(getattr(self, "_live_context", {}).get("artist", ""))
        seed_title = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        if not seed_title:
            seed_title = _clean_cell(getattr(self, "_live_context", {}).get("track", ""))
        spotify_domain = _clean_cell(getattr(self, "_live_context", {}).get("spotify_domain", ""))
        return compute_match_score(
            seed_artist,
            seed_title,
            cand_artist,
            cand_title,
            spotify_domain,
            candidate_domain,
        )

    def _update_row_match_score(self, df: pd.DataFrame, row_idx, score: float) -> None:
        if "Match_Score" not in df.columns:
            return
        try:
            current = float(df.at[row_idx, "Match_Score"])
            if math.isnan(current):
                current = 0.0
        except Exception:
            current = 0.0
        cleaned = round(max(0.0, min(float(score or 0.0), 1.0)), 4)
        if cleaned > current:
            df.at[row_idx, "Match_Score"] = cleaned

    def _row_is_festival_expansion(self, row: pd.Series) -> bool:
        discovery_tier = _clean_cell(row.get("Discovery Tier", "")).strip().lower()
        expansion_parent = _clean_cell(row.get("Expansion Parent", ""))
        return discovery_tier == FESTIVAL_EXPANSION_DISCOVERY_TIER or bool(expansion_parent)

    def _row_is_festival_origin(self, row: pd.Series) -> bool:
        if self._row_is_festival_expansion(row):
            return False
        priority = _clean_cell(row.get("Seed Priority", "")).strip().lower()
        festival_sources = _clean_cell(row.get("Festival Sources", ""))
        festival_count = _clean_cell(row.get("Festival Count", ""))
        source_dir = _clean_cell(row.get("Source Directory", "")).strip().lower()
        if priority in {"festival", "festival_high"}:
            return True
        if festival_sources:
            return True
        try:
            if int(float(festival_count or "0")) > 0:
                return True
        except Exception:
            pass
        return source_dir.startswith("festival_")

    def _build_festival_expansion_row(
        self,
        parent_row: pd.Series,
        candidate_artist: str,
        origin: str,
    ) -> Dict[str, Any]:
        row = {
            "Artist Name": candidate_artist,
            "Location": "",
            "Song Title": "",
            "Sounds Like": "",
            "Social Link": "",
            "SoundCloud Link": "",
            "Played on triple J": "",
            "Played on Unearthed": "",
            "Release Date": "",
            "Primary Genre": "",
            "Date Added": datetime.date.today().isoformat(),
            "External Links": "",
            "Email": "",
            "Source Directory": "",
            "Source URL": "",
            "Seed Priority": "",
            "Expansion Parent": _clean_cell(parent_row.get("Artist Name", "")),
            "Expansion Origin": origin,
            "Discovery Tier": FESTIVAL_EXPANSION_DISCOVERY_TIER,
        }
        festival_sources = _clean_cell(parent_row.get("Festival Sources", ""))
        festival_count = _clean_cell(parent_row.get("Festival Count", ""))
        if festival_sources:
            row["Festival Sources"] = festival_sources
        if festival_count:
            row["Festival Count"] = festival_count
        return row

    def _stage_festival_expansion_candidates(
        self,
        df: pd.DataFrame,
        row_idx,
        related_artists: Iterable[str],
        origin: str,
    ) -> int:
        if not related_artists:
            return 0
        row = df.loc[row_idx]
        if not self._row_is_festival_origin(row):
            return 0
        parent_artist = _clean_cell(row.get("Artist Name", ""))
        parent_key = normalise_artist_name(parent_artist)
        if not parent_key:
            return 0
        staged = 0
        skipped_existing = 0
        extracted = 0
        for candidate in related_artists:
            if staged >= FESTIVAL_EXPANSION_MAX_RELATED_ARTISTS:
                break
            cleaned = _clean_bandcamp_related_artist_name(candidate)
            candidate_key = normalise_artist_name(cleaned)
            if not candidate_key or candidate_key == parent_key:
                continue
            extracted += 1
            if candidate_key in self._festival_expansion_existing_keys or candidate_key in self._festival_expansion_staged_keys:
                skipped_existing += 1
                continue
            self._festival_expansion_rows.append(
                self._build_festival_expansion_row(row, cleaned, origin)
            )
            self._festival_expansion_staged_keys.add(candidate_key)
            staged += 1
        self.log_message.emit(
            f"[FestivalExpansion] parent={parent_artist!r} origin={origin} "
            f"extracted={extracted} staged={staged} skipped_existing={skipped_existing}"
        )
        return staged

    def _write_festival_expansion_sidecar(self) -> None:
        if not self._festival_expansion_rows:
            return
        path = self._festival_expansion_raw_csv_path
        if not path:
            return
        try:
            expansion_df = pd.DataFrame(self._festival_expansion_rows)
            _ensure_parent_dir(path)
            expansion_df.to_csv(path, index=False, encoding="utf-8-sig")
            self.log_message.emit(
                f"[FestivalExpansion] staged rows total={len(expansion_df.index)} -> {path}"
            )
        except Exception as exc:
            self.log_message.emit(f"[FestivalExpansion] failed to write staged rows: {exc}")

    def _apply_payload_guarded(
        self,
        df: pd.DataFrame,
        row_idx,
        payload: EnrichmentPayload,
        artist_name: str,
        spotify_id: str = "",
    ) -> bool:
        if not payload:
            return False
        score = max(0.0, min(float(getattr(payload, "match_score", 0.0) or 0.0), 1.0))
        if STRICT_MATCHING and score < MATCH_THRESHOLD:
            self.log_message.emit(
                f"[Enricher] low-confidence match skipped for '{artist_name}' (Spotify ID {spotify_id or '<unknown>'}) – "
                f"score={score:.2f}, candidate={payload.summary() or '<none>'}"
            )
            return False
        self._update_row_match_score(df, row_idx, score)
        self._apply_payload(df, row_idx, payload)
        related_artists = getattr(payload, "related_artists", None) or []
        if related_artists and (payload.source_dir or "").startswith(FESTIVAL_EXPANSION_ORIGIN_BANDCAMP):
            self._stage_festival_expansion_candidates(
                df,
                row_idx,
                related_artists,
                origin=FESTIVAL_EXPANSION_ORIGIN_BANDCAMP,
            )
        return True

    def _payload_from_directory_matches(
        self,
        matches: Iterable[Dict[str, Any]],
        source: str,
    ) -> Tuple[Optional[EnrichmentPayload], Optional[Dict[str, Any]]]:
        best_row: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for row in matches:
            cand_artist = _clean_cell(row.get("Artist Name"))
            cand_title = _extract_directory_track_title(row)
            candidate_domain = extract_domain(_extract_profile_url(row) or "")
            score = self._compute_match_score_for_candidate(
                cand_artist, cand_title, candidate_domain
            )
            if score > best_score:
                best_score = score
                best_row = row
        if not best_row:
            return (None, None)
        payload = self._payload_from_directory_rows(
            [best_row],
            source,
            match_score=best_score,
            candidate_name=_clean_cell(best_row.get("Artist Name")),
        )
        return payload, best_row

    def _find_directory_matches(
        self,
        directory_index: DirectoryIndex,
        artist_key: str,
        track_key: str,
        url_candidates: Iterable[str],
    ) -> List[Dict[str, Any]]:
        if not directory_index:
            return []
        matches: List[Dict[str, Any]] = []
        if artist_key:
            name_matches = directory_index.lookup_artist(artist_key)
            if track_key:
                filtered = [row for row in name_matches if track_key in _row_track_tokens(row)]
                if filtered:
                    name_matches = filtered
            matches.extend(name_matches)
        for url in url_candidates or []:
            matches.extend(directory_index.lookup_profile_url(url))
        return _dedupe_rows(matches)

    def _payload_from_directory_rows(
        self,
        rows: Iterable[Dict[str, Any]],
        source: str,
        match_score: float = 0.0,
        candidate_name: str = "",
    ) -> Optional[EnrichmentPayload]:
        socials: Set[str] = set()
        websites: Set[str] = set()
        emails: Set[str] = set()
        link_hubs: Set[str] = set()
        source_url = ""
        for row in rows:
            row_socials, row_websites, row_emails, row_link_hubs = _extract_directory_fields(
                row, source=source
            )
            socials |= row_socials
            websites |= row_websites
            emails |= row_emails
            link_hubs |= row_link_hubs
            if not source_url:
                source_url = _extract_profile_url(row) or ""
        if not (socials or websites or emails or link_hubs):
            return None
        payload = EnrichmentPayload(
            socials=socials,
            websites=websites,
            emails=emails,
            link_hubs=link_hubs,
            source_dir=source,
            source_url=source_url,
            source_detail=_format_source_display(source),
            match_score=match_score,
            candidate_name=candidate_name,
        )
        return payload

    def _apply_payload(self, df: pd.DataFrame, row_idx, payload: EnrichmentPayload) -> None:
        email_before = _row_email_summary_snapshot(df, row_idx)
        original_social_raw = df.at[row_idx, "Social Link"]
        original_sites_raw = df.at[row_idx, "External Links"]
        existing_socials = _split_pipe_cell(original_social_raw)
        existing_sites = _split_pipe_cell(original_sites_raw)
        existing_emails = _split_pipe_cell(df.at[row_idx, "Email"], is_email=True)
        new_socials = set(payload.socials)
        new_sites = set(payload.websites)
        new_emails = set(payload.emails)

        def _set_email_provenance(source_url: str, source_type: str, method: str = "regex") -> None:
            if not source_url:
                return
            if "Email_Source_URL" not in df.columns:
                df["Email_Source_URL"] = ""
            if "Email_Source_Type" not in df.columns:
                df["Email_Source_Type"] = ""
            if "Email_Extract_Method" not in df.columns:
                df["Email_Extract_Method"] = ""
            if not _coerce_directory_value(df.at[row_idx, "Email_Source_URL"]):
                df.at[row_idx, "Email_Source_URL"] = source_url
            if not _coerce_directory_value(df.at[row_idx, "Email_Source_Type"]):
                df.at[row_idx, "Email_Source_Type"] = source_type
            if not _coerce_directory_value(df.at[row_idx, "Email_Extract_Method"]):
                df.at[row_idx, "Email_Extract_Method"] = method

        if payload.link_hubs and MAX_LINK_HUB_HOPS_PER_ROW > 0:
            hops = 0
            for hub in payload.link_hubs:
                if hops >= MAX_LINK_HUB_HOPS_PER_ROW:
                    break
                hops += 1
                new_socials |= _scrape_link_hub_socials(self.session, hub)
        socials_all = existing_socials | new_socials
        sites_all = existing_sites | new_sites
        emails_all = existing_emails | new_emails
        if socials_all:
            ordered_socials = sorted(socials_all, key=_social_sort_key)
            ordered_socials = _prioritise_facebook_first(ordered_socials)
            df.at[row_idx, "Social Link"] = MULTI_VALUE_SEPARATOR.join(ordered_socials)
        elif original_social_raw:
            # Drop placeholder noise socials when nothing useful remains.
            df.at[row_idx, "Social Link"] = ""
        if sites_all:
            ordered_sites = sorted(sites_all, key=_website_sort_key)
            if MAX_WEBSITES:
                ordered_sites = ordered_sites[:MAX_WEBSITES]
            df.at[row_idx, "External Links"] = MULTI_VALUE_SEPARATOR.join(ordered_sites)
        elif original_sites_raw:
            df.at[row_idx, "External Links"] = ""
        if emails_all:
            df.at[row_idx, "Email"] = MULTI_VALUE_SEPARATOR.join(sorted(emails_all))
            provenance_url = payload.source_url or ""
            provenance_type = payload.source_dir or (payload.source_detail or "cross_directory_enricher")
            _set_email_provenance(provenance_url, provenance_type, method="regex")
        if (
            payload.source_dir
            and payload.source_dir.startswith("bandcamp")
            and payload.source_url
            and "Bandcamp_URL" in df.columns
        ):
            canonical_bc = _canonicalise_bandcamp_url(payload.source_url)
            if canonical_bc and "bandcamp.com" in canonical_bc.lower():
                current_bc = _coerce_directory_value(df.at[row_idx, "Bandcamp_URL"])
                if not current_bc:
                    df.at[row_idx, "Bandcamp_URL"] = canonical_bc
        if (
            payload.source_dir
            and payload.source_dir.startswith("soundcloud")
            and payload.source_url
            and "SoundCloud Link" in df.columns
        ):
            # Persist the matched SoundCloud profile link when the seed is missing it.
            current_sc = _coerce_directory_value(df.at[row_idx, "SoundCloud Link"])
            if not current_sc:
                df.at[row_idx, "SoundCloud Link"] = payload.source_url
        if payload.source_dir:
            current_raw = _clean_cell(df.at[row_idx, "Source Directory"]) or ""
            current_key = _canonical_source_key(current_raw)
            current_priority = SOURCE_PRIORITY.get(current_key, 999)
            candidate_key = payload.source_dir
            candidate_priority = SOURCE_PRIORITY.get(candidate_key, 999)
            if not current_key or candidate_priority < current_priority:
                display_value = payload.source_detail or _format_source_display(candidate_key)
                source_url = payload.source_url or ""
                if candidate_key.startswith("bandcamp"):
                    canonical = _canonicalise_bandcamp_url(source_url)
                    if canonical:
                        source_url = canonical
                df.at[row_idx, "Source Directory"] = display_value
                df.at[row_idx, "Source URL"] = source_url
        try:
            from pipeline_runner import record_email_summary_row_change

            record_email_summary_row_change(
                email_before,
                _row_email_summary_snapshot(df, row_idx),
            )
        except Exception:
            pass
        if new_emails and self is not None and hasattr(self, "_index_domain_email_reuse_from_row"):
            self._index_domain_email_reuse_from_row(
                df,
                row_idx,
                _clean_cell(getattr(self, "_live_context", {}).get("spotify_domain", "")),
                source_label=payload.source_detail or payload.source_dir or "",
            )

    def _apply_structured_fields(
        self,
        df: pd.DataFrame,
        row_idx,
        matches: List[Tuple[str, Dict[str, Any]]],
    ) -> bool:
        if not matches:
            return False
        updated = False

        def _current_value(column: str) -> str:
            if column not in df.columns:
                return ""
            return _coerce_directory_value(df.at[row_idx, column])

        def _set_email_provenance(source_url: str, source_type: str, method: str = "regex") -> None:
            if not source_url:
                return
            if "Email_Source_URL" not in df.columns:
                df["Email_Source_URL"] = ""
            if "Email_Source_Type" not in df.columns:
                df["Email_Source_Type"] = ""
            if "Email_Extract_Method" not in df.columns:
                df["Email_Extract_Method"] = ""
            if not _coerce_directory_value(df.at[row_idx, "Email_Source_URL"]):
                df.at[row_idx, "Email_Source_URL"] = source_url
            if not _coerce_directory_value(df.at[row_idx, "Email_Source_Type"]):
                df.at[row_idx, "Email_Source_Type"] = source_type
            if not _coerce_directory_value(df.at[row_idx, "Email_Extract_Method"]):
                df.at[row_idx, "Email_Extract_Method"] = method

        current_location = _current_value("Location")
        if not current_location:
            location_candidate = _first_directory_value(matches, "location")
            if location_candidate:
                df.at[row_idx, "Location"] = location_candidate
                updated = True

        current_genre = _current_value("Primary Genre")
        if not current_genre:
            genre_candidate = _first_directory_value(matches, "primary_genre")
            if genre_candidate:
                df.at[row_idx, "Primary Genre"] = genre_candidate
                updated = True

        release_updated = False
        current_release = _current_value("Release Date")
        current_release_date = _parse_release_date(current_release)
        best_value = current_release
        best_date = current_release_date
        fallback_string = ""
        for source, row in matches:
            for candidate in _iter_directory_field_values(row, source, "release_date"):
                if not fallback_string:
                    fallback_string = candidate
                parsed_candidate = _parse_release_date(candidate)
                if not parsed_candidate:
                    continue
                if best_date is None or parsed_candidate > best_date:
                    best_date = parsed_candidate
                    best_value = candidate
        if best_date and (current_release_date is None or best_date > current_release_date or not current_release):
            if best_value and best_value != current_release:
                df.at[row_idx, "Release Date"] = best_value
                release_updated = True
        elif not current_release and fallback_string:
            df.at[row_idx, "Release Date"] = fallback_string
            release_updated = True
        if release_updated:
            updated = True
        return updated

    def _live_lookup(
        self,
        artist_name: str,
        skip_soundcloud: bool = False,
        skip_bandcamp: bool = False,
        skip_lastfm: bool = False,
    ) -> Optional[EnrichmentPayload]:
        if not artist_name:
            return None
        self._bc_reset_row_stats()
        if skip_bandcamp:
            self._last_bc_row_stats.update(
                {
                    "status": "skipped_existing_url",
                    "mode": "skip",
                    "attempts": self._last_bc_row_stats.get("attempts", 0),
                    "http_403": self._last_bc_row_stats.get("http_403", 0),
                }
            )
            self._set_platform_state("bandcamp", "skipped")
        if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
            if not self._notified_limit:
                self.log_message.emit(
                    "[Enricher] Live search limit reached; skipping live lookups"
                )
                self._notified_limit = True
            if not skip_bandcamp:
                self._last_bc_row_stats.update({"status": "skipped_live_limit", "mode": "skip"})
            return None
        best_payload: Optional[EnrichmentPayload] = None
        sources: Tuple[str, ...] = ("bandcamp", "soundcloud", "lastfm")
        adaptive_enabled = bool(getattr(self, "_live_lookup_bclf_adaptive_enabled", False))
        if adaptive_enabled and not skip_bandcamp and not skip_lastfm:
            ordered_bc_lf = self._live_lookup_bclf_order()
            sources = (ordered_bc_lf[0], "soundcloud", ordered_bc_lf[1])
        elif skip_bandcamp:
            sources = ("soundcloud", "lastfm")
        for source in sources:
            payload = None
            if source == "bandcamp":
                if adaptive_enabled:
                    self._record_live_lookup_bclf_attempt("bandcamp")
                payload = self._live_search_bandcamp(artist_name)
            elif source == "soundcloud":
                if skip_soundcloud or getattr(self, "night_mode", False):
                    continue
                payload = self._live_search_soundcloud(artist_name)
            elif source == "lastfm":
                if skip_lastfm:
                    if getattr(self, "_row_enrichment_state", {}).get("lastfm") == "pending":
                        self._set_platform_state("lastfm", "skipped")
                    continue
                lf_search_skipped_before = int(getattr(self, "_lf_search_skipped_cooldown", 0) or 0)
                lf_profile_skipped_before = int(getattr(self, "_lf_profile_skipped_cooldown", 0) or 0)
                if adaptive_enabled:
                    self._record_live_lookup_bclf_attempt("lastfm")
                payload = self._live_search_lastfm(artist_name)
                if adaptive_enabled and (
                    int(getattr(self, "_lf_search_skipped_cooldown", 0) or 0) > lf_search_skipped_before
                    or int(getattr(self, "_lf_profile_skipped_cooldown", 0) or 0) > lf_profile_skipped_before
                ):
                    self._record_live_lookup_bclf_cooldown("lastfm")
            if payload:
                current_best = getattr(best_payload, "match_score", 0.0) if best_payload else 0.0
                candidate_score = getattr(payload, "match_score", 0.0) or 0.0
                if candidate_score > current_best:
                    best_payload = payload
                elif best_payload is None:
                    best_payload = payload
        return best_payload

    def _increment_live_counter(self) -> bool:
        if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
            if not self._notified_limit:
                self.log_message.emit(
                    "[Enricher] Live search limit reached; remaining rows skip live lookup."
                )
                self._notified_limit = True
            return False
        self.live_search_attempts += 1
        return True

    def _live_search_bandcamp(self, artist_name: str) -> Optional[EnrichmentPayload]:
        # Skip if already attempted for this row
        if not self._platform_attempt_allowed("bandcamp", artist_name, "Bandcamp Enrich"):
            self._last_bc_row_stats["status"] = "skipped"
            return None
        if not self._increment_live_counter():
            self._set_platform_state("bandcamp", "skipped")
            self._last_bc_row_stats["status"] = "skipped_live_limit"
            return None

        song_title = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        location_hint = _clean_cell(getattr(self, "_live_context", {}).get("location", ""))
        primary_genre_hint = _clean_cell(getattr(self, "_live_context", {}).get("genre", ""))

        search_allowed = BC_ENABLE_SEARCH_ENDPOINT and not self._bc_should_skip_search()
        debug_attempts = bool(os.getenv("BC_DEBUG_ATTEMPTS"))
        if debug_attempts:
            try:
                bc_index = getattr(self, "_directory_indexes", {}).get("bandcamp") if hasattr(self, "_directory_indexes") else None
                self.log_message.emit(
                    "[BC Debug] live_search_bandcamp: bandcamp_csv_path_set=%s index_artists=%s "
                    "search_allowed=%s breaker_tripped=%s attempts=%d"
                    % (
                        bool(getattr(self, "bandcamp_csv_path", "")),
                        bc_index.unique_artist_count() if bc_index else 0,
                        search_allowed,
                        self._bc_search_breaker_tripped,
                        self._last_bc_row_stats.get("attempts", 0),
                    )
                )
            except Exception:
                pass

        # 1) Directory-based fallback (discover substitute, no network)
        directory_payload = self._bc_directory_fallback(artist_name, song_title)
        if directory_payload:
            self._set_platform_state("bandcamp", "matched")
            self._last_bc_row_stats.update(
                {
                    "status": "fallback_ok",
                    "mode": "directory_discover",
                    "attempts": self._last_bc_row_stats.get("attempts", 0),
                }
            )
            self._bc_matches += 1
            self.log_message.emit(
                f"[Enricher] Bandcamp: status=fallback_ok mode=directory_discover attempts={self._last_bc_row_stats.get('attempts', 0)} 403={self._last_bc_row_stats.get('http_403', 0)} artist='{artist_name}'"
            )
            return directory_payload

        # 2) Optional /search (breaker only applies here)
        best_payload: Optional[EnrichmentPayload] = None
        best_score = 0.0
        best_rank_score = 0.0
        best_match_score = 0.0
        if search_allowed:
            queries = build_bandcamp_queries(artist_name, song_title)

            def _search(query: str) -> Tuple[Optional[EnrichmentPayload], float, float, Optional[int]]:
                quoted = urllib.parse.quote_plus(query)
                url = f"https://bandcamp.com/search?q={quoted}&item_type=b"
                html, status = self._bc_http_get(url, label="Bandcamp search", count_breaker=True)
                if status == 403:
                    self._bc_search_breaker_tripped = True
                    if not self._bc_search_breaker_reason:
                        self._bc_search_breaker_reason = "http_403"
                    return (None, 0.0, 0.0, status)
                if self._bc_search_breaker_tripped:
                    return (None, 0.0, 0.0, status)
                if not html:
                    return (None, 0.0, 0.0, status)
                soup = BeautifulSoup(html, "html.parser")
                first_link = soup.select_one("li.searchresult a.itemurl, li.searchresult a[href*='bandcamp.com']")
                if not first_link:
                    return (None, 0.0, 0.0, status)
                display_name = ""
                parent_li = first_link.find_parent("li")
                if parent_li:
                    name_el = parent_li.select_one(".heading") or parent_li.select_one("div.heading")
                    if name_el:
                        display_name = name_el.get_text(" ", strip=True)
                    if not display_name:
                        display_name = parent_li.get_text(" ", strip=True)
                if not display_name:
                    display_name = first_link.get_text(" ", strip=True)
                profile_url = (first_link.get("href") or "").strip()
                if not profile_url:
                    return (None, 0.0, 0.0, status)
                artist_norm = normalize_name(artist_name)
                bc_name_norm = normalize_name(display_name)
                if artist_norm and bc_name_norm:
                    prefix_artist = artist_norm.split()[0] if artist_norm.split() else ""
                    prefix_bc = bc_name_norm.split()[0] if bc_name_norm.split() else ""
                    prefix_match = (
                        prefix_artist
                        and prefix_bc
                        and (prefix_artist.startswith(prefix_bc[:4]) or prefix_bc.startswith(prefix_artist[:4]))
                    )
                    if not (
                        artist_norm == bc_name_norm
                        or artist_norm in bc_name_norm
                        or bc_name_norm in artist_norm
                        or prefix_match
                    ):
                        return (None, 0.0, 0.0, status)
                confidence = _bandcamp_confidence(
                    artist_name, display_name, profile_url, song_title=song_title if query != artist_name else ""
                )
                rank_confidence = _locale_rank_score(confidence, display_name, profile_url)
                payload: Optional[EnrichmentPayload] = None
                if confidence >= MIN_BC_CONFIDENCE:
                    payload = self._fetch_profile_and_build(profile_url, "bandcamp", confidence=confidence)
                    if payload:
                        payload.match_score = self._compute_match_score_for_candidate(
                            display_name or profile_url,
                            song_title,
                            extract_domain(profile_url),
                        )
                        payload.candidate_name = display_name or ""
                return (payload, confidence, rank_confidence, status)

            for idx, query in enumerate(queries):
                payload, confidence, rank_confidence, status_code = _search(query)
                candidate_match = getattr(payload, "match_score", 0.0) if payload else 0.0
                if payload and (
                    candidate_match > best_match_score
                    or confidence > best_score
                    or (
                        abs(confidence - best_score) <= 0.02
                        and rank_confidence > best_rank_score
                    )
                ):
                    best_payload = payload
                    best_score = confidence
                    best_rank_score = rank_confidence
                    best_match_score = candidate_match
                else:
                    best_score = max(best_score, confidence)
                    best_rank_score = max(best_rank_score, rank_confidence)
                if status_code == 403 or self._bc_search_breaker_tripped:
                    break
                if best_payload and best_score >= 0.95 and '"' in query:
                    break
                if idx < len(queries) - 1:
                    self._bc_gap()

        if best_payload:
            self._set_platform_state("bandcamp", "matched")
            self._last_bc_row_stats.update(
                {"status": "ok", "mode": "search", "attempts": self._last_bc_row_stats.get("attempts", 0)}
            )
            self._bc_matches += 1
            self.log_message.emit(
                f"[Enricher] Bandcamp: status=ok mode=search attempts={self._last_bc_row_stats.get('attempts', 0)} 403={self._last_bc_row_stats.get('http_403', 0)} artist='{artist_name}'"
            )
            return best_payload

        # If discover/search failed, try slug fallback (bounded).
        fallback_payload = None
        if self._bc_fallback_used < BC_FALLBACK_MAX_PER_RUN:
            fallback_payload = self._bc_slug_fallback(artist_name, song_title)
            if fallback_payload:
                self._bc_fallback_matches += 1
        if fallback_payload:
            self._set_platform_state("bandcamp", "matched")
            self._last_bc_row_stats.update(
                {"status": "fallback_ok", "mode": "fallback_guess", "attempts": self._last_bc_row_stats.get("attempts", 0)}
            )
            self._bc_matches += 1
            self.log_message.emit(
                f"[Enricher] Bandcamp: status=fallback_ok mode=fallback_guess attempts={self._last_bc_row_stats.get('attempts', 0)} 403={self._last_bc_row_stats.get('http_403', 0)} artist='{artist_name}'"
            )
            return fallback_payload

        # Record breaker log once if tripped during this row.
        if self._bc_search_breaker_tripped and self._bc_search_breaker_reason:
            if not self._bc_breaker_logged:
                self.log_message.emit(
                    f"[Enricher] Bandcamp: circuit breaker engaged (reason={self._bc_search_breaker_reason}, attempts={self._bc_search_attempts}, 403s={self._bc_total_403}). Skipping further BC searches this run."
                )
                self._bc_breaker_logged = True

        best_confidence_display = max(best_score, best_rank_score)
        status_label = "blocked_403" if (search_allowed and (self._bc_consecutive_403 or self._bc_total_403)) else "no_match"
        mode_label = "directory_discover"
        if search_allowed and self._last_bc_row_stats.get("attempts", 0) > 0:
            mode_label = "search"
        self._last_bc_row_stats.update(
            {
                "status": status_label if status_label != "blocked_403" or best_confidence_display == 0 else status_label,
                "mode": mode_label,
                "attempts": self._last_bc_row_stats.get("attempts", 0),
            }
        )
        self._bc_no_match += 1
        self.log_message.emit(
            f"[Enricher] Bandcamp: status={self._last_bc_row_stats.get('status','no_match')} mode={mode_label} attempts={self._last_bc_row_stats.get('attempts', 0)} 403={self._last_bc_row_stats.get('http_403', 0)} artist='{artist_name}'"
        )
        self._set_platform_state("bandcamp", "skipped")
        return None

    def _bc_slug_fallback(self, artist_name: str, song_title: str) -> Optional[EnrichmentPayload]:
        """
        Conservative fallback: test a few band subdomain guesses and verify with confidence gate.
        """
        slugs = _bc_slug_candidates(artist_name)
        if not slugs:
            return None
        used = 0
        for slug in slugs:
            if self._bc_fallback_used >= BC_FALLBACK_MAX_PER_RUN:
                break
            used += 1
            self._bc_fallback_used += 1
            url = f"https://{slug}.bandcamp.com/"
            html, status = self._bc_http_get(url, label="Bandcamp slug", count_breaker=False)
            if status == 403:
                continue
            if status and status != 200:
                continue
            title_text = ""
            if html:
                try:
                    soup = BeautifulSoup(html, "html.parser")
                    title_el = soup.find("title")
                    if title_el:
                        title_text = title_el.get_text(" ", strip=True)
                    if not title_text:
                        h1 = soup.find("h1")
                        if h1:
                            title_text = h1.get_text(" ", strip=True)
                except Exception:
                    title_text = ""
            display_name = title_text or slug
            confidence = _bandcamp_confidence(artist_name, display_name, url, song_title=song_title)
            if confidence >= MIN_BC_CONFIDENCE:
                payload = self._fetch_profile_and_build(url, "bandcamp", confidence=confidence)
                if payload:
                    payload.match_score = self._compute_match_score_for_candidate(
                        display_name or url,
                        song_title,
                        extract_domain(url),
                    )
                    payload.candidate_name = display_name or ""
                    return payload
        return None

    def _start_night_sc_attempt(self) -> _NightSCAttempt:
        global _NIGHT_SC_PIPELINE_LOGGED
        if getattr(self, "night_mode", False) and not _NIGHT_SC_PIPELINE_LOGGED:
            try:
                self.log_message.emit(
                    "[Night SC] using day-mode candidate pipeline: SoundCloudEngine.people_search_candidates_v2 + HTML fallback"
                )
            except Exception:
                pass
            _NIGHT_SC_PIPELINE_LOGGED = True
        attempt = _NightSCAttempt(
            start_time=time.time(),
            max_seconds=_night_sc_budget_seconds(),
            max_fetches=_night_sc_max_fetches(),
        )
        self._active_night_sc_attempt = attempt
        return attempt

    def _night_mode_soundcloud_search(self, artist_name: str) -> Tuple[Optional[EnrichmentPayload], Optional[_NightSCAttempt]]:
        if not getattr(self, "night_mode", False):
            return self._live_search_soundcloud(artist_name), None
        attempt = self._start_night_sc_attempt()
        try:
            payload = self._live_search_soundcloud(artist_name)
            return payload, attempt
        finally:
            self._active_night_sc_attempt = None

    def _night_sc_http_get(self, url: str, label: str, attempt: _NightSCAttempt) -> Tuple[Optional[int], str]:
        if getattr(self, "_sc_live_enrich_disabled", False):
            attempt.challenge = True
            attempt.reason = attempt.reason or "breaker_tripped"
            return (None, "")
        if getattr(self, "_sc_rss_only_mode", False):
            attempt.reason = attempt.reason or "rss_only_mode"
            self._sc_rss_only_engine_fetch_skips += 1
            try:
                self.log_message.emit(
                    "[Night SC] rss_only=1 -> skipping engine/profile fetch label=%s" % (label,)
                )
            except Exception:
                pass
            return (None, "")
        if not attempt.note_fetch():
            return (None, "")
        debug = os.getenv("NIGHT_SC_DEBUG")
        resp = None
        status: Optional[int] = None
        text: str = ""
        blocked = False
        challenge = False

        def _record_challenge(hit: bool) -> None:
            try:
                if not hasattr(self, "_night_sc_challenge_streak"):
                    self._night_sc_challenge_streak = 0
                if hit:
                    self._sc_record_html_challenge()
                else:
                    self._night_sc_challenge_streak = 0
                    self._sc_last_challenge_at = 0.0
            except Exception:
                pass

        def _log_debug(body: str) -> None:
            if not debug:
                return
            try:
                parsed = urlparse(url)
                host_path = f"{parsed.netloc}{parsed.path}"
                snippet = re.sub(r"\\s+", " ", body or "")[:80]
                self.log_message.emit(
                    f"[Night SC][debug] label={label} url={host_path} status={status} len={len(body or '')} blocked={blocked} challenge={challenge} body=\"{snippet}\""
                )
            except Exception:
                pass

        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT)
            status = getattr(resp, "status_code", None)
            text = getattr(resp, "text", "") or ""
            attempt.http_status = status
            blocked = _sc_is_blocked(status, text)
            challenge = _sc_is_challenge_page(text)
            if status == 403:
                attempt.saw_403 = True
                _log_debug(text)
                _record_challenge(False)
                return (status, "")
            if blocked:
                attempt.saw_403 = True
                _log_debug(text)
                _record_challenge(False)
                return (status, "")
            if challenge:
                attempt.challenge = True
                attempt.reason = attempt.reason or "challenge_page"
                _log_debug(text)
                _record_challenge(True)
                return (status, "")
            if status == 200 and not text.strip():
                attempt.reason = attempt.reason or "empty_body"
                _log_debug(text)
                _record_challenge(False)
                return (status, "")
            resp.raise_for_status()
            _log_debug(text)
            _record_challenge(False)
            return (status, text)
        except Exception:
            if attempt.reason == "":
                attempt.reason = "http_error"
            if status is None:
                status = attempt.http_status
            else:
                attempt.http_status = attempt.http_status or status
            _log_debug("")
            _record_challenge(False)
            return (attempt.http_status, "")

    def _night_sc_search_candidates(
        self,
        artist_name: str,
        sc_query: str,
        location_hint: str,
        place_hint: str,
        genre_hint: str,
        track_hint: str,
        attempt: _NightSCAttempt,
        country_hint: str = "",
    ) -> Optional[Dict[str, Any]]:
        best_candidate: Optional[Dict[str, Any]] = None
        candidate_source = "none"

        def _is_better_candidate(candidate: Dict[str, Any], current: Optional[Dict[str, Any]]) -> bool:
            if not candidate:
                return False
            cand_score = candidate.get("score", 0)
            cand_rank = candidate.get("rank_score", cand_score)
            curr_score = current.get("score", 0) if current else 0
            curr_rank = current.get("rank_score", curr_score) if current else 0
            if cand_score > curr_score:
                return True
            if curr_score > cand_score + 0.02:
                return False
            return cand_rank > curr_rank

        def _is_long_or_spammy_name(name: str) -> Tuple[bool, Tuple[int, int, float]]:
            cleaned = name or ""
            token_count = len(re.findall(r"[A-Za-z0-9]+", cleaned))
            char_len = len(cleaned.strip())
            alnum_chars = len(re.findall(r"[A-Za-z0-9]", cleaned))
            digit_count = len(re.findall(r"[0-9]", cleaned))
            digit_ratio = float(digit_count) / float(max(1, alnum_chars))
            flag = char_len >= 60 or token_count >= 10 or digit_ratio >= 0.25
            return flag, (char_len, token_count, digit_ratio)

        # Prefer v2 API people search (shared with seed scraper); fall back to HTML only when empty/error.
        alias_map = {
            "uk": "United Kingdom",
            "usa": "United States",
            "us": "United States",
            "uae": "United Arab Emirates",
        }

        def _map_place(token: str) -> Optional[str]:
            cleaned = (token or "").strip()
            if not cleaned:
                return None
            alias_key = re.sub(r"[^a-z]", "", cleaned.lower())
            mapped = alias_map.get(alias_key)
            return mapped or cleaned

        api_place: Optional[str] = None
        source_tag = "none"
        country_clean = (country_hint or "").strip()
        location_clean = (location_hint or "").strip()

        if country_clean:
            mapped = _map_place(country_clean)
            if mapped and len(mapped) >= 3:
                api_place = mapped
                source_tag = "country"

        if api_place is None and location_clean:
            if "," in location_clean:
                rhs = location_clean.split(",")[-1]
                mapped = _map_place(rhs)
                if mapped and len(mapped) >= 3:
                    api_place = mapped
                    source_tag = "location_derived"
            if api_place is None:
                mapped = _map_place(location_clean)
                if mapped and len(mapped) >= 3:
                    api_place = mapped
                    source_tag = "location_raw"

        if api_place is None or len(api_place) < 3:
            api_place = None
            source_tag = "none"

        self.log_message.emit(f"[Night SC] API people search place='{api_place or ''}' source={source_tag}")
        try:
            api_candidates = _SC_SHARED_ENGINE.people_search_candidates_v2(
                artist_name, api_place, max_results=12
            )
            self.log_message.emit(
                f"[Night SC] API people search -> handles={len(api_candidates)} query='{artist_name}' place='{api_place or ''}'"
            )
            if not api_candidates and api_place:
                try:
                    self.log_message.emit(
                        f"[Night SC] API people search returned 0 handles for place='{api_place}'; retrying without place filter."
                    )
                except Exception:
                    pass
                retry_candidates = _SC_SHARED_ENGINE.people_search_candidates_v2(
                    artist_name, None, max_results=12
                )
                self.log_message.emit(
                    f"[Night SC] API people search -> handles={len(retry_candidates)} query='{artist_name}' place=''"
                )
                if retry_candidates:
                    api_candidates = retry_candidates
            if api_candidates:
                candidate_source = "api"
                candidate = self._pick_best_soundcloud_candidate(
                    artist_name, api_candidates, location_hint, genre_hint
                )
                if candidate:
                    attempt.candidate_source = candidate_source
                    return candidate
                self.log_message.emit("[Night SC] API people search produced handles but no acceptable candidate; falling back to HTML search.")
            else:
                self.log_message.emit("[Night SC] API people search returned 0 handles; falling back to HTML search.")
        except Exception as exc:
            self.log_message.emit(f"[Night SC] API people search error ({exc}); falling back to HTML search.")

        if getattr(self, "_sc_rss_only_mode", False):
            attempt.candidate_source = candidate_source
            return best_candidate

        long_flag, long_metrics = _is_long_or_spammy_name(artist_name)
        if not api_candidates and long_flag:
            char_len, token_count, digit_ratio = long_metrics
            try:
                self.log_message.emit(
                    f"[Night SC] Skipping HTML fallback for long/spammy artist name (len={char_len}, tokens={token_count}, digit_ratio={digit_ratio:.2f})."
                )
            except Exception:
                pass
            attempt.reason = attempt.reason or "no_candidates_long_name"
            attempt.candidate_source = candidate_source
            return None

        for query in _build_soundcloud_queries(sc_query, track_hint, location_hint):
            url = f"https://soundcloud.com/search/people?q={urllib.parse.quote_plus(query)}"
            status, html = self._night_sc_http_get(url, "Night SC search", attempt)
            if not html:
                if attempt.challenge or attempt.saw_403 or attempt.budget_exceeded:
                    break
                continue
            if attempt.budget_exceeded:
                break
            candidates = self._parse_soundcloud_search_results(html, url)
            if candidates and candidate_source == "none":
                candidate_source = "html"
            candidate = self._pick_best_soundcloud_candidate(
                artist_name, candidates, location_hint, genre_hint
            )
            if candidate and _is_better_candidate(candidate, best_candidate):
                best_candidate = candidate
            if best_candidate and best_candidate.get("score", 0) >= _SC_CONFIDENCE_ACCEPT:
                break
        if (not best_candidate or best_candidate.get("score", 0) < _SC_CONFIDENCE_MIN) and attempt.budget_ok():
            url = f"https://soundcloud.com/search?q={urllib.parse.quote_plus(sc_query)}"
            status, html = self._night_sc_http_get(url, "Night SC universal search", attempt)
            if not html:
                attempt.candidate_source = candidate_source
                return best_candidate
            if not attempt.budget_exceeded:
                candidates = self._parse_soundcloud_search_results(html, url)
                if candidates and candidate_source == "none":
                    candidate_source = "html"
                candidate = self._pick_best_soundcloud_candidate(
                    artist_name, candidates, location_hint, genre_hint
                )
                if candidate and _is_better_candidate(candidate, best_candidate):
                    best_candidate = candidate
        attempt.candidate_source = candidate_source
        return best_candidate

    def _night_sc_fetch_profile_payload(
        self,
        profile_url: str,
        attempt: _NightSCAttempt,
    ) -> Tuple[Optional[EnrichmentPayload], bool]:
        # In RSS-only mode we must avoid any profile fetching that could trigger challenges.
        if getattr(self, "_sc_rss_only_mode", False):
            attempt.profile_source = "rss_only"
            attempt.challenge = False
            attempt.reason = attempt.reason or "rss_only_mode"
            self._sc_rss_only_engine_fetch_skips += 1
            try:
                self.log_message.emit(
                    "[Night SC] rss_only=1 -> skipping profile fetch url=%s" % (profile_url or "<missing>",)
                )
            except Exception:
                pass
            return (None, False)
        handle = _sc_handle_from_profile_url(profile_url) if profile_url else ""
        engine_enabled = _night_sc_engine_enabled(getattr(self, "night_mode", False)) and handle
        engine_failed = False
        data: Optional[Dict[str, Any]] = None
        if engine_enabled:
            if not attempt.note_fetch():
                return (None, False)
            try:
                data = _SC_SHARED_ENGINE.fetch_profile(handle) or {}
            except Exception as exc:
                engine_failed = True
                try:
                    self.log_message.emit(f"[Night SC] Engine fetch_profile error ({exc}); falling back to legacy HTML.")
                except Exception:
                    pass
                data = None
            if data is None and not engine_failed:
                engine_failed = True
            if data is not None:
                attempt.profile_source = "engine"
                status = data.get("status", "")
                if status == "non_actionable_challenge":
                    attempt.challenge = True
                    attempt.reason = attempt.reason or "challenge_page"
                    self._note_sc_challenge(status, data.get("reason"), data.get("challenge_page"))
                    if getattr(self, "_sc_rss_only_mode", False):
                        pass
                    return (None, False)
                if status == "blocked_403":
                    attempt.saw_403 = True
                    # Force a consistent reason so RSS fallback triggers even if an earlier step set a different reason.
                    attempt.reason = "api_403"
                    return (None, False)
                attempt.http_status = 200
                socials: Set[str] = set()
                websites: Set[str] = set()
                emails: Set[str] = set()
                link_hubs: Set[str] = set()
                for email in data.get("emails") or []:
                    if email and isinstance(email, str):
                        emails.add(email.strip())
                for url in data.get("external_urls") or []:
                    parsed = urllib.parse.urlparse(url)
                    host = (parsed.netloc or "").lower()
                    path_lower = (parsed.path or "").lower()
                    if host.endswith("soundcloud.com"):
                        continue
                    if host in LINK_HUB_HOSTS:
                        link_hubs.add(url)
                        websites.add(url)
                        continue
                    if any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST):
                        socials.add(url)
                        continue
                    if host in JUNK_WEBSITE_HOSTS:
                        continue
                    if any(keyword in path_lower for keyword in JUNK_WEBSITE_PATH_KEYWORDS):
                        continue
                    websites.add(url)
                website = data.get("website")
                if website:
                    websites.add(website)
                payload = EnrichmentPayload(
                    socials=socials,
                    websites=websites,
                    emails=emails,
                    link_hubs=link_hubs,
                    source_dir="soundcloud",
                    source_url=profile_url or f"https://soundcloud.com/{handle}",
                    source_detail=_format_source_display("soundcloud_live"),
                )
                try:
                    self._night_sc_challenge_streak = 0
                except Exception:
                    pass
                return (payload, bool(_payload_actionable(payload)))
        if not engine_enabled or engine_failed:
            attempt.profile_source = "legacy_html"
        if getattr(self, "_sc_rss_only_mode", False):
            attempt.profile_source = attempt.profile_source or "rss_only"
            return (None, False)
        status, html = self._night_sc_http_get(profile_url, "Night SC profile", attempt)
        if attempt.challenge:
            return (None, False)
        if status == 403:
            attempt.saw_403 = True
            return (None, False)
        if not html:
            return (None, False)
        if _sc_is_challenge_page(html):
            attempt.challenge = True
            attempt.reason = attempt.reason or "challenge_page"
            return (None, False)
        socials, websites, emails, link_hubs = _extract_links_from_profile(html, "soundcloud", profile_url)
        payload = EnrichmentPayload(
            socials=socials,
            websites=websites,
            emails=emails,
            link_hubs=link_hubs,
            source_dir="soundcloud",
            source_url=profile_url,
            source_detail=_format_source_display("soundcloud_live"),
        )
        actionable = _payload_actionable(payload) or False
        return (payload, actionable)

    def _sc_build_rss_payload(
        self,
        handle: str,
        base_payload: Optional[EnrichmentPayload],
        row_idx: Optional[int] = None,
    ) -> Tuple[Optional[EnrichmentPayload], bool, bool, str]:
        if not handle:
            self._sc_record_rss_result(False, reason="missing_handle", row_idx=row_idx)
            return (None, False, False, "missing_handle")
        try:
            # Count the attempt up front so used_rss reflects every RSS try, even failures.
            sc_engine._sc_stat_inc("rss_used", 1)
        except Exception:
            pass
        rss_payload: Optional[EnrichmentPayload] = None
        available = True
        failure_reason = "rss_fail"
        def _try_fetch() -> Optional[EnrichmentPayload]:
            nonlocal available
            nonlocal failure_reason
            try:
                uid = sc_engine._sc_resolve_handle_uid(_SC_SHARED_ENGINE.session, handle)
                if not uid:
                    available = False
                    failure_reason = "rss_unavailable"
                client_id = sc_engine._sc_get_client_id(_SC_SHARED_ENGINE.session)
                track = sc_engine._sc_fetch_latest_track_metadata(_SC_SHARED_ENGINE.session, client_id, uid, handle)
                if not track:
                    track = sc_engine._sc_fetch_latest_track_rss(_SC_SHARED_ENGINE.session, uid, handle)
                    if track and track.get("permalink_url"):
                        track.setdefault("source", "rss")
                flags = _SC_SHARED_ENGINE.get_run_flags() if "_SC_SHARED_ENGINE" in globals() else {}
                if not track and (flags.get("tracks_api_blocked") or flags.get("root_fetch_disabled")):
                    failure_reason = "blocked_api"
                permalink = track.get("permalink_url") or "" if track else ""
                if track:
                    websites = set(getattr(base_payload, "websites", set()) if base_payload else set())
                    if permalink:
                        websites.add(permalink)
                    payload = base_payload or EnrichmentPayload(
                        socials=set(),
                        websites=websites,
                        emails=set(),
                        link_hubs=set(),
                        source_dir="soundcloud",
                        source_url=f"https://soundcloud.com/{handle}",
                        source_detail=_format_source_display("soundcloud_live"),
                    )
                    payload.websites = websites
                    return payload
            except Exception:
                return None
            return None

        rss_payload = _try_fetch()
        if not rss_payload and available:
            time.sleep(0.35)  # small backoff before one retry
            rss_payload = _try_fetch()
        success = bool(rss_payload and (_payload_actionable(rss_payload) or rss_payload.websites))
        if not success and available and failure_reason == "rss_fail":
            # If we reached here, feed exists but empty / non-actionable.
            failure_reason = "rss_empty"
        self._sc_record_rss_result(success, reason=failure_reason, row_idx=row_idx)
        return (rss_payload if success else None, success, available, failure_reason)

    def _night_sc_attempt_row(
        self,
        df: pd.DataFrame,
        row_idx,
        artist_name: str,
        spotify_id: str = "",
    ) -> bool:
        global _SC_TRACKS_API_FALLBACK_LOGGED
        try:
            self._sc_rows_seen += 1
            if getattr(self, "_sc_rss_only_mode", False):
                self._sc_rss_only_rows += 1
                self._sc_maybe_exit_rss_only(row_idx=row_idx)
        except Exception:
            pass
        if self._sc_in_live_cooldown():
            try:
                cooldown_left = int(max(1.0, self._sc_live_disabled_until - time.time()))
                self.log_message.emit(
                    f"[Night SC] Live enrichment cooldown active ({cooldown_left}s remaining); skipping SC for '{artist_name}'."
                )
            except Exception:
                pass
            return False
        attempt = self._start_night_sc_attempt()
        sc_link = _coerce_directory_value(df.at[row_idx, "SoundCloud Link"]) if "SoundCloud Link" in df.columns else ""
        song_title = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        sc_query = _clean_soundcloud_query(build_search_query(artist_name, song_title))
        location_hint = _clean_cell(getattr(self, "_live_context", {}).get("location", ""))
        country_hint = _clean_cell(df.at[row_idx, "Country_Derived"]) if "Country_Derived" in df.columns else _clean_cell(df.at[row_idx, "Country"]) if "Country" in df.columns else ""
        place_hint = country_hint or location_hint
        track_hint = _clean_cell(getattr(self, "_live_context", {}).get("track", ""))
        genre_hint = _clean_cell(getattr(self, "_live_context", {}).get("genre", ""))

        # Per-row guards for RSS-first and single fallback.
        sc_rss_first_attempted = False
        sc_fallback_used = False

        # Force RSS-only path: skip HTML/profile fetches and try RSS directly for every row when enabled.
        if getattr(self, "_sc_rss_only_mode", False):
            attempt.challenge = False
            attempt.reason = attempt.reason or "rss_only_mode"
            attempt.profile_source = "rss_only"
            handle = ""
            profile_url = ""

            # Prefer chosen handle from API people search; avoid HTML fallback entirely.
            if sc_query:
                best_candidate = self._night_sc_search_candidates(
                    artist_name,
                    sc_query,
                    location_hint,
                    place_hint,
                    genre_hint,
                    track_hint,
                    attempt,
                    country_hint=country_hint,
                )
                if best_candidate:
                    handle = best_candidate.get("handle") or _sc_handle_from_profile_url(best_candidate.get("profile_url") or "") or ""
                    profile_url = best_candidate.get("profile_url") or profile_url
                    attempt.confidence = float(best_candidate.get("score", 0.0) or 0.0)
                    attempt.match_score = attempt.confidence

            # If still empty, fall back to explicit link/handle on the row.
            if not handle and sc_link and "soundcloud.com" in sc_link.lower():
                profile_url = _normalise_url(sc_link) or sc_link
                handle = _sc_handle_from_profile_url(profile_url) or ""

            attempt.handle = handle
            attempt.profile_url = profile_url or (handle and f"https://soundcloud.com/{handle}") or ""

            payload: Optional[EnrichmentPayload] = None
            rss_ok = False
            rss_attempted = bool(handle)

            if handle:
                # Always attempt RSS when a handle is available; _sc_build_rss_payload increments rss_used even on failure.
                rss_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(handle, None, row_idx=row_idx)
                payload = rss_payload
                if rss_ok:
                    attempt.status = "rss_success"
                    attempt.reason = "rss_success"
                else:
                    attempt.status = "rss_unavailable" if not rss_available else "rss_fail"
                    attempt.reason = attempt.status
            else:
                attempt.status = "rss_unavailable"
                attempt.reason = "rss_unavailable"

            try:
                flags = _SC_SHARED_ENGINE.get_run_flags()
            except Exception:
                flags = {}
            rss_used_total = _sc_get_rss_used_total()
            flags_used_rss = int(flags.get("used_rss", 0))
            try:
                self.log_message.emit(
                    "[Night SC] rss_only=1 handle=%s url=%s rss_attempted=%d outcome=%s flags_used_rss=%d rss_used_total=%d"
                    % (
                        attempt.handle or handle or "<missing>",
                        attempt.profile_url or "",
                        int(rss_attempted),
                        attempt.status,
                        flags_used_rss,
                        int(rss_used_total or 0),
                    )
                )
            except Exception:
                pass

            if payload:
                payload.match_score = payload.match_score or attempt.match_score or 1.0
                payload.candidate_name = artist_name
                applied = self._apply_payload_guarded(df, row_idx, payload, artist_name, spotify_id=spotify_id)
            else:
                applied = False

            self._finalize_night_sc(df, row_idx, attempt, payload if applied else None, artist_name)
            return bool(applied)
        # If a seed SoundCloud Link is present, prefer it and skip search.
        if sc_link and "soundcloud.com" in sc_link.lower():
            attempt.profile_url = _normalise_url(sc_link) or sc_link
            attempt.handle = _sc_handle_from_profile_url(attempt.profile_url) or attempt.handle
            cached = self._night_sc_cache_lookup(attempt.handle, attempt.profile_url)
            if cached:
                attempt.cached_snapshot = cached
                attempt.cached_payload = _payload_from_snapshot(cached.get("payload"))
                attempt.status = "skipped_already_attempted"
                attempt.reason = cached.get("reason") or "cache_hit"
                attempt.http_status = cached.get("http", attempt.http_status)
                attempt.fetches = cached.get("fetches", attempt.fetches)
                attempt.confidence = float(cached.get("confidence", attempt.confidence or 0.0))
                attempt.match_score = float(cached.get("match_score") or 0.0)
                applied = self._apply_sc_snapshot_to_row(df, row_idx, cached, artist_name, spotify_id=spotify_id)
                self._finalize_night_sc(df, row_idx, attempt, attempt.cached_payload if applied else None, artist_name)
                return bool(applied)
            if not attempt.budget_ok():
                attempt.budget_exceeded = True
                attempt.status = "no_confident_match"
                attempt.reason = "budget_exceeded"
                self._finalize_night_sc(df, row_idx, attempt, None, artist_name)
                return False
            payload: Optional[EnrichmentPayload] = None
            actionable = False

            handle_for_rss = attempt.handle or _sc_handle_from_profile_url(attempt.profile_url or "") or ""
            if handle_for_rss:
                sc_rss_first_attempted = True
                attempt.profile_source = attempt.profile_source or "rss_first"
                rss_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(handle_for_rss, None, row_idx=row_idx)
                if rss_ok:
                    attempt.status = "rss_success"
                    attempt.reason = "rss_success"
                else:
                    attempt.status = "rss_unavailable" if not rss_available else "rss_fail"
                    attempt.reason = attempt.status
                try:
                    flags = _SC_SHARED_ENGINE.get_run_flags()
                except Exception:
                    flags = {}
                rss_used_total = _sc_get_rss_used_total()
                flags_used_rss = int(flags.get("used_rss", 0))
                try:
                    self.log_message.emit(
                        "[Night SC] rss_first=1 handle=%s url=%s rss_attempted=1 outcome=%s flags_used_rss=%d rss_used_total=%d"
                        % (
                            handle_for_rss or "<missing>",
                            attempt.profile_url or "",
                            attempt.status,
                            flags_used_rss,
                            int(rss_used_total or 0),
                        )
                    )
                except Exception:
                    pass
                if rss_payload and rss_ok:
                    rss_payload.match_score = rss_payload.match_score or attempt.match_score or 1.0
                    rss_payload.candidate_name = artist_name
                    applied = self._apply_payload_guarded(df, row_idx, rss_payload, artist_name, spotify_id=spotify_id)
                    self._finalize_night_sc(df, row_idx, attempt, rss_payload if applied else None, artist_name)
                    return bool(applied)

            try:
                flags = _SC_SHARED_ENGINE.get_run_flags()
            except Exception:
                flags = {}
            root_fetch_disabled = int(flags.get("root_fetch_disabled", 0) or 0)
            tracks_api_blocked = int(flags.get("tracks_api_blocked", 0) or 0)
            allow_tracks_fallback = _sc_allow_fallback_on_tracks_api_block()
            tracks_blocking = tracks_api_blocked == 1 and not allow_tracks_fallback
            engine_unstable = tracks_blocking
            fallback_allowed = (
                not getattr(self, "_sc_rss_only_mode", False)
                and not sc_fallback_used
                and not attempt.challenge
                and not engine_unstable
            )
            if os.getenv("SC_DEBUG_FALLBACK_GATE") == "1":
                try:
                    self.log_message.emit(
                        "[SC DEBUG] row=%s flags={ root_fetch_disabled=%d, tracks_api_blocked=%d, allow_tracks_fallback=%d, tracks_blocking=%d, html_challenges=%d, engine_unstable=%d, fallback_allowed=%d, rss_first_attempted=%d }"
                        % (
                            row_idx,
                            root_fetch_disabled,
                            tracks_api_blocked,
                            int(bool(allow_tracks_fallback)),
                            int(bool(tracks_blocking)),
                            int(getattr(self, "_sc_html_challenge_count", 0)),
                            int(bool(engine_unstable)),
                            int(bool(fallback_allowed)),
                            int(bool(sc_rss_first_attempted)),
                        )
                    )
                except Exception:
                    pass
            if tracks_api_blocked and allow_tracks_fallback and fallback_allowed and not _SC_TRACKS_API_FALLBACK_LOGGED:
                try:
                    self.log_message.emit(
                        "[SC] tracks API blocked (401/403); continuing with HTML/about fallback (SC_ALLOW_FALLBACK_ON_TRACKS_401_403=1)"
                    )
                except Exception:
                    pass
                _SC_TRACKS_API_FALLBACK_LOGGED = True
            if sc_rss_first_attempted and not fallback_allowed and engine_unstable:
                try:
                    self.log_message.emit(
                        "[Night SC] fallback_blocked=1 reason=engine_unstable root_fetch_disabled=%d tracks_api_blocked=%d html_challenges=%d"
                        % (
                            root_fetch_disabled,
                            tracks_api_blocked,
                            getattr(self, "_sc_html_challenge_count", 0),
                        )
                    )
                except Exception:
                    pass
            if fallback_allowed:
                sc_fallback_used = True
                if sc_rss_first_attempted:
                    attempt.status = ""
                    attempt.reason = ""
                payload, actionable = self._night_sc_fetch_profile_payload(attempt.profile_url, attempt)
            else:
                if sc_rss_first_attempted:
                    self._finalize_night_sc(df, row_idx, attempt, None, artist_name)
                    return False
                payload, actionable = (None, False)

            # If RSS-only tripped mid-row due to a challenge, reroute this row immediately to RSS.
            if attempt.challenge and getattr(self, "_sc_rss_only_mode", False):
                attempt.challenge = False
                attempt.profile_source = "rss_only"
                reroute_handle = attempt.handle or _sc_handle_from_profile_url(attempt.profile_url or "") or (
                    _sc_handle_from_profile_url(sc_link) if sc_link else ""
                )
                reroute_payload: Optional[EnrichmentPayload] = None
                rss_ok = False
                rss_attempted = bool(reroute_handle)
                if reroute_handle:
                    reroute_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(reroute_handle, None, row_idx=row_idx)
                    if rss_ok:
                        attempt.status = "rss_success"
                        attempt.reason = "rss_success"
                    else:
                        attempt.status = "rss_unavailable" if not rss_available else "rss_fail"
                        attempt.reason = attempt.status
                else:
                    attempt.status = "rss_unavailable"
                    attempt.reason = "rss_unavailable"
                attempt.handle = reroute_handle or attempt.handle
                attempt.profile_url = attempt.profile_url or (reroute_handle and f"https://soundcloud.com/{reroute_handle}") or ""
                try:
                    flags = _SC_SHARED_ENGINE.get_run_flags()
                except Exception:
                    flags = {}
                rss_used_total = _sc_get_rss_used_total()
                flags_used_rss = int(flags.get("used_rss", 0))
                try:
                    self.log_message.emit(
                        "[Night SC] rss_only=1 reroute=1 handle=%s url=%s rss_attempted=%d outcome=%s flags_used_rss=%d rss_used_total=%d"
                        % (
                            reroute_handle or "<missing>",
                            attempt.profile_url or "",
                            int(rss_attempted),
                            attempt.status,
                            flags_used_rss,
                            int(rss_used_total or 0),
                        )
                    )
                except Exception:
                    pass
                if reroute_payload:
                    reroute_payload.match_score = reroute_payload.match_score or attempt.match_score or 1.0
                    reroute_payload.candidate_name = artist_name
                    applied = self._apply_payload_guarded(df, row_idx, reroute_payload, artist_name, spotify_id=spotify_id)
                else:
                    applied = False
                self._finalize_night_sc(df, row_idx, attempt, reroute_payload if applied else None, artist_name)
                return bool(applied)
            if getattr(self, "night_mode", False) and (handle := (attempt.handle or _sc_handle_from_profile_url(attempt.profile_url) or "")):
                if (getattr(self, "_sc_rss_only_mode", False) or (attempt.saw_403 and (attempt.reason or "").startswith("api_403"))) and not payload:
                    if attempt.saw_403 and (attempt.reason or "").startswith("api_403"):
                        try:
                            self.log_message.emit(
                                f"[Night SC] api_403 on engine fetch; attempting RSS fallback for handle={handle}."
                            )
                        except Exception:
                            pass
                    rss_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(handle, payload, row_idx=row_idx)
                    if rss_payload and rss_ok:
                        payload = rss_payload
                        actionable = _payload_actionable(payload) or bool(payload.websites or payload.socials or payload.emails)
                        attempt.reason = attempt.reason or "rss_fallback"
                        attempt.saw_403 = False
                    elif getattr(self, "_sc_rss_only_mode", False):
                        attempt.reason = attempt.reason or "rss_only_mode"
            if payload:
                payload.match_score = 1.0
                payload.candidate_name = artist_name
                attempt.confidence = 1.0
                attempt.match_score = 1.0
            applied = False
            if payload:
                applied = self._apply_payload_guarded(df, row_idx, payload, artist_name, spotify_id=spotify_id)
            self._finalize_night_sc(df, row_idx, attempt, payload if applied else None, artist_name)
            return applied
        if not sc_query:
            attempt.status = "no_confident_match"
            attempt.reason = "no_query"
            self._finalize_night_sc(df, row_idx, attempt, None, artist_name)
            return False
        best_candidate = self._night_sc_search_candidates(
            artist_name,
            sc_query,
            location_hint,
            place_hint,
            genre_hint,
            track_hint,
            attempt,
            country_hint=country_hint,
        )
        if not best_candidate:
            attempt.status = "no_confident_match"
            attempt.reason = "no_candidate"
            self._finalize_night_sc(df, row_idx, attempt, None, artist_name)
            return False
        profile_url = best_candidate.get("profile_url") or ""
        handle = _sc_handle_from_profile_url(profile_url) or best_candidate.get("handle") or ""
        attempt.handle = handle
        attempt.profile_url = profile_url
        attempt.confidence = float(best_candidate.get("score", 0.0) or 0.0)
        attempt.match_score = attempt.confidence
        if _night_sc_engine_enabled(getattr(self, "night_mode", False)) and os.getenv("NIGHT_SC_DEBUG"):
            self.log_message.emit(f"[Night SC] Rediscovery chose handle={handle or '<unknown>'} url={profile_url}")

        payload: Optional[EnrichmentPayload] = None
        actionable = False

        if handle:
            sc_rss_first_attempted = True
            attempt.profile_source = attempt.profile_source or "rss_first"
            rss_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(handle, None, row_idx=row_idx)
            if rss_ok:
                attempt.status = "rss_success"
                attempt.reason = "rss_success"
            else:
                attempt.status = "rss_unavailable" if not rss_available else "rss_fail"
                attempt.reason = attempt.status
            try:
                flags = _SC_SHARED_ENGINE.get_run_flags()
            except Exception:
                flags = {}
            rss_used_total = _sc_get_rss_used_total()
            flags_used_rss = int(flags.get("used_rss", 0))
            try:
                self.log_message.emit(
                    "[Night SC] rss_first=1 handle=%s url=%s rss_attempted=1 outcome=%s flags_used_rss=%d rss_used_total=%d"
                    % (
                        handle or "<missing>",
                        profile_url or "",
                        attempt.status,
                        flags_used_rss,
                        int(rss_used_total or 0),
                    )
                )
            except Exception:
                pass
            if rss_payload and rss_ok:
                rss_payload.match_score = self._compute_match_score_for_candidate(
                    best_candidate.get("display_name") or best_candidate.get("handle") or "",
                    song_title,
                    extract_domain(profile_url),
                )
                rss_payload.candidate_name = best_candidate.get("display_name") or best_candidate.get("handle") or ""
                applied = self._apply_payload_guarded(df, row_idx, rss_payload, artist_name, spotify_id=spotify_id)
                self._finalize_night_sc(df, row_idx, attempt, rss_payload if applied else None, artist_name)
                return bool(applied)

        cached = self._night_sc_cache_lookup(handle, profile_url)
        if cached:
            attempt.cached_snapshot = cached
            attempt.cached_payload = _payload_from_snapshot(cached.get("payload"))
            attempt.status = "skipped_already_attempted"
            attempt.reason = cached.get("reason") or "cache_hit"
            attempt.http_status = cached.get("http", attempt.http_status)
            attempt.fetches = cached.get("fetches", attempt.fetches)
            applied = self._apply_sc_snapshot_to_row(df, row_idx, cached, artist_name, spotify_id=spotify_id)
            self._finalize_night_sc(df, row_idx, attempt, attempt.cached_payload if applied else None, artist_name)
            return bool(applied)
        try:
            flags = _SC_SHARED_ENGINE.get_run_flags()
        except Exception:
            flags = {}
        root_fetch_disabled = int(flags.get("root_fetch_disabled", 0) or 0)
        tracks_api_blocked = int(flags.get("tracks_api_blocked", 0) or 0)
        allow_tracks_fallback = _sc_allow_fallback_on_tracks_api_block()
        tracks_blocking = tracks_api_blocked == 1 and not allow_tracks_fallback
        engine_unstable = tracks_blocking
        fallback_allowed = (
            not getattr(self, "_sc_rss_only_mode", False)
            and not sc_fallback_used
            and not attempt.challenge
            and not engine_unstable
        )
        if os.getenv("SC_DEBUG_FALLBACK_GATE") == "1":
            try:
                self.log_message.emit(
                    "[SC DEBUG] row=%s flags={ root_fetch_disabled=%d, tracks_api_blocked=%d, allow_tracks_fallback=%d, tracks_blocking=%d, html_challenges=%d, engine_unstable=%d, fallback_allowed=%d, rss_first_attempted=%d }"
                    % (
                        row_idx,
                        root_fetch_disabled,
                        tracks_api_blocked,
                        int(bool(allow_tracks_fallback)),
                        int(bool(tracks_blocking)),
                        int(getattr(self, "_sc_html_challenge_count", 0)),
                        int(bool(engine_unstable)),
                        int(bool(fallback_allowed)),
                        int(bool(sc_rss_first_attempted)),
                    )
                )
            except Exception:
                pass
        if tracks_api_blocked and allow_tracks_fallback and fallback_allowed and not _SC_TRACKS_API_FALLBACK_LOGGED:
            try:
                self.log_message.emit(
                    "[SC] tracks API blocked (401/403); continuing with HTML/about fallback (SC_ALLOW_FALLBACK_ON_TRACKS_401_403=1)"
                )
            except Exception:
                pass
            _SC_TRACKS_API_FALLBACK_LOGGED = True
        if sc_rss_first_attempted and not fallback_allowed and engine_unstable:
            try:
                self.log_message.emit(
                    "[Night SC] fallback_blocked=1 reason=engine_unstable root_fetch_disabled=%d tracks_api_blocked=%d html_challenges=%d"
                    % (
                        root_fetch_disabled,
                        tracks_api_blocked,
                        getattr(self, "_sc_html_challenge_count", 0),
                    )
                )
            except Exception:
                pass
        if fallback_allowed:
            sc_fallback_used = True
            if sc_rss_first_attempted:
                attempt.status = ""
                attempt.reason = ""
            payload, actionable = self._night_sc_fetch_profile_payload(profile_url, attempt)
        else:
            if sc_rss_first_attempted:
                self._finalize_night_sc(df, row_idx, attempt, None, artist_name)
                return False
            payload, actionable = (None, False)

        # If RSS-only tripped mid-row due to a challenge, reroute this row immediately to RSS.
        if attempt.challenge and getattr(self, "_sc_rss_only_mode", False):
            attempt.challenge = False
            attempt.profile_source = "rss_only"
            reroute_handle = attempt.handle or _sc_handle_from_profile_url(profile_url or "") or (
                _sc_handle_from_profile_url(sc_link) if sc_link else ""
            )
            reroute_payload: Optional[EnrichmentPayload] = None
            rss_ok = False
            rss_attempted = bool(reroute_handle)
            if reroute_handle:
                reroute_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(reroute_handle, None, row_idx=row_idx)
                if rss_ok:
                    attempt.status = "rss_success"
                    attempt.reason = "rss_success"
                else:
                    attempt.status = "rss_unavailable" if not rss_available else "rss_fail"
                    attempt.reason = attempt.status
            else:
                attempt.status = "rss_unavailable"
                attempt.reason = "rss_unavailable"
            attempt.handle = reroute_handle or attempt.handle
            attempt.profile_url = attempt.profile_url or profile_url or (reroute_handle and f"https://soundcloud.com/{reroute_handle}") or ""
            try:
                flags = _SC_SHARED_ENGINE.get_run_flags()
            except Exception:
                flags = {}
            rss_used_total = _sc_get_rss_used_total()
            flags_used_rss = int(flags.get("used_rss", 0))
            try:
                self.log_message.emit(
                    "[Night SC] rss_only=1 reroute=1 handle=%s url=%s rss_attempted=%d outcome=%s flags_used_rss=%d rss_used_total=%d"
                    % (
                        reroute_handle or "<missing>",
                        attempt.profile_url or "",
                        int(rss_attempted),
                        attempt.status,
                        flags_used_rss,
                        int(rss_used_total or 0),
                    )
                )
            except Exception:
                pass
            if reroute_payload:
                reroute_payload.match_score = reroute_payload.match_score or attempt.match_score or 1.0
                reroute_payload.candidate_name = artist_name
                applied = self._apply_payload_guarded(df, row_idx, reroute_payload, artist_name, spotify_id=spotify_id)
            else:
                applied = False
            self._finalize_night_sc(df, row_idx, attempt, reroute_payload if applied else None, artist_name)
            return bool(applied)
        if getattr(self, "night_mode", False) and handle:
            needs_rss = False
            if attempt.saw_403 and (attempt.reason or "").startswith("api_403"):
                try:
                    self.log_message.emit(
                        f"[Night SC] api_403 on engine fetch; attempting RSS fallback for handle={handle}."
                    )
                except Exception:
                    pass
                needs_rss = True
            if getattr(self, "_sc_rss_only_mode", False) and not payload:
                needs_rss = True
            if needs_rss:
                rss_payload, rss_ok, rss_available, rss_reason = self._sc_build_rss_payload(handle, payload, row_idx=row_idx)
                if rss_payload and rss_ok:
                    payload = rss_payload
                    actionable = _payload_actionable(payload) or bool(payload.websites or payload.socials or payload.emails)
                    attempt.reason = attempt.reason or "rss_fallback"
                    attempt.saw_403 = False
                    try:
                        attempt.http_status = attempt.http_status or 200
                    except Exception:
                        pass
                elif getattr(self, "_sc_rss_only_mode", False):
                    attempt.reason = attempt.reason or "rss_only_mode"
        if payload:
            payload.match_score = self._compute_match_score_for_candidate(
                best_candidate.get("display_name") or best_candidate.get("handle") or "",
                song_title,
                extract_domain(profile_url),
            )
            payload.candidate_name = best_candidate.get("display_name") or best_candidate.get("handle") or ""
        applied = False
        if payload:
            applied = self._apply_payload_guarded(df, row_idx, payload, artist_name, spotify_id=spotify_id)
        payload_to_cache = payload if applied else None
        self._finalize_night_sc(df, row_idx, attempt, payload_to_cache, artist_name)
        return applied

    def _apply_sc_snapshot_to_row(
        self,
        df: pd.DataFrame,
        row_idx,
        snapshot: Optional[Dict[str, Any]],
        artist_name: str,
        spotify_id: str = "",
    ) -> bool:
        payload = _payload_from_snapshot((snapshot or {}).get("payload"))
        if not payload:
            return False
        # Preserve stored match_score/candidate_name for deterministic reuse.
        payload.match_score = float((snapshot or {}).get("match_score") or getattr(payload, "match_score", 0.0) or 0.0)
        payload.candidate_name = (snapshot or {}).get("candidate_name") or getattr(payload, "candidate_name", "")
        return self._apply_payload_guarded(df, row_idx, payload, artist_name, spotify_id=spotify_id)

    def _write_sc_status_columns(
        self,
        df: pd.DataFrame,
        row_idx,
        status: str,
        reason: str,
        fetches: int,
        elapsed_ms: int,
    ) -> None:
        if "SC_Status" in df.columns:
            df.at[row_idx, "SC_Status"] = status
        if "SC_Reason" in df.columns:
            df.at[row_idx, "SC_Reason"] = reason
        if "SC_Fetches" in df.columns:
            df.at[row_idx, "SC_Fetches"] = fetches
        if "SC_ms" in df.columns:
            df.at[row_idx, "SC_ms"] = elapsed_ms

    def _cache_night_sc_snapshot(
        self,
        attempt: _NightSCAttempt,
        status: str,
        reason: str,
        payload: Optional[EnrichmentPayload],
    ) -> None:
        keys = _night_sc_cache_keys(attempt.handle, attempt.profile_url)
        if not keys:
            return
        snapshot = {
            "status": status,
            "reason": reason,
            "fetches": attempt.fetches,
            "ms": attempt.elapsed_ms(),
            "http": attempt.http_status,
            "confidence": attempt.confidence,
            "payload": _snapshot_payload(payload),
            "match_score": attempt.match_score,
            "candidate_name": getattr(payload, "candidate_name", "") if payload else "",
        }
        for key in keys:
            self._night_sc_cache[key] = snapshot

    def _night_sc_cache_lookup(self, handle: str, profile_url: str) -> Optional[Dict[str, Any]]:
        if not getattr(self, "night_mode", False):
            return None
        for key in _night_sc_cache_keys(handle, profile_url):
            cached = self._night_sc_cache.get(key)
            if cached:
                return cached
        return None

    def _finalize_night_sc(
        self,
        df: pd.DataFrame,
        row_idx,
        attempt: Optional[_NightSCAttempt],
        payload: Optional[EnrichmentPayload],
        artist_name: str,
    ) -> None:
        if not attempt or attempt.finalized:
            return
        attempt.finalized = True
        attempt.budget_ok()  # Refresh budget flag based on elapsed time.
        effective_payload = payload or attempt.cached_payload
        actionable = _payload_actionable(effective_payload)
        if actionable is None:
            actionable = False
        confidence = float(attempt.confidence or getattr(effective_payload, "match_score", 0.0) or 0.0)
        http_status = attempt.http_status
        if http_status is None:
            http_status = getattr(self, "_last_http_status", None)
        status = attempt.status or ""
        reason = attempt.reason or ""
        # Preserve RSS-only outcome labelling and avoid HTML challenge/status mappings when RSS-only is active.
        if attempt.profile_source == "rss_only" or getattr(self, "_sc_rss_only_mode", False):
            if not status:
                if actionable:
                    status = "rss_success"
                elif reason:
                    status = reason
                else:
                    status = "rss_fail"
            if not reason:
                if actionable:
                    reason = "rss_success"
                else:
                    reason = status
            attempt.status = status
            attempt.reason = reason
        rss_labeled = bool((status or "").startswith("rss_"))
        if status == "skipped_already_attempted":
            pass
        elif rss_labeled:
            pass
        elif attempt.challenge:
            status = "non_actionable_challenge"
            if not reason or reason == "no_candidate":
                reason = "challenge_page"
        elif attempt.budget_exceeded:
            status = "blocked_403" if attempt.saw_403 else "no_confident_match"
            reason = reason or "budget_exceeded"
        elif attempt.saw_403 and actionable is not True:
            status = "blocked_403"
            reason = reason or "api_403"
        elif actionable:
            status = "actionable"
        else:
            status = "no_confident_match"
        if not reason and status == "no_confident_match":
            reason = "no_match"
        attempt.status = status
        attempt.reason = reason
        # 403 streak tracking for RSS-only guardrail (API/profile failures).
        if attempt.saw_403:
            self._sc_record_403(row_idx=row_idx, source=attempt.profile_source or attempt.candidate_source or "unknown")
        else:
            self._sc_reset_403_streak()
        self._write_sc_status_columns(df, row_idx, status, reason, attempt.fetches, attempt.elapsed_ms())
        if getattr(self, "night_mode", False):
            try:
                flags = _SC_SHARED_ENGINE.get_run_flags()
            except Exception:
                flags = {
                    "root_fetch_disabled": 0,
                    "about_disabled": 0,
                    "tracks_api_blocked": 0,
                    "used_user_api": 0,
                    "used_rss": 0,
                }
            rss_used_total = _sc_get_rss_used_total()
            candidate_src = getattr(attempt, "candidate_source", "none") or "none"
            profile_src = getattr(attempt, "profile_source", "none") or "none"
            try:
                self.log_message.emit(
                    "[Night SC] candidate_source=%s profile_source=%s root_fetch_disabled=%d about_disabled=%d tracks_api_blocked=%d used_user_api=%d used_rss=%d rss_used_total=%d"
                    % (
                        candidate_src,
                        profile_src,
                        int(flags.get("root_fetch_disabled", 0)),
                        int(flags.get("about_disabled", 0)),
                        int(flags.get("tracks_api_blocked", 0)),
                        int(flags.get("used_user_api", 0)),
                        int(flags.get("used_rss", 0)),
                        int(rss_used_total),
                    )
                )
            except Exception:
                pass
        try:
            self.log_message.emit(
                "[Night SC] Final status: %s handle=%s http=%s confidence=%.2f ms=%s fetches=%s reason=%s"
                % (
                    status,
                    attempt.handle or "<unknown>",
                    http_status if http_status is not None else "",
                    confidence,
                    attempt.elapsed_ms(),
                    attempt.fetches,
                    reason or "",
                )
            )
        except Exception:
            pass
        self._cache_night_sc_snapshot(attempt, status, reason, effective_payload)
    def _live_search_soundcloud(self, artist_name: str) -> Optional[EnrichmentPayload]:
        if getattr(self, "_sc_rss_only_mode", False):
            self._sc_rss_only_engine_fetch_skips += 1
            try:
                self.log_message.emit(
                    "[Enricher][SC] rss_only=1 -> skipping live SoundCloud search for '%s'." % (artist_name,)
                )
            except Exception:
                pass
            self._set_platform_state("soundcloud", "skipped")
            return None
        if getattr(self, "_sc_live_enrich_disabled", False):
            reason = self._sc_live_enrich_disabled_reason or "first_challenge_page"
            self.log_message.emit(
                f"[Enricher][SC] Live enrichment disabled (reason={reason}); skipping live SC check for '{artist_name}'."
            )
            self._set_platform_state("soundcloud", "skipped")
            return None
        if self._sc_in_live_cooldown():
            cooldown_left = int(max(1.0, (self._sc_live_disabled_until - time.time())))
            self.log_message.emit(
                f"[Enricher][SC] Live enrichment in cooldown ({cooldown_left}s remaining); skipping live SC check for '{artist_name}'."
            )
            self._set_platform_state("soundcloud", "skipped")
            return None
        if not self._platform_attempt_allowed("soundcloud", artist_name, "SoundCloud Enrich"):
            return None
        if not self._increment_live_counter():
            self.log_message.emit("[Enricher] SoundCloud live search skipped (limit reached).")
            self._set_platform_state("soundcloud", "skipped")
            return None
        song_title = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        sc_query = _clean_soundcloud_query(build_search_query(artist_name, song_title))
        if not sc_query:
            self._set_platform_state("soundcloud", "skipped")
            return None
        engine = os.getenv("NIGHTMODE_SC_ENGINE", "current").lower()
        night_mode_active = bool(getattr(self, "night_mode", False))
        use_t007_engine = bool(night_mode_active and engine == "t007")
        location_hint = _clean_cell(getattr(self, "_live_context", {}).get("location", ""))
        track_hint = _clean_cell(getattr(self, "_live_context", {}).get("track", ""))
        genre_hint = _clean_cell(getattr(self, "_live_context", {}).get("genre", ""))
        # Prefer people/artist search results, expanding the query with optional track or location hints.
        self.log_message.emit(f"[Enricher] SoundCloud Enrich: searching for '{sc_query}'")
        best_candidate: Optional[Dict[str, Any]] = None

        def _is_better_candidate(candidate: Dict[str, Any], current: Optional[Dict[str, Any]]) -> bool:
            if not candidate:
                return False
            cand_score = candidate.get("score", 0)
            cand_rank = candidate.get("rank_score", cand_score)
            curr_score = current.get("score", 0) if current else 0
            curr_rank = current.get("rank_score", curr_score) if current else 0
            if cand_score > curr_score:
                return True
            if curr_score > cand_score + 0.02:
                return False
            return cand_rank > curr_rank

        for query in _build_soundcloud_queries(sc_query, track_hint, location_hint):
            candidates = self._soundcloud_people_search_candidates(query)
            candidate = self._pick_best_soundcloud_candidate(
                artist_name, candidates, location_hint, genre_hint
            )
            if candidate and _is_better_candidate(candidate, best_candidate):
                best_candidate = candidate
            # Early exit when we have a high-confidence handle/display-name match.
            if best_candidate and best_candidate["score"] >= _SC_CONFIDENCE_ACCEPT:
                break
        if not best_candidate or best_candidate["score"] < _SC_CONFIDENCE_MIN:
            self.log_message.emit(
                "[Enricher] SoundCloud people search: no confident match, trying universal search..."
            )
            uni_candidates = self._soundcloud_universal_search_candidates(sc_query)
            candidate = self._pick_best_soundcloud_candidate(
                artist_name, uni_candidates, location_hint, genre_hint
            )
            if candidate and _is_better_candidate(candidate, best_candidate):
                best_candidate = candidate
        best_score = best_candidate.get("score", 0) if best_candidate else 0.0
        if not best_candidate or best_score < MIN_SC_CONFIDENCE:
            # Optional fallback: broaden to artist-only query if we started with artist + track.
            if song_title:
                fallback_query = _clean_soundcloud_query(artist_name)
                self.log_message.emit(
                    f"[Enricher] SoundCloud Enrich: primary search found no safe match for '{artist_name}', "
                    f"trying artist-only query '{fallback_query}'."
                )
                fallback_candidate = self._soundcloud_best_candidate_for_query(
                    artist_name, fallback_query, location_hint, genre_hint
                )
                if (
                    fallback_candidate
                    and fallback_candidate.get("score", 0) >= MIN_SC_CONFIDENCE
                    and (best_candidate is None or _is_better_candidate(fallback_candidate, best_candidate))
                ):
                    best_candidate = fallback_candidate
                    best_score = fallback_candidate.get("score", 0)
            if not best_candidate or best_candidate.get("score", 0) < MIN_SC_CONFIDENCE:
                best_score = best_candidate.get("score", 0) if best_candidate else 0.0
                self.log_message.emit(
                    f"[Enricher] SoundCloud Enrich: no safe match for '{artist_name}' "
                    f"(best_confidence={best_score:.2f}), skipping."
                )
                self._set_platform_state("soundcloud", "skipped")
                return None
        profile_url = best_candidate["profile_url"]

        def _payload_from_t007(data: Dict[str, Any]) -> EnrichmentPayload:
            socials: Set[str] = set()
            websites: Set[str] = set()
            emails: Set[str] = set()
            link_hubs: Set[str] = set()
            extra_social_hosts = ("spotify.com", "bandcamp.com")
            for email in data.get("emails") or []:
                addr = (email or "").strip().lower()
                if addr:
                    emails.add(addr)
            for url in data.get("external_urls") or []:
                normalised = _normalise_url(url)
                if not normalised or _is_noise_url(normalised):
                    continue
                parsed = urllib.parse.urlparse(normalised)
                host = parsed.netloc.lower()
                path_lower = (parsed.path or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                if host.endswith("soundcloud.com"):
                    continue
                if host in LINK_HUB_HOSTS:
                    link_hubs.add(normalised)
                    websites.add(normalised)
                    continue
                if any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST) or any(
                    host.endswith(domain) for domain in extra_social_hosts
                ):
                    socials.add(normalised)
                    continue
                if host in JUNK_WEBSITE_HOSTS:
                    continue
                if any(keyword in path_lower for keyword in JUNK_WEBSITE_PATH_KEYWORDS):
                    continue
                websites.add(normalised)
            return EnrichmentPayload(
                socials=socials,
                websites=websites,
                emails=emails,
                link_hubs=link_hubs,
                source_dir="soundcloud",
                source_url=profile_url,
                source_detail=_format_source_display("soundcloud_live"),
            )

        payload: Optional[EnrichmentPayload] = None
        if use_t007_engine:
            handle = _sc_handle_from_profile_url(profile_url) or best_candidate.get("handle") or ""
            self.log_message.emit(
                f"[Enricher] SoundCloud Enrich: engine=t007 handle={handle or '<unknown>'} url={profile_url}"
            )
            self._fetch_url(profile_url, label="soundcloud profile preflight", max_attempts=1)
            if getattr(self, "_sc_blocked_for_row", False):
                return None
            sc_helper = _get_t007_sc_helper()
            if sc_helper and handle:
                try:
                    sc_result = sc_helper(handle) or {}
                    sc_data = sc_result.get("data") if isinstance(sc_result, dict) else {}
                    if sc_data is None:
                        sc_data = {}
                    payload = _payload_from_t007(sc_data)
                    self._last_fetch_ok = True
                except Exception as exc:
                    self.log_message.emit(
                        f"[Enricher] SoundCloud Enrich: engine=t007 fallback to legacy ({exc})"
                    )
                    payload = None
            elif not sc_helper:
                self.log_message.emit(
                    "[Enricher] SoundCloud Enrich: engine=t007 helper unavailable, falling back to legacy."
                )
            elif not handle:
                self.log_message.emit(
                    "[Enricher] SoundCloud Enrich: engine=t007 missing handle, falling back to legacy."
                )
            if payload is None:
                if _night_sc_engine_enabled(night_mode_active):
                    handle = _sc_handle_from_profile_url(profile_url) or best_candidate.get("handle") or ""
                    if handle:
                        sc_data = _SC_SHARED_ENGINE.fetch_profile(handle) or {}
                        self._note_sc_challenge(
                            sc_data.get("status"),
                            sc_data.get("reason"),
                            sc_data.get("challenge_page"),
                        )
                        payload = _payload_from_t007(sc_data)
                        self._last_fetch_ok = True
            if payload is None:
                payload = self._fetch_profile_and_build(profile_url, "soundcloud")
        else:
            payload = self._fetch_profile_and_build(profile_url, "soundcloud")
        fetch_ok_flag = getattr(self, "_last_fetch_ok", None)
        actionable_flag = _payload_actionable(payload)
        if fetch_ok_flag is False and actionable_flag is None:
            actionable_flag = False
        outcome_suffix = _format_outcome_suffix(
            fetch_ok=fetch_ok_flag,
            actionable=actionable_flag,
            http_status=getattr(self, "_last_http_status", None),
        )
        self.log_message.emit(
            f"[Enricher] SoundCloud Enrich: best match '{best_candidate.get('display_name') or best_candidate.get('handle')}' "
            f"({best_candidate.get('profile_url')}), confidence={best_candidate.get('score'):.2f}{outcome_suffix}"
        )
        if payload:
            payload.match_score = self._compute_match_score_for_candidate(
                best_candidate.get("display_name") or best_candidate.get("handle") or "",
                song_title,
                extract_domain(best_candidate.get("profile_url") or ""),
            )
            payload.candidate_name = best_candidate.get("display_name") or best_candidate.get("handle") or ""
            self._set_platform_state("soundcloud", "matched")
            return payload
        self._set_platform_state("soundcloud", "skipped")
        return None

    def _live_search_lastfm(self, artist_name: str) -> Optional[EnrichmentPayload]:
        artist_key = normalise_artist_name(artist_name)
        # Clear expired cooldowns
        for endpoint in ("search", "profile"):
            if endpoint == "search" and self._lf_search_cooldown_until and not self._lf_endpoint_in_cooldown("search"):
                try:
                    self.log_message.emit("[Enricher][LF] search cooldown expired; resuming")
                except Exception:
                    pass
                self._lf_clear_endpoint_cooldown("search")
            if endpoint == "profile" and self._lf_profile_cooldown_until and not self._lf_endpoint_in_cooldown("profile"):
                try:
                    self.log_message.emit("[Enricher][LF] profile cooldown expired; resuming")
                except Exception:
                    pass
                self._lf_clear_endpoint_cooldown("profile")

        if not self._platform_attempt_allowed("lastfm", artist_name, "Last.fm Enrich"):
            return None
        decision = self._row_allows_heavy_enricher(None, getattr(self, "_live_context", {}) or {}, "lastfm")
        if not decision.allowed:
            self._log_low_confidence_skip("lastfm", artist_name, decision)
            self._set_platform_state("lastfm", "skipped")
            return None

        # Profile-first paths (run-scoped cache, seed URL, search reuse)
        seed_lastfm_urls: Set[str] = set()
        try:
            seed_lastfm_urls = set(getattr(self, "_live_context", {}).get("seed_lastfm_urls") or [])
        except Exception:
            seed_lastfm_urls = set()

        cached_profile_url: Optional[str] = None
        if artist_key and artist_key in self._lf_profile_url_cache:
            cached_profile_url = self._lf_profile_url_cache[artist_key]
            try:
                self.log_message.emit(f"[LF] cache hit -> profile-first for '{artist_name}'")
            except Exception:
                pass
        elif seed_lastfm_urls:
            cached_profile_url = next(iter(seed_lastfm_urls))
            try:
                self.log_message.emit("[LF] profile-first path used")
            except Exception:
                pass
        elif artist_key and artist_key in self._lf_search_result_cache:
            cached = self._lf_search_result_cache[artist_key]
            try:
                self.log_message.emit(f"[LF] search reuse -> skipping new search for '{artist_name}'")
            except Exception:
                pass
            if cached:
                cached_profile_url = cached
            else:
                self._set_platform_state("lastfm", "skipped")
                return None

        if cached_profile_url:
            payload, resolved_url = self._lf_fetch_profile_only(cached_profile_url, artist_key, artist_name)
            fetch_ok_flag = getattr(self, "_last_fetch_ok", None)
            actionable_flag = _payload_actionable(payload)
            if fetch_ok_flag is False and actionable_flag is None:
                actionable_flag = False
            outcome_suffix = _format_outcome_suffix(
                fetch_ok=fetch_ok_flag,
                actionable=actionable_flag,
                http_status=getattr(self, "_last_http_status", None),
            )
            self.log_message.emit(
                f"[Enricher] Last.fm Enrich: profile-first for '{artist_name}' -> {resolved_url or cached_profile_url}{outcome_suffix}"
            )
            return payload

        if self._lf_endpoint_in_cooldown("search"):
            if not self._lf_search_cooldown_skip_logged:
                cooldown = self._lf_endpoint_cooldown_remaining("search")
                try:
                    self.log_message.emit(
                        f"[Enricher][LF] search cooldown active; skipping row '{artist_name}' expires_in={cooldown}s"
                    )
                except Exception:
                    pass
                self._lf_search_cooldown_skip_logged = True
            self._lf_search_skipped_cooldown += 1
            self._set_platform_state("lastfm", "skipped")
            return None
        song_title_raw = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        sanitized_title = _sanitize_lastfm_track_title(song_title_raw)
        if sanitized_title and sanitized_title != song_title_raw.strip():
            orig = (song_title_raw or "").strip()
            cleaned = sanitized_title
            if len(orig) > 80:
                orig = orig[:80]
            if len(cleaned) > 80:
                cleaned = cleaned[:80]
            try:
                self.log_message.emit(
                    f"[Enricher][LF] Sanitized track title: orig='{orig}' -> cleaned='{cleaned}'"
                )
            except Exception:
                pass
        elif not sanitized_title and song_title_raw:
            try:
                self.log_message.emit("[Enricher][LF] Skipping track query (sanitized empty); using artist-only.")
            except Exception:
                pass

        use_track = bool(sanitized_title)
        primary_query = build_search_query(artist_name, sanitized_title) if use_track else artist_name
        if not self._increment_live_counter():
            self._set_platform_state("lastfm", "skipped")
            return None
        lf_unhealthy = self._lf_endpoint_in_cooldown("search") or self._lf_search_consecutive_406 > 0

        def _parse_first_candidate(html_doc: str, log_no_results: bool) -> Optional[Tuple[str, str, float, float, float]]:
            if not html_doc:
                return None
            soup = BeautifulSoup(html_doc, "html.parser")
            first_link = soup.select_one("a[href*='/music/']")
            if not first_link:
                if log_no_results:
                    self.log_message.emit("[Enricher] Last.fm search: no artist results.")
                return None
            disp = first_link.get_text(" ", strip=True)
            prof = (first_link.get("href") or "").strip()
            if prof.startswith("/"):
                prof = f"https://www.last.fm{prof}"
            score = _lastfm_confidence(artist_name, disp)
            rank = _locale_rank_score(score, disp, prof)
            match_score = self._compute_match_score_for_candidate(
                disp, sanitized_title or song_title_raw, extract_domain(prof)
            )
            self.log_message.emit(
                f"[Enricher] Last.fm Enrich: candidate '{disp or prof}' for '{artist_name}' has confidence={score:.2f}."
            )
            return disp, prof, score, rank, match_score

        best_score = 0.0
        best_rank_score = 0.0
        best_match_score = 0.0
        display_name = ""
        profile_url = ""

        def _run_search_attempt(
            query: str,
            *,
            label: str,
            announce_url: bool = False,
            artist_only_log: bool = False,
            log_no_results: bool,
        ) -> Optional[Tuple[str, str, float, float, float]]:
            quoted_query = urllib.parse.quote_plus(query)
            search_url = f"https://www.last.fm/search?q={quoted_query}&type=artist"
            if announce_url:
                self.log_message.emit(f"[Enricher] Last.fm live search: {search_url}")
            if artist_only_log:
                self.log_message.emit(
                    f"[Enricher] Last.fm Enrich: artist-only search for '{artist_name}'."
                )
            current_unhealthy = self._lf_endpoint_in_cooldown("search") or self._lf_search_consecutive_406 > 0
            _lf_sleep(current_unhealthy)
            html_doc = self._fetch_url(search_url, label=label, max_attempts=1, endpoint="search")
            return _parse_first_candidate(html_doc, log_no_results=log_no_results) if html_doc else None

        primary_candidate: Optional[Tuple[str, str, float, float, float]] = None
        first_search_406 = False
        if use_track and sanitized_title:
            primary_candidate = _run_search_attempt(
                primary_query,
                label="Last.fm search",
                announce_url=True,
                log_no_results=True,
            )
            first_search_406 = getattr(self, "_last_http_status", None) == 406 and getattr(self, "_last_fetch_ok", None) is False

        if primary_candidate:
            display_name, profile_url, best_score, best_rank_score, best_match_score = primary_candidate

        fallback_needed = (not use_track) or first_search_406
        if fallback_needed:
            if first_search_406:
                try:
                    self.log_message.emit(
                        f"[Enricher][LF] primary search for '{artist_name}' returned 406; trying artist-only fallback."
                    )
                except Exception:
                    pass
            fallback_candidate = _run_search_attempt(
                artist_name,
                label="Last.fm search (fallback)",
                artist_only_log=True,
                log_no_results=False,
            )
            if fallback_candidate:
                fb_display, fb_profile, fb_conf, fb_rank, fb_match_score = fallback_candidate
                display_name = fb_display
                profile_url = fb_profile
                best_score = fb_conf
                best_rank_score = fb_rank
                best_match_score = fb_match_score
        if best_score < MIN_LF_CONFIDENCE or not profile_url:
            self.log_message.emit(
                f"[Enricher] Last.fm Enrich: no safe match for '{artist_name}' (best_confidence={best_score:.2f}), skipping."
            )
            if artist_key:
                self._lf_search_result_cache[artist_key] = None
            self._set_platform_state("lastfm", "skipped")
            return None
        if artist_key:
            self._lf_profile_url_cache[artist_key] = profile_url
            self._lf_search_result_cache[artist_key] = profile_url

        payload, resolved_url = self._lf_fetch_profile_only(profile_url, artist_key, artist_name)
        fetch_ok_flag = getattr(self, "_last_fetch_ok", None)
        actionable_flag = _payload_actionable(payload)
        if fetch_ok_flag is False and actionable_flag is None:
            actionable_flag = False
        outcome_suffix = _format_outcome_suffix(
            fetch_ok=fetch_ok_flag,
            actionable=actionable_flag,
            http_status=getattr(self, "_last_http_status", None),
        )
        self.log_message.emit(
            f"[Enricher] Last.fm Enrich: candidate '{display_name or resolved_url}' for '{artist_name}' has confidence={best_score:.2f}{outcome_suffix}"
        )
        if payload:
            payload.match_score = best_match_score
            payload.candidate_name = display_name or ""
            return payload
        return None

    def _lf_fetch_profile_only(
        self, profile_url: str, artist_key: str, artist_name: str
    ) -> Tuple[Optional[EnrichmentPayload], str]:
        """Fetch a Last.fm profile using caches/canonical mapping without issuing search requests."""
        url_to_fetch = profile_url
        if profile_url in self._lf_canonical_url_cache:
            url_to_fetch = self._lf_canonical_url_cache[profile_url]
            try:
                self.log_message.emit("[LF] canonical reuse -> using cached canonical URL")
            except Exception:
                pass
        payload = self._fetch_profile_and_build(url_to_fetch, "lastfm")
        resolved_url = getattr(self, "_last_resolved_profile_url", "") or url_to_fetch
        if resolved_url and profile_url and resolved_url != profile_url:
            self._lf_canonical_url_cache[profile_url] = resolved_url
        if payload and artist_key:
            self._lf_profile_url_cache[artist_key] = resolved_url
            self._lf_search_result_cache[artist_key] = resolved_url
            self._set_platform_state("lastfm", "matched")
        else:
            if artist_key and artist_key not in self._lf_search_result_cache:
                self._lf_search_result_cache[artist_key] = None
            self._set_platform_state("lastfm", "skipped")
        return payload, resolved_url

    def _fetch_url(
        self,
        url: str,
        label: str,
        max_attempts: int = 2,
        headers: Optional[Dict[str, str]] = None,
        *,
        endpoint: Optional[str] = None,
    ) -> Optional[str]:
        # Track the most recent fetch outcome for instrumentation.
        self._last_fetch_ok: Optional[bool] = None
        self._last_http_status: Optional[int] = None
        self._last_final_url: Optional[str] = None
        is_lastfm = "last.fm" in url.lower()
        lf_endpoint = endpoint if is_lastfm and endpoint in ("search", "profile") else None
        headers_name = "default"
        if is_lastfm and headers is None:
            headers = LASTFM_HEADERS
            headers_name = "lastfm_html"
        elif headers is not None:
            headers_name = "custom"

        if lf_endpoint:
            now_mono = self._lf_now()
            if self._lf_endpoint_in_cooldown(lf_endpoint, now_mono):
                remaining = self._lf_endpoint_cooldown_remaining(lf_endpoint, now_mono)
                skip_logged_attr = "_lf_profile_cooldown_skip_logged" if lf_endpoint == "profile" else "_lf_search_cooldown_skip_logged"
                if not getattr(self, skip_logged_attr, False):
                    try:
                        self.log_message.emit(
                            f"[Enricher][LF] {lf_endpoint} cooldown active; skipping Last.fm fetch expires_in={remaining}s"
                        )
                    except Exception:
                        pass
                    setattr(self, skip_logged_attr, True)
                if lf_endpoint == "profile":
                    self._lf_profile_skipped_cooldown += 1
                else:
                    self._lf_search_skipped_cooldown += 1
                self._last_fetch_ok = False
                self._last_http_status = None
                return None

        attempt = 0
        pw_attempted = False
        while attempt < max_attempts:
            attempt += 1
            allow_pw = attempt == 1
            status = None
            text = ""
            try:
                resp = self.session.get(url, timeout=HTTP_TIMEOUT, headers=headers)
                status = getattr(resp, "status_code", None)
                self._last_final_url = getattr(resp, "url", None) or url
                text = getattr(resp, "text", "") or ""
                self._last_http_status = status

                if is_lastfm and lf_endpoint and status == 406:
                    pass
                elif is_lastfm and lf_endpoint:
                    if status and status < 400:
                        self._lf_mark_success(lf_endpoint)
                if is_lastfm:
                    try:
                        self.log_message.emit(
                            f"[Enricher][LF] {label}: status={status} attempt={attempt}/{max_attempts} headers={headers_name}"
                        )
                    except Exception:
                        pass

                if getattr(self, "night_mode", False) and "soundcloud" in label.lower():
                    if text == "":
                        text = getattr(resp, "text", "") or ""
                    if status == 403 and "profile" in label.lower() and "soundcloud.com" in url and "/about" not in url:
                        about_url = url.rstrip("/") + "/about"
                        try:
                            about_resp = self.session.get(about_url, timeout=HTTP_TIMEOUT)
                            about_status = getattr(about_resp, "status_code", None)
                            about_text = getattr(about_resp, "text", "") or ""
                            if about_status == 200:
                                if _sc_is_challenge_page(about_text):
                                    self._last_fetch_ok = False
                                    self._last_http_status = about_status
                                    self.log_message.emit(
                                        "[Night SC] Root 403; about returned challenge page, treating as non-actionable."
                                    )
                                    return None
                                self._last_fetch_ok = True
                                self._last_http_status = about_status
                                self.log_message.emit(
                                    "[Night SC] Root 403; using about page fallback."
                                )
                                return about_text
                            if _sc_is_blocked(about_status, about_text):
                                self._last_fetch_ok = False
                                self._flag_sc_blocked(status_code=about_status, html=about_text)
                                return None
                        except Exception:
                            pass
                    if _sc_is_blocked(status, text):
                        self._last_fetch_ok = False
                        self._flag_sc_blocked(status_code=status, html=text)
                        return None

                resp.raise_for_status()
                if text == "":
                    text = getattr(resp, "text", "") or ""
                self._last_fetch_ok = True
                return text
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                self._last_http_status = status
                try:
                    self._last_final_url = exc.response.url  # type: ignore[attr-defined]
                except Exception:
                    self._last_final_url = url
                html = exc.response.text if exc.response is not None else ""

                if is_lastfm and lf_endpoint and status == 406:
                    self._lf_mark_406(lf_endpoint)
                    self._last_fetch_ok = False
                    suffix = _format_outcome_suffix(fetch_ok=False, actionable=None, http_status=status)
                    self.log_message.emit(
                        f"[Enricher][LF] {label} received 406; treating as cooldown (no retry){suffix}"
                    )
                    return None

                trigger_pw = allow_pw and (not pw_attempted) and (
                    status in {403, 429, 503} or _detect_soft_block(html)
                )
                if trigger_pw:
                    pw_attempted = True
                    old_headers = None
                    if headers:
                        old_headers = dict(self.session.headers)
                        self.session.headers.update(headers)
                    try:
                        fallback = fetch_html(
                            url,
                            session=self.session,
                            directory=getattr(self, "source_dir", None) or None,
                            job_id=None,
                            required_selectors=None,
                            allow_browser_fallback=True,
                            timeout_s=HTTP_TIMEOUT,
                        )
                    finally:
                        if old_headers is not None:
                            self.session.headers.clear()
                            self.session.headers.update(old_headers)
                    if fallback.get("mode_used") == "playwright" and (fallback.get("html") or ""):
                        self._last_fetch_ok = True
                        self._last_http_status = fallback.get("status") or status
                        return fallback.get("html") or ""

                if getattr(self, "night_mode", False) and "soundcloud" in label.lower():
                    if _sc_is_blocked(status, html):
                        self._last_fetch_ok = False
                        self._flag_sc_blocked(status_code=status, html=html)
                        return None
                if status and 500 <= status < 600 and attempt < max_attempts:
                    self.log_message.emit(
                        f"[Enricher] {label} {status} for {url} (attempt {attempt}/{max_attempts}), retrying..."
                    )
                    time.sleep(1.0)
                    continue
                self._last_fetch_ok = False
                suffix = _format_outcome_suffix(fetch_ok=False, actionable=None, http_status=status)
                self.log_message.emit(f"[Enricher] {label} failed: {exc}{suffix}")
                return None
            except Exception as exc:
                if allow_pw and not pw_attempted:
                    pw_attempted = True
                    old_headers = None
                    if headers:
                        old_headers = dict(self.session.headers)
                        self.session.headers.update(headers)
                    try:
                        fallback = fetch_html(
                            url,
                            session=self.session,
                            directory=getattr(self, "source_dir", None) or None,
                            job_id=None,
                            required_selectors=None,
                            allow_browser_fallback=True,
                            timeout_s=HTTP_TIMEOUT,
                        )
                        if fallback.get("mode_used") == "playwright" and (fallback.get("html") or ""):
                            self._last_fetch_ok = True
                            self._last_http_status = fallback.get("status")
                            return fallback.get("html") or ""
                    except Exception:
                        pass
                    finally:
                        if old_headers is not None:
                            self.session.headers.clear()
                            self.session.headers.update(old_headers)
                self._last_fetch_ok = False
                self._last_http_status = None
                suffix = _format_outcome_suffix(fetch_ok=False)
                self.log_message.emit(f"[Enricher] {label} failed: {exc}{suffix}")
                return None
        return None

    def _lf_resolve_canonical_profile_url(self, original_url: str, html: str) -> str:
        canonical = original_url or ""
        final_url = getattr(self, "_last_final_url", "") or ""
        final_norm = _normalise_url(final_url) if final_url else None
        if final_norm:
            canonical = final_norm
        try:
            soup = BeautifulSoup(html, "html.parser")
            link_tag = soup.find("link", rel=lambda val: val and "canonical" in str(val).lower())
            og_url = soup.find("meta", attrs={"property": "og:url"})
            candidates = []
            if link_tag:
                candidates.append(link_tag.get("href") or "")
            if og_url:
                candidates.append(og_url.get("content") or "")
            for cand in candidates:
                normalised = _normalise_url(cand)
                if normalised:
                    canonical = normalised
                    break
        except Exception:
            pass
        if canonical:
            self._lf_canonical_url_cache[original_url] = canonical
            self._lf_canonical_url_cache.setdefault(canonical, canonical)
        return canonical

    def _fetch_profile_and_build(
        self, profile_url: str, source_dir: str, confidence: Optional[float] = None
    ) -> Optional[EnrichmentPayload]:
        self.log_message.emit(f"[Enricher] Fetching {source_dir} profile: {profile_url}")
        self._last_resolved_profile_url = profile_url
        attempts = LF_SEARCH_RETRY_MAX if source_dir == "lastfm" else 2
        lf_endpoint = "profile" if source_dir == "lastfm" else None
        html = self._fetch_url(profile_url, label=f"{source_dir} profile", max_attempts=attempts, endpoint=lf_endpoint)
        fetched_ok = bool(html)
        if not fetched_ok:
            return None
        if source_dir == "lastfm":
            profile_url = self._lf_resolve_canonical_profile_url(profile_url, html)
            self._last_resolved_profile_url = profile_url
        socials, websites, emails, link_hubs = _extract_links_from_profile(
            html, source_dir, profile_url
        )
        profile_fetch_ok = getattr(self, "_last_fetch_ok", fetched_ok)
        profile_http_status = getattr(self, "_last_http_status", None)
        profile_soup: Optional[BeautifulSoup] = None
        related_artists: List[str] = []
        if source_dir == "bandcamp":
            try:
                profile_soup = BeautifulSoup(html, "html.parser")
            except Exception:
                profile_soup = None
            related_artists = _extract_bandcamp_related_artist_names(
                profile_soup,
                profile_url,
                limit=FESTIVAL_EXPANSION_MAX_RELATED_ARTISTS,
            )
            contact_follow_done = False
            track_follow_done = False
            if not (socials or websites or emails or link_hubs):
                emails |= _extract_emails_from_html_text(html)
            if not (socials or websites or emails or link_hubs):
                contact_url = _bandcamp_pick_internal_follow_url(profile_soup, profile_url)
                if contact_url and not contact_follow_done:
                    contact_follow_done = True
                    before_counts = (
                        len(socials),
                        len(websites),
                        len(emails),
                        len(link_hubs),
                    )
                    contact_html = self._fetch_url(contact_url, label="bandcamp contact")
                    if contact_html:
                        contact_socials, contact_websites, contact_emails, contact_link_hubs = _extract_links_from_profile(
                            contact_html, "bandcamp", contact_url
                        )
                        socials |= contact_socials
                        websites |= contact_websites
                        emails |= contact_emails
                        emails |= _extract_emails_from_html_text(contact_html)
                        link_hubs |= contact_link_hubs
                    after_counts = (
                        len(socials),
                        len(websites),
                        len(emails),
                        len(link_hubs),
                    )
                    contact_actionable = after_counts != before_counts
                    contact_suffix = _format_outcome_suffix(
                        fetch_ok=getattr(self, "_last_fetch_ok", None),
                        actionable=contact_actionable,
                        http_status=getattr(self, "_last_http_status", None),
                    )
                    if contact_actionable:
                        self.log_message.emit(
                            f"[Enricher] Bandcamp contact/about follow succeeded: {contact_url}{contact_suffix}"
                        )
                    else:
                        self.log_message.emit(
                            f"[Enricher] Bandcamp contact/about follow attempted (no actionable found): {contact_url}{contact_suffix}"
                        )
            if not (socials or websites or emails or link_hubs):
                track_url = _bandcamp_pick_first_track_url(profile_soup, profile_url)
                if track_url and not track_follow_done:
                    track_follow_done = True
                    before_counts = (
                        len(socials),
                        len(websites),
                        len(emails),
                        len(link_hubs),
                    )
                    track_html = self._fetch_url(track_url, label="bandcamp track")
                    if track_html:
                        track_socials, track_websites, track_emails, track_link_hubs = _extract_links_from_profile(
                            track_html, "bandcamp", track_url
                        )
                        socials |= track_socials
                        websites |= track_websites
                        emails |= track_emails
                        emails |= _extract_emails_from_html_text(track_html)
                        link_hubs |= track_link_hubs
                    after_counts = (
                        len(socials),
                        len(websites),
                        len(emails),
                        len(link_hubs),
                    )
                    track_actionable = after_counts != before_counts
                    track_suffix = _format_outcome_suffix(
                        fetch_ok=getattr(self, "_last_fetch_ok", None),
                        actionable=track_actionable,
                        http_status=getattr(self, "_last_http_status", None),
                    )
                    if track_actionable:
                        self.log_message.emit(
                            f"[Enricher] Bandcamp track follow succeeded: {track_url}{track_suffix}"
                        )
                    else:
                        self.log_message.emit(
                            f"[Enricher] Bandcamp track follow attempted (no actionable found): {track_url}{track_suffix}"
                        )
        actionable_found = bool(socials or websites or emails or link_hubs)
        live_key = f"{source_dir}_live"
        fetch_ok_flag = profile_fetch_ok
        http_status = profile_http_status
        if not actionable_found:
            if source_dir == "soundcloud":
                # Still return a payload so we can record the matched SoundCloud profile URL even if no external links.
                self.log_message.emit(
                    f"[Enricher] No outbound links on SoundCloud profile, keeping profile URL: {profile_url}"
                    f"{_format_outcome_suffix(fetch_ok=fetch_ok_flag, actionable=False, http_status=http_status)}"
                )
            else:
                self.log_message.emit(
                    f"[Enricher] No actionable data on {source_dir} profile: {profile_url}"
                    f"{_format_outcome_suffix(fetch_ok=fetch_ok_flag, actionable=False, http_status=http_status)}"
                )
                if (
                    source_dir == "bandcamp"
                    and confidence is not None
                    and confidence >= MIN_BC_CONFIDENCE
                ):
                    canonical_url = _canonicalise_bandcamp_url(profile_url) or profile_url
                    payload = EnrichmentPayload(
                        socials=set(),
                        websites=set(),
                        emails=set(),
                        link_hubs=set(),
                        source_dir=source_dir,
                        source_url=canonical_url,
                        source_detail=_format_source_display(source_dir),
                        related_artists=related_artists,
                    )
                    self.log_message.emit(
                        f"[Enricher] Bandcamp: safe match but no actionable fields; returning url-only payload url={canonical_url}"
                        f"{_format_outcome_suffix(fetch_ok=fetch_ok_flag, actionable=False, http_status=http_status)}"
                    )
                    return payload
                return None
        payload = EnrichmentPayload(
            socials=socials,
            websites=websites,
            emails=emails,
            link_hubs=link_hubs,
            source_dir=source_dir,
            source_url=profile_url,
            source_detail=_format_source_display(live_key),
            related_artists=related_artists,
        )
        return payload

    def _soundcloud_people_search_candidates(self, artist_query: str) -> List[Dict[str, Any]]:
        """
        Primary SoundCloud search path: people search endpoint keeps results scoped to artist profiles.
        """
        if _night_sc_engine_enabled(getattr(self, "night_mode", False)):
            engine_cands = _SC_SHARED_ENGINE.find_candidates(artist_query, None, max_results=12)
            # Map into existing shape
            mapped = []
            for cand in engine_cands:
                mapped.append(
                    {
                        "profile_url": cand.get("profile_url") or f"https://soundcloud.com/{cand.get('handle','')}",
                        "handle": cand.get("handle") or "",
                        "display_name": cand.get("display_name") or cand.get("handle") or "",
                        "location": cand.get("location") or cand.get("context") or "",
                        "context": cand.get("context") or "",
                        "score": cand.get("score", 0),
                        "rank_score": cand.get("rank_score", cand.get("score", 0)),
                    }
                )
            if mapped:
                return mapped
        quoted = urllib.parse.quote_plus(artist_query)
        url = f"https://soundcloud.com/search/people?q={quoted}"
        self.log_message.emit(f"[Enricher] SoundCloud live search: {url}")
        html = self._fetch_url(url, label="SoundCloud search")
        candidates = self._parse_soundcloud_search_results(html, url)
        if not candidates:
            api_candidates = self._soundcloud_api_user_search(artist_query)
            if api_candidates:
                self.log_message.emit("[Enricher] SoundCloud API fallback provided candidates.")
                candidates = api_candidates
        return candidates

    def _soundcloud_universal_search_candidates(self, artist_query: str) -> List[Dict[str, Any]]:
        """
        Broader fallback: generic search page that may include tracks/playlists; we filter to profile links.
        """
        quoted = urllib.parse.quote_plus(artist_query)
        url = f"https://soundcloud.com/search?q={quoted}"
        self.log_message.emit(f"[Enricher] SoundCloud universal search fallback: {url}")
        html = self._fetch_url(url, label="SoundCloud universal search")
        candidates = self._parse_soundcloud_search_results(html, url)
        if not candidates:
            api_candidates = self._soundcloud_api_user_search(artist_query)
            if api_candidates:
                self.log_message.emit("[Enricher] SoundCloud API fallback provided candidates.")
                candidates = api_candidates
        return candidates

    def _soundcloud_best_candidate_for_query(
        self,
        artist_name: str,
        query: str,
        location_hint: str,
        genre_hint: str,
    ) -> Optional[Dict[str, Any]]:
        def _is_better_candidate(candidate: Dict[str, Any], current: Optional[Dict[str, Any]]) -> bool:
            if not candidate:
                return False
            cand_score = candidate.get("score", 0)
            cand_rank = candidate.get("rank_score", cand_score)
            curr_score = current.get("score", 0) if current else 0
            curr_rank = current.get("rank_score", curr_score) if current else 0
            if cand_score > curr_score:
                return True
            if curr_score > cand_score + 0.02:
                return False
            return cand_rank > curr_rank

        candidates = self._soundcloud_people_search_candidates(query)
        best_candidate = self._pick_best_soundcloud_candidate(
            artist_name, candidates, location_hint, genre_hint
        )
        if not best_candidate or best_candidate.get("score", 0) < _SC_CONFIDENCE_MIN:
            # Try universal + API fallback using the same query.
            uni_candidates = self._soundcloud_universal_search_candidates(query)
            candidate = self._pick_best_soundcloud_candidate(
                artist_name, uni_candidates, location_hint, genre_hint
            )
            if candidate and _is_better_candidate(candidate, best_candidate):
                best_candidate = candidate
        return best_candidate

    def _parse_soundcloud_search_results(
        self,
        html: Optional[str],
        search_url: str = "",
        max_candidates: int = 12,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        if not html:
            return candidates
        soup = BeautifulSoup(html, "html.parser")
        search_container = soup
        noscript = soup.find("noscript")
        if noscript:
            try:
                search_container = BeautifulSoup(noscript.decode_contents(), "html.parser")
            except Exception:
                search_container = soup
        seen_urls: Set[str] = set()
        # Consider any anchor that looks like a profile link; harvest nearby text as display/location hints.
        anchors = search_container.select("a[href]")
        for anchor in anchors:
            href = (anchor.get("href") or "").strip()
            profile_url = self._normalise_soundcloud_profile_href(href)
            if not profile_url or profile_url in seen_urls:
                continue
            handle = _sc_handle_from_url(profile_url)
            container = (
                anchor.find_parent("li")
                or anchor.find_parent("article")
                or anchor.find_parent("div")
            )
            display_name = anchor.get_text(" ", strip=True)
            location_text = ""
            context_text = ""
            if container:
                context_text = container.get_text(" ", strip=True)
                if not display_name:
                    display_name = context_text[:200]
                loc_el = container.select_one(
                    ".userBadgeListItem__additional, "
                    ".userBadge__additional, "
                    ".userBadgeListItem__metadata, "
                    ".userBadgeListItem__info, "
                    "[data-testid='user-badge-metadata']"
                )
                if loc_el:
                    location_text = loc_el.get_text(" ", strip=True)
            candidates.append(
                {
                    "profile_url": profile_url,
                    "handle": handle,
                    "display_name": display_name,
                    "location": location_text or context_text,
                    "context": context_text,
                }
            )
            seen_urls.add(profile_url)
            if len(candidates) >= max_candidates:
                break
        if not candidates and search_url:
            self.log_message.emit(f"[Enricher] SoundCloud search: no candidates found for {search_url}")
        return candidates

    def _soundcloud_api_user_search(self, artist_query: str, limit: int = 8) -> List[Dict[str, Any]]:
        client_id = _sc_get_client_id(self.session)
        if not client_id:
            self.log_message.emit("[Enricher] SoundCloud API fallback unavailable (no client_id).")
            return []
        params = {
            "q": artist_query,
            "client_id": client_id,
            "limit": limit,
        }
        try:
            resp = self.session.get(
                "https://api-v2.soundcloud.com/search/users",
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json() or {}
        except Exception as exc:
            self.log_message.emit(f"[Enricher] SoundCloud API fallback failed: {exc}")
            return []
        collection = payload.get("collection") if isinstance(payload, dict) else None
        if not isinstance(collection, list):
            return []
        candidates: List[Dict[str, Any]] = []
        for user in collection:
            if not isinstance(user, dict):
                continue
            permalink = user.get("permalink") or ""
            handle = (permalink or "").strip().lower()
            if not handle:
                continue
            profile_url = user.get("permalink_url") or f"https://soundcloud.com/{handle}"
            candidates.append(
                {
                    "profile_url": profile_url,
                    "handle": handle,
                    "display_name": user.get("full_name") or user.get("username") or handle,
                    "location": f"{user.get('city') or ''} {user.get('country_code') or ''}".strip(),
                    "context": user.get("description") or "",
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def _pick_best_soundcloud_candidate(
        self,
        artist_name: str,
        candidates: List[Dict[str, Any]],
        location_hint: str = "",
        genre_hint: str = "",
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        artist_norm_basic = _sc_strip_basic(_sc_normalise_text(artist_name))
        for candidate in candidates:
            handle = candidate.get("handle") or ""
            display = candidate.get("display_name") or ""
            location_text = candidate.get("location") or ""
            score = _sc_score_candidate(
                artist_name,
                display,
                handle,
                location_hint=location_hint,
                candidate_location=location_text,
                genre_hint=genre_hint,
            )
            rank_score = _locale_rank_score(score, location_text, candidate.get("context", ""))
            candidate["score"] = score
            candidate["rank_score"] = rank_score
            candidate_domain = extract_domain(candidate.get("profile_url") or "")
            candidate["match_score"] = self._compute_match_score_for_candidate(
                display or handle, "", candidate_domain
            )
            best_rank = best.get("rank_score", best.get("score", 0)) if best else 0
            best_score = best.get("score", 0) if best else 0
            best_match_score = best.get("match_score", 0) if best else 0
            if best is None:
                best = candidate
                continue
            if candidate["match_score"] > best_match_score:
                best = candidate
                continue
            if candidate["match_score"] + 0.01 < best_match_score:
                continue
            if score > best_score or (
                abs(score - best_score) <= 0.02 and rank_score > best_rank
            ):
                best = candidate
        # Short-name guard: require near-exact match for very short artist names.
        if best and artist_norm_basic and len(artist_norm_basic) <= 3:
            best_basic_display = _sc_strip_basic(_sc_normalise_text(best.get("display_name") or best.get("handle") or ""))
            if not (
                artist_norm_basic == best_basic_display
                or (best.get("score", 0) >= 0.98 and not best_basic_display.split()[1:])
            ):
                self.log_message.emit(
                    f"[Enricher] SoundCloud Enrich: rejecting candidate '{best.get('display_name') or best.get('handle')}' "
                    f"for short artist '{artist_name}' (confidence={best.get('score', 0):.2f})"
                )
                return None
            if best.get("score", 0) < 0.99:
                self.log_message.emit(
                    f"[Enricher] SoundCloud Enrich: rejecting candidate '{best.get('display_name') or best.get('handle')}' "
                    f"for short artist '{artist_name}' (confidence={best.get('score', 0):.2f})"
                )
                return None
        return best

    def _update_progress(self, current: int, total: int) -> None:
        pct = int((current / max(1, total)) * 100)
        self.progress.emit(pct)

    def _soundcloud_people_search_first_profile_url(self, artist_name: str) -> Optional[str]:
        candidates = self._soundcloud_people_search_candidates(artist_name)
        if candidates:
            return candidates[0].get("profile_url")
        return None

    def _soundcloud_universal_search_first_profile_url(self, artist_name: str) -> Optional[str]:
        candidates = self._soundcloud_universal_search_candidates(artist_name)
        for candidate in candidates:
            profile_url = candidate.get("profile_url")
            if profile_url:
                self.log_message.emit(
                    f"[Enricher] SoundCloud universal search candidate profile: {profile_url}"
                )
                return profile_url
        self.log_message.emit("[Enricher] SoundCloud universal search: no fallback candidates.")
        return None

    def _normalise_soundcloud_profile_href(self, href: str) -> Optional[str]:
        if not href:
            return None
        href = href.strip()
        if not href or href.startswith("#"):
            return None
        if href.startswith("//"):
            href = f"https:{href}"
        if href.startswith("/"):
            href = f"https://soundcloud.com{href}"
        elif not href.startswith("http"):
            href = f"https://soundcloud.com/{href.lstrip('/')}"
        parsed = urllib.parse.urlparse(href)
        host = parsed.netloc.lower()
        if host not in {"soundcloud.com", "www.soundcloud.com"}:
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return None
        first_segment = path_parts[0]
        if first_segment in {"search", "pages"}:
            return None
        profile_url = f"https://soundcloud.com/{first_segment}"
        return profile_url


# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------
class _EnricherProgressDialog(QtWidgets.QDialog):
    def __init__(self, worker: CrossDirectoryEnricherWorker, parent=None) -> None:
        super().__init__(parent)
        self.worker = worker
        self.output_path = ""
        self.setWindowTitle("Spotify CSV Enricher")
        self.setModal(True)
        layout = QtWidgets.QVBoxLayout()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
        self.log_console = QtWidgets.QPlainTextEdit()
        self.log_console.setReadOnly(True)
        layout.addWidget(self.log_console)
        self.close_button = QtWidgets.QPushButton("Close")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)
        self.setLayout(layout)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log_message.connect(self._append_log)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _append_log(self, message: str) -> None:
        self.log_console.appendPlainText(message)
        scrollbar = self.log_console.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())

    def _on_finished(self, output_path: str) -> None:
        self.output_path = output_path
        self.close_button.setEnabled(True)
        if not output_path:
            self.log_console.appendPlainText("[Enricher] Finished with errors.")
        else:
            self.log_console.appendPlainText("[Enricher] Done.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_cross_directory_enrichment(
    seed_csv_path: str,
    output_csv_path: str,
    bandcamp_csv_path: str = "",
    soundcloud_csv_path: str = "",
    unearthed_csv_path: str = "",
    lastfm_csv_path: str = "",
    enable_live_search: bool = True,
    max_live_searches: int = LIVE_SEARCH_MAX_ATTEMPTS,
    logger=None,
    night_mode: bool = False,
) -> str:
    """
    Headless wrapper around the existing CrossDirectoryEnricherWorker for programmatic use.
    Preserves existing behaviour; logs via logger callable or prints.
    """

    def _log(msg: str) -> None:
        if not msg:
            return
        if logger:
            try:
                logger(msg)
                return
            except Exception:
                pass
        try:
            print(msg)
        except Exception:
            pass

    worker = CrossDirectoryEnricherWorker(
        seed_csv_path=seed_csv_path,
        output_csv_path=output_csv_path,
        bandcamp_csv_path=bandcamp_csv_path,
        soundcloud_csv_path=soundcloud_csv_path,
        unearthed_csv_path=unearthed_csv_path,
        lastfm_csv_path=lastfm_csv_path,
        enable_live_search=enable_live_search,
        max_live_searches=max_live_searches,
    )
    worker.night_mode = bool(night_mode)

    # Bypass Qt event loop by providing simple emit stubs.
    worker.log_message = type("obj", (), {"emit": _log})
    worker.progress = type("obj", (), {"emit": lambda *args, **kwargs: None})
    worker.finished = type("obj", (), {"emit": lambda *args, **kwargs: None})

    worker._run_impl()
    return output_csv_path


def run_spotify_enricher_dialog(parent: Optional[QtWidgets.QWidget] = None) -> None:
    app = QtWidgets.QApplication.instance()
    parent = parent or (app.activeWindow() if app else None)
    seed_path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent,
        "Select Spotify Seed CSV",
        "",
        "CSV or TSV Files (*.csv *.tsv *.txt);;All Files (*)",
    )
    if not seed_path:
        return
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(seed_path)
    ext = ext or ".csv"
    output_path = f"{base}_enriched_{timestamp}{ext}"
    seed_dir = os.path.dirname(os.path.abspath(seed_path))
    directory_paths = {
        source: _resolve_csv_path(filename, seed_dir) or ""
        for source, filename in DIRECTORY_FILES.items()
    }
    worker = CrossDirectoryEnricherWorker(
        seed_csv_path=seed_path,
        output_csv_path=output_path,
        bandcamp_csv_path=directory_paths.get("bandcamp", ""),
        soundcloud_csv_path=directory_paths.get("soundcloud", ""),
        unearthed_csv_path=directory_paths.get("unearthed", ""),
        lastfm_csv_path=directory_paths.get("lastfm", ""),
        enable_live_search=True,
        max_live_searches=LIVE_SEARCH_MAX_ATTEMPTS,
    )
    dialog = _EnricherProgressDialog(worker, parent=parent)
    dialog.exec_()
    final_path = dialog.output_path
    if final_path:
        QtWidgets.QMessageBox.information(
            parent,
            "Spotify CSV Enricher",
            f"Rows processed: {worker.total_rows}\nOutput saved to:\n{final_path}",
        )
    else:
        QtWidgets.QMessageBox.warning(
            parent,
            "Spotify CSV Enricher",
            "Enrichment did not complete successfully.",
        )
