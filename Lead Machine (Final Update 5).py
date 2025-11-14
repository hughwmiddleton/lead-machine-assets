#!/usr/bin/env python3
"""
Lead Machine 11

This script scrapes artist data (Page 1) and Facebook pages (Page 2) and exports the results to CSV files.
It detects whether a song has been played on triple j or triple j unearthed by examining the drum logo on
the artist's page. The artist scraping output CSV includes two columns:
"Played on triple J" and "Played on Unearthed" (with "yes" if detected, or blank otherwise).
When scraping Facebook pages (Page 2), these columns are carried over from the input CSV.

Before running this script, please ensure you have installed the following packages:

    pip install pandas tqdm selenium beautifulsoup4 webdriver_manager PyQt5

Usage:
    python lead_machine11.py
"""

from __future__ import annotations

import sys
import subprocess
import platform
import traceback

# ---------------------------
# Dependency Check and Installation
# ---------------------------
required_packages = {
    "pandas": "pandas",
    "tqdm": "tqdm",
    "selenium": "selenium",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "webdriver_manager": "webdriver_manager",
    "PyQt5": "PyQt5",
    "requests": "requests"
}

def install_package(package_name):
    """Install a package using pip."""
    print(f"Installing {package_name}...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])

def check_and_install_dependencies():
    missing_packages = []
    for import_name, package_name in required_packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_packages.append((import_name, package_name))
    if missing_packages:
        print("The following packages are missing:")
        for imp, pkg in missing_packages:
            print(f" - {pkg}")
        ans = input("Would you like to install them now? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            for imp, pkg in missing_packages:
                try:
                    install_package(pkg)
                except Exception as e:
                    print(f"Failed to install {pkg}: {e}")
                    sys.exit(1)
        else:
            print("Dependencies are missing. Exiting.")
            sys.exit(1)

check_and_install_dependencies()

# ---------------------------
# Prevent Sleep on macOS using caffeinate
# ---------------------------
if platform.system() == "Darwin":
    print("Detected macOS – starting caffeinate to prevent sleep.")
    caffeinate_proc = subprocess.Popen(['caffeinate'])
else:
    caffeinate_proc = None

# ---------------------------
# Now import the dependencies
# ---------------------------
import os
import html
import time
import random
import re
import pandas as pd
import datetime
import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup as BS
try:
    from bs4 import FeatureNotFound
except Exception:
    class FeatureNotFound(Exception):
        pass
BeautifulSoup = BS
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import (
    urlparse,
    urljoin,
    parse_qs,
    parse_qsl,
    urlunparse,
    urlencode,
    unquote,
    quote,
)
import urllib.parse as _urlparse
from PyQt5 import QtWidgets, QtCore
from dateutil import parser as dparser
from dateutil.relativedelta import relativedelta
import unicodedata

# ---------------------------
# Bandcamp Configuration
# ---------------------------
BANDCAMP_SEED_TAGS = ["united-kingdom", "london", "manchester", "brighton", "leeds", "bristol", "glasgow"]
BANDCAMP_PAGES_PER_TAG = 5
BANDCAMP_MIN_CONTACT_REQUIREMENT = True
BANDCAMP_DEFAULT_TAG_URL = "https://bandcamp.com/tag/united-kingdom"
BANDCAMP_LOCATION_FALLBACK = False
BANDCAMP_TARGET_ROWS = 40
BANDCAMP_MAX_CANDIDATES = 120
BANDCAMP_DISCOVER_PAGES_DEFAULT = 5
UNEARTHED_DEFAULT_URL = "https://www.abc.net.au/triplejunearthed/music/"

# ---------------------------
# SoundCloud Configuration
# ---------------------------
SOUNDCLOUD_SEED_TAGS = ["indie", "rock", "electronic", "hip-hop", "pop", "alternative", "singer-songwriter", "punk", "garage", "ambient"]
SOUNDCLOUD_PAGES_PER_TAG = 5
SOUNDCLOUD_MIN_CONTACT_REQUIREMENT = True
SOUNDCLOUD_DEFAULT_TAG_URL = "https://soundcloud.com/tags/indie"

# ---------------------------
# SoundCloud FAST mode (fb/email focus)
# ---------------------------
SOUNDCLOUD_FAST_FACEBOOK_EMAIL_ONLY = True
SOUNDCLOUD_FAST_TIMEOUT_SEC = 10
SOUNDCLOUD_FAST_MAX_CANDIDATES = 600
SC_HANDLE_RE = re.compile(r"^https?://soundcloud\.com/([a-z0-9][a-z0-9._-]{1,49})/?$", re.IGNORECASE)
SC_HANDLE_BAN = {
    "feed", "upload", "terms-of-use", "imprint", "transparency-reports", "pages",
    "you", "stream", "discover", "explore", "popular"
}
SC_SOCIAL_SELECTORS = [
    'a[href^="mailto:"]',
    'a[href*="instagram.com"]',
    'a[href*="facebook.com"]',
    'a[href*="linktr.ee"]',
    'a[href*="bandcamp.com"]',
    'a[href*="youtube.com"]',
    'a[href*="tiktok.com"]',
    'a[href*="twitter.com"]',
    'a[href*="x.com"]',
    'a[href*="beacons.ai"]',
    'a[href*="carrd.co"]',
    'a[href*="flow.page"]'
]
SC_AGGREGATOR_HOSTS = ("linktr.ee", "beacons.ai", "bandcamp.com", "carrd.co", "flow.page")
SC_AGGREGATOR_PREFERENCE = ("linktr.ee", "beacons.ai", "bandcamp.com", "carrd.co", "flow.page")
SC_REQUEST_TIMEOUT = (5, 10)
SC_SEARCH_USERS_API = "https://api-v2.soundcloud.com/search/users"
SC_MAX_WORKERS = 8
SC_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soundcloud_about_cache.json")
SC_CACHE_MAX_AGE_DAYS = 7

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
SC_LINK_BATCH_SIZE = 25
SYMBOL_CAT = {"So", "Cs"}
_SC_GENRE_DENY = {"melbourne", "naarm", "australia"}
CONFIG = {}
SC_ABOUT_FIRST = CONFIG.get("SC_ABOUT_FIRST", True)
SC_EXPAND_1HOP = CONFIG.get("SC_EXPAND_1HOP", True)
_SC_CLIENT_ID_LOCK = threading.Lock()
_SC_CLIENT_ID = None
SC_CLIENT_ID_CANDIDATES = ["MaZ7bR62GvbulJgV8EUjQnHfbZGDEKaI"]
SOCIAL_HOSTS = (
    "linktr.ee", "beacons.ai", "bandcamp.com", "carrd.co", "flow.page",
    "instagram.com", "facebook.com", "x.com", "twitter.com", "youtube.com", "tiktok.com",
    "soundcloud.com"
)
SOCIAL_HOSTS_PATTERN = r"(?:linktr\.ee|beacons\.ai|bandcamp\.com|carrd\.co|flow\.page|instagram\.com|facebook\.com|x\.com|twitter\.com|youtube\.com|tiktok\.com)"
URL_RE = re.compile(rf"https?://{SOCIAL_HOSTS_PATTERN}[^\s\"'<)]+", re.IGNORECASE)
HANDLE_RE = re.compile(r"^/[a-z0-9][a-z0-9._-]{1,49}$", re.IGNORECASE)
SC_DISCOVERY_BAN = {
    "feed", "upload", "terms-of-use", "imprint", "transparency-reports", "pages",
    "you", "stream", "discover", "explore", "popular"
}
COUNTRY_CODE_OVERRIDES = {
    "au": "Australia",
    "us": "United States",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "ca": "Canada",
    "nz": "New Zealand",
    "de": "Germany",
    "fr": "France",
    "es": "Spain",
    "it": "Italy",
    "ie": "Ireland",
    "se": "Sweden",
    "no": "Norway",
    "fi": "Finland",
    "dk": "Denmark",
    "nl": "Netherlands",
    "be": "Belgium",
    "br": "Brazil",
    "mx": "Mexico",
    "jp": "Japan"
}

# -----------------------------------------------------------------------------
# Helper: URL Normalization
# -----------------------------------------------------------------------------
def normalize_url(url):
    """Normalize a URL by stripping trailing slashes and converting to lowercase."""
    return url.rstrip('/').lower()


_PARSER_USED = None


def get_soup(html: str):
    """Prefer lxml; fallback to html.parser if lxml is unavailable."""
    global _PARSER_USED
    try:
        soup = BS(html or "", "lxml")
        if _PARSER_USED is None:
            _PARSER_USED = "lxml"
            print("[init] BeautifulSoup parser=lxml")
        return soup
    except FeatureNotFound:
        soup = BS(html or "", "html.parser")
        if _PARSER_USED is None:
            _PARSER_USED = "html.parser"
            print("[init] BeautifulSoup parser=html.parser (fallback)")
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


SC_ASSET_JS_PATTERN = re.compile(r"https://a-v2\.sndcdn\.com/assets/\d+-[a-z0-9]+\.js", re.IGNORECASE)
SC_CLIENT_ID_PATTERN = re.compile(r'client_id:"([a-zA-Z0-9]+)"')


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
    sources = [
        "https://soundcloud.com",
        "https://soundcloud.com/discover",
    ]
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
                print(f"[init] SoundCloud client_id={candidate[:6]}… (predefined)")
                return _SC_CLIENT_ID
        scraped = _sc_scrape_client_id(session)
        if scraped:
            _SC_CLIENT_ID = scraped
            print(f"[init] SoundCloud client_id={scraped[:6]}… (scraped)")
            return _SC_CLIENT_ID
    print("[warn] Unable to acquire SoundCloud client_id.")
    return ""


def _safe_bs(html: str, parser: str = "lxml"):
    if parser == "lxml":
        return get_soup(html)
    try:
        return BS(html, parser)
    except Exception:
        return get_soup(html)


def _extract_handles_generic(html: str):
    doc = get_soup(html)
    handles = []
    for anchor in doc.select('a[href^="/"]'):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if not HANDLE_RE.match(href):
            continue
        slug = href.strip("/").lower()
        if slug in SC_DISCOVERY_BAN:
            continue
        handles.append(slug)
    seen = set()
    ordered = []
    for handle in handles:
        if handle not in seen:
            seen.add(handle)
            ordered.append(handle)
    return ordered


def scrape_handles_from_people_search(session, url: str, limit=None):
    try:
        api_handles = _sc_fetch_people_search_api(session, url, limit)
        if api_handles:
            return api_handles
    except Exception as exc:
        print(f"SoundCloud: people search API fallback triggered ({exc}).")
    resp = session.get(url, timeout=(6, 12), headers=_rand_headers())
    resp.raise_for_status()
    polite_sleep()
    return _extract_handles_generic(resp.text)


def _sc_fetch_people_search_api(session, url: str, limit=None) -> list:
    if not url:
        return []
    client_id = _sc_get_client_id(session)
    if not client_id:
        return []
    parsed = urlparse(url)
    query_params = parse_qs((parsed.query or ""))
    raw_query = (query_params.get("q", [""])[0] or "").strip()
    query_limit = None
    if query_params.get("limit"):
        try:
            query_limit = int(query_params["limit"][0])
        except (ValueError, TypeError):
            query_limit = None
    target_cap = limit if isinstance(limit, int) and limit > 0 else query_limit
    if not target_cap or target_cap <= 0:
        target_cap = 50
    target_cap = max(1, min(target_cap, 500))
    offset = 0
    if query_params.get("offset"):
        try:
            offset = int(query_params["offset"][0])
        except (ValueError, TypeError):
            offset = 0
    passthrough = {}
    for key, values in query_params.items():
        if not values:
            continue
        value = values[0]
        if key in {"q", "limit", "offset"}:
            continue
        passthrough[key] = value
    handles = []
    while len(handles) < target_cap:
        batch_limit = min(50, target_cap - len(handles))
        params = {
            "client_id": client_id,
            "linked_partitioning": 1,
            "limit": batch_limit,
            "offset": offset,
        }
        if raw_query:
            params["q"] = raw_query
        params.update(passthrough)
        resp = session.get(
            SC_SEARCH_USERS_API,
            params=params,
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        collection = data.get("collection") or []
        for item in collection:
            handle = (item.get("permalink") or "").strip().lower()
            if _sc_handle_ok(handle):
                handles.append(handle)
                if len(handles) >= target_cap:
                    break
        if len(handles) >= target_cap:
            break
        next_href = data.get("next_href")
        if not next_href or not collection:
            break
        try:
            parsed_next = urlparse(next_href)
            query_next = parse_qs(parsed_next.query or "")
            offset = int(query_next.get("offset", [offset + batch_limit])[0])
        except Exception:
            offset += batch_limit
    return handles


def scrape_handles_from_tag_page(session, url: str):
    resp = session.get(url, timeout=(6, 12), headers=_rand_headers())
    resp.raise_for_status()
    polite_sleep()
    return _extract_handles_generic(resp.text)


def discover_handles(session, source_url: str, limit=None):
    if not source_url:
        return []
    lowered = source_url.lower()
    if "/search/people" in lowered:
        return scrape_handles_from_people_search(session, source_url, limit=limit)
    if "/tags/" in lowered:
        return scrape_handles_from_tag_page(session, source_url)
    match = re.match(r"^https?://soundcloud\.com/([a-z0-9][a-z0-9._-]{1,49})/?$", source_url, re.IGNORECASE)
    if match:
        return [match.group(1).lower()]
    return []

def _ensure_parent_dir(path: str):
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
    except Exception:
        pass

def _write_empty_csv_with_headers(path: str):
    _ensure_parent_dir(path)
    headers = [
        'Artist Name', 'Location', 'Song Title', 'Sounds Like', 'Social Link', 'SoundCloud Link',
        'Played on triple J', 'Played on Unearthed', 'Release Date', 'Primary Genre', 'Date Added'
    ]
    pd.DataFrame(columns=headers).to_csv(path, index=False, encoding="utf-8-sig")

# -----------------------------------------------------------------------------
# Helper: Drum Status Detection from Page Source using BeautifulSoup
# -----------------------------------------------------------------------------
def get_drum_status_from_source(page_source):
    """
    Parses the page source to determine drum status for the most recent song release.
    
    It looks for the "Played on:" list (<ul> with class "oqAY3 PARBR") and then the first
    list item (<li data-component="ListItem"). Within that <li>:
      - If a screen-reader <span> (data-component="ScreenReaderOnly") is found and its text 
        contains "unearthed", returns "triple j unearthed".
      - Else if an SVG element with data-component "TripleJDrum" is found, returns "triple j".
      - Otherwise, returns an empty string.
    """
    soup = BeautifulSoup(page_source, 'html.parser')
    played_on_list = soup.find("ul", class_="oqAY3 PARBR")
    if played_on_list:
        li = played_on_list.find("li", attrs={"data-component": "ListItem"})
        if li:
            sr_span = li.find("span", attrs={"data-component": "ScreenReaderOnly"})
            if sr_span:
                text = sr_span.get_text().strip().lower()
                if "unearthed" in text:
                    return "triple j unearthed"
                elif "triple j" in text:
                    return "triple j"
            drum_svg = li.find("svg", attrs={"data-component": "TripleJDrum"})
            if drum_svg:
                return "triple j"
    return ""

# -----------------------------------------------------------------------------
# General Driver Setup for Artist Scraping (Headless)
# -----------------------------------------------------------------------------
def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1440,900")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--lang=en-US,en")
    chrome_options.page_load_strategy = 'eager'
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    try:
        driver.set_page_load_timeout(35)
        driver.set_script_timeout(35)
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        })
    except Exception:
        pass
    return driver

# -----------------------------------------------------------------------------
# Facebook Driver Setup (Visible / Head Mode with Optimizations)
# -----------------------------------------------------------------------------
def setup_facebook_driver():
    chrome_options = Options()
    # Run in visible mode for Facebook scraping.
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.page_load_strategy = 'eager'
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

# =============================================================================
# Scraping Functions for Artist Data (Page 1)
# =============================================================================
def scrape_website(url, existing_csv="artist_social_links.csv", max_artists=200):
    driver = setup_driver()
    artist_data = []
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'HU3iy'))
        )
        # Load existing CSV data if available
        existing_data = pd.DataFrame()
        if os.path.exists(existing_csv):
            existing_data = pd.read_csv(existing_csv)
        profile_urls = set()
        while len(profile_urls) < max_artists:
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            artist_links = soup.find_all('a', class_='HU3iy p1_Ju mqDRk FQED6 O_grP', href=True)
            for link in artist_links:
                href = link['href']
                if href.startswith('/triplejunearthed/artist/'):
                    profile_urls.add("https://www.abc.net.au" + href)
            print(f"Found {len(profile_urls)} artist profile URLs so far...")
            try:
                load_more_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "Load more")]'))
                )
                load_more_button.click()
                time.sleep(random.uniform(3, 5))
            except Exception as e:
                print("No more 'Load More' button found or error:", e)
                break
        profile_urls = list(profile_urls)[:max_artists]
        print(f"Total artist profile URLs to scrape: {len(profile_urls)}")
        if not profile_urls:
            print("No artist profile URLs found. Please check the website structure or selectors.")
        for profile_url in profile_urls:
            social_links, location, song_title, sounds_like, artist_name, release_date = scrape_artist_profile(driver, profile_url)
            # Determine drum status from the full page source.
            drum_status_raw = get_drum_status_from_source(driver.page_source)
            played_on_triplej = "yes" if drum_status_raw == "triple j" else ""
            played_on_unearthed = "yes" if drum_status_raw == "triple j unearthed" else ""
            artist_data.append((artist_name, location, song_title, sounds_like, social_links,
                                "", played_on_triplej, played_on_unearthed, release_date, ""))
    except Exception as e:
        print(f"Error during website scraping: {e}")
    finally:
        driver.quit()
    save_to_csv(artist_data, existing_csv)

# ---------------------------
# Unearthed: release date extraction (robust)
# ---------------------------
_UNEARTHED_DATE_PATTERNS = [
    r"\breleased\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
    r"\breleased\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+([A-Za-z]+)\s+(\d{4})",
    r"\b(released|release\s+date|published)\b[:\s]+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
    r"\b(released|release\s+date|published)\b[:\s]+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    r"\b(20\d{2}|19\d{2})\b"
]

def unearthed_extract_release_date(html: str) -> str:
    """
    Best-effort extraction of a release date from an Unearthed artist/track page.
    Strategy:
      1) Look for <time datetime="..."> or time-like elements
      2) Scan meta/aria/accessible-description blocks
      3) Regex search for 'released ...' phrases and common date shapes
    Returns a normalized string if found (prefer YYYY-MM-DD when datetime attr present),
    else returns "".
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) <time datetime="YYYY-MM-DD"> if present (most reliable)
    for t in soup.find_all("time"):
        dtattr = (t.get("datetime") or "").strip()
        if dtattr and re.match(r"^\d{4}-\d{2}-\d{2}$", dtattr):
            return dtattr
        txt = t.get_text(" ", strip=True)
        if txt and re.search(r"\d{4}", txt):
            return txt

    # 2) meta description sometimes contains 'released ...'
    ogd = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if ogd and ogd.get("content"):
        content = ogd["content"]
        if "release" in content.lower() or "released" in content.lower():
            return content

    # 3) Visible text search in likely containers
    blocks = []
    blocks += [b.get_text(" ", strip=True) for b in soup.select("[data-component], .card, .content, .section, .divwU, .fRXHI, main")]
    text = " ".join(blocks) or soup.get_text(" ", strip=True)

    for pat in _UNEARTHED_DATE_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = " ".join(g for g in m.groups() if g) if m.groups() else m.group(0)
        candidate = candidate.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
            return candidate
        return candidate

    return ""

def scrape_artist_profile(driver, profile_url):
    social_links = []
    location = ""
    song_title = ""
    sounds_like = ""
    artist_name = profile_url.split('/')[-1]
    release_date = ""
    exclude_social_urls = {
        "https://www.facebook.com/triplejunearthed",
        "https://www.instagram.com/triple_j_unearthed",
        "https://twitter.com/triplejunearthd",
        "https://www.facebook.com/abc",
        "https://www.instagram.com/abcaustralia",
        "https://twitter.com/abcaustralia"
    }
    try:
        driver.get(profile_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        release_date = unearthed_extract_release_date(page_source) or ""
        links = soup.find_all('a', href=True)
        for link in links:
            href = link['href']
            if any(domain in href for domain in ['facebook.com', 'instagram.com', 'twitter.com', 'spotify.com']):
                if normalize_url(href) in exclude_social_urls:
                    continue
                social_links.append(href)
        location_element = soup.find('div', class_='divwU')
        if location_element:
            location = location_element.get_text(strip=True)
        song_title_element = soup.find('span', class_='fRXHI')
        if song_title_element:
            song_title = song_title_element.get_text(strip=True)
        sounds_like_element = soup.find('h2', string="Sounds Like")
        if sounds_like_element:
            sounds_like_list = sounds_like_element.find_next('p')
            if sounds_like_list:
                sounds_like = sounds_like_list.get_text(strip=True)
    except Exception as e:
        print(f"Error scraping profile {profile_url}: {e}")
    return social_links, location, song_title, sounds_like, artist_name, release_date

def save_to_csv(data, filename):
    _ensure_parent_dir(filename)
    headers = [
        'Artist Name', 'Location', 'Song Title', 'Sounds Like', 'Social Link', 'SoundCloud Link',
        'Played on triple J', 'Played on Unearthed', 'Release Date', 'Primary Genre', 'Date Added'
    ]
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    if not data:
        pd.DataFrame(columns=headers).to_csv(filename, index=False, encoding="utf-8-sig")
        print(f"Created empty CSV with headers at {filename}")
        return

    existing_data = pd.DataFrame()
    if os.path.exists(filename):
        try:
            existing_data = pd.read_csv(filename)
        except Exception:
            existing_data = pd.DataFrame()
        for col in headers:
            if col not in existing_data.columns:
                existing_data[col] = ""

    new_data = []
    expected_fields = len(headers) - 1  # exclude Date Added (set during write)
    for entry in data:
        entry_list = list(entry)
        while len(entry_list) < expected_fields:
            entry_list.append("")
        artist_name, location, song_title, sounds_like, social_links, soundcloud_link, played_on_triplej, played_on_unearthed, release_date, primary_genre = entry_list[:expected_fields]
        if isinstance(social_links, (str, bytes)):
            links_iterable = [social_links] if social_links else []
        else:
            links_iterable = list(social_links or [])
        if not links_iterable:
            links_iterable = [""]
        for link in links_iterable:
            new_data.append({
                'Artist Name': artist_name,
                'Location': location,
                'Song Title': song_title,
                'Sounds Like': sounds_like,
                'Social Link': link,
                'SoundCloud Link': soundcloud_link,
                'Played on triple J': played_on_triplej,
                'Played on Unearthed': played_on_unearthed,
                'Release Date': release_date,
                'Primary Genre': primary_genre,
                'Date Added': current_date
            })

    combined = pd.concat([existing_data, pd.DataFrame(new_data)], ignore_index=True)
    if not combined.empty:
        combined = combined.drop_duplicates(subset=['Artist Name', 'Social Link'])
    combined.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"Data saved to {filename}")


def save_soundcloud_csv(rows, filename):
    _ensure_parent_dir(filename)
    headers = [
        'Artist Name', 'Location', 'Song Title', 'Sounds Like', 'Social Link', 'SoundCloud Link',
        'Played on triple J', 'Played on Unearthed', 'Release Date', 'Primary Genre', 'Date Added',
        'Email'
    ]
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    if not rows:
        pd.DataFrame(columns=headers).to_csv(filename, index=False, encoding="utf-8-sig")
        print(f"SoundCloud: created empty CSV with headers at {filename}")
        return

    existing_data = pd.DataFrame()
    if os.path.exists(filename):
        try:
            existing_data = pd.read_csv(filename)
        except Exception:
            existing_data = pd.DataFrame()
    for col in headers:
        if col not in existing_data.columns:
            existing_data[col] = ""

    new_df = pd.DataFrame(rows)
    for col in headers:
        if col not in new_df.columns:
            new_df[col] = ""
    new_df["Date Added"] = current_date
    for col in ["Social Link", "SoundCloud Link", "Location", "Artist Name", "Primary Genre", "Email"]:
        if col in new_df.columns:
            new_df[col] = new_df[col].fillna("").astype(str)

    combined = pd.concat([existing_data, new_df], ignore_index=True, sort=False)
    combined = combined[headers]
    combined = combined.drop_duplicates(subset=["SoundCloud Link", "Social Link", "Email"])
    combined.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"SoundCloud: data saved to {filename}")

# =========================== Bandcamp Scraper ===========================

# ---------------------------
# Bandcamp release date extraction (robust)
# ---------------------------
_BC_RELEASE_PATTERNS = [
    r"\breleased\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
    r"\breleased\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+([A-Za-z]+)\s+(\d{4})",
    r"\breleased\s+(\d{4})"
]

def _parse_any_date_to_iso(text: str):
    """
    Try to parse any human date into ISO YYYY-MM-DD.
    Returns (date_iso, precision) where precision is 'day'|'month'|'year'.
    """
    if not text:
        return None, None
    text_clean = " ".join(text.split())
    try:
        dt = dparser.parse(
            text_clean,
            fuzzy=True,
            dayfirst=False,
            default=datetime.datetime(1900, 1, 1)
        )
        year = dt.year
        now_year = datetime.datetime.now().year
        if 2000 <= year <= now_year + 1:
            return dt.strftime("%Y-%m-%d"), "day"
    except Exception:
        pass
    month_match = re.search(r"\b([A-Za-z]+)\s+(\d{4})\b", text_clean)
    if month_match:
        try:
            dt = dparser.parse(
                f"01 {month_match.group(1)} {month_match.group(2)}",
                fuzzy=True,
                dayfirst=True
            )
            return dt.strftime("%Y-%m-%d"), "month"
        except Exception:
            pass
    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", text_clean)
    if year_match:
        year = int(year_match.group(1))
        now_year = datetime.datetime.now().year
        if 2000 <= year <= now_year + 1:
            return f"{year:04d}-01-01", "year"
    return None, None

def _extract_from_json_ld(soup) -> tuple:
    """Scan all JSON-LD blocks for datePublished/uploadDate/dateCreated."""
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            for key in ("datePublished", "uploadDate", "dateCreated"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    date_iso, prec = _parse_any_date_to_iso(val)
                    if date_iso:
                        return date_iso, prec, val
    return None, None, None

def _extract_from_meta(soup) -> tuple:
    metas = []
    metas += soup.select('meta[itemprop="datePublished"]')
    metas += soup.select('meta[itemprop="dateCreated"]')
    metas += soup.select('meta[name="date"]')
    metas += soup.select('meta[property="music:release_date"]')
    for meta in metas:
        val = (meta.get("content") or meta.get("value") or "").strip()
        if val:
            date_iso, prec = _parse_any_date_to_iso(val)
            if date_iso:
                return date_iso, prec, val
    og_meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if og_meta:
        desc = (og_meta.get("content") or "").strip()
        if "released" in desc.lower():
            date_iso, prec = _parse_any_date_to_iso(desc)
            if date_iso:
                return date_iso, prec, desc
    return None, None, None

def _extract_from_time_tag(soup) -> tuple:
    for time_el in soup.find_all("time"):
        dt_attr = (time_el.get("datetime") or "").strip()
        if dt_attr:
            date_iso, prec = _parse_any_date_to_iso(dt_attr)
            if date_iso:
                return date_iso, prec, dt_attr
        text = time_el.get_text(" ", strip=True)
        if text:
            date_iso, prec = _parse_any_date_to_iso(text)
            if date_iso:
                return date_iso, prec, text
    return None, None, None

def _extract_from_text_released(soup) -> tuple:
    containers = []
    containers += soup.select(".tralbum-credits")
    containers += soup.select(".tralbumData")
    containers += soup.select("#trackInfoInner, #bio-container")
    collected_text = " ".join([c.get_text(" ", strip=True) for c in containers]) or soup.get_text(" ", strip=True)
    for pattern in _BC_RELEASE_PATTERNS:
        match = re.search(pattern, collected_text, flags=re.IGNORECASE)
        if match:
            raw = match.group(0)
            date_iso, prec = _parse_any_date_to_iso(raw)
            if date_iso:
                return date_iso, prec, raw
    return None, None, None

def bandcamp_extract_release_date(html: str) -> dict:
    """
    Robust extractor. Order: JSON-LD -> meta -> <time> -> free-text 'released ...'
    Returns dict with keys date_iso, precision, raw.
    """
    soup = BeautifulSoup(html, "html.parser")
    extractors = (
        _extract_from_json_ld,
        _extract_from_meta,
        _extract_from_time_tag,
        _extract_from_text_released,
    )
    for extractor in extractors:
        try:
            date_iso, precision, raw = extractor(soup)
            if date_iso:
                return {"date_iso": date_iso, "precision": precision, "raw": raw}
        except Exception:
            continue
    return {"date_iso": None, "precision": None, "raw": None}

# ---------------------------
# Bandcamp genres (tags) + sounds-like extraction
# ---------------------------
_BC_SOUNDS_PATTERNS = [
    r"\bffo\b[:\-–]\s*([^.;\n]+)",
    r"\briyl\b[:\-–]\s*([^.;\n]+)",
    r"\bfor\s+fans\s+of\b[:\-–]?\s*([^.;\n]+)",
    r"\bsounds\s+like\b[:\-–]?\s*([^.;\n]+)",
    r"\binfluences?\b[:\-–]?\s*([^.;\n]+)",
    r"\binspired\s+by\b[:\-–]?\s*([^.;\n]+)",
]

def _norm_tokens(line: str) -> list:
    """Split a comma/pipe/slash separated line into clean tokens."""
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

def bandcamp_extract_genres(soup) -> list:
    """Collect Bandcamp tags/genres from artist or album pages."""
    tags = set()
    for anchor in soup.select(".tralbum-tags a, a.tag, #tags a"):
        txt = anchor.get_text(" ", strip=True)
        if txt:
            tags.add(txt.lower())
    meta_keywords = soup.select_one('meta[name="keywords"]')
    if meta_keywords and meta_keywords.get("content"):
        for token in _norm_tokens(meta_keywords["content"]):
            if token:
                tags.add(token.lower())
    return list(tags)

def bandcamp_extract_sounds_like(soup) -> str:
    """Pull FFO/RIYL/sounds-like phrases from descriptive text."""
    blocks = []
    blocks += [b.get_text(" ", strip=True) for b in soup.select("#bio-container, .tralbum-credits, .tralbumData, #trackInfoInner")]
    desc_meta = soup.select_one('meta[property="og:description"], meta[name="description"]')
    if desc_meta and desc_meta.get("content"):
        blocks.append(desc_meta["content"])
    text = " \n".join(filter(None, blocks))
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _BC_SOUNDS_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.group(1):
            tokens = _norm_tokens(match.group(1))
            if tokens:
                return ", ".join(t.title() for t in tokens[:5])
    fallback = re.search(r"\b(ffo|riyl)\b[:\-–]\s*([^.;\n]+)", text, flags=re.IGNORECASE)
    if fallback and fallback.group(2):
        tokens = _norm_tokens(fallback.group(2))
        if tokens:
            return ", ".join(t.title() for t in tokens[:5])
    return ""

# ---------------------------
# Bandcamp: card-level genre extraction (uses <p class="genre">)
# ---------------------------
def bandcamp_extract_primary_genre_from_card(card) -> str:
    """
    Extracts the visible genre displayed directly beneath the album title/artist
    on Bandcamp discover/tag grids (inside <p class="genre"> ... </p>).
    Falls back to text in the .meta container if the class is absent.
    """
    genre_el = card.select_one("p.genre")
    if genre_el:
        txt = genre_el.get_text(" ", strip=True)
        if txt:
            return txt.lower()
    meta_el = card.select_one(".meta, .result-info")
    if meta_el:
        lines = meta_el.get_text("\n", strip=True).split("\n")
        for line in reversed(lines):
            if line and "by " not in line.lower() and len(line) < 40:
                return line.lower()
    alt = card.select_one("[class*='genre']")
    if alt:
        txt = alt.get_text(" ", strip=True)
        if txt:
            return txt.lower()
    return ""

def _bandcamp_card_candidates_with_genre(soup, base_url) -> list:
    """
    Returns list of dicts with candidate URLs and primary genres from Bandcamp grids.
    """
    selectors = ["li.results-grid-item", ".discover-results .item", ".music-grid .item"]
    out = []
    seen = set()
    excluded_hosts = {
        "bandcamp.com",
        "store.bandcamp.com",
        "daily.bandcamp.com",
        "blog.bandcamp.com",
        "community.bandcamp.com",
        "supporters.bandcamp.com"
    }
    for selector in selectors:
        for card in soup.select(selector):
            href = None
            for anchor in card.select("a[href]"):
                raw_href = (anchor.get("href") or "").strip()
                if not raw_href:
                    continue
                if raw_href.startswith("//"):
                    candidate = f"https:{raw_href}"
                elif raw_href.startswith("http"):
                    candidate = raw_href
                elif raw_href.startswith("/"):
                    candidate = urljoin(base_url, raw_href)
                elif "bandcamp.com" in raw_href:
                    candidate = f"https://{raw_href.lstrip('/')}"
                else:
                    continue
                lowered = candidate.lower()
                if any(token in lowered for token in ["/album", "/track", ".bandcamp.com"]):
                    href = candidate
                    break
            if not href or href in seen:
                continue
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if not host.endswith("bandcamp.com") or host in excluded_hosts:
                continue
            seen.add(href)
            out.append({
                "url": href,
                "primary_genre": bandcamp_extract_primary_genre_from_card(card)
            })
    return out

_BANDCAMP_EXCLUDED_HOSTS = {
    "bandcamp.com",
    "store.bandcamp.com",
    "daily.bandcamp.com",
    "blog.bandcamp.com",
    "community.bandcamp.com",
    "supporters.bandcamp.com"
}

def _bandcamp_extract_tag_from_url(url: str) -> str | None:
    if not url:
        return None
    match = re.search(r"/tag/([^/?#]+)", url)
    if match:
        return match.group(1).lower()
    return None

def _bandcamp_is_discover_url(url: str) -> bool:
    if not url:
        return False
    normalized = url.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized.lstrip('/')}"
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    host = parsed.netloc.lower()
    return "bandcamp.com" in host and "/discover" in parsed.path

def _bandcamp_replace_query_param(url: str, key: str, value) -> str:
    if not url:
        return ""
    normalized = url.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized.lstrip('/')}"
    parsed = urlparse(normalized)
    pairs = []
    replaced = False
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k == key:
            if not replaced:
                pairs.append((k, str(value)))
                replaced = True
            continue
        pairs.append((k, v))
    if not replaced:
        pairs.append((key, str(value)))
    new_query = urlencode(pairs)
    return urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

def _bandcamp_build_discover_page_urls(base_url: str, pages: int) -> list[str]:
    if pages <= 0:
        pages = 1
    urls = []
    for index in range(pages):
        urls.append(_bandcamp_replace_query_param(base_url, "p", index))
    return urls

def _bandcamp_label_from_discover_url(url: str) -> str:
    if not url:
        return "discover:custom"
    try:
        parsed = urlparse(url)
    except Exception:
        return "discover:custom"
    params = parse_qs(parsed.query)
    parts = []
    loc = (params.get("loc") or [""])[0]
    if loc:
        parts.append(f"loc={loc}")
    genre = (params.get("g") or [""])[0]
    if genre:
        parts.append(f"genre={genre}")
    sort_mode = (params.get("s") or [""])[0]
    if sort_mode:
        parts.append(f"sort={sort_mode}")
    if not parts:
        return "discover:custom"
    return "discover:" + ",".join(parts)

def _normalize_location_text(value: str) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.strip().lower()

def _norm_text_(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    return re.sub(r"[\s,;|/]+", " ", value.strip())

def _nf(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s)

def _bc_normalize_tag_slug(city_or_tag: str) -> str:
    s = (city_or_tag or "").strip().lower()
    s = re.sub(r"[,+]+", " ", s)
    s = re.sub(r"[\s/_]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s

def _bc_make_tag_url(slug: str, page: int) -> str:
    slug = _bc_normalize_tag_slug(slug)
    if not slug:
        return ""
    return f"https://bandcamp.com/tag/{quote(slug)}?tab=artists&page={int(page)}"

def _bc_is_bandcamp_url(u: str) -> bool:
    try:
        parsed = urlparse(u or "")
        return parsed.scheme in ("http", "https") and (parsed.netloc or "").lower().endswith("bandcamp.com")
    except Exception:
        return False

def _bc_url_kind(u: str) -> str:
    if not _bc_is_bandcamp_url(u):
        return "unknown"
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    if host == "bandcamp.com" and path.startswith("/discover"):
        return "discover"
    if host == "bandcamp.com" and "/tag/" in path:
        return "tag"
    if host.endswith(".bandcamp.com"):
        if "/album/" in path or "/track/" in path:
            return "album_or_track"
        return "artist_profile"
    if "/album/" in path or "/track/" in path:
        return "album_or_track"
    return "unknown"

def _bc_parse_discover_params(u: str) -> dict:
    parsed = urlparse(u or "")
    query = parse_qs(parsed.query or "")
    loc = (query.get("loc", [""])[0] or "").strip()
    sort_val = (query.get("s", ["new"])[0] or "new").strip() or "new"
    try:
        page = int((query.get("p", ["1"])[0] or "1").strip() or "1")
    except Exception:
        page = 1
    return {"loc": loc, "s": sort_val, "p": page}

def _bc_api_params_from_url(url: str) -> dict:
    parsed = urlparse((url or "").strip())
    query = parse_qs(parsed.query or "")
    params = {k: (v[-1] if isinstance(v, list) else v) for k, v in query.items()}
    base = {}
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    if segments and segments[0] in {"discover", "tag"} and len(segments) > 1:
        slug = _bc_normalize_tag_slug(segments[-1])
        if slug:
            base["t"] = slug
    if "tag" in params and not base.get("t"):
        slug = _bc_normalize_tag_slug(params["tag"])
        if slug:
            base["t"] = slug
    if not base.get("t") and params.get("t"):
        slug = _bc_normalize_tag_slug(params["t"])
        if slug:
            base["t"] = slug
    base["s"] = params.get("s") or params.get("sort") or "new"
    base["g"] = params.get("g") or "all"
    for key in ("loc", "f", "time", "format", "item_type", "c"):
        if key in params:
            base[key] = params[key]
    if params.get("tab") == "artists":
        base.setdefault("item_type", "a")
    return base

def _bc_discover_city_label_from_html(driver, url: str) -> str:
    try:
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        soup = BeautifulSoup(driver.page_source, "html.parser")
        text_block = soup.get_text(" ", strip=True)
        match = re.search(r"artists\s+from\s+([A-Za-z\-\s\.’']+)", text_block, re.IGNORECASE)
        if match:
            city = match.group(1).strip()
            if _is_valid_city_label(city):
                return city
        label = soup.select_one("[aria-pressed='true'], .selected, .active")
        if label:
            txt = label.get_text(" ", strip=True)
            match = re.search(r"artists\s+from\s+([A-Za-z\-\s\.’']+)", txt, re.IGNORECASE)
            if match:
                city = match.group(1).strip()
                if _is_valid_city_label(city):
                    return city
    except Exception:
        pass
    return ""

def _bc_discover_city_label_from_api(items: list) -> str:
    counts = {}
    for item in items or []:
        loc = (item.get("location_text") or "").strip()
        if not loc:
            continue
        city = (loc.split(",")[0] or "").strip()
        if not city:
            continue
        key = _nf(city)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    key = max(counts, key=counts.get)
    for item in items or []:
        loc = (item.get("location_text") or "").strip()
        if not loc:
            continue
        city = (loc.split(",")[0] or "").strip()
        if city and _nf(city) == key and _is_valid_city_label(city):
            return city
    return ""

def _bc_discover_api_fetch_items(params: dict) -> list:
    api_url = "https://bandcamp.com/api/discover/3/get_web"
    session = build_hardened_session()
    per_page = 40
    page_index = max(int(params.get("p", 1)) - 1, 0)
    query = {
        "loc": params.get("loc", ""),
        "s": params.get("s", "new") or "new",
        "p": page_index,
        "limit": per_page,
        "offset": page_index * per_page,
    }
    try:
        resp = session.get(api_url, params=query, timeout=(6, 15), headers=_rand_headers())
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        print(f"Bandcamp: discover API fetch failed: {exc}")
        return []
    return payload.get("items") or []
def _norm_txt(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value)


_COUNTRY_CANON = {
    "the netherlands": "netherlands",
    "holland": "netherlands",
    "u.k.": "united kingdom",
    "u.s.a.": "usa",
}

_INVALID_DISCOVER_SLUGS = {
    "anywhere-fresh-clear-all-filters-artist-index-new-releases-about-buttons",
}


def _is_valid_city_label(city: str) -> bool:
    slug = _bc_normalize_tag_slug(city)
    return bool(slug and slug not in _INVALID_DISCOVER_SLUGS)


def _canon_location(loc: str) -> str:
    loc = (loc or "").strip()
    if not loc:
        return ""
    parts = [part.strip() for part in loc.split(",")]
    if len(parts) >= 2:
        city = parts[0]
        country = ", ".join(parts[1:]).lower()
        country = _COUNTRY_CANON.get(country, country)
        city_tc = " ".join(word.capitalize() if word.isalpha() else word for word in re.split(r"\s+", city))
        country_tc = " ".join(word.capitalize() for word in country.split())
        return f"{city_tc}, {country_tc}"
    return loc

_BAD_CONTACT_DOMAINS = (
    "get.bandcamp.help",
    "help.bandcamp.com",
    "f0.bcbits.com",
    "f1.bcbits.com",
    "f2.bcbits.com",
    "f3.bcbits.com",
    "f4.bcbits.com",
    "f5.bcbits.com",
    "popplers5.bandcamp.com",
)

_BAD_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".mp3", ".flac", ".wav", ".zip")


def _bandcamp_contact_is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if not host:
            return False
        if any(host.endswith(bad) for bad in _BAD_CONTACT_DOMAINS):
            return False
        if any(path.endswith(ext) for ext in _BAD_EXTS):
            return False
        if parsed.scheme in ("mailto", "tel"):
            return True
        if parsed.scheme not in ("http", "https"):
            return False
        if host.endswith("bandcamp.com"):
            return False
        return True
    except Exception:
        return False


def _bandcamp_contact_is_valid(url: str) -> bool:
    return _bandcamp_contact_is_valid_url(url)

def _bandcamp_collect_contacts(artist_dict: dict) -> list:
    contacts = []
    website = artist_dict.get("website")
    if website and _bandcamp_contact_is_valid(website):
        contacts.append(website)
    socials = artist_dict.get("socials", {})
    for value in socials.values():
        if value and _bandcamp_contact_is_valid(value):
            contacts.append(value)
    email_value = artist_dict.get("email")
    if email_value:
        contacts.append(f"mailto:{email_value}")
    seen = set()
    deduped = []
    for link in contacts:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped

def _bandcamp_location_match_(profile_loc: str, api_hint: str, requested_label: str | None, requested_hint: str | None) -> bool:
    if not requested_label and not requested_hint:
        return True
    profile_norm = _norm_text_(profile_loc)
    api_norm = _norm_text_(api_hint)
    if requested_label:
        label_norm = _norm_text_(requested_label)
        if label_norm and (label_norm in profile_norm or label_norm in api_norm):
            return True
    if requested_hint:
        hint_norm = _norm_text_(requested_hint)
        if hint_norm and (hint_norm in profile_norm or hint_norm in api_norm):
            return True
    return False

_BANDCAMP_CONTACT_PRIORITY = [
    ("linktr.ee", "linktree", "beacons.ai", "solo.to", "carrd.co", "band.link"),
    ("instagram.com",),
    ("soundcloud.com",),
    ("youtube.com", "youtu.be"),
    ("facebook.com", "fb.me", "m.me"),
    ("twitter.com", "x.com"),
    ("spotify.com",),
    ("bandcamp.com",),
]

def _best_contact_(links: list[str]) -> str:
    if not links:
        return ""
    cleaned = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            cleaned.append(link)
    for group in _BANDCAMP_CONTACT_PRIORITY:
        for link in cleaned:
            lowered = link.lower()
            if any(dom in lowered for dom in group):
                return link
    return cleaned[0]

def _bandcamp_parse_discover_params(url: str) -> dict:
    result = {"g": "all", "s": "new"}
    if not url:
        return result
    normalized = url.strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized.lstrip('/')}"
    parsed = urlparse(normalized)
    params = {}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if not values:
            continue
        if key.lower().startswith("utm_"):
            continue
        params[key.lower()] = values[-1]
    if params.get("g"):
        result["g"] = params["g"]
    if params.get("s"):
        result["s"] = params["s"]
    loc_value = params.get("loc") or params.get("location")
    if loc_value:
        result["loc"] = loc_value
        if "loct" in params:
            result["loct"] = params.get("loct")
        else:
            result["loct"] = params.get("location_type", "1")
    tag_value = params.get("t") or params.get("tag")
    if tag_value:
        result["t"] = tag_value
    return result

def _bandcamp_location_label_from_url(url: str) -> dict:
    if not _bandcamp_is_discover_url(url):
        return {"display_label": "", "hint": ""}
    params = _bandcamp_parse_discover_params(url)
    loc_value = params.get("loc") or ""
    display_label = ""
    try:
        session = build_hardened_session()
        response = session.get(url, headers=_rand_headers(), timeout=(6, 15))
        response.raise_for_status()
        match = re.search(r'id="DiscoverApp"[^>]+data-blob="([^"]+)"', response.text)
        if match:
            blob = json.loads(html.unescape(match.group(1)))
            locations = (
                blob.get("appData", {})
                    .get("initialState", {})
                    .get("locations", [])
            )
            for entry in locations:
                if str(entry.get("id")) == str(loc_value):
                    display_label = entry.get("label", "") or ""
                    break
    except Exception:
        display_label = ""
    if not display_label and loc_value and not loc_value.isdigit():
        display_label = loc_value
    hint = ""
    if display_label:
        hint = display_label
    elif loc_value.isdigit():
        hint = f"loc:{loc_value}"
    elif loc_value:
        hint = loc_value
    return {"display_label": display_label, "hint": hint}


def _bandcamp_fetch_profile_html(profile_url: str, session=None) -> str:
    session = session or _bandcamp_thread_session()
    try:
        response = session.get(profile_url, timeout=(6, 15), headers=_rand_headers())
        response.raise_for_status()
        return response.text
    except Exception:
        return ""

def _bandcamp_quick_visit(driver, profile_url: str) -> str:
    try:
        driver.set_page_load_timeout(20)
    except Exception:
        pass
    try:
        driver.get(profile_url)
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        return driver.page_source
    except Exception as exc:
        print(f"Bandcamp: selenium quick visit failed {profile_url}: {exc}")
        return ""

def scrape_bandcamp(seed_tags_or_url, pages_per_tag=5, existing_csv="artist_social_links.csv", max_artists=200):
    """
    High-level Bandcamp entry point. Accepts any Bandcamp URL (discover/tag/artist/album/track)
    or the legacy list of tag seeds.
    """
    driver = setup_driver()
    url_input = ""
    seed_tags = []

    if isinstance(seed_tags_or_url, str):
        candidate = seed_tags_or_url.strip()
        if candidate:
            if _bc_is_bandcamp_url(candidate):
                url_input = candidate
            else:
                seed_tags = [candidate]
    elif isinstance(seed_tags_or_url, (list, tuple, set)):
        for entry in seed_tags_or_url:
            if entry is None:
                continue
            value = str(entry).strip()
            if value:
                seed_tags.append(value)
    else:
        seed_tags = list(BANDCAMP_SEED_TAGS)

    if not seed_tags and not url_input:
        seed_tags = list(BANDCAMP_SEED_TAGS)

    try:
        pages_to_scan = int(pages_per_tag)
    except Exception:
        pages_to_scan = BANDCAMP_DISCOVER_PAGES_DEFAULT
    if pages_to_scan <= 0:
        pages_to_scan = BANDCAMP_DISCOVER_PAGES_DEFAULT

    rows_limit = max_artists if max_artists and max_artists > 0 else BANDCAMP_TARGET_ROWS or BANDCAMP_PAGES_PER_TAG
    if BANDCAMP_TARGET_ROWS and BANDCAMP_TARGET_ROWS > 0:
        rows_limit = min(rows_limit, BANDCAMP_TARGET_ROWS)
    rows_limit = max(rows_limit, 1)

    computed_cap = max(max_artists * 5, max_artists + 40)
    if BANDCAMP_MAX_CANDIDATES and BANDCAMP_MAX_CANDIDATES > 0:
        computed_cap = min(computed_cap, BANDCAMP_MAX_CANDIDATES)
    max_candidates = max(computed_cap, rows_limit)
    hit_candidate_cap = False

    bandcamp_rows = []
    enriched_rows = []
    candidate_profiles = []
    seen_profiles = set()
    requested_label = ""
    requested_hint = ""
    contacts_required = BANDCAMP_MIN_CONTACT_REQUIREMENT and not bool(url_input)

    def enqueue_candidate(source_tag, candidate, api_location=""):
        nonlocal hit_candidate_cap
        profile_url = _bandcamp_resolve_artist_profile_url(candidate.get("url"))
        if not profile_url:
            return False
        key = profile_url.rstrip("/").lower()
        if key in seen_profiles or len(candidate_profiles) >= max_candidates:
            if len(candidate_profiles) >= max_candidates:
                hit_candidate_cap = True
            return False
        seen_profiles.add(key)
        candidate_profiles.append({
            "profile_url": profile_url,
            "seed_genre": candidate.get("primary_genre", ""),
            "source_tag": source_tag,
            "api_location": api_location,
        })
        if len(candidate_profiles) >= max_candidates:
            hit_candidate_cap = True
        return True

    def collect_tag_candidates(tag_list, params_map=None):
        params_map = params_map or {}
        if not tag_list:
            return
        for tag in tag_list:
            if not tag:
                continue
            print(f"Bandcamp: scanning tag '{tag}', pages={pages_to_scan}")
            candidates = _bandcamp_collect_tag_pages(
                driver,
                tag,
                pages_to_scan,
                search_query=tag,
                api_params=params_map.get(tag)
            )
            if not candidates:
                continue
            for candidate in candidates:
                if not enqueue_candidate(tag, candidate, candidate.get("location", "")):
                    if hit_candidate_cap:
                        break
            if hit_candidate_cap:
                break

    url_processed = False

    try:
        if url_input:
            kind = _bc_url_kind(url_input)
            if kind == "discover":
                url_processed = True
                print("Bandcamp: collecting tiles directly from discover page")
                discover_tiles = _bandcamp_collect_discover_dom(driver, url_input, pages_per_tag)
                for tile in discover_tiles:
                    enqueue_candidate("discover", tile, tile.get("location", ""))
            elif kind == "tag":
                tag_slug = _bandcamp_extract_tag_from_url(url_input)
                if tag_slug:
                    params_map = {tag_slug: _bc_api_params_from_url(url_input)}
                    collect_tag_candidates([tag_slug], params_map=params_map)
                    url_processed = True
            elif kind in {"artist_profile", "album_or_track"}:
                resolved = _bandcamp_resolve_artist_profile_url(url_input)
                if resolved:
                    print(f"Bandcamp: queuing profile {resolved}")
                    url_processed = enqueue_candidate("direct", {"url": resolved, "primary_genre": ""})
                else:
                    print(f"Bandcamp: unable to resolve artist profile from {url_input}")
            else:
                extracted_tag = _bandcamp_extract_tag_from_url(url_input)
                if extracted_tag:
                    collect_tag_candidates([extracted_tag])
                    url_processed = True
                else:
                    print("Bandcamp: unknown URL shape; falling back to legacy tag flow.")
        if not url_processed:
            collect_tag_candidates(seed_tags or list(BANDCAMP_SEED_TAGS))

        print(f"Bandcamp: total candidate links found {len(candidate_profiles)}")

        aggregated = {}
        http_success = 0
        selenium_used = 0
        kept_after_location = 0
        rejected_location = 0
        sample_kept = ""
        sample_rejected = ""
        stop_processing = False

        def process_artist(candidate, artist_dict):
            nonlocal kept_after_location, sample_kept, sample_rejected, rejected_location, stop_processing
            if not artist_dict:
                return
            artist_dict["source_tag"] = candidate.get("source_tag", "")
            profile_location = artist_dict.get("location", "")
            api_hint = candidate.get("api_location", "")
            location_ok = _bandcamp_location_match_(profile_location, api_hint, requested_label, requested_hint)
            if (not location_ok and BANDCAMP_LOCATION_FALLBACK and requested_label and not profile_location):
                label_norm = _norm_text_(requested_label)
                hint_norm = _norm_text_(api_hint)
                if label_norm and label_norm in hint_norm:
                    location_ok = True
            if not location_ok:
                rejected_location += 1
                if not sample_rejected:
                    sample_rejected = f"{artist_dict.get('artist_name', '') or 'unknown'} ({artist_dict.get('location', '') or 'n/a'})"
                return
            contacts = _bandcamp_collect_contacts(artist_dict)
            if contacts_required and not contacts:
                return
            tile_info = {
                "artist": (candidate.get("tile_artist") or "").strip(),
                "title": (candidate.get("tile_title") or "").strip(),
            }
            key = artist_dict["profile_url"].rstrip("/").lower()
            entry = aggregated.get(key)
            if entry:
                entry["contacts"].update(contacts)
                current = entry["artist"]
                for field in ["artist_name", "location", "latest_release_title", "latest_release_date", "website", "email"]:
                    if not current.get(field) and artist_dict.get(field):
                        current[field] = artist_dict[field]
                entry["artist"] = current
                if tile_info and any(tile_info.values()) and not entry.get("tile"):
                    entry["tile"] = tile_info
            else:
                aggregated[key] = {
                    "artist": artist_dict,
                    "contacts": set(contacts),
                    "tile": tile_info
                }
            kept_after_location += 1
            if not sample_kept:
                sample_kept = f"{artist_dict.get('artist_name', '')} ({artist_dict.get('location', '')})"
            if kept_after_location >= rows_limit:
                stop_processing = True

        http_workers = min(6, len(candidate_profiles)) or 1
        fallback_candidates = []
        with ThreadPoolExecutor(max_workers=http_workers) as executor:
            future_map = {executor.submit(_bandcamp_fetch_profile_html, cand["profile_url"]): cand for cand in candidate_profiles}
            for future in as_completed(future_map):
                if stop_processing:
                    break
                cand = future_map[future]
                html = future.result()
                if html:
                    http_success += 1
                    artist_dict = _bandcamp_parse_html(cand["profile_url"], html, cand.get("seed_genre", ""))
                    process_artist(cand, artist_dict)
                else:
                    fallback_candidates.append(cand)
                if stop_processing:
                    break

        for cand in fallback_candidates:
            if stop_processing:
                break
            html = _bandcamp_quick_visit(driver, cand["profile_url"])
            if not html:
                continue
            selenium_used += 1
            artist_dict = _bandcamp_parse_html(cand["profile_url"], html, cand.get("seed_genre", ""))
            process_artist(cand, artist_dict)
            if stop_processing:
                break

        stop_reason = "done"
        if kept_after_location >= rows_limit:
            stop_reason = "target"
        elif hit_candidate_cap:
            stop_reason = "cap"

        print(f"Bandcamp: candidates={len(candidate_profiles)} http_ok={http_success} selenium_fallback={selenium_used} kept_after_location={kept_after_location} stop_reason={stop_reason}")
        if sample_kept:
            print(f"Bandcamp: kept sample -> {sample_kept}")
        if sample_rejected:
            print(f"Bandcamp: rejected (location) sample -> {sample_rejected}")
        if kept_after_location == 0 and requested_label:
            print("Bandcamp: warning – no profiles matched requested location. Consider widening filters.")

        for entry in aggregated.values():
            artist_dict = entry["artist"]
            contacts = [link for link in entry["contacts"] if _bandcamp_contact_is_valid_url(link)]
            best_contact = _best_contact_(contacts) if contacts else ""
            tile = entry.get("tile") or {}
            tile_artist = (tile.get("artist") or "").strip()
            tile_title = (tile.get("title") or "").strip()
            primary_genre_value = artist_dict.get("primary_genre", "")
            if isinstance(primary_genre_value, str):
                primary_genre_value = primary_genre_value.title()
            release_date_value = artist_dict.get("latest_release_date", "") or ""
            contact_payload = [best_contact] if best_contact else []
            song_title_value = tile_title or artist_dict.get("latest_release_title", "") or ""
            artist_name_value = tile_artist or artist_dict.get("artist_name", "")
            bandcamp_rows.append((
                artist_name_value,
                artist_dict.get("location", ""),
                song_title_value,
                artist_dict.get("sounds_like", ""),
                contact_payload,
                "",
                "",
                "",
                release_date_value,
                primary_genre_value
            ))
            socials = artist_dict.get("socials", {})
            enriched_rows.append({
                "Artist Name": artist_name_value,
                "Profile URL": artist_dict.get("profile_url", ""),
                "Website": artist_dict.get("website", ""),
                "Email": artist_dict.get("email", ""),
                "Instagram": socials.get("instagram", ""),
                "Twitter": socials.get("twitter", ""),
                "Facebook": socials.get("facebook", ""),
                "Linktree": socials.get("linktree", ""),
                "YouTube": socials.get("youtube", ""),
                "Location": artist_dict.get("location", ""),
                "Genres": "; ".join(artist_dict.get("genres", [])),
                "Latest Release": artist_dict.get("latest_release_title", ""),
                "Latest Release Date": artist_dict.get("latest_release_date", ""),
                "Latest Release Precision": artist_dict.get("latest_release_precision", ""),
                "Sounds Like": artist_dict.get("sounds_like", ""),
                "Primary Genre": primary_genre_value,
                "Source Tag": artist_dict.get("source_tag", "")
            })
            if len(bandcamp_rows) >= rows_limit:
                break
    finally:
        driver.quit()
    if bandcamp_rows:
        save_to_csv(bandcamp_rows, existing_csv)
    if enriched_rows:
        _bandcamp_write_enriched_csv(enriched_rows, existing_csv)

def _bandcamp_base_from_page(page_url: str) -> str:
    if not page_url:
        return "https://bandcamp.com"
    if "/tag/" in page_url:
        return page_url.split("/tag/")[0] or "https://bandcamp.com"
    if "/discover" in page_url:
        return page_url.split("/discover")[0] or "https://bandcamp.com"
    return "https://bandcamp.com"


def _bandcamp_candidates_from_html(html: str, page_url: str) -> list:
    candidates = []
    if not html:
        return candidates
    try:
        soup = BeautifulSoup(html, 'html.parser')
        base_url = _bandcamp_base_from_page(page_url)
        candidates = _bandcamp_card_candidates_with_genre(soup, base_url)
        if not candidates:
            for anchor in soup.find_all('a', href=True):
                href = anchor['href']
                absolute = urljoin(page_url, href)
                if not absolute:
                    continue
                parsed = urlparse(absolute)
                host = (parsed.netloc or "").lower()
                path = (parsed.path or "").lower()
                if not host.endswith("bandcamp.com") or host in _BANDCAMP_EXCLUDED_HOSTS:
                    continue
                allowed_path = (
                    path in ("", "/") or
                    path.startswith("/album") or
                    path.startswith("/track") or
                    path.startswith("/music")
                )
                if allowed_path:
                    candidates.append({
                        "url": f"{parsed.scheme or 'https'}://{host}{parsed.path}",
                        "primary_genre": ""
                    })
    except Exception as exc:
        print(f"Bandcamp: failed to parse HTML listing {page_url}: {exc}")
    return candidates

def _bandcamp_collect_city_tag_candidates(driver, city_label: str, pages: int) -> list:
    if not city_label:
        return []
    return _bandcamp_collect_tag_pages(driver, city_label, pages, search_query=city_label)


def _bandcamp_collect_from_tag_page(driver, tag_url) -> list:
    candidates = []
    counts = {}
    try:
        try:
            html_text = driver.page_source or ""
        except Exception:
            html_text = ""
        soup = BeautifulSoup(html_text, 'html.parser')
        base_url = "https://bandcamp.com"
        selector_sets = [
            ("li.searchresult",),
            (".result-items li",),
            ("li.results-grid-item",),
            (".music-grid .item",),
            (".discover-results .item",),
            ("ul.results-grid li",),
        ]
        seen = set()
        for sels in selector_sets:
            total_here = 0
            for sel in sels:
                for card in soup.select(sel):
                    href = None
                    for anchor in card.select("a[href]"):
                        raw = (anchor.get("href") or "").strip()
                        if not raw:
                            continue
                        if raw.startswith("//"):
                            cand = f"https:{raw}"
                        elif raw.startswith("http"):
                            cand = raw
                        elif raw.startswith("/"):
                            cand = urljoin(base_url, raw)
                        elif "bandcamp.com" in raw:
                            cand = f"https://{raw.lstrip('/')}"
                        else:
                            continue
                        lowered = cand.lower()
                        if any(token in lowered for token in ["/album", "/track", ".bandcamp.com"]):
                            href = cand
                            break
                    if not href or href in seen:
                        continue
                    seen.add(href)
                    candidates.append({
                        "url": href,
                        "primary_genre": bandcamp_extract_primary_genre_from_card(card)
                    })
                    total_here += 1
            counts[",".join(s for s in sels)] = total_here
        if not candidates:
            excluded_hosts = {
                "bandcamp.com",
                "store.bandcamp.com",
                "daily.bandcamp.com",
                "blog.bandcamp.com",
                "community.bandcamp.com",
                "supporters.bandcamp.com"
            }
            generic = 0
            for anchor in soup.find_all('a', href=True):
                absolute = urljoin(tag_url, anchor['href'])
                parsed = urlparse(absolute)
                host = (parsed.netloc or "").lower()
                path = (parsed.path or "").lower()
                if not host.endswith("bandcamp.com") or host in excluded_hosts:
                    continue
                if (path in ("", "/") or
                        path.startswith("/album") or
                        path.startswith("/track") or
                        path.startswith("/music")):
                    normalized = f"{parsed.scheme or 'https'}://{host}{parsed.path}"
                    if normalized not in seen:
                        seen.add(normalized)
                        candidates.append({"url": normalized, "primary_genre": ""})
                        generic += 1
            counts["generic_anchor_sweep"] = generic
            if not candidates:
                blob_candidates = _bandcamp_collect_from_tag_blob(html_text)
                if blob_candidates:
                    print(f"Bandcamp: tag blob fallback yielded {len(blob_candidates)} items")
                    candidates.extend(blob_candidates)
        try:
            total = sum(counts.values()) if counts else 0
            print(f"Bandcamp: tag selectors counts → {counts} (total {total})")
        except Exception:
            pass
    except Exception as exc:
        print(f"Bandcamp: failed to collect links from {tag_url}: {exc}")
    return candidates


def _bandcamp_collect_from_search(driver, query, page=1) -> list:
    """
    Fallback: search Bands for the provided query.
    """
    q = quote((query or "").strip())
    url = f"https://bandcamp.com/search?item_type=b&q={q}&page={int(page)}"
    try:
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    except Exception as exc:
        print(f"Bandcamp: error loading {url}: {exc}")
        return []
    soup = BeautifulSoup(driver.page_source or "", "html.parser")
    out = []
    seen = set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://bandcamp.com", href)
        lowered = href.lower()
        if ".bandcamp.com" in lowered and "community.bandcamp.com" not in lowered:
            if href not in seen:
                seen.add(href)
                out.append({"url": href, "primary_genre": ""})
        elif "/album/" in lowered or "/track/" in lowered:
            if href not in seen:
                seen.add(href)
                out.append({"url": href, "primary_genre": ""})
    print(f"Bandcamp: search fallback found {len(out)} candidates on page {page} for '{query}'")
    return out


def _bandcamp_collect_from_tag_blob(html_text: str) -> list:
    candidates = []
    if not html_text:
        return candidates
    soup = BeautifulSoup(html_text, "html.parser")
    blob_attr = None
    blob_holder = soup.select_one("[data-blob]")
    if blob_holder and blob_holder.has_attr("data-blob"):
        blob_attr = blob_holder["data-blob"]
    if not blob_attr:
        match = re.search(r'data-blob="([^"]+)"', html_text)
        if match:
            blob_attr = match.group(1)
    if not blob_attr:
        return candidates
    try:
        blob = json.loads(html.unescape(blob_attr))
    except Exception as exc:
        print(f"Bandcamp: tag blob decode failed: {exc}")
        return candidates
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            url = node.get("item_url") or node.get("tralbum_url") or node.get("url")
            if isinstance(url, str) and url:
                normalized = url.strip()
                if normalized.startswith("//"):
                    normalized = f"https:{normalized}"
                elif normalized.startswith("/"):
                    normalized = f"https://bandcamp.com{normalized}"
                elif not normalized.startswith(("http://", "https://")):
                    normalized = f"https://{normalized.lstrip('/')}"
                lowered = normalized.lower()
                if ("bandcamp.com" in lowered and
                        any(token in lowered for token in ["/album", "/track", ".bandcamp.com"])):
                    if normalized not in seen:
                        seen.add(normalized)
                        candidates.append({
                            "url": normalized,
                            "primary_genre": node.get("genre_text") or node.get("genre") or ""
                        })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blob)
    return candidates




def _bandcamp_collect_tag_via_api(slug: str, page_index: int, base_params: dict | None = None) -> list:
    api_url = "https://bandcamp.com/api/discover/3/get_web"
    session = build_hardened_session()
    params = dict(base_params or {})
    if slug and not params.get("t"):
        params["t"] = slug
    per_page = int(params.get("limit") or 40)
    params["limit"] = per_page
    params["p"] = page_index
    params["offset"] = page_index * per_page
    try:
        resp = session.get(api_url, params=params, timeout=(6, 15), headers=_rand_headers())
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        print(f"Bandcamp: tag API failed (slug={slug}, page={page_index}): {exc}")
        return []
    items = payload.get("items") or []
    candidates = []
    seen = set()
    for item in items:
        hints = item.get("url_hints") or {}
        subdomain = (hints.get("subdomain") or "").strip()
        custom_domain = (hints.get("custom_domain") or "").strip()
        profile_url = ""
        if custom_domain:
            profile_url = custom_domain if custom_domain.startswith(("http://", "https://")) else f"https://{custom_domain.lstrip('/')}"
        elif subdomain:
            profile_url = f"https://{subdomain}.bandcamp.com/"
        if not profile_url:
            continue
        key = profile_url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "url": profile_url,
            "primary_genre": item.get("genre_text") or "",
            "location": item.get("location_text") or "",
            "tile_artist": item.get("secondary_text") or "",
            "tile_title": item.get("primary_text") or "",
        })
    return candidates


def _bandcamp_collect_discover_dom(driver, discover_url: str, max_pages: int = 1) -> list:
    max_pages = max(1, int(max_pages or 1))
    candidates = []
    selectors = [
        "ul.result-items li",
        ".discover-results li",
        "li.results-grid-item",
    ]
    seen = set()
    page_urls = _bandcamp_build_discover_page_urls(discover_url, max_pages)
    for page_index, page_url in enumerate(page_urls, start=1):
        try:
            driver.get(page_url)
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ul.result-items li, li.results-grid-item, .discover-results li"))
            )
        except Exception as exc:
            print(f"Bandcamp: discover DOM load failed for page {page_index}: {exc}")
            continue
        soup = BeautifulSoup(driver.page_source or "", "html.parser")
        for sel in selectors:
            for card in soup.select(sel):
                link = card.find("a", href=True)
                if not link:
                    continue
                href = link.get("href", "").strip()
                if href.startswith("//"):
                    href = f"https:{href}"
                elif href.startswith("/"):
                    href = urljoin("https://bandcamp.com", href)
                elif not href.startswith(("http://", "https://")):
                    href = f"https://bandcamp.com{href if href.startswith('/') else '/' + href}"
                lowered = href.lower()
                if "bandcamp.com" not in lowered:
                    continue
                if "/help/" in lowered or "/daily" in lowered:
                    continue
                profile_url = _bandcamp_resolve_artist_profile_url(href)
                if not profile_url:
                    continue
                key = profile_url.rstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                title_text = card.get_text(" ", strip=True) or ""
                tile_title = ""
                tile_artist = ""
                if " by " in title_text:
                    parts = title_text.split(" by ", 1)
                    tile_title = parts[0].strip()
                    tile_artist = parts[1].strip()
                elif "\n" in title_text:
                    parts = title_text.split("\n", 1)
                    tile_title = parts[0].strip()
                    tile_artist = parts[1].strip()
                else:
                    tile_title = title_text.strip()
                candidates.append({
                    "url": profile_url,
                    "primary_genre": "",
                    "location": "",
                    "tile_artist": tile_artist,
                    "tile_title": tile_title,
                })
    return candidates


def _bandcamp_collect_tag_pages(driver, tag_label: str, pages: int, search_query: str | None = None, api_params: dict | None = None) -> list:
    slug = _bc_normalize_tag_slug(tag_label)
    if not slug:
        return []
    total_candidates = []
    page_count = max(1, pages)
    base_params = dict(api_params or {})
    base_params.pop("p", None)
    base_params.pop("limit", None)
    base_params.pop("offset", None)
    if "t" not in base_params and slug:
        base_params["t"] = slug
    for page_index in range(page_count):
        api_candidates = _bandcamp_collect_tag_via_api(slug, page_index, base_params)
        page_candidates = api_candidates
        if not page_candidates:
            tag_url = _bc_make_tag_url(slug, page_index + 1)
            if not tag_url:
                continue
            try:
                driver.get(tag_url)
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            except Exception as exc:
                print(f"Bandcamp: error loading {tag_url}: {exc}")
                continue
            page_candidates = _bandcamp_collect_from_tag_page(driver, tag_url)
            time.sleep(random.uniform(1.0, 2.0))
        if page_candidates:
            total_candidates.extend(page_candidates)
        if BANDCAMP_MAX_CANDIDATES and len(total_candidates) >= BANDCAMP_MAX_CANDIDATES:
            break
    if not total_candidates:
        query = search_query or tag_label or slug
        for page in range(1, page_count + 1):
            search_candidates = _bandcamp_collect_from_search(driver, query, page=page)
            if search_candidates:
                total_candidates.extend(search_candidates)
                break
    return total_candidates


def _bandcamp_collect_from_discover_html_page(session, base_url: str, page_index: int) -> list:
    page_url = _bandcamp_replace_query_param(base_url, "p", page_index)
    try:
        resp = session.get(page_url, timeout=(6, 15), headers=_rand_headers())
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        print(f"Bandcamp: discover HTML fallback failed for page {page_index}: {exc}")
        return []
    return _bandcamp_candidates_from_html(html, page_url)

def _bandcamp_resolve_artist_profile_url(candidate_url: str) -> str:
    """Normalize candidate links and resolve to canonical artist profile (https://artistname.bandcamp.com/)."""
    if not candidate_url:
        return ""
    url = candidate_url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.startswith("http"):
        url = f"https://{url.lstrip('/')}"
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host.endswith("bandcamp.com"):
        return ""
    excluded_hosts = {
        "bandcamp.com",
        "store.bandcamp.com",
        "daily.bandcamp.com",
        "blog.bandcamp.com",
        "community.bandcamp.com",
        "supporters.bandcamp.com"
    }
    if host in excluded_hosts:
        return ""
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}/"
def _bc_extract_artist_name_from_profile_soup(soup: BeautifulSoup) -> str:
    meta_site = soup.find('meta', attrs={'property': 'og:site_name'})
    if meta_site and meta_site.get('content'):
        name = meta_site['content'].strip()
        if name:
            return name
    meta_title = soup.find('meta', attrs={'property': 'og:title'})
    if meta_title and meta_title.get('content'):
        raw = meta_title['content'].strip()
        left = raw.split('·')[0].strip()
        if left:
            return left
    for sel in ['.band-name', 'h1.band-name', 'h1.title', '#name-section h1', 'header h1', 'h2.band-name']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt:
                return txt
    header_link = soup.select_one('header a[href]')
    if header_link:
        txt = header_link.get_text(" ", strip=True)
        if txt:
            return txt
    return ""

def _bandcamp_parse_html(profile_url: str, html: str, seed_primary_genre: str = "") -> dict:
    artist = {
        "artist_name": "",
        "profile_url": profile_url,
        "location": "",
        "website": "",
        "email": "",
        "socials": {
            "instagram": "",
            "twitter": "",
            "facebook": "",
            "youtube": "",
            "linktree": "",
            "spotify": "",
            "bandsintown": "",
            "songkick": ""
        },
        "genres": [],
        "latest_release_title": "",
        "latest_release_date": "",
        "latest_release_precision": "",
        "sounds_like": "",
        "primary_genre": "",
        "source_tag": ""
    }
    page_source = ""
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    artist["artist_name"] = _bc_extract_artist_name_from_profile_soup(soup)
    artist["genres"] = bandcamp_extract_genres(soup)
    primary_genre = (seed_primary_genre or (artist["genres"][0] if artist["genres"] else "")).strip()
    artist["primary_genre"] = primary_genre
    if not artist["genres"] and primary_genre:
        artist["genres"] = [primary_genre]
    artist["sounds_like"] = bandcamp_extract_sounds_like(soup)
    release_info = bandcamp_extract_release_date(html)
    if release_info.get("date_iso"):
        artist["latest_release_date"] = release_info.get("date_iso", "")
        artist["latest_release_precision"] = release_info.get("precision", "") or ""
    location_el = soup.find(class_=re.compile('location', re.I))
    if location_el:
        artist["location"] = location_el.get_text(" ", strip=True)
    if not artist["location"]:
        bio_el = soup.find('div', class_=re.compile('location', re.I))
        if bio_el:
            artist["location"] = bio_el.get_text(" ", strip=True)
    artist["location"] = _canon_location(artist.get("location", ""))
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        if href.startswith("mailto:"):
            email_value = href.split("mailto:")[-1].split("?")[0]
            if email_value:
                artist["email"] = email_value
            continue
        normalized = href.split('#')[0]
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"
        if normalized.startswith("/"):
            normalized = urljoin(profile_url, normalized)
        parsed = urlparse(normalized)
        if not parsed.scheme.startswith("http"):
            continue
        netloc = parsed.netloc.lower()
        if netloc.endswith("bandcamp.com"):
            continue
        if "instagram.com" in netloc:
            artist["socials"]["instagram"] = normalized
        elif "facebook.com" in netloc or "fb.me" in netloc:
            artist["socials"]["facebook"] = normalized
        elif "twitter.com" in netloc or "x.com" in netloc:
            artist["socials"]["twitter"] = normalized
        elif "youtube.com" in netloc or "youtu.be" in netloc:
            artist["socials"]["youtube"] = normalized
        elif any(domain in netloc for domain in ["linktr.ee", "linktree", "withkoji.com", "beacons.ai"]):
            artist["socials"]["linktree"] = normalized
        elif "spotify.com" in netloc:
            artist["socials"]["spotify"] = normalized
        elif "bandsintown.com" in netloc:
            artist["socials"]["bandsintown"] = normalized
        elif "songkick.com" in netloc:
            artist["socials"]["songkick"] = normalized
        else:
            if not artist["website"]:
                artist["website"] = normalized
    release_container = soup.find('li', class_=re.compile('music-grid-item', re.I))
    if release_container:
        title_el = release_container.find(class_=re.compile('title', re.I))
        if title_el:
            artist["latest_release_title"] = title_el.get_text(strip=True)
        date_el = release_container.find(class_=re.compile('release', re.I))
        if date_el and not artist["latest_release_date"]:
            raw_text = date_el.get_text(strip=True)
            date_iso, prec = _parse_any_date_to_iso(raw_text)
            if date_iso:
                artist["latest_release_date"] = date_iso
                artist["latest_release_precision"] = prec or artist["latest_release_precision"]
            else:
                artist["latest_release_date"] = raw_text
    if not artist["latest_release_title"]:
        track_title = soup.find(class_=re.compile('trackTitle', re.I))
        if track_title:
            artist["latest_release_title"] = track_title.get_text(strip=True)
    if not artist["latest_release_date"]:
        release_text = soup.find(class_=re.compile('release-date', re.I))
        if release_text:
            raw_text = release_text.get_text(strip=True)
            date_iso, prec = _parse_any_date_to_iso(raw_text)
            if date_iso:
                artist["latest_release_date"] = date_iso
                artist["latest_release_precision"] = prec or artist["latest_release_precision"]
            else:
                artist["latest_release_date"] = raw_text
    if not artist["latest_release_date"]:
        credits = soup.find('div', class_=re.compile(r'tralbum-credits', re.I))
        if credits:
            credits_text = credits.get_text(" ", strip=True)
            match = re.search(r"released\s+(.+)", credits_text, re.I)
            if match:
                raw_text = match.group(1).strip()
                date_iso, prec = _parse_any_date_to_iso(raw_text)
                if date_iso:
                    artist["latest_release_date"] = date_iso
                    artist["latest_release_precision"] = prec or artist["latest_release_precision"]
                else:
                    artist["latest_release_date"] = raw_text
            else:
                artist["latest_release_date"] = credits_text.strip()
    if not artist["latest_release_date"]:
        artist["latest_release_date"] = "not present"
    if not artist["latest_release_title"]:
        artist["latest_release_title"] = ""
    return artist

def _bandcamp_parse_artist_profile(driver, profile_url, seed_primary_genre="") -> dict:
    """Visit artist profile via Selenium fallback."""
    page_source = _bandcamp_quick_visit(driver, profile_url)
    if not page_source:
        page_source = _bandcamp_fetch_profile_html(profile_url)
    return _bandcamp_parse_html(profile_url, page_source, seed_primary_genre)

def _bandcamp_is_actionable(artist_dict: dict) -> bool:
    """Return True if website or email or at least one social exists."""
    if not artist_dict:
        return False
    socials = artist_dict.get("socials", {})
    has_social = any(value for value in socials.values())
    return bool(artist_dict.get("website") or artist_dict.get("email") or has_social)

def _bandcamp_write_enriched_csv(rows, existing_csv):
    columns = [
        "Artist Name",
        "Profile URL",
        "Website",
        "Email",
        "Instagram",
        "Twitter",
        "Facebook",
        "Linktree",
        "YouTube",
        "Location",
        "Genres",
        "Latest Release",
        "Latest Release Date",
        "Latest Release Precision",
        "Sounds Like",
        "Source Tag"
    ]
    base_dir = os.path.dirname(os.path.abspath(existing_csv))
    enriched_path = os.path.join(base_dir, "bandcamp_enriched.csv")
    existing_df = pd.DataFrame(columns=columns)
    if os.path.exists(enriched_path):
        try:
            existing_df = pd.read_csv(enriched_path)
        except Exception:
            existing_df = pd.DataFrame(columns=columns)
    for col in columns:
        if col not in existing_df.columns:
            existing_df[col] = ""
    if not existing_df.empty:
        existing_df = existing_df[columns]
    new_df = pd.DataFrame(rows)
    for col in columns:
        if col not in new_df.columns:
            new_df[col] = ""
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined["__dedupe_key"] = (
        combined["Artist Name"].fillna("").str.strip().str.lower()
        + "||" +
        combined["Profile URL"].fillna("").str.rstrip("/").str.lower()
    )
    combined = combined.drop_duplicates(subset="__dedupe_key")
    combined = combined.drop(columns="__dedupe_key")
    combined = combined[columns]
    combined.to_csv(enriched_path, index=False, encoding="utf-8-sig")

# ---------------------------
# SoundCloud Helpers
# ---------------------------
def clean_display_name(value: str) -> str:
    text = value or ""
    out = []
    for ch in text:
        if unicodedata.category(ch) in SYMBOL_CAT:
            continue
        out.append(ch)
    cleaned = "".join(out)
    cleaned = re.sub(r"[^\w\s\.\&\-\’\'/|]", "", cleaned, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def export_soundcloud_row(data: dict) -> dict:
    fields = [
        "Artist Name", "Location", "Song Title", "Sounds Like", "Social Link",
        "SoundCloud Link", "Played on triple J", "Played on Unearthed",
        "Release Date", "Primary Genre", "Date Added", "Email"
    ]
    row = {field: "" for field in fields}
    handle = data.get("handle", "")
    display = data.get("display_name") or handle
    row["Artist Name"] = clean_display_name(display) if display else ""
    row["SoundCloud Link"] = data.get("soundcloud_link") or (f"https://soundcloud.com/{handle}" if handle else "")
    exts = data.get("external_urls") or []
    emails = data.get("emails") or []
    row["Social Link"] = "; ".join(exts[:5])
    row["Email"] = emails[0] if emails else ""
    row["Song Title"] = ""
    row["Release Date"] = ""
    row["Played on triple J"] = ""
    row["Played on Unearthed"] = ""
    if row["Social Link"] == "http://firefox.com":
        row["Social Link"] = ""
    return row


def _resolve_country_name(value: str) -> str:
    if not value:
        return ""
    raw = value.strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower in COUNTRY_CODE_OVERRIDES:
        return COUNTRY_CODE_OVERRIDES[lower]
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return raw


def normalize_location(city: str, country: str) -> str:
    city_clean = (city or "").strip()
    country_clean = _resolve_country_name(country)
    if "naarm" in city_clean.lower():
        city_clean = "Melbourne"
        if not country_clean:
            country_clean = "Australia"
    if city_clean and country_clean:
        return f"{city_clean}, {country_clean}"
    return city_clean or country_clean or ""


def choose_primary_genre(user_genre: str, fallback_tags=None) -> str:
    fallback_tags = fallback_tags or []
    primary = (user_genre or "").strip()
    if primary and primary.lower() not in _SC_GENRE_DENY:
        return primary
    for tag in fallback_tags:
        candidate = (tag or "").strip()
        if not candidate:
            continue
        if candidate.lower() in _SC_GENRE_DENY:
            continue
        return candidate
    return ""


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

_SC_THREAD_LOCAL = threading.local()

_BANDCAMP_THREAD_LOCAL = threading.local()

def _bandcamp_thread_session():
    session = getattr(_BANDCAMP_THREAD_LOCAL, "session", None)
    if session is None:
        session = build_hardened_session()
        _BANDCAMP_THREAD_LOCAL.session = session
    return session


def _build_sc_session() -> requests.Session:
    return build_hardened_session()


def is_valid_sc_url(url: str):
    if not url:
        return False, None
    match = SC_HANDLE_RE.match(url.strip())
    if not match:
        return False, None
    slug = match.group(1).lower()
    if slug in SC_HANDLE_BAN:
        return False, slug
    return True, slug


AGG_PREF = SC_AGGREGATOR_PREFERENCE

def _sc_is_people_search_url(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    return parsed.netloc.lower().endswith("soundcloud.com") and parsed.path.strip("/").lower() == "search/people"


def _sc_parse_people_search(value: str) -> dict:
    result = {"q": "", "place": ""}
    if not value:
        return result
    try:
        parsed = urlparse(value.strip())
        query = parse_qs(parsed.query or "")
    except Exception:
        return result
    result["q"] = (query.get("q", [""])[0] or "").strip()
    result["place"] = (query.get("filter.place", [""])[0] or "").strip()
    return result


def expand_for_email(session, url):
    mails = set()
    if not url:
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


def _sc_track_release_iso(track: dict) -> tuple:
    """
    Pick the best available date field from a track payload and convert
    it to (iso_date, precision). Falls back to created_at if needed.
    """
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


def _sc_fetch_latest_track_metadata(session, client_id: str, user_id) -> dict:
    """
    Fetch the user's most recent track via the public API so we can
    capture title, release date, and tags without relying on Selenium.
    """
    if not client_id or not user_id:
        return {}
    api_url = f"https://api-v2.soundcloud.com/users/{user_id}/tracks"
    params = {
        "client_id": client_id,
        "limit": 1,
        "linked_partitioning": 1,
        "order": "published_at",
    }
    try:
        resp = session.get(api_url, params=params, timeout=SC_REQUEST_TIMEOUT, headers=_rand_headers())
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        print(f"[warn] SoundCloud latest-track API failed for user_id={user_id}: {exc}")
        return {}
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
        }
    return {}


def _sc_fetch_api_profile(session, handle: str) -> dict:
    client_id = _sc_get_client_id(session)
    if not client_id:
        return {}
    try:
        resp = session.get(
            "https://api-v2.soundcloud.com/resolve",
            params={
                "url": f"https://soundcloud.com/{handle}",
                "client_id": client_id,
            },
            timeout=SC_REQUEST_TIMEOUT,
            headers=_rand_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[warn] SoundCloud resolve API failed for {handle}: {exc}")
        return {}
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
    }
    user_urn = data.get("urn") or ""
    user_id = data.get("id") or ""
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
                    url = item.get("url")
                    if url:
                        profile["external_urls"].append(url)
        except Exception as exc:
            print(f"[warn] SoundCloud web profiles failed for {handle}: {exc}")
    latest_track = _sc_fetch_latest_track_metadata(session, client_id, user_id or user_urn)
    if latest_track:
        profile["latest_track_title"] = latest_track.get("title") or ""
        profile["latest_track_release_date"] = latest_track.get("release_date") or ""
        profile["latest_track_precision"] = latest_track.get("precision") or ""
        profile["latest_track_genre"] = latest_track.get("genre") or ""
        profile["latest_track_tags"] = latest_track.get("tags") or []
    return profile


def extract_sc_links(session: requests.Session, handle: str) -> dict:
    cached = SC_ABOUT_CACHE.get(handle)
    if cached:
        cached_data = cached.get("data", {}) or {}
        exts = [u for u in (cached_data.get("external_urls") or []) if u and u.lower() != "http://firefox.com"]
        emails = cached_data.get("emails") or []
        has_required = all(key in cached_data for key in SC_ABOUT_CACHE_REQUIRED_KEYS)
        if cached_data and (exts or emails) and has_required:
            cached_data["external_urls"] = exts
            return cached_data

    external_urls, emails = set(), set()
    display_name = handle
    user_city = ""
    user_country = ""
    user_genre = ""
    t0 = time.perf_counter()
    html = ""
    bio_text = ""
    latest_title = ""
    latest_release = ""
    latest_precision = ""
    latest_genre = ""
    latest_tags = []
    about_url = f"https://soundcloud.com/{handle}/about"
    print(f"[dbg] fetching {about_url}")
    try:
        resp = session.get(about_url, timeout=(6, 12), headers=_rand_headers())
        print(f"[dbg] fetched {handle} status={resp.status_code} len={len(resp.text)}")
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        print(f"[warn] {handle} about fetch failed: {exc}")
    finally:
        polite_sleep()

    if html:
        doc = get_soup(html)
        name_el = doc.select_one("h1, .profileHeaderInfo__userName, .profileHeaderInfo__content")
        if name_el:
            text = name_el.get_text(strip=True)
            if text:
                display_name = text

        for a in doc.select(
            'a[href^="mailto:"], '
            'a[href*="instagram.com"], a[href*="facebook.com"], '
            'a[href*="linktr.ee"], a[href*="bandcamp.com"], '
            'a[href*="youtube.com"], a[href*="tiktok.com"], '
            'a[href*="twitter.com"], a[href*="x.com"], '
            'a[href*="beacons.ai"], a[href*="carrd.co"], '
            'a[href*="flow.page"]'
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
                        if isinstance(value, (list, tuple)) and key and key.lower() in ("sameas", "externalurls", "externallinks", "external_url", "socials"):
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
        # plain-text email fallback (bios often list contact without mailto)
        try:
            text_blob = doc.get_text(" ", strip=True)
            for address in extract_emails(text_blob):
                if address and not address.lower().endswith("@soundcloud.com"):
                    emails.add(address)
        except Exception:
            pass

    did_expand = False
    for candidate in list(external_urls):
        host = (urlparse(candidate).hostname or "").lower()
        if any(host.endswith(pref) for pref in AGG_PREF):
            for mail in expand_for_email(session, candidate):
                emails.add(mail)
            did_expand = True
            break

    api_profile = _sc_fetch_api_profile(session, handle)
    if api_profile:
        if api_profile.get("display_name"):
            display_name = api_profile["display_name"]
        user_city = api_profile.get("city") or user_city
        user_country = api_profile.get("country") or user_country
        user_genre = api_profile.get("genre") or user_genre
        external_urls.update(api_profile.get("external_urls") or [])
        if not bio_text and api_profile.get("description"):
            bio_text = api_profile["description"]
        latest_title = api_profile.get("latest_track_title") or latest_title
        latest_release = api_profile.get("latest_track_release_date") or latest_release
        latest_precision = api_profile.get("latest_track_precision") or latest_precision
        latest_genre = api_profile.get("latest_track_genre") or latest_genre
        latest_tags = api_profile.get("latest_track_tags") or latest_tags

    norm_exts = []
    seen_norm = set()
    for url in external_urls:
        normalized = normalize_external_url(url)
        if not normalized or normalized in seen_norm:
            continue
        seen_norm.add(normalized)
        norm_exts.append(normalized)

    elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
    payload = {
        "handle": handle,
        "display_name": display_name,
        "external_urls": norm_exts,
        "emails": sorted(emails),
        "city": user_city,
        "country": user_country,
        "genre": user_genre,
        "elapsed_ms": elapsed_ms,
        "aggregator_expanded": int(did_expand),
        "bio_text": bio_text,
        "sounds_like": _sc_sounds_like_from_bio(bio_text),
        "latest_track_title": latest_title,
        "latest_track_release_date": latest_release,
        "latest_track_precision": latest_precision,
        "latest_track_genre": latest_genre,
        "latest_track_tags": latest_tags,
    }
    if payload["external_urls"] or payload["emails"]:
        SC_ABOUT_CACHE.set(handle, payload)
    return payload


def _sc_thread_session() -> requests.Session:
    session = getattr(_SC_THREAD_LOCAL, "session", None)
    if session is None:
        session = _build_sc_session()
        _SC_THREAD_LOCAL.session = session
    return session


def _sc_fetch_contact_payload(handle: str) -> dict:
    session = _sc_thread_session()
    started = time.perf_counter()
    error = ""
    try:
        data = extract_sc_links(session, handle)
    except Exception as exc:
        error = str(exc)
        data = {"emails": [], "external_urls": [], "aggregator_expanded": 0}
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    emails_len = len(data.get("emails", []) or [])
    links_len = len(data.get("external_urls", []) or [])
    elapsed = data.get("elapsed_ms", elapsed_ms)
    site_flag = int(data.get("aggregator_expanded", data.get("_aggregator_tried", 0)))
    print(f"[sc] handle={handle} links={links_len} email={emails_len} site={site_flag} ms={elapsed}")
    return {
        "data": data,
        "elapsed_ms": elapsed,
        "links": links_len,
        "emails": emails_len,
        "site": site_flag,
        "error": error,
    }


def _sc_fetch_contacts_concurrently(handles: list) -> dict:
    results = {}
    if not handles:
        return results
    max_workers = min(SC_MAX_WORKERS, max(1, len(handles)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_sc_fetch_contact_payload, handle): handle for handle in handles}
        for future in as_completed(future_map):
            handle = future_map[future]
            try:
                results[handle] = future.result()
            except Exception as exc:
                print(f"[sc] handle={handle} error={exc}")
                results[handle] = {
                    "data": {"emails": [], "external_urls": [], "_aggregator_tried": 0},
                    "elapsed_ms": 0,
                    "links": 0,
                    "emails": 0,
                    "site": 0,
                    "error": str(exc),
                }
    return results


def _sc_has_contact_links(entry) -> bool:
    if not entry:
        return False
    payload = entry.get("data") if isinstance(entry, dict) else entry
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("emails") or payload.get("external_urls"))


def _sc_collect_contact_links(handle_jobs: list, min_yield: int) -> tuple:
    contact_map = {}
    processed_jobs = []
    hits = 0
    if not handle_jobs:
        return contact_map, processed_jobs, hits
    for start in range(0, len(handle_jobs), SC_LINK_BATCH_SIZE):
        chunk = handle_jobs[start:start + SC_LINK_BATCH_SIZE]
        handles = [job["handle"] for job in chunk]
        batch_results = _sc_fetch_contacts_concurrently(handles)
        for job in chunk:
            handle = job["handle"]
            contact_map[handle] = batch_results.get(handle, {
                "data": {"emails": [], "external_urls": [], "aggregator_expanded": 0},
                "elapsed_ms": 0,
                "links": 0,
                "emails": 0,
                "site": 0,
                "error": "not-fetched",
            })
        processed_jobs.extend(chunk)
    hits = sum(1 for handle in (job["handle"] for job in processed_jobs) if _sc_has_contact_links(contact_map.get(handle)))
    return contact_map, processed_jobs, hits


def _sc_apply_row_guards(row: dict):
    socials_raw = row.get("Social Link") or ""
    if socials_raw:
        cleaned_links = []
        for token in [part.strip() for part in socials_raw.split(";")]:
            if not token or token.lower() == "http://firefox.com":
                continue
            cleaned_links.append(token)
        row["Social Link"] = "; ".join(cleaned_links)
    artist = (row.get("Artist Name") or "").strip()
    location = (row.get("Location") or "").strip()
    if artist and location and artist.lower() == location.lower():
        row["Location"] = ""
    genre = (row.get("Primary Genre") or "").strip()
    if genre.lower() in _SC_GENRE_DENY:
        row["Primary Genre"] = ""
    assert row.get("Social Link", "") != "http://firefox.com"


def _sc_build_row(handle: str, payload: dict, soundcloud_link: str, fallback_name: str = "",
                  fallback_location: str = "", song_title: str = "", release_date: str = "",
                  sounds_like: str = "", fallback_tags=None, fallback_external=None, fallback_emails=None):
    payload = payload or {}
    fallback_tags = list(fallback_tags or [])
    fallback_external = fallback_external or []
    fallback_emails = fallback_emails or []
    default_name = handle.replace("-", " ").replace("_", " ").title()
    display_name = payload.get("display_name") or fallback_name or default_name
    location_value = normalize_location(
        payload.get("city"),
        payload.get("country") or payload.get("country_name")
    )
    if not location_value:
        location_value = (fallback_location or "").strip()
    latest_tags = payload.get("latest_track_tags") or []
    if latest_tags:
        fallback_tags.extend(latest_tags)
    genre_source = payload.get("genre") or payload.get("latest_track_genre")
    primary_genre_value = choose_primary_genre(genre_source, fallback_tags)
    external_sources = list(payload.get("external_urls") or []) + list(fallback_external or [])
    emails_source = list(payload.get("emails") or []) + list(fallback_emails or [])
    if not song_title:
        song_title = payload.get("latest_track_title") or ""
    release_candidate = payload.get("latest_track_release_date") or ""
    if (not release_date) or (release_date and release_date.strip().lower() == "not present"):
        release_date = release_candidate or release_date
    if not sounds_like:
        sounds_like = payload.get("sounds_like") or ""

    def _dedupe_external(items):
        seen = set()
        cleaned = []
        for item in items:
            if not item or not isinstance(item, str):
                continue
            val = item.strip()
            if not val.startswith(("http://", "https://")):
                continue
            if val.lower() == "http://firefox.com":
                continue
            if val in seen:
                continue
            seen.add(val)
            cleaned.append(val)
        return cleaned

    def _dedupe_emails(items):
        seen = set()
        cleaned = []
        for item in items:
            if not item or not isinstance(item, str):
                continue
            val = item.strip()
            if not val or val.lower().endswith("@soundcloud.com"):
                continue
            if val in seen:
                continue
            seen.add(val)
            cleaned.append(val)
        return cleaned

    external_urls = _dedupe_external(external_sources)
    emails = _dedupe_emails(emails_source)
    row_data = {
        "handle": handle,
        "display_name": display_name,
        "external_urls": external_urls,
        "emails": emails,
        "soundcloud_link": soundcloud_link,
    }
    row = export_soundcloud_row(row_data)
    row["Location"] = location_value
    row["Primary Genre"] = primary_genre_value
    row["Song Title"] = (song_title or "").strip()
    row["Release Date"] = (release_date or "").strip()
    row["Sounds Like"] = (sounds_like or "").strip()
    _sc_apply_row_guards(row)
    return row, external_urls, emails


def _sc_log_csv_row(handle: str, row: dict, external_urls=None, emails=None):
    external_urls = external_urls or []
    emails = emails or []
    social_ok = bool(row.get("Social Link"))
    print(
        f'[csv] {handle} name="{row.get("Artist Name","")}" '
        f'loc="{row.get("Location","")}" genre="{row.get("Primary Genre","")}"\n'
        f'      social={social_ok} email={bool(row.get("Email"))} links_ct={len(external_urls)}'
    )
    if external_urls and not social_ok:
        print(f"[alert] mapping mismatch: links found but Social Link empty; exts[:3]={external_urls[:3]}")


def _sc_print_dry_run_row(handle: str, row: dict, external_urls: list, emails: list):
    print(f'[dry-run] {handle} name="{row.get("Artist Name","")}" '
          f'social="{row.get("Social Link","")}" email="{row.get("Email","")}" '
          f'external_count={len(external_urls)}')


def _sc_normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/"):
        return "https://soundcloud.com" + u
    return u

def _sc_accept_consent_if_present(driver):
    """Dismiss OneTrust or generic consent banners so content loads."""
    try:
        WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        for xp in [
            "//*[@id='onetrust-accept-btn-handler']",
            "//button[contains(@class,'onetrust-accept-btn-handler')]",
            "//button[contains(., 'Accept All')]",
            "//button[contains(., 'Accept all')]",
            "//button[contains(., 'I agree')]",
            "//button[contains(., 'Accept & Continue')]",
        ]:
            try:
                btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.5)
                break
            except Exception:
                continue
    except Exception:
        pass

def _sc_soft_scroll(driver):
    try:
        for y in (300, 900, 1600):
            driver.execute_script(f"window.scrollTo(0,{y});")
            time.sleep(0.3)
    except Exception:
        pass

def _sc_unwrap_gate(href: str) -> str:
    """
    Unwrap gate.sc redirect URLs that embed the real target in ?url=.
    """
    if not href:
        return href
    try:
        parsed = urlparse(href)
        if parsed.netloc and "gate.sc" in parsed.netloc.lower():
            inner = parse_qs(parsed.query or "").get("url", [None])[0]
            if inner:
                return unquote(inner)
    except Exception:
        pass
    return href

def _sc_handle_from_profile(profile_url: str) -> str:
    try:
        parsed = urlparse(profile_url)
        return (parsed.path or "/").strip("/").split("/")[0]
    except Exception:
        return ""

def _norm_url_general(href: str) -> str:
    if not href:
        return ""
    href = _sc_unwrap_gate(href.strip())
    if href.startswith(("facebook.com/", "m.facebook.com/", "fb.me/")):
        href = "https://" + href
    try:
        parsed = urlparse(href)
        host = (parsed.netloc or "").lower()
        if host in _FB_REDIRECT_HOSTS:
            inner = parse_qs(parsed.query or "").get("u", [None])[0]
            if inner:
                return unquote(inner)
    except Exception:
        pass
    return href

def _sc_try_dismiss_consent(driver):
    selectors = [
        "#onetrust-accept-btn-handler",
        ".onetrust-close-btn-handler",
        "button[aria-label='Accept All']",
        "button[aria-label='Accept all']",
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            el.click()
            time.sleep(0.2)
            return
        except Exception:
            continue

def _sc_try_show_more(driver):
    xpaths = [
        "//button[.//*[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]]",
        "//a[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show more')]",
        "//button[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'see more')]",
    ]
    for xp in xpaths:
        try:
            btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
            btn.click()
            time.sleep(0.25)
            return
        except Exception:
            continue

def _sc_scroll_sidebar(driver):
    try:
        driver.execute_script("window.scrollTo(0,0);")
        driver.execute_script(
            "const el = document.querySelector('.profileSidebar') || document.querySelector('.profileHeaderInfo') || document.querySelector('ul.profileLinks__linkList');"
            "if (el) el.scrollIntoView({behavior:'instant', block:'center'});"
        )
        time.sleep(0.25)
    except Exception:
        pass

def _norm_fb(url: str) -> str:
    return _norm_url_general(url)

def _is_fb(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(h in host for h in _FB_HOSTS)
    except Exception:
        return False

def _is_company_fb(href: str) -> bool:
    try:
        path = (urlparse(href).path or "").lower()
        return any(path.startswith(deny) for deny in _FB_PATH_DENY)
    except Exception:
        return False

def _sc_extract_objects_from_hydration(html: str) -> list:
    try:
        match = re.search(r"__sc_hydration\s*=\s*(\[[\s\S]*?\])\s*;?", html, flags=re.M)
        if not match:
            return []
        data = json.loads(match.group(1))
        return data if isinstance(data, list) else [data]
    except Exception:
        return []

def _sc_collect_urls_from_obj(obj, bucket: set):
    if isinstance(obj, str):
        if obj.startswith("http") or obj.startswith("mailto:"):
            bucket.add(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            _sc_collect_urls_from_obj(value, bucket)
    elif isinstance(obj, list):
        for value in obj:
            _sc_collect_urls_from_obj(value, bucket)

def _sc_extract_urls_from_hydration(html: str) -> set:
    urls = set()
    for obj in _sc_extract_objects_from_hydration(html):
        _sc_collect_urls_from_obj(obj, urls)
    return urls


def _extract_from_json_or_regex(html: str) -> set:
    urls = set()
    soup = _safe_bs(html)
    for script in soup.find_all("script"):
        txt = script.string or ""
        if not txt or ("http" not in txt and "sameAs" not in txt):
            continue
        urls.update(URL_RE.findall(txt))
        try:
            data = json.loads(txt)
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for key, value in cur.items():
                    if isinstance(value, (list, tuple)) and key and key.lower() in ("sameas", "externallinks", "external_url", "externalurls", "socials"):
                        for item in value:
                            if isinstance(item, str) and item.startswith("http"):
                                urls.add(item)
                    stack.append(value)
            elif isinstance(cur, list):
                stack.extend(cur)
    if not urls:
        urls.update(URL_RE.findall(html or ""))
    return urls

def _sc_extract_urls_from_ldjson(soup) -> set:
    urls = set()
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        _sc_collect_urls_from_obj(data, urls)
    return urls


def _sc_scan_for_user_blob(root):
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            has_location = any(key in cur for key in ("city", "city_name", "country", "country_name", "country_code"))
            has_identity = any(key in cur for key in ("display_name", "full_name", "username", "permalink"))
            if has_location and has_identity:
                return cur
            for value in cur.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            for value in cur:
                if isinstance(value, (dict, list)):
                    stack.append(value)
    return {}


def _sc_extract_user_profile_from_html(html: str) -> dict:
    for obj in _sc_extract_objects_from_hydration(html):
        candidate = _sc_scan_for_user_blob(obj)
        if candidate:
            display_name = (
                candidate.get("display_name")
                or candidate.get("full_name")
                or candidate.get("username")
                or candidate.get("permalink")
                or ""
            )
            city = candidate.get("city") or candidate.get("city_name") or ""
            country_raw = (
                candidate.get("country_name")
                or candidate.get("country")
                or candidate.get("country_code")
                or ""
            )
            country_name = _resolve_country_name(country_raw)
            genre = candidate.get("genre") or candidate.get("music_style") or ""
            return {
                "display_name": display_name,
                "city": city,
                "country": country_name,
                "genre": genre,
            }
    return {}


def _sc_try_linktree_for_contacts(driver, urls: set, timeout=6) -> tuple:
    linktree_url = None
    for candidate in urls or set():
        try:
            host = (urlparse(candidate).netloc or "").lower()
            if any(h in host for h in {"linktr.ee", "linktree.com", "www.linktr.ee", "www.linktree.com"}):
                linktree_url = candidate
                break
        except Exception:
            continue
    if not linktree_url:
        return "", ""
    try:
        driver.get(linktree_url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        soup = BeautifulSoup(driver.page_source, "html.parser")
        fb = ""
        em = ""
        for anchor in soup.find_all("a", href=True):
            href = _norm_url_general(anchor["href"])
            if _is_fb(href) and not _is_company_fb(href):
                fb = href
                break
            if href.startswith("mailto:"):
                candidate_email = href.split("mailto:")[-1].split("?")[0]
                if candidate_email and not candidate_email.lower().endswith("@soundcloud.com"):
                    em = candidate_email
        return fb, em
    except Exception:
        return "", ""


def _sc_collect_from_people_search(driver, search_url, max_handles=200) -> list:
    """
    Given a SoundCloud people search URL, return handles by scrolling.
    """
    handles, seen = [], set()
    try:
        driver.get(search_url)
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _sc_try_dismiss_consent(driver)
    except Exception:
        pass

    for _ in range(10):
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if not href or href in ("/", "#"):
                continue
            absolute = _sc_normalize_url(href)
            ok, slug = is_valid_sc_url(absolute)
            if not ok or not slug:
                continue
            if slug in seen:
                continue
            seen.add(slug)
            handles.append(slug)
            if len(handles) >= max_handles:
                return handles
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            break
        time.sleep(0.8)
    return handles


def _sc_rel_to_iso(text: str) -> str:
    """
    Convert 'x day(s)/week(s)/month(s)/year(s) ago' to an approximate ISO date (YYYY-MM-DD).
    If not parseable, return "not present".
    """
    if not text:
        return "not present"
    s = text.strip().lower()
    m = re.search(r'(\d+)\s*(day|week|month|year)s?\s+ago', s)
    if not m:
        return "not present"
    n = int(m.group(1))
    unit = m.group(2)
    now = datetime.datetime.now()
    try:
        if unit == "day":
            dt = now - relativedelta(days=n)
        elif unit == "week":
            dt = now - relativedelta(weeks=n)
        elif unit == "month":
            dt = now - relativedelta(months=n)
        else:
            dt = now - relativedelta(years=n)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "not present"


def _sc_first_text(soup, selectors):
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return ""


def _sc_extract_profile_meta(driver) -> dict:
    """
    Best-effort metadata from a SoundCloud profile root page:
      - artist_name (if present)
      - location (right column: 'Based in ...')
      - song_title (first visible track or spotlight)
      - primary_genre (first '#Tag' chip near the first track)
      - release_date (approx from 'x months ago', else 'not present')
    """
    meta = {
        "artist_name": "",
        "location": "",
        "song_title": "",
        "primary_genre": "",
        "release_date": "not present",
        "sounds_like": ""
    }
    try:
        _sc_try_dismiss_consent(driver)
    except Exception:
        pass

    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")

    meta["artist_name"] = _sc_first_text(soup, [
        "h1.soundTitle__username",
        "div.profileHeaderInfo h1",
        "meta[property='og:title']"
    ])
    if not meta["artist_name"]:
        og = soup.select_one("meta[property='og:title']")
        if og and og.get("content"):
            meta["artist_name"] = og["content"].strip()

    loc = _sc_first_text(soup, [
        "div.profileHeaderInfo",
        ".profileSidebar"
    ])
    m = re.search(r"based in\s+(.+?)(?:\s{2,}|$)", loc, flags=re.I)
    if m:
        meta["location"] = m.group(1).strip()

    track_container = None
    for sel in [
        ".spotlight .soundList__item",
        ".profileStream__list .soundList__item",
        ".soundList__item"
    ]:
        track_container = soup.select_one(sel)
        if track_container:
            break

    if track_container:
        t = _sc_first_text(track_container, [
            "a.soundTitle__title",
            "a.trackItem__trackTitle",
            ".soundTitle__title",
            "[data-e2e='track-title']",
        ])
        if t:
            meta["song_title"] = t

        tag = _sc_first_text(track_container, [
            "a[href*='/tags/']",
            ".sc-tag",
            "a[aria-label^='#']"
        ])
        if tag:
            meta["primary_genre"] = tag.lstrip("#").strip().title()

        rel = _sc_first_text(track_container, [
            ".relativeTime",
            "time[datetime]",
            "time",
            "span[aria-label*='ago']"
        ])
        if rel:
            if "datetime" in rel.lower():
                tm = track_container.select_one("time[datetime]")
                if tm and tm.get("datetime"):
                    meta["release_date"] = tm["datetime"][:10]
            if meta["release_date"] == "not present":
                meta["release_date"] = _sc_rel_to_iso(rel)

    return meta

def _sc_collect_profile_links(driver, timeout=6, **_ignored) -> set:
    """
    Return a set of outbound link hrefs visible on a SoundCloud profile page.
    Accepts a timeout kwarg for compatibility. Safe if called with extra kwargs.
    """
    found = set()
    try:
        _sc_try_dismiss_consent(driver)
        _sc_try_show_more(driver)
        _sc_scroll_sidebar(driver)

        # Give the sidebar a brief chance to render
        try:
            WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ul.profileLinks__linkList a[href]"))
            )
        except Exception:
            pass

        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        # 1) Preferred: explicit sidebar list
        for a in soup.select("ul.profileLinks__linkList a[href]"):
            found.add(a.get("href", "").strip())

        # 2) Broad fallback: any obvious social/email anchors
        for a in soup.find_all("a", href=True):
            href = (a["href"] or "").strip()
            low = href.lower()
            if any(k in low for k in [
                "facebook.com", "fb.me/", "mailto:", "linktr.ee", "linktree",
                "gate.sc?url=http", "l.facebook.com/l.php?u="
            ]):
                found.add(href)

        # 3) Hydration fallback (external_links / externalLinks)
        m = re.search(r"__sc_hydration\s*=\s*(\[[\s\S]*?\])\s*;?", html, flags=re.M)
        if m:
            try:
                data = json.loads(m.group(1))
                stack = [data]
                while stack:
                    v = stack.pop()
                    if isinstance(v, dict):
                        ext = v.get("external_links") or v.get("externalLinks")
                        if isinstance(ext, list):
                            for it in ext:
                                if isinstance(it, dict) and isinstance(it.get("url"), str):
                                    found.add(it["url"])
                        for vv in v.values():
                            if isinstance(vv, (dict, list)):
                                stack.append(vv)
                            elif isinstance(vv, str):
                                if any(s in vv for s in [
                                    "facebook.com", "mailto:", "linktr.ee", "linktree",
                                    "gate.sc?url=http", "l.facebook.com/l.php?u="
                                ]):
                                    found.add(vv)
                    elif isinstance(v, list):
                        stack.extend(v)
            except Exception:
                pass
    except Exception:
        pass
    return found

def _sc_collect_from_tag_page(driver, tag_url: str) -> list:
    """
    Extract artist profile URLs from /tags/{tag}?page=N by inspecting track links.
    Only accepts handles that pass _sc_is_valid_handle.
    """
    results = []
    seen = set()
    tag_value = ""
    match = re.search(r"/tags/([^/?#]+)", tag_url or "")
    if match:
        tag_value = match.group(1)
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        anchors = soup.find_all("a", href=True)
        for anchor in anchors:
            href = anchor["href"].strip()
            if not href:
                continue
            if href.startswith("http"):
                parsed = urlparse(href)
                if "soundcloud.com" not in (parsed.netloc or "").lower():
                    continue
                path = (parsed.path or "").strip("/")
            else:
                path = href.strip("/")
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) < 2:
                continue
            handle = parts[0]
            if not _sc_is_valid_handle(handle):
                continue
            profile_url = f"https://soundcloud.com/{handle}"
            if profile_url in seen:
                continue
            seen.add(profile_url)
            results.append({"url": profile_url, "primary_genre": tag_value})
    except Exception as exc:
        print(f"SoundCloud: collect failed on {tag_url}: {exc}")
    return results

def _sc_quick_has_fb_or_email(driver, url: str, timeout=10, debug_prefix="") -> tuple:
    fb, em = "", ""
    try:
        handle = _sc_handle_from_profile(url) or url.rstrip("/").split("/")[-1]
        links_links = set()
        root_links = set()
        if handle:
            try:
                links_url = f"https://soundcloud.com/{handle}/links"
                driver.get(links_url)
                WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                _sc_try_dismiss_consent(driver)
                time.sleep(0.6)
                links_links = _sc_collect_profile_links(driver, timeout=6)
                if debug_prefix:
                    print(f"{debug_prefix} links_found={len(links_links)}")
            except Exception:
                if debug_prefix:
                    print(f"{debug_prefix} links page not available")
        if handle and not links_links:
            try:
                root_url = f"https://soundcloud.com/{handle}"
                driver.get(root_url)
                WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                _sc_try_dismiss_consent(driver)
                _sc_try_show_more(driver)
                _sc_scroll_sidebar(driver)
                time.sleep(0.6)
                root_links = _sc_collect_profile_links(driver, timeout=6)
                if debug_prefix:
                    print(f"{debug_prefix} root_found={len(root_links)}")
            except Exception:
                if debug_prefix:
                    print(f"{debug_prefix} root page not available")
        if not handle:
            driver.get(url)
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            root_links = _sc_collect_profile_links(driver, timeout=timeout)
            if debug_prefix:
                print(f"{debug_prefix} root_found={len(root_links)}")

        found = set(links_links) | set(root_links)
        cleaned = []
        for raw in found:
            normalized = _norm_url_general(raw)
            if not normalized:
                continue
            host = (urlparse(normalized).netloc or "").lower()
            if not host or "soundcloud.com" in host:
                continue
            cleaned.append(normalized)

        for candidate in cleaned:
            if _is_fb(candidate) and not _is_company_fb(candidate):
                fb = candidate
                break

        if not em:
            for candidate in cleaned:
                if candidate.startswith("mailto:"):
                    address = candidate.split("mailto:")[-1].split("?")[0]
                    if address and not address.lower().endswith("@soundcloud.com"):
                        em = address
                        break
        if not em:
            try:
                soup = BeautifulSoup(driver.page_source, "html.parser")
                for address in extract_emails(soup.get_text(" ", strip=True)):
                    if address and not address.lower().endswith("@soundcloud.com"):
                        em = address
                        break
            except Exception:
                pass

        if not fb and not em and cleaned:
            fb, em = _sc_try_linktree_for_contacts(driver, set(cleaned), timeout=6)

        if not fb and not em and debug_prefix:
            print(f"{debug_prefix} no fb/email")

    except Exception as exc:
        if debug_prefix:
            print(f"{debug_prefix} error: {exc}\n{traceback.format_exc()}")
    return fb, em

def _sc_profile_basics(driver, profile_url: str, timeout=10) -> tuple:
    location = ""
    bio_text = ""
    try:
        driver.get(profile_url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        try:
            WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".profileHeaderInfo")))
        except Exception:
            pass
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        loc_el = (soup.select_one(".profileHeaderInfo__additional") or
                  soup.select_one(".profileHeaderInfo__location") or
                  soup.select_one("[itemprop='addressLocality']"))
        if loc_el:
            location = loc_el.get_text(" ", strip=True)
        bio_el = (soup.select_one(".profileHeaderInfo__bio") or
                  soup.select_one('[data-testid="profile-bio"]'))
        if bio_el:
            bio_text = bio_el.get_text(" ", strip=True)
        if not location or not bio_text:
            for obj in _sc_extract_objects_from_hydration(html):
                if not isinstance(obj, dict):
                    continue
                if not location:
                    city = obj.get("city") or obj.get("address_city")
                    country = obj.get("country_code") or obj.get("address_country")
                    candidate_loc = " ".join([str(city or ""), str(country or "")]).strip()
                    if candidate_loc:
                        location = candidate_loc
                if not bio_text and isinstance(obj.get("description"), str):
                    bio_text = obj.get("description")
                if location and bio_text:
                    break
    except Exception:
        pass
    return location, bio_text

def _sc_quick_first_track_meta(driver, profile_url: str, timeout=12, hop=True) -> tuple:
    title = ""
    date_iso = ""
    precision = ""
    genres = []
    try:
        handle = profile_url.rstrip("/").split("/")[-1]
        tracks_url = f"https://soundcloud.com/{handle}/tracks"
        driver.get(tracks_url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        card = None
        for c in soup.select(".soundList__item, .lazyLoadingList__item, li, article"):
            t = c.find("time")
            if t and (t.get("datetime") or "").strip():
                card = c
                break
        if card:
            anchor = (card.select_one("a.soundTitle__title") or
                      card.select_one("a.sc-link-primary") or
                      card.select_one(f"a[href^='/{handle}/']"))
            if anchor:
                candidate = anchor.get_text(" ", strip=True)
                if candidate and candidate.lower() not in ("home", "tracks", "likes"):
                    title = candidate[:200]
            t = card.find("time")
            if t:
                dt = (t.get("datetime") or "").strip()
                if dt:
                    iso, prec = _parse_any_date_to_iso(dt)
                    if iso:
                        date_iso, precision = iso, prec or "day"
        for obj in _sc_extract_objects_from_hydration(html):
            if not isinstance(obj, dict):
                continue
            g = obj.get("genre")
            if isinstance(g, str) and g.strip():
                genres.append(g.strip().lower())
            tag_list = obj.get("tag_list")
            if isinstance(tag_list, str) and tag_list.strip():
                for token in re.split(r"[, ]+", tag_list.strip()):
                    if token:
                        genres.append(token.lower())
        if hop and (not title or not date_iso):
            first_url = None
            if card:
                a = card.select_one("a[href]")
                if a and a.get("href"):
                    href = a["href"]
                    first_url = href if href.startswith("http") else f"https://soundcloud.com{href}"
            if first_url:
                driver.get(first_url)
                WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                track_html = driver.page_source
                track_soup = BeautifulSoup(track_html, "html.parser")
                if not title:
                    og = track_soup.select_one('meta[property="og:title"]')
                    if og and og.get("content"):
                        title = og["content"].strip()[:200]
                if not date_iso:
                    t = track_soup.find("time")
                    if t:
                        dt = (t.get("datetime") or "").strip()
                        if dt:
                            iso, prec = _parse_any_date_to_iso(dt)
                            if iso:
                                date_iso, precision = iso, prec or "day"
                for obj in _sc_extract_objects_from_hydration(track_html):
                    if not isinstance(obj, dict):
                        continue
                    g = obj.get("genre")
                    if isinstance(g, str) and g.strip():
                        genres.append(g.strip().lower())
                    tag_list = obj.get("tag_list")
                    if isinstance(tag_list, str) and tag_list.strip():
                        for token in re.split(r"[, ]+", tag_list.strip()):
                            if token:
                                genres.append(token.lower())
        if genres:
            seen = set()
            clean = []
            for g in genres:
                normalized = re.sub(r"[^a-z0-9 +\-_/]", "", g.lower()).strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    clean.append(normalized)
            genres = clean[:6]
    except Exception:
        pass
    return title, date_iso or "", precision or "", genres

def _sc_resolve_artist_profile_url(candidate_url: str) -> str:
    """
    Normalize a candidate SoundCloud URL to canonical artist profile if possible.
    - If already looks like https://soundcloud.com/{handle}, return as is.
    - If it's a track URL, try to strip to /{handle}.
    """
    if not candidate_url:
        return ""
    try:
        url = _sc_normalize_url(candidate_url)
        parsed = urlparse(url)
        if "soundcloud.com" not in parsed.netloc.lower():
            return ""
        path = (parsed.path or "").strip("/")
        if not path:
            return ""
        parts = path.split("/")
        handle = parts[0].strip()
        if not _sc_is_valid_handle(handle):
            return ""
        return f"https://soundcloud.com/{handle}"
    except Exception:
        return ""

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

_SC_JUNK_HOSTS = (
    _SC_JUNK_HOSTS
    if "_SC_JUNK_HOSTS" in globals()
    else {
        "www.enable-javascript.com", "enable-javascript.com",
        "firefox.com", "www.firefox.com", "mozilla.org", "www.mozilla.org",
        "google.com", "www.google.com", "chrome.com", "www.chrome.com",
    }
)

_SC_STORE_HOSTS = (
    _SC_STORE_HOSTS if "_SC_STORE_HOSTS" in globals() else set()
)
_SC_STORE_HOSTS = set(_SC_STORE_HOSTS) | {"apps.apple.com", "itunes.apple.com", "play.google.com"}

_SC_CONSENT_HOSTS = {"onetrust.com", "www.onetrust.com", "cookiepro.com", "www.cookiepro.com"}
_FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.me", "l.facebook.com", "lm.facebook.com"}
_FB_PATH_DENY = {"/soundcloud"}
_FB_REDIRECT_HOSTS = {"l.facebook.com", "lm.facebook.com"}
_LINKTREE_HOSTS = {"linktr.ee", "www.linktr.ee", "linktree", "linktree.com", "www.linktree.com"}
_SC_HOSTS = {"soundcloud.com", "www.soundcloud.com"}
_SC_SOUNDS_PATTERNS = [
    r"\bffo\b[:\-–]\s*([^.;\n]+)",
    r"\briyl\b[:\-–]\s*([^.;\n]+)",
    r"\bfor\s+fans\s+of\b[:\-–]?\s*([^.;\n]+)",
    r"\bsounds\s+like\b[:\-–]?\s*([^.;\n]+)",
    r"\binfluences?\b[:\-–]?\s*([^.;\n]+)",
    r"\binspired\s+by\b[:\-–]?\s*([^.;\n]+)",
]

_SC_RESERVED_SLUGS = {
    "soundcloud", "feed", "upload", "artist", "artists", "tags", "getstarted", "transparency-reports",
    "terms-of-use", "terms", "privacy", "cookie-policy", "cookies", "legal", "copyright", "imprint",
    "contact", "press", "about", "company", "jobs", "developers", "forartists", "pro", "go",
    "on-soundcloud", "login", "signup", "you", "stream", "discover", "charts", "popular", "stations",
    "settings", "pages", "help", "brand", "policy", "resources", "ads"
}

_RESERVED_SC = {
    "search", "popular", "charts", "stream", "you", "discover", "stations",
    "groups", "pro", "for", "creators", "repost", "likes", "home",
    "soundcloud", "soundcloud-scenes", "radio", "radio-indie"
}

def _sc_handle_ok(h: str) -> bool:
    if not h or "/" in h or h.startswith("_"):
        return False
    h = h.strip().lower()
    if h in _RESERVED_SC:
        return False
    return bool(re.match(r"^[a-z0-9][a-z0-9\-_.]{2,}$", h))

def _sc_is_valid_handle(handle: str) -> bool:
    if not handle:
        return False
    h = handle.strip("/").lower()
    if h in _SC_RESERVED_SLUGS:
        return False
    if not any(ch.isalpha() for ch in h):
        return False
    if len(h) < 2 or len(h) > 50:
        return False
    if re.fullmatch(r"[0-9\-]+", h):
        return False
    return True

def _sc_unpack3(candidate):
    if not candidate:
        return ("", "", "")
    if isinstance(candidate, (str, bytes)):
        return (str(candidate).strip(), "", "")
    if isinstance(candidate, dict):
        url = str(candidate.get("url", "")).strip()
        tag = str(candidate.get("tag", "")).strip()
        genre = str(candidate.get("genre", "")).strip()
        if url:
            return (url, tag, genre)
        candidate = list(candidate.values())
    try:
        seq = list(candidate)
    except Exception:
        return (str(candidate).strip(), "", "")
    url = str(seq[0]).strip() if len(seq) > 0 else ""
    tag = str(seq[1]).strip() if len(seq) > 1 else ""
    genre = str(seq[2]).strip() if len(seq) > 2 else ""
    return (url, tag, genre)

def _sc_append(candidate_profiles, candidate):
    candidate_profiles.append(_sc_unpack3(candidate))

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

def _sc_url_mode(url: str) -> tuple:
    if not url:
        return ("tags", "")
    try:
        parsed = _urlparse.urlparse(url)
        host = (parsed.netloc or "").lower()
        if not any(h in host for h in _SC_HOSTS):
            return ("tags", "")
        path = (parsed.path or "").strip("/")
        if path.startswith("search/people"):
            qs = _urlparse.parse_qs(parsed.query or "")
            query = (qs.get("q", [""])[0] or "").strip()
            return ("search_people", query)
        segments = [seg for seg in path.split("/") if seg]
        if segments and segments[0] not in {"search", "discover"}:
            return ("profile", segments[0])
    except Exception:
        pass
    return ("tags", "")

def _sc_extract_tags(soup) -> list:
    """
    Best-effort tag/genre extraction from profile + meta.
    """
    tags = set()
    meta_k = soup.select_one('meta[name="keywords"]')
    if meta_k and meta_k.get("content"):
        for token in _norm_tokens(meta_k["content"]):
            if token:
                tags.add(token.lower())
    for span in soup.find_all(["a", "span"], class_=re.compile("tag|genre|chip", re.I)):
        txt = span.get_text(" ", strip=True)
        if txt:
            tags.add(txt.lower())
    return list(tags)

def _sc_fetch_latest_track(driver, profile_url: str) -> tuple:
    """Return (title, date_iso, precision) strictly from /tracks with <time datetime>."""
    try:
        parsed = urlparse(profile_url)
        handle = (parsed.path or "/").strip("/").split("/")[0]
        if not handle:
            return "", "", ""
        tracks_url = f"https://soundcloud.com/{handle}/tracks"
        driver.get(tracks_url)
        _sc_accept_consent_if_present(driver)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _sc_soft_scroll(driver)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        card = None
        for c in soup.select(".soundList__item, .lazyLoadingList__item, li, article"):
            t = c.find("time")
            if t and (t.get("datetime") or "").strip():
                card = c
                break
        if not card:
            return "", "", ""
        title = ""
        t_anchor = (card.select_one("a.soundTitle__title") or
                    card.select_one("a.sc-link-primary") or
                    card.select_one(f"a[href*='/{handle}/']"))
        if t_anchor:
            candidate = t_anchor.get_text(" ", strip=True)
            if candidate and candidate.lower() not in ("home", "tracks", "likes"):
                title = candidate[:200]
        date_iso, precision = "", ""
        t = card.find("time")
        if t:
            dt = (t.get("datetime") or "").strip()
            if dt:
                iso, prec = _parse_any_date_to_iso(dt)
                if iso:
                    date_iso, precision = iso, (prec or "day")
        return title, date_iso, precision
    except Exception:
        return "", "", ""

def _sc_parse_profile(driver, profile_url: str, seed_primary_genre="") -> dict:
    """
    Visit artist profile and extract details similar to Bandcamp.
    Very defensive: SoundCloud markup changes often.
    """
    artist = {
        "artist_name": "",
        "profile_url": profile_url,
        "location": "",
        "website": "",
        "email": "",
        "socials": {k: "" for k in ["instagram", "twitter", "facebook", "youtube", "linktree", "spotify", "bandsintown", "songkick"]},
        "genres": [],
        "latest_release_title": "",
        "latest_release_date": "",
        "latest_release_precision": "",
        "sounds_like": "",
        "primary_genre": seed_primary_genre or "",
        "source_tag": ""
    }
    try:
        driver.get(profile_url)
        _sc_accept_consent_if_present(driver)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        _sc_soft_scroll(driver)
        profile_html = driver.page_source
        soup = BeautifulSoup(profile_html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            anchor["href"] = _sc_unwrap_gate(anchor.get("href", ""))

        og_title = soup.select_one('meta[property="og:title"]')
        if og_title and og_title.get("content"):
            artist["artist_name"] = og_title["content"].strip()
        if not artist["artist_name"]:
            h1 = soup.find(["h1", "h2"], attrs={"itemprop": re.compile("name", re.I)})
            if h1:
                artist["artist_name"] = h1.get_text(strip=True)

        loc = ""
        for el in soup.select("header [class*='location'], header [class*='small'], header [class*='subheader'], .profileHeader"):
            txt = el.get_text(" ", strip=True)
            if txt and 3 <= len(txt) <= 80 and re.search(r"[A-Za-z]", txt):
                loc = txt
                break
        artist["location"] = loc

        for a in soup.find_all("a", href=True):
            href = _sc_unwrap_gate(_sc_normalize_url(a["href"]))
            if not href:
                continue
            if href.startswith("mailto:"):
                artist["email"] = href.split("mailto:")[-1].split("?")[0]
                continue
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if (not host or
                "soundcloud.com" in host or
                host in _SC_JUNK_HOSTS or
                host in _SC_STORE_HOSTS or
                host in _SC_CONSENT_HOSTS):
                continue
            matched = None
            for dom, key in _SC_SOCIAL_DOMAINS.items():
                if dom in host:
                    matched = key
                    break
            if matched:
                if not artist["socials"].get(matched):
                    artist["socials"][matched] = href
            elif not artist["website"]:
                if parsed.scheme in ("http", "https") and len(host.split(".")) >= 2:
                    artist["website"] = href

        more_links = set()
        try:
            parsed_profile = urlparse(profile_url)
            handle = (parsed_profile.path or "/").strip("/").split("/")[0]
            if handle:
                links_url = f"https://soundcloud.com/{handle}/links"
                driver.get(links_url)
                WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                more_links = _sc_collect_profile_links(driver, timeout=6)
        except Exception:
            more_links = set()

        for raw in more_links:
            href = _sc_unwrap_gate(_sc_normalize_url(raw))
            if not href:
                continue
            if href.startswith("mailto:"):
                if not artist["email"]:
                    address = href.split("mailto:")[-1].split("?")[0]
                    if address and not address.lower().endswith("@soundcloud.com"):
                        artist["email"] = address
                continue
            parsed_href = urlparse(href)
            host = (parsed_href.netloc or "").lower()
            if (not host or
                "soundcloud.com" in host or
                host in _SC_JUNK_HOSTS or
                host in _SC_STORE_HOSTS or
                host in _SC_CONSENT_HOSTS):
                continue
            matched = None
            for dom, key in _SC_SOCIAL_DOMAINS.items():
                if dom in host:
                    matched = key
                    break
            if matched:
                if not artist["socials"].get(matched):
                    artist["socials"][matched] = href
            elif not artist["website"]:
                if parsed_href.scheme in ("http", "https") and len(host.split(".")) >= 2:
                    artist["website"] = href

        artist["genres"] = _sc_extract_tags(soup)
        if not artist["primary_genre"]:
            artist["primary_genre"] = (artist["genres"][0] if artist["genres"] else "")

        lt_title, lt_date, lt_prec = _sc_fetch_latest_track(driver, profile_url)
        if lt_title:
            artist["latest_release_title"] = lt_title
        if lt_date:
            artist["latest_release_date"] = lt_date
            artist["latest_release_precision"] = lt_prec
        else:
            artist["latest_release_date"] = artist.get("latest_release_date", "") or "not present"

        if not artist["latest_release_title"]:
            first_title = soup.find(["a", "div"], attrs={"title": True})
            if first_title:
                t = first_title.get("title", "").strip()
                if t and len(t) <= 120 and t.lower() != "home":
                    artist["latest_release_title"] = t

        text_blob = soup.get_text(" ", strip=True)
        if not artist["sounds_like"]:
            for pattern in _BC_SOUNDS_PATTERNS:
                match = re.search(pattern, text_blob, flags=re.IGNORECASE)
                if match and match.group(1):
                    tokens = _norm_tokens(match.group(1))
                    if tokens:
                        artist["sounds_like"] = ", ".join(t.title() for t in tokens[:5])
                        break

        return artist
    except Exception as exc:
        print(f"SoundCloud: profile parse failed {profile_url}: {exc}")
        return {}

def _sc_is_actionable(artist_dict: dict) -> bool:
    if not artist_dict:
        return False
    if artist_dict.get("website") or artist_dict.get("email"):
        return True
    socials = artist_dict.get("socials", {})
    return any(bool(v) for v in socials.values())

def _sc_write_enriched_csv(rows, existing_csv):
    columns = [
        "Artist Name", "Profile URL", "Website", "Email", "Instagram", "Twitter", "Facebook", "Linktree", "YouTube",
        "Location", "Genres", "Latest Release", "Latest Release Date", "Latest Release Precision", "Sounds Like", "Primary Genre", "Source Tag"
    ]
    base_dir = os.path.dirname(os.path.abspath(existing_csv))
    enriched_path = os.path.join(base_dir, "soundcloud_enriched.csv")
    existing_df = pd.DataFrame(columns=columns)
    if os.path.exists(enriched_path):
        try:
            existing_df = pd.read_csv(enriched_path)
        except Exception:
            existing_df = pd.DataFrame(columns=columns)
    for col in columns:
        if col not in existing_df.columns:
            existing_df[col] = ""
    if not existing_df.empty:
        existing_df = existing_df[columns]
    new_df = pd.DataFrame(rows)
    for col in columns:
        if col not in new_df.columns:
            new_df[col] = ""
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined["__dedupe_key"] = (
        combined["Artist Name"].fillna("").str.strip().str.lower()
        + "||" +
        combined["Profile URL"].fillna("").str.rstrip("/").str.lower()
    )
    combined = combined.drop_duplicates(subset="__dedupe_key").drop(columns="__dedupe_key")
    combined = combined[columns]
    combined.to_csv(enriched_path, index=False, encoding="utf-8-sig")

def scrape_soundcloud(website_url, seed_tags=None, pages_per_tag=SOUNDCLOUD_PAGES_PER_TAG,
                      existing_csv="artist_social_links.csv", max_artists=200,
                      max_handles=None, min_yield=3, dry_run=False):
    print("[init] SoundCloud scraper starting…")
    driver = setup_driver()
    try:
        discovery_session = build_hardened_session()
    except Exception as exc:
        print(f"SoundCloud: failed to build hardened session ({exc}); falling back to basic session.")
        discovery_session = requests.Session()
        discovery_session.headers.update(_rand_headers())
    candidate_profiles = []
    seen_profiles = set()
    sc_rows = []
    enriched_rows = []
    actionable_count = 0
    ACTIONABLE_LIMIT = max_artists
    fast = bool(SOUNDCLOUD_FAST_FACEBOOK_EMAIL_ONLY)
    try:
        url = (website_url or "").strip()
        url_lower = url.lower()
        use_people_url = _sc_is_people_search_url(url)
        use_profile_url = (
            url_lower.startswith("https://soundcloud.com/")
            and "/search/people" not in url_lower
        )
        print(f"SoundCloud: source_url = {url or '(none)'}")

        handles_with_tags = []

        if use_people_url:
            people_params = _sc_parse_people_search(url)
            query = people_params.get("q", "")
            place = people_params.get("place", "")
            param_parts = []
            if query:
                try:
                    encoded_query = _urlparse.quote(query)
                except Exception:
                    encoded_query = query
                param_parts.append(f"q={encoded_query}")
            if place:
                try:
                    encoded_place = _urlparse.quote(place)
                except Exception:
                    encoded_place = place
                param_parts.append(f"filter.place={encoded_place}")
            if param_parts:
                people_url = f"https://soundcloud.com/search/people?{'&'.join(param_parts)}"
            else:
                people_url = url
            search_cap = max_handles or max_artists
            handles = []
            try:
                handles = discover_handles(discovery_session, people_url, limit=search_cap)
            except Exception as exc:
                print(f"SoundCloud: people search fetch failed: {exc}")
            if search_cap:
                handles = handles[:search_cap]
            tag_hint = query or place or ""
            print(f"SoundCloud: people search -> {len(handles)} handles (query='{query}' place='{place}')")
            handles_with_tags.extend((h, tag_hint) for h in handles)

        elif use_profile_url:
            parsed = urlparse(url)
            path = (parsed.path or "").strip("/")
            if path and "/" not in path:
                handle = path
                if _sc_handle_ok(handle):
                    print(f"SoundCloud: single profile mode -> {url}")
                    handles_with_tags.append((handle, ""))
                else:
                    print(f"SoundCloud: provided profile URL has an invalid handle: {handle}")
            else:
                print("SoundCloud: provided URL is not a single profile; skipping.")

        else:
            tags = [ (t or "").strip() for t in (seed_tags or []) if (t or "").strip() ]
            if not url and not tags:
                print("SoundCloud: no usable URL or tags provided; nothing to do.")
            elif tags:
                print(f"SoundCloud: fallback to tags (last resort): {tags}")
                for tag in tags:
                    normalized_tag = tag.lower()
                    normalized_tag = re.sub(r"\s+", "-", normalized_tag).strip("-") or normalized_tag
                    try:
                        tag_path = _urlparse.quote(normalized_tag)
                    except Exception:
                        tag_path = normalized_tag
                    tag_url = f"https://soundcloud.com/tags/{tag_path}"
                    try:
                        tag_handles = discover_handles(discovery_session, tag_url)
                    except Exception as exc:
                        print(f"SoundCloud: tag '{tag}' page fetch failed: {exc}")
                        tag_handles = []
                    if tag_handles:
                        print(f"SoundCloud: tag '{tag}' (/tags) -> {len(tag_handles)} handles")
                        handles_with_tags.extend((h, tag) for h in tag_handles)
                    if len(handles_with_tags) >= max_artists:
                        break
                    try:
                        encoded = _urlparse.quote(tag)
                    except Exception:
                        encoded = tag
                    people_url = f"https://soundcloud.com/search/people?q={encoded}"
                    search_cap = max_handles or max_artists
                    handles = []
                    try:
                        handles = discover_handles(discovery_session, people_url, limit=search_cap)
                    except Exception as exc:
                        print(f"SoundCloud: tag '{tag}' people search fetch failed: {exc}")
                    if search_cap:
                        handles = handles[:search_cap]
                    if handles:
                        print(f"SoundCloud: tag '{tag}' (search) -> {len(handles)} handles")
                        handles_with_tags.extend((h, tag) for h in handles)
                    if len(handles_with_tags) >= max_artists:
                        break
            else:
                print("SoundCloud: provided URL is not SoundCloud; no fallback tags, aborting.")

        dedup_handles = []
        seen_handles = set()
        for handle, source_tag in handles_with_tags:
            clean = (handle or "").strip()
            if not _sc_handle_ok(clean):
                continue
            if clean in seen_handles:
                continue
            seen_handles.add(clean)
            dedup_handles.append((clean, source_tag))

        if dry_run:
            dedup_handles = dedup_handles[:10]
            print(f"SoundCloud: dry-run mode limiting to {len(dedup_handles)} handles.")

        if max_handles and max_handles > 0:
            dedup_handles = dedup_handles[:max_handles]

        print(f"SoundCloud: total artist handles to visit {len(dedup_handles)}")
        if dedup_handles[:5]:
            print(f"SoundCloud: first 5 handles -> {[h for h, _ in dedup_handles[:5]]}")

        for handle, tag_value in dedup_handles:
            profile_url = f"https://soundcloud.com/{handle}"
            key = profile_url.rstrip("/").lower()
            if key in seen_profiles:
                continue
            seen_profiles.add(key)
            candidate_profiles.append((profile_url, tag_value or "", ""))

        if not candidate_profiles:
            print("SoundCloud: no candidate_profiles after provided input; check URL or filters.")

        if fast and len(candidate_profiles) > SOUNDCLOUD_FAST_MAX_CANDIDATES:
            candidate_profiles = candidate_profiles[:SOUNDCLOUD_FAST_MAX_CANDIDATES]

        print(f"SoundCloud: total artist profiles resolved {len(candidate_profiles)}")
        if not candidate_profiles:
            print("SoundCloud: no candidate_profiles; check tag or page selectors.")
        else:
            preview_handles = [
                (_sc_unpack3(c)[0].rstrip("/").split("/")[-1])
                for c in candidate_profiles[:5]
            ]
            print("SoundCloud: first 5 handles ->", preview_handles)
        weird_shapes = [c for c in candidate_profiles if not isinstance(c, (tuple, list)) or len(c) != 3]
        if weird_shapes:
            print(f"SoundCloud: normalized {len(weird_shapes)} non-3-tuples in candidates")
        if fast:
            handle_jobs = []
            for cand in candidate_profiles:
                profile_url, source_tag, seed_primary_genre = _sc_unpack3(cand)
                if not profile_url:
                    continue
                handle = _sc_handle_from_profile(profile_url)
                if not handle:
                    continue
                handle_jobs.append({
                    "handle": handle,
                    "profile_url": profile_url,
                    "source_tag": source_tag,
                    "seed_primary_genre": seed_primary_genre,
                })
            contact_map, processed_jobs, batch_hits = _sc_collect_contact_links(handle_jobs, min_yield or 0)
            min_yield_msg = None
            if (min_yield or 0) and batch_hits < (min_yield or 0):
                min_yield_msg = f"SoundCloud: last batch produced {batch_hits} contacts (< {min_yield}). Consider adjusting your query."
            for idx, job in enumerate(processed_jobs):
                profile_url = job["profile_url"]
                source_tag = job["source_tag"]
                seed_primary_genre = job["seed_primary_genre"]
                handle = job["handle"]
                if not profile_url:
                    continue
                if not _sc_is_valid_handle(handle):
                    print(f"skip[{idx}] invalid handle: {profile_url}")
                    continue
                contact_payload = contact_map.get(handle, {})
                contact_data = contact_payload.get("data")
                if not _sc_has_contact_links(contact_payload):
                    print(f"skip[{idx}] no links: {handle}")
                    continue
                contact_song_title = ""
                contact_release_date = ""
                contact_tags = []
                contact_sounds_like = ""
                if isinstance(contact_data, dict):
                    contact_song_title = (contact_data.get("latest_track_title") or "").strip()
                    contact_release_date = (contact_data.get("latest_track_release_date") or "").strip()
                    tags_candidate = contact_data.get("latest_track_tags") or []
                    if isinstance(tags_candidate, (list, tuple)):
                        contact_tags = [tag for tag in tags_candidate if isinstance(tag, str)]
                    contact_sounds_like = (contact_data.get("sounds_like") or "").strip()
                    if not contact_sounds_like:
                        contact_sounds_like = _sc_sounds_like_from_bio(contact_data.get("bio_text", ""))
                location_text, bio_text = _sc_profile_basics(driver, profile_url, timeout=10)
                title, date_iso, prec, genres = _sc_quick_first_track_meta(driver, profile_url, timeout=12, hop=True)
                try:
                    driver.get(profile_url)
                    WebDriverWait(driver, SOUNDCLOUD_FAST_TIMEOUT_SEC).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    soup_name = BeautifulSoup(driver.page_source, "html.parser")
                except Exception:
                    soup_name = None
                fallback_name = handle.replace("-", " ").replace("_", " ").title()
                if soup_name:
                    try:
                        og = soup_name.select_one('meta[property="og:title"]')
                        if og and og.get("content"):
                            candidate_name = og["content"].strip()
                            if candidate_name:
                                fallback_name = candidate_name[:200]
                    except Exception:
                        pass
                sounds_like_value = _sc_sounds_like_from_bio(bio_text)
                try:
                    meta = _sc_extract_profile_meta(driver)
                except Exception:
                    meta = {
                        "artist_name": "",
                        "location": "",
                        "song_title": "",
                        "primary_genre": "",
                        "release_date": "not present",
                        "sounds_like": ""
                    }
                fallback_name = (meta.get("artist_name") or fallback_name or "").strip()
                fallback_location = meta.get("location") or location_text or ""
                song_title_value = (meta.get("song_title") or contact_song_title or title or "").strip()
                if meta.get("sounds_like"):
                    sounds_like_value = meta["sounds_like"]
                elif contact_sounds_like:
                    sounds_like_value = contact_sounds_like
                meta_release = meta.get("release_date") or ""
                release_date_value = meta_release if meta_release and meta_release.lower() != "not present" else (contact_release_date or date_iso or "")
                combined_tags = list(genres or [])
                if contact_tags:
                    combined_tags.extend(contact_tags)
                row, external_urls, emails = _sc_build_row(
                    handle=handle,
                    payload=contact_data,
                    soundcloud_link=profile_url,
                    fallback_name=fallback_name,
                    fallback_location=fallback_location,
                    song_title=song_title_value,
                    release_date=release_date_value,
                    sounds_like=sounds_like_value,
                    fallback_tags=combined_tags,
                    fallback_external=None,
                    fallback_emails=None,
                )
                _sc_log_csv_row(handle, row, external_urls, emails)
                if dry_run:
                    _sc_print_dry_run_row(handle, row, external_urls, emails)
                    continue
                sc_rows.append(row)
                actionable_count += 1
                if actionable_count >= ACTIONABLE_LIMIT:
                    break
                time.sleep(random.uniform(0.2, 0.6))
            if dry_run:
                print(f"SoundCloud: dry-run complete – {batch_hits}/{len(processed_jobs)} handles yielded outbound links.")
            if min_yield_msg:
                print(min_yield_msg)
        else:
            for idx, cand in enumerate(candidate_profiles):
                profile_url, source_tag, seed_primary_genre = _sc_unpack3(cand)
                if not profile_url:
                    continue
                artist = _sc_parse_profile(driver, profile_url, seed_primary_genre=seed_primary_genre or source_tag)
                if not artist:
                    continue
                artist["source_tag"] = source_tag
                if SOUNDCLOUD_MIN_CONTACT_REQUIREMENT and not _sc_is_actionable(artist):
                    continue
                artist_profile_link = (artist.get("profile_url") or profile_url or "").strip()

                contact_links = []
                if artist.get("website"):
                    contact_links.append(artist["website"])
                for s in artist.get("socials", {}).values():
                    if s:
                        contact_links.append(s)
                if artist.get("email"):
                    contact_links.append(f"mailto:{artist['email']}")
                contact_links = list(dict.fromkeys([x for x in contact_links if x]))
                contact_links = [
                    link for link in contact_links
                    if urlparse(link).netloc.lower() not in _SC_CONSENT_HOSTS
                ]

                artist_name_value = artist.get("artist_name", "").strip()
                if not (artist_name_value and contact_links):
                    continue

                location_value = artist.get("location", "")
                song_title_value = artist.get("latest_release_title", "")
                sounds_like_value = artist.get("sounds_like", "")
                release_date_value = artist.get("latest_release_date", "") or "not present"

                try:
                    current_url = ""
                    try:
                        current_url = driver.current_url
                    except Exception:
                        current_url = ""
                    if current_url.rstrip("/") != profile_url.rstrip("/"):
                        driver.get(profile_url)
                        WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    meta = _sc_extract_profile_meta(driver)
                except Exception:
                    meta = {
                        "artist_name": "",
                        "location": "",
                        "song_title": "",
                        "primary_genre": "",
                        "release_date": "not present",
                        "sounds_like": ""
                    }

                artist_name_value = (meta.get("artist_name") or artist_name_value).strip()
                location_value = meta.get("location") or location_value or ""
                song_title_value = meta.get("song_title") or song_title_value or ""
                sounds_like_value = meta.get("sounds_like") or sounds_like_value or ""
                meta_release = meta.get("release_date") or ""
                if meta_release and meta_release.lower() != "not present":
                    release_date_value = meta_release
                elif not release_date_value:
                    release_date_value = "not present"

                meta_primary = (meta.get("primary_genre") or "").strip()
                primary_genre_value = meta_primary or (artist.get("primary_genre", "") or "")
                if isinstance(primary_genre_value, str) and primary_genre_value:
                    primary_genre_value = primary_genre_value.title()

                http_links = [
                    link for link in contact_links
                    if isinstance(link, str)
                    and link.startswith(("http://", "https://"))
                    and link.lower() != "http://firefox.com"
                ]
                email_fallback = [artist.get("email")] if artist.get("email") else []
                handle_slug = _sc_handle_from_profile(profile_url) or artist_name_value or ""
                payload_override = {
                    "display_name": artist_name_value,
                    "city": "",
                    "country": "",
                    "genre": artist.get("primary_genre", ""),
                    "external_urls": http_links,
                    "emails": email_fallback,
                }
                row, external_urls, emails = _sc_build_row(
                    handle=handle_slug,
                    payload=payload_override,
                    soundcloud_link=artist_profile_link,
                    fallback_name=artist_name_value,
                    fallback_location=location_value,
                    song_title=song_title_value,
                    release_date="" if release_date_value.lower() == "not present" else release_date_value,
                    sounds_like=sounds_like_value,
                    fallback_tags=artist.get("genres", []),
                    fallback_external=None,
                    fallback_emails=None
                )
                _sc_log_csv_row(handle_slug, row, external_urls, emails)
                sc_rows.append(row)

                socials = artist.get("socials", {})
                enriched_rows.append({
                    "Artist Name": artist_name_value,
                    "Profile URL": artist.get("profile_url", ""),
                    "Website": artist.get("website", ""),
                    "Email": artist.get("email", ""),
                    "Instagram": socials.get("instagram", ""),
                    "Twitter": socials.get("twitter", ""),
                    "Facebook": socials.get("facebook", ""),
                    "Linktree": socials.get("linktree", ""),
                    "YouTube": socials.get("youtube", ""),
                    "Location": artist.get("location", ""),
                    "Genres": "; ".join(artist.get("genres", [])),
                    "Latest Release": artist.get("latest_release_title", ""),
                    "Latest Release Date": artist.get("latest_release_date", ""),
                    "Latest Release Precision": artist.get("latest_release_precision", ""),
                    "Sounds Like": artist.get("sounds_like", ""),
                    "Primary Genre": primary_genre_value,
                    "Source Tag": artist.get("source_tag", "")
                })

                actionable_count += 1
                if actionable_count >= ACTIONABLE_LIMIT:
                    break
                time.sleep(random.uniform(1.0, 2.0))
        print(f"SoundCloud: total actionable artists written {actionable_count}")
    finally:
        try:
            discovery_session.close()
        except Exception:
            pass
        driver.quit()

    if dry_run:
        print("SoundCloud: dry-run requested; skipping CSV write.")
        return

    save_soundcloud_csv(sc_rows, existing_csv)
    if enriched_rows:
        _sc_write_enriched_csv(enriched_rows, existing_csv)
# =============================================================================
# Facebook Scraping Functions (Page 2)
# =============================================================================
def extract_emails(text):
    email_pattern = r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
    return list(set(re.findall(email_pattern, text)))

def login_facebook(driver, fb_username, fb_password):
    driver.get('https://www.facebook.com/')
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, 'email')))
    driver.find_element(By.ID, 'email').send_keys(fb_username)
    driver.find_element(By.ID, 'pass').send_keys(fb_password)
    driver.find_element(By.NAME, 'login').click()
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))

def _extract_social_links(row):
    """Return all usable social URLs (split on ';' or ',') from likely columns."""
    candidate_columns = [
        "Social Link",
        "social link",
        "SOCIAL LINK",
        "Facebook",
        "facebook",
        "FACEBOOK"
    ]
    urls = []
    for col in candidate_columns:
        if col in row and pd.notna(row[col]):
            value = str(row[col]).strip()
            if value:
                parts = re.split(r"[;,]", value)
                for part in parts:
                    url = part.strip()
                    if url:
                        urls.append(url)
    return urls


def _extract_social_link_from_row(row):
    """Maintain backward compatibility: return the first social link if present."""
    links = _extract_social_links(row)
    return links[0] if links else ""

def _safe_row_value(row, key, fallback=""):
    if key not in row:
        return fallback
    value = row.get(key)
    if pd.isna(value):
        return fallback
    return value

def _goto_facebook_about(driver, page_url: str, timeout: float = 5.0) -> bool:
    """
    Try multiple strategies to land on the About tab for a Facebook page.
    Falls back to direct /about URLs when buttons are not available.
    """
    normalized = (page_url or "").strip()
    if not normalized:
        return False
    about_selectors = [
        (By.XPATH, "//a[contains(@href,'about_contact_and_basic_info')]"),
        (By.XPATH, "//a[contains(@href,'about_details')]"),
        (By.XPATH, "//a[contains(@href,'/about')]"),
        (By.XPATH, "//a[.//span[text()='About']]"),
        (By.XPATH, "//a[normalize-space(text())='About']"),
    ]
    for by, locator in about_selectors:
        try:
            target = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, locator)))
            driver.execute_script("arguments[0].click();", target)
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            return True
        except Exception:
            continue
    base = normalized.rstrip("/")
    about_variants = []
    try:
        parsed = urlparse(normalized)
        path = (parsed.path or "").rstrip("/")
        base = f"{parsed.scheme}://{parsed.netloc}{path}"
        about_variants = [
            f"{base}/about_contact_and_basic_info",
            f"{base}/about_details",
            f"{base}/about",
        ]
    except Exception:
        about_variants = [f"{base}/about"]
    for candidate in about_variants:
        try:
            driver.get(candidate)
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            return True
        except Exception:
            continue
    return False

# =============================================================================
# UPDATED scrape_csv Function (Simpler Version with Wait Times of 0.5 sec and Session Refresh every 20 pages)
# =============================================================================
def scrape_csv(input_csv, output_csv, fb_username, fb_password, max_emails=None):
    existing_data = pd.DataFrame()
    if os.path.exists(output_csv):
        existing_data = pd.read_csv(output_csv)
    data = pd.read_csv(input_csv)
    # Normalize column names to remove extra whitespace.
    data.columns = [col.strip() for col in data.columns]
    results = []
    emails_found = 0
    processed_urls = set(existing_data['url'].tolist() if not existing_data.empty else [])
    exclude_urls = {"https://www.facebook.com/triplejunearthed/", "https://www.facebook.com/abc/"}
    exclude_urls_lower = {url.lower() for url in exclude_urls}
    facebook_rows = []
    for index, row in data.iterrows():
        links = _extract_social_links(row)
        if not links:
            continue
        for candidate in links:
            url = candidate.strip()
            if not url:
                continue
            url_lower = url.lower()
            if url_lower in exclude_urls_lower or url in processed_urls:
                continue
            if "facebook.com" in url_lower:
                facebook_rows.append((row, url))
                break
    if not facebook_rows:
        print("No Facebook pages to process.")
        return
    driver = setup_facebook_driver()
    login_facebook(driver, fb_username, fb_password)
    session_counter = 0
    for row, url in facebook_rows:
        if not url:
            continue
        preexisting_emails = []
        if 'Email' in row and pd.notna(row['Email']):
            preexisting_emails = extract_emails(str(row['Email']))
        try:
            print(f"Scraping Facebook page: {url}")
            driver.get(url)
            session_counter += 1
            # Refresh the session every 20 pages.
            if session_counter % 20 == 0:
                print("Refreshing Facebook session...")
                driver.quit()
                driver = setup_facebook_driver()
                login_facebook(driver, fb_username, fb_password)
            navigated = _goto_facebook_about(driver, url, timeout=5)
            if not navigated:
                print(f"Warning: could not open About section for {url}; scanning current page.")
            time.sleep(1.0)
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            emails = list(preexisting_emails)
            body_text = soup.get_text(" ", strip=True)
            if body_text:
                emails.extend(extract_emails(body_text))
            for anchor in soup.select('a[href^="mailto:"]'):
                href = anchor.get("href") or ""
                if href.startswith("mailto:"):
                    addr = href.split("mailto:")[-1].split("?", 1)[0]
                    if addr:
                        emails.append(addr)
            unique_emails = sorted(set(email.strip() for email in emails if email))
            if unique_emails:
                # Format artist name: replace hyphens with spaces and capitalise each word.
                artist_name = row.get('Artist Name', '')
                artist_name = artist_name.replace('-', ' ').title()
                song_title = _safe_row_value(row, 'Song Title', '')
                if (not song_title) and ('Latest Release' in row):
                    song_title = _safe_row_value(row, 'Latest Release', '')
                release_date_value = _safe_row_value(row, 'Release Date', '') or _safe_row_value(row, 'Latest Release Date', '')
                primary_genre_value = _safe_row_value(row, 'Primary Genre', '')
                source_tag = _safe_row_value(row, 'Source Tag', '')
                results.append({
                    'artist': artist_name,
                    'location': row.get('Location', ''),
                    'song_title': song_title,
                    'sounds_like': row.get('Sounds Like', ''),
                    'release_date': release_date_value,
                    'Release Date': release_date_value,
                    'url': url,
                    'emails': ', '.join(unique_emails),
                    'Played on triple J': row.get('Played on triple J', ''),
                    'Played on Unearthed': row.get('Played on Unearthed', ''),
                    'latest_release_date': release_date_value,
                    'primary_genre': primary_genre_value,
                    'source_tag': source_tag,
                    'date_added': datetime.datetime.now().strftime("%Y-%m-%d")
                })
                emails_found += len(unique_emails)
                if max_emails is not None and emails_found >= max_emails:
                    break
        except Exception as e:
            print(f"Error scraping {url}: {e}")
        processed_urls.add(url)
        # Random sleep between 1 and 2 seconds.
        time.sleep(random.uniform(1, 2))
    driver.quit()
    results_df = pd.DataFrame(results)
    combined_data = pd.concat([existing_data, results_df]).drop_duplicates(subset=['url', 'emails'])
    combined_data.to_csv(output_csv, index=False)
    print(f"Scraping completed. Results saved to {output_csv}")

# =============================================================================
# PyQt5 GUI Code
# =============================================================================
class ArtistScraperThread(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal()
    def __init__(self, website_url, max_artists, output_csv, source="Unearthed",
                 pages_per_tag=BANDCAMP_PAGES_PER_TAG, seed_tags=None, parent=None):
        super().__init__(parent)
        self.website_url = website_url
        self.max_artists = max_artists
        self.output_csv = output_csv
        self.source = source
        self.pages_per_tag = pages_per_tag
        if seed_tags is not None:
            self.seed_tags = list(seed_tags)
        elif self.source and self.source.lower() == "soundcloud":
            if website_url and website_url.strip():
                self.seed_tags = []
            else:
                self.seed_tags = list(SOUNDCLOUD_SEED_TAGS)
        else:
            self.seed_tags = list(BANDCAMP_SEED_TAGS)
    def run(self):
        self.log_signal.emit("Starting artist scraping...")
        try:
            if self.source.lower() == "bandcamp":
                target = self.website_url if _bc_is_bandcamp_url((self.website_url or "").strip()) else self.seed_tags
                scrape_bandcamp(
                    target,
                    pages_per_tag=self.pages_per_tag,
                    existing_csv=self.output_csv,
                    max_artists=self.max_artists
                )
                self.log_signal.emit("Bandcamp scraping completed.")
            elif self.source.lower() == "soundcloud":
                scrape_soundcloud(
                    (self.website_url or "").strip(),
                    seed_tags=self.seed_tags,
                    pages_per_tag=self.pages_per_tag,
                    existing_csv=self.output_csv,
                    max_artists=self.max_artists
                )
                self.log_signal.emit("SoundCloud scraping completed.")
            else:
                scrape_website(self.website_url, existing_csv=self.output_csv, max_artists=self.max_artists)
                self.log_signal.emit("Artist scraping completed.")
        except Exception as e:
            self.log_signal.emit(f"Error in artist scraping: {e}")
        self.finished_signal.emit()

class FacebookScraperThread(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal()
    def __init__(self, input_csv, output_csv, fb_username, fb_password, max_emails, parent=None):
        super().__init__(parent)
        self.input_csv = input_csv
        self.output_csv = output_csv
        self.fb_username = fb_username
        self.fb_password = fb_password
        self.max_emails = max_emails
    def run(self):
        self.log_signal.emit("Starting Facebook scraping...")
        try:
            scrape_csv(self.input_csv, self.output_csv, self.fb_username, self.fb_password, self.max_emails)
            self.log_signal.emit("Facebook scraping completed.")
        except Exception as e:
            self.log_signal.emit(f"Error in Facebook scraping: {e}")
        self.finished_signal.emit()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Artist & Facebook Scraper")
        self.setMinimumSize(800, 600)
        self.create_menu()
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.artist_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.artist_tab, "Artist Scraping")
        self.create_artist_tab()
        self.facebook_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.facebook_tab, "Facebook Scraping")
        self.create_facebook_tab()
    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        save_as_action = QtWidgets.QAction("Save As (Facebook Output)...", self)
        save_as_action.triggered.connect(self.save_as_facebook_csv)
        file_menu.addAction(save_as_action)
    def save_as_facebook_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Facebook Output CSV As", "", "CSV Files (*.csv)")
        if file_path:
            self.output_csv_edit.setText(file_path)
    def browse_artist_output_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Select Artist Output CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.artist_output_csv_edit.setText(file_path)
    def browse_input_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Input CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.input_csv_edit.setText(file_path)
    def browse_output_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Select Facebook Output CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.output_csv_edit.setText(file_path)
    def create_artist_tab(self):
        layout = QtWidgets.QVBoxLayout()
        source_layout = QtWidgets.QHBoxLayout()
        source_label = QtWidgets.QLabel("Source:")
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItems(["Unearthed", "Bandcamp", "SoundCloud"])
        self.source_combo.currentTextChanged.connect(self.on_source_changed)
        source_layout.addWidget(source_label)
        source_layout.addWidget(self.source_combo)
        layout.addLayout(source_layout)
        url_layout = QtWidgets.QHBoxLayout()
        url_label = QtWidgets.QLabel("Website URL:")
        self.url_edit = QtWidgets.QLineEdit(UNEARTHED_DEFAULT_URL)
        self.url_edit.setPlaceholderText(UNEARTHED_DEFAULT_URL)
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_edit)
        layout.addLayout(url_layout)
        pages_layout = QtWidgets.QHBoxLayout()
        pages_label = QtWidgets.QLabel("Pages per Tag:")
        self.pages_per_tag_edit = QtWidgets.QLineEdit(str(BANDCAMP_PAGES_PER_TAG))
        self.pages_per_tag_edit.setEnabled(False)
        pages_layout.addWidget(pages_label)
        pages_layout.addWidget(self.pages_per_tag_edit)
        layout.addLayout(pages_layout)
        max_artists_layout = QtWidgets.QHBoxLayout()
        max_artists_label = QtWidgets.QLabel("Max Artists:")
        self.max_artists_edit = QtWidgets.QLineEdit("200")
        max_artists_layout.addWidget(max_artists_label)
        max_artists_layout.addWidget(self.max_artists_edit)
        layout.addLayout(max_artists_layout)
        artist_output_layout = QtWidgets.QHBoxLayout()
        artist_output_label = QtWidgets.QLabel("Output CSV:")
        self.artist_output_csv_edit = QtWidgets.QLineEdit("artist_social_links.csv")
        artist_output_browse = QtWidgets.QPushButton("Browse")
        artist_output_browse.clicked.connect(self.browse_artist_output_csv)
        artist_output_layout.addWidget(artist_output_label)
        artist_output_layout.addWidget(self.artist_output_csv_edit)
        artist_output_layout.addWidget(artist_output_browse)
        layout.addLayout(artist_output_layout)
        self.artist_start_button = QtWidgets.QPushButton("Start Artist Scraping")
        self.artist_start_button.clicked.connect(self.start_artist_scraping)
        layout.addWidget(self.artist_start_button)
        self.artist_progress_bar = QtWidgets.QProgressBar()
        self.artist_progress_bar.setRange(0, 0)
        self.artist_progress_bar.setVisible(False)
        layout.addWidget(self.artist_progress_bar)
        self.artist_log = QtWidgets.QTextEdit()
        self.artist_log.setReadOnly(True)
        layout.addWidget(self.artist_log)
        self.artist_tab.setLayout(layout)
    def on_source_changed(self, source_text):
        if source_text == "Bandcamp":
            self.url_edit.setPlaceholderText(BANDCAMP_DEFAULT_TAG_URL)
            current = self.url_edit.text().strip()
            if not current or current in (UNEARTHED_DEFAULT_URL, SOUNDCLOUD_DEFAULT_TAG_URL):
                self.url_edit.setText(BANDCAMP_DEFAULT_TAG_URL)
            self.pages_per_tag_edit.setEnabled(True)
        elif source_text == "SoundCloud":
            self.url_edit.setPlaceholderText(SOUNDCLOUD_DEFAULT_TAG_URL)
            current = self.url_edit.text().strip()
            if not current or current in (UNEARTHED_DEFAULT_URL, BANDCAMP_DEFAULT_TAG_URL):
                self.url_edit.setText(SOUNDCLOUD_DEFAULT_TAG_URL)
            self.pages_per_tag_edit.setEnabled(True)
        else:
            self.url_edit.setPlaceholderText(UNEARTHED_DEFAULT_URL)
            current = self.url_edit.text().strip()
            if not current or current in (BANDCAMP_DEFAULT_TAG_URL, SOUNDCLOUD_DEFAULT_TAG_URL):
                self.url_edit.setText(UNEARTHED_DEFAULT_URL)
            self.pages_per_tag_edit.setEnabled(False)
    def create_facebook_tab(self):
        layout = QtWidgets.QVBoxLayout()
        input_layout = QtWidgets.QHBoxLayout()
        input_label = QtWidgets.QLabel("Input CSV:")
        self.input_csv_edit = QtWidgets.QLineEdit("test_artist_social_links.csv")
        input_browse = QtWidgets.QPushButton("Browse")
        input_browse.clicked.connect(self.browse_input_csv)
        input_layout.addWidget(input_label)
        input_layout.addWidget(self.input_csv_edit)
        input_layout.addWidget(input_browse)
        layout.addLayout(input_layout)
        output_layout = QtWidgets.QHBoxLayout()
        output_label = QtWidgets.QLabel("Output CSV:")
        self.output_csv_edit = QtWidgets.QLineEdit("test_combined_artist_data.csv")
        output_browse = QtWidgets.QPushButton("Browse")
        output_browse.clicked.connect(self.browse_output_csv)
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_csv_edit)
        output_layout.addWidget(output_browse)
        layout.addLayout(output_layout)
        fb_layout = QtWidgets.QHBoxLayout()
        fb_user_label = QtWidgets.QLabel("Facebook Username:")
        self.fb_username_edit = QtWidgets.QLineEdit()
        fb_pass_label = QtWidgets.QLabel("Facebook Password:")
        self.fb_password_edit = QtWidgets.QLineEdit()
        self.fb_password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        fb_layout.addWidget(fb_user_label)
        fb_layout.addWidget(self.fb_username_edit)
        fb_layout.addWidget(fb_pass_label)
        fb_layout.addWidget(self.fb_password_edit)
        layout.addLayout(fb_layout)
        max_emails_layout = QtWidgets.QHBoxLayout()
        max_emails_label = QtWidgets.QLabel("Max Emails (optional):")
        self.max_emails_edit = QtWidgets.QLineEdit()
        max_emails_layout.addWidget(max_emails_label)
        max_emails_layout.addWidget(self.max_emails_edit)
        layout.addLayout(max_emails_layout)
        self.fb_start_button = QtWidgets.QPushButton("Start Facebook Scraping")
        self.fb_start_button.clicked.connect(self.start_facebook_scraping)
        layout.addWidget(self.fb_start_button)
        self.fb_progress_bar = QtWidgets.QProgressBar()
        self.fb_progress_bar.setRange(0, 0)
        self.fb_progress_bar.setVisible(False)
        layout.addWidget(self.fb_progress_bar)
        self.fb_log = QtWidgets.QTextEdit()
        self.fb_log.setReadOnly(True)
        layout.addWidget(self.fb_log)
        self.facebook_tab.setLayout(layout)
    def start_artist_scraping(self):
        source = self.source_combo.currentText()
        url = self.url_edit.text().strip()
        if source in ("Bandcamp", "SoundCloud") and not url:
            default_url = BANDCAMP_DEFAULT_TAG_URL if source == "Bandcamp" else SOUNDCLOUD_DEFAULT_TAG_URL
            url = default_url
            self.url_edit.setText(url)
        if source == "Unearthed" and not url:
            self.artist_log.append("Please enter a valid website URL.")
            return
        try:
            max_artists = int(self.max_artists_edit.text().strip())
        except ValueError:
            max_artists = 200
        try:
            pages_per_tag = int(self.pages_per_tag_edit.text().strip())
        except ValueError:
            if source == "Bandcamp":
                pages_per_tag = BANDCAMP_PAGES_PER_TAG
            elif source == "SoundCloud":
                pages_per_tag = SOUNDCLOUD_PAGES_PER_TAG
            else:
                pages_per_tag = BANDCAMP_PAGES_PER_TAG
        if max_artists <= 0:
            max_artists = 200
        if source == "Bandcamp":
            default_pages = BANDCAMP_PAGES_PER_TAG
        elif source == "SoundCloud":
            default_pages = SOUNDCLOUD_PAGES_PER_TAG
        else:
            default_pages = BANDCAMP_PAGES_PER_TAG
        if pages_per_tag <= 0:
            pages_per_tag = default_pages
        seed_tags = None
        if source == "Bandcamp":
            if _bandcamp_is_discover_url(url):
                seed_tags = []
            else:
                extracted_tag = _bandcamp_extract_tag_from_url(url)
                seed_tags = [extracted_tag] if extracted_tag else list(BANDCAMP_SEED_TAGS)
        elif source == "SoundCloud":
            match = re.search(r"/tags/([^/?#]+)", url)
            seed_tags = [match.group(1).lower()] if match else list(SOUNDCLOUD_SEED_TAGS)
        output_csv = self.artist_output_csv_edit.text().strip()
        self.artist_start_button.setEnabled(False)
        self.artist_progress_bar.setVisible(True)
        self.artist_log.append("Initiating artist scraping...")
        self.artist_thread = ArtistScraperThread(
            url,
            max_artists,
            output_csv,
            source=source,
            pages_per_tag=pages_per_tag,
            seed_tags=seed_tags
        )
        self.artist_thread.log_signal.connect(self.update_artist_log)
        self.artist_thread.finished_signal.connect(self.artist_scraping_finished)
        self.artist_thread.start()
    def update_artist_log(self, message):
        self.artist_log.append(message)
    def artist_scraping_finished(self):
        self.artist_log.append("Artist scraping thread finished.")
        self.artist_progress_bar.setVisible(False)
        self.artist_start_button.setEnabled(True)
    def start_facebook_scraping(self):
        input_csv = self.input_csv_edit.text().strip()
        output_csv = self.output_csv_edit.text().strip()
        fb_username = self.fb_username_edit.text().strip()
        fb_password = self.fb_password_edit.text().strip()
        max_emails_text = self.max_emails_edit.text().strip()
        max_emails = int(max_emails_text) if max_emails_text.isdigit() else None
        if not input_csv or not output_csv or not fb_username or not fb_password:
            self.fb_log.append("Please fill in all required fields for Facebook scraping.")
            return
        self.fb_start_button.setEnabled(False)
        self.fb_progress_bar.setVisible(True)
        self.fb_log.append("Initiating Facebook scraping...")
        self.fb_thread = FacebookScraperThread(input_csv, output_csv, fb_username, fb_password, max_emails)
        self.fb_thread.log_signal.connect(self.update_fb_log)
        self.fb_thread.finished_signal.connect(self.facebook_scraping_finished)
        self.fb_thread.start()
    def update_fb_log(self, message):
        self.fb_log.append(message)
    def facebook_scraping_finished(self):
        self.fb_log.append("Facebook scraping thread finished.")
        self.fb_progress_bar.setVisible(False)
        self.fb_start_button.setEnabled(True)


def _handle_cli_entry(argv=None):
    argv = argv or sys.argv
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--soundcloud-url", dest="soundcloud_url")
    parser.add_argument("--soundcloud-tags", nargs="*", dest="soundcloud_tags")
    parser.add_argument("--max-artists", type=int, dest="max_artists")
    parser.add_argument("--max-handles", type=int, dest="max_handles")
    parser.add_argument("--min-yield", type=int, dest="min_yield", default=3)
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--soundcloud-cli", action="store_true", dest="soundcloud_cli")
    args, remaining = parser.parse_known_args(argv[1:])
    cli_requested = bool(args.soundcloud_cli or args.soundcloud_url or (args.soundcloud_tags and len(args.soundcloud_tags) > 0))
    if cli_requested:
        scrape_soundcloud(
            (args.soundcloud_url or "").strip(),
            seed_tags=args.soundcloud_tags,
            existing_csv="artist_social_links.csv",
            max_artists=args.max_artists or 200,
            max_handles=args.max_handles,
            min_yield=args.min_yield if args.min_yield is not None else 3,
            dry_run=args.dry_run,
        )
        return True, [argv[0]] + remaining
    return False, [argv[0]] + remaining


if __name__ == "__main__":
    ran_cli, qt_args = _handle_cli_entry(sys.argv)
    if ran_cli:
        sys.exit(0)
    app = QtWidgets.QApplication(qt_args)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

# ---------------------------
# Stop caffeinate if it was started (macOS)
# ---------------------------
if caffeinate_proc:
    print("Stopping caffeinate. You may now allow sleep.")
    caffeinate_proc.kill()
