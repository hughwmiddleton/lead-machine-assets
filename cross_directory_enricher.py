#!/usr/bin/env python3
"""Spotify CSV post-processor that can reuse directory CSVs or perform live lookups."""

from __future__ import annotations

import datetime
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
from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, pyqtSignal
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LIVE_SEARCH_MAX_ATTEMPTS = 40  # 0 = no limit
MAX_LINK_HUB_HOPS_PER_ROW = 1
HTTP_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/117.0.0.0 Safari/537.36"
)

ENABLE_FACEBOOK_ENRICHMENT = True
FACEBOOK_SEARCH_URL = "https://www.facebook.com/search/pages/"
FACEBOOK_CATEGORY_KEYWORDS = ("musician", "band", "artist", "music")
FACEBOOK_SEARCH_WAIT_SECONDS = 10

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
    "bandcamp_live": 5,
    "soundcloud_live": 5,
    "lastfm_live": 5,
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


@dataclass
class EnrichmentPayload:
    socials: Set[str] = field(default_factory=set)
    websites: Set[str] = field(default_factory=set)
    emails: Set[str] = field(default_factory=set)
    link_hubs: Set[str] = field(default_factory=set)
    source_dir: Optional[str] = None
    source_url: Optional[str] = None
    source_detail: Optional[str] = None


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
        candidates: List[Tuple[float, str, str]] = []
        for anchor in soup.select("a[href]"):
            href = cell_to_str(anchor.get("href"))
            if "facebook.com" not in href:
                continue
            if any(fragment in href for fragment in ("sharer.php", "logout.php", "login.php", "l.php")):
                continue
            absolute = urllib.parse.urljoin(FACEBOOK_SEARCH_URL, href)
            normalised = _normalise_url(absolute.split("?", 1)[0])
            if not normalised or "facebook.com" not in normalised:
                continue
            parsed = urllib.parse.urlparse(normalised)
            path_parts = [part for part in parsed.path.split("/") if part]
            if not path_parts:
                continue
            if path_parts[0] in {"search", "plugins", "dialog", "privacy"}:
                continue
            name_text = cell_to_str(anchor.get_text(" ", strip=True) or anchor.get("aria-label"))
            context_text = ""
            parent = anchor.find_parent(["div", "span"])
            if parent:
                context_text = cell_to_str(parent.get_text(" ", strip=True))
            score = _facebook_candidate_score(artist_name, location, name_text, context_text)
            if score <= 0:
                continue
            candidates.append((score, normalised, name_text))
        if not candidates:
            _safe_log(self.logger, "[FB Enrich] No Facebook page candidates for '%s'.", artist_name)
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        best_score, best_url, _ = candidates[0]
        _safe_log(self.logger, "[FB Enrich] Chosen page: %s (score %.2f)", best_url, best_score)
        return best_url


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
    row["Social Link"] = _append_link(row.get("Social Link", ""), fb_url)
    if "External Links" in row:
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
            ]
            for column in required_columns:
                if column not in seed_df.columns:
                    seed_df[column] = ""
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
            priority = ["bandcamp", "soundcloud", "lastfm", "unearthed"]
            for position, row_idx in enumerate(seed_df.index, start=1):
                row = seed_df.loc[row_idx]
                artist = _clean_cell(row.get("Artist Name"))
                key = normalise_artist_name(artist)
                track_key = _extract_seed_track_key(row)
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
                    matches_used.extend((source, match) for match in matches)
                    payload = self._payload_from_directory_rows(matches, source)
                    if not payload:
                        continue
                    self._apply_payload(seed_df, row_idx, payload)
                    enriched = True
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
                if not enriched and self.enable_live_search:
                    payload = self._live_lookup(artist)
                    if payload:
                        self._apply_payload(seed_df, row_idx, payload)
                        enriched = True
                if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:
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
        )
        return payload

    def _apply_payload(self, df: pd.DataFrame, row_idx, payload: EnrichmentPayload) -> None:
        existing_socials = _split_pipe_cell(df.at[row_idx, "Social Link"])
        existing_sites = _split_pipe_cell(df.at[row_idx, "External Links"])
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
        if sites_all:
            ordered_sites = sorted(sites_all)
            if MAX_WEBSITES:
                ordered_sites = ordered_sites[:MAX_WEBSITES]
            df.at[row_idx, "External Links"] = MULTI_VALUE_SEPARATOR.join(ordered_sites)
        if emails_all:
            df.at[row_idx, "Email"] = MULTI_VALUE_SEPARATOR.join(sorted(emails_all))
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

    def _live_lookup(self, artist_name: str) -> Optional[EnrichmentPayload]:
        if not artist_name:
            return None
        if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
            if not self._notified_limit:
                self.log_message.emit(
                    "[Enricher] Live search limit reached; skipping live lookups"
                )
                self._notified_limit = True
            return None
        for source in ("bandcamp", "soundcloud", "lastfm"):
            payload = None
            if source == "bandcamp":
                payload = self._live_search_bandcamp(artist_name)
            elif source == "soundcloud":
                payload = self._live_search_soundcloud(artist_name)
                if not payload:
                    cleaned = _normalise_for_soundcloud(artist_name)
                    if cleaned and cleaned != artist_name:
                        payload = self._live_search_soundcloud(cleaned)
            elif source == "lastfm":
                payload = self._live_search_lastfm(artist_name)
            if payload:
                return payload
        return None

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
        quoted = urllib.parse.quote_plus(artist_name)
        url = f"https://bandcamp.com/search?q={quoted}&item_type=b"
        if not self._increment_live_counter():
            return None
        self.log_message.emit(f"[Enricher] Bandcamp live search: {url}")
        html = self._fetch_url(url, label="Bandcamp search")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        first_link = soup.select_one("li.searchresult a.itemurl, li.searchresult a[href*='bandcamp.com']")
        if not first_link:
            self.log_message.emit("[Enricher] Bandcamp search: no results found.")
            return None
        profile_url = (first_link.get("href") or "").strip()
        if not profile_url:
            self.log_message.emit("[Enricher] Bandcamp search result missing href.")
            return None
        return self._fetch_profile_and_build(profile_url, "bandcamp")

    def _live_search_soundcloud(self, artist_name: str) -> Optional[EnrichmentPayload]:
        if not self._increment_live_counter():
            return None
        profile_url = self._soundcloud_people_search_first_profile_url(artist_name)
        if not profile_url:
            self.log_message.emit(
                "[Enricher] SoundCloud people search: no results found, trying universal search..."
            )
            profile_url = self._soundcloud_universal_search_first_profile_url(artist_name)
        if not profile_url:
            self.log_message.emit(
                "[Enricher] SoundCloud search: no results found (people + universal)."
            )
            return None
        return self._fetch_profile_and_build(profile_url, "soundcloud")

    def _live_search_lastfm(self, artist_name: str) -> Optional[EnrichmentPayload]:
        quoted = urllib.parse.quote_plus(artist_name)
        url = f"https://www.last.fm/search?q={quoted}&type=artist"
        if not self._increment_live_counter():
            return None
        self.log_message.emit(f"[Enricher] Last.fm live search: {url}")
        html = self._fetch_url(url, label="Last.fm search")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        first_link = soup.select_one("a[href*='/music/']")
        if not first_link:
            self.log_message.emit("[Enricher] Last.fm search: no artist results.")
            return None
        profile_url = (first_link.get("href") or "").strip()
        if profile_url.startswith("/"):
            profile_url = f"https://www.last.fm{profile_url}"
        return self._fetch_profile_and_build(profile_url, "lastfm")

    def _fetch_url(self, url: str, label: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            self.log_message.emit(f"[Enricher] {label} failed: {exc}")
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

    def _update_progress(self, current: int, total: int) -> None:
        pct = int((current / max(1, total)) * 100)
        self.progress.emit(pct)

    def _soundcloud_people_search_first_profile_url(self, artist_name: str) -> Optional[str]:
        quoted = urllib.parse.quote_plus(artist_name)
        url = f"https://soundcloud.com/search/people?q={quoted}"
        self.log_message.emit(f"[Enricher] SoundCloud live search: {url}")
        html = self._fetch_url(url, label="SoundCloud search")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        first_link = soup.select_one("a.userBadge__title, a[href^='https://soundcloud.com/']")
        if not first_link:
            return None
        profile_url = (first_link.get("href") or "").strip()
        if not profile_url:
            self.log_message.emit("[Enricher] SoundCloud search: result missing href.")
            return None
        if not profile_url.startswith("http"):
            profile_url = f"https://soundcloud.com{profile_url}"
        return profile_url

    def _soundcloud_universal_search_first_profile_url(self, artist_name: str) -> Optional[str]:
        quoted = urllib.parse.quote_plus(artist_name)
        url = f"https://soundcloud.com/search?q={quoted}"
        self.log_message.emit(f"[Enricher] SoundCloud universal search fallback: {url}")
        html = self._fetch_url(url, label="SoundCloud universal search")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        noscript = soup.find("noscript")
        search_container = soup
        if noscript:
            try:
                search_container = BeautifulSoup(noscript.decode_contents(), "html.parser")
            except Exception:
                search_container = soup
        links = search_container.select("a[href]")
        for link in links:
            href = (link.get("href") or "").strip()
            profile_url = self._normalise_soundcloud_profile_href(href)
            if not profile_url:
                continue
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
