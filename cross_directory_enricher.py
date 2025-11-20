#!/usr/bin/env python3
"""
Spotify CSV post-processor that cross-pollinates existing directory CSVs and
performs light live searches to append socials / websites / emails to rows.
"""

from __future__ import annotations

import datetime
import math
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Sequence, Set, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PyQt5 import QtWidgets

LIVE_SEARCH_MAX_ATTEMPTS = 40  # 0 = no limit
MAX_LINK_HUB_HOPS_PER_ROW = 1

UNEARTHED_CSV = "unearthed_output.csv"
BANDCAMP_CSV = "bandcamp_output.csv"
SOUNDCLOUD_CSV = "soundcloud_output.csv"
LASTFM_CSV = "lastfm_output.csv"

HTTP_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/117.0.0.0 Safari/537.36"
)

LINK_HUB_HOSTS = {
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "lnk.bio",
}

SOCIAL_HOST_WHITELIST = (
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
)

LASTFM_BRAND_KEYWORDS = (
    "lastfm",
    "last.fm",
)

SOCIAL_PRIORITY = [
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
]

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

DIRECTORY_PRIORITY = ("bandcamp", "soundcloud", "lastfm", "unearthed")

SOURCE_PRIORITY = {
    "bandcamp": 0,
    "soundcloud": 1,
    "lastfm": 2,
    "unearthed": 3,
    "live_search": 4,
}

DIRECTORY_FILES = {
    "bandcamp": BANDCAMP_CSV,
    "soundcloud": SOUNDCLOUD_CSV,
    "lastfm": LASTFM_CSV,
    "unearthed": UNEARTHED_CSV,
}


@dataclass
class EnrichmentPayload:
    socials: Set[str] = field(default_factory=set)
    websites: Set[str] = field(default_factory=set)
    emails: Set[str] = field(default_factory=set)
    link_hubs: Set[str] = field(default_factory=set)
    source_dir: Optional[str] = None
    source_url: Optional[str] = None


def run_spotify_enricher_dialog(parent: Optional[QtWidgets.QWidget] = None) -> None:
    """Entry point called from the main UI."""
    app = QtWidgets.QApplication.instance()
    parent = parent or (app.activeWindow() if app else None)
    input_path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent,
        "Select Spotify Seed CSV",
        "",
        "CSV or TSV Files (*.csv *.tsv *.txt);;All Files (*)",
    )
    if not input_path:
        return
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(input_path)
    ext = ext or ".csv"
    output_path = f"{base}_enriched_{timestamp}{ext}"
    print(f"[Enricher] Starting with seed CSV {input_path}")
    try:
        enricher = SpotifyCSVEnricher(input_path, output_path)
        row_count = enricher.run()
    except Exception as exc:
        QtWidgets.QMessageBox.critical(
            parent,
            "Spotify CSV Enricher",
            f"Failed to run Spotify CSV Enricher:\n{exc}",
        )
        return
    QtWidgets.QMessageBox.information(
        parent,
        "Spotify CSV Enricher",
        f"Rows processed: {row_count}\nOutput saved to:\n{output_path}",
    )


class SpotifyCSVEnricher:
    def __init__(self, input_path: str, output_path: str) -> None:
        self.input_path = os.path.abspath(input_path)
        self.output_path = os.path.abspath(output_path)
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.session = self._build_session()
        self.live_search_attempts = 0
        self.directory_maps = self._load_directory_maps()

    def run(self) -> int:
        df = _read_csv_flexible(self.input_path)
        if df is None:
            raise RuntimeError("Spotify CSV could not be loaded.")
        row_count = len(df.index)
        print(f"[Enricher] Loaded {row_count} Spotify rows.")
        for col in ("Social Link", "External Links", "Email", "Source Directory", "Source URL"):
            if col not in df.columns:
                df[col] = ""
        try:
            for idx in df.index:
                row = df.loc[idx].to_dict()
                artist_name = _extract_artist_name(row)
                norm_name = _norm_name(artist_name)
                if not norm_name:
                    continue
                socials_current = _split_pipe_cell(row.get("Social Link"))
                sites_current = _split_pipe_cell(row.get("External Links"))
                emails_current = _split_pipe_cell(row.get("Email"), is_email=True)
                new_socials: Set[str] = set()
                new_sites: Set[str] = set()
                new_emails: Set[str] = set()
                link_hubs: Set[str] = set()
                dir_payload = self._enrich_from_directories(norm_name)
                if dir_payload:
                    new_socials |= dir_payload.socials
                    new_sites |= dir_payload.websites
                    new_emails |= dir_payload.emails
                    link_hubs |= dir_payload.link_hubs
                    if dir_payload.source_dir:
                        self._apply_source_choice(
                            df,
                            idx,
                            dir_payload.source_dir,
                            dir_payload.source_url or "",
                        )
                needs_live_lookup = (
                    not (socials_current or new_socials)
                    and not (emails_current or new_emails)
                )
                live_payload = None
                if needs_live_lookup:
                    live_payload = self._live_enrich(artist_name, row, norm_name)
                    if live_payload:
                        new_socials |= live_payload.socials
                        new_sites |= live_payload.websites
                        new_emails |= live_payload.emails
                        link_hubs |= live_payload.link_hubs
                        if live_payload.source_dir:
                            self._apply_source_choice(
                                df,
                                idx,
                                live_payload.source_dir,
                                live_payload.source_url or "",
                            )
                if link_hubs and MAX_LINK_HUB_HOPS_PER_ROW > 0:
                    added = self._scrape_link_hubs(link_hubs)
                    if added:
                        new_socials |= added
                socials_all = socials_current | new_socials
                sites_all = sites_current | new_sites
                emails_all = emails_current | new_emails
                if socials_all:
                    df.at[idx, "Social Link"] = " | ".join(
                        sorted(socials_all, key=_social_sort_key)
                    )
                if sites_all:
                    df.at[idx, "External Links"] = " | ".join(sorted(sites_all))
                if emails_all:
                    df.at[idx, "Email"] = " | ".join(sorted(emails_all))
        finally:
            try:
                self.session.close()
            except Exception:
                pass
        _ensure_parent_dir(self.output_path)
        df.to_csv(self.output_path, index=False, encoding="utf-8-sig")
        print(f"[Enricher] Enriched CSV written to {self.output_path}")
        return row_count

    def _build_session(self) -> requests.Session:
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

    def _load_directory_maps(self) -> Dict[str, Dict[str, dict]]:
        maps: Dict[str, Dict[str, dict]] = {}
        for source, filename in DIRECTORY_FILES.items():
            resolved = self._resolve_csv_path(filename)
            if not resolved or not os.path.exists(resolved):
                continue
            df = _read_csv_flexible(resolved)
            if df is None or df.empty:
                continue
            index: Dict[str, dict] = {}
            for _, row in df.iterrows():
                artist_name = _extract_artist_name(row)
                norm_name = _norm_name(artist_name)
                if not norm_name or norm_name in index:
                    continue
                index[norm_name] = row.to_dict()
            if index:
                maps[source] = index
                print(
                    f"[Enricher] Loaded {len(index)} entries from {source} ({resolved})."
                )
        return maps

    def _resolve_csv_path(self, filename: str) -> Optional[str]:
        if not filename:
            return None
        candidates = []
        if os.path.isabs(filename):
            candidates.append(filename)
        else:
            candidates.append(os.path.join(self.base_dir, filename))
            input_dir = os.path.dirname(self.input_path)
            candidates.append(os.path.join(input_dir, filename))
            candidates.append(os.path.abspath(filename))
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def _enrich_from_directories(self, norm_name: str) -> Optional[EnrichmentPayload]:
        payload = EnrichmentPayload()
        best_source: Optional[str] = None
        best_url: Optional[str] = None
        for source in DIRECTORY_PRIORITY:
            mapping = self.directory_maps.get(source)
            if not mapping:
                continue
            row = mapping.get(norm_name)
            if not row:
                continue
            socials, sites, emails, hubs = _extract_directory_fields(row)
            payload.socials.update(socials)
            payload.websites.update(sites)
            payload.emails.update(emails)
            payload.link_hubs.update(hubs)
            profile_url = _extract_profile_url(row)
            if profile_url:
                if best_source is None or SOURCE_PRIORITY[source] < SOURCE_PRIORITY.get(
                    best_source, 999
                ):
                    best_source = source
                    best_url = profile_url
        if not (
            payload.socials or payload.websites or payload.emails or payload.link_hubs
        ):
            return None
        payload.source_dir = best_source
        payload.source_url = best_url
        return payload

    def _live_enrich(
        self, artist_name: str, row: dict, norm_name: str
    ) -> Optional[EnrichmentPayload]:
        payload = None
        bandcamp_result = self._search_bandcamp(artist_name)
        if bandcamp_result:
            payload = bandcamp_result
        else:
            soundcloud_result = self._search_soundcloud(artist_name)
            if not soundcloud_result:
                cleaned = _normalise_for_soundcloud(artist_name)
                if cleaned and cleaned != artist_name:
                    soundcloud_result = self._search_soundcloud(cleaned)
            if soundcloud_result:
                payload = soundcloud_result
            else:
                lastfm_result = self._search_lastfm(artist_name)
                if lastfm_result:
                    payload = lastfm_result
        return payload

    def _search_bandcamp(self, artist_name: str) -> Optional[EnrichmentPayload]:
        search_url = (
            "https://bandcamp.com/search?item_type=b&q="
            + urllib.parse.quote_plus(artist_name)
        )
        html = self._fetch_search_page(search_url, "Bandcamp")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        profile_url = None
        for li in soup.select("li.searchresult"):
            classes = li.get("class", [])
            class_tokens = {token.lower() for token in classes}
            if class_tokens and "fan" in class_tokens:
                continue
            anchor = li.find("a", href=True)
            if not anchor:
                continue
            href = anchor["href"].strip()
            if "bandcamp.com" not in href:
                continue
            profile_url = href.split("?")[0]
            break
        if not profile_url:
            return None
        return self._fetch_profile_links(profile_url, "bandcamp")

    def _search_soundcloud(self, artist_name: str) -> Optional[EnrichmentPayload]:
        search_url = (
            "https://soundcloud.com/search/people?q="
            + urllib.parse.quote_plus(artist_name)
        )
        html = self._fetch_search_page(search_url, "SoundCloud")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        profile_url = None
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href:
                continue
            if href.startswith("/"):
                href = urllib.parse.urljoin("https://soundcloud.com", href)
            if "soundcloud.com" not in href:
                continue
            parsed = urllib.parse.urlparse(href)
            parts = parsed.path.strip("/").split("/")
            if not parts or parts[0] in ("search", ""):
                continue
            profile_url = urllib.parse.urlunparse(
                (
                    "https",
                    parsed.netloc,
                    "/" + parts[0],
                    "",
                    "",
                    "",
                )
            )
            break
        if not profile_url:
            return None
        return self._fetch_profile_links(profile_url, "soundcloud")

    def _search_lastfm(self, artist_name: str) -> Optional[EnrichmentPayload]:
        search_url = (
            "https://www.last.fm/search?q="
            + urllib.parse.quote_plus(artist_name)
            + "&type=artist"
        )
        html = self._fetch_search_page(search_url, "Last.fm")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        profile_url = None
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or "/music/" not in href:
                continue
            if href.startswith("/"):
                href = urllib.parse.urljoin("https://www.last.fm", href)
            if "last.fm" not in href:
                continue
            profile_url = href.split("?")[0]
            break
        if not profile_url:
            return None
        return self._fetch_profile_links(profile_url, "lastfm")

    def _fetch_profile_links(
        self, profile_url: str, source_dir: str
    ) -> Optional[EnrichmentPayload]:
        print(f"[Enricher] Fetching {source_dir} profile: {profile_url}")
        try:
            resp = self.session.get(profile_url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[Enricher] Failed to fetch {profile_url}: {exc}")
            return None
        socials, websites, emails, link_hubs = _extract_links_from_profile(
            resp.text, source_dir, profile_url
        )
        if not (socials or websites or emails):
            return None
        payload = EnrichmentPayload(
            socials=socials,
            websites=websites,
            emails=emails,
            link_hubs=link_hubs,
            source_dir=source_dir,
            source_url=profile_url,
        )
        return payload

    def _apply_source_choice(
        self, df: pd.DataFrame, idx, candidate_dir: str, candidate_url: str
    ) -> None:
        current = (df.at[idx, "Source Directory"] or "").strip().lower()
        current_priority = SOURCE_PRIORITY.get(current, 999)
        candidate_priority = SOURCE_PRIORITY.get(candidate_dir, 999)
        if not current or candidate_priority < current_priority:
            df.at[idx, "Source Directory"] = candidate_dir
            df.at[idx, "Source URL"] = candidate_url

    def _fetch_search_page(self, url: str, label: str) -> Optional[str]:
        if LIVE_SEARCH_MAX_ATTEMPTS > 0 and self.live_search_attempts >= LIVE_SEARCH_MAX_ATTEMPTS:
            print("[Enricher] Live search budget exhausted.")
            return None
        self.live_search_attempts += 1
        print(f"[Enricher] {label} live search: {url}")
        try:
            resp = self.session.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            print(f"[Enricher] {label} search failed: {exc}")
            return None

    def _scrape_link_hubs(self, hub_urls: Iterable[str]) -> Set[str]:
        socials: Set[str] = set()
        hops = 0
        for hub in hub_urls:
            if hops >= MAX_LINK_HUB_HOPS_PER_ROW:
                break
            hops += 1
            socials |= _scrape_link_hub_socials(self.session, hub)
        return socials


def _read_csv_flexible(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    last_exc = None
    for kwargs in ({"sep": None, "engine": "python"}, {}):
        try:
            return pd.read_csv(path, **kwargs)
        except Exception as exc:
            last_exc = exc
    print(f"[Enricher] Failed to read CSV {path}: {last_exc}")
    return None


def _ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def _extract_artist_name(row: dict) -> str:
    if isinstance(row, pd.Series):
        row = row.to_dict()
    for key in ("Artist Name", "artist_name", "Artist", "Name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = row.get("Spotify Artist") or row.get("Spotify_Artist")
    if isinstance(value, str):
        return value.strip()
    return ""


def _norm_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = name.strip().lower()
    if not name:
        return ""
    return " ".join(name.split())


def _normalise_for_soundcloud(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch.isspace())
    return " ".join(cleaned.split())


def _split_pipe_cell(value, is_email: bool = False) -> Set[str]:
    if value is None:
        return set()
    if isinstance(value, float) and math.isnan(value):
        return set()
    if not isinstance(value, str):
        value = str(value)
    parts = [part.strip() for part in value.split("|")]
    results: Set[str] = set()
    for part in parts:
        if not part:
            continue
        if is_email:
            results.add(part.lower())
        else:
            normalized = _normalise_url(part)
            if normalized:
                results.add(normalized)
    return results


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
    normalized = urllib.parse.urlunparse(
        (parsed.scheme, netloc, path, "", parsed.query, "")
    )
    return normalized


def _social_sort_key(url: str) -> Tuple[int, str]:
    host = ""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        host = ""
    for idx, domain in enumerate(SOCIAL_PRIORITY):
        if host.endswith(domain):
            return (idx, url)
    return (len(SOCIAL_PRIORITY), url)


def _extract_directory_fields(row: dict) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    socials: Set[str] = set()
    websites: Set[str] = set()
    emails: Set[str] = set()
    link_hubs: Set[str] = set()
    row_dict = row.to_dict() if isinstance(row, pd.Series) else row
    for column in DIRECTORY_SOCIAL_COLUMNS:
        value = row_dict.get(column)
        for item in _split_multi_value(value):
            normalized = _normalise_url(item)
            if not normalized:
                continue
            host = _host(normalized)
            if host in LINK_HUB_HOSTS:
                link_hubs.add(normalized)
                websites.add(normalized)
            elif any(host.endswith(dom) for dom in SOCIAL_HOST_WHITELIST):
                socials.add(normalized)
            else:
                websites.add(normalized)
    for column in DIRECTORY_WEBSITE_COLUMNS:
        value = row_dict.get(column)
        for item in _split_multi_value(value):
            normalized = _normalise_url(item)
            if not normalized:
                continue
            host = _host(normalized)
            if host in LINK_HUB_HOSTS:
                link_hubs.add(normalized)
            websites.add(normalized)
    email_val = row_dict.get("Email") or row_dict.get("Emails")
    for email in _split_multi_value(email_val):
        email_clean = email.strip().lower()
        if email_clean:
            emails.add(email_clean)
    return socials, websites, emails, link_hubs


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


def _extract_profile_url(row: dict) -> Optional[str]:
    data = row.to_dict() if isinstance(row, pd.Series) else row
    for column in PROFILE_URL_CANDIDATES:
        value = data.get(column)
        normalized = _normalise_url(value) if isinstance(value, str) else None
        if normalized:
            return normalized
    return None


def _extract_links_from_profile(
    html: str, source_dir: str, profile_url: str
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
        href = anchor.get("href", "")
        if not href:
            continue
        href = href.strip()
        if not href:
            continue
        if href.startswith("mailto:"):
            email = href[7:].split("?")[0].strip().lower()
            if email:
                emails.add(email)
            continue
        absolute = urllib.parse.urljoin(profile_url, href)
        normalized = _normalise_url(absolute)
        if not normalized:
            continue
        parsed = urllib.parse.urlparse(normalized)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.netloc.lower()
        if source_dir == "bandcamp" and host.endswith("bandcamp.com"):
            continue
        if source_dir == "soundcloud" and host.endswith("soundcloud.com"):
            continue
        if source_dir == "lastfm" and (
            host.endswith("last.fm") or host.endswith("lastfm.com") or "lastfm" in host
        ):
            continue
        path_lower = parsed.path.lower()
        if any(fragment in path_lower for fragment in PATH_NOISE):
            continue
        if host in LINK_HUB_HOSTS:
            link_hubs.add(normalized)
            websites.add(normalized)
        elif any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST):
            if source_dir == "lastfm" and any(
                keyword in normalized for keyword in LASTFM_BRAND_KEYWORDS
            ):
                continue
            socials.add(normalized)
        else:
            websites.add(normalized)
    return socials, websites, emails, link_hubs


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


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
        href = anchor["href"].strip()
        if not href:
            continue
        absolute = urllib.parse.urljoin(hub_url, href)
        normalized = _normalise_url(absolute)
        if not normalized:
            continue
        host = _host(normalized)
        if any(host.endswith(domain) for domain in SOCIAL_HOST_WHITELIST):
            socials.add(normalized)
    return socials
