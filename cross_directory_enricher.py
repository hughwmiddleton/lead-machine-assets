#!/usr/bin/env python3
"""Spotify CSV post-processor that can reuse directory CSVs or perform live lookups."""

from __future__ import annotations

import datetime
import math
import os
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Set, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, pyqtSignal

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

LINK_HUB_HOSTS = {
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "lnk.bio",
}

SOCIAL_PRIORITY = [
    "facebook.com",
    "m.facebook.com",
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

DIRECTORY_SOCIAL_COLUMNS = (
    "Social Link",
    "Instagram",
    "Facebook",
    "Twitter",
    "X",
    "TikTok",
    "Youtube",
    "YouTube",
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


@dataclass
class EnrichmentPayload:
    socials: Set[str] = field(default_factory=set)
    websites: Set[str] = field(default_factory=set)
    emails: Set[str] = field(default_factory=set)
    link_hubs: Set[str] = field(default_factory=set)
    source_dir: Optional[str] = None
    source_url: Optional[str] = None
    source_detail: Optional[str] = None


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
    if delimiter:
        parts = value.split(delimiter)
    else:
        parts = re.split(r"[|;,]", value)
    return [part.strip() for part in parts if part.strip()]


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
            if normalised:
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


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _extract_profile_url(row: dict) -> Optional[str]:
    for column in PROFILE_URL_CANDIDATES:
        value = row.get(column)
        if not isinstance(value, str):
            continue
        normalised = _normalise_url(value)
        if normalised:
            return normalised
    return None


def _extract_directory_fields(row: dict) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    socials: Set[str] = set()
    websites: Set[str] = set()
    emails: Set[str] = set()
    link_hubs: Set[str] = set()
    for column in DIRECTORY_SOCIAL_COLUMNS:
        value = row.get(column)
        for item in _split_multi_value(value):
            normalised = _normalise_url(item)
            if not normalised:
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
            if not normalised:
                continue
            host = _host(normalised)
            if host in LINK_HUB_HOSTS:
                link_hubs.add(normalised)
            websites.add(normalised)
    email_val = row.get("Email") or row.get("Emails") or row.get("email")
    for email in _split_multi_value(email_val):
        cleaned = email.strip().lower()
        if cleaned:
            emails.add(cleaned)
    return socials, websites, emails, link_hubs


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
        if not normalised:
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
        if not normalised:
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


def _load_directory_csv(path: str, source_name: str) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
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
        if not key or key in index:
            continue
        index[key] = row.to_dict()
    print(f"[Enricher] Loaded {len(index)} artists for {source_name} from {path}")
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
        for column in ["Social Link", "External Links", "Email", "Source Directory", "Source URL"]:
            if column not in seed_df.columns:
                seed_df[column] = ""
        directory_indexes: Dict[str, Dict[str, Dict[str, Any]]] = {}
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
            if not key:
                self.log_message.emit(
                    f"[Enricher] Row {position}/{total}: invalid artist name; skipping."
                )
                self._update_progress(position, total)
                continue
            enriched = False
            for source in priority:
                mapping = directory_indexes.get(source) or {}
                match = mapping.get(key)
                if not match:
                    continue
                payload = self._payload_from_directory_row(match, source)
                if not payload:
                    continue
                self._apply_payload(seed_df, row_idx, payload)
                enriched = True
                self.log_message.emit(
                    f"[Enricher] Row {position}/{total}: matched {artist!r} via {source}."
                )
                break
            if not enriched and self.enable_live_search:
                payload = self._live_lookup(artist)
                if payload:
                    self._apply_payload(seed_df, row_idx, payload)
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

    def _payload_from_directory_row(self, row: Dict[str, Any], source: str) -> Optional[EnrichmentPayload]:
        socials, websites, emails, link_hubs = _extract_directory_fields(row)
        if not (socials or websites or emails or link_hubs):
            return None
        payload = EnrichmentPayload(
            socials=socials,
            websites=websites,
            emails=emails,
            link_hubs=link_hubs,
            source_dir=source,
            source_url=_extract_profile_url(row),
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
            df.at[row_idx, "Social Link"] = MULTI_VALUE_SEPARATOR.join(
                sorted(socials_all, key=_social_sort_key)
            )
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
        quoted = urllib.parse.quote_plus(artist_name)
        url = f"https://soundcloud.com/search/people?q={quoted}"
        if not self._increment_live_counter():
            return None
        self.log_message.emit(f"[Enricher] SoundCloud live search: {url}")
        html = self._fetch_url(url, label="SoundCloud search")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        first_link = soup.select_one("a.userBadge__title, a[href^='https://soundcloud.com/']")
        if not first_link:
            self.log_message.emit("[Enricher] SoundCloud search: no results found.")
            return None
        profile_url = (first_link.get("href") or "").strip()
        if not profile_url:
            self.log_message.emit("[Enricher] SoundCloud search: result missing href.")
            return None
        if not profile_url.startswith("http"):
            profile_url = f"https://soundcloud.com{profile_url}"
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
