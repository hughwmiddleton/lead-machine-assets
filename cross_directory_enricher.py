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
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from rapidfuzz import fuzz
from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, pyqtSignal
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, parse_qs, unquote
from unidecode import unidecode

from facebook_enrich import (
    FbCandidate,
    detect_corporate_token,
    extract_fb_category,
    classify_corporate_signals,
    has_corporate_token,
    is_noisy_fb_text_block,
    looks_like_music_fallback,
    clean_fb_category_text,
    MUSIC_CATEGORY_KEYWORDS,
    FB_MUSIC_CATEGORY_TOKENS,
    MUSIC_TOKENS,
    normalize_fb_name,
    score_fb_candidate,
    is_music_page,
    _corporate_hit,
    _looks_corporate,
    _looks_music_related,
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
}

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

DIRECTORY_WEBSITE_COLUMNS = (
    "External Links",
    "Website",
    "Websites",
    "Linktree",
    "Link Tree",
    "Linktr.ee",
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MULTI_VALUE_SEPARATOR = ", "
FACEBOOK_HELPERS_PATH = os.path.join(BASE_DIR, "Lead Machine (Final Update 5).py")
ENRICHER_FB_PROFILE = os.path.join(os.path.expanduser("~"), "LeadMachine", "fb_enricher_profile")
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

    def summary(self) -> str:
        parts = []
        if self.candidate_name:
            parts.append(self.candidate_name)
        if self.source_detail:
            parts.append(self.source_detail)
        if self.source_url:
            parts.append(self.source_url)
        return " | ".join(part for part in parts if part) or (self.source_dir or "")


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


def _row_has_facebook_or_email(row) -> bool:
    """
    Return True if the row already has any Facebook URL or any email address.
    Considers both seed CSV contents and any enrichment already applied.
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

    social = _get("Social Link")
    email = _get("Email")
    candidates = []
    if social:
        candidates.extend([p.strip() for p in re.split(r"[,\s]+", social) if p.strip()])
    has_fb = any("facebook.com" in url.lower() or "fb.me" in url.lower() for url in candidates)
    has_email = bool(email)
    return has_fb or has_email


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

    def find_best_page_url(self, artist_name: str, location: Optional[str] = None) -> Optional[str]:
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
        seen_urls: Set[str] = set()
        candidates: List[FbCandidate] = []
        anchor_candidates = list(soup.select("a[href]")) + list(soup.select('a[role="link"][href*="facebook.com"]')) + list(
            soup.select('div[role="article"] a[href*="facebook.com"]')
        )
        for anchor in anchor_candidates:
            href = cell_to_str(anchor.get("href"))
            if "facebook.com" not in href:
                continue
            if any(fragment in href for fragment in ("sharer.php", "logout.php", "login.php", "l.php")):
                continue
            absolute = urllib.parse.urljoin(FACEBOOK_SEARCH_URL, href)
            normalised = _normalise_url(absolute.split("?", 1)[0])
            if not normalised or "facebook.com" not in normalised:
                continue
            if normalised in seen_urls:
                continue
            parsed = urllib.parse.urlparse(normalised)
            path_parts = [part for part in parsed.path.split("/") if part]
            if not path_parts:
                continue
            if path_parts[0] in {"search", "plugins", "dialog", "privacy"}:
                continue
            name_text = cell_to_str(anchor.get_text(" ", strip=True) or anchor.get("aria-label"))
            # Prefer aria-label if it contains music cues.
            aria_label = cell_to_str(anchor.get("aria-label"))
            if aria_label and any(tok in aria_label.lower() for tok in MUSIC_TOKENS):
                name_text = aria_label
                category_raw = aria_label
            else:
                category_raw = ""
            fallback_name = name_text or (path_parts[0] if path_parts else "") or normalised
            parent = anchor.find_parent(["div", "span"])
            category_raw = category_raw or extract_fb_category(parent, name_text) or ""
            if not category_raw:
                for probe in (aria_label, name_text):
                    if probe and any(tok in probe.lower() for tok in MUSIC_TOKENS):
                        category_raw = probe
                        break
            if not category_raw and parent:
                try:
                    card_blob = parent.get_text(" ", strip=True)
                    if card_blob and any(tok in card_blob.lower() for tok in MUSIC_TOKENS):
                        category_raw = card_blob
                except Exception:
                    pass
            if not category_raw:
                ancestor = parent.find_parent(["div", "section", "article"]) if parent else None
                if ancestor:
                    try:
                        blob = ancestor.get_text(" ", strip=True)
                        if blob and any(tok in blob.lower() for tok in MUSIC_TOKENS):
                            category_raw = blob
                    except Exception:
                        pass
            if not category_raw:
                shells = []
                try:
                    shells.append(anchor.get_text(" ", strip=True))
                except Exception:
                    pass
                for node in (parent, getattr(parent, "parent", None), getattr(getattr(parent, "parent", None), "parent", None)):
                    if not node:
                        continue
                    try:
                        shells.append(node.get_text(" ", strip=True))
                    except Exception:
                        continue
            for blob in shells:
                if blob and any(tok in (blob or "").lower() for tok in MUSIC_TOKENS):
                    category_raw = blob
                    break
            if not category_raw:
                try:
                    for sib in anchor.next_siblings:
                        try:
                            text_blob = getattr(sib, "get_text", lambda *_: str(sib))(" ", strip=True)
                        except Exception:
                            continue
                        if text_blob and any(tok in text_blob.lower() for tok in MUSIC_TOKENS):
                            category_raw = text_blob
                            break
                        parent_sib = getattr(sib, "parent", None)
                        if parent_sib:
                            try:
                                text_blob = parent_sib.get_text(" ", strip=True)
                            except Exception:
                                text_blob = ""
                            if text_blob and any(tok in text_blob.lower() for tok in MUSIC_TOKENS):
                                category_raw = text_blob
                                break
                except Exception:
                    pass
            if not category_raw:
                try:
                    for span in anchor.find_all("span"):
                        span_text = span.get_text(" ", strip=True)
                        if span_text and any(tok in span_text.lower() for tok in MUSIC_TOKENS):
                            category_raw = span_text
                            break
                    if not category_raw and parent:
                        for span in parent.find_all("span"):
                            span_text = span.get_text(" ", strip=True)
                            if span_text and any(tok in span_text.lower() for tok in MUSIC_TOKENS):
                                category_raw = span_text
                                break
                except Exception:
                    pass
            # Last resort: pull any music-tagged text from nearest ancestors/siblings into category.
            if not category_raw:
                context_blobs: List[str] = []
                try:
                    context_blobs.append(anchor.get_text(" ", strip=True))
                except Exception:
                    pass
                for node in (parent, getattr(parent, "parent", None), getattr(getattr(parent, "parent", None), "parent", None)):
                    if not node:
                        continue
                    try:
                        context_blobs.append(node.get_text(" ", strip=True))
                    except Exception:
                        continue
                try:
                    container = anchor.find_parent(["div", "section", "article"])
                    if container:
                        context_blobs.append(container.get_text(" ", strip=True))
                except Exception:
                    pass
                for blob in context_blobs:
                    if blob and any(tok in blob.lower() for tok in MUSIC_TOKENS):
                        category_raw = blob
                        break
            # If we still have no explicit category, see if any nearby text carries music cues and reuse it.
            context_blobs: List[str] = [category_raw or "", aria_label or "", name_text or "", fallback_name or ""]
            try:
                if parent:
                    context_blobs.append(parent.get_text(" ", strip=True))
                    anc = parent.find_parent(["div", "section", "article"])
                    if anc:
                        context_blobs.append(anc.get_text(" ", strip=True))
            except Exception:
                pass
            try:
                for sib in anchor.next_siblings:
                    try:
                        context_blobs.append(getattr(sib, "get_text", lambda *_: str(sib))(" ", strip=True))
                    except Exception:
                        continue
            except Exception:
                pass
            for blob in context_blobs:
                if blob and any(tok in blob.lower() for tok in MUSIC_TOKENS):
                    if not category_raw:
                        category_raw = blob
                    break
            cand.category = category_raw
            candidates.append(
                FbCandidate(
                    name=fallback_name,
                    url=normalised,
                    category=category_raw,
                )
            )
            seen_urls.add(normalised)

        if not candidates:
            slug = normalize_fb_name(artist_name).replace(" ", "")
            if slug and len(slug) >= 4:
                fallback_url = f"https://www.facebook.com/{urllib.parse.quote(slug)}"
                if fallback_url not in seen_urls:
                    candidates.append(
                        FbCandidate(
                            name=artist_name,
                            url=fallback_url,
                            category="",
                        )
                    )
                    seen_urls.add(fallback_url)
                    _safe_log(
                        self.logger,
                        "[FB Enrich] No FB search candidates for '%s'; trying slug fallback '%s'.",
                        artist_name,
                        fallback_url,
                    )
            if not candidates:
                _safe_log(
                    self.logger,
                    "[FB Enrich] No safe Facebook page candidates for '%s'",
                    artist_name,
                )
                return None

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
            if not category:
                return False
            cat = category.lower()
            return any(tok in cat for tok in music_category_tokens)

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
                (cat and any(tok in (cat or "").lower() for tok in FB_MUSIC_CATEGORY_TOKENS))
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
                    (best_candidate.name or "").lower(),
                    (best_candidate.url or "").lower(),
                    (page_category_text or "").lower(),
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


def facebook_find_best_page(
    artist_name: str, location: str, fb_client, logger
) -> Optional[str]:
    artist_name = cell_to_str(artist_name)
    location = cell_to_str(location)
    if not fb_client or not hasattr(fb_client, "find_best_page_url"):
        _safe_log(logger, "[FB Enrich] No Facebook search client available; skipping '%s'.", artist_name)
        return None
    try:
        return fb_client.find_best_page_url(artist_name, location)
    except Exception as exc:
        _safe_log(logger, "[FB Enrich] Facebook search client error for '%s': %s", artist_name, exc)
        return None


def enrich_row_with_facebook(row: dict, logger, fb_client) -> None:
    artist_name = cell_to_str(row.get("Artist Name") or row.get("artist"))
    if not artist_name:
        _safe_log(logger, "[FB Enrich] Skipping row with empty artist name: %r", row)
        return
    existing_links_raw = [
        cell_to_str(row.get("Social Link")),
        cell_to_str(row.get("External Links")),
        cell_to_str(row.get("Facebook_URL")),
    ]
    if any("facebook.com" in value.lower() for value in existing_links_raw if value):
        _safe_log(logger, "[FB Enrich] Row already has Facebook link, skipping: %s", artist_name)
        return
    location = cell_to_str(row.get("Location") or row.get("location"))
    fb_url = facebook_find_best_page(artist_name, location, fb_client, logger)
    if not fb_url:
        return
    fb_url = cell_to_str(fb_url)
    if not fb_url:
        return
    fb_url = fb_url.split("?", 1)[0].rstrip("/")
    normalised = _normalise_url(fb_url)
    if normalised and "facebook.com" in normalised.lower():
        fb_url = normalised
    if "facebook.com" not in fb_url.lower():
        return
    # Only back-fill Social Link if empty; never overwrite existing seed value.
    if not cell_to_str(row.get("Social Link")):
        row["Social Link"] = _append_link(row.get("Social Link", ""), fb_url)
    if "External Links" in row and not cell_to_str(row.get("External Links")):
        row["External Links"] = _append_link(row.get("External Links", ""), fb_url)
    if "Facebook_URL" in row:
        row["Facebook_URL"] = cell_to_str(fb_url)


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

    seen: Set[Tuple[str, str, str, str]] = set()
    keep_indices: List[Any] = []
    best_contact_for_key: Dict[Tuple[str, str, str, str], Tuple[Any, bool]] = {}
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
        if composite in seen:
            _, existing_has_email = best_contact_for_key.get(composite, (None, False))
            # Prefer the row that carries an email if the first kept row lacked one.
            if has_email and not existing_has_email:
                previous_idx, _ = best_contact_for_key[composite]
                try:
                    keep_indices.remove(previous_idx)
                except ValueError:
                    pass
                keep_indices.append(idx)
                best_contact_for_key[composite] = (idx, has_email)
            continue
        seen.add(composite)
        best_contact_for_key[composite] = (idx, has_email)
        keep_indices.append(idx)

    deduped_df = df.loc[keep_indices].copy()
    deduped_df.reset_index(drop=True, inplace=True)
    removed = total_before - len(deduped_df.index)
    _log_dedupe(removed, len(deduped_df.index))
    return deduped_df


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
        self.seed_csv_path = seed_csv_path
        self.output_csv_path = output_csv_path
        self.bandcamp_csv_path = bandcamp_csv_path
        self.soundcloud_csv_path = soundcloud_csv_path
        self.unearthed_csv_path = unearthed_csv_path
        self.lastfm_csv_path = lastfm_csv_path
        self.enable_live_search = enable_live_search
        self.max_live_searches = max_live_searches
        self.session = _build_session()
        self.live_search_attempts = 0
        self._notified_limit = False
        self.total_rows = 0
        self._live_context: Dict[str, Any] = {}
        self._row_enrichment_state: Dict[str, str] = {}

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
            total = len(seed_df.index)
            self.total_rows = total
            if total == 0:
                self.log_message.emit("[Enricher] Seed CSV has no rows; nothing to do.")
                self.finished.emit("")
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
            ]
            match_score_column = "Match_Score"
            for column in required_columns:
                if column not in seed_df.columns:
                    seed_df[column] = ""
                seed_df[column] = seed_df[column].fillna("").astype(str)
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
            self.log_message.emit(f"[Enricher] Starting enrichment for {total} rows...")
            self.log_message.emit(
                f"[Enricher] Live search enabled={self.enable_live_search} max={self.max_live_searches}"
            )
            if self.soundcloud_csv_path:
                self.log_message.emit(
                    f"[Enricher] SoundCloud directory path set -> {self.soundcloud_csv_path}"
                )
            priority = ["bandcamp", "soundcloud", "lastfm", "unearthed"]
            for position, row_idx in enumerate(seed_df.index, start=1):
                row = seed_df.loc[row_idx]
                had_fb_or_email_from_seed = _row_has_facebook_or_email(row)
                artist = _clean_cell(row.get("Artist Name"))
                key = normalise_artist_name(artist)
                track_key = _extract_seed_track_key(row)
                seed_song_title = _extract_seed_track_text(row)
                spotify_domain = extract_domain(_clean_cell(row.get("Spotify_Website_URL", "")))
                self._live_context = {
                    "artist": artist,
                    "location": _clean_cell(row.get("Location")),
                    "track": track_key,
                    "genre": _coerce_directory_value(row.get("Primary Genre")) if "Primary Genre" in row else "",
                    "song_title": seed_song_title,
                    "spotify_domain": spotify_domain,
                    "spotify_id": _clean_cell(row.get("Spotify_Artist_ID")),
                }
                spotify_id = self._live_context.get("spotify_id", "")
                self._init_row_enrichment_state()
                seed_links_by_source = _extract_seed_links_by_source(row)
                if not key:
                    self.log_message.emit(
                        f"[Enricher] Row {position}/{total}: invalid artist name; skipping."
                    )
                    self._update_progress(position, total)
                    continue
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
                # Even if another source enriched the row, optionally try SoundCloud live lookup to attach a profile link/socials when missing.
                if self.enable_live_search and not _coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"]):
                    self.log_message.emit(
                        f"[Enricher] SoundCloud live check for '{artist}' (current SC link missing)."
                    )
                    sc_payload = self._live_search_soundcloud(artist)
                    if sc_payload:
                        applied = self._apply_payload_guarded(
                            seed_df, row_idx, sc_payload, artist, spotify_id=spotify_id
                        )
                        if applied:
                            enriched = True
                            if "soundcloud" not in sources_logged:
                                sources_logged.append("soundcloud")
                if self.enable_live_search:
                    skip_soundcloud = bool(_coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"]))
                    payload = self._live_lookup(artist, skip_soundcloud=skip_soundcloud)
                    if payload:
                        applied = self._apply_payload_guarded(
                            seed_df, row_idx, payload, artist, spotify_id=spotify_id
                        )
                        if applied:
                            enriched = True
                if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:
                    fb_attempted = False
                    fb_matched = False
                    if not self._platform_attempt_allowed("facebook", artist, "Facebook Enrich"):
                        fb_attempted = False
                    else:
                        fb_attempted = True
                        has_fb_or_email_after_directories = _row_has_facebook_or_email(seed_df.loc[row_idx])
                        if had_fb_or_email_from_seed or has_fb_or_email_after_directories:
                            self.log_message.emit(
                                f"[FB Enrich] Skipping Facebook enrichment for '{artist}' (already has Facebook/email from seed or directory enrichment)."
                            )
                        else:
                            current_social_links = [
                                cell_to_str(seed_df.at[row_idx, "Social Link"]),
                                cell_to_str(seed_df.at[row_idx, "External Links"]),
                                cell_to_str(seed_df.at[row_idx, "Facebook_URL"]),
                            ]
                            current_email = cell_to_str(seed_df.at[row_idx, "Email"])
                            has_fb_link = any(
                                isinstance(link, str) and "facebook.com" in link.lower()
                                for link in current_social_links
                                if link
                            )
                            has_email = bool((current_email or "").strip())
                            if has_fb_link or has_email:
                                self.log_message.emit(
                                    f"[FB Enrich] Skipping Facebook enrichment for '{artist}' (already has Facebook/email from directory enrichment)."
                                )
                            else:
                                social_link_val = cell_to_str(row.get("Social Link", ""))
                                external_link_val = cell_to_str(row.get("External Links", ""))
                                fb_url_val = cell_to_str(row.get("Facebook_URL", ""))
                                existing_fb_links: List[str] = []
                                for blob in (social_link_val, external_link_val, fb_url_val):
                                    if not blob:
                                        continue
                                    parts = [part.strip() for part in blob.split(",") if part.strip()]
                                    for part in parts:
                                        if "facebook.com" in part.lower():
                                            existing_fb_links.append(part)
                                fb_emails: List[str] = []
                                page_url_used = ""
                                try:
                                    if existing_fb_links:
                                        fb_candidates = (
                                            [existing_fb_links]
                                            if isinstance(existing_fb_links, str)
                                            else list(existing_fb_links)
                                        )
                                        for candidate in fb_candidates:
                                            candidate_norm = normalize_external_url(candidate)
                                            found = fb_scrape_emails_from_page(
                                                fb_driver, candidate_norm, log_fn=self.log_message.emit
                                            )
                                            try:
                                                current_url = (fb_driver.current_url or "").lower()
                                                if "facebook.com/login" in current_url:
                                                    self.log_message.emit(
                                                        "[FB Enrich] Facebook login wall detected for this page; enrichment skipped (not logged in)."
                                                    )
                                                    found = []
                                            except Exception:
                                                pass
                                            if found:
                                                fb_emails = found
                                                page_url_used = candidate_norm
                                                break
                                    elif fb_find_page_and_emails_by_name:
                                        page_url_used, fb_emails = fb_find_page_and_emails_by_name(
                                            fb_driver,
                                            artist,
                                            location=cell_to_str(row.get("Location") or row.get("location")),
                                            log_fn=self.log_message.emit,
                                        )
                                        try:
                                            current_url = (fb_driver.current_url or "").lower()
                                            if "facebook.com/login" in current_url:
                                                self.log_message.emit(
                                                    "[FB Enrich] Facebook login wall detected for this page; enrichment skipped (not logged in)."
                                                )
                                                fb_emails = []
                                                page_url_used = ""
                                        except Exception:
                                            pass
                                except Exception as exc:
                                    self.log_message.emit(
                                        f"[FB Enrich] Error enriching row {position}/{total} ({artist}): {exc}"
                                    )
                                if fb_emails:
                                    current_email = cell_to_str(seed_df.at[row_idx, "Email"])
                                    if not current_email:
                                        seed_df.at[row_idx, "Email"] = fb_emails[0]
                                    if not existing_fb_links and page_url_used:
                                        if not seed_df.at[row_idx, "Social Link"]:
                                            seed_df.at[row_idx, "Social Link"] = page_url_used
                                    enriched = True
                                    fb_matched = True
                    if fb_attempted and not fb_matched:
                        self._set_platform_state("facebook", "skipped")
                    elif fb_matched:
                        self._set_platform_state("facebook", "matched")
                if not enriched:
                    self.log_message.emit(
                        f"[Enricher] Row {position}/{total}: no enrichment for {artist!r}."
                    )
                self._update_progress(position, total)
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
            "facebook": "pending",
        }

    def _set_platform_state(self, platform: str, status: str) -> None:
        if not hasattr(self, "_row_enrichment_state"):
            self._row_enrichment_state = {}
        self._row_enrichment_state[platform] = status

    def _platform_attempt_allowed(self, platform: str, artist_name: str, label: str) -> bool:
        state = getattr(self, "_row_enrichment_state", {}).get(platform)
        if state in {"matched", "skipped"}:
            prefix = "[FB Enrich]" if platform == "facebook" else "[Enricher]"
            self.log_message.emit(f"{prefix} {label}: skipping '{artist_name}' (already attempted).")
            return False
        return True

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
        original_social_raw = df.at[row_idx, "Social Link"]
        original_sites_raw = df.at[row_idx, "External Links"]
        existing_socials = _split_pipe_cell(original_social_raw)
        existing_sites = _split_pipe_cell(original_sites_raw)
        existing_emails = _split_pipe_cell(df.at[row_idx, "Email"], is_email=True)
        new_socials = set(payload.socials)
        new_sites = set(payload.websites)
        new_emails = set(payload.emails)
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
            ordered_sites = sorted(sites_all)
            if MAX_WEBSITES:
                ordered_sites = ordered_sites[:MAX_WEBSITES]
            df.at[row_idx, "External Links"] = MULTI_VALUE_SEPARATOR.join(ordered_sites)
        elif original_sites_raw:
            df.at[row_idx, "External Links"] = ""
        if emails_all:
            df.at[row_idx, "Email"] = MULTI_VALUE_SEPARATOR.join(sorted(emails_all))
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
                df.at[row_idx, "Source Directory"] = display_value
                df.at[row_idx, "Source URL"] = payload.source_url or ""

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

    def _live_lookup(self, artist_name: str, skip_soundcloud: bool = False) -> Optional[EnrichmentPayload]:
        if not artist_name:
            return None
        if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
            if not self._notified_limit:
                self.log_message.emit(
                    "[Enricher] Live search limit reached; skipping live lookups"
                )
                self._notified_limit = True
            return None
        best_payload: Optional[EnrichmentPayload] = None
        for source in ("bandcamp", "soundcloud", "lastfm"):
            payload = None
            if source == "bandcamp":
                payload = self._live_search_bandcamp(artist_name)
            elif source == "soundcloud":
                if skip_soundcloud:
                    continue
                payload = self._live_search_soundcloud(artist_name)
            elif source == "lastfm":
                payload = self._live_search_lastfm(artist_name)
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
        if not self._platform_attempt_allowed("bandcamp", artist_name, "Bandcamp Enrich"):
            return None
        if not self._increment_live_counter():
            self._set_platform_state("bandcamp", "skipped")
            return None
        song_title = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        queries = build_bandcamp_queries(artist_name, song_title)

        def _search(query: str) -> Tuple[Optional[EnrichmentPayload], float, float]:
            quoted = urllib.parse.quote_plus(query)
            url = f"https://bandcamp.com/search?q={quoted}&item_type=b"
            self.log_message.emit(f"[Enricher] Bandcamp live search: {url}")
            html = self._fetch_url(url, label="Bandcamp search")
            if not html:
                return (None, 0.0, 0.0)
            soup = BeautifulSoup(html, "html.parser")
            first_link = soup.select_one("li.searchresult a.itemurl, li.searchresult a[href*='bandcamp.com']")
            if not first_link:
                self.log_message.emit("[Enricher] Bandcamp search: no results found.")
                return (None, 0.0, 0.0)
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
                self.log_message.emit("[Enricher] Bandcamp search result missing href.")
                return (None, 0.0, 0.0)
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
                    self.log_message.emit(
                        f"[Enricher] Bandcamp candidate '{display_name or profile_url}' rejected for artist '{artist_name}' (name mismatch)."
                    )
                    return (None, 0.0, 0.0)
            confidence = _bandcamp_confidence(artist_name, display_name, profile_url, song_title=song_title if query != artist_name else "")
            rank_confidence = _locale_rank_score(confidence, display_name, profile_url)
            self.log_message.emit(
                f"[Enricher] Bandcamp Enrich: best candidate '{profile_url}' for '{artist_name}' has confidence={confidence:.2f}."
            )
            if confidence >= MIN_BC_CONFIDENCE:
                payload = self._fetch_profile_and_build(profile_url, "bandcamp")
                if payload:
                    payload.match_score = self._compute_match_score_for_candidate(
                        display_name or profile_url,
                        song_title,
                        extract_domain(profile_url),
                    )
                    payload.candidate_name = display_name or ""
                return (payload, confidence, rank_confidence)
            return (None, confidence, rank_confidence)

        best_payload: Optional[EnrichmentPayload] = None
        best_score = 0.0
        best_rank_score = 0.0
        best_match_score = 0.0
        for query in queries:
            payload, confidence, rank_confidence = _search(query)
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
            if best_payload and best_score >= 0.95 and '"' in query:
                break
        if best_payload:
            self._set_platform_state("bandcamp", "matched")
            return best_payload
        best_confidence_display = max(best_score, best_rank_score)
        self.log_message.emit(
            f"[Enricher] Bandcamp Enrich: no safe match for '{artist_name}' (best_confidence={best_confidence_display:.2f}), skipping."
        )
        self._set_platform_state("bandcamp", "skipped")
        return None

    def _live_search_soundcloud(self, artist_name: str) -> Optional[EnrichmentPayload]:
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
        self.log_message.emit(
            f"[Enricher] SoundCloud Enrich: best match '{best_candidate.get('display_name') or best_candidate.get('handle')}' "
            f"({best_candidate.get('profile_url')}), confidence={best_candidate.get('score'):.2f}"
        )
        payload = self._fetch_profile_and_build(best_candidate["profile_url"], "soundcloud")
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
        if not self._platform_attempt_allowed("lastfm", artist_name, "Last.fm Enrich"):
            return None
        song_title = _clean_cell(getattr(self, "_live_context", {}).get("song_title", ""))
        primary_query = build_search_query(artist_name, song_title)
        quoted = urllib.parse.quote_plus(primary_query)
        url = f"https://www.last.fm/search?q={quoted}&type=artist"
        if not self._increment_live_counter():
            self._set_platform_state("lastfm", "skipped")
            return None
        self.log_message.emit(f"[Enricher] Last.fm live search: {url}")
        html = self._fetch_url(url, label="Last.fm search")
        if not html:
            self._set_platform_state("lastfm", "skipped")
            return None
        soup = BeautifulSoup(html, "html.parser")
        first_link = soup.select_one("a[href*='/music/']")
        if not first_link:
            self.log_message.emit("[Enricher] Last.fm search: no artist results.")
            self._set_platform_state("lastfm", "skipped")
            return None
        display_name = first_link.get_text(" ", strip=True)
        profile_url = (first_link.get("href") or "").strip()
        if profile_url.startswith("/"):
            profile_url = f"https://www.last.fm{profile_url}"
        confidence = _lastfm_confidence(artist_name, display_name)
        rank_confidence = _locale_rank_score(confidence, display_name, profile_url)
        best_match_score = self._compute_match_score_for_candidate(
            display_name, song_title, extract_domain(profile_url)
        )
        self.log_message.emit(
            f"[Enricher] Last.fm Enrich: candidate '{display_name or profile_url}' for '{artist_name}' has confidence={confidence:.2f}."
        )
        best_score = confidence
        best_rank_score = rank_confidence
        if confidence < MIN_LF_CONFIDENCE and song_title and primary_query != artist_name:
            fallback_query = artist_name
            self.log_message.emit(
                f"[Enricher] Last.fm Enrich: no safe match for '{artist_name}', trying artist-only query '{fallback_query}'."
            )
            quoted_fb = urllib.parse.quote_plus(fallback_query)
            fb_url = f"https://www.last.fm/search?q={quoted_fb}&type=artist"
            fb_html = self._fetch_url(fb_url, label="Last.fm search (fallback)")
            if fb_html:
                fb_soup = BeautifulSoup(fb_html, "html.parser")
                fb_link = fb_soup.select_one("a[href*='/music/']")
                if fb_link:
                    fb_display = fb_link.get_text(" ", strip=True)
                    fb_profile = (fb_link.get("href") or "").strip()
                    if fb_profile.startswith("/"):
                        fb_profile = f"https://www.last.fm{fb_profile}"
                    fb_conf = _lastfm_confidence(artist_name, fb_display)
                    fb_rank = _locale_rank_score(fb_conf, fb_display, fb_profile)
                    fb_match_score = self._compute_match_score_for_candidate(
                        fb_display, song_title, extract_domain(fb_profile)
                    )
                    self.log_message.emit(
                        f"[Enricher] Last.fm Enrich: fallback candidate '{fb_display or fb_profile}' for '{artist_name}' has confidence={fb_conf:.2f}."
                    )
                    if (
                        fb_match_score > best_match_score
                        or fb_conf > best_score
                        or (
                            abs(fb_conf - best_score) <= 0.02
                            and fb_rank > best_rank_score
                        )
                    ):
                        display_name = fb_display
                        profile_url = fb_profile
                        best_score = fb_conf
                        best_rank_score = fb_rank
                        best_match_score = fb_match_score
        if best_score < MIN_LF_CONFIDENCE:
            self.log_message.emit(
                f"[Enricher] Last.fm Enrich: no safe match for '{artist_name}' (best_confidence={best_score:.2f}), skipping."
            )
            self._set_platform_state("lastfm", "skipped")
            return None
        payload = self._fetch_profile_and_build(profile_url, "lastfm")
        if payload:
            payload.match_score = best_match_score
            payload.candidate_name = display_name or ""
            self._set_platform_state("lastfm", "matched")
            return payload
        self._set_platform_state("lastfm", "skipped")
        return None

    def _fetch_url(self, url: str, label: str) -> Optional[str]:
        max_attempts = 2
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                resp = self.session.get(url, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                return resp.text
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status and 500 <= status < 600 and attempt < max_attempts:
                    self.log_message.emit(
                        f"[Enricher] {label} {status} for {url} (attempt {attempt}/{max_attempts}), retrying..."
                    )
                    time.sleep(1.0)
                    continue
                self.log_message.emit(f"[Enricher] {label} failed: {exc}")
                return None
            except Exception as exc:
                self.log_message.emit(f"[Enricher] {label} failed: {exc}")
                return None
        return None

    def _fetch_profile_and_build(self, profile_url: str, source_dir: str) -> Optional[EnrichmentPayload]:
        self.log_message.emit(f"[Enricher] Fetching {source_dir} profile: {profile_url}")
        html = self._fetch_url(profile_url, label=f"{source_dir} profile")
        if not html:
            return None
        socials, websites, emails, link_hubs = _extract_links_from_profile(
            html, source_dir, profile_url
        )
        if not (socials or websites or emails or link_hubs):
            if source_dir == "soundcloud":
                # Still return a payload so we can record the matched SoundCloud profile URL even if no external links.
                self.log_message.emit(
                    f"[Enricher] No outbound links on SoundCloud profile, keeping profile URL: {profile_url}"
                )
            else:
                self.log_message.emit(
                    f"[Enricher] No actionable data on {source_dir} profile: {profile_url}"
                )
                return None
        live_key = f"{source_dir}_live"
        payload = EnrichmentPayload(
            socials=socials,
            websites=websites,
            emails=emails,
            link_hubs=link_hubs,
            source_dir=live_key,
            source_url=profile_url,
            source_detail=_format_source_display(live_key),
        )
        return payload

    def _soundcloud_people_search_candidates(self, artist_query: str) -> List[Dict[str, Any]]:
        """
        Primary SoundCloud search path: people search endpoint keeps results scoped to artist profiles.
        """
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
