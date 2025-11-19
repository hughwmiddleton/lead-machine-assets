from __future__ import annotations

from typing import Dict, Any
import os
import re
import unicodedata
import time
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PyQt5.QtCore import QThread, pyqtSignal

SOCIAL_DOMAINS = {
    "facebook": ("facebook.com", "m.facebook.com"),
    "instagram": ("instagram.com", "www.instagram.com"),
    "x": ("x.com", "twitter.com", "mobile.twitter.com"),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "linktree": ("linktr.ee",),
    "beacons": ("beacons.ai",),
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
    "unearthed": ("abc.net.au", "www.abc.net.au"),
    "spotify": ("spotify.com", "open.spotify.com", "i.scdn.co"),
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

MAX_WEBSITES = 2


def normalise_artist_name(name: str) -> str:
    if not name:
        return ""
    text = name.strip().lower()
    text = re.sub(r"^(the|a)\s+", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.strip()


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _load_directory_csv(path: str, source_name: str) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    if not path or not os.path.exists(path):
        return index
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        print(f"[Enricher] Failed to read CSV for {source_name}: {path}: {exc}")
        return index
    if "Artist Name" not in df.columns:
        print(f"[Enricher] CSV for {source_name} has no 'Artist Name' column: {path}")
        return index
    for _, row in df.iterrows():
        artist = _clean_cell(row.get("Artist Name"))
        if not artist:
            continue
        key = normalise_artist_name(artist)
        if not key:
            continue
        index[key] = row.to_dict()
    print(f"[Enricher] Loaded {len(index)} artists for {source_name} from {path}")
    return index


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
        enable_live_search: bool = False,
        max_live_searches: int = 40,
        parent=None,
    ):
        super().__init__(parent)
        self.seed_csv_path = seed_csv_path
        self.output_csv_path = output_csv_path
        self.bandcamp_csv_path = bandcamp_csv_path
        self.soundcloud_csv_path = soundcloud_csv_path
        self.unearthed_csv_path = unearthed_csv_path
        self.lastfm_csv_path = lastfm_csv_path
        self.enable_live_search = enable_live_search
        self.max_live_searches = max_live_searches

    def run(self):
        try:
            self._run_impl()
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Error: {exc}")
            self.finished.emit("")

    def _run_impl(self):
        if not os.path.exists(self.seed_csv_path):
            self.log_message.emit(f"[Enricher] Seed CSV not found: {self.seed_csv_path}")
            self.finished.emit("")
            return
        self.log_message.emit(f"[Enricher] Loading seed CSV: {self.seed_csv_path}")
        try:
            seed_df = pd.read_csv(self.seed_csv_path)
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Failed to read seed CSV: {exc}")
            self.finished.emit("")
            return
        if "Artist Name" not in seed_df.columns:
            self.log_message.emit("[Enricher] Seed CSV is missing 'Artist Name' column; aborting.")
            self.finished.emit("")
            return
        directory_indexes: Dict[str, Dict[str, Dict[str, Any]]] = {}
        if self.unearthed_csv_path:
            directory_indexes["unearthed"] = _load_directory_csv(self.unearthed_csv_path, "Unearthed")
        if self.bandcamp_csv_path:
            directory_indexes["bandcamp"] = _load_directory_csv(self.bandcamp_csv_path, "Bandcamp")
        if self.soundcloud_csv_path:
            directory_indexes["soundcloud"] = _load_directory_csv(self.soundcloud_csv_path, "SoundCloud")
        if self.lastfm_csv_path:
            directory_indexes["lastfm"] = _load_directory_csv(self.lastfm_csv_path, "Last.fm")
        total = len(seed_df)
        if total == 0:
            self.log_message.emit("[Enricher] Seed CSV has no rows; nothing to do.")
            self.finished.emit("")
            return
        for column in ["Social Link", "External Links", "Email", "Source Directory", "Source URL"]:
            if column not in seed_df.columns:
                seed_df[column] = ""
        self.log_message.emit(f"[Enricher] Starting enrichment for {total} rows...")
        if self.enable_live_search:
            self.log_message.emit(
                "[Enricher] Live search enabled for Bandcamp/SoundCloud/Last.fm; "
                "Unearthed remains CSV-only."
            )
        priority = ["unearthed", "bandcamp", "soundcloud", "lastfm"]
        live_search_attempts = 0
        live_limit = self.max_live_searches if self.max_live_searches > 0 else None
        live_limit_notified = False
        for position, (row_idx, row) in enumerate(seed_df.iterrows(), start=1):
            artist = _clean_cell(row.get("Artist Name"))
            key = normalise_artist_name(artist)
            if not key:
                self.log_message.emit(f"[Enricher] Row {position}/{total}: empty or invalid artist name; skipping.")
                self._update_progress(position, total)
                continue
            existing_social = _clean_cell(row.get("Social Link"))
            existing_email = _clean_cell(row.get("Email"))
            existing_external = _clean_cell(row.get("External Links"))
            if existing_social or existing_email or existing_external:
                self.log_message.emit(
                    f"[Enricher] Row {position}/{total}: {artist or 'Unknown'} already has socials/email; skipping."
                )
                self._update_progress(position, total)
                continue
            enriched = False
            for source in priority:
                idx = directory_indexes.get(source) or {}
                match = idx.get(key)
                if not match:
                    continue
                m_social = _clean_cell(match.get("Social Link"))
                m_email = _clean_cell(match.get("Email"))
                m_external = _clean_cell(match.get("External Links"))
                source_url = ""
                for candidate_col in ["SoundCloud Link", "External Links", "Profile URL", "URL", "Website"]:
                    if candidate_col in match:
                        candidate_val = _clean_cell(match.get(candidate_col))
                        if candidate_val:
                            source_url = candidate_val
                            break
                if not source_url and m_external:
                    source_url = m_external
                if any([m_social, m_email, m_external, source_url]):
                    if not existing_social and m_social:
                        seed_df.at[row_idx, "Social Link"] = m_social
                    if not existing_email and m_email:
                        seed_df.at[row_idx, "Email"] = m_email
                    if not existing_external and m_external:
                        seed_df.at[row_idx, "External Links"] = m_external
                    seed_df.at[row_idx, "Source Directory"] = source
                    seed_df.at[row_idx, "Source URL"] = source_url
                    enriched = True
                    self.log_message.emit(
                        f"[Enricher] Row {position}/{total}: matched {artist!r} via {source}, "
                        f"social={bool(m_social)}, email={bool(m_email)}, url={source_url or 'None'}"
                    )
                    break
            if not enriched:
                can_live_search = self.enable_live_search and (
                    live_limit is None or live_search_attempts < live_limit
                )
                if can_live_search:
                    live_search_attempts += 1
                    success = self._live_search_and_enrich(
                        seed_df,
                        row_idx,
                        artist or "",
                        key,
                        directory_indexes,
                    )
                    if success:
                        enriched = True
                    if (
                        live_limit is not None
                        and live_search_attempts >= live_limit
                        and not live_limit_notified
                    ):
                        self.log_message.emit(
                            "[Enricher] Live search limit reached; remaining rows will skip live lookup."
                        )
                        live_limit_notified = True
                if not enriched:
                    self.log_message.emit(
                        f"[Enricher] Row {position}/{total}: no match for {artist!r} in directory CSVs."
                    )
            self._update_progress(position, total)
        out_path = self.output_csv_path
        out_dir = os.path.dirname(out_path) or "."
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Failed to create output directory {out_dir}: {exc}")
            self.finished.emit("")
            return
        try:
            seed_df.to_csv(out_path, index=False)
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Failed to write enriched CSV: {exc}")
            self.finished.emit("")
            return
        self.log_message.emit(f"[Enricher] Enriched CSV written to {out_path}")
        # TODO: Optional phase 2 – attempt live directory lookups when CSVs miss a match.
        self.finished.emit(out_path)

    def _update_progress(self, current: int, total: int):
        pct = int((current / max(1, total)) * 100)
        self.progress.emit(pct)

    @staticmethod
    def _social_sort_key(url: str):
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        for idx, domain in enumerate(SOCIAL_PRIORITY):
            if host.endswith(domain):
                return (idx, url)
        return (len(SOCIAL_PRIORITY), url)

    def _live_search_and_enrich(
        self,
        df: pd.DataFrame,
        row_idx,
        artist_name: str,
        key: str,
        directory_indexes: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> bool:
        loaded_sources = ", ".join(sorted(directory_indexes.keys())) or "none"
        self.log_message.emit(
            f"[Enricher] Live search: attempting online lookup for {artist_name!r} "
            f"(normalized={key!r}, CSV sources={loaded_sources})..."
        )
        # Unearthed lacks a public name-search endpoint, so we only probe
        # directories that can be queried reliably.
        for source in ["bandcamp", "soundcloud", "lastfm"]:
            try:
                if source == "bandcamp":
                    success = self._live_search_bandcamp(df, row_idx, artist_name)
                elif source == "soundcloud":
                    success = self._live_search_soundcloud(df, row_idx, artist_name)
                elif source == "lastfm":
                    success = self._live_search_lastfm(df, row_idx, artist_name)
                else:
                    success = False
                if success:
                    return True
                time.sleep(1.0)
            except Exception as exc:
                self.log_message.emit(f"[Enricher] Live search error for {source} ({artist_name!r}): {exc}")
        self.log_message.emit(f"[Enricher] Live search: no match found online for {artist_name!r}.")
        return False

    def _live_search_bandcamp(self, df, row_idx, artist_name: str) -> bool:
        quoted = requests.utils.quote(artist_name)
        url = f"https://bandcamp.com/search?q={quoted}&item_type=b"
        self.log_message.emit(f"[Enricher] Bandcamp live search: {url}")
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Bandcamp search request failed: {exc}")
            return False
        soup = BeautifulSoup(resp.text, "html.parser")
        first_link = soup.select_one("li.searchresult a.itemurl, li.searchresult a[href*='bandcamp.com']")
        if not first_link:
            self.log_message.emit("[Enricher] Bandcamp search: no results found.")
            return False
        profile_url = (first_link.get("href") or "").strip()
        if not profile_url:
            self.log_message.emit("[Enricher] Bandcamp search: first result has no href.")
            return False
        self.log_message.emit(f"[Enricher] Bandcamp search: candidate profile {profile_url}")
        return self._fetch_profile_and_apply(df, row_idx, "bandcamp", profile_url)

    def _live_search_soundcloud(self, df, row_idx, artist_name: str) -> bool:
        quoted = requests.utils.quote(artist_name)
        url = f"https://soundcloud.com/search/people?q={quoted}"
        self.log_message.emit(f"[Enricher] SoundCloud live search: {url}")
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as exc:
            self.log_message.emit(f"[Enricher] SoundCloud search request failed: {exc}")
            return False
        soup = BeautifulSoup(resp.text, "html.parser")
        first_link = soup.select_one("a.userBadge__title, a[href^='https://soundcloud.com/']")
        if not first_link:
            self.log_message.emit("[Enricher] SoundCloud search: no results found.")
            return False
        profile_url = (first_link.get("href") or "").strip()
        if not profile_url.startswith("http"):
            profile_url = f"https://soundcloud.com{profile_url}"
        self.log_message.emit(f"[Enricher] SoundCloud search: candidate profile {profile_url}")
        return self._fetch_profile_and_apply(df, row_idx, "soundcloud", profile_url)

    def _live_search_unearthed(self, df, row_idx, artist_name: str) -> bool:
        """
        Unearthed live search is intentionally disabled. The CSV index produced
        by the core Unearthed scraper remains available in Phase 1.
        """
        self.log_message.emit("[Enricher] Unearthed live search disabled; skipping.")
        return False

    def _live_search_lastfm(self, df, row_idx, artist_name: str) -> bool:
        quoted = requests.utils.quote(artist_name)
        url = f"https://www.last.fm/search?q={quoted}&type=artist"
        self.log_message.emit(f"[Enricher] Last.fm live search: {url}")
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Last.fm search request failed: {exc}")
            return False
        soup = BeautifulSoup(resp.text, "html.parser")
        first_link = soup.select_one("a[href*='/music/']")
        if not first_link:
            self.log_message.emit("[Enricher] Last.fm search: no artist results found.")
            return False
        profile_url = (first_link.get("href") or "").strip()
        if profile_url.startswith("/"):
            profile_url = f"https://www.last.fm{profile_url}"
        self.log_message.emit(f"[Enricher] Last.fm search: candidate profile {profile_url}")
        return self._fetch_profile_and_apply(df, row_idx, "lastfm", profile_url)

    def _fetch_profile_and_apply(self, df, row_idx, source_name: str, profile_url: str) -> bool:
        self.log_message.emit(f"[Enricher] Fetching {source_name} profile: {profile_url}")
        try:
            resp = requests.get(profile_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as exc:
            self.log_message.emit(f"[Enricher] Profile request failed for {source_name}: {exc}")
            return False
        soup = BeautifulSoup(resp.text, "html.parser")
        anchors = soup.select("a[href]")
        social_candidates: Dict[str, list] = {key: [] for key in SOCIAL_DOMAINS.keys()}
        social_seen: Dict[str, set] = {key: set() for key in SOCIAL_DOMAINS.keys()}
        websites: list[str] = []
        website_seen: set[str] = set()
        emails: list[str] = []
        email_seen: set[str] = set()
        platform_hosts = PLATFORM_HOSTS.get(source_name, ())
        lastfm_brand_markers = ("lastfm", "last.fm", "last_fm")
        static_tokens = (".jpg", ".jpeg", ".png", ".gif", ".webp", "/img/", "/image/", "/static/", "/assets/")
        skip_paths = ("/login", "/signup", "/help", "/support", "/download", "/about")

        for anchor in anchors:
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            lower = href.lower()
            if lower.startswith("mailto:"):
                addr = href.split("mailto:", 1)[1].split("?", 1)[0].strip()
                if addr and addr not in email_seen:
                    emails.append(addr)
                    email_seen.add(addr)
                continue
            if lower.startswith("#") or lower.startswith("javascript:"):
                continue
            parsed = urlparse(href)
            scheme = (parsed.scheme or "").lower()
            if scheme not in ("http", "https"):
                continue
            host = (parsed.netloc or "").split("@")[-1]
            host = host.split(":")[0].lower()
            if not host:
                continue
            path = (parsed.path or "").lower()
            handled_social = False
            for key, domains in SOCIAL_DOMAINS.items():
                if any(host.endswith(domain) for domain in domains):
                    if source_name == "lastfm":
                        if any(marker in lower for marker in lastfm_brand_markers):
                            handled_social = True
                            break
                    if href not in social_seen[key]:
                        social_candidates[key].append(href)
                        social_seen[key].add(href)
                    handled_social = True
                    break
            if handled_social:
                continue
            if any(host.endswith(ph) for ph in platform_hosts):
                continue
            if host in JUNK_WEBSITE_HOSTS:
                continue
            if source_name == "lastfm":
                if any(marker in lower for marker in lastfm_brand_markers):
                    continue
            if any(token in path for token in static_tokens):
                continue
            if any(keyword in path for keyword in JUNK_WEBSITE_PATH_KEYWORDS):
                continue
            if any(path.endswith(token) or token in path for token in skip_paths):
                continue
            if href not in website_seen:
                websites.append(href)
                website_seen.add(href)

        all_socials: list[str] = []
        seen_socials: set[str] = set()
        for values in social_candidates.values():
            for item in values:
                if item not in seen_socials:
                    seen_socials.add(item)
                    all_socials.append(item)
        ordered_socials = sorted(all_socials, key=self._social_sort_key)

        if not ordered_socials and not websites and not emails:
            self.log_message.emit(f"[Enricher] No useful socials/website/email found on {profile_url}")
            return False

        existing_social = _clean_cell(df.at[row_idx, "Social Link"])
        existing_external = _clean_cell(df.at[row_idx, "External Links"])
        existing_email = _clean_cell(df.at[row_idx, "Email"])

        if ordered_socials and not existing_social:
            df.at[row_idx, "Social Link"] = " | ".join(ordered_socials)
        if websites and not existing_external:
            website_list = websites
            if MAX_WEBSITES:
                website_list = website_list[:MAX_WEBSITES]
            df.at[row_idx, "External Links"] = " | ".join(website_list)
        if emails and not existing_email:
            df.at[row_idx, "Email"] = emails[0]

        df.at[row_idx, "Source Directory"] = source_name
        df.at[row_idx, "Source URL"] = profile_url
        self.log_message.emit(
            f"[Enricher] Live search success ({source_name}): "
            f"socials={len(ordered_socials)}, websites={len(websites)}, emails={len(emails)}"
        )
        return True
