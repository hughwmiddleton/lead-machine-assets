#!/usr/bin/env python3
"""Spotify CSV post-processor that can reuse directory CSVs or perform live lookups."""

from __future__ import annotations

import copy
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
from contextlib import contextmanager
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
    canonicalize_bandcamp_url as _shared_canonicalize_bandcamp_url,
    canonicalize_facebook_url,
    ensure_canonical_facebook_url,
    is_spotify_origin_row,
    normalize_identity_url as _shared_normalize_identity_url,
    promote_facebook_url,
    soundcloud_handle_from_profile_url as _shared_soundcloud_handle_from_profile_url,
)
from musicbrainz_relationship_bridge import (
    KNOWN_PROFILE_ACCEPTED,
    KNOWN_PROFILE_CHALLENGE_UNAVAILABLE,
    KNOWN_PROFILE_ERROR,
    KNOWN_PROFILE_IDENTITY_REJECTED,
    KnownProfileFetchResult,
    build_relationship_bridge_plan,
    musicbrainz_relationship_bridge_enabled,
)
import html_fetcher
from html_fetcher import fetch_html, _detect_soft_block
from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from rapidfuzz import fuzz
from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, pyqtSignal
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, parse_qs, unquote
from unidecode import unidecode
from email_normalizer import (
    filter_platform_support_emails,
    filter_system_telemetry_emails,
    is_obvious_placeholder_email,
    normalize_email_value,
    normalize_obfuscated_email_patterns,
)
from email_provenance import (
    EMAIL_PROVENANCE_JSON_COL,
    _set_email_with_provenance,
    merge_email_provenance_into_target,
    row_has_successful_source_url_provenance,
)
from progress_state import init_progress, update_progress
from fb_attribution import (
    FB_ATTEMPT_STATE_COL,
    FB_DEBUG_REASON_COL,
    FB_EXTRACT_STATE_COL,
    FB_GATE_STATE_COL,
    FB_OPPORTUNITY_STATE_COL,
    FB_TERMINAL_REASON_COL,
    FB_WRITE_STATE_COL,
    IG_ATTEMPT_STATE_COL,
    IG_EXECUTION_PATH_COL,
    IG_EXTRACT_STATE_COL,
    IG_NORMALIZED_TERMINAL_OUTCOME_COL,
    IG_NORMALIZED_TERMINAL_REASON_COL,
    IG_OPPORTUNITY_STATE_COL,
    IG_SURFACE_REASON_COL,
    IG_TERMINAL_REASON_COL,
    IG_WRITE_STATE_COL,
    apply_fb_opportunity_state_df,
    ensure_fb_attribution_columns,
    ensure_ig_attribution_columns,
    finalize_ig_normalized_terminal,
)

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
    FacebookAcceptedPageFetchResult,
    FacebookDriverError,
    NightFBRunState,
    NightModeFacebookEnricher,
    _build_fb_discovery_query,
    explicit_fb_entrypoint_present_for_row,
    fb_share_runtime_fallback_urls_for_row,
    classify_explicit_fb_intake,
    explicit_fb_entrypoint_urls_for_row,
    _extract_emails_from_html,
    _log_fb_email_surface_debug,
    _extract_fb_visible_text_with_container_fallback,
    _guard_homepage_fb_search_candidates,
    _fb_search_surface_miss_reason,
    _is_fb_login_or_security_url,
    _looks_like_fb_warning_or_block,
    _merge_email_all,
    _normalise_fb_url,
    _run_bounded_fb_accepted_page_sweep,
    _run_fb_homepage_search,
    disable_night_fb_run_state,
    ensure_night_fb_run_session,
    normalize_night_fb_session_source,
    probe_night_fb_session_decision,
    reset_night_fb_run_runtime_state,
    update_night_fb_run_state,
)

NIGHT_RUNTIME_RESET_INTERVAL_ROWS_DEFAULT = 50
NIGHT_RUNTIME_CANARY_TIMEOUT_S = 1.0
NIGHT_RUNTIME_CANARY_IG_URL = "https://www.instagram.com/"


class NightRuntimeCanaryFailure(RuntimeError):
    """Raised when a post-reset runtime canary cannot prove runtime usability."""

    def __init__(self, source: str, reason: str, attempt_index: int):
        self.source = str(source or "").strip().lower() or "unknown"
        self.reason = str(reason or "").strip().lower() or "unknown_failure"
        self.attempt_index = int(attempt_index or 0)
        super().__init__(
            f"post-reset runtime canary failed source={self.source} "
            f"reason={self.reason} attempt={self.attempt_index}"
        )


ENRICHMENT_YIELD_SOURCE_ALIASES = {
    "website_enrich": "website",
    "website": "website",
    "facebook_enrich": "facebook",
    "facebook": "facebook",
    "domain_reuse": "domain_reuse",
}

DOMAIN_PROFILE_ARTISTS_SAMPLE_MAX = 5
DOMAIN_PROFILE_MANAGEMENT_LOCAL_PARTS = frozenset({"mgmt", "management", "manager"})
DOMAIN_PROFILE_BOOKING_LOCAL_PARTS = frozenset({"booking", "bookings", "agent", "agents"})
DOMAIN_PROFILE_LABEL_LOCAL_PARTS = frozenset({"demo", "demos", "releases", "label"})
DOMAIN_PROFILE_STRONG_LABEL_PATTERNS = (
    r"\blabel\b",
    r"\bdemos?\b",
    r"\breleases?\b",
    r"\brecords?\b",
    r"\brecordings\b",
)
DOMAIN_REUSE_ROLE_PRIORITY = {
    "management": 0,
    "booking": 1,
    "press": 2,
    "label_related": 3,
    "general": 4,
}
DOMAIN_REUSE_SOURCE_PRIORITY = {
    "website_enrich": 0,
    "facebook_enrich": 1,
    "instagram_enrich": 2,
    "soundcloud": 3,
    "soundcloud_live": 3,
    "bandcamp": 4,
    "bandcamp_live": 4,
    "lastfm": 5,
    "lastfm_live": 5,
    "live_search": 6,
}
DOMAIN_PROFILE_CONTACT_META_KEY = "_contact_meta"
DOMAIN_ORG_SIDECAR_COLUMNS: Tuple[str, ...] = (
    "domain",
    "org_type",
    "artist_count",
    "primary_email",
    "emails",
    "roles_seen",
    "sources_seen",
)
FB_DISCOVERY_ATTEMPT_FLAG_COL = "__fb_discovery_attempted_this_run"
FB_DISCOVERY_ATTEMPT_FLAG_ALIASES: Tuple[str, ...] = (
    FB_DISCOVERY_ATTEMPT_FLAG_COL,
    "__fb_discovery_attempted",
    "fb_discovery_attempted",
)


@dataclass
class EnrichmentYieldTracker:
    counts: Dict[str, int] = field(default_factory=dict)
    _seen_events: Set[Tuple[str, str]] = field(default_factory=set)

    @staticmethod
    def _emails(row_like: Any) -> Set[str]:
        if row_like is None or not hasattr(row_like, "get"):
            return set()
        try:
            from pipeline_runner import normalize_emails as _normalize_emails
        except Exception:
            _normalize_emails = None

        def _fallback_normalize(value: Any) -> List[str]:
            text = "" if value is None else str(value)
            parts = re.split(r"[\s,;]+", text)
            normalized: List[str] = []
            seen: Set[str] = set()
            for part in parts:
                email = part.strip().lower()
                if not email or "@" not in email or "." not in email.split("@", 1)[-1]:
                    continue
                if email in seen:
                    continue
                seen.add(email)
                normalized.append(email)
            return normalized

        normalize_fn = _normalize_emails or _fallback_normalize
        emails: Set[str] = set()
        for column in ("Email", "Email_All"):
            try:
                raw_value = row_like.get(column, "")
            except Exception:
                raw_value = ""
            for email in normalize_fn(raw_value):
                emails.add(email)
        return emails

    @staticmethod
    def _canonical_source(source_name: Any) -> str:
        source = _clean_cell(source_name).lower()
        if not source:
            return ""
        return ENRICHMENT_YIELD_SOURCE_ALIASES.get(source, source)

    def record_transition(self, row_idx: Any, before_row: Any, after_row: Any, source_name: Any) -> bool:
        source = self._canonical_source(source_name)
        if not source:
            return False
        if self._emails(before_row):
            return False
        if not self._emails(after_row):
            return False

        event_key = (source, str(row_idx))
        if event_key in self._seen_events:
            return False

        self._seen_events.add(event_key)
        self.counts[source] = self.counts.get(source, 0) + 1
        return True


@dataclass
class ChunkYieldSourceRowState:
    opportunity: bool = False
    attempt_seams: Set[str] = field(default_factory=set)
    email_found: bool = False
    email_written: bool = False

    @property
    def attempted(self) -> bool:
        return bool(self.attempt_seams)


@dataclass
class ChunkYieldWindow:
    chunk_index: int
    row_ids: List[Any]
    configured_interval: int
    source_rows: Dict[str, Dict[Any, ChunkYieldSourceRowState]] = field(
        default_factory=lambda: {"facebook": {}, "instagram": {}}
    )

    @property
    def row_start_index(self) -> Any:
        return self.row_ids[0] if self.row_ids else "<none>"

    @property
    def row_end_index(self) -> Any:
        return self.row_ids[-1] if self.row_ids else "<none>"

    @property
    def rows_in_chunk(self) -> int:
        return len(self.row_ids)

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
SPOTIFY_BC_RECOVERY_SUFFIXES: Tuple[str, ...] = ("music", "band", "official", "au")
SPOTIFY_BC_RECOVERY_MAX_SLUGS = 6

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
    "campsite.bio",
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

WEBSITE_ENRICH_PLATFORM_HOSTS = (
    "open.spotify.com",
    "spotify.com",
    "music.apple.com",
    "deezer.com",
)

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
_FB_DRIVER_PROFILE_DIR = ""
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


def enricher_fb_profile_has_cookies(profile_dir: Optional[str] = None) -> bool:
    target_profile = profile_dir or ENRICHER_FB_PROFILE
    return os.path.exists(os.path.join(target_profile, "Default", "Cookies"))


def persistent_fb_driver(profile_dir: Optional[str] = None):
    target_profile = profile_dir or ENRICHER_FB_PROFILE
    chrome_options = Options()
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.page_load_strategy = "eager"
    chrome_options.add_argument(f"--user-data-dir={target_profile}")
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


def _get_enricher_facebook_driver(profile_dir: Optional[str] = None):
    """
    Lazily initialize and return a shared Selenium Chrome driver for Facebook enrichment.
    Uses a persistent user-data-dir so login persists across runs.
    """
    global _FB_DRIVER, _FB_DRIVER_PROFILE_DIR
    target_profile = profile_dir or ENRICHER_FB_PROFILE
    with _FB_DRIVER_LOCK:
        if _FB_DRIVER is not None and _FB_DRIVER_PROFILE_DIR and _FB_DRIVER_PROFILE_DIR != target_profile:
            try:
                _FB_DRIVER.quit()
            except Exception:
                pass
            _FB_DRIVER = None
            _FB_DRIVER_PROFILE_DIR = ""
        if _FB_DRIVER is not None:
            return _FB_DRIVER
        os.makedirs(target_profile, exist_ok=True)
        _FB_DRIVER = persistent_fb_driver(target_profile)
        _FB_DRIVER_PROFILE_DIR = target_profile
        return _FB_DRIVER


def _cleanup_enricher_facebook_driver():
    """Safely close the Enricher's shared Facebook driver, if one was created."""
    global _FB_DRIVER, _FB_DRIVER_PROFILE_DIR
    with _FB_DRIVER_LOCK:
        if _FB_DRIVER is not None:
            try:
                _FB_DRIVER.quit()
            except Exception:
                pass
            _FB_DRIVER = None
            _FB_DRIVER_PROFILE_DIR = ""


def _fb_exception_is_fatal_session(exc: Exception) -> bool:
    if isinstance(exc, InvalidSessionIdException):
        return True
    message = cell_to_str(exc).lower()
    fatal_tokens = (
        "invalid session id",
        "disconnected",
        "not connected to devtools",
        "chrome not reachable",
        "target window already closed",
        "no such window",
        "web view not found",
        "session deleted because of page crash",
        "browser has disconnected",
    )
    return any(token in message for token in fatal_tokens)


def _fb_driver_has_auth_cookie(driver) -> Optional[bool]:
    """
    Best-effort c_user probe that avoids navigating to Facebook.
    Returns True when authenticated state is confirmed, False when the cookie
    is definitely absent, and None when the driver cannot report cookie state.
    """
    if driver is None:
        return False
    try:
        cookie = driver.get_cookie("c_user")
        if cookie:
            return True
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            raise
    try:
        payload = driver.execute_cdp_cmd("Storage.getCookies", {})
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            raise
        return None
    cookies = payload.get("cookies", []) if isinstance(payload, dict) else []
    for cookie in cookies:
        if cell_to_str(cookie.get("name")) != "c_user":
            continue
        domain = cell_to_str(cookie.get("domain")).lower().lstrip(".")
        if domain == "facebook.com" or domain.endswith(".facebook.com"):
            return True
    return False


def _classify_fb_auth_surface(current_url: str, page_source: str) -> str:
    current_url = cell_to_str(current_url).lower()
    page_source = cell_to_str(page_source).lower()
    if _is_fb_login_or_security_url(current_url):
        if "checkpoint" in current_url:
            return "checkpoint"
        if "security" in current_url:
            return "security"
        return "redirect_login"
    for token, reason in (
        ("checkpoint", "checkpoint"),
        ("consent", "consent"),
        ("recover", "recover"),
        ("two_factor", "two_factor"),
        ("two-factor", "two_factor"),
        ("mfa", "two_factor"),
        ("login", "redirect_login"),
        ("register", "redirect_login"),
    ):
        if token in current_url:
            return reason
    for token, reason in (
        ("checkpoint", "checkpoint"),
        ("consent", "consent"),
        ("security check", "checkpoint"),
        ("two-factor", "two_factor"),
        ("captcha", "captcha"),
    ):
        if token in page_source:
            return reason
    if "log in" in page_source and "facebook" in page_source:
        return "redirect_login"
    return ""


def _probe_fb_session_state(driver, *, visit_home: bool) -> Tuple[bool, str]:
    if driver is None:
        return False, "no_driver"
    try:
        if visit_home:
            driver.get("https://www.facebook.com/")
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            return False, "session_invalid"
        return False, "driver_error"
    try:
        current_url = getattr(driver, "current_url", "") or ""
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            return False, "session_invalid"
        current_url = ""
    try:
        page_source = getattr(driver, "page_source", "") or ""
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            return False, "session_invalid"
        page_source = ""
    reason = _classify_fb_auth_surface(current_url, page_source)
    if reason:
        return False, reason
    try:
        if driver.get_cookie("c_user"):
            return True, "authenticated"
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            return False, "session_invalid"
        return False, "driver_error"
    return False, "not_authenticated"


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


def _domain_org_index_path(output_csv_path: str) -> str:
    base, ext = os.path.splitext(output_csv_path)
    return f"{base}_domain_org_index{ext or '.csv'}"


def _domain_reuse_role_sort_key(role: str) -> Tuple[int, str]:
    role_key = _clean_cell(role)
    return (DOMAIN_REUSE_ROLE_PRIORITY.get(role_key, len(DOMAIN_REUSE_ROLE_PRIORITY)), role_key)


def _domain_reuse_source_sort_key(source_type: str) -> Tuple[int, str]:
    source_key = _clean_cell(source_type)
    return (_domain_reuse_source_rank(source_key), source_key)


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


def _sanitize_fb_song_title(title: str) -> str:
    """Lightly clean a song title for Facebook discovery query use only."""
    if not isinstance(title, str):
        return ""

    working = title.strip()
    if not working:
        return ""

    working = re.sub(r"\([^)]*\)", " ", working)
    working = re.sub(r"\s*[/\\\\|]+\s*", " ", working)
    working = re.sub(r"\s+", " ", working)
    return working.strip()


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


def _bandcamp_location_signal(location_hint: str) -> str:
    raw = (location_hint or "").strip()
    if not raw:
        return ""
    first_segment = re.split(r"[|;/]", raw, maxsplit=1)[0].strip()
    parts = [part.strip() for part in first_segment.split(",") if part.strip()]
    preferred = parts[0] if parts else first_segment
    tokens = [token for token in normalize_name(preferred).split() if len(token) >= 3]
    return " ".join(tokens[:2])


def _bandcamp_genre_signal(primary_genre_hint: str) -> str:
    tokens = [token for token in normalize_name(primary_genre_hint or "").split() if len(token) >= 3]
    return " ".join(tokens[:2])


def _bandcamp_query_metadata_term(location_hint: str, primary_genre_hint: str) -> str:
    return _bandcamp_location_signal(location_hint) or _bandcamp_genre_signal(primary_genre_hint)


def _bandcamp_should_use_metadata_query(artist_name: str, metadata_term: str) -> bool:
    if not metadata_term:
        return False
    artist_tokens = [token for token in normalize_name(artist_name).split() if token]
    if not artist_tokens:
        return False
    total_chars = sum(len(token) for token in artist_tokens)
    if len(artist_tokens) == 1:
        return len(artist_tokens[0]) <= 12
    return len(artist_tokens) == 2 and total_chars <= 12


def build_bandcamp_queries(
    artist_name: str,
    track_title: Optional[str] = None,
    location_hint: str = "",
    primary_genre_hint: str = "",
) -> List[str]:
    artist = (artist_name or "").strip()
    track = (track_title or "").strip()
    metadata_term = _bandcamp_query_metadata_term(location_hint, primary_genre_hint)

    queries: List[str] = []
    if track:
        if _bandcamp_should_use_metadata_query(artist, metadata_term):
            queries.append(f'"{artist}" "{track}" "{metadata_term}"')
            queries.append(f'"{artist}" "{track}"')
        else:
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
    Uses identity-evidence scoring for the artist name instead of raw fuzz ratio.
    """
    seed_artist_n = normalize_text(seed_artist)
    seed_title_n = normalize_text(seed_title)
    cand_artist_n = normalize_text(cand_artist)
    cand_title_n = normalize_text(cand_title)

    # Identity-based artist name score
    artist_identity_score, artist_tier, _ = _compute_identity_match_score(
        seed_artist=seed_artist,
        candidate_display=cand_artist,
        candidate_handle="",
    )
    # Map identity classification to a base contribution.
    # Exact and strong must exceed the 0.7 MATCH_THRESHOLD on their own.
    if artist_tier == "exact":
        score = 0.80
    elif artist_tier == "strong":
        score = 0.72
    elif artist_tier == "plausible":
        score = 0.35
    elif artist_identity_score >= 0.30:
        score = 0.12
    else:
        score = 0.0

    # Title match (preserved from original, but capped lower)
    title_score = fuzz.ratio(seed_title_n, cand_title_n) if seed_title_n and cand_title_n else 0
    if title_score >= 90:
        score += 0.25
    elif title_score >= 80:
        score += 0.15
    elif title_score >= 70:
        score += 0.08

    # Domain match
    spotify_domain = (spotify_domain or "").lower()
    candidate_domain = (candidate_domain or "").lower()
    if spotify_domain and candidate_domain:
        if spotify_domain == candidate_domain or candidate_domain.endswith("." + spotify_domain):
            score += 0.18

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


def _row_has_usable_email_for_fb_skip(row) -> bool:
    if row is None:
        return False

    emails: List[str] = []
    for key in ("Email", "Email_All", "Email All"):
        try:
            raw = row.get(key, "")
        except AttributeError:
            try:
                raw = row[key]
            except Exception:
                raw = ""
        text = str(raw or "").strip()
        if not text:
            continue
        for token in re.split(r"[\s,;|]+", text):
            normalized = normalize_email_value(token)
            if normalized:
                emails.append(normalized)
    if not emails:
        return False
    return any(not is_obvious_placeholder_email(email) for email in emails)


def _row_is_unearthed_source(row: Any) -> bool:
    if row is None:
        return False

    values: List[str] = []
    for key in ("Source Directory", "Source Tag", "__source_job"):
        try:
            raw = row.get(key, "")
        except AttributeError:
            try:
                raw = row[key]
            except Exception:
                raw = ""
        text = str(raw or "").strip().lower()
        if text:
            values.append(text)
    return any(("unearthed" in value) or ("triple j" in value) for value in values)


def _row_has_usable_unearthed_instagram_entrypoint(row: Any) -> bool:
    if not _row_is_unearthed_source(row):
        return False
    try:
        return bool(_get_canonical_instagram_url(row))
    except Exception:
        return False


def _row_has_usable_unearthed_fb_entrypoint(row: Any) -> bool:
    if row is None or not _row_is_unearthed_source(row):
        return False
    try:
        canonical_url, _ = ensure_canonical_facebook_url(row, set_row=False)
    except Exception:
        canonical_url = ""
    if canonicalize_facebook_url(canonical_url):
        return True
    try:
        row_payload = row.to_dict() if hasattr(row, "to_dict") else row
    except Exception:
        row_payload = row
    try:
        explicit_urls = explicit_fb_entrypoint_urls_for_row(row_payload)
        share_runtime_fallback_urls = fb_share_runtime_fallback_urls_for_row(row_payload)
    except Exception:
        explicit_urls = []
        share_runtime_fallback_urls = []
    return explicit_fb_entrypoint_present_for_row(
        row_payload,
        accepted_urls=explicit_urls,
        share_runtime_fallback_urls=share_runtime_fallback_urls,
    )


def _should_force_unearthed_platform_enrichment(row: Any, platform: str) -> bool:
    platform_norm = (platform or "").strip().lower()
    if platform_norm == "instagram":
        return _row_has_usable_unearthed_instagram_entrypoint(row)
    if platform_norm == "facebook":
        return _row_has_usable_unearthed_fb_entrypoint(row)
    return False


def _classify_contact_role_from_email(email: str) -> Optional[str]:
    """Classify a reusable org email from its normalized local-part only."""
    normalized = normalize_email_value(email)
    if not normalized or "@" not in normalized:
        return None
    local_part = normalized.split("@", 1)[0]
    if local_part in {"mgmt", "management", "manager"}:
        return "management"
    if local_part in {"booking", "bookings", "agent", "agents"}:
        return "booking"
    if local_part in {"press", "media", "pr"}:
        return "press"
    if local_part in {"info", "hello", "contact"}:
        return "general"
    if local_part in {"demo", "releases", "label"}:
        return "label_related"
    return None


def _collect_same_domain_contacts(domain_norm: str, *values: Any) -> List[str]:
    domain_key = _clean_cell(domain_norm).lower()
    if not domain_key:
        return []

    contacts: List[str] = []
    seen: Set[str] = set()
    for raw_value in values:
        if raw_value is None:
            continue
        items = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
        for item in items:
            for token in re.split(r"[\s,;]+", "" if item is None else str(item)):
                normalized = normalize_email_value(token)
                if not normalized or "@" not in normalized:
                    continue
                if normalized.split("@", 1)[1] != domain_key or normalized in seen:
                    continue
                seen.add(normalized)
                contacts.append(normalized)
    return contacts


def _domain_reuse_source_rank(source_type: str) -> int:
    source_key = _clean_cell(source_type).lower()
    if not source_key:
        return 999
    return DOMAIN_REUSE_SOURCE_PRIORITY.get(source_key, 100)


def _merge_domain_reuse_contact_meta(
    existing_meta: Optional[Dict[str, Any]],
    candidate_meta: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    merged = {
        key: _clean_cell(value)
        for key, value in dict(existing_meta or {}).items()
        if _clean_cell(value)
    }
    candidate = {
        key: _clean_cell(value)
        for key, value in dict(candidate_meta or {}).items()
        if _clean_cell(value)
    }
    if not candidate:
        return merged

    meta_fields = ("source_url", "source_type", "extract_method", "email_type")
    existing_source = merged.get("source_type", "")
    candidate_source = candidate.get("source_type", "")
    replace_meta_fields = False
    if candidate_source:
        existing_rank = _domain_reuse_source_rank(existing_source)
        candidate_rank = _domain_reuse_source_rank(candidate_source)
        if not existing_source or candidate_rank < existing_rank:
            replace_meta_fields = True
        elif candidate_rank == existing_rank:
            existing_completeness = sum(bool(merged.get(field, "")) for field in meta_fields)
            candidate_completeness = sum(bool(candidate.get(field, "")) for field in meta_fields)
            if candidate_completeness > existing_completeness:
                replace_meta_fields = True

    if replace_meta_fields:
        for field in meta_fields:
            value = candidate.get(field, "")
            if value:
                merged[field] = value
    else:
        for field in meta_fields:
            value = candidate.get(field, "")
            if value and not merged.get(field, ""):
                merged[field] = value

    role = candidate.get("role", "")
    if role:
        merged["role"] = role
    return merged


def _domain_profile_contact_meta(profile: Optional[Dict[str, Any]], contact: str) -> Dict[str, str]:
    if not isinstance(profile, dict):
        return {}
    contact_meta = profile.get(DOMAIN_PROFILE_CONTACT_META_KEY) or {}
    return {
        key: _clean_cell(value)
        for key, value in dict(contact_meta.get(contact) or {}).items()
        if _clean_cell(value)
    }


def _select_best_reusable_domain_contact(
    domain_norm: str,
    candidate_contacts: Optional[Iterable[str]],
    *,
    profile: Optional[Dict[str, Any]] = None,
    existing_entry: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[str]]:
    contact_counts = (profile or {}).get("contact_counts") or {}
    contact_meta = (profile or {}).get(DOMAIN_PROFILE_CONTACT_META_KEY) or {}
    aggregate_contacts = _collect_same_domain_contacts(
        domain_norm,
        list(candidate_contacts or []),
        (profile or {}).get("contacts") or [],
        list(contact_counts.keys()),
        (existing_entry or {}).get("email", ""),
        (existing_entry or {}).get("email_all", ""),
    )
    if not aggregate_contacts:
        return ("", [])

    def _rank(contact: str) -> Tuple[int, int, int, str]:
        role_rank = DOMAIN_REUSE_ROLE_PRIORITY.get(
            _classify_contact_role_from_email(contact),
            len(DOMAIN_REUSE_ROLE_PRIORITY),
        )
        seen_count = int(contact_counts.get(contact, 0) or 0)
        source_rank = _domain_reuse_source_rank((contact_meta.get(contact) or {}).get("source_type", ""))
        return (role_rank, -seen_count, source_rank, contact)

    best_contact = min(aggregate_contacts, key=_rank)
    return (best_contact, aggregate_contacts)


def _domain_profile_contact_local_parts(profile: Optional[Dict[str, Any]]) -> Set[str]:
    local_parts: Set[str] = set()
    if not isinstance(profile, dict):
        return local_parts
    raw_contacts = list(profile.get("contacts") or [])
    raw_contacts.extend((profile.get("contact_counts") or {}).keys())
    for contact in raw_contacts:
        normalized = normalize_email_value(contact)
        if not normalized or "@" not in normalized:
            continue
        local_parts.add(normalized.split("@", 1)[0])
    return local_parts


def _profile_has_strong_label_string_signal(domain_norm: str, profile: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(profile, dict):
        return False
    text_parts: List[str] = []
    if domain_norm:
        text_parts.append(domain_norm)
    first_source_url = _clean_cell(profile.get("first_source_url", ""))
    if first_source_url:
        text_parts.append(first_source_url)
    for source_type in profile.get("source_types") or []:
        source_type_clean = _clean_cell(source_type)
        if source_type_clean:
            text_parts.append(source_type_clean)
    if not text_parts:
        return False
    normalized_text = " ".join(normalize_role_text(part) for part in text_parts if part)
    strong_hits = 0
    for pattern in DOMAIN_PROFILE_STRONG_LABEL_PATTERNS:
        if re.search(pattern, normalized_text):
            strong_hits += 1
    return strong_hits >= 2


def _infer_domain_org_type(domain_norm: str, profile: Optional[Dict[str, Any]]) -> str:
    if not isinstance(profile, dict):
        return "unknown"
    try:
        artist_count = int(profile.get("artist_count", 0) or 0)
    except Exception:
        artist_count = 0
    if artist_count < 2:
        return "unknown"

    local_parts = _domain_profile_contact_local_parts(profile)
    has_management = bool(local_parts & DOMAIN_PROFILE_MANAGEMENT_LOCAL_PARTS)
    has_booking = bool(local_parts & DOMAIN_PROFILE_BOOKING_LOCAL_PARTS)
    has_label = bool(local_parts & DOMAIN_PROFILE_LABEL_LOCAL_PARTS)
    if not has_label:
        has_label = _profile_has_strong_label_string_signal(_clean_cell(domain_norm).lower(), profile)

    matched_type_count = int(has_management) + int(has_booking) + int(has_label)
    if matched_type_count != 1:
        return "unknown"
    if has_management:
        return "management"
    if has_booking:
        return "booking_agency"
    if has_label:
        return "label"
    return "unknown"


def _domain_artist_keys_from_profile(profile: Optional[Dict[str, Any]]) -> Set[str]:
    artist_keys: Set[str] = set()
    if not isinstance(profile, dict):
        return artist_keys
    raw_keys = profile.get("_artist_keys")
    if isinstance(raw_keys, set):
        artist_keys.update(_clean_cell(key) for key in raw_keys if _clean_cell(key))
    elif isinstance(raw_keys, (list, tuple)):
        artist_keys.update(_clean_cell(key) for key in raw_keys if _clean_cell(key))
    for artist_name in profile.get("artists_sample") or []:
        artist_key = normalise_artist_name(_clean_cell(artist_name))
        if artist_key:
            artist_keys.add(artist_key)
    return artist_keys


def _domain_profile_artist_samples(profile: Optional[Dict[str, Any]]) -> List[str]:
    samples: List[str] = []
    if not isinstance(profile, dict):
        return samples
    seen: Set[str] = set()
    for artist_name in profile.get("artists_sample") or []:
        cleaned = _clean_cell(artist_name)
        if not cleaned:
            continue
        artist_key = normalise_artist_name(cleaned)
        if not artist_key or artist_key in seen:
            continue
        seen.add(artist_key)
        samples.append(cleaned)
    return samples


def _merge_domain_profile_indexes(
    left_index: Optional[Dict[str, Dict[str, Any]]],
    right_index: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    left_index = left_index or {}
    right_index = right_index or {}
    for domain in sorted(set(left_index) | set(right_index)):
        domain_key = _clean_cell(domain).lower()
        if not domain_key:
            continue
        combined = {
            "contacts": [],
            "artist_count": 0,
            "artists_sample": [],
            "source_types": [],
            "first_source_url": "",
            "seen_count": 0,
            "contact_counts": {},
            "org_type": "unknown",
            DOMAIN_PROFILE_CONTACT_META_KEY: {},
            "_artist_keys": set(),
        }
        contact_seen: Set[str] = set()
        source_seen: Set[str] = set()
        artist_sample_seen: Set[str] = set()
        for profile in (left_index.get(domain_key), right_index.get(domain_key)):
            if not isinstance(profile, dict):
                continue
            combined["seen_count"] += int(profile.get("seen_count", 0) or 0)
            first_source_url = _clean_cell(profile.get("first_source_url", ""))
            if first_source_url and not combined["first_source_url"]:
                combined["first_source_url"] = first_source_url
            for contact in _collect_same_domain_contacts(
                domain_key,
                profile.get("contacts") or [],
                list((profile.get("contact_counts") or {}).keys()),
                list(((profile.get(DOMAIN_PROFILE_CONTACT_META_KEY) or {}).keys())),
            ):
                if contact in contact_seen:
                    continue
                contact_seen.add(contact)
                combined["contacts"].append(contact)
            for contact, count in dict(profile.get("contact_counts") or {}).items():
                normalized = normalize_email_value(contact)
                if not normalized or "@" not in normalized or normalized.split("@", 1)[1] != domain_key:
                    continue
                combined["contact_counts"][normalized] = combined["contact_counts"].get(normalized, 0) + int(count or 0)
            for contact, meta in dict(profile.get(DOMAIN_PROFILE_CONTACT_META_KEY) or {}).items():
                normalized = normalize_email_value(contact)
                if not normalized or "@" not in normalized or normalized.split("@", 1)[1] != domain_key:
                    continue
                current_meta = combined[DOMAIN_PROFILE_CONTACT_META_KEY].get(normalized)
                combined[DOMAIN_PROFILE_CONTACT_META_KEY][normalized] = _merge_domain_reuse_contact_meta(current_meta, meta)
            for source_type in profile.get("source_types") or []:
                source_clean = _clean_cell(source_type)
                if not source_clean or source_clean in source_seen:
                    continue
                source_seen.add(source_clean)
                combined["source_types"].append(source_clean)
            combined["_artist_keys"].update(_domain_artist_keys_from_profile(profile))
            for artist_name in _domain_profile_artist_samples(profile):
                artist_key = normalise_artist_name(artist_name)
                if not artist_key or artist_key in artist_sample_seen:
                    continue
                artist_sample_seen.add(artist_key)
                if len(combined["artists_sample"]) < DOMAIN_PROFILE_ARTISTS_SAMPLE_MAX:
                    combined["artists_sample"].append(artist_name)
        combined["artist_count"] = len(combined["_artist_keys"])
        combined["org_type"] = _infer_domain_org_type(domain_key, combined)
        merged[domain_key] = combined
    return merged


def _merge_domain_email_reuse_indexes(
    profile_index: Optional[Dict[str, Dict[str, Any]]],
    left_index: Optional[Dict[str, Dict[str, Any]]],
    right_index: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {}
    profile_index = profile_index or {}
    left_index = left_index or {}
    right_index = right_index or {}
    for domain in sorted(set(profile_index) | set(left_index) | set(right_index)):
        domain_key = _clean_cell(domain).lower()
        if not domain_key:
            continue
        profile = profile_index.get(domain_key)
        left_entry = left_index.get(domain_key) or {}
        right_entry = right_index.get(domain_key) or {}
        best_contact, aggregate_contacts = _select_best_reusable_domain_contact(
            domain_key,
            _collect_same_domain_contacts(
                domain_key,
                left_entry.get("email", ""),
                left_entry.get("email_all", ""),
                right_entry.get("email", ""),
                right_entry.get("email_all", ""),
            ),
            profile=profile,
        )
        if not best_contact:
            continue
        selected_meta = _merge_domain_reuse_contact_meta(
            _domain_profile_contact_meta(profile, best_contact),
            {},
        )
        for entry in (left_entry, right_entry):
            if _clean_cell(entry.get("email", "")) == best_contact:
                selected_meta = _merge_domain_reuse_contact_meta(selected_meta, entry)
        entry = {
            "email": best_contact,
            "email_all": ";".join(aggregate_contacts),
            "source_url": _clean_cell(selected_meta.get("source_url", "")),
            "source_type": _clean_cell(selected_meta.get("source_type", "")),
            "extract_method": _clean_cell(selected_meta.get("extract_method", "")) or "regex",
            "email_type": _clean_cell(selected_meta.get("email_type", "")),
        }
        role = _classify_contact_role_from_email(best_contact) or _clean_cell(selected_meta.get("role", ""))
        if role:
            entry["role"] = role
        merged[domain_key] = entry
    return merged


def _build_domain_org_export_rows(
    domain_profile_index: Optional[Dict[str, Dict[str, Any]]],
    domain_email_reuse_index: Optional[Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    domain_profile_index = domain_profile_index or {}
    domain_email_reuse_index = domain_email_reuse_index or {}
    for domain in sorted(set(domain_profile_index) | set(domain_email_reuse_index)):
        domain_key = _clean_cell(domain).lower()
        if not domain_key:
            continue
        profile = domain_profile_index.get(domain_key) or {}
        reuse_entry = domain_email_reuse_index.get(domain_key) or {}
        contacts = _collect_same_domain_contacts(
            domain_key,
            profile.get("contacts") or [],
            list((profile.get("contact_counts") or {}).keys()),
            reuse_entry.get("email", ""),
            reuse_entry.get("email_all", ""),
        )
        roles_seen: Set[str] = set()
        for contact in contacts:
            role = _classify_contact_role_from_email(contact)
            if role:
                roles_seen.add(role)
        for meta in dict(profile.get(DOMAIN_PROFILE_CONTACT_META_KEY) or {}).values():
            role = _clean_cell((meta or {}).get("role", ""))
            if role:
                roles_seen.add(role)
        reuse_role = _clean_cell(reuse_entry.get("role", ""))
        if reuse_role:
            roles_seen.add(reuse_role)
        sources_seen: Set[str] = set()
        for source_type in profile.get("source_types") or []:
            source_clean = _clean_cell(source_type)
            if source_clean:
                sources_seen.add(source_clean)
        for meta in dict(profile.get(DOMAIN_PROFILE_CONTACT_META_KEY) or {}).values():
            source_clean = _clean_cell((meta or {}).get("source_type", ""))
            if source_clean:
                sources_seen.add(source_clean)
        reuse_source = _clean_cell(reuse_entry.get("source_type", ""))
        if reuse_source:
            sources_seen.add(reuse_source)
        try:
            artist_count = int(profile.get("artist_count", 0) or 0)
        except Exception:
            artist_count = 0
        row = {
            "domain": domain_key,
            "org_type": _clean_cell(profile.get("org_type", "")) or "unknown",
            "artist_count": artist_count,
            "primary_email": normalize_email_value(reuse_entry.get("email", "")),
            "emails": ";".join(sorted(contacts)),
            "roles_seen": "|".join(sorted(roles_seen, key=_domain_reuse_role_sort_key)),
            "sources_seen": "|".join(sorted(sources_seen, key=_domain_reuse_source_sort_key)),
        }
        if row["primary_email"] or row["emails"] or row["artist_count"] > 0 or row["sources_seen"] or row["roles_seen"]:
            rows.append(row)
    return rows


def _write_domain_org_sidecar(
    output_csv_path: str,
    domain_profile_index: Optional[Dict[str, Dict[str, Any]]],
    domain_email_reuse_index: Optional[Dict[str, Dict[str, Any]]],
    *,
    log_fn=None,
) -> str:
    path = _domain_org_index_path(output_csv_path)
    rows = _build_domain_org_export_rows(domain_profile_index, domain_email_reuse_index)
    if not rows:
        if callable(log_fn):
            log_fn("[DomainOrg] No exportable domain/org data; sidecar skipped.")
        return ""
    df = pd.DataFrame(rows, columns=list(DOMAIN_ORG_SIDECAR_COLUMNS))
    try:
        from pipeline_runner import _safe_atomic_write_csv

        _safe_atomic_write_csv(
            df,
            path,
            list(DOMAIN_ORG_SIDECAR_COLUMNS),
            reason="domain_org_index",
        )
    except Exception:
        _ensure_parent_dir(path)
        df.to_csv(path, index=False, encoding="utf-8-sig")
    if callable(log_fn):
        log_fn(f"[DomainOrg] Wrote domain/org index sidecar: {path}")
    return path


def _row_email_summary_snapshot(df: pd.DataFrame, row_idx) -> Dict[str, str]:
    """Capture the row email fields used by scheduler email accounting."""
    snapshot: Dict[str, str] = {}
    for col in ("Email", "Email_All"):
        if col in df.columns:
            snapshot[col] = cell_to_str(df.at[row_idx, col])
        else:
            snapshot[col] = ""
    return snapshot


_CHUNK_YIELD_EMAIL_VALUE_SPLIT_RE = re.compile(r"[;,|\n\r]+")


def _normalized_email_set_from_values(*values: Any) -> Set[str]:
    """Normalize Email/Email_All-style values for chunk attribution metrics."""

    emails: Set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            items = [value]
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            for token in _CHUNK_YIELD_EMAIL_VALUE_SPLIT_RE.split(text):
                fragment = str(token or "").strip()
                if not fragment:
                    continue
                normalized = normalize_email_value(fragment)
                if normalized:
                    emails.add(normalized)
                    continue
                if any(ch.isspace() for ch in fragment):
                    for fallback_token in re.split(r"\s+", fragment):
                        fallback_normalized = normalize_email_value(fallback_token)
                        if fallback_normalized:
                            emails.add(fallback_normalized)
    return emails


def _row_email_set(row_like: Any) -> Set[str]:
    if row_like is None or not hasattr(row_like, "get"):
        return set()
    return _normalized_email_set_from_values(
        row_like.get("Email", ""),
        row_like.get("Email_All", ""),
    )


def _committed_row_email_delta(
    before_row: Any,
    after_row: Any,
) -> Set[str]:
    """Return the committed email delta for chunk write attribution.

    Snapshot fields:
    - before_row["Email"]
    - before_row["Email_All"]
    - after_row["Email"]
    - after_row["Email_All"]

    Normalization rules:
    - split multi-value fields on ";", ",", "|", or line breaks
    - trim surrounding whitespace
    - collapse whitespace around "@"
    - lowercase
    - dedupe via sets
    - drop invalid/non-email tokens
    - ignore empty tokens
    - ignore ordering in Email_All

    Delta computation:
    - committed_delta = normalized(after Email + Email_All) - normalized(before Email + Email_All)
    """

    before_email_set = _row_email_set(before_row)
    after_email_set = _row_email_set(after_row)
    return after_email_set - before_email_set


def _committed_row_email_delta_intersection(
    before_row: Any,
    after_row: Any,
    found_emails: Iterable[Any],
) -> Tuple[Set[str], Set[str]]:
    """Return the committed delta and its source-bounded overlap with found emails.

    The found email set is normalized with the same rules as the committed row
    snapshots. The overlap is:
    - committed_delta & normalized(found_emails)

    IG chunk written attribution is allowed only when this overlap is non-empty,
    which prevents later writes from other sources from being attributed back to
    the IG path.
    """

    committed_delta = _committed_row_email_delta(before_row, after_row)
    found_email_set = _normalized_email_set_from_values(found_emails)
    return committed_delta, committed_delta & found_email_set


_IG_EXECUTION_PATH_DEPTH: Dict[str, int] = {
    "direct_profile": 1,
    "live_bridge": 2,
    "one_hop": 3,
    "other": 4,
}


@dataclass
class InstagramExecutionPathTracker:
    attempted_paths: List[str] = field(default_factory=list)
    deepest_executed_path: str = ""
    found_path: str = ""
    written_path: str = ""

    @staticmethod
    def _normalize(path: Any) -> str:
        value = cell_to_str(path).strip().lower()
        if value in {"direct_profile", "live_bridge", "one_hop", "other"}:
            return value
        return "other" if value else ""

    def mark_attempt(self, path: Any) -> str:
        normalized = self._normalize(path)
        if not normalized:
            return ""
        if normalized not in self.attempted_paths:
            self.attempted_paths.append(normalized)
        if (
            not self.deepest_executed_path
            or _IG_EXECUTION_PATH_DEPTH.get(normalized, 0)
            >= _IG_EXECUTION_PATH_DEPTH.get(self.deepest_executed_path, 0)
        ):
            self.deepest_executed_path = normalized
        return normalized

    def mark_found(self, path: Any) -> str:
        normalized = self.mark_attempt(path)
        if normalized and not self.found_path and not self.written_path:
            self.found_path = normalized
        return normalized

    def mark_written(self, path: Any) -> str:
        normalized = self.mark_found(path)
        if normalized and not self.written_path:
            self.written_path = normalized
        return normalized

    def terminal_path(self) -> str:
        return self.written_path or self.found_path or self.deepest_executed_path or ""


def _classify_instagram_opportunity_state(row_like: Any) -> str:
    return "ig_opportunity_present" if _get_canonical_instagram_url(row_like) else "no_ig_opportunity"


def _ensure_instagram_row_attribution(seed_df: pd.DataFrame, row_idx: Any) -> None:
    ensure_ig_attribution_columns(seed_df)
    seed_df.at[row_idx, IG_OPPORTUNITY_STATE_COL] = _classify_instagram_opportunity_state(seed_df.loc[row_idx])
    if not cell_to_str(seed_df.at[row_idx, IG_ATTEMPT_STATE_COL]):
        seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "ig_not_attempted"
    if not cell_to_str(seed_df.at[row_idx, IG_EXTRACT_STATE_COL]):
        if cell_to_str(seed_df.at[row_idx, IG_OPPORTUNITY_STATE_COL]) == "no_ig_opportunity":
            seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_extract_not_applicable"
        else:
            seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_extract_not_attempted"
    if not cell_to_str(seed_df.at[row_idx, IG_WRITE_STATE_COL]):
        seed_df.at[row_idx, IG_WRITE_STATE_COL] = "ig_no_email_written"


def finalize_instagram_row_attribution(seed_df: pd.DataFrame, row_idx: Any) -> None:
    if seed_df is None or row_idx not in getattr(seed_df, "index", []):
        return
    ensure_ig_attribution_columns(seed_df)
    finalize_ig_normalized_terminal(seed_df, row_idx)


def _mark_instagram_not_attempted(
    seed_df: pd.DataFrame,
    row_idx: Any,
    *,
    terminal_reason: str,
) -> None:
    _ensure_instagram_row_attribution(seed_df, row_idx)
    opportunity_state = cell_to_str(seed_df.at[row_idx, IG_OPPORTUNITY_STATE_COL])
    seed_df.at[row_idx, IG_SURFACE_REASON_COL] = ""
    seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "ig_not_attempted"
    if opportunity_state == "no_ig_opportunity":
        seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_extract_not_applicable"
        seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "no_ig_opportunity"
    else:
        seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_extract_not_attempted"
        seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = terminal_reason
    if not cell_to_str(seed_df.at[row_idx, IG_WRITE_STATE_COL]):
        seed_df.at[row_idx, IG_WRITE_STATE_COL] = "ig_no_email_written"


def _classify_instagram_write_state(
    before_row: Any,
    after_row: Any,
    found_emails: Iterable[Any],
) -> str:
    found_email_set = _normalized_email_set_from_values(found_emails)
    if not found_email_set:
        return "ig_no_email_written"

    committed_delta, delta_intersection = _committed_row_email_delta_intersection(
        before_row,
        after_row,
        found_email_set,
    )
    if not delta_intersection:
        return "ig_found_email_not_applied"

    before_email_set = _normalized_email_set_from_values(
        before_row.get("Email", "") if hasattr(before_row, "get") else ""
    )
    after_email_set = _normalized_email_set_from_values(
        after_row.get("Email", "") if hasattr(after_row, "get") else ""
    )
    if delta_intersection & (after_email_set - before_email_set):
        return "ig_wrote_email"
    if committed_delta & found_email_set:
        return "ig_wrote_email_all_only"
    return "ig_found_email_not_applied"


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


def _row_has_fb_discovery_attempt_flag(row: Any) -> bool:
    if row is None:
        return False
    for key in FB_DISCOVERY_ATTEMPT_FLAG_ALIASES:
        try:
            value = row.get(key, "")
        except AttributeError:
            try:
                value = row[key]
            except Exception:
                value = ""
        if _clean_cell(value):
            return True
    return False


def _get_direct_canonical_fb_urls(row: Any) -> Set[str]:
    canonical_urls: Set[str] = set()
    if row is None:
        return canonical_urls

    for key in ("Facebook_URL", "facebook_url", "Facebook URL"):
        try:
            raw = row.get(key, "")
        except AttributeError:
            try:
                raw = row[key]
            except Exception:
                raw = ""
        canonical = canonicalize_facebook_url(cell_to_str(raw))
        if canonical:
            canonical_urls.add(canonical)
    return canonical_urls


def _get_payload_canonical_facebook_urls(payload: Optional["EnrichmentPayload"]) -> Set[str]:
    canonical_urls: Set[str] = set()
    if not payload:
        return canonical_urls

    for raw in set(getattr(payload, "socials", set()) or set()) | set(getattr(payload, "websites", set()) or set()):
        canonical = canonicalize_facebook_url(raw)
        if canonical:
            canonical_urls.add(canonical)
    return canonical_urls


def _promote_payload_facebook_url(df: pd.DataFrame, row_idx, payload: Optional["EnrichmentPayload"]) -> bool:
    canonical_urls = _get_payload_canonical_facebook_urls(payload)
    if len(canonical_urls) != 1:
        return False

    promoted_url = next(iter(canonical_urls))
    row = df.loc[row_idx]
    existing_direct_urls = _get_direct_canonical_fb_urls(row)
    current_canonical = canonicalize_facebook_url(_coerce_directory_value(row.get("Facebook_URL", "")))

    if current_canonical:
        return current_canonical == promoted_url
    if any(existing_url != promoted_url for existing_url in existing_direct_urls):
        return False

    if "Facebook_URL" not in df.columns:
        df["Facebook_URL"] = ""
    df.at[row_idx, "Facebook_URL"] = promoted_url
    return True


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
_SPOTIFY_IG_SEED_MIN_HANDLE_LEN = 5
_SPOTIFY_IG_SEED_MAX_CANDIDATES = 6
_SPOTIFY_IG_SEED_REJECT_HANDLES = frozenset(
    {
        "artist",
        "artists",
        "band",
        "bands",
        "dj",
        "music",
        "musician",
        "official",
    }
)


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


def _instagram_profile_handle_token_from_url(
    raw: Any,
    *,
    allow_routed_subpaths: bool = False,
) -> str:
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
    if not segments or (not allow_routed_subpaths and len(segments) != 1):
        return ""
    handle = _spotify_seed_instagram_identity_handle_token(segments[0])
    if not handle or handle in _INSTAGRAM_REJECT_SEGMENTS:
        return ""
    return handle


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


def _spotify_instagram_identity_website_candidate(row: Any) -> str:
    if row is None:
        return ""
    spotify_website_url = _normalise_url(
        _clean_cell(row.get("Spotify_Website_URL", "")) if hasattr(row, "get") else ""
    )
    if spotify_website_url:
        return spotify_website_url
    if hasattr(row, "get"):
        for token in _split_multi_value(row.get("External Links", "")):
            normalised = _normalise_url(token)
            if not normalised or _host(normalised) not in LINK_HUB_HOSTS:
                continue
            return normalised
    return ""


_SPOTIFY_IG_EXTERNAL_SOURCE_BRANCH_KEYS = frozenset({"spotifyinstagramrecovery"})
_SPOTIFY_IG_IDENTITY_GENERIC_TOKENS = frozenset({"artist", "band", "dj", "music", "producer"})


def _spotify_seed_instagram_identity_handle_token(raw_value: Any) -> str:
    value = cell_to_str(raw_value)
    if not value:
        return ""
    token = value.strip().strip("/")
    if token.startswith("@"):
        token = token[1:]
    if (
        not token
        or "/" in token
        or any(ch.isspace() for ch in token)
    ):
        return ""
    token = unicodedata.normalize("NFKD", token)
    token = "".join(ch for ch in token if not unicodedata.combining(ch)).lower()
    if not _INSTAGRAM_HANDLE_RE.fullmatch(token):
        return ""
    return token


def _spotify_seed_instagram_external_source_urls(row: Any) -> List[str]:
    candidates: List[str] = []
    seen: Set[str] = set()

    def _push(raw_value: Any) -> None:
        canonical = _canonicalize_instagram_profile_url(cell_to_str(raw_value))
        if canonical and canonical not in seen:
            seen.add(canonical)
            candidates.append(canonical)

    if row is None or not hasattr(row, "get"):
        return candidates

    # Spotify website links are a direct non-Instagram upstream input.
    _push(row.get("Spotify_Website_URL", ""))

    source_dir_key = re.sub(r"[^a-z0-9]+", "", _clean_cell(row.get("Source Directory", "")).lower())
    if source_dir_key in _SPOTIFY_IG_EXTERNAL_SOURCE_BRANCH_KEYS:
        # Only trust already-present row IG URLs when the writing branch is a known
        # external-source recovery path.
        _push(row.get("Source URL", ""))

    return candidates


def _spotify_seed_instagram_external_corroboration_tokens(row: Any) -> Set[str]:
    tokens: Set[str] = set()
    if row is None or not hasattr(row, "get"):
        return tokens

    def _push(raw_value: Any) -> None:
        token = _spotify_seed_instagram_identity_handle_token(raw_value)
        if token:
            tokens.add(token)

    def _push_bandcamp_url(raw_url: Any) -> None:
        canonical = _canonicalise_bandcamp_url(cell_to_str(raw_url))
        host = _host(canonical)
        if not host or not host.endswith(".bandcamp.com"):
            return
        subdomain = host[: -len(".bandcamp.com")]
        if subdomain and "." not in subdomain:
            _push(subdomain)

    def _push_soundcloud_url(raw_url: Any) -> None:
        url = _normalise_url(cell_to_str(raw_url)) or cell_to_str(raw_url)
        if not url:
            return
        handle = _sc_handle_from_profile_url(url) or _sc_handle_from_url(url)
        _push(handle)

    _push_bandcamp_url(row.get("Bandcamp_URL", ""))
    _push_soundcloud_url(row.get("SoundCloud Link", ""))
    _push_soundcloud_url(row.get("SoundCloud URL", ""))

    source_url = _normalise_url(_clean_cell(row.get("Source URL", ""))) or ""
    source_url_source = _source_for_url(source_url) or ""
    if source_url_source == "bandcamp":
        _push_bandcamp_url(source_url)
    elif source_url_source == "soundcloud":
        _push_soundcloud_url(source_url)

    return tokens


def _spotify_seed_instagram_identity_acceptance_reason(
    row: Any,
    candidate_url: str,
    artist_name: str,
    spotify_id: str = "",
) -> str:
    canonical = _canonicalize_instagram_profile_url(candidate_url)
    if not canonical:
        return ""
    if canonical in set(_spotify_seed_instagram_external_source_urls(row)):
        return "external_source"
    try:
        parsed = urllib.parse.urlparse(canonical)
    except Exception:
        parsed = None
    handle = next((segment for segment in (parsed.path or "").split("/") if segment), "") if parsed else ""
    handle_token = _spotify_seed_instagram_identity_handle_token(handle)
    if handle_token and handle_token in _spotify_seed_instagram_external_corroboration_tokens(row):
        return "external_corroboration"
    if _spotify_seed_instagram_identity_validated(
        canonical,
        artist_name,
        spotify_id=spotify_id,
    ):
        return "identity_validated"
    return ""


def _spotify_seed_instagram_candidate_urls(
    row: Any,
    artist_name: str,
    spotify_id: str = "",
) -> List[str]:
    if row is None:
        return []

    trusted_external_urls = _spotify_seed_instagram_external_source_urls(row)
    if trusted_external_urls:
        return trusted_external_urls[:_SPOTIFY_IG_SEED_MAX_CANDIDATES]
    if _spotify_instagram_identity_website_candidate(row):
        return []

    artist_parts = [token for token in normalize_name(artist_name).split() if token]
    artist_compact = "".join(artist_parts)
    if (
        len(artist_compact) < _SPOTIFY_IG_SEED_MIN_HANDLE_LEN
        or artist_compact in _SPOTIFY_IG_SEED_REJECT_HANDLES
        or artist_compact.isdigit()
    ):
        return []

    candidates: List[str] = []
    seen: Set[str] = set()

    def _push_candidate_url(candidate_url: str) -> None:
        canonical = _canonicalize_instagram_profile_url(candidate_url)
        if (
            not canonical
            or canonical in seen
            or len(candidates) >= _SPOTIFY_IG_SEED_MAX_CANDIDATES
        ):
            return
        seen.add(canonical)
        candidates.append(canonical)

    def _push_candidate_handle(raw_handle: str) -> None:
        token = _spotify_seed_instagram_identity_handle_token(raw_handle)
        if not token:
            return
        compact = re.sub(r"[^a-z0-9]+", "", normalize_name(token))
        if (
            len(compact) < _SPOTIFY_IG_SEED_MIN_HANDLE_LEN
            or compact in _SPOTIFY_IG_SEED_REJECT_HANDLES
            or compact.isdigit()
        ):
            return
        _push_candidate_url(f"https://www.instagram.com/{token}/")

    def _push_handle_variant(raw_value: str) -> None:
        token = _spotify_seed_instagram_identity_handle_token(raw_value)
        if not token:
            return
        compact = re.sub(r"[^a-z0-9]+", "", normalize_name(token))
        if compact != artist_compact:
            return
        if (
            len(compact) < _SPOTIFY_IG_SEED_MIN_HANDLE_LEN
            or compact in _SPOTIFY_IG_SEED_REJECT_HANDLES
            or compact.isdigit()
        ):
            return
        _push_candidate_url(f"https://www.instagram.com/{token}/")

    def _variant_parts(raw_value: Any) -> List[str]:
        value = cell_to_str(raw_value)
        if not value:
            return []
        value = value.strip().strip("/")
        if value.startswith("@"):
            value = value[1:]
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch)).lower()
        return [token for token in re.split(r"[^a-z0-9]+", value) if token]

    def _push_external_token_variants(raw_value: Any) -> None:
        parts = _variant_parts(raw_value)
        if not parts:
            return
        raw_token = cell_to_str(raw_value).strip().strip("/")
        if raw_token.startswith("@"):
            raw_token = raw_token[1:]
        raw_token = unicodedata.normalize("NFKD", raw_token)
        raw_token = "".join(ch for ch in raw_token if not unicodedata.combining(ch)).lower()
        compact = "".join(parts)
        underscore = "_".join(parts) if len(parts) > 1 else ""
        dotted = ".".join(parts) if len(parts) > 1 else ""
        for handle in (raw_token, compact, underscore, dotted):
            _push_candidate_handle(handle)

    external_seed_tokens: List[str] = []
    external_seen: Set[str] = set()

    def _push_external_seed(raw_value: Any) -> None:
        value = cell_to_str(raw_value).strip().strip("/")
        if not value:
            return
        key = value.lower()
        if key in external_seen:
            return
        external_seen.add(key)
        external_seed_tokens.append(value)

    bandcamp_url = _canonicalise_bandcamp_url(cell_to_str(row.get("Bandcamp_URL", "")))
    bandcamp_host = _host(bandcamp_url)
    if bandcamp_host.endswith(".bandcamp.com"):
        bandcamp_token = bandcamp_host[: -len(".bandcamp.com")]
        if bandcamp_token and "." not in bandcamp_token:
            _push_external_seed(bandcamp_token)

    for key in ("SoundCloud Link", "SoundCloud URL"):
        soundcloud_url = _normalise_url(cell_to_str(row.get(key, ""))) or cell_to_str(row.get(key, ""))
        soundcloud_token = _sc_handle_from_profile_url(soundcloud_url) or _sc_handle_from_url(soundcloud_url)
        _push_external_seed(soundcloud_token)

    artist_underscore = "_".join(artist_parts) if len(artist_parts) > 1 else ""
    artist_dot = ".".join(artist_parts) if len(artist_parts) > 1 else ""

    _push_candidate_handle(artist_compact)
    _push_handle_variant(artist_name)
    _push_handle_variant(spotify_id)
    for token in external_seed_tokens:
        _push_external_token_variants(token)
    for handle in (
        artist_underscore,
        artist_dot,
        f"{artist_underscore}_official" if artist_underscore else "",
        f"{artist_dot}.official" if artist_dot else "",
        f"{artist_underscore}_music" if artist_underscore else "",
        f"{artist_dot}.music" if artist_dot else "",
        f"{artist_underscore}_band" if artist_underscore else "",
        f"{artist_dot}.band" if artist_dot else "",
        f"{artist_compact}official",
        f"{artist_compact}music",
        f"{artist_compact}band",
    ):
        _push_candidate_handle(handle)
    return candidates


def _spotify_seed_instagram_probe_direct_emails(
    session: requests.Session,
    candidate_url: str,
) -> List[str]:
    canonical = _canonicalize_instagram_profile_url(candidate_url)
    if not canonical:
        return []
    with _instagram_profile_fetch_scope(session, canonical, retain_live_page=False) as profile_fetch:
        html = profile_fetch.html
        status = profile_fetch.status
        if not _instagram_profile_fetch_usable(status, html):
            return []
        soup = BeautifulSoup(html, "html.parser")
        return _extract_instagram_profile_candidate_emails(html, soup=soup)


def _spotify_seed_instagram_identity_tokens(
    artist_name: str,
    spotify_id: str = "",
) -> Set[str]:
    tokens: Set[str] = set()

    def _push(raw_value: str) -> None:
        token = _spotify_seed_instagram_identity_handle_token(raw_value)
        if token:
            tokens.add(token)

    _push(artist_name)
    _push(spotify_id)
    return tokens


def _spotify_seed_instagram_identity_validated(
    candidate_url: str,
    artist_name: str,
    spotify_id: str = "",
) -> bool:
    canonical = _canonicalize_instagram_profile_url(candidate_url)
    if not canonical:
        return False
    try:
        parsed = urllib.parse.urlparse(canonical)
    except Exception:
        return False
    handle = next((segment for segment in (parsed.path or "").split("/") if segment), "")
    if not handle:
        return False
    handle_norm = normalize_name(handle)
    if not handle_norm:
        return False
    return any(
        normalize_name(token) == handle_norm
        for token in _spotify_seed_instagram_identity_tokens(artist_name, spotify_id=spotify_id)
    )


def _spotify_seed_instagram_admission_profile_validation(
    session: requests.Session,
    row: Any,
    candidate_url: str,
    artist_name: str,
) -> Tuple[bool, str]:
    canonical = _canonicalize_instagram_profile_url(candidate_url)
    if not canonical:
        return (False, "blocked:invalid_candidate")

    with _instagram_profile_fetch_scope(session, canonical, retain_live_page=False) as profile_fetch:
        html = profile_fetch.html
        status = profile_fetch.status
    if not _instagram_profile_fetch_usable(status, html):
        return (False, "blocked:profile_unavailable")

    soup = BeautifulSoup(html, "html.parser")
    seen_texts: Set[str] = set()
    title_texts: List[str] = []
    profile_texts: List[str] = []

    def _append_text(target: List[str], raw_value: Any) -> None:
        text = _clean_cell(raw_value)
        norm = normalize_name(text)
        if not norm or norm in seen_texts:
            return
        seen_texts.add(norm)
        target.append(text)

    for meta_tag in soup.select('meta[property="og:title"], meta[name="og:title"], meta[name="title"]'):
        _append_text(title_texts, meta_tag.get("content"))
    for title_tag in soup.select("title"):
        _append_text(title_texts, title_tag.get_text(" ", strip=True))
    for meta_tag in soup.select('meta[property="og:description"], meta[name="description"]'):
        _append_text(profile_texts, meta_tag.get("content"))
    for node in soup.select("h1, h2"):
        _append_text(profile_texts, node.get_text(" ", strip=True))

    artist_tokens = [
        token
        for token in normalize_name(artist_name).split()
        if token and token not in _SPOTIFY_IG_IDENTITY_GENERIC_TOKENS
    ]
    artist_norm = " ".join(artist_tokens)
    artist_compact = re.sub(r"\s+", "", artist_norm)

    def _strong_artist_match(text: str) -> bool:
        text_norm = normalize_name(text)
        if not artist_norm or not text_norm:
            return False
        if artist_norm in text_norm:
            return True
        text_compact = re.sub(r"\s+", "", text_norm)
        return bool(artist_compact and len(artist_compact) >= 4 and artist_compact in text_compact)

    if any(_strong_artist_match(text) for text in title_texts):
        return (True, "strong:profile_name_match")
    if any(_strong_artist_match(text) for text in profile_texts):
        return (True, "strong:profile_text_match")

    trusted_urls: Set[str] = set()
    trusted_host_keys: Set[str] = set()

    def _append_trusted_url(raw_value: Any, *, source_hint: str = "") -> None:
        value = _clean_cell(raw_value)
        if not value:
            return
        if source_hint == "bandcamp":
            normalised = _canonicalise_bandcamp_url(value)
        else:
            normalised = _normalise_url(value) or value
        host = _host(normalised)
        if not normalised or not host or host in {"instagram.com", "www.instagram.com", "instagr.am"}:
            return
        trusted_urls.add(normalised)
        cache_key = _website_cache_key(normalised) or host
        if cache_key:
            trusted_host_keys.add(cache_key)

    if row is not None and hasattr(row, "get"):
        _append_trusted_url(row.get("Spotify_Website_URL", ""))
        _append_trusted_url(row.get("Bandcamp_URL", ""), source_hint="bandcamp")
        _append_trusted_url(row.get("SoundCloud Link", ""))
        _append_trusted_url(row.get("SoundCloud URL", ""))
        source_url = _clean_cell(row.get("Source URL", ""))
        if (_source_for_url(source_url) or "") != "instagram":
            _append_trusted_url(source_url)

    outbound_links = _collect_instagram_bio_link_fetch_urls(
        html,
        soup=soup,
        profile_url=canonical,
    )
    if any(link in trusted_urls for link in outbound_links):
        return (True, "strong:trusted_external_link")

    weak_signals: List[str] = []
    combined_text_norm = normalize_name(" ".join([*title_texts, *profile_texts]))
    combined_tokens = set(combined_text_norm.split())
    if artist_tokens:
        shared_tokens = {token for token in artist_tokens if len(token) >= 3 and token in combined_tokens}
        if shared_tokens:
            weak_signals.append("partial_name_overlap")
    if outbound_links:
        weak_signals.append("outbound_link_present")
    if trusted_host_keys and any(
        (_website_cache_key(link) or _host(link)) in trusted_host_keys and link not in trusted_urls
        for link in outbound_links
    ):
        weak_signals.append("trusted_host_overlap")

    if len(set(weak_signals)) >= 2:
        return (True, "weak:" + "+".join(sorted(set(weak_signals))[:2]))
    return (False, "blocked:insufficient_identity")


_INSTAGRAM_MIN_PROFILE_HTML_CHARS = 48
_INSTAGRAM_REQUIRED_SELECTOR = 'meta[property="og:description"]'
_INSTAGRAM_RENDER_READY_TIMEOUT_MS = 2500
_INSTAGRAM_PROFILE_SURFACE_READY_ATTEMPTS = 3
_INSTAGRAM_PROFILE_SURFACE_MARKER_SELECTOR = "header, section, article, a[href], button, img, h1, h2, ul li"
_INSTAGRAM_PROFILE_SURFACE_CONTENT_SELECTOR = "a[href], button, img, h1, h2, ul li"
_INSTAGRAM_PROFILE_SURFACE_STRUCTURE_SELECTOR = "header, section, article"
_INSTAGRAM_PROFILE_SURFACE_FALLBACK_ROOT_TAGS = frozenset({"section", "article", "div"})
_INSTAGRAM_PROFILE_SURFACE_EXCLUDED_ANCESTOR_TAGS = frozenset({"nav", "footer", "aside", "form", "dialog"})
_INSTAGRAM_PROFILE_SURFACE_MIN_TEXT_LENGTH = 16
_INSTAGRAM_PROFILE_SURFACE_MIN_DESCENDANTS = 4
_INSTAGRAM_PROFILE_SURFACE_MAX_HEADER_ANCESTOR_HOPS = 5
_INSTAGRAM_RENDER_READY_JS = """
() => {
  const main = document.querySelector('main');
  if (main) {
    const profileStructure = main.querySelector('header, section, article');
    const profileContent = main.querySelector('a[href], button, img, h1, h2, ul li');
    const text = (main.innerText || '').replace(/\\s+/g, ' ').trim();
    if (profileStructure && profileContent && text.length >= 16) {
      return 'profile_surface';
    }
  }
  return false;
}
"""
_INSTAGRAM_PROFILE_SURFACE_READY_PROBE_JS = """
() => {
  const PROFILE_MARKER_SELECTOR = 'header, section, article, a[href], button, img, h1, h2, ul li';
  const PROFILE_CONTENT_SELECTOR = 'a[href], button, img, h1, h2, ul li';
  const PROFILE_STRUCTURE_SELECTOR = 'header, section, article';
  const EXCLUDED_ANCESTOR_SELECTOR = 'nav, footer, aside, form, dialog';
  const ROOT_TAGS = new Set(['section', 'article', 'div']);
  const MIN_TEXT_LENGTH = 16;
  const MIN_DESCENDANTS = 4;
  const MAX_HEADER_ANCESTOR_HOPS = 5;

  const normalizeText = (node) =>
    ((node && (node.innerText || node.textContent)) || '').replace(/\\s+/g, ' ').trim();

  const measureRoot = (root) => {
    const header = root ? root.querySelector('header') : null;
    const profileMarkers = root ? root.querySelectorAll(PROFILE_MARKER_SELECTOR).length : 0;
    const descendants = root ? root.querySelectorAll('*').length : 0;
    const text = normalizeText(root);
    return {
      header: header ? 1 : 0,
      profile_markers: profileMarkers,
      descendants,
      text_length: text.length,
    };
  };

  const isExcluded = (node) =>
    !!(node && typeof node.closest === 'function' && node.closest(EXCLUDED_ANCESTOR_SELECTOR));

  const isEligibleFallbackRoot = (root) => {
    if (!root || isExcluded(root)) {
      return false;
    }
    const tagName = (root.tagName || '').toLowerCase();
    if (!ROOT_TAGS.has(tagName)) {
      return false;
    }
    const metrics = measureRoot(root);
    if (metrics.descendants < MIN_DESCENDANTS || metrics.text_length < MIN_TEXT_LENGTH) {
      return false;
    }
    const hasStructure = !!root.querySelector(PROFILE_STRUCTURE_SELECTOR);
    const hasContent = !!root.querySelector(PROFILE_CONTENT_SELECTOR);
    return hasStructure && hasContent && (metrics.header > 0 || metrics.profile_markers >= 4);
  };

  const resolveProfileRoot = () => {
    const main = document.querySelector('main');
    if (main) {
      return main;
    }

    const candidates = [];
    const seen = new Set();
    for (const header of Array.from(document.querySelectorAll('header'))) {
      if (isExcluded(header)) {
        continue;
      }
      let current = header.parentElement;
      let hops = 0;
      while (current && hops < MAX_HEADER_ANCESTOR_HOPS) {
        if (isEligibleFallbackRoot(current)) {
          if (!seen.has(current)) {
            seen.add(current);
            candidates.push(current);
          }
          break;
        }
        current = current.parentElement;
        hops += 1;
      }
    }

    if (!candidates.length) {
      for (const node of Array.from(document.querySelectorAll(PROFILE_STRUCTURE_SELECTOR))) {
        const current = node;
        if (!isEligibleFallbackRoot(current)) {
          continue;
        }
        if (!seen.has(current)) {
          seen.add(current);
          candidates.push(current);
        }
      }
    }

    if (!candidates.length) {
      return null;
    }

    candidates.sort((left, right) => {
      const leftMetrics = measureRoot(left);
      const rightMetrics = measureRoot(right);
      if (leftMetrics.descendants !== rightMetrics.descendants) {
        return leftMetrics.descendants - rightMetrics.descendants;
      }
      if (leftMetrics.profile_markers !== rightMetrics.profile_markers) {
        return rightMetrics.profile_markers - leftMetrics.profile_markers;
      }
      return rightMetrics.text_length - leftMetrics.text_length;
    });
    return candidates[0];
  };

  const main = resolveProfileRoot();
  const metrics = measureRoot(main);
  return {
    main: main ? 1 : 0,
    header: metrics.header,
    profile_markers: metrics.profile_markers,
    descendants: metrics.descendants,
    text_length: metrics.text_length,
    ready: main && metrics.header > 0 && metrics.profile_markers > 0 && metrics.descendants > 0 && metrics.text_length >= 16 ? 1 : 0,
  };
}
"""
_INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS = """
() => {
  const main = document.querySelector('main');
  if (!main) {
    return false;
  }
  const profileStructure = main.querySelector('header, section, article');
  const profileContent = main.querySelector('a[href], button, img, h1, h2, ul li');
  if (profileStructure || profileContent) {
    return 'profile_surface_candidate';
  }
  return false;
}
"""
_CONTACT_SURFACE_EXTRA_ATTR_NAMES = {"content", "title", "aria-label", "alt", "value"}


def _instagram_profile_fetch_usable(status: Optional[int], html: str) -> bool:
    html_text = html if isinstance(html, str) else str(html or "")
    if status != 200:
        return False
    if not html_text or not html_text.strip():
        return False
    if len(html_text.strip()) < _INSTAGRAM_MIN_PROFILE_HTML_CHARS:
        return False
    if _detect_soft_block(html_text):
        return False
    return True


def _instagram_profile_render_ready_marker(page: Any) -> str:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return ""
    marker_value = evaluate(_INSTAGRAM_RENDER_READY_JS)
    if isinstance(marker_value, str):
        return marker_value.strip()
    if marker_value:
        return cell_to_str(marker_value).strip()
    return ""


def _wait_for_instagram_profile_render(page: Any, timeout_s: float) -> bool:
    if page is None:
        return False
    timeout_ms = max(int(float(timeout_s or 0) * 1000), 0)
    if timeout_ms <= 0:
        return False
    wait_for_function = getattr(page, "wait_for_function", None)
    if callable(wait_for_function):
        wait_for_function(_INSTAGRAM_RENDER_READY_JS, timeout=timeout_ms)
        return _instagram_profile_render_ready_marker(page) == "profile_surface"
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        marker = _instagram_profile_render_ready_marker(page)
        if marker == "profile_surface":
            return True
        remaining_ms = int(max((deadline - time.monotonic()) * 1000, 0))
        if remaining_ms <= 0:
            return False
        sleep_ms = min(100, remaining_ms)
        if callable(wait_for_timeout):
            wait_for_timeout(sleep_ms)
        else:
            time.sleep(sleep_ms / 1000.0)
    return False


def _instagram_landed_page_is_plausible_profile_surface(page: Any) -> bool:
    current_url = cell_to_str(getattr(page, "url", "")).strip()
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return not current_url
    try:
        marker_value = evaluate(_INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS)
    except Exception:
        return not current_url
    if isinstance(marker_value, str):
        marker = marker_value.strip()
    elif marker_value:
        marker = cell_to_str(marker_value).strip()
    else:
        marker = ""
    if marker == "profile_surface_candidate":
        return True
    return not current_url


def _probe_instagram_profile_surface_state(page: Any) -> Dict[str, int]:
    state = {
        "main": 0,
        "header": 0,
        "profile_markers": 0,
        "descendants": 0,
        "text_length": 0,
        "ready": 0,
    }
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return state
    try:
        raw_state = evaluate(_INSTAGRAM_PROFILE_SURFACE_READY_PROBE_JS)
    except Exception:
        return state
    if not isinstance(raw_state, dict):
        return state
    for key in state:
        try:
            state[key] = int(raw_state.get(key, 0) or 0)
        except Exception:
            state[key] = 0
    return state


def _instagram_profile_surface_root_metrics(root: Any) -> Dict[str, int]:
    header = root.select_one("header") if root is not None else None
    profile_markers = len(root.select(_INSTAGRAM_PROFILE_SURFACE_MARKER_SELECTOR)) if root is not None else 0
    descendants = len(root.select("*")) if root is not None else 0
    text = (
        re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()
        if root is not None
        else ""
    )
    return {
        "main": 1 if root is not None else 0,
        "header": 1 if header is not None else 0,
        "profile_markers": profile_markers,
        "descendants": descendants,
        "text_length": len(text),
        "ready": (
            1
            if root is not None
            and header is not None
            and profile_markers > 0
            and descendants > 0
            and len(text) >= _INSTAGRAM_PROFILE_SURFACE_MIN_TEXT_LENGTH
            else 0
        ),
    }


def _instagram_profile_surface_node_is_excluded(node: Any) -> bool:
    current = node
    while current is not None:
        name = cell_to_str(getattr(current, "name", "")).strip().lower()
        if name in _INSTAGRAM_PROFILE_SURFACE_EXCLUDED_ANCESTOR_TAGS:
            return True
        current = getattr(current, "parent", None)
    return False


def _instagram_profile_surface_root_is_eligible(root: Any) -> bool:
    if root is None or _instagram_profile_surface_node_is_excluded(root):
        return False
    if cell_to_str(getattr(root, "name", "")).strip().lower() not in _INSTAGRAM_PROFILE_SURFACE_FALLBACK_ROOT_TAGS:
        return False
    metrics = _instagram_profile_surface_root_metrics(root)
    if (
        metrics["descendants"] < _INSTAGRAM_PROFILE_SURFACE_MIN_DESCENDANTS
        or metrics["text_length"] < _INSTAGRAM_PROFILE_SURFACE_MIN_TEXT_LENGTH
    ):
        return False
    has_structure = root.select_one(_INSTAGRAM_PROFILE_SURFACE_STRUCTURE_SELECTOR) is not None
    has_content = root.select_one(_INSTAGRAM_PROFILE_SURFACE_CONTENT_SELECTOR) is not None
    return has_structure and has_content and (metrics["header"] > 0 or metrics["profile_markers"] >= 4)


def _resolve_instagram_profile_surface_root(soup: BeautifulSoup) -> Any:
    main = soup.select_one("main")
    if main is not None:
        return main

    candidates: List[Any] = []
    seen: Set[int] = set()

    for header in soup.select("header"):
        if _instagram_profile_surface_node_is_excluded(header):
            continue
        current = getattr(header, "parent", None)
        hops = 0
        while current is not None and hops < _INSTAGRAM_PROFILE_SURFACE_MAX_HEADER_ANCESTOR_HOPS:
            candidate_id = id(current)
            if candidate_id not in seen and _instagram_profile_surface_root_is_eligible(current):
                candidates.append(current)
                seen.add(candidate_id)
                break
            current = getattr(current, "parent", None)
            hops += 1

    if not candidates:
        for candidate in soup.select(_INSTAGRAM_PROFILE_SURFACE_STRUCTURE_SELECTOR):
            candidate_id = id(candidate)
            if candidate_id in seen or not _instagram_profile_surface_root_is_eligible(candidate):
                continue
            candidates.append(candidate)
            seen.add(candidate_id)

    if not candidates:
        return None

    return min(
        candidates,
        key=lambda candidate: (
            _instagram_profile_surface_root_metrics(candidate)["descendants"],
            -_instagram_profile_surface_root_metrics(candidate)["profile_markers"],
            -_instagram_profile_surface_root_metrics(candidate)["text_length"],
        ),
    )


def _instagram_profile_surface_state_from_html(html: str) -> Dict[str, int]:
    state = {
        "main": 0,
        "header": 0,
        "profile_markers": 0,
        "descendants": 0,
        "text_length": 0,
        "ready": 0,
    }
    html_text = html if isinstance(html, str) else str(html or "")
    if not html_text.strip():
        return state
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return state
    root = _resolve_instagram_profile_surface_root(soup)
    state.update(_instagram_profile_surface_root_metrics(root))
    return state


def _instagram_bridge_surface_assessment(
    page: Any,
    profile_url: str,
    *,
    allow_html_fallback: bool = False,
) -> Dict[str, Any]:
    current_url = cell_to_str(getattr(page, "url", "")).strip()
    current_title = ""
    current_html = ""
    current_body_text = ""

    try:
        current_title = cell_to_str(page.title())
    except Exception:
        current_title = ""
    try:
        current_html = cell_to_str(page.content())
    except Exception:
        current_html = ""
    try:
        current_body_text = cell_to_str(
            page.evaluate("(document.body && (document.body.innerText || document.body.textContent)) || ''")
        )
    except Exception:
        current_body_text = ""

    state = _probe_instagram_profile_surface_state(page)
    if allow_html_fallback:
        html_state = _instagram_profile_surface_state_from_html(current_html)
        for key in state:
            state[key] = max(int(state.get(key, 0) or 0), int(html_state.get(key, 0) or 0))

    target_canonical = _canonicalize_instagram_profile_url(profile_url)
    current_canonical = _canonicalize_instagram_profile_url(current_url)
    target_handle = _instagram_profile_handle_token_from_url(target_canonical or profile_url)
    current_profile_handle = _instagram_profile_handle_token_from_url(
        current_url,
        allow_routed_subpaths=True,
    )
    same_profile = bool(target_canonical and current_canonical and target_canonical == current_canonical)
    unknown_profile_url = not current_url
    same_profile_routed = bool(
        target_handle
        and current_profile_handle
        and target_handle == current_profile_handle
    )

    url_lower = current_url.lower()
    text_lower = " ".join([current_title, current_body_text, current_html]).lower()
    hard_blocked = False
    if any(token in url_lower for token in ("/accounts/login", "/challenge", "/checkpoint", "/consent")):
        hard_blocked = True
    elif _detect_soft_block(current_html):
        hard_blocked = True
    elif any(
        token in text_lower
        for token in (
            "security check",
            "challenge_required",
            "checkpoint",
            "account suspended",
        )
    ):
        hard_blocked = True

    login_text_shell = any(
        token in text_lower
        for token in (
            "log in to instagram",
            "login • instagram",
            "sign up to see photos and videos",
            "see instagram photos and videos from",
        )
    )
    unavailable_text_shell = any(
        token in text_lower
        for token in (
            "page isn't available",
            "sorry, this page isn't available",
        )
    )
    recoverable_shell_indicators = login_text_shell or unavailable_text_shell
    recoverable_logged_out_shell = False
    empty_live_probe = None
    profile_identity_markers = None
    if not hard_blocked and recoverable_shell_indicators and target_handle and (same_profile or same_profile_routed):
        empty_live_probe = (
            state["main"] <= 0
            and state["header"] <= 0
            and state["descendants"] <= 0
            and state["text_length"] <= 0
            and state["profile_markers"] <= 0
        )
        profile_identity_markers = 0
        if f"@{target_handle}" in text_lower or f"(@{target_handle})" in text_lower:
            profile_identity_markers += 1
        if "instagram photos and videos" in text_lower:
            profile_identity_markers += 1
        if "\"@type\":\"profilepage\"" in text_lower or "\"@type\": \"profilepage\"" in text_lower:
            profile_identity_markers += 1
        if (
            re.search(r"\b\d[\d,]*\s+followers?\b", text_lower)
            and re.search(r"\b\d[\d,]*\s+following\b", text_lower)
            and re.search(r"\b\d[\d,]*\s+posts?\b", text_lower)
        ):
            profile_identity_markers += 1
        recoverable_logged_out_shell = empty_live_probe and profile_identity_markers >= 2

    blocked = hard_blocked or (recoverable_shell_indicators and not recoverable_logged_out_shell)
    print("[IG DEBUG FINAL]", {
        "recoverable_logged_out_shell": recoverable_logged_out_shell,
        "blocked": blocked,
        "empty_live_probe": empty_live_probe,
        "profile_identity_markers": profile_identity_markers,
        "same_profile": same_profile,
        "same_profile_routed": same_profile_routed,
    })

    has_header_or_bio = state["header"] > 0 or state["profile_markers"] >= 2
    has_meaningful_descendants = state["descendants"] >= 4 or state["profile_markers"] >= 3
    has_non_trivial_text = state["text_length"] >= 16
    plausible_surface = _instagram_landed_page_is_plausible_profile_surface(page) or _instagram_landed_page_is_html_handoff_usable(
        profile_url,
        current_url,
        current_html,
    )
    relaxed_ready = (
        not blocked
        and state["main"] > 0
        and has_non_trivial_text
        and has_header_or_bio
        and has_meaningful_descendants
    )
    profile_shell = (
        not blocked
        and not relaxed_ready
        and (same_profile or unknown_profile_url)
        and (state["main"] > 0 or plausible_surface or current_canonical == target_canonical)
    )
    promoted_shell = (
        not blocked
        and not relaxed_ready
        and state["main"] > 0
        and state["profile_markers"] >= 2
        and state["descendants"] >= 2
        and state["text_length"] >= 12
        and has_header_or_bio
        and (same_profile or same_profile_routed or (unknown_profile_url and plausible_surface))
    )

    reason = "not_profile_surface"
    if blocked:
        reason = "blocked_page"
    elif relaxed_ready:
        reason = "profile_surface"
    elif promoted_shell:
        reason = "profile_shell"
    elif recoverable_logged_out_shell:
        reason = "recoverable_logged_out_shell"
    elif profile_shell:
        reason = "profile_shell"
    elif plausible_surface:
        reason = "profile_surface_candidate"
    elif same_profile or unknown_profile_url:
        reason = "profile_shell"

    return {
        "current_url": current_url,
        "current_title": current_title,
        "current_html": current_html,
        "current_body_text": current_body_text,
        "same_profile": same_profile,
        "same_profile_routed": same_profile_routed,
        "unknown_profile_url": unknown_profile_url,
        "blocked": blocked,
        "plausible_surface": plausible_surface,
        "profile_shell": profile_shell,
        "promoted_shell": promoted_shell,
        "recoverable_logged_out_shell": recoverable_logged_out_shell,
        "empty_live_probe": empty_live_probe,
        "profile_identity_markers": profile_identity_markers,
        "ready": relaxed_ready,
        "reason": reason,
        "allow_retry": not blocked and reason in {"profile_shell", "profile_surface_candidate", "recoverable_logged_out_shell"},
        "allow_reload": not blocked and (
            (
                reason == "profile_shell"
                and (same_profile or same_profile_routed or unknown_profile_url)
            )
            or (
                recoverable_logged_out_shell
                and (same_profile or same_profile_routed or unknown_profile_url)
            )
        ),
        "state": state,
        "main": state["main"],
        "header": state["header"],
        "descendants": state["descendants"],
        "text_length": state["text_length"],
    }


def _wait_for_instagram_live_profile_surface(
    page: Any,
    profile_url: str,
    *,
    timeout_s: float,
) -> Tuple[bool, str]:
    attempt_timeout_s = min(max(float(timeout_s or 0), 0.0), _INSTAGRAM_RENDER_READY_TIMEOUT_MS / 1000.0)
    recovery_active = False

    def _log_surface_check(assessment: Dict[str, Any]) -> None:
        print(f"[IG Bridge] attempt={_log_surface_check.attempt} url={assessment['current_url'] or profile_url}")
        print(
            f"[IG Bridge] surface_check ready={1 if assessment['ready'] else 0} "
            f"reason={assessment['reason']} main={assessment['main']} header={assessment['header']} "
            f"descendants={assessment['descendants']} text={assessment['text_length']}"
        )

    _log_surface_check.attempt = 1  # type: ignore[attr-defined]

    def _log_recoverable_shell(assessment: Dict[str, Any]) -> None:
        nonlocal recovery_active
        if not assessment.get("recoverable_logged_out_shell"):
            return
        recovery_active = True
        recovery_scope = "same_profile"
        if assessment.get("same_profile_routed"):
            recovery_scope = "same_profile_routed"
        elif assessment.get("unknown_profile_url"):
            recovery_scope = "unknown_profile"
        print(
            f"[IG Bridge] recoverable_shell kind=logged_out_ssr "
            f"scope={recovery_scope} url={assessment['current_url'] or profile_url}"
        )

    assessment = _instagram_bridge_surface_assessment(page, profile_url, allow_html_fallback=False)
    _log_surface_check(assessment)
    _log_recoverable_shell(assessment)
    if assessment["ready"] or assessment.get("promoted_shell"):
        if assessment.get("promoted_shell") and not assessment["ready"]:
            print("[IG Bridge] action=promote_profile_shell")
        print(f"[IG Bridge] success attempt=1 final_url={assessment['current_url'] or profile_url}")
        return (True, "profile_surface")
    if assessment["blocked"]:
        print("[IG Bridge] failure reason=not_profile_surface")
        return (False, "not_profile_surface")

    if assessment["allow_retry"]:
        print("[IG Bridge] action=retry_surface_check")
        if assessment.get("recoverable_logged_out_shell"):
            print("[IG Bridge] recovery_attempt attempt=1 kind=logged_out_ssr action=extra_wait")
        try:
            _wait_for_instagram_profile_render(page, attempt_timeout_s)
        except Exception:
            pass
        _log_surface_check.attempt = 2  # type: ignore[attr-defined]
        assessment = _instagram_bridge_surface_assessment(page, profile_url, allow_html_fallback=True)
        _log_surface_check(assessment)
        _log_recoverable_shell(assessment)
        if assessment["ready"] or assessment.get("promoted_shell"):
            if assessment.get("promoted_shell") and not assessment["ready"]:
                print("[IG Bridge] action=promote_profile_shell")
            if recovery_active:
                print("[IG Bridge] recovery_success attempt=2 kind=logged_out_ssr")
            print(f"[IG Bridge] success attempt=2 final_url={assessment['current_url'] or profile_url}")
            return (True, "profile_surface")
        if assessment["blocked"]:
            if recovery_active:
                print("[IG Bridge] recovery_exhausted kind=logged_out_ssr final_reason=not_profile_surface")
            print("[IG Bridge] failure reason=not_profile_surface")
            return (False, "not_profile_surface")

    if assessment["allow_reload"]:
        print("[IG Bridge] action=reload_same_profile reason=profile_shell")
        if recovery_active:
            print("[IG Bridge] recovery_attempt attempt=2 kind=logged_out_ssr action=reload_same_profile")
        page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        _log_surface_check.attempt = 3  # type: ignore[attr-defined]
        assessment = _instagram_bridge_surface_assessment(page, profile_url, allow_html_fallback=True)
        _log_surface_check(assessment)
        _log_recoverable_shell(assessment)
        if assessment["ready"] or assessment.get("promoted_shell"):
            if assessment.get("promoted_shell") and not assessment["ready"]:
                print("[IG Bridge] action=promote_profile_shell")
            if recovery_active:
                print("[IG Bridge] recovery_success attempt=3 kind=logged_out_ssr")
            print(f"[IG Bridge] success attempt=3 final_url={assessment['current_url'] or profile_url}")
            return (True, "profile_surface")

    if recovery_active:
        print("[IG Bridge] recovery_exhausted kind=logged_out_ssr final_reason=not_profile_surface")
    print("[IG Bridge] failure reason=not_profile_surface")
    return (False, "not_profile_surface")


def _instagram_landed_page_is_html_handoff_usable(
    target_url: str,
    landed_url: str,
    html: str,
) -> bool:
    target_canonical = _canonicalize_instagram_profile_url(target_url)
    landed_canonical = _canonicalize_instagram_profile_url(landed_url)
    if not target_canonical or landed_canonical != target_canonical:
        return False
    html_text = html if isinstance(html, str) else str(html or "")
    if not _instagram_profile_requests_html_usable(200, html_text):
        return False
    soup = BeautifulSoup(html_text, "html.parser")
    if _extract_instagram_direct_profile_candidate_emails(html_text, soup=soup):
        return True

    scoped_html = _extract_instagram_onehop_profile_surface_html(html_text)
    if not scoped_html.strip():
        return False
    scoped_soup = BeautifulSoup(scoped_html, "html.parser")
    scoped_main = scoped_soup.find("main")
    if scoped_main is None:
        return False
    profile_structure = scoped_main.select_one("header, section, article")
    profile_content = scoped_main.select_one("a[href], button, img, h1, h2, ul li")
    scoped_text = re.sub(r"\s+", " ", scoped_main.get_text(" ", strip=True)).strip()
    if profile_structure is None or profile_content is None or len(scoped_text) < 16:
        return False
    return bool(
        _collect_instagram_bio_link_fetch_urls(
            scoped_html,
            soup=scoped_soup,
            profile_url=target_url,
        )
    )


def _append_contact_surface_value(values: List[str], seen: Set[str], raw_value: Any) -> None:
    value = cell_to_str(raw_value)
    if not value or value in seen:
        return
    seen.add(value)
    values.append(value)


def _collect_contact_surface_attribute_values(soup_obj: BeautifulSoup) -> List[str]:
    values: List[str] = []
    seen: Set[str] = set()
    for tag in soup_obj.find_all(True):
        attrs = getattr(tag, "attrs", {}) or {}
        for attr_name, raw_value in attrs.items():
            attr_key = cell_to_str(attr_name).strip().lower()
            if not attr_key:
                continue
            if attr_key == "href":
                continue
            if attr_key not in _CONTACT_SURFACE_EXTRA_ATTR_NAMES and not attr_key.startswith("data-"):
                continue
            if isinstance(raw_value, (list, tuple, set)):
                for item in raw_value:
                    _append_contact_surface_value(values, seen, item)
            else:
                _append_contact_surface_value(values, seen, raw_value)
    return values


def _extract_static_page_candidate_emails(
    html: str,
    *,
    soup: Optional[BeautifulSoup] = None,
    normalize_surface_text: bool = False,
) -> List[str]:
    html_text = html if isinstance(html, str) else str(html or "")
    if not html_text.strip():
        return []
    soup_obj = soup or BeautifulSoup(html_text, "html.parser")
    anchor_values: List[str] = []
    anchor_seen: Set[str] = set()
    for anchor in soup_obj.select("a[href]"):
        _append_contact_surface_value(anchor_values, anchor_seen, anchor.get("href"))

    surface_texts: List[str] = []
    meta_seen: Set[str] = set()
    for meta_tag in soup_obj.select('meta[property="og:description"], meta[name="description"]'):
        _append_contact_surface_value(surface_texts, meta_seen, meta_tag.get("content"))
    for title_tag in soup_obj.select("title"):
        _append_contact_surface_value(surface_texts, meta_seen, title_tag.get_text(" ", strip=True))
    rendered_surface_text = " ".join(surface_texts)
    if normalize_surface_text:
        rendered_surface_text = unicodedata.normalize("NFKC", rendered_surface_text)

    ig_emails, _ = _extract_emails_from_html(
        html_text,
        soup=soup_obj,
        rendered_text=rendered_surface_text,
        anchor_values=anchor_values,
    )

    attribute_values = _collect_contact_surface_attribute_values(soup_obj)
    attribute_emails: List[str] = []
    if attribute_values:
        rendered_attribute_text = " ".join(attribute_values)
        if normalize_surface_text:
            rendered_attribute_text = unicodedata.normalize("NFKC", rendered_attribute_text)
        attribute_emails, _ = _extract_emails_from_html(
            "",
            rendered_text=rendered_attribute_text,
            anchor_values=attribute_values,
        )

    return filter_system_telemetry_emails([*ig_emails, *attribute_emails])


def _extract_instagram_profile_candidate_emails(
    html: str,
    *,
    soup: Optional[BeautifulSoup] = None,
) -> List[str]:
    return _extract_static_page_candidate_emails(html, soup=soup)


def _extract_instagram_direct_profile_candidate_emails(
    html: str,
    *,
    soup: Optional[BeautifulSoup] = None,
) -> List[str]:
    return _extract_static_page_candidate_emails(
        html,
        soup=soup,
        normalize_surface_text=True,
    )


_INSTAGRAM_EMAIL_ARTIFACT_FILE_EXTENSIONS = frozenset({"js", "css", "map"})


def _instagram_email_candidate_rejection_reason(email: str) -> str:
    normalized = normalize_email_value(email)
    if not normalized:
        return "invalid"
    if is_obvious_placeholder_email(normalized):
        return "placeholder"
    _, domain = normalized.split("@", 1)
    domain_labels = [label for label in domain.strip(".").split(".") if label]
    if domain_labels and domain_labels[-1] in _INSTAGRAM_EMAIL_ARTIFACT_FILE_EXTENSIONS:
        return "asset_artifact"
    return ""


def _filter_instagram_email_candidates_for_acceptance(
    emails: Iterable[str],
    *,
    log: Optional[Any] = None,
) -> List[str]:
    filtered: List[str] = []
    for email in filter_platform_support_emails(filter_system_telemetry_emails(emails)):
        reason = _instagram_email_candidate_rejection_reason(email)
        if reason:
            if callable(log):
                log(f"[IG Email] rejected_email_candidate reason={reason} value={email}")
            continue
        filtered.append(email)
    return filtered


def _normalise_instagram_bio_link_fetch_url(raw: str, *, base_url: str = "") -> str:
    candidate = cell_to_str(raw)
    if not candidate:
        return ""
    lowered = candidate.strip().lower()
    if lowered.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
        return ""
    resolved = normalize_external_url(candidate) or candidate
    try:
        if base_url:
            resolved = urllib.parse.urljoin(base_url, resolved)
    except Exception:
        return ""
    normalised = _normalise_url(resolved)
    if not normalised or _is_noise_url(normalised):
        return ""
    try:
        parsed = urllib.parse.urlparse(normalised)
    except Exception:
        return ""
    if (parsed.scheme or "").lower() not in {"http", "https"}:
        return ""
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or host in {"instagram.com", "instagr.am"}:
        return ""
    return normalised


_INSTAGRAM_BIO_LINK_LOG_SAMPLE_MAX = 2
_INSTAGRAM_BIO_LINK_LOG_SAMPLE_CHARS = 96


def _classify_instagram_bio_link_fetch_url_drop(raw: Any, *, base_url: str = "") -> str:
    candidate = cell_to_str(raw)
    if not candidate or not candidate.strip():
        return "empty_or_invalid"
    lowered = candidate.strip().lower()
    if lowered.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
        return "non_http"
    resolved = normalize_external_url(candidate) or candidate
    try:
        if base_url:
            resolved = urllib.parse.urljoin(base_url, resolved)
    except Exception:
        return "malformed"
    normalised = _normalise_url(resolved)
    if not normalised or _is_noise_url(normalised):
        return "empty_or_invalid"
    try:
        parsed = urllib.parse.urlparse(normalised)
    except Exception:
        return "malformed"
    if (parsed.scheme or "").lower() not in {"http", "https"}:
        return "non_http"
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "empty_or_invalid"
    if host in {"instagram.com", "instagr.am"}:
        return "self_instagram"
    return "empty_or_invalid"


def _instagram_bio_link_log_sample(values: Iterable[Any], *, limit: int = _INSTAGRAM_BIO_LINK_LOG_SAMPLE_MAX) -> str:
    sample: List[str] = []
    for raw_value in values:
        value = cell_to_str(raw_value).strip()
        if not value:
            continue
        if len(value) > _INSTAGRAM_BIO_LINK_LOG_SAMPLE_CHARS:
            value = value[: _INSTAGRAM_BIO_LINK_LOG_SAMPLE_CHARS - 3] + "..."
        sample.append(value)
        if len(sample) >= limit:
            break
    return ",".join(sample) if sample else "-"


def _format_instagram_bio_link_drop_reasons(counts: Counter) -> str:
    if not counts:
        return "-"
    return ",".join(f"{reason}:{count}" for reason, count in counts.most_common(3))


def _instagram_truth_profile_key(raw_url: Any) -> str:
    url = cell_to_str(raw_url).strip()
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return url.rstrip("/").lower()
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/").lower()
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))


def _instagram_truth_logging_enabled(profile_url: str = "") -> bool:
    enabled = cell_to_str(os.getenv("IG_TRUTH_CAPTURE", "")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return False
    target_url = cell_to_str(os.getenv("IG_TRUTH_TARGET_URL", "")).strip()
    if not target_url:
        return True
    return _instagram_truth_profile_key(profile_url) == _instagram_truth_profile_key(target_url)


def _emit_instagram_truth(message: str, *, emit: Optional[Any] = None) -> None:
    if not message:
        return
    if callable(emit):
        try:
            emit(message)
            return
        except Exception:
            pass
    try:
        print(message)
    except Exception:
        pass


_INSTAGRAM_ONEHOP_LINK_HUB_HOSTS = frozenset(
    set(LINK_HUB_HOSTS)
    | {
        "direct.me",
        "hype.co",
        "stan.store",
    }
)
_INSTAGRAM_ONEHOP_MUSIC_SERVICE_HOSTS = frozenset(
    {
        "bandcamp.com",
        "ffm.to",
        "lnk.to",
        "music.apple.com",
        "open.spotify.com",
        "push.fm",
        "songwhip.com",
        "soundcloud.com",
        "spotify.com",
        "youtu.be",
        "youtube.com",
    }
)
_INSTAGRAM_ONEHOP_LOW_VALUE_SOCIAL_HOSTS = frozenset(
    {
        "facebook.com",
        "fb.me",
        "linkedin.com",
        "m.facebook.com",
        "pinterest.com",
        "threads.net",
        "tiktok.com",
        "twitter.com",
        "x.com",
    }
)
_INSTAGRAM_ONEHOP_BLOCKED_HOSTS = frozenset(
    {
        "about.instagram.com",
        "about.meta.com",
        "cdninstagram.com",
        "help.instagram.com",
        "instagram.com",
        "instagr.am",
        "meta.com",
        "privacycenter.instagram.com",
        "static.cdninstagram.com",
    }
)
_INSTAGRAM_ONEHOP_BLOCKED_HOST_SUFFIXES = (
    "cdninstagram.com",
    "meta.com",
)
_INSTAGRAM_ONEHOP_BLOCKED_FACEBOOK_PATH_SEGMENTS = frozenset(
    {
        "about",
        "business",
        "help",
        "legal",
        "meta",
        "policies",
        "policy",
        "privacy",
        "safety",
        "security",
        "settings",
        "terms",
    }
)
_INSTAGRAM_ONEHOP_STATIC_EXTENSIONS = frozenset(
    NOISE_FILE_EXTENSIONS
    | {
        ".css",
        ".eot",
        ".ico",
        ".js",
        ".json",
        ".map",
        ".mjs",
        ".otf",
        ".ttf",
        ".webmanifest",
        ".woff",
        ".woff2",
    }
)
_INSTAGRAM_ONEHOP_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "igsh",
        "si",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)


def _instagram_onehop_host_matches(host: str, domain: str) -> bool:
    host_value = cell_to_str(host).strip().lower()
    domain_value = cell_to_str(domain).strip().lower()
    if not host_value or not domain_value:
        return False
    return host_value == domain_value or host_value.endswith("." + domain_value)


def _instagram_onehop_host(url: str) -> str:
    host = _host(url)
    if host.startswith("www."):
        host = host[4:]
    return host.split(":", 1)[0]


def _instagram_onehop_is_static_asset_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    path = (parsed.path or "").lower()
    _, ext = os.path.splitext(path)
    if ext and ext in _INSTAGRAM_ONEHOP_STATIC_EXTENSIONS:
        return True
    return any(token in path for token in ("/assets/", "/static/", "/images/", "/fonts/"))


def _instagram_onehop_block_reason(url: str) -> str:
    host = _instagram_onehop_host(url)
    if not host:
        return "invalid"
    if _instagram_onehop_is_static_asset_url(url):
        return "static_asset"
    if host in _INSTAGRAM_ONEHOP_BLOCKED_HOSTS:
        return "internal_meta"
    if any(_instagram_onehop_host_matches(host, suffix) for suffix in _INSTAGRAM_ONEHOP_BLOCKED_HOST_SUFFIXES):
        return "internal_meta"
    if _instagram_onehop_host_matches(host, "facebook.com"):
        try:
            segments = [segment.lower() for segment in urllib.parse.urlparse(url).path.split("/") if segment]
        except Exception:
            segments = []
        if not segments:
            return "internal_meta"
        if segments[0] in _INSTAGRAM_ONEHOP_BLOCKED_FACEBOOK_PATH_SEGMENTS:
            return "internal_meta"
    return ""


def _instagram_onehop_target_specificity(url: str) -> Tuple[int, int, int, str]:
    try:
        parsed = urllib.parse.urlparse(url)
        segments = [
            urllib.parse.unquote(segment).strip().lower()
            for segment in (parsed.path or "").split("/")
            if segment and urllib.parse.unquote(segment).strip()
        ]
    except Exception:
        segments = []

    meaningful_segments = [
        segment
        for segment in segments
        if segment not in {"default", "home", "homepage", "index", "welcome"}
    ]
    generic_surface_penalty = 1 if not meaningful_segments else 0
    specificity_depth_rank = -min(len(meaningful_segments), 3)
    specificity_length_rank = -min(sum(len(segment) for segment in meaningful_segments), 64)
    if meaningful_segments:
        specificity_label = "specific_path"
    elif segments:
        specificity_label = "generic_homepage"
    else:
        specificity_label = "generic_root"
    return (
        generic_surface_penalty,
        specificity_depth_rank,
        specificity_length_rank,
        specificity_label,
    )


def _instagram_onehop_is_utility_info_target(url: str) -> bool:
    host = _instagram_onehop_host(url)
    if not host:
        return False
    if any(_instagram_onehop_host_matches(host, domain) for domain in _INSTAGRAM_ONEHOP_LINK_HUB_HOSTS):
        return False
    if any(_instagram_onehop_host_matches(host, domain) for domain in _INSTAGRAM_ONEHOP_MUSIC_SERVICE_HOSTS):
        return False

    platform_owned_host = (
        _instagram_onehop_host_matches(host, "facebook.com")
        or _instagram_onehop_host_matches(host, "meta.com")
        or _instagram_onehop_host_matches(host, "threads.com")
        or any(_instagram_onehop_host_matches(host, domain) for domain in _INSTAGRAM_ONEHOP_LOW_VALUE_SOCIAL_HOSTS)
    )
    if not platform_owned_host:
        return False

    utility_tokens = {
        "developer",
        "developers",
        "docs",
        "help",
        "legal",
        "policies",
        "policy",
        "privacy",
        "support",
        "terms",
    }
    host_labels = {label for label in host.split(".") if label}
    if host_labels & utility_tokens:
        return True

    try:
        parsed = urllib.parse.urlparse(url)
        path_segments = {
            urllib.parse.unquote(segment).strip().lower()
            for segment in (parsed.path or "").split("/")
            if segment and urllib.parse.unquote(segment).strip()
        }
    except Exception:
        path_segments = set()
    return bool(path_segments & utility_tokens)


def _instagram_onehop_target_tier(url: str) -> Tuple[int, str]:
    host = _instagram_onehop_host(url)
    if any(_instagram_onehop_host_matches(host, domain) for domain in _INSTAGRAM_ONEHOP_LINK_HUB_HOSTS):
        return (0, "linkhub")
    if any(_instagram_onehop_host_matches(host, domain) for domain in _INSTAGRAM_ONEHOP_MUSIC_SERVICE_HOSTS):
        return (2, "music_service")
    if _instagram_onehop_is_utility_info_target(url):
        return (3, "external_info")
    if _instagram_onehop_host_matches(host, "threads.com"):
        return (3, "external_info")
    if any(_instagram_onehop_host_matches(host, domain) for domain in _INSTAGRAM_ONEHOP_LOW_VALUE_SOCIAL_HOSTS):
        return (3, "external_info")
    return (1, "external_domain")


def _instagram_onehop_target_sort_key(url: str) -> Tuple[int, int, int, int, int, int, int, int, str]:
    tier_rank, _ = _instagram_onehop_target_tier(url)
    generic_surface_penalty, specificity_depth_rank, specificity_length_rank, _ = (
        _instagram_onehop_target_specificity(url)
    )
    utility_info_penalty = int(_instagram_onehop_is_utility_info_target(url))
    try:
        parsed = urllib.parse.urlparse(url)
        query_keys = {key.lower() for key in urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)}
    except Exception:
        parsed = urllib.parse.urlparse("")
        query_keys = set()
    tracking_penalty = 1 if any(key.startswith("utm_") or key in _INSTAGRAM_ONEHOP_TRACKING_QUERY_KEYS for key in query_keys) else 0
    query_penalty = 1 if parsed.query else 0
    fragment_penalty = 1 if parsed.fragment else 0
    return (
        generic_surface_penalty,
        utility_info_penalty,
        tier_rank,
        tracking_penalty,
        query_penalty,
        fragment_penalty,
        specificity_depth_rank,
        specificity_length_rank,
        url,
    )


def _select_instagram_onehop_target(
    candidate_urls: Iterable[str],
    *,
    log: Optional[Any] = None,
) -> str:
    ranked_candidates: List[Tuple[Tuple[int, int, int, int, int, int, int, int, str], str, str, str]] = []
    for url in candidate_urls:
        blocked_reason = _instagram_onehop_block_reason(url)
        if blocked_reason:
            if callable(log):
                log(f"[IG OneHop] target_blocked reason={blocked_reason} url={url}")
            continue
        _, _, _, specificity_label = _instagram_onehop_target_specificity(url)
        _, tier_name = _instagram_onehop_target_tier(url)
        ranked_candidates.append((_instagram_onehop_target_sort_key(url), tier_name, url, specificity_label))

    if not ranked_candidates:
        if callable(log):
            log("[IG OneHop] no_useful_target_after_ranking")
        return ""

    ranked_candidates.sort(key=lambda item: item[0])
    _, tier_name, selected_url, selected_specificity = ranked_candidates[0]
    if callable(log):
        log(f"[IG OneHop] ranked_candidates count={len(ranked_candidates)}")
        for rank, (_, candidate_tier, candidate_url, candidate_specificity) in enumerate(ranked_candidates, start=1):
            generic_root = int(candidate_specificity != "specific_path")
            low_value_platform = int(candidate_tier == "external_info")
            utility_info = int(_instagram_onehop_is_utility_info_target(candidate_url))
            log(
                "[IG OneHop] ranked_candidate "
                f"rank={rank} "
                f"tier={candidate_tier} "
                f"specificity={candidate_specificity} "
                f"generic_root={generic_root} "
                f"low_value_platform={low_value_platform} "
                f"utility_info={utility_info} "
                f"url={candidate_url}"
            )
        log(f"[IG OneHop] ranked_target_selected tier={tier_name} url={selected_url}")
        competing_candidates = ranked_candidates[1:]
        generic_root_demotion = int(
            selected_specificity == "specific_path"
            and any(candidate_specificity != "specific_path" for _, _, _, candidate_specificity in competing_candidates)
        )
        low_value_platform_demotion = int(
            selected_specificity == "specific_path"
            and any(candidate_tier == "external_info" for _, candidate_tier, _, _ in competing_candidates)
        )
        fallback_weak = int(all(candidate_specificity != "specific_path" for _, _, _, candidate_specificity in ranked_candidates))
        log(
            "[IG OneHop] ranked_target_decision "
            f"specificity={selected_specificity} "
            f"generic_root_demotion={generic_root_demotion} "
            f"low_value_platform_demotion={low_value_platform_demotion} "
            f"fallback_weak={fallback_weak} "
            f"url={selected_url}"
        )
    return selected_url


def _instagram_onehop_target_is_meaningful_fetch_target(url: str) -> bool:
    if not url or _instagram_onehop_block_reason(url):
        return False

    _, tier_name = _instagram_onehop_target_tier(url)
    if tier_name in {"linkhub", "music_service", "external_domain"}:
        return True

    _, _, _, specificity_label = _instagram_onehop_target_specificity(url)
    return specificity_label == "specific_path" and not _instagram_onehop_is_utility_info_target(url)


_INSTAGRAM_BIO_LINK_META_KEY_ATTRS = ("property", "name", "itemprop")
_INSTAGRAM_BIO_LINK_META_ALLOW_TOKENS = ("url", "link", "website", "external", "sameas", "same_as")
_INSTAGRAM_BIO_LINK_META_SKIP_TOKENS = ("image", "video", "audio", "icon", "thumbnail", "player")
_INSTAGRAM_BIO_LINK_STRUCTURED_SCRIPT_TYPES = {"application/json", "application/ld+json"}
_INSTAGRAM_BIO_LINK_STRUCTURED_SCRIPT_PREFIXES = ("window._sharedData =",)
_INSTAGRAM_BIO_LINK_STRUCTURED_CONTEXT_KEYS = {
    "bio_links",
    "bio_link",
    "external_links",
    "external_urls",
    "links",
    "sameas",
    "same_as",
    "website_links",
}
_INSTAGRAM_BIO_LINK_STRUCTURED_DIRECT_KEYS = {
    "external_url",
    "external_url_linkshimmed",
    "href",
    "link_url",
    "outbound_url",
    "outgoing_url",
    "web_uri",
    "web_url",
    "website",
    "website_url",
}
_INSTAGRAM_BIO_LINK_STRUCTURED_CONTEXTUAL_KEYS = {"link", "links", "uri", "url"}
_INSTAGRAM_BIO_LINK_STRUCTURED_SKIP_KEY_TOKENS = {
    "avatar",
    "image",
    "img",
    "logo",
    "media",
    "photo",
    "pic",
    "player",
    "profile_pic",
    "thumbnail",
    "video",
}
_INSTAGRAM_BIO_LINK_STRUCTURED_MAX_SCRIPT_CHARS = 250000
_INSTAGRAM_BIO_LINK_STRUCTURED_MAX_NODES = 2048
_INSTAGRAM_DIRECT_BIO_TEXT_KEYS = {"bio", "biography"}
_INSTAGRAM_DIRECT_BIO_TEXT_ENTITY_CONTAINER_KEYS = {"biography_with_entities"}
_INSTAGRAM_DIRECT_BIO_TEXT_ENTITY_VALUE_KEYS = {"raw_text", "text"}
_INSTAGRAM_DIRECT_RUNTIME_TEXT_KEYS = {
    "about",
    "about_text",
    "bio",
    "biography",
    "headline",
}
_INSTAGRAM_DIRECT_RUNTIME_TEXT_ENTITY_CONTAINER_KEYS = {
    "about_with_entities",
    "biography_with_entities",
    "headline_with_entities",
}


def _iter_instagram_bio_link_meta_values(soup_obj: BeautifulSoup) -> Iterable[str]:
    for meta_tag in soup_obj.find_all("meta"):
        key_parts = []
        for attr_name in _INSTAGRAM_BIO_LINK_META_KEY_ATTRS:
            attr_value = cell_to_str(meta_tag.get(attr_name)).strip().lower()
            if attr_value:
                key_parts.append(attr_value)
        if not key_parts:
            continue
        meta_key = " ".join(key_parts)
        if any(token in meta_key for token in _INSTAGRAM_BIO_LINK_META_SKIP_TOKENS):
            continue
        if not any(token in meta_key for token in _INSTAGRAM_BIO_LINK_META_ALLOW_TOKENS):
            continue
        content = cell_to_str(meta_tag.get("content")).strip()
        if content.startswith(("http://", "https://")):
            yield content


def _load_instagram_bio_link_structured_script_payload(script_tag: Any) -> Optional[Any]:
    raw_text = cell_to_str(script_tag.string or script_tag.get_text(" ", strip=False)).strip()
    if not raw_text or len(raw_text) > _INSTAGRAM_BIO_LINK_STRUCTURED_MAX_SCRIPT_CHARS:
        return None

    payload_text = ""
    script_type = cell_to_str(script_tag.get("type")).strip().lower()
    if script_type in _INSTAGRAM_BIO_LINK_STRUCTURED_SCRIPT_TYPES or raw_text[:1] in "{[":
        payload_text = raw_text
    else:
        for prefix in _INSTAGRAM_BIO_LINK_STRUCTURED_SCRIPT_PREFIXES:
            if raw_text.startswith(prefix):
                payload_text = raw_text[len(prefix):].strip()
                break
    payload_text = payload_text.rstrip(";").strip()
    if payload_text[:1] not in "{[":
        return None
    try:
        return json.loads(payload_text)
    except Exception:
        return None


def _instagram_bio_link_structured_key_allows_url(key: str, *, parent_context: bool = False) -> bool:
    if not key:
        return False
    if any(token in key for token in _INSTAGRAM_BIO_LINK_STRUCTURED_SKIP_KEY_TOKENS):
        return False
    if key in _INSTAGRAM_BIO_LINK_STRUCTURED_DIRECT_KEYS:
        return True
    if key in _INSTAGRAM_BIO_LINK_STRUCTURED_CONTEXT_KEYS or "bio_link" in key:
        return True
    if parent_context and key in _INSTAGRAM_BIO_LINK_STRUCTURED_CONTEXTUAL_KEYS:
        return True
    return False


def _instagram_bio_link_structured_key_allows_email(key: str) -> bool:
    if not key:
        return False
    if any(token in key for token in _INSTAGRAM_BIO_LINK_STRUCTURED_SKIP_KEY_TOKENS):
        return False
    return key == "email" or key.endswith("_email")


def _normalise_instagram_bio_link_structured_email(value: Any) -> str:
    candidate = cell_to_str(value).strip()
    if not candidate:
        return ""
    normalized = normalize_email_value(candidate)
    if normalized:
        return normalized
    normalized_candidate, _ = normalize_obfuscated_email_patterns(candidate)
    if normalized_candidate == candidate:
        return ""
    return normalize_email_value(normalized_candidate)


def _iter_instagram_bio_link_structured_values(payload: Any) -> Iterable[str]:
    stack: List[Tuple[Any, bool, bool]] = [(payload, False, False)]
    nodes_seen = 0
    while stack and nodes_seen < _INSTAGRAM_BIO_LINK_STRUCTURED_MAX_NODES:
        current, url_context, email_context = stack.pop()
        nodes_seen += 1
        if isinstance(current, dict):
            for raw_key, value in current.items():
                key = cell_to_str(raw_key).strip().lower()
                child_url_context = _instagram_bio_link_structured_key_allows_url(
                    key,
                    parent_context=url_context,
                ) or url_context
                child_email_context = _instagram_bio_link_structured_key_allows_email(key) or email_context
                if isinstance(value, str):
                    candidate = value.strip()
                    if child_url_context and candidate.startswith(("http://", "https://")):
                        yield candidate
                    elif child_email_context:
                        normalized_email = _normalise_instagram_bio_link_structured_email(candidate)
                        if normalized_email:
                            yield normalized_email
                elif isinstance(value, (dict, list, tuple)):
                    stack.append((value, child_url_context, child_email_context))
        elif isinstance(current, (list, tuple)):
            for item in reversed(current):
                stack.append((item, url_context, email_context))
        elif isinstance(current, str):
            candidate = current.strip()
            if url_context and candidate.startswith(("http://", "https://")):
                yield candidate
            elif email_context:
                normalized_email = _normalise_instagram_bio_link_structured_email(candidate)
                if normalized_email:
                    yield normalized_email


def _collect_instagram_bio_link_structured_emails(payloads: Iterable[Any]) -> List[str]:
    emails: List[str] = []
    seen: Set[str] = set()
    for payload in payloads:
        for raw_value in _iter_instagram_bio_link_structured_values(payload):
            normalized = normalize_email_value(raw_value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            emails.append(normalized)
    return emails


def _collect_instagram_bio_equivalent_structured_texts(payloads: Iterable[Any]) -> List[str]:
    texts: List[str] = []
    seen: Set[str] = set()
    stack: List[Tuple[Any, bool]] = [(payload, False) for payload in payloads]
    nodes_seen = 0
    while stack and nodes_seen < _INSTAGRAM_BIO_LINK_STRUCTURED_MAX_NODES:
        current, bio_text_context = stack.pop()
        nodes_seen += 1
        if isinstance(current, dict):
            for raw_key, value in current.items():
                key = cell_to_str(raw_key).strip().lower()
                child_bio_text_context = (
                    bio_text_context or key in _INSTAGRAM_DIRECT_BIO_TEXT_ENTITY_CONTAINER_KEYS
                )
                if isinstance(value, str):
                    if key in _INSTAGRAM_DIRECT_BIO_TEXT_KEYS or (
                        bio_text_context and key in _INSTAGRAM_DIRECT_BIO_TEXT_ENTITY_VALUE_KEYS
                    ):
                        normalized = unicodedata.normalize("NFKC", value).strip()
                        if normalized and normalized not in seen:
                            seen.add(normalized)
                            texts.append(normalized)
                elif isinstance(value, (dict, list, tuple)):
                    stack.append((value, child_bio_text_context))
        elif isinstance(current, (list, tuple)):
            for item in reversed(current):
                stack.append((item, bio_text_context))
        elif isinstance(current, str) and bio_text_context:
            normalized = unicodedata.normalize("NFKC", current).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                texts.append(normalized)
    return texts


def _collect_instagram_direct_runtime_candidate_strings(payloads: Iterable[Any]) -> List[str]:
    texts: List[str] = []
    seen: Set[str] = set()
    stack: List[Tuple[Any, bool]] = [(payload, False) for payload in payloads]
    nodes_seen = 0
    while stack and nodes_seen < _INSTAGRAM_BIO_LINK_STRUCTURED_MAX_NODES:
        current, text_context = stack.pop()
        nodes_seen += 1
        if isinstance(current, dict):
            for raw_key, value in current.items():
                key = cell_to_str(raw_key).strip().lower()
                child_text_context = (
                    text_context or key in _INSTAGRAM_DIRECT_RUNTIME_TEXT_ENTITY_CONTAINER_KEYS
                )
                if isinstance(value, str):
                    normalized = unicodedata.normalize("NFKC", value).strip()
                    if not normalized:
                        continue
                    if _instagram_bio_link_structured_key_allows_email(key) or (
                        key in _INSTAGRAM_DIRECT_RUNTIME_TEXT_KEYS
                    ) or (
                        text_context and key in _INSTAGRAM_DIRECT_BIO_TEXT_ENTITY_VALUE_KEYS
                    ):
                        if normalized not in seen:
                            seen.add(normalized)
                            texts.append(normalized)
                elif isinstance(value, (dict, list, tuple)):
                    stack.append((value, child_text_context))
        elif isinstance(current, (list, tuple)):
            for item in reversed(current):
                stack.append((item, text_context))
        elif isinstance(current, str) and text_context:
            normalized = unicodedata.normalize("NFKC", current).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                texts.append(normalized)
    return texts


def _extract_instagram_onehop_profile_surface_html(html: str) -> str:
    html_text = html if isinstance(html, str) else str(html or "")
    if not html_text.strip():
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    scoped_root = soup.find("main") or soup.find("body")
    if scoped_root is None:
        return ""
    scoped_soup = BeautifulSoup(str(scoped_root), "html.parser")
    scoped_root_copy = scoped_soup.find(scoped_root.name)
    if scoped_root_copy is None:
        return ""
    for noise_node in scoped_root_copy.select("footer, nav, aside, script, style, noscript"):
        noise_node.decompose()
    return str(scoped_root_copy)


_INSTAGRAM_LIVE_ONEHOP_CLICKABLE_SELECTOR = "a[href], button, [role='button'], [role='link']"
_INSTAGRAM_LIVE_ONEHOP_MAX_SCOPE_ROOTS = 3
_INSTAGRAM_LIVE_ONEHOP_MAX_CLICKABLE_NODES = 24
_INSTAGRAM_LIVE_ONEHOP_MAX_ATTRS_PER_NODE = 24
_INSTAGRAM_LIVE_ONEHOP_INTERACTION_MARKER_ATTR = "data-ig-live-bio-link-recovery"
_INSTAGRAM_LIVE_ONEHOP_INTERACTION_STATE_KEY = "__igLiveBioLinkInteractionRecovery"
_INSTAGRAM_LIVE_ONEHOP_INTERACTION_ONLY_SENTINEL_PREFIX = "__ig_interaction_candidate__:"
_INSTAGRAM_TRUTH_SAMPLE_LIMIT = 10
_INSTAGRAM_TRUTH_RETRY_SNAPSHOTS = 3
_INSTAGRAM_TRUTH_RETRY_WAIT_MS = 200
_INSTAGRAM_LIVE_ONEHOP_REDIRECT_QUERY_KEYS = frozenset(
    {
        "dest",
        "destination",
        "q",
        "redirect",
        "redirect_uri",
        "redirect_url",
        "target",
        "u",
        "url",
    }
)


def _collect_instagram_live_profile_clickable_control_values(page: Any) -> List[str]:
    def _log_detect(message: str) -> None:
        try:
            print(message)
        except Exception:
            pass

    profile_url = cell_to_str(getattr(page, "url", "")).strip() if page is not None else ""
    truth_enabled = _instagram_truth_logging_enabled(profile_url)

    def _log_truth(message: str) -> None:
        if truth_enabled:
            _emit_instagram_truth(message)

    page_present = 1 if page is not None else 0
    evaluate_present = 1 if page is not None and hasattr(page, "evaluate") else 0
    if truth_enabled:
        _log_truth(
            f"[IG Truth] detector_enter profile_url={profile_url or '-'} "
            f"page_present={page_present} evaluate_present={evaluate_present}"
        )

    if page is None or not hasattr(page, "evaluate"):
        _log_detect("[IG Detect] candidates raw=0 kept=0 interaction=0 dropped=0 sample=-")
        _log_detect("[IG Detect] drop_reasons=-")
        _log_detect("[IG Detect] control_values count=0 interaction_only=0 sample=-")
        if truth_enabled:
            _log_truth("[IG Truth] output raw_control_values count=0 sample=-")
        return []

    marker_name_json = json.dumps("ig-live-bio-link-control-surface")
    selector_json = json.dumps(_INSTAGRAM_LIVE_ONEHOP_CLICKABLE_SELECTOR)
    interaction_only_prefix_json = json.dumps(_INSTAGRAM_LIVE_ONEHOP_INTERACTION_ONLY_SENTINEL_PREFIX)
    capture_truth_json = "true" if truth_enabled else "false"
    raw_payload: Any = {}
    truth_sample_limit_json = int(_INSTAGRAM_TRUTH_SAMPLE_LIMIT)

    def _evaluate_payload(*, retry_snapshot_index: int = 0) -> Any:
      return page.evaluate(
            f"""
() => {{
  const marker = {marker_name_json};
  const selector = {selector_json};
  const captureTruth = {capture_truth_json};
  const maxScopeRoots = {_INSTAGRAM_LIVE_ONEHOP_MAX_SCOPE_ROOTS};
  const maxNodes = {_INSTAGRAM_LIVE_ONEHOP_MAX_CLICKABLE_NODES};
  const maxAttrsPerNode = {_INSTAGRAM_LIVE_ONEHOP_MAX_ATTRS_PER_NODE};
  const truthLimit = {truth_sample_limit_json};
  const retrySnapshotIndex = {int(retry_snapshot_index)};
  const maxProbeNodesPerRoot = 80;
  const maxNearbyNodesPerCandidate = 10;
  const maxNearbyAnchorsPerNode = 4;
  const maxNearbyClickablesPerNode = 4;
  const maxRuntimeOwnProps = 16;
  const maxRuntimeObjectDepth = 2;
  const maxRuntimeObjectEntries = 16;
  const maxRuntimeArrayItems = 8;
  const maxSampleLabels = 3;
  const interactionOnlyPrefix = {interaction_only_prefix_json};
  const runtimeValueKeyPattern = /(href|url|uri|link|path|pathname|target|dest|destination|redirect|website|external|outbound|web)/i;
  const runtimeNodePropPattern = /^(__reactProps\\$|__reactEventHandlers\\$|__reactFiber\\$|__reactInternalInstance\\$|__reactContainer\\$)/;
  const urlPattern = /https?:\\/\\/[^\\s'"<>()]+/gi;
  const bareDomainPattern = /\\b(?:www\\.)?(?:[a-z0-9-]+\\.)+(?:com|co|net|org|fm|tv|io|gg|ly|me|bio|to|live|page|link|music|band|store|art|studio|agency|ai|ee)(?:\\/[^\\s'"<>()]*)?/gi;
  const positivePattern = /(https?:\\/\\/|www\\.|(?:[a-z0-9-]+\\.)+(?:com|co|net|org|fm|tv|io|gg|ly|me|bio|to|live|page|link|music|band|store|art|studio|agency|ai|ee)\\b|\\b(link(?:\\s+in\\s+bio)?|bio\\s+link|website|external|official\\s+site)\\b)/i;
  const weakContextPattern = /\\b(bio|website|external|link)\\b/i;
  const tabPattern = /\\b(posts?|reels?|tagged)\\b/i;
  const followerPattern = /\\bfollowers?\\b|\\bfollowing\\b/i;
  const menuPattern = /\\b(menu|settings|options|more)\\b/i;
  const sharePattern = /\\bshare\\b/i;
  const profileActionPattern = /\\b(message|email|call|text|follow)\\b/i;
  const blockedValuePattern = /(?:developers\\.facebook\\.com|about\\.instagram\\.com|about\\.meta\\.com|meta\\.ai|threads\\.com|threads\\.net)/i;
  const relaxedSelector = selector + ", [tabindex], [onclick], [data-link], [data-url], [data-href], [data-target], [data-uri], [data-web-uri], [data-web-destination]";
  const probeSelector = relaxedSelector + ", div, span, section, article, li, p";
  const main = document.querySelector('main');
  if (!main) {{
      return {{
        marker,
        values: [],
        rawCandidateCount: 0,
        keptCandidateCount: 0,
        interactionCandidateCount: 0,
        interactionOnlyValueCount: 0,
        keptLabels: [],
        dropReasons: {{}},
        relaxedKeepSamples: [],
        mainPresent: 0,
        headerPresent: 0,
        scopeRoots: [],
        rootSnapshots: [],
        rawProbeNodeCount: 0,
      dropSamples: [],
      keepSamples: [],
      retrySnapshotIndex,
    }};
  }}
  const cleanText = (value, limit = 280) => {{
    if (value == null) return '';
    return String(value).replace(/\\s+/g, ' ').trim().slice(0, limit);
  }};
  const isExplicitlyHidden = (el) => {{
    if (!el) return true;
    if (el.closest('[hidden], [aria-hidden="true"]')) return true;
    const style = window.getComputedStyle(el);
    return !!(style && (style.display === 'none' || style.visibility === 'hidden'));
  }};
  const hasVisibleBox = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  const patternTest = (pattern, value) => {{
    if (!pattern) {{
      return false;
    }}
    try {{
      pattern.lastIndex = 0;
    }} catch (error) {{
    }}
    return pattern.test(value || '');
  }};
  const isVisible = (el) => {{
    if (!el) return false;
    return !isExplicitlyHidden(el) && hasVisibleBox(el);
  }};
  const isWithinRoot = (candidate, scopeRoot) => {{
    if (!candidate || !scopeRoot) {{
      return false;
    }}
    return candidate === scopeRoot || !!(scopeRoot.contains && scopeRoot.contains(candidate));
  }};
  const isRelaxedClickable = (node) => {{
    if (!node) {{
      return false;
    }}
    const tagName = (node.tagName || '').toLowerCase();
    const role = cleanText(node.getAttribute && node.getAttribute('role') || '').toLowerCase();
    if (tagName === 'a') {{
      return !!(node.getAttribute && node.getAttribute('href'));
    }}
    if (tagName === 'button' || role === 'button' || role === 'link') {{
      return true;
    }}
    const tabindex = cleanText(node.getAttribute && node.getAttribute('tabindex') || '');
    if (tabindex && tabindex !== '-1') {{
      return true;
    }}
    for (const attrName of ['onclick', 'data-link', 'data-url', 'data-href', 'data-target', 'data-uri', 'data-web-uri', 'data-web-destination']) {{
      if (cleanText(node.getAttribute && node.getAttribute(attrName) || '')) {{
        return true;
      }}
    }}
    try {{
      if (typeof node.onclick === 'function') {{
        return true;
      }}
    }} catch (error) {{
    }}
    try {{
      const style = window.getComputedStyle(node);
      if (style && style.cursor === 'pointer') {{
        return true;
      }}
    }} catch (error) {{
    }}
    return false;
  }};
  const linkishAttrNames = ['data-link', 'data-url', 'data-href', 'data-target', 'data-uri', 'data-web-uri', 'data-web-destination'];
  const summarizeNode = (node) => {{
    if (!node) {{
      return null;
    }}
    const tag = cleanText(node.tagName || '', 32).toLowerCase() || '-';
    const role = cleanText(node.getAttribute && node.getAttribute('role') || '', 48) || '-';
    const tabindex = cleanText(node.getAttribute && node.getAttribute('tabindex') || '', 16) || '-';
    const href = cleanText(node.getAttribute && node.getAttribute('href') || '', 160) || '-';
    const aria = cleanText(node.getAttribute && node.getAttribute('aria-label') || '', 120) || '-';
    const title = cleanText(node.getAttribute && node.getAttribute('title') || '', 120) || '-';
    const text = cleanText(node.innerText || node.textContent || '', 120) || '-';
    return {{
      tag,
      role,
      tabindex,
      href,
      aria,
      title,
      text,
      relaxedClickable: isRelaxedClickable(node) ? 1 : 0,
    }};
  }};
  const summarizeRoot = (node) => {{
    if (!node) {{
      return '-';
    }}
    const tag = cleanText(node.tagName || '', 32).toLowerCase() || 'node';
    if (tag === 'header' || tag === 'main') {{
      return tag;
    }}
    const role = cleanText(node.getAttribute && node.getAttribute('role') || '', 32).toLowerCase();
    if (role) {{
      return `${{tag}}:${{role}}`;
    }}
    const testId = cleanText(node.getAttribute && node.getAttribute('data-testid') || '', 48).toLowerCase();
    if (testId) {{
      return `${{tag}}:${{testId}}`;
    }}
    const text = cleanText(node.innerText || node.textContent || '', 40).toLowerCase();
    if (text) {{
      return `${{tag}}:${{text}}`;
    }}
    return tag;
  }};
  const countLinkishDataNodes = (nodes) => nodes.filter((node) => {{
    if (!node || typeof node.getAttribute !== 'function') {{
      return false;
    }}
    return linkishAttrNames.some((attrName) => cleanText(node.getAttribute(attrName) || '', 120));
  }}).length;
  const scopeRoots = [];
  const seenRoots = new Set();
  const addRoot = (node) => {{
    if (!node || seenRoots.has(node)) {{
      return;
    }}
    const tagName = (node.tagName || '').toLowerCase();
    if (['aside', 'footer', 'nav', 'script', 'style', 'noscript'].includes(tagName)) {{
      return;
    }}
    seenRoots.add(node);
    scopeRoots.push(node);
  }};
  const header = main.querySelector('header');
  addRoot(header);
  for (const child of Array.from(main.children || [])) {{
    if (scopeRoots.length >= maxScopeRoots) {{
      break;
    }}
    if (!child || child === header) {{
      continue;
    }}
    addRoot(child);
  }}
  if (!scopeRoots.length) {{
    addRoot(main);
  }}
  const truth = captureTruth ? {{
    mainPresent: 1,
    headerPresent: header ? 1 : 0,
    scopeRoots: scopeRoots.slice(0, truthLimit).map((root) => summarizeRoot(root)),
    rootSnapshots: [],
    rawProbeNodeCount: 0,
    candidateSamples: [],
    dropSamples: [],
    keepSamples: [],
  }} : null;
  if (truth) {{
    for (const root of scopeRoots.slice(0, truthLimit)) {{
      const descendants = Array.from(root.querySelectorAll ? root.querySelectorAll('*') : []);
      const descendantSamples = descendants.slice(0, truthLimit).map((node) => summarizeNode(node)).filter(Boolean);
      truth.rootSnapshots.push({{
        root: summarizeRoot(root),
        descendants: descendants.length,
        anchors: descendants.filter((node) => node && node.matches && node.matches('a[href]')).length,
        buttons: descendants.filter((node) => cleanText(node && node.tagName || '', 32).toLowerCase() === 'button').length,
        roleNodes: descendants.filter((node) => cleanText(node && node.getAttribute && node.getAttribute('role') || '', 32)).length,
        tabindexNodes: descendants.filter((node) => {{
          const tabindex = cleanText(node && node.getAttribute && node.getAttribute('tabindex') || '', 16);
          return !!tabindex && tabindex !== '-1';
        }}).length,
        onclickNodes: descendants.filter((node) => {{
          if (!node) {{
            return false;
          }}
          try {{
            if (typeof node.onclick === 'function') {{
              return true;
            }}
          }} catch (error) {{
          }}
          return !!cleanText(node.getAttribute && node.getAttribute('onclick') || '', 120);
        }}).length,
        dataLinkish: countLinkishDataNodes(descendants),
        nodes: descendantSamples,
      }});
    }}
  }}
  const values = [];
  const seenValues = new Set();
  const dropReasons = {{}};
  const relaxedKeepSamples = [];
  const keptCandidates = [];
  const seenCandidateNodes = new Set();
  let rawCandidateCount = 0;
  let strongCandidateCount = 0;
  let weakCandidateCount = 0;
  let interactionCandidateCount = 0;
  let interactionOnlyValueCount = 0;
  const addDropReason = (reason, kind = 'hard') => {{
    const key = cleanText(reason || '', 64) || 'other';
    const normalizedKey = key.includes(':') ? key : `${{kind}}:${{key}}`;
    dropReasons[normalizedKey] = (dropReasons[normalizedKey] || 0) + 1;
  }};
  const addValue = (value) => {{
    if (typeof value !== 'string') {{
      return;
    }}
    const trimmed = value.trim();
    if (!trimmed || blockedValuePattern.test(trimmed) || seenValues.has(trimmed)) {{
      return;
    }}
    seenValues.add(trimmed);
    values.push(trimmed);
  }};
  const addTextMatches = (value) => {{
    const text = cleanText(value, 512);
    if (!text) {{
      return;
    }}
    try {{
      urlPattern.lastIndex = 0;
    }} catch (error) {{
    }}
    const urlMatches = text.match(urlPattern) || [];
    for (const match of urlMatches.slice(0, maxRuntimeArrayItems)) {{
      addValue(match.replace(/[.,;:!?)]}}]+$/g, ''));
    }}
    try {{
      bareDomainPattern.lastIndex = 0;
    }} catch (error) {{
    }}
    const bareMatches = Array.from(text.matchAll(bareDomainPattern)).slice(0, maxRuntimeArrayItems);
    for (const match of bareMatches) {{
      const rawMatch = cleanText(match && match[0] ? match[0] : '', 256);
      const matchIndex = match && typeof match.index === 'number' ? match.index : -1;
      if (matchIndex > 0 && text[matchIndex - 1] === '@') {{
        continue;
      }}
      const cleaned = rawMatch.replace(/[.,;:!?)]}}]+$/g, '');
      if (!cleaned || blockedValuePattern.test(cleaned)) {{
        continue;
      }}
      if (/^https?:\\/\\//i.test(cleaned)) {{
        addValue(cleaned);
      }} else {{
        addValue(`https://${{cleaned}}`);
      }}
    }}
  }};
  const addValueWithMatches = (value, includeFull = false) => {{
    if (typeof value !== 'string') {{
      return;
    }}
    const cleaned = cleanText(value, 512);
    if (!cleaned) {{
      return;
    }}
    addTextMatches(cleaned);
    if (
      includeFull
      && (
        /^https?:\\/\\//i.test(cleaned)
        || cleaned.startsWith('//')
        || cleaned.startsWith('/')
        || cleaned.startsWith('{{')
        || cleaned.startsWith('[')
        || runtimeValueKeyPattern.test(cleaned)
      )
    ) {{
      addValue(cleaned);
    }}
  }};
  const addTextValues = (node) => {{
    if (!node) {{
      return;
    }}
    addTextMatches(node.innerText || node.textContent || '');
    if (typeof node.getAttribute === 'function') {{
      addTextMatches(node.getAttribute('aria-label') || '');
      addTextMatches(node.getAttribute('title') || '');
    }}
  }};
  const addAttributeValues = (node) => {{
    if (!node) {{
      return;
    }}
    const attrNames = typeof node.getAttributeNames === 'function' ? node.getAttributeNames() : [];
    for (const attrName of attrNames.slice(0, maxAttrsPerNode)) {{
      const key = cleanText(attrName || '', 64).toLowerCase();
      if (!key || key === 'style') {{
        continue;
      }}
      if (
        key === 'href'
        || key === 'title'
        || key === 'aria-label'
        || key === 'onclick'
        || key === 'class'
        || runtimeValueKeyPattern.test(key)
        || key.startsWith('data-')
      ) {{
        addValueWithMatches(node.getAttribute(attrName) || '', true);
      }}
    }}
  }};
  const addStringProperty = (node, propName) => {{
    if (!node || !propName) {{
      return;
    }}
    try {{
      const value = node[propName];
      if (typeof value === 'string') {{
        addValueWithMatches(value, true);
      }}
    }} catch (error) {{
    }}
  }};
  const scanRuntimeObject = (value, depth, seenObjects) => {{
    if (!value || depth > maxRuntimeObjectDepth) {{
      return;
    }}
    if (typeof value === 'string') {{
      addValueWithMatches(value, true);
      return;
    }}
    if ((typeof value !== 'object' && typeof value !== 'function') || value === window || value === document || value.nodeType) {{
      return;
    }}
    if (seenObjects.has(value)) {{
      return;
    }}
    seenObjects.add(value);
    if (Array.isArray(value)) {{
      for (const item of value.slice(0, maxRuntimeArrayItems)) {{
        scanRuntimeObject(item, depth + 1, seenObjects);
      }}
      return;
    }}
    let entries = [];
    try {{
      entries = Object.entries(value);
    }} catch (error) {{
      return;
    }}
    let processed = 0;
    for (const [key, entryValue] of entries) {{
      if (processed >= maxRuntimeObjectEntries) {{
        break;
      }}
      processed += 1;
      if (typeof entryValue === 'string') {{
        if (
          runtimeValueKeyPattern.test(key || '')
          || /^https?:\\/\\//i.test(entryValue)
          || entryValue.startsWith('//')
          || entryValue.startsWith('/')
        ) {{
          addValueWithMatches(entryValue, true);
        }}
        continue;
      }}
      if (!entryValue || (typeof entryValue !== 'object' && typeof entryValue !== 'function')) {{
        continue;
      }}
      if (runtimeValueKeyPattern.test(key || '') || depth < 1) {{
        scanRuntimeObject(entryValue, depth + 1, seenObjects);
      }}
    }}
  }};
  const addRuntimeValues = (node) => {{
    if (!node) {{
      return;
    }}
    for (const propName of ['href', 'url', 'uri', 'to', 'path', 'pathname', 'target', 'destination', 'redirectUrl', 'redirectUri', 'website', 'externalUrl', 'action', 'formAction']) {{
      addStringProperty(node, propName);
    }}
    let ownProps = [];
    try {{
      ownProps = Object.getOwnPropertyNames(node) || [];
    }} catch (error) {{
      ownProps = [];
    }}
    const candidateProps = ownProps.filter(
      (propName) => runtimeNodePropPattern.test(propName || '') || runtimeValueKeyPattern.test(propName || '')
    );
    for (const propName of candidateProps.slice(0, maxRuntimeOwnProps)) {{
      try {{
        const propValue = node[propName];
        if (typeof propValue === 'string') {{
          addValueWithMatches(propValue, true);
          continue;
        }}
        if (propValue && (typeof propValue === 'object' || typeof propValue === 'function')) {{
          scanRuntimeObject(propValue, 0, new Set());
        }}
      }} catch (error) {{
      }}
    }}
  }};
  const addAnchorValues = (anchor) => {{
    if (!anchor || !isVisible(anchor)) {{
      return;
    }}
    addAttributeValues(anchor);
    addTextValues(anchor);
    addStringProperty(anchor, 'href');
  }};
  const collectRelatedNodes = (scopeRoot, node) => {{
    const nearbyNodes = [];
    const seenNearbyNodes = new Set();
    const enqueue = (candidate) => {{
      if (!candidate || seenNearbyNodes.has(candidate) || nearbyNodes.length >= maxNearbyNodesPerCandidate) {{
        return;
      }}
      if (candidate !== scopeRoot && !isWithinRoot(candidate, scopeRoot)) {{
        return;
      }}
      seenNearbyNodes.add(candidate);
      nearbyNodes.push(candidate);
    }};
    enqueue(node);
    let cursor = node;
    for (let depth = 0; depth < 3 && cursor; depth += 1) {{
      enqueue(cursor.parentElement);
      enqueue(cursor.previousElementSibling);
      enqueue(cursor.nextElementSibling);
      if (cursor.parentElement) {{
        enqueue(cursor.parentElement.previousElementSibling);
        enqueue(cursor.parentElement.nextElementSibling);
      }}
      if (cursor === scopeRoot) {{
        break;
      }}
      cursor = cursor.parentElement;
    }}
    return nearbyNodes;
  }};
  const addNearbySubtreeValues = (scopeRoot, node) => {{
    if (!scopeRoot || !node) {{
      return;
    }}
    const ancestorAnchor = node.closest ? node.closest('a[href]') : null;
    if (ancestorAnchor && isWithinRoot(ancestorAnchor, scopeRoot)) {{
      addAnchorValues(ancestorAnchor);
    }}
    for (const nearbyNode of collectRelatedNodes(scopeRoot, node)) {{
      if (!nearbyNode) {{
        continue;
      }}
      addAttributeValues(nearbyNode);
      addTextValues(nearbyNode);
      addRuntimeValues(nearbyNode);
      if (nearbyNode.matches && nearbyNode.matches('a[href]')) {{
        addAnchorValues(nearbyNode);
      }}
      if (typeof nearbyNode.querySelectorAll !== 'function') {{
        continue;
      }}
      const descendantAnchors = Array.from(nearbyNode.querySelectorAll('a[href]')).slice(0, maxNearbyAnchorsPerNode);
      for (const anchor of descendantAnchors) {{
        addAnchorValues(anchor);
      }}
      const nearbyClickables = Array.from(nearbyNode.querySelectorAll(relaxedSelector)).slice(0, maxNearbyClickablesPerNode);
      for (const clickable of nearbyClickables) {{
        addAttributeValues(clickable);
        addTextValues(clickable);
        addRuntimeValues(clickable);
      }}
    }}
  }};
  const collectAttrText = (node) => {{
    if (!node || typeof node.getAttributeNames !== 'function') {{
      return '';
    }}
    const parts = [];
    for (const attrName of node.getAttributeNames().slice(0, maxAttrsPerNode)) {{
      const key = cleanText(attrName || '', 64).toLowerCase();
      if (!key || key === 'style') {{
        continue;
      }}
      if (
        key === 'aria-label'
        || key === 'title'
        || key === 'href'
        || key === 'class'
        || key === 'data-testid'
        || runtimeValueKeyPattern.test(key)
        || key.startsWith('data-')
      ) {{
        parts.push(cleanText(node.getAttribute(attrName) || '', 180));
      }}
    }}
    return cleanText(parts.join(' '), 280);
  }};
  const shouldRelaxProfileSurfaceVisibility = (node, scopeRoot, combinedText, ownText, relaxedClickable) => {{
    if (!node || !scopeRoot || isVisible(node) || isExplicitlyHidden(node)) {{
      return false;
    }}
    if (!isWithinRoot(node, scopeRoot)) {{
      return false;
    }}
    if (node.closest('nav, footer, aside, [role="tablist"]')) {{
      return false;
    }}
    if (
      tabPattern.test(ownText)
      || menuPattern.test(ownText)
      || followerPattern.test(ownText)
      || sharePattern.test(ownText)
    ) {{
      return false;
    }}
    const withinHeader = !!(header && (header === scopeRoot || header.contains(scopeRoot) || header.contains(node)));
    const interactiveAncestor = node.closest ? node.closest(relaxedSelector) : null;
    const boundedInteractive = !!(
      interactiveAncestor
      && interactiveAncestor !== node
      && isWithinRoot(interactiveAncestor, scopeRoot)
    );
    const hasStrongProfileSignal = patternTest(positivePattern, ownText) || patternTest(positivePattern, combinedText);
    const hasWeakProfileSignal = patternTest(weakContextPattern, ownText) || patternTest(weakContextPattern, combinedText);
    if ((withinHeader || boundedInteractive) && relaxedClickable && (hasStrongProfileSignal || hasWeakProfileSignal)) {{
      return true;
    }}
    if (withinHeader && hasStrongProfileSignal) {{
      return true;
    }}
    return false;
  }};
  const classifyHardDropReason = (node, scopeRoot, ownText, visibilityRelaxed) => {{
    if (!node) {{
      return 'not_visible';
    }}
    if (!isWithinRoot(node, scopeRoot)) {{
      return 'out_of_scope';
    }}
    if (node.closest('nav, footer, aside')) {{
      return 'global_nav';
    }}
    if (node.closest('[role="tablist"]') || tabPattern.test(ownText)) {{
      return 'tab';
    }}
    if (menuPattern.test(ownText)) {{
      return 'menu';
    }}
    if (followerPattern.test(ownText)) {{
      return 'follower_control';
    }}
    if (sharePattern.test(ownText)) {{
      return 'share';
    }}
    if (!visibilityRelaxed && !isVisible(node)) {{
      return 'not_visible';
    }}
    return '';
  }};
  const scoreSeed = (node, scopeRoot) => {{
    if (!node) {{
      return {{ countable: false, keep: false, dropReason: '', dropKind: 'hard', softDropReasons: [] }};
    }}
    const relaxedClickable = isRelaxedClickable(node);
    const labelText = cleanText(
      [
        node.getAttribute && node.getAttribute('aria-label') || '',
        node.getAttribute && node.getAttribute('title') || '',
        node.innerText || node.textContent || '',
      ].join(' '),
      280,
    );
    const contextText = cleanText(
      [
        node.parentElement ? (node.parentElement.innerText || node.parentElement.textContent || '') : '',
        node.previousElementSibling ? (node.previousElementSibling.innerText || node.previousElementSibling.textContent || '') : '',
        node.nextElementSibling ? (node.nextElementSibling.innerText || node.nextElementSibling.textContent || '') : '',
      ].join(' '),
      280,
    );
    const attrText = collectAttrText(node);
    const combined = cleanText([labelText, contextText, attrText].join(' '), 320);
    const ownText = cleanText([labelText, attrText].join(' '), 280);
    const countable = relaxedClickable || patternTest(positivePattern, combined) || patternTest(weakContextPattern, combined);
    if (!countable) {{
      return {{ countable: false, keep: false, dropReason: '', dropKind: 'hard', softDropReasons: [] }};
    }}
    const visibilityRelaxed = shouldRelaxProfileSurfaceVisibility(
      node,
      scopeRoot,
      combined,
      ownText,
      relaxedClickable,
    );
    const hardDropReason = classifyHardDropReason(node, scopeRoot, ownText, visibilityRelaxed);
    if (hardDropReason) {{
      return {{ countable: true, keep: false, dropReason: hardDropReason, dropKind: 'hard', softDropReasons: [] }};
    }}
    let score = 0;
    const hasLabelUrl = patternTest(urlPattern, labelText) || patternTest(bareDomainPattern, labelText);
    const hasAttrUrl = patternTest(urlPattern, attrText) || patternTest(bareDomainPattern, attrText);
    const hasPositiveLabel = patternTest(positivePattern, labelText);
    const hasPositiveAttr = patternTest(positivePattern, attrText);
    const hasPositiveContext = patternTest(positivePattern, contextText);
    const hasWeakContext = patternTest(weakContextPattern, contextText) || patternTest(weakContextPattern, attrText);
    if (hasLabelUrl) {{
      score += 10;
    }}
    if (hasAttrUrl) {{
      score += 10;
    }}
    if (hasPositiveLabel) {{
      score += 6;
    }}
    if (hasPositiveAttr) {{
      score += 6;
    }}
    if (hasPositiveContext) {{
      score += 3;
    }}
    if (hasWeakContext) {{
      score += 2;
    }}
    if (relaxedClickable) {{
      score += 2;
    }}
    if (header && header.contains(node)) {{
      score += 1;
    }}
    const minimumScore = relaxedClickable ? 4 : 6;
    const hasStrongSignal = hasLabelUrl || hasAttrUrl || hasPositiveLabel || hasPositiveAttr || hasPositiveContext;
    const softDropReasons = [];
    if (visibilityRelaxed) {{
      softDropReasons.push('profile_surface_visibility_relaxed');
    }}
    if (patternTest(profileActionPattern, ownText)) {{
      softDropReasons.push('profile_action');
    }}
    if (score < minimumScore) {{
      softDropReasons.push('weak_signal');
    }}
    if (relaxedClickable && !hasStrongSignal && !patternTest(weakContextPattern, combined)) {{
      softDropReasons.push('ambiguous_wrapper');
    }}
    let label = 'bio_surface_wrapper';
    if (node.matches && node.matches('a[href]')) {{
      label = 'direct_anchor';
    }} else if (relaxedClickable) {{
      label = 'nested_clickable';
    }}
    let candidateClass = 'weak_candidate';
    if (relaxedClickable) {{
      candidateClass = 'interaction_candidate';
    }}
    if (!softDropReasons.length && score >= minimumScore && hasStrongSignal) {{
      candidateClass = 'strong_candidate';
    }} else if (!softDropReasons.length && (!relaxedClickable || hasStrongSignal || score >= minimumScore)) {{
      candidateClass = 'weak_candidate';
    }}
    return {{
      countable: true,
      keep: true,
      dropReason: '',
      dropKind: 'hard',
      label,
      node,
      scopeRoot,
      score,
      candidateClass,
      interactionEligible: relaxedClickable,
      softDropReasons,
      softPenaltyCount: softDropReasons.length,
      logText: ownText || combined || labelText || attrText,
    }};
  }};
  for (const root of scopeRoots) {{
    const probeNodes = [];
    const seenProbeNodes = new Set();
    const enqueueProbe = (node) => {{
      if (!node || seenProbeNodes.has(node) || probeNodes.length >= maxProbeNodesPerRoot) {{
        return;
      }}
      if (!isWithinRoot(node, root)) {{
        return;
      }}
      seenProbeNodes.add(node);
      probeNodes.push(node);
    }};
    enqueueProbe(root);
    if (typeof root.querySelectorAll === 'function') {{
      for (const node of Array.from(root.querySelectorAll(probeSelector))) {{
        enqueueProbe(node);
        if (probeNodes.length >= maxProbeNodesPerRoot) {{
          break;
        }}
      }}
    }}
    if (truth) {{
      truth.rawProbeNodeCount += probeNodes.length;
    }}
    for (const node of probeNodes) {{
      const scored = scoreSeed(node, root);
      if (!scored.countable) {{
        continue;
      }}
      rawCandidateCount += 1;
      if (truth && truth.candidateSamples.length < truthLimit) {{
        truth.candidateSamples.push(summarizeNode(node));
      }}
      if (!scored.keep) {{
        addDropReason(scored.dropReason || 'weak_signal', scored.dropKind || 'hard');
        if (truth && truth.dropSamples.length < truthLimit) {{
          truth.dropSamples.push({{
            stage: 'hard_exclusion',
            reason: scored.dropReason || 'weak_signal',
            summary: summarizeNode(node),
          }});
        }}
        continue;
      }}
      if (seenCandidateNodes.has(scored.node)) {{
        addDropReason('duplicate', 'hard');
        if (truth && truth.dropSamples.length < truthLimit) {{
          truth.dropSamples.push({{
            stage: 'dedupe',
            reason: 'duplicate',
            summary: summarizeNode(scored.node),
          }});
        }}
        continue;
      }}
      seenCandidateNodes.add(scored.node);
      for (const softReason of scored.softDropReasons || []) {{
        addDropReason(softReason, 'soft');
      }}
      if (scored.candidateClass === 'strong_candidate') {{
        strongCandidateCount += 1;
      }} else if (scored.candidateClass === 'interaction_candidate') {{
        interactionCandidateCount += 1;
      }} else {{
        weakCandidateCount += 1;
      }}
      keptCandidates.push(scored);
      if (keptCandidates.length >= maxNodes) {{
        break;
      }}
    }}
    if (keptCandidates.length >= maxNodes) {{
      break;
    }}
  }}
  keptCandidates.sort((left, right) => {{
    const classRank = {{
      strong_candidate: 0,
      weak_candidate: 1,
      interaction_candidate: 2,
    }};
    const leftRank = classRank[left.candidateClass] ?? 9;
    const rightRank = classRank[right.candidateClass] ?? 9;
    if (leftRank !== rightRank) {{
      return leftRank - rightRank;
    }}
    if ((left.softPenaltyCount || 0) !== (right.softPenaltyCount || 0)) {{
      return (left.softPenaltyCount || 0) - (right.softPenaltyCount || 0);
    }}
    if (right.score !== left.score) {{
      return right.score - left.score;
    }}
    return left.label.localeCompare(right.label);
  }});
  const selectedCandidates = keptCandidates.slice(0, maxNodes);
  for (let index = 0; index < selectedCandidates.length; index += 1) {{
    const candidate = selectedCandidates[index];
    const valueCountBefore = values.length;
    addAttributeValues(candidate.node);
    addTextValues(candidate.node);
    addRuntimeValues(candidate.node);
    addNearbySubtreeValues(candidate.scopeRoot, candidate.node);
    if (values.length === valueCountBefore && candidate.interactionEligible) {{
      addValue(`${{interactionOnlyPrefix}}${{candidate.label}}:${{index}}`);
      interactionOnlyValueCount += 1;
    }}
    if (
      Array.isArray(candidate.softDropReasons)
      && candidate.softDropReasons.includes('profile_surface_visibility_relaxed')
      && relaxedKeepSamples.length < truthLimit
    ) {{
      relaxedKeepSamples.push({{
        candidateClass: candidate.candidateClass,
        signal: 'profile_surface_visibility_relaxed',
        text: cleanText(candidate.logText || '', 120),
      }});
    }}
    if (truth && truth.keepSamples.length < truthLimit) {{
      const producedValue = values.length > valueCountBefore ? 1 : 0;
      truth.keepSamples.push({{
        candidateClass: candidate.candidateClass,
        score: candidate.score,
        softReasons: Array.isArray(candidate.softDropReasons) ? candidate.softDropReasons.slice(0, truthLimit) : [],
        producedValue,
        summary: summarizeNode(candidate.node),
      }});
      if (!producedValue && truth.dropSamples.length < truthLimit) {{
        truth.dropSamples.push({{
          stage: 'value_extraction_empty',
          reason: 'no_extracted_value',
          summary: summarizeNode(candidate.node),
        }});
      }}
    }}
  }}
  const keptLabels = selectedCandidates.map((candidate) => candidate.label).filter(Boolean);
  return {{
    marker,
    values,
    rawCandidateCount,
    keptCandidateCount: Math.min(strongCandidateCount + weakCandidateCount, maxNodes),
    interactionCandidateCount: Math.min(interactionCandidateCount, maxNodes),
    interactionOnlyValueCount,
    keptLabels: keptLabels.slice(0, maxSampleLabels),
    dropReasons,
    relaxedKeepSamples,
    mainPresent: 1,
    headerPresent: header ? 1 : 0,
    scopeRoots: truth ? truth.scopeRoots : [],
    rootSnapshots: truth ? truth.rootSnapshots : [],
    rawProbeNodeCount: truth ? truth.rawProbeNodeCount : 0,
    candidateSamples: truth ? truth.candidateSamples : [],
    dropSamples: truth ? truth.dropSamples : [],
    keepSamples: truth ? truth.keepSamples : [],
    retrySnapshotIndex,
  }};
}}
"""
        )
    try:
        raw_payload = _evaluate_payload()
    except Exception:
        raw_payload = {}

    extracted_values = raw_payload.get("values") if isinstance(raw_payload, dict) else raw_payload
    values: List[str] = []
    seen: Set[str] = set()
    for raw_value in extracted_values or []:
        value = cell_to_str(raw_value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)

    def _truth_clean(raw_value: Any, *, limit: int = 120) -> str:
        text = re.sub(r"\s+", " ", cell_to_str(raw_value).strip())
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        return text or "-"

    def _truth_quote(raw_value: Any, *, limit: int = 120) -> str:
        text = _truth_clean(raw_value, limit=limit).replace("'", '"')
        return f"'{text}'"

    def _truth_node_summary(node_payload: Any) -> str:
        if not isinstance(node_payload, dict):
            return "tag=- role=- tabindex=- href=- aria='-' title='-' text='-' relaxed_clickable=0"
        return (
            f"tag={_truth_clean(node_payload.get('tag'), limit=32)} "
            f"role={_truth_clean(node_payload.get('role'), limit=48)} "
            f"tabindex={_truth_clean(node_payload.get('tabindex'), limit=16)} "
            f"href={_truth_clean(node_payload.get('href'), limit=160)} "
            f"aria={_truth_quote(node_payload.get('aria'), limit=96)} "
            f"title={_truth_quote(node_payload.get('title'), limit=96)} "
            f"text={_truth_quote(node_payload.get('text'), limit=96)} "
            f"relaxed_clickable={1 if int(node_payload.get('relaxedClickable') or 0) else 0}"
        )

    raw_candidate_count = 0
    kept_candidate_count = 0
    interaction_candidate_count = 0
    interaction_only_value_count = 0
    kept_labels: List[str] = []
    drop_reasons: Dict[str, int] = {}
    relaxed_keep_samples: List[Dict[str, str]] = []
    if isinstance(raw_payload, dict):
        try:
            raw_candidate_count = max(int(raw_payload.get("rawCandidateCount") or 0), 0)
        except Exception:
            raw_candidate_count = 0
        try:
            kept_candidate_count = max(int(raw_payload.get("keptCandidateCount") or 0), 0)
        except Exception:
            kept_candidate_count = 0
        try:
            interaction_candidate_count = max(int(raw_payload.get("interactionCandidateCount") or 0), 0)
        except Exception:
            interaction_candidate_count = 0
        try:
            interaction_only_value_count = max(int(raw_payload.get("interactionOnlyValueCount") or 0), 0)
        except Exception:
            interaction_only_value_count = 0
        for raw_label in raw_payload.get("keptLabels") or []:
            label = cell_to_str(raw_label).strip()
            if label and label not in kept_labels:
                kept_labels.append(label)
        raw_drop_reasons = raw_payload.get("dropReasons") or {}
        if isinstance(raw_drop_reasons, dict):
            for reason, count in raw_drop_reasons.items():
                reason_key = cell_to_str(reason).strip()
                if not reason_key:
                    continue
                try:
                    numeric_count = int(count)
                except Exception:
                    continue
                if numeric_count > 0:
                    drop_reasons[reason_key] = numeric_count
        for raw_sample in raw_payload.get("relaxedKeepSamples") or []:
            if not isinstance(raw_sample, dict):
                continue
            sample_class = cell_to_str(raw_sample.get("candidateClass")).strip()
            sample_signal = cell_to_str(raw_sample.get("signal")).strip()
            sample_text = cell_to_str(raw_sample.get("text")).strip()
            if not sample_class or not sample_signal:
                continue
            relaxed_keep_samples.append(
                {
                    "candidate_class": sample_class,
                    "signal": sample_signal,
                    "text": sample_text,
                }
            )
    else:
        kept_candidate_count = len(values)
        raw_candidate_count = kept_candidate_count

    if truth_enabled and isinstance(raw_payload, dict):
        scope_roots = [
            _truth_clean(root, limit=48)
            for root in (raw_payload.get("scopeRoots") or [])
            if _truth_clean(root, limit=48) != "-"
        ]
        main_present = 1 if int(raw_payload.get("mainPresent") or 0) else 0
        header_present = 1 if int(raw_payload.get("headerPresent") or 0) else 0
        _log_truth(
            f"[IG Truth] scope roots count={len(scope_roots)} main={main_present} "
            f"header={header_present} sample={','.join(scope_roots[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]) or '-'}"
        )
        for idx, snapshot in enumerate((raw_payload.get("rootSnapshots") or [])[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]):
            if not isinstance(snapshot, dict):
                continue
            _log_truth(
                f"[IG Truth] root_snapshot idx={idx} root={_truth_clean(snapshot.get('root'), limit=48)} "
                f"descendants={max(int(snapshot.get('descendants') or 0), 0)} "
                f"anchors={max(int(snapshot.get('anchors') or 0), 0)} "
                f"buttons={max(int(snapshot.get('buttons') or 0), 0)} "
                f"role_nodes={max(int(snapshot.get('roleNodes') or 0), 0)} "
                f"tabindex_nodes={max(int(snapshot.get('tabindexNodes') or 0), 0)} "
                f"onclick_nodes={max(int(snapshot.get('onclickNodes') or 0), 0)} "
                f"data_linkish={max(int(snapshot.get('dataLinkish') or 0), 0)}"
            )
            for node_idx, node_payload in enumerate((snapshot.get("nodes") or [])[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]):
                _log_truth(f"[IG Truth] node idx={node_idx} {_truth_node_summary(node_payload)}")

        candidate_sample = " | ".join(
            _truth_node_summary(node_payload)
            for node_payload in (raw_payload.get("candidateSamples") or [])[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]
        ) or "-"
        _log_truth(
            f"[IG Truth] candidates raw_probe={max(int(raw_payload.get('rawProbeNodeCount') or 0), 0)} "
            f"countable={raw_candidate_count} sample={candidate_sample}"
        )
        for drop_payload in (raw_payload.get("dropSamples") or [])[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]:
            if not isinstance(drop_payload, dict):
                continue
            _log_truth(
                f"[IG Truth] drop stage={_truth_clean(drop_payload.get('stage'), limit=32)} "
                f"reason={_truth_clean(drop_payload.get('reason'), limit=64)} "
                f"{_truth_node_summary(drop_payload.get('summary'))}"
            )
        for keep_payload in (raw_payload.get("keepSamples") or [])[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]:
            if not isinstance(keep_payload, dict):
                continue
            soft_reasons = [
                _truth_clean(reason, limit=32)
                for reason in (keep_payload.get("softReasons") or [])
                if _truth_clean(reason, limit=32) != "-"
            ]
            _log_truth(
                f"[IG Truth] keep class={_truth_clean(keep_payload.get('candidateClass'), limit=32)} "
                f"score={max(int(keep_payload.get('score') or 0), 0)} "
                f"soft={','.join(soft_reasons) if soft_reasons else '-'} "
                f"produced_value={1 if int(keep_payload.get('producedValue') or 0) else 0} "
                f"{_truth_node_summary(keep_payload.get('summary'))}"
            )
        _log_truth(
            f"[IG Truth] output raw_control_values count={len(values)} "
            f"sample={_instagram_bio_link_log_sample(values)}"
        )
        if not values:
            wait_for_timeout = getattr(page, "wait_for_timeout", None)
            for retry_snapshot in range(1, _INSTAGRAM_TRUTH_RETRY_SNAPSHOTS + 1):
                if callable(wait_for_timeout):
                    try:
                        wait_for_timeout(_INSTAGRAM_TRUTH_RETRY_WAIT_MS)
                    except Exception:
                        pass
                else:
                    time.sleep(_INSTAGRAM_TRUTH_RETRY_WAIT_MS / 1000.0)
                try:
                    retry_payload = _evaluate_payload(retry_snapshot_index=retry_snapshot)
                except Exception:
                    retry_payload = {}
                if not isinstance(retry_payload, dict):
                    retry_payload = {}
                descendant_count = 0
                for snapshot in retry_payload.get("rootSnapshots") or []:
                    if not isinstance(snapshot, dict):
                        continue
                    try:
                        descendant_count += max(int(snapshot.get("descendants") or 0), 0)
                    except Exception:
                        continue
                _log_truth(
                    f"[IG Truth] zero_first_pass retry_snapshot={retry_snapshot} "
                    f"descendants={descendant_count} "
                    f"countable={max(int(retry_payload.get('rawCandidateCount') or 0), 0)}"
                )

    dropped_candidate_count = max(raw_candidate_count - kept_candidate_count - interaction_candidate_count, 0)
    candidate_sample = ",".join(kept_labels[:3]) if kept_labels else "-"
    control_sample = _instagram_bio_link_log_sample(values) if values else candidate_sample
    for sample in relaxed_keep_samples[:_INSTAGRAM_TRUTH_SAMPLE_LIMIT]:
        _log_detect(
            f"[IG Detect] keep class={sample['candidate_class']} "
            f"signal={sample['signal']} text={_truth_quote(sample.get('text'))}"
        )
    if drop_reasons:
        prioritized_reasons: List[Tuple[str, int]] = []
        prioritized_keys = (
            "hard:not_visible",
            "soft:profile_surface_visibility_relaxed",
        )
        for key in prioritized_keys:
            count = drop_reasons.get(key)
            if count:
                prioritized_reasons.append((key, count))
        for reason, count in sorted(drop_reasons.items(), key=lambda item: (-item[1], item[0])):
            if any(reason == existing_reason for existing_reason, _ in prioritized_reasons):
                continue
            prioritized_reasons.append((reason, count))
            if len(prioritized_reasons) >= 5:
                break
        drop_reason_text = ",".join(f"{reason}:{count}" for reason, count in prioritized_reasons)
    else:
        drop_reason_text = "-"

    _log_detect(
        f"[IG Detect] candidates raw={raw_candidate_count} kept={kept_candidate_count} "
        f"interaction={interaction_candidate_count} "
        f"dropped={dropped_candidate_count} sample={candidate_sample}"
    )
    _log_detect(f"[IG Detect] drop_reasons={drop_reason_text}")
    _log_detect(
        f"[IG Detect] control_values count={len(values)} "
        f"interaction_only={interaction_only_value_count} sample={control_sample}"
    )
    return values


def _recover_instagram_live_profile_clickable_bio_link_url_via_interaction(page: Any) -> str:
    if page is None or not hasattr(page, "evaluate") or not hasattr(page, "click"):
        return ""
    marker_name = "ig-live-bio-link-interaction-recovery"
    marker_attr_json = json.dumps(_INSTAGRAM_LIVE_ONEHOP_INTERACTION_MARKER_ATTR)
    state_key_json = json.dumps(_INSTAGRAM_LIVE_ONEHOP_INTERACTION_STATE_KEY)
    selector_json = json.dumps(_INSTAGRAM_LIVE_ONEHOP_CLICKABLE_SELECTOR)
    marker_name_json = json.dumps(marker_name)

    def _log_recover(message: str) -> None:
        try:
            print(message)
        except Exception:
            pass

    def _coerce_candidate_entry(raw_entry: Any, fallback_label: str) -> Optional[Dict[str, str]]:
        if isinstance(raw_entry, dict):
            selector_value = cell_to_str(raw_entry.get("selector", "")).strip()
            label_value = cell_to_str(raw_entry.get("label", fallback_label)).strip() or fallback_label
            if selector_value:
                return {"selector": selector_value, "label": label_value}
            return None
        selector_value = cell_to_str(raw_entry).strip()
        if not selector_value:
            return None
        return {"selector": selector_value, "label": fallback_label}

    def _load_candidates() -> List[Dict[str, str]]:
        try:
            raw_payload = page.evaluate(
                f"""
() => {{
  const marker = {marker_name_json};
  const markerAttr = {marker_attr_json};
  const stateKey = {state_key_json};
  const selector = {selector_json};
  const maxScopeRoots = {_INSTAGRAM_LIVE_ONEHOP_MAX_SCOPE_ROOTS};
  const maxNodes = {_INSTAGRAM_LIVE_ONEHOP_MAX_CLICKABLE_NODES};
  const maxNestedCandidates = 4;
  const maxNearbyNodes = 10;
  const maxNearbyClickables = 4;
  const main = document.querySelector('main');
  if (!main) {{
    return {{ candidates: [], sample: '-' }};
  }}
  const cleanText = (value, limit = 280) => {{
    if (value == null) return '';
    return String(value).replace(/\\s+/g, ' ').trim().slice(0, limit);
  }};
  const isVisible = (el) => {{
    if (!el) return false;
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  const isClickable = (node) => {{
    if (!node) return false;
    const tagName = (node.tagName || '').toLowerCase();
    const role = cleanText(node.getAttribute && node.getAttribute('role') || '').toLowerCase();
    if (tagName === 'a') {{
      return !!(node.getAttribute && node.getAttribute('href'));
    }}
    return tagName === 'button' || role === 'button' || role === 'link';
  }};
  const isWithinRoot = (candidate, scopeRoot) => {{
    if (!candidate || !scopeRoot) {{
      return false;
    }}
    return candidate === scopeRoot || !!(scopeRoot.contains && scopeRoot.contains(candidate));
  }};
  const scopeRoots = [];
  const seenRoots = new Set();
  const addRoot = (node) => {{
    if (!node || seenRoots.has(node)) {{
      return;
    }}
    const tagName = (node.tagName || '').toLowerCase();
    if (['aside', 'footer', 'nav', 'noscript', 'script', 'style'].includes(tagName)) {{
      return;
    }}
    seenRoots.add(node);
    scopeRoots.push(node);
  }};
  const header = main.querySelector('header');
  addRoot(header);
  for (const child of Array.from(main.children || [])) {{
    if (scopeRoots.length >= maxScopeRoots) {{
      break;
    }}
    if (!child || child === header) {{
      continue;
    }}
    addRoot(child);
  }}
  if (!scopeRoots.length) {{
    addRoot(main);
  }}
  const positivePattern = /(https?:\\/\\/|www\\.|(?:[a-z0-9-]+\\.)+(?:com|co|net|org|fm|tv|io|gg|ly|me|bio|to|live|page|link|music|band|store|art|studio|agency)\\b|\\b(link(?:\\s+in\\s+bio)?|bio\\s+link|website|external)\\b)/i;
  const negativePattern = /\\b(posts?|reels?|tagged|followers?|following|follow|message|email|call|text|menu|settings|options|more|share)\\b/i;
  const contextPattern = /(bio|website|external|link)/i;
  const collectNodeAttrs = (node) => {{
    if (!node || typeof node.getAttributeNames !== 'function') {{
      return '';
    }}
    const parts = [];
    for (const attrName of node.getAttributeNames().slice(0, 16)) {{
      if (!attrName) {{
        continue;
      }}
      const key = String(attrName).trim().toLowerCase();
      if (!key || key === 'style') {{
        continue;
      }}
      if (
        key === 'aria-label'
        || key === 'title'
        || key === 'href'
        || key === 'data-testid'
        || key.startsWith('data-')
        || key === 'class'
      ) {{
        parts.push(cleanText(node.getAttribute(attrName) || '', 180));
      }}
    }}
    return cleanText(parts.join(' '), 280);
  }};
  const scoreSeed = (node, scopeRoot) => {{
    if (!node || !isClickable(node) || !isVisible(node)) {{
      return null;
    }}
    if (!isWithinRoot(node, scopeRoot) || node.closest('nav, footer, aside, [role="tablist"]')) {{
      return null;
    }}
    const labelText = cleanText(
      [
        node.getAttribute && node.getAttribute('aria-label') || '',
        node.getAttribute && node.getAttribute('title') || '',
        node.innerText || node.textContent || '',
      ].join(' '),
      280,
    );
    const contextText = cleanText(
      [
        node.parentElement ? (node.parentElement.innerText || node.parentElement.textContent || '') : '',
        node.closest('header, section, article, div') ? (
          node.closest('header, section, article, div').getAttribute('class') || ''
        ) : '',
      ].join(' '),
      280,
    );
    const attrText = collectNodeAttrs(node);
    const combined = cleanText([labelText, contextText, attrText].join(' '), 280);
    if (!combined || negativePattern.test(combined)) {{
      return null;
    }}
    let score = 0;
    if (positivePattern.test(labelText)) {{
      score += 8;
    }}
    if (positivePattern.test(attrText)) {{
      score += 8;
    }}
    if (positivePattern.test(contextText)) {{
      score += 4;
    }}
    if (contextPattern.test(attrText) || contextPattern.test(contextText)) {{
      score += 2;
    }}
    if (header && header.contains(node)) {{
      score += 1;
    }}
    if (score < 6) {{
      return null;
    }}
    return {{
      node,
      scopeRoot,
      score,
      labelLength: labelText.length,
    }};
  }};
  const collectNearbyNodes = (node, scopeRoot) => {{
    const nearbyNodes = [];
    const seenNearbyNodes = new Set();
    const enqueue = (candidate) => {{
      if (!candidate || seenNearbyNodes.has(candidate) || nearbyNodes.length >= maxNearbyNodes) {{
        return;
      }}
      if (!isWithinRoot(candidate, scopeRoot)) {{
        return;
      }}
      seenNearbyNodes.add(candidate);
      nearbyNodes.push(candidate);
    }};
    enqueue(node);
    let cursor = node;
    for (let depth = 0; depth < 3 && cursor; depth += 1) {{
      enqueue(cursor.parentElement);
      enqueue(cursor.previousElementSibling);
      enqueue(cursor.nextElementSibling);
      if (cursor.parentElement) {{
        enqueue(cursor.parentElement.previousElementSibling);
        enqueue(cursor.parentElement.nextElementSibling);
      }}
      if (cursor === scopeRoot) {{
        break;
      }}
      cursor = cursor.parentElement;
    }}
    return nearbyNodes;
  }};
  const state = window[stateKey] || (window[stateKey] = {{}});
  state.marker = marker;
  state.markerAttr = markerAttr;
  state.beforeUrl = cleanText(window.location && window.location.href ? window.location.href : '', 512);
  state.resolvedUrl = '';
  state.popupUrl = '';
  state.dialogUrl = '';
  state.clickedMarker = '';
  state.clickedKind = '';
  const rememberUrl = (value) => {{
    if (value == null) {{
      return '';
    }}
    let candidate = cleanText(value, 512);
    if (!candidate) {{
      return '';
    }}
    if (candidate.startsWith('//')) {{
      candidate = window.location.protocol + candidate;
    }}
    try {{
      candidate = new URL(candidate, window.location.href).toString();
    }} catch (error) {{
    }}
    if (!state.resolvedUrl) {{
      state.resolvedUrl = candidate;
    }}
    return candidate;
  }};
  state.rememberUrl = rememberUrl;
  const inspectNode = (entry) => {{
    if (!entry) {{
      return;
    }}
    try {{
      if (typeof entry.href === 'string' && entry.href) {{
        rememberUrl(entry.href);
      }}
    }} catch (error) {{
    }}
    if (typeof entry.getAttribute === 'function') {{
      for (const attrName of ['href', 'data-url', 'data-href', 'data-link', 'data-target', 'title', 'aria-label', 'onclick']) {{
        try {{
          rememberUrl(entry.getAttribute(attrName) || '');
        }} catch (error) {{
        }}
      }}
    }}
  }};
  if (!state.installed) {{
    state.installed = true;
    const wrapMethod = (owner, key, assignPopup = false) => {{
      if (!owner) {{
        return;
      }}
      let original = null;
      try {{
        original = owner[key];
      }} catch (error) {{
        return;
      }}
      if (typeof original !== 'function') {{
        return;
      }}
      owner[key] = function(...args) {{
        let recovered = '';
        try {{
          recovered = rememberUrl(args[0]);
        }} catch (error) {{
        }}
        if (assignPopup && recovered) {{
          state.popupUrl = recovered;
        }}
        return original.apply(this, args);
      }};
    }};
    wrapMethod(window, 'open', true);
    wrapMethod(window.history, 'pushState');
    wrapMethod(window.history, 'replaceState');
    try {{
      wrapMethod(window.location, 'assign');
      wrapMethod(window.location, 'replace');
    }} catch (error) {{
    }}
    document.addEventListener(
      'click',
      (event) => {{
        const path = event && typeof event.composedPath === 'function' ? event.composedPath() : [];
        let markedNode = null;
        for (const entry of path) {{
          if (entry && typeof entry.getAttribute === 'function' && entry.getAttribute(markerAttr)) {{
            markedNode = entry;
            break;
          }}
        }}
        if (!markedNode && event && event.target && typeof event.target.closest === 'function') {{
          markedNode = event.target.closest(`[${{markerAttr}}]`);
        }}
        if (!markedNode) {{
          return;
        }}
        state.clickedMarker = cleanText(markedNode.getAttribute(markerAttr) || '', 80);
        state.clickedKind = cleanText(markedNode.getAttribute('data-ig-live-bio-link-recovery-kind') || '', 80);
        inspectNode(markedNode);
        inspectNode(event && event.target ? event.target : null);
        for (const entry of path) {{
          inspectNode(entry);
        }}
      }},
      true,
    );
  }}
  for (const existing of Array.from(main.querySelectorAll(`[${{markerAttr}}]`)).slice(0, maxNodes)) {{
    existing.removeAttribute(markerAttr);
    existing.removeAttribute('data-ig-live-bio-link-recovery-kind');
  }}
  const seeds = [];
  const seenSeedNodes = new Set();
  for (const root of scopeRoots) {{
    const nodes = [];
    if (root.matches && root.matches(selector)) {{
      nodes.push(root);
    }}
    for (const node of Array.from(root.querySelectorAll(selector))) {{
      nodes.push(node);
    }}
    for (const node of nodes) {{
      if (!node || seenSeedNodes.has(node)) {{
        continue;
      }}
      seenSeedNodes.add(node);
      const scored = scoreSeed(node, root);
      if (!scored) {{
        continue;
      }}
      seeds.push(scored);
      if (seeds.length >= maxNodes) {{
        break;
      }}
    }}
    if (seeds.length >= maxNodes) {{
      break;
    }}
  }}
  seeds.sort((left, right) => {{
    if (right.score !== left.score) {{
      return right.score - left.score;
    }}
    return right.labelLength - left.labelLength;
  }});
  const candidateGroups = {{
    direct_anchor: [],
    nested_clickable_child: [],
    parent_anchor: [],
    adjacent_bio_surface: [],
  }};
  const seenCandidateNodes = new Set();
  const registerCandidate = (kind, node, seed) => {{
    if (!node || seenCandidateNodes.has(node) || !isVisible(node)) {{
      return;
    }}
    if (!isWithinRoot(node, seed.scopeRoot) || node.closest('nav, footer, aside, [role="tablist"]')) {{
      return;
    }}
    if (kind === 'direct_anchor' && !(node.matches && node.matches('a[href]'))) {{
      return;
    }}
    if (kind !== 'direct_anchor' && !isClickable(node)) {{
      return;
    }}
    seenCandidateNodes.add(node);
    candidateGroups[kind].push({{
      node,
      score: seed.score,
      labelLength: cleanText(node.innerText || node.textContent || '', 280).length,
    }});
  }};
  for (const seed of seeds) {{
    const node = seed.node;
    if (node.matches && node.matches('a[href]')) {{
      registerCandidate('direct_anchor', node, seed);
    }}
    if (typeof node.querySelectorAll === 'function') {{
      const nestedClickables = Array.from(node.querySelectorAll(selector)).slice(0, maxNestedCandidates);
      for (const nested of nestedClickables) {{
        if (nested === node) {{
          continue;
        }}
        registerCandidate('nested_clickable_child', nested, seed);
      }}
    }}
    let ancestor = node.parentElement;
    while (ancestor && ancestor !== seed.scopeRoot.parentElement) {{
      if (isWithinRoot(ancestor, seed.scopeRoot) && isClickable(ancestor)) {{
        registerCandidate('parent_anchor', ancestor, seed);
        break;
      }}
      if (ancestor === seed.scopeRoot) {{
        break;
      }}
      ancestor = ancestor.parentElement;
    }}
    const nearbyNodes = collectNearbyNodes(node, seed.scopeRoot);
    for (const nearbyNode of nearbyNodes) {{
      if (nearbyNode && nearbyNode !== node && isClickable(nearbyNode)) {{
        registerCandidate('adjacent_bio_surface', nearbyNode, seed);
      }}
      if (!nearbyNode || typeof nearbyNode.querySelectorAll !== 'function') {{
        continue;
      }}
      const nearbyClickables = Array.from(nearbyNode.querySelectorAll(selector)).slice(0, maxNearbyClickables);
      for (const nearbyClickable of nearbyClickables) {{
        if (nearbyClickable === node) {{
          continue;
        }}
        registerCandidate('adjacent_bio_surface', nearbyClickable, seed);
      }}
    }}
  }}
  const orderedKinds = ['direct_anchor', 'nested_clickable_child', 'parent_anchor', 'adjacent_bio_surface'];
  const orderedCandidates = [];
  for (const kind of orderedKinds) {{
    candidateGroups[kind].sort((left, right) => {{
      if (right.score !== left.score) {{
        return right.score - left.score;
      }}
      return right.labelLength - left.labelLength;
    }});
    for (const candidate of candidateGroups[kind]) {{
      if (orderedCandidates.length >= maxNodes) {{
        break;
      }}
      const markerValue = `ig-live-bio-link-recovery-${{orderedCandidates.length}}`;
      candidate.node.setAttribute(markerAttr, markerValue);
      candidate.node.setAttribute('data-ig-live-bio-link-recovery-kind', kind);
      orderedCandidates.push({{
        selector: `[${{markerAttr}}="${{markerValue}}"]`,
        label: kind,
      }});
    }}
    if (orderedCandidates.length >= maxNodes) {{
      break;
    }}
  }}
  const sample = orderedCandidates.length
    ? orderedCandidates.slice(0, 3).map((entry) => entry.label || '').filter(Boolean).join(',')
    : '-';
  return {{
    candidates: orderedCandidates,
    sample,
  }};
}}
"""
            )
        except Exception:
            return []
        candidates: List[Dict[str, str]] = []
        fallback_label = "selected_candidate"
        if isinstance(raw_payload, dict):
            raw_candidates = raw_payload.get("candidates") or []
        elif isinstance(raw_payload, (list, tuple)):
            raw_candidates = raw_payload
        else:
            raw_candidates = [raw_payload]
        for raw_entry in raw_candidates:
            candidate_entry = _coerce_candidate_entry(raw_entry, fallback_label)
            if candidate_entry:
                candidates.append(candidate_entry)
        return candidates

    def _snapshot_context_pages() -> List[Any]:
        context = getattr(page, "context", None)
        if context is None:
            return []
        pages = getattr(context, "pages", None)
        try:
            if callable(pages):
                pages = pages()
        except Exception:
            return []
        return list(pages or [])

    def _reset_recovery_state() -> None:
        try:
            page.evaluate(
                f"""
() => {{
  const marker = {marker_name_json};
  const state = window["{_INSTAGRAM_LIVE_ONEHOP_INTERACTION_STATE_KEY}"]
    || (window["{_INSTAGRAM_LIVE_ONEHOP_INTERACTION_STATE_KEY}"] = {{}});
  state.marker = marker;
  state.beforeUrl = window.location && typeof window.location.href === 'string'
    ? window.location.href.trim()
    : '';
  state.resolvedUrl = '';
  state.popupUrl = '';
  state.dialogUrl = '';
  state.clickedMarker = '';
  state.clickedKind = '';
  return true;
}}
"""
            )
        except Exception:
            pass

    def _read_post_click_state() -> Dict[str, Any]:
        try:
            raw_state = page.evaluate(
                f"""
() => {{
  const marker = {marker_name_json};
  const markerAttr = {marker_attr_json};
  const state = window["{_INSTAGRAM_LIVE_ONEHOP_INTERACTION_STATE_KEY}"];
  if (!state || state.marker !== marker) {{
    return '';
  }}
  const cleanText = (value, limit = 512) => {{
    if (value == null) return '';
    return String(value).replace(/\\s+/g, ' ').trim().slice(0, limit);
  }};
  const isVisible = (el) => {{
    if (!el) return false;
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  const rememberUrl = typeof state.rememberUrl === 'function'
    ? state.rememberUrl
    : (value) => {{
        if (value == null) {{
          return '';
        }}
        let candidate = cleanText(value, 512);
        if (!candidate) {{
          return '';
        }}
        if (candidate.startsWith('//')) {{
          candidate = window.location.protocol + candidate;
        }}
        try {{
          candidate = new URL(candidate, window.location.href).toString();
        }} catch (error) {{
        }}
        if (!state.resolvedUrl) {{
          state.resolvedUrl = candidate;
        }}
        return candidate;
      }};
  const inspectNode = (entry) => {{
    if (!entry) {{
      return;
    }}
    try {{
      if (typeof entry.href === 'string' && entry.href) {{
        rememberUrl(entry.href);
      }}
    }} catch (error) {{
    }}
    if (typeof entry.getAttribute === 'function') {{
      for (const attrName of ['href', 'data-url', 'data-href', 'data-link', 'data-target', 'title', 'aria-label', 'onclick']) {{
        try {{
          rememberUrl(entry.getAttribute(attrName) || '');
        }} catch (error) {{
        }}
      }}
    }}
  }};
  const inspectDialog = (dialog) => {{
    if (!dialog || !isVisible(dialog)) {{
      return '';
    }}
    inspectNode(dialog);
    if (typeof dialog.querySelectorAll !== 'function') {{
      return '';
    }}
    const dialogNodes = Array.from(dialog.querySelectorAll('a[href], button, [role="button"], [role="link"]')).slice(0, 8);
    for (const entry of dialogNodes) {{
      inspectNode(entry);
      if (state.resolvedUrl) {{
        return state.resolvedUrl;
      }}
    }}
    return '';
  }};
  const inspectNearby = (node, scopeRoot) => {{
    if (!node || !scopeRoot) {{
      return;
    }}
    const nearbyNodes = [];
    const seenNearbyNodes = new Set();
    const enqueue = (candidate) => {{
      if (!candidate || seenNearbyNodes.has(candidate) || nearbyNodes.length >= 10) {{
        return;
      }}
      if (candidate !== scopeRoot && !(scopeRoot.contains && scopeRoot.contains(candidate))) {{
        return;
      }}
      seenNearbyNodes.add(candidate);
      nearbyNodes.push(candidate);
    }};
    enqueue(node);
    let cursor = node;
    for (let depth = 0; depth < 3 && cursor; depth += 1) {{
      enqueue(cursor.parentElement);
      enqueue(cursor.previousElementSibling);
      enqueue(cursor.nextElementSibling);
      if (cursor.parentElement) {{
        enqueue(cursor.parentElement.previousElementSibling);
        enqueue(cursor.parentElement.nextElementSibling);
      }}
      if (cursor === scopeRoot) {{
        break;
      }}
      cursor = cursor.parentElement;
    }}
    for (const nearbyNode of nearbyNodes) {{
      inspectNode(nearbyNode);
      if (state.resolvedUrl) {{
        return;
      }}
      if (!nearbyNode || typeof nearbyNode.querySelectorAll !== 'function') {{
        continue;
      }}
      const nearbyAnchors = Array.from(nearbyNode.querySelectorAll('a[href], [data-url], [data-href], [data-link], [data-target]')).slice(0, 6);
      for (const entry of nearbyAnchors) {{
        inspectNode(entry);
        if (state.resolvedUrl) {{
          return;
        }}
      }}
    }}
  }};
  const activeNode = state.clickedMarker
    ? document.querySelector(`[${{markerAttr}}="${{state.clickedMarker}}"]`)
    : document.querySelector(`[${{markerAttr}}]`);
  if (activeNode) {{
    inspectNode(activeNode);
    const scopeRoot = activeNode.closest('header, section, article') || activeNode.closest('main') || activeNode.parentElement;
    inspectNearby(activeNode, scopeRoot);
  }}
  let dialogUrl = '';
  for (const dialog of Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [data-testid*="dialog"]')).slice(0, 3)) {{
    dialogUrl = inspectDialog(dialog);
    if (dialogUrl) {{
      state.dialogUrl = dialogUrl;
      break;
    }}
  }}
  const resolvedUrl = cleanText(state.resolvedUrl || '', 512);
  const popupUrl = cleanText(state.popupUrl || '', 512);
  const currentUrl = window.location && typeof window.location.href === 'string'
    ? window.location.href.trim()
    : '';
  const navChange = currentUrl && currentUrl !== (state.beforeUrl || '') ? 1 : 0;
  return {{
    resolved_url: resolvedUrl || (navChange ? currentUrl : ''),
    nav_change: navChange,
    popup_url: popupUrl,
    dialog_url: cleanText(state.dialogUrl || dialogUrl || '', 512),
  }};
}}
"""
            )
        except Exception:
            return {"url": "", "nav_change": 0, "popup": 0, "dialog": 0}
        if isinstance(raw_state, dict):
            resolved_url = cell_to_str(raw_state.get("resolved_url", "")).strip()
            popup_url = cell_to_str(raw_state.get("popup_url", "")).strip()
            dialog_url = cell_to_str(raw_state.get("dialog_url", "")).strip()
            nav_change = 1 if raw_state.get("nav_change") else 0
            return {
                "url": resolved_url,
                "nav_change": nav_change,
                "popup": 1 if popup_url else 0,
                "dialog": 1 if dialog_url else 0,
                "popup_url": popup_url,
                "dialog_url": dialog_url,
            }
        return {
            "url": cell_to_str(raw_state).strip(),
            "nav_change": 0,
            "popup": 0,
            "dialog": 0,
            "popup_url": "",
            "dialog_url": "",
        }

    candidates = _load_candidates()
    sample = ",".join(
        [candidate["label"] for candidate in candidates[:3] if cell_to_str(candidate.get("label", "")).strip()]
    ) or "-"
    _log_recover(f"[IG Recover] candidates count={len(candidates)} sample={sample}")
    if not candidates:
        _log_recover("[IG Recover] no_post_click_url")
        return ""

    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    for candidate in candidates:
        selector = cell_to_str(candidate.get("selector", "")).strip()
        label = cell_to_str(candidate.get("label", "")).strip() or "selected_candidate"
        if not selector:
            continue
        _reset_recovery_state()
        before_pages = _snapshot_context_pages()
        before_page_ids = {id(entry) for entry in before_pages}
        before_url = cell_to_str(getattr(page, "url", "")).strip()
        clicked = 0
        try:
            page.click(selector, timeout=750, force=True)
            clicked = 1
        except TypeError:
            try:
                page.click(selector)
                clicked = 1
            except Exception:
                clicked = 0
        except Exception:
            clicked = 0
        if callable(wait_for_timeout):
            try:
                wait_for_timeout(250)
            except Exception:
                pass
        popup_url = ""
        popup_flag = 0
        for popup_page in _snapshot_context_pages():
            if id(popup_page) in before_page_ids or popup_page is page:
                continue
            popup_url = cell_to_str(getattr(popup_page, "url", "")).strip()
            if popup_url:
                popup_flag = 1
                break
        after_url = cell_to_str(getattr(page, "url", "")).strip()
        post_click_state = _read_post_click_state()
        nav_change = 1 if after_url and after_url != before_url else int(post_click_state.get("nav_change") or 0)
        recovered_url = (
            popup_url
            or cell_to_str(post_click_state.get("popup_url", "")).strip()
            or cell_to_str(post_click_state.get("url", "")).strip()
            or cell_to_str(post_click_state.get("dialog_url", "")).strip()
            or (after_url if nav_change else "")
        )
        dialog_flag = 1 if cell_to_str(post_click_state.get("dialog_url", "")).strip() else int(
            post_click_state.get("dialog") or 0
        )
        popup_flag = popup_flag or int(post_click_state.get("popup") or 0)
        _log_recover(
            f"[IG Recover] click_attempt candidate={label} clicked={clicked} "
            f"popup={popup_flag} nav_change={nav_change} dialog={dialog_flag}"
        )
        if recovered_url:
            _log_recover(f"[IG Recover] recovered url={recovered_url}")
            return recovered_url
    _log_recover("[IG Recover] no_post_click_url")
    return ""


def _collect_instagram_live_profile_clickable_bio_link_urls(
    page: Any,
    *,
    profile_url: str = "",
    raw_control_values: Optional[Iterable[Any]] = None,
) -> List[str]:
    truth_enabled = _instagram_truth_logging_enabled(profile_url)
    candidate_urls: List[str] = []
    seen: Set[str] = set()

    def _add_candidate(raw_value: Any) -> None:
        normalised = _normalise_instagram_bio_link_fetch_url(raw_value, base_url=profile_url)
        if not normalised or normalised in seen:
            return
        seen.add(normalised)
        candidate_urls.append(normalised)

    def _ingest_raw_value(raw_value: Any) -> None:
        raw_text = cell_to_str(raw_value).strip()
        if not raw_text:
            return
        extracted_redirect_target = False
        try:
            parsed = urllib.parse.urlparse(raw_text)
        except Exception:
            parsed = urllib.parse.urlparse("")
        query_values = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)
        for key, values in query_values.items():
            if key.lower() not in _INSTAGRAM_LIVE_ONEHOP_REDIRECT_QUERY_KEYS:
                continue
            for value in values:
                decoded_value = urllib.parse.unquote(cell_to_str(value)).strip()
                if decoded_value.startswith("//"):
                    decoded_value = "https:" + decoded_value
                if not decoded_value:
                    continue
                extracted_redirect_target = True
                _add_candidate(decoded_value)
                for match in re.findall(r"https?://[^\s'\"<>()]+", decoded_value):
                    _add_candidate(match.rstrip(".,;:!?)]}"))
        decoded_text = urllib.parse.unquote(raw_text)
        if not extracted_redirect_target:
            _add_candidate(raw_text)
            for match in re.findall(r"https?://[^\s'\"<>()]+", decoded_text):
                _add_candidate(match.rstrip(".,;:!?)]}"))

    collected_raw_control_values = (
        list(raw_control_values)
        if raw_control_values is not None
        else _collect_instagram_live_profile_clickable_control_values(page)
    )
    if truth_enabled:
        _emit_instagram_truth(
            f"[IG Truth] bio_link_urls_enter profile_url={profile_url or '-'} "
            f"raw_control_values count={len(collected_raw_control_values)} "
            f"sample={_instagram_bio_link_log_sample(collected_raw_control_values)}"
        )
    for raw_value in collected_raw_control_values:
        _ingest_raw_value(raw_value)
    usable_raw_values = [
        cell_to_str(raw_value).strip()
        for raw_value in collected_raw_control_values
        if cell_to_str(raw_value).strip()
        and not cell_to_str(raw_value).strip().startswith(_INSTAGRAM_LIVE_ONEHOP_INTERACTION_ONLY_SENTINEL_PREFIX)
    ]
    has_usable_raw_values = bool(usable_raw_values)
    has_candidate_urls = bool(candidate_urls)
    should_run_interaction_fallback = not has_candidate_urls and not has_usable_raw_values
    if should_run_interaction_fallback:
        _ingest_raw_value(_recover_instagram_live_profile_clickable_bio_link_url_via_interaction(page))
    if truth_enabled:
        _emit_instagram_truth(
            f"[IG Truth] bio_link_urls_output candidate_urls count={len(candidate_urls)} "
            f"sample={_instagram_bio_link_log_sample(candidate_urls)} "
            f"usable_raw_values={1 if has_usable_raw_values else 0} "
            f"interaction_fallback={1 if should_run_interaction_fallback else 0}"
        )
    return candidate_urls


def _collect_instagram_runtime_bio_link_structured_payloads(page: Any) -> List[Any]:
    if page is None or not hasattr(page, "evaluate"):
        return []
    try:
        raw_payloads = page.evaluate(
            f"""
() => {{
  const maxScriptChars = {_INSTAGRAM_BIO_LINK_STRUCTURED_MAX_SCRIPT_CHARS};
  const maxNodes = {_INSTAGRAM_BIO_LINK_STRUCTURED_MAX_NODES};
  const directTextKeys = new Set({json.dumps(sorted(_INSTAGRAM_DIRECT_RUNTIME_TEXT_KEYS))});
  const directEntityContainerKeys = new Set(
    {json.dumps(sorted(_INSTAGRAM_DIRECT_RUNTIME_TEXT_ENTITY_CONTAINER_KEYS))}
  );
  const directEmailKeyPattern = /(^email$|_email$)/i;
  const payloads = [];
  const seen = new Set();
  const addPayload = (value) => {{
    if (!value || typeof value !== 'object') return;
    try {{
      const key = JSON.stringify(value);
      if (!key || seen.has(key)) return;
      seen.add(key);
      payloads.push(value);
    }} catch (_error) {{
      // Skip non-serializable values.
    }}
  }};
  const addDirectProfilePayload = (source) => {{
    if (!source || typeof source !== 'object' || Array.isArray(source)) return;
    const extracted = {{}};
    for (const [rawKey, value] of Object.entries(source)) {{
      const key = String(rawKey || '').trim().toLowerCase();
      if (!key || value == null) {{
        continue;
      }}
      if (key === 'bio_links' && Array.isArray(value)) {{
        extracted.bio_links = value;
        continue;
      }}
      if (directEmailKeyPattern.test(key) && typeof value === 'string') {{
        extracted[key] = value;
        continue;
      }}
      if (directTextKeys.has(key) && typeof value === 'string') {{
        extracted[key] = value;
        continue;
      }}
      if (directEntityContainerKeys.has(key) && value && typeof value === 'object') {{
        extracted[key] = value;
      }}
    }}
    if (Object.keys(extracted).length) {{
      addPayload(extracted);
    }}
  }};
  const collectAllowedStructures = (root) => {{
    const stack = [root];
    let nodesSeen = 0;
    while (stack.length && nodesSeen < maxNodes) {{
      const current = stack.pop();
      nodesSeen += 1;
      if (Array.isArray(current)) {{
        for (let idx = current.length - 1; idx >= 0; idx -= 1) {{
          stack.push(current[idx]);
        }}
        continue;
      }}
      if (!current || typeof current !== 'object') {{
        continue;
      }}
      const user = current.user;
      if (user && typeof user === 'object') {{
        if (Array.isArray(user.bio_links)) {{
          addPayload({{ bio_links: user.bio_links }});
        }}
        addDirectProfilePayload(user);
      }}
      const webProfileInfo = current.web_profile_info;
      if (webProfileInfo && typeof webProfileInfo === 'object') {{
        addPayload(webProfileInfo);
        addDirectProfilePayload(webProfileInfo);
      }}
      addDirectProfilePayload(current);
      for (const value of Object.values(current)) {{
        if (value && typeof value === 'object') {{
          stack.push(value);
        }}
      }}
    }}
  }};

  for (const script of Array.from(document.querySelectorAll('script'))) {{
    const text = (script.textContent || '').trim();
    if (!text || text.length > maxScriptChars) {{
      continue;
    }}
    if (!text.includes('bio_links') && !text.includes('web_profile_info')) {{
      continue;
    }}
    const type = (script.getAttribute('type') || '').toLowerCase();
    let payloadText = '';
    if (type === 'application/json' || type === 'application/ld+json' || text.startsWith('window._sharedData =')) {{
      payloadText = text;
    }} else {{
      continue;
    }}
    if (payloadText.startsWith('window._sharedData =')) {{
      payloadText = payloadText.slice('window._sharedData ='.length).trim();
    }}
    payloadText = payloadText.replace(/;\\s*$/, '').trim();
    if (!payloadText.startsWith('{{') && !payloadText.startsWith('[')) {{
      continue;
    }}
    try {{
      collectAllowedStructures(JSON.parse(payloadText));
    }} catch (_error) {{
      continue;
    }}
  }}
  for (const key of ['_sharedData', '__additionalData', '__initialData']) {{
    try {{
      const runtimeValue = window[key];
      if (runtimeValue && typeof runtimeValue === 'object') {{
        collectAllowedStructures(runtimeValue);
      }}
    }} catch (_error) {{
      continue;
    }}
  }}
  return payloads;
}}
"""
        )
    except Exception:
        return []
    if not isinstance(raw_payloads, list):
        return []
    return [payload for payload in raw_payloads if isinstance(payload, (dict, list, tuple))]


def _wait_for_instagram_runtime_bio_link_structured_payloads(
    page: Any,
    timeout_s: float,
) -> List[Any]:
    if page is None:
        return []
    timeout_ms = max(int(float(timeout_s or 0) * 1000), 0)
    payloads = _collect_instagram_runtime_bio_link_structured_payloads(page)
    if payloads or timeout_ms <= 0:
        return payloads
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        remaining_ms = int(max((deadline - time.monotonic()) * 1000, 0))
        if remaining_ms <= 0:
            break
        sleep_ms = min(100, remaining_ms)
        if callable(wait_for_timeout):
            wait_for_timeout(sleep_ms)
        else:
            time.sleep(sleep_ms / 1000.0)
        payloads = _collect_instagram_runtime_bio_link_structured_payloads(page)
        if payloads:
            return payloads
    return payloads


def _collect_instagram_bio_link_fetch_urls(
    html: str,
    *,
    soup: Optional[BeautifulSoup] = None,
    profile_url: str = "",
    log: Optional[Any] = None,
    runtime_structured_payloads: Optional[Iterable[Any]] = None,
) -> List[str]:
    html_text = html if isinstance(html, str) else str(html or "")
    source_group_names = {
        "anchor": "anchors",
        "attr": "attributes",
        "meta": "meta",
        "script": "structured_scripts",
    }
    group_stats: Dict[str, Dict[str, Any]] = {
        group_name: {
            "raw": 0,
            "kept": 0,
            "dropped": 0,
            "raw_sample": [],
            "kept_sample": [],
            "drop_reasons": Counter(),
        }
        for group_name in source_group_names.values()
    }

    def _emit_summary(final_urls: List[str]) -> None:
        if log is None:
            return
        parts: List[str] = []
        for group_name in ("anchors", "attributes", "meta", "structured_scripts"):
            stats = group_stats[group_name]
            sample = _instagram_bio_link_log_sample(
                stats["kept_sample"] or stats["raw_sample"],
                limit=1,
            )
            parts.append(
                f"{group_name}(raw={stats['raw']} kept={stats['kept']} dropped={stats['dropped']} "
                f"drop_reasons={_format_instagram_bio_link_drop_reasons(stats['drop_reasons'])} sample={sample})"
            )
        log(
            "[IG OneHop] helper_summary "
            + " ".join(parts)
            + f" total_unique={len(final_urls)} final_sample={_instagram_bio_link_log_sample(final_urls)}"
        )

    if not html_text.strip():
        _emit_summary([])
        return []
    soup_obj = soup or BeautifulSoup(html_text, "html.parser")
    candidate_groups: Dict[str, List[str]] = {
        "anchor_hub": [],
        "anchor_direct": [],
        "attr_hub": [],
        "attr_direct": [],
        "meta_hub": [],
        "meta_direct": [],
        "script_hub": [],
        "script_direct": [],
    }
    seen: Set[str] = set()

    def _add_candidate(raw_value: Any, *, source_key: str) -> None:
        group_name = source_group_names[source_key]
        stats = group_stats[group_name]
        stats["raw"] += 1
        if len(stats["raw_sample"]) < _INSTAGRAM_BIO_LINK_LOG_SAMPLE_MAX:
            stats["raw_sample"].append(raw_value)
        normalised = _normalise_instagram_bio_link_fetch_url(raw_value, base_url=profile_url)
        if not normalised:
            stats["dropped"] += 1
            stats["drop_reasons"][_classify_instagram_bio_link_fetch_url_drop(raw_value, base_url=profile_url)] += 1
            return
        if normalised in seen:
            stats["dropped"] += 1
            stats["drop_reasons"]["duplicate"] += 1
            return
        seen.add(normalised)
        stats["kept"] += 1
        if len(stats["kept_sample"]) < _INSTAGRAM_BIO_LINK_LOG_SAMPLE_MAX:
            stats["kept_sample"].append(normalised)
        host = _host(normalised)
        if host.startswith("www."):
            host = host[4:]
        if host in LINK_HUB_HOSTS:
            candidate_groups[f"{source_key}_hub"].append(normalised)
        else:
            candidate_groups[f"{source_key}_direct"].append(normalised)

    for anchor in soup_obj.select("a[href]"):
        _add_candidate(anchor.get("href"), source_key="anchor")
    for raw_value in _collect_contact_surface_attribute_values(soup_obj):
        _add_candidate(raw_value, source_key="attr")
    for raw_value in _iter_instagram_bio_link_meta_values(soup_obj):
        _add_candidate(raw_value, source_key="meta")
    for script_tag in soup_obj.find_all("script"):
        payload = _load_instagram_bio_link_structured_script_payload(script_tag)
        if payload is None:
            continue
        for raw_value in _iter_instagram_bio_link_structured_values(payload):
            _add_candidate(raw_value, source_key="script")
    for payload in runtime_structured_payloads or ():
        for raw_value in _iter_instagram_bio_link_structured_values(payload):
            _add_candidate(raw_value, source_key="script")
    ordered_keys = (
        "anchor_hub",
        "anchor_direct",
        "attr_hub",
        "attr_direct",
        "meta_hub",
        "meta_direct",
        "script_hub",
        "script_direct",
    )
    final_urls = [candidate for key in ordered_keys for candidate in candidate_groups[key]]
    _emit_summary(final_urls)
    return final_urls


def _instagram_onehop_emails_from_surface(
    session: requests.Session,
    html: str,
    *,
    profile_url: str = "",
    log: Optional[Any] = None,
    state_label: str = "bio_link_urls",
    runtime_structured_payloads: Optional[Iterable[Any]] = None,
    live_raw_control_values: Optional[Iterable[str]] = None,
    live_rendered_bio_link_urls: Optional[Iterable[str]] = None,
) -> Tuple[List[str], str, str, str]:
    html_text = html if isinstance(html, str) else str(html or "")
    soup = BeautifulSoup(html_text, "html.parser") if html_text.strip() else None
    structured_payloads: List[Any] = []
    if soup is not None:
        for script_tag in soup.find_all("script"):
            payload = _load_instagram_bio_link_structured_script_payload(script_tag)
            if payload is not None:
                structured_payloads.append(payload)
    structured_payloads.extend(
        payload for payload in (runtime_structured_payloads or ()) if isinstance(payload, (dict, list, tuple))
    )
    bio_link_urls = _collect_instagram_bio_link_fetch_urls(
        html_text,
        soup=soup,
        profile_url=profile_url,
        log=log,
        runtime_structured_payloads=runtime_structured_payloads,
    )
    static_bio_link_url_count = len(bio_link_urls)
    live_raw_values = [cell_to_str(raw_value).strip() for raw_value in (live_raw_control_values or ()) if cell_to_str(raw_value).strip()]
    live_normalised_urls = [cell_to_str(raw_value).strip() for raw_value in (live_rendered_bio_link_urls or ()) if cell_to_str(raw_value).strip()]
    live_clickable_candidates: List[str] = []
    live_seen = set(bio_link_urls)
    for raw_value in live_normalised_urls:
        normalised = _normalise_instagram_bio_link_fetch_url(raw_value, base_url=profile_url)
        if not normalised or normalised in live_seen:
            continue
        live_seen.add(normalised)
        live_clickable_candidates.append(normalised)
    if callable(log) and live_rendered_bio_link_urls is not None:
        log(
            "[IG Probe] raw_control_values "
            f"count={len(live_raw_values)} sample={_instagram_bio_link_log_sample(live_raw_values)}"
        )
        log(
            "[IG Probe] normalised_urls "
            f"count={len(live_normalised_urls)} sample={_instagram_bio_link_log_sample(live_normalised_urls)}"
        )
        log(
            "[IG Probe] live_admission "
            f"admitted={len(live_clickable_candidates)} sample={_instagram_bio_link_log_sample(live_clickable_candidates)}"
        )
        merged_bio_link_url_count = static_bio_link_url_count + len(live_clickable_candidates)
        log(
            "[IG OneHop] primary_candidate_merge "
            f"static_count={static_bio_link_url_count} "
            f"live_admitted={len(live_clickable_candidates)} "
            f"merged_count={merged_bio_link_url_count} "
            f"live_sample={_instagram_bio_link_log_sample(live_clickable_candidates)}"
        )
    if live_clickable_candidates:
        bio_link_urls.extend(live_clickable_candidates)
    if callable(log):
        log(
            f"[IG OneHop] {state_label} state={'non_empty' if bio_link_urls else 'empty'} "
            f"count={len(bio_link_urls)} sample={_instagram_bio_link_log_sample(bio_link_urls)}"
        )
    structured_emails = _collect_instagram_bio_link_structured_emails(structured_payloads)
    if structured_emails:
        return (structured_emails, profile_url, "regex", "")
    if not bio_link_urls:
        if callable(log):
            log(f"[IG OneHop] {state_label}_empty")
        return ([], "", "regex", "")

    onehop_target = _select_instagram_onehop_target(
        bio_link_urls,
        log=log,
    )
    if not onehop_target:
        return ([], "", "regex", "")

    if callable(log):
        log(f"[IG OneHop] onehop_selected_target={onehop_target}")
    if not _instagram_onehop_target_is_meaningful_fetch_target(onehop_target):
        if callable(log):
            log(f"[IG OneHop] onehop_fetch_skipped reason=no_meaningful_target url={onehop_target}")
        return ([], "", "regex", "")

    if callable(log):
        log(f"[IG OneHop] onehop_fetch_attempted={onehop_target}")
    bio_link_result = _fetch_website_html_bounded(
        session,
        onehop_target,
        timeout_s=WEBSITE_EMAIL_TIMEOUT,
        max_bytes=WEBSITE_EMAIL_MAX_BYTES,
    )
    if not (bio_link_result.is_html and bio_link_result.html):
        return ([], "", "regex", onehop_target)

    all_ig_emails, used_mailto = _extract_website_emails_from_html(
        bio_link_result.html
    )
    if not all_ig_emails:
        return ([], "", "regex", onehop_target)

    return (
        all_ig_emails,
        bio_link_result.final_url or onehop_target,
        "mailto" if used_mailto else "regex",
        onehop_target,
    )


_INSTAGRAM_HIDDEN_CONTACT_LABEL_PRIORITY = {
    "show email": 0,
    "bookings email": 1,
    "email": 2,
    "e mail": 2,
    "contact info": 3,
    "contact": 4,
    "get in touch": 5,
}

_INSTAGRAM_HIDDEN_CONTACT_INTERACTIVE_SELECTOR = (
    "a[href], button, div[role='button'], span[role='button'], a[role='button']"
)


def _normalise_instagram_hidden_contact_label(value: Any) -> str:
    raw_value = unidecode(cell_to_str(value)).lower()
    return re.sub(r"[^a-z0-9]+", " ", raw_value).strip()


def _instagram_hidden_contact_candidate_priority(
    candidate: Dict[str, Any],
) -> Optional[Tuple[int, int, int]]:
    field_values = (
        (0, candidate.get("text")),
        (1, candidate.get("aria_label")),
        (2, candidate.get("title")),
        (3, candidate.get("value")),
    )
    best_priority: Optional[Tuple[int, int, int]] = None
    for field_rank, raw_value in field_values:
        normalized = _normalise_instagram_hidden_contact_label(raw_value)
        if not normalized:
            continue
        label_priority = _INSTAGRAM_HIDDEN_CONTACT_LABEL_PRIORITY.get(normalized)
        if label_priority is None:
            continue
        priority = (label_priority, field_rank, int(candidate.get("dom_index", 0) or 0))
        if best_priority is None or priority < best_priority:
            best_priority = priority
    return best_priority


def _collect_instagram_hidden_contact_candidates(page: Any) -> List[Dict[str, Any]]:
    if page is None or not hasattr(page, "evaluate"):
        return []
    try:
        raw_candidates = page.evaluate(
            f"""
() => {{
  const selectors = "{_INSTAGRAM_HIDDEN_CONTACT_INTERACTIVE_SELECTOR}";
  const nodes = Array.from(document.querySelectorAll(selectors));
  const isVisible = (el) => {{
    if (!el) return false;
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }};
  return nodes
    .filter(isVisible)
    .map((el, index) => {{
      const marker = `ig-hidden-contact-${{index}}`;
      el.setAttribute('data-ig-hidden-contact', marker);
      return {{
        selector: `[data-ig-hidden-contact="${{marker}}"]`,
        dom_index: index,
        text: (el.innerText || el.textContent || '').trim(),
        aria_label: (el.getAttribute('aria-label') || '').trim(),
        title: (el.getAttribute('title') || '').trim(),
        value: (el.getAttribute('value') || '').trim(),
      }};
    }});
}}
"""
        )
    except Exception:
        return []

    candidates: List[Dict[str, Any]] = []
    for raw_candidate in raw_candidates or []:
        if not isinstance(raw_candidate, dict):
            continue
        selector = cell_to_str(raw_candidate.get("selector"))
        if not selector:
            continue
        candidate = dict(raw_candidate)
        priority = _instagram_hidden_contact_candidate_priority(candidate)
        if priority is None:
            continue
        candidate["_priority"] = priority
        candidates.append(candidate)
    candidates.sort(key=lambda item: item.get("_priority") or (999, 999, 999))
    return candidates


def _click_instagram_hidden_contact_candidate(page: Any, candidate: Dict[str, Any]) -> bool:
    selector = cell_to_str(candidate.get("selector"))
    if page is None or not selector:
        return False
    try:
        page.click(selector, timeout=750)
    except TypeError:
        try:
            page.click(selector)
        except Exception:
            return False
    except Exception:
        return False
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        try:
            wait_for_timeout(250)
        except Exception:
            pass
    return True


def _extract_instagram_hidden_contact_emails(html: str) -> Tuple[List[str], str]:
    html_text = html if isinstance(html, str) else str(html or "")
    if not html_text.strip():
        return ([], "regex")
    soup = BeautifulSoup(html_text, "html.parser")
    emails = _extract_instagram_profile_candidate_emails(html_text, soup=soup)
    _, used_mailto = _mailto_emails_from_soup(soup)
    return (emails, "mailto" if used_mailto else "regex")


def _instagram_profile_requests_html_usable(status: Optional[int], html: str) -> bool:
    if not _instagram_profile_fetch_usable(status, html):
        return False
    html_text = html if isinstance(html, str) else str(html or "")
    soup = BeautifulSoup(html_text, "html.parser")
    if _extract_instagram_profile_candidate_emails(html_text, soup=soup):
        return True
    return soup.select_one(_INSTAGRAM_REQUIRED_SELECTOR) is not None


@dataclass
class InstagramLivePageBridge:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    owns_browser_stack: bool = True
    closed: bool = False

    def snapshot_html(self) -> str:
        if self.closed:
            return ""
        try:
            return cell_to_str(self.page.content())
        except Exception:
            return ""

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.page.close()
        except Exception:
            pass
        if not self.owns_browser_stack:
            return
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            stop_fn = getattr(self.playwright, "stop", None)
            if callable(stop_fn):
                stop_fn()
            else:
                close_fn = getattr(self.playwright, "close", None)
                if callable(close_fn):
                    close_fn()
        except Exception:
            pass


@dataclass
class InstagramProfileFetchResult:
    html: str
    status: Optional[int]
    live_page: Optional[InstagramLivePageBridge] = None


def _load_instagram_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _open_instagram_live_page_bridge(
    url: str,
    *,
    timeout_s: float = HTTP_TIMEOUT,
) -> Optional[InstagramLivePageBridge]:
    def _debug_probe_preview(value: Any, limit: int = 1200) -> str:
        text = cell_to_str(value).replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[:limit]

    playwright = None
    browser = None
    context = None
    page = None
    owns_browser_stack = True
    persistent_profile_dir = str(os.getenv("IG_PERSISTENT_PROFILE_DIR") or "").strip()
    if persistent_profile_dir:
        persistent_profile_dir = os.path.abspath(os.path.expanduser(persistent_profile_dir))

    def _env_flag(name: str) -> bool:
        return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}

    def _profile_has_saved_state(profile_dir: str) -> bool:
        if not profile_dir or not os.path.isdir(profile_dir):
            return False
        try:
            with os.scandir(profile_dir) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    return True
        except Exception:
            return False
        return False

    def _page_url(current_page: Any) -> str:
        try:
            return cell_to_str(getattr(current_page, "url", "")).strip()
        except Exception:
            return ""

    def _is_login_redirect(current_page: Any) -> bool:
        return "/accounts/login" in _page_url(current_page).lower()

    persistent_profile_exists = bool(persistent_profile_dir and os.path.isdir(persistent_profile_dir))
    persistent_profile_reused = _profile_has_saved_state(persistent_profile_dir)
    persistent_headed = bool(persistent_profile_dir) and _env_flag("IG_BRIDGE_HEADED")
    print(
        f"[IG Session] persistent_context={1 if persistent_profile_dir else 0} "
        f"path={persistent_profile_dir or '<unset>'} "
        f"exists={1 if persistent_profile_exists else 0} "
        f"headed={1 if persistent_headed else 0} "
        f"reused={1 if persistent_profile_reused else 0}"
    )
    try:
        if persistent_profile_dir:
            try:
                sync_playwright = _load_instagram_playwright()
                playwright = sync_playwright().start()
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=persistent_profile_dir,
                    headless=not persistent_headed,
                )
                browser = None
            except Exception as persistent_error:
                print(
                    "[IG Session] persistent_context_launch_failed "
                    f"path={persistent_profile_dir} error={persistent_error!r} fallback=1"
                )
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if playwright is not None:
                    try:
                        playwright.stop()
                    except Exception:
                        pass
                playwright = None
                browser = None
                context = None
        if context is None:
            shared_job_browser = getattr(html_fetcher, "_JOB_BROWSERS", {}).get("global")
            if shared_job_browser is not None:
                shared_playwright = getattr(shared_job_browser, "playwright", None)
                shared_browser = getattr(shared_job_browser, "browser", None)
                shared_context = getattr(shared_job_browser, "context", None)
                if shared_playwright is not None and shared_browser is not None and shared_context is not None:
                    playwright = shared_playwright
                    browser = shared_browser
                    context = shared_context
                    owns_browser_stack = False
        if context is None:
            sync_playwright = _load_instagram_playwright()
            playwright = sync_playwright().start()
            headless = str(os.getenv("PLAYWRIGHT_HEADLESS", "1")).lower() not in {"0", "false", "off"}
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        if persistent_profile_dir and persistent_headed and _is_login_redirect(page):
            manual_login_timeout_s = max(float(timeout_s or 0), 300.0)
            print(
                f"[IG Session] awaiting_manual_login=1 path={persistent_profile_dir} "
                f"timeout_s={manual_login_timeout_s:g}"
            )
            wait_for_function = getattr(page, "wait_for_function", None)
            if callable(wait_for_function):
                try:
                    wait_for_function(
                        "() => !(window.location.pathname || '').includes('/accounts/login')",
                        timeout=manual_login_timeout_s * 1000,
                    )
                except Exception:
                    pass
            else:
                deadline = time.time() + manual_login_timeout_s
                while time.time() < deadline:
                    if not _is_login_redirect(page):
                        break
                    time.sleep(1.0)
            if not _is_login_redirect(page):
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            landed_url: Any = ""
            landed_title: Any = ""
            landed_html: Any = ""
            landed_body_text: Any = ""

            try:
                landed_url = getattr(page, "url", "")
            except Exception as probe_error:
                landed_url = f"<error: {probe_error!r}>"

            try:
                landed_title = page.title()
            except Exception as probe_error:
                landed_title = f"<error: {probe_error!r}>"

            try:
                landed_html = page.content()
            except Exception as probe_error:
                landed_html = f"<error: {probe_error!r}>"

            try:
                landed_body_text = page.evaluate(
                    "(document.body && (document.body.innerText || document.body.textContent)) || ''"
                )
            except Exception as probe_error:
                landed_body_text = f"<error: {probe_error!r}>"

            try:
                landed_html_len: Any = len(cell_to_str(landed_html))
            except Exception as probe_error:
                landed_html_len = f"<error: {probe_error!r}>"

            try:
                landed_body_text_len: Any = len(cell_to_str(landed_body_text))
            except Exception as probe_error:
                landed_body_text_len = f"<error: {probe_error!r}>"

            print(f"DEBUG IG: landed_url = {_debug_probe_preview(landed_url)}")
            print(f"DEBUG IG: landed_title = {_debug_probe_preview(landed_title)}")
            print(f"DEBUG IG: landed_html_len = {landed_html_len}")
            print(f"DEBUG IG: landed_body_text_len = {landed_body_text_len}")
            print(f"DEBUG IG: landed_html_head = {_debug_probe_preview(landed_html)}")
            print(f"DEBUG IG: landed_body_text_head = {_debug_probe_preview(landed_body_text)}")
        except Exception as probe_error:
            print(f"DEBUG IG: landed page probe failed: {probe_error!r}")
        surface_ready, failure_reason = _wait_for_instagram_live_profile_surface(
            page,
            url,
            timeout_s=timeout_s,
        )
        if not surface_ready:
            if failure_reason == "not_render_ready":
                raise RuntimeError("RUNTIME_PAGE_STATE_NOT_RENDER_READY")
            raise RuntimeError("RUNTIME_PAGE_STATE_NOT_PROFILE_SURFACE")
        return InstagramLivePageBridge(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            owns_browser_stack=owns_browser_stack,
        )
    except Exception as e:
        print("DEBUG IG: live page bridge failed:", repr(e))
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if owns_browser_stack:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
        return None


def _request_instagram_profile_html(session: requests.Session, url: str) -> Tuple[str, Optional[int]]:
    if not session or not url:
        return ("", None)
    html = ""
    status = None
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False)
        status = getattr(resp, "status_code", None)
        html = getattr(resp, "text", "") or ""
    except Exception:
        status = None
        html = ""
    return (html, status)


def _fetch_instagram_profile_result(
    session: requests.Session,
    url: str,
    *,
    retain_live_page: bool = False,
) -> InstagramProfileFetchResult:
    html, status = _request_instagram_profile_html(session, url)
    if _instagram_profile_requests_html_usable(status, html):
        if not retain_live_page:
            return InstagramProfileFetchResult(html=html, status=status)
        soup = BeautifulSoup(html, "html.parser")
        if _extract_instagram_profile_candidate_emails(html, soup=soup):
            return InstagramProfileFetchResult(html=html, status=status)
        live_page = _open_instagram_live_page_bridge(url, timeout_s=HTTP_TIMEOUT)
        if live_page is not None:
            live_html = live_page.snapshot_html()
            if _instagram_profile_fetch_usable(200, live_html):
                return InstagramProfileFetchResult(
                    html=live_html,
                    status=200,
                    live_page=live_page,
                )
            live_page.close()
        return InstagramProfileFetchResult(html=html, status=status)

    try:
        if retain_live_page:
            live_page = _open_instagram_live_page_bridge(url, timeout_s=HTTP_TIMEOUT)
            if live_page is not None:
                live_html = live_page.snapshot_html()
                if _instagram_profile_fetch_usable(200, live_html):
                    return InstagramProfileFetchResult(
                        html=live_html,
                        status=200,
                        live_page=live_page,
                    )
                live_page.close()
        fallback = fetch_html(
            url,
            session=session,
            directory="instagram",
            required_selectors=[_INSTAGRAM_REQUIRED_SELECTOR],
            allow_browser_fallback=True,
            browser_ready_wait=_wait_for_instagram_profile_render,
            timeout_s=HTTP_TIMEOUT,
        )
    except Exception:
        return InstagramProfileFetchResult(html=html, status=status)
    fallback_html = str(fallback.get("html") or "")
    fallback_status = fallback.get("status")
    if fallback_status == 200 and fallback_html:
        return InstagramProfileFetchResult(html=fallback_html, status=fallback_status)
    return InstagramProfileFetchResult(html=html, status=status)


@contextmanager
def _instagram_profile_fetch_scope(
    session: requests.Session,
    url: str,
    *,
    retain_live_page: bool = False,
):
    if retain_live_page:
        result = _fetch_instagram_profile_result(
            session,
            url,
            retain_live_page=True,
        )
    else:
        html, status = _fetch_instagram_profile_html(session, url)
        result = InstagramProfileFetchResult(html=html, status=status)
    try:
        yield result
    finally:
        if result.live_page is not None:
            result.live_page.close()


def _fetch_instagram_profile_html(session: requests.Session, url: str) -> Tuple[str, Optional[int]]:
    """Fetch a single Instagram profile page with one bounded shared fallback."""
    result = _fetch_instagram_profile_result(session, url, retain_live_page=False)
    return (result.html, result.status)


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


def _extract_fb_emails_bounded(fb_driver, fb_url: str, log_fn=None, fb_session=None, _stalltrace_row_label: str = "") -> tuple[list[str], str, str]:
    """
    Visit at most two Facebook pages (main + about/info/contact) to extract emails.
    Returns (emails, resolved_url, status_reason).
    """
    import time as _time_mod
    _st_bounded_t0 = _time_mod.perf_counter()
    _st_row = _stalltrace_row_label

    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    if not fb_driver or not fb_url:
        return ([], fb_url or "", "no_fb_url")

    class _FBEnrichFastPathSessionAdapter:
        def __init__(self, session) -> None:
            self._session = session

        def __getattr__(self, name: str):
            return getattr(self._session, name)

        def navigate(
            self,
            url: str,
            logger=None,
            unblock_on_ready: bool = False,
            validate_session: bool = True,
        ):
            try:
                return self._session.navigate(
                    url,
                    logger=logger,
                    unblock_on_ready=unblock_on_ready,
                    validate_session=validate_session,
                )
            except TypeError as exc:
                message = cell_to_str(exc)
                if "validate_session" not in message and "unblock_on_ready" not in message:
                    raise
            try:
                return self._session.navigate(
                    url,
                    logger=logger,
                    unblock_on_ready=unblock_on_ready,
                )
            except TypeError as exc:
                if "unblock_on_ready" not in cell_to_str(exc):
                    raise
            try:
                return self._session.navigate(url, logger=logger)
            except TypeError:
                return self._session.navigate(url)

    class _FBEnrichAcceptedPageFastPathBridge:
        _clear_last_fb_email_surface_state = NightModeFacebookEnricher._clear_last_fb_email_surface_state
        _apply_trust_budget_health = NightModeFacebookEnricher._apply_trust_budget_health
        _log_page_health = NightModeFacebookEnricher._log_page_health
        _refresh_driver = NightModeFacebookEnricher._refresh_driver
        _fetch_html_with_url = NightModeFacebookEnricher._fetch_html_with_url

        def __init__(self, session, logger) -> None:
            self.session = _FBEnrichFastPathSessionAdapter(session)
            self.logger = logger
            self.legacy = None
            self._run_state = None
            self._page_budget_remaining = 2
            self.fb_email_pages_visited = 0
            self._session_failed = False
            self._session_failed_reason = ""
            self._skip_fb_due_to_warning = False
            self._skip_fb_due_to_warning_reason = ""
            self._last_fb_timeout = False
            self._last_fb_timeout_url = ""
            self._last_fb_visible_text = ""
            self._last_fb_live_anchor_values = []
            self._last_fb_reveal_actions = []
            self._last_fb_surface_html = None
            self._last_fb_surface_url = ""
            self._last_fb_surface_driver_kind = ""
            self._last_fb_render_invalid_reason = ""
            self._last_fb_surface_html_available = False
            self._last_fb_visible_text_available = False
            self._last_fb_anchor_values_available = False
            self._last_fb_reveal_actions_available = False

        def _ensure_session(self, prewarm_session: bool = True):
            return self.session

        def _ensure_driver_alive(self, session):
            return session

        def _current_trust_score(self) -> int:
            return 0

        def _set_trust_score(self, score: int) -> int:
            return score

    fast_path_fetcher = None
    if fb_session is not None and hasattr(fb_session, "navigate"):
        fast_path_fetcher = _FBEnrichAcceptedPageFastPathBridge(fb_session, log_fn)

    def _fetch_surface(target: str) -> FacebookAcceptedPageFetchResult:
        from night_mode_fb import _stalltrace
        _st_fetch_t = _time_mod.perf_counter()
        target_fetch = _normalise_fb_surface_url(target) or _normalise_fb_url(normalize_external_url(target))
        if not target_fetch:
            return FacebookAcceptedPageFetchResult(
                requested_url=target or "",
                resolved_url=fb_url or "",
                status_reason="fetch_error",
            )
        try:
            active_driver = fb_driver
            nav_current_url = ""
            nav_html = ""
            anchor_values: List[str] = []
            rendered_text = ""
            if fast_path_fetcher is not None:
                _st_fp_t = _time_mod.perf_counter()
                try:
                    nav_html, nav_current_url = fast_path_fetcher._fetch_html_with_url(
                        target_fetch,
                        goto_about=False,
                        collect_surfaces=True,
                        skip_pre_nav_session_validation=True,
                    )
                    if log_fn:
                        _stalltrace(log_fn, _st_row, f"fetch_surface:fast_path:done url={target_fetch!r}", _st_fp_t, _st_bounded_t0)
                except FacebookDriverError as exc:
                    if _fb_exception_is_fatal_session(exc):
                        raise
                    reason = ""
                    exc_message = cell_to_str(exc)
                    if exc_message.startswith("fb_circuit_breaker:"):
                        reason = exc_message.split("fb_circuit_breaker:", 1)[-1] or "warning_interstitial"
                    if reason:
                        resolved_url = (
                            _normalise_fb_surface_url(fast_path_fetcher._last_fb_surface_url)
                            or _normalise_fb_url(
                                normalize_external_url(fast_path_fetcher._last_fb_surface_url) or fast_path_fetcher._last_fb_surface_url
                            )
                            or target_fetch
                        )
                        _log(f"[FB Enrich] Warning/block page detected ({reason}); skipping row.")
                        return FacebookAcceptedPageFetchResult(
                            requested_url=target_fetch,
                            resolved_url=resolved_url,
                            status_reason=reason,
                        )
                    _log(f"[FB Enrich] Error fetching FB page '{target_fetch}': {exc}")
                    return FacebookAcceptedPageFetchResult(
                        requested_url=target_fetch,
                        resolved_url=target_fetch,
                        status_reason="fetch_error",
                    )
                active_driver = getattr(fb_session, "driver", None) or fb_driver
                nav_current_url = str(nav_current_url or fast_path_fetcher._last_fb_surface_url or "").strip()
                nav_html = nav_html or (fast_path_fetcher._last_fb_surface_html or "")
                rendered_text = fast_path_fetcher._last_fb_visible_text or ""
                anchor_values = list(fast_path_fetcher._last_fb_live_anchor_values or [])
            elif fb_session is not None and hasattr(fb_session, "navigate"):
                try:
                    active_driver = fb_session.navigate(target_fetch, logger=log_fn)
                except TypeError:
                    active_driver = fb_session.navigate(target_fetch)
                active_driver = active_driver or getattr(fb_session, "driver", None) or fb_driver
                nav_current_url = str(getattr(fb_session, "last_nav_current_url", "") or "").strip()
                nav_html = getattr(fb_session, "last_nav_page_source", "") or ""
            else:
                fb_driver.get(target_fetch)
            current_url = nav_current_url or getattr(active_driver, "current_url", "") or target_fetch
            resolved_url = _normalise_fb_surface_url(current_url) or _normalise_fb_url(normalize_external_url(current_url) or current_url) or current_url
            if _is_fb_login_or_security_url(current_url):
                _log("[FB Enrich] Facebook login/checkpoint detected; skipping.")
                return FacebookAcceptedPageFetchResult(
                    requested_url=target_fetch,
                    resolved_url=resolved_url,
                    status_reason="login_wall",
                )
            html = nav_html or getattr(active_driver, "page_source", "") or ""
            warning = _looks_like_fb_warning_or_block(html, current_url)
            if warning:
                _log(f"[FB Enrich] Warning/block page detected ({warning}); skipping row.")
                return FacebookAcceptedPageFetchResult(
                    requested_url=target_fetch,
                    resolved_url=resolved_url,
                    status_reason=warning,
                )
            if not rendered_text:
                _st_vis_t = _time_mod.perf_counter()
                rendered_text = _extract_fb_visible_text_with_container_fallback(active_driver)
                if log_fn:
                    _stalltrace(log_fn, _st_row, "fetch_surface:visible_text_fallback:done", _st_vis_t, _st_bounded_t0)
            if log_fn:
                _stalltrace(log_fn, _st_row, f"fetch_surface:complete url={target_fetch!r}", _st_fetch_t, _st_bounded_t0)
            _log_fb_email_surface_debug(log_fn, f"page:{resolved_url}", html, rendered_text)
            return FacebookAcceptedPageFetchResult(
                requested_url=target_fetch,
                resolved_url=resolved_url,
                html=html,
                rendered_text=rendered_text,
                anchor_values=anchor_values,
            )
        except Exception as exc:  # pragma: no cover - defensive
            if _fb_exception_is_fatal_session(exc):
                raise
            _log(f"[FB Enrich] Error fetching FB page '{target_fetch}': {exc}")
            return FacebookAcceptedPageFetchResult(
                requested_url=target_fetch,
                resolved_url=target_fetch,
                status_reason="fetch_error",
            )

    main_target = _normalise_fb_surface_url(fb_url) or _normalise_fb_url(normalize_external_url(fb_url))
    if main_target:
        _log(f"[FB Enrich] Visiting {main_target}")

    from night_mode_fb import _stalltrace
    if log_fn:
        _stalltrace(log_fn, _st_row, "bounded:entry", _st_bounded_t0, _st_bounded_t0)

    def _fallback_about_urls(base_url: str) -> List[str]:
        parsed = urllib.parse.urlparse(base_url or fb_url)
        base_path = (parsed.path or "").rstrip("/") or "/"
        fallback_url = urllib.parse.urlunparse(
            parsed._replace(path=base_path + "/about", query="", fragment="")
        )
        return [fallback_url]

    sweep_result = _run_bounded_fb_accepted_page_sweep(
        fb_url,
        _fetch_surface,
        select_secondary_url=_select_fb_contact_surface_url,
        fallback_secondary_urls=_fallback_about_urls,
        on_secondary_selected=lambda target: _log(f"[FB Enrich] Visiting contact/about page: {target}"),
        on_secondary_fallback=lambda target: _log(f"[FB Enrich] Visiting contact/about page: {target}"),
        on_no_secondary=lambda: _log("[FB Enrich] No contact/about link found"),
        _stalltrace_log_fn=log_fn,
        _stalltrace_row_label=_st_row,
    )

    if log_fn:
        _stalltrace(log_fn, _st_row, "bounded:return", _st_bounded_t0, _st_bounded_t0)

    status_reason = sweep_result.secondary_status_reason or sweep_result.status_reason or ""
    resolved = sweep_result.final_resolved_url or fb_url
    return (list(sweep_result.combined_emails or []), resolved, status_reason)


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


_FB_DAYTIME_SEARCH_RESULT_SURFACE_SELECTORS: Tuple[str, ...] = (
    'div[role="main"] div[aria-label="Search results"]',
    'div[aria-label="Search results"]',
    'div[role="main"] section[aria-label*="Search results"]',
    'div[role="main"] [data-pagelet^="SearchResults"]',
    'div[role="main"] div[aria-label*="Search results"]',
)


def _extract_daytime_fb_search_results_surface_html(page_html: str) -> Tuple[str, str]:
    """
    Restrict daytime bounded discovery harvesting to explicit search-results surfaces.
    Generic feed/main/article containers remain available to shared/night helpers, but
    daytime discovery only passes through repo-grounded search-result indicators.
    """
    html = cell_to_str(page_html)
    if not html:
        return "", ""

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return "", ""

    for selector in _FB_DAYTIME_SEARCH_RESULT_SURFACE_SELECTORS:
        try:
            containers = soup.select(selector)
        except Exception:
            containers = []
        for container in containers:
            try:
                container_html = str(container)
            except Exception:
                container_html = ""
            if not container_html.strip():
                continue
            # Hard-lock daytime bounded discovery to the verified search-results
            # subtree so downstream DOM gating cannot re-select generic feed/main
            # containers after NM-S162 has already proven the surface.
            return (
                f'<div aria-label="Search results" data-fb-daytime-lock="1">{container_html}</div>',
                selector,
            )

    return "", ""


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
            if _fb_exception_is_fatal_session(exc):
                raise
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
                if _fb_exception_is_fatal_session(exc):
                    raise
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
                if _fb_exception_is_fatal_session(exc):
                    raise
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

    def _fetch_search_surface(
        self,
        query: str,
        *,
        search_method: str,
    ) -> Tuple[str, str, bool]:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import TimeoutException, WebDriverException
        except Exception as exc:
            _safe_log(self.logger, "[FB Enrich] Selenium imports unavailable: %s", exc)
            return "", "", False

        if search_method == "direct_route":
            search_url = f"{FACEBOOK_SEARCH_URL}?q={urllib.parse.quote_plus(query)}"
            _safe_log(self.logger, "[FB Enrich] search_method=direct_route query='%s' url=%s", query, search_url)
            try:
                self.driver.get(search_url)
            except WebDriverException as exc:
                if _fb_exception_is_fatal_session(exc):
                    raise
                _safe_log(
                    self.logger,
                    "[FB Enrich] WebDriver error while navigating FB for '%s': %s",
                    query,
                    exc,
                )
                return "", search_url, False
            timed_out = False
            try:
                WebDriverWait(self.driver, FACEBOOK_SEARCH_WAIT_SECONDS).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='facebook.com'], div[role='main']"))
                )
            except TimeoutException:
                timed_out = True
            try:
                page_html = self.driver.page_source or ""
            except Exception:
                page_html = ""
            try:
                current_url = self.driver.current_url or search_url
            except Exception:
                current_url = search_url
            return page_html, current_url, timed_out

        if search_method == "homepage_ui":
            _safe_log(self.logger, "[FB Enrich] search_method=homepage_ui query='%s'", query)
            return _run_fb_homepage_search(
                self.driver,
                query,
                logger=self.logger,
                log_prefix="[FB Enrich]",
            )

        raise ValueError(f"Unsupported search_method: {search_method}")

    def find_best_page_url(
        self,
        artist_name: str,
        extra_signal: Optional[str] = None,
        *,
        require_strong_candidate: bool = False,
        defer_identity_floor_to_postscrape: bool = False,
        skip_login_check: bool = False,
    ) -> Optional[str]:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import TimeoutException, WebDriverException
        except Exception as exc:
            _safe_log(self.logger, "[FB Enrich] Selenium imports unavailable: %s", exc)
            return None
        if not skip_login_check and not self.ensure_facebook_logged_in():
            _safe_log(self.logger, "[FB Enrich] Facebook login not available, skipping.")
            return None
        query_parts = [cell_to_str(artist_name)]
        if extra_signal:
            query_parts.append(cell_to_str(extra_signal))
        query = " ".join(part for part in query_parts if part)
        if not query:
            return None
        page_html, current_url, search_timed_out = self._fetch_search_surface(query, search_method="homepage_ui")
        search_surface_html, search_surface_selector = _extract_daytime_fb_search_results_surface_html(page_html)
        if search_surface_selector:
            _safe_log(
                self.logger,
                "[FB Enrich] search_method=homepage_ui search_surface_selector=%s query='%s'",
                search_surface_selector,
                query,
            )
        elif page_html:
            _safe_log(
                self.logger,
                "[FB Enrich] search_method=homepage_ui search_surface_selector=NONE query='%s'",
                query,
            )
        extraction_html = search_surface_html if search_surface_selector and search_surface_html else page_html
        candidates = _fb_extract_candidates_from_search_dom(
            extraction_html,
            logger=self.logger,
            debug=os.getenv("FB_DEBUG_DOM_GATE") == "1",
            search_name=artist_name,
        )
        candidates, homepage_failure_mode = _guard_homepage_fb_search_candidates(
            candidates,
            page_html=page_html,
            current_url=current_url,
            logger=self.logger,
            log_prefix="[FB Enrich]",
            query=query,
        )
        if homepage_failure_mode:
            _safe_log(
                self.logger,
                "[FB Enrich] search_method=homepage_ui failure_mode=%s query='%s'",
                homepage_failure_mode,
                query,
            )
        elif not candidates:
            homepage_miss_reason = _fb_search_surface_miss_reason(
                page_html,
                driver=self.driver,
                current_url=current_url,
                timed_out=search_timed_out,
            )
            if homepage_miss_reason:
                _safe_log(
                    self.logger,
                    "[FB Enrich] search_method=homepage_ui failure_mode=%s query='%s'",
                    homepage_miss_reason,
                    query,
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
                fallback_candidates.append((final_score, name_score, cat_boost, True, False, cand))
            else:
                generic_candidates.append((final_score, name_score, cat_boost, True, False, cand))

        def _bucket_selection_key(item: Tuple[float, float, float, bool, bool, FbCandidate]) -> Tuple[float, int]:
            score, _, _, _, _, cand = item
            try:
                path = (urllib.parse.urlparse(cand.url or "").path or "").strip("/").lower()
            except Exception:
                path = ""
            if not path or path == "profile.php":
                return (score, 0)
            if path.startswith("pages/") or "/" not in path:
                return (score, 1)
            return (score, 0)

        MIN_FINAL_SCORE = 1.0
        MAX_PRE_SCRAPE_RANKED_CANDIDATES = 2

        ranked_entries: List[Tuple[str, Tuple[float, float, float, bool, bool, FbCandidate]]] = []
        for bucket_name, bucket in (
            ("strong", strong_music_candidates),
            ("fallback", fallback_candidates),
            ("generic", generic_candidates),
        ):
            for entry in sorted(bucket, key=_bucket_selection_key, reverse=True):
                if entry[0] < MIN_FINAL_SCORE or not entry[3]:
                    continue
                ranked_entries.append((bucket_name, entry))

        if not ranked_entries:
            _safe_log(self.logger, "[FB Enrich] No high-confidence Facebook match for '%s'.", artist_name)
            return None

        for ranked_index, (bucket_name, best_entry) in enumerate(
            ranked_entries[:MAX_PRE_SCRAPE_RANKED_CANDIDATES],
            start=1,
        ):
            best_score, best_name_score, best_cat_boost, best_is_music, best_is_corp, best_candidate = best_entry
            deferred_identity_floor = False

            if require_strong_candidate:
                has_identity_evidence, identity_reason = _facebook_candidate_has_min_identity_evidence(
                    artist_name,
                    best_candidate,
                    name_score=best_name_score,
                )
                if not has_identity_evidence:
                    if defer_identity_floor_to_postscrape and identity_reason == "identity_floor":
                        deferred_identity_floor = True
                        _safe_log(
                            self.logger,
                            "[FB Discover] Deferring candidate for '%s' to post-scrape validation after weak identity evidence: %s",
                            artist_name,
                            identity_reason,
                        )
                    else:
                        _safe_log(
                            self.logger,
                            "[FB Discover] Rejected candidate for '%s' before scrape - weak identity evidence: %s",
                            artist_name,
                            identity_reason,
                        )
                        if ranked_index < min(len(ranked_entries), MAX_PRE_SCRAPE_RANKED_CANDIDATES):
                            _safe_log(
                                self.logger,
                                "[FB Discover] Considering next plausible ranked candidate for '%s'.",
                                artist_name,
                            )
                        continue

            if best_name_score == 0.0 and best_cat_boost == 0.0:
                best_name_lc = (best_candidate.name or "").lower()
                best_url_lc = (best_candidate.url or "").lower()
                best_category_lc = (best_candidate.category or "").lower()
                zero_signal_music_flag = any(tok in best_category_lc for tok in strong_cat_tokens) or is_music_page(
                    best_name_lc,
                    best_url_lc,
                    best_category_lc,
                )
                if not zero_signal_music_flag:
                    best_candidate_name_norm = normalize_fb_name(best_candidate.name or "")
                    best_artist_norm = normalize_fb_name(artist_name)
                    try:
                        best_candidate_username_norm = normalize_fb_name(
                            urllib.parse.urlparse(best_candidate.url or "").path.strip("/").split("/")[0]
                        )
                    except Exception:
                        best_candidate_username_norm = ""
                    zero_signal_music_flag = (
                        best_candidate_name_norm
                        and best_artist_norm
                        and best_candidate_name_norm == best_artist_norm
                    ) or (
                        best_candidate_username_norm
                        and best_artist_norm
                        and best_candidate_username_norm == best_artist_norm
                    )
                if not zero_signal_music_flag:
                    _safe_log(
                        self.logger,
                        "[FB Discover] Skipping zero-signal candidate for '%s' before scrape: %s",
                        artist_name,
                        best_candidate.url,
                    )
                    if ranked_index < min(len(ranked_entries), MAX_PRE_SCRAPE_RANKED_CANDIDATES):
                        _safe_log(
                            self.logger,
                            "[FB Discover] Considering next plausible ranked candidate for '%s'.",
                            artist_name,
                        )
                    continue

            if bucket_name == "fallback":
                _safe_log(
                    self.logger,
                    "[FB Enrich] Trying uncertain music FB candidate '%s' for '%s' (category='%s', base_score=%.2f).",
                    best_candidate.name or best_candidate.url,
                    artist_name,
                    best_candidate.category or "<none>",
                    best_name_score,
                )
            elif bucket_name == "generic":
                _safe_log(
                    self.logger,
                    "[FB Enrich] Trying very loose FB candidate '%s' for '%s' (category='%s', base_score=%.2f).",
                    best_candidate.name or best_candidate.url,
                    artist_name,
                    best_candidate.category or "<none>",
                    best_name_score,
                )

            # Second-layer validation: fetch page category and reject late if corporate or not music.
            page_music = False
            confirmed_logged = False
            page_html = ""
            page_category_text = None
            page_text_blocks: List[str] = []
            outbound_links: List[str] = []
            try:
                self.driver.get(best_candidate.url)
                WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                page_html = self.driver.page_source or ""
                raw_html_lc = (page_html or "").lower()
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
                if _fb_exception_is_fatal_session(exc):
                    raise
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
                    allow_identity_floor_page_signal_override=deferred_identity_floor,
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

        _safe_log(self.logger, "[FB Enrich] No high-confidence Facebook match for '%s'.", artist_name)
        return None


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


def _facebook_candidate_has_min_identity_evidence(
    artist_name: str,
    candidate: FbCandidate,
    *,
    name_score: Optional[float] = None,
) -> Tuple[bool, str]:
    artist_norm = normalize_fb_name(artist_name)
    if not artist_norm:
        return False, "identity_floor"
    artist_compact = artist_norm.replace(" ", "")

    if name_score is not None:
        try:
            if float(name_score) > 0.0:
                return True, "name_score"
        except Exception:
            pass

    candidate_name_norm = normalize_fb_name(cell_to_str(getattr(candidate, "name", "")))
    candidate_url = cell_to_str(getattr(candidate, "url", ""))
    try:
        slug = urllib.parse.urlparse(candidate_url).path.strip("/").split("/")[0]
    except Exception:
        slug = ""
    username_norm = normalize_fb_name(slug)
    candidate_name_compact = candidate_name_norm.replace(" ", "")
    username_compact = username_norm.replace(" ", "")

    if candidate_name_norm == artist_norm or username_norm == artist_norm:
        return True, "exact_norm"
    if artist_compact and (candidate_name_compact == artist_compact or username_compact == artist_compact):
        return True, "compact_norm"
    if candidate_name_norm.startswith(artist_norm) or username_norm.startswith(artist_norm):
        return True, "prefix_norm"
    compact_spacing_variant = (
        (candidate_name_norm and candidate_name_compact != candidate_name_norm)
        or (username_norm and username_compact != username_norm)
        or (artist_norm and artist_compact != artist_norm)
    )
    if compact_spacing_variant and len(artist_compact) >= 5:
        if candidate_name_compact.startswith(artist_compact) or username_compact.startswith(artist_compact):
            return True, "compact_prefix_norm"
    if candidate_name_norm and artist_norm in candidate_name_norm:
        return True, "name_contains_artist"
    if compact_spacing_variant and len(artist_compact) >= 5 and candidate_name_compact and artist_compact in candidate_name_compact:
        return True, "compact_name_contains_artist"

    artist_tokens = [token for token in artist_norm.split() if len(token) >= 4]
    for token in artist_tokens:
        if token in candidate_name_norm or token in username_norm:
            return True, "token_overlap"

    return False, "identity_floor"


def _facebook_candidate_is_strong(
    artist_name: str,
    candidate: FbCandidate,
    page_html: str,
    page_category_text,
    page_text_blocks,
    outbound_links,
    allow_identity_floor_page_signal_override: bool = False,
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

    scored = score_fb_candidate(
        artist_name,
        candidate_name,
        candidate_url,
        cell_to_str(getattr(candidate, "category", "")),
    )
    zero_name_score_identity_floor = False
    if scored is not None:
        _, name_score, _ = scored
        if name_score <= 0.0:
            if not allow_identity_floor_page_signal_override:
                return False, "identity_floor"
            zero_name_score_identity_floor = True

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
    if zero_name_score_identity_floor:
        return False, "identity_floor"

    return False, "slug_or_name_only_match"


def facebook_find_best_page(
    artist_name: str,
    extra_signal: str,
    fb_client,
    logger,
    *,
    require_strong_candidate: bool = False,
    defer_identity_floor_to_postscrape: bool = False,
    skip_login_check: bool = False,
) -> Optional[str]:
    artist_name = cell_to_str(artist_name)
    extra_signal = cell_to_str(extra_signal)
    if not fb_client or not hasattr(fb_client, "find_best_page_url"):
        _safe_log(logger, "[FB Enrich] No Facebook search client available; skipping '%s'.", artist_name)
        return None
    try:
        return fb_client.find_best_page_url(
            artist_name,
            extra_signal,
            require_strong_candidate=require_strong_candidate,
            defer_identity_floor_to_postscrape=defer_identity_floor_to_postscrape,
            skip_login_check=skip_login_check,
        )
    except Exception as exc:
        if _fb_exception_is_fatal_session(exc):
            raise
        _safe_log(logger, "[FB Enrich] Facebook search client error for '%s': %s", artist_name, exc)
        return None


def _discover_facebook_url_bounded(fb_driver, artist_name: str, extra_signal: str, logger) -> str:
    """Attempt one bounded daytime Facebook discovery using the existing driver."""
    if not fb_driver:
        return ""
    artist_name = cell_to_str(artist_name)
    extra_signal = cell_to_str(extra_signal)
    if not artist_name:
        return ""
    try:
        fb_client = FacebookSearchClient(driver=fb_driver, logger=logger)
    except Exception:
        return ""
    fb_url = facebook_find_best_page(
        artist_name,
        extra_signal,
        fb_client,
        logger,
        require_strong_candidate=True,
        skip_login_check=True,
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
    return _shared_normalize_identity_url(value)


def _canonicalise_bandcamp_url(value: str) -> str:
    return _shared_canonicalize_bandcamp_url(value)


def _canonicalise_musicbrainz_bandcamp_url(value: str) -> str:
    canonical = _canonicalise_bandcamp_url(value or "")
    if not canonical:
        return ""
    try:
        parsed = urllib.parse.urlparse(canonical)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"https://{host}/" if host.endswith(".bandcamp.com") else ""


def _bandcamp_challenge_reason(html: str) -> str:
    """Classify only deterministic Bandcamp challenge surfaces as unavailable."""
    if not html:
        return ""
    if _detect_soft_block(html):
        return "recognized_soft_block"
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")
        title_text = _clean_cell(title.get_text(" ", strip=True)) if title else ""
    except Exception:
        title_text = ""
    if normalize_name(title_text) == "client challenge":
        return "client_challenge_title"
    return ""


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
    return any(host.endswith(domain) for domains in PLATFORM_HOSTS.values() for domain in domains) or any(
        host.endswith(domain) for domain in WEBSITE_ENRICH_PLATFORM_HOSTS
    )


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


def _collect_website_enrich_link_hubs(row: Any) -> List[str]:
    if row is None:
        return []

    hub_urls: List[str] = []
    seen_urls: Set[str] = set()

    def _add_value(value: Any) -> None:
        for token in _split_multi_value(value):
            normalised = _normalise_url(token)
            if not normalised or normalised in seen_urls:
                continue
            if _host(normalised) not in LINK_HUB_HOSTS:
                continue
            seen_urls.add(normalised)
            hub_urls.append(normalised)

    _add_value(row.get("Spotify_Website_URL", ""))
    _add_value(row.get("External Links", ""))

    for field in WEBSITE_EMAIL_OPTIONAL_FIELDS:
        if hasattr(row, "__contains__") and field not in row:
            continue
        _add_value(row.get(field, ""))

    return hub_urls


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
    emails = set(filter_platform_support_emails(list(emails)))
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
    emails = set(filter_platform_support_emails(list(emails)))
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
    return _shared_soundcloud_handle_from_profile_url(url)


# ---------------------------------------------------------------------------
# Hardened cross-directory artist identity validation (Ticket 4)
# ---------------------------------------------------------------------------

_IDENTITY_EXACT_THRESHOLD = 0.95
_IDENTITY_STRONG_THRESHOLD = 0.80
_IDENTITY_PLAUSIBLE_THRESHOLD = 0.60

_IDENTITY_MANAGEMENT_TOKENS = {
    "mgmt",
    "management",
    "manager",
    "managers",
    "label",
    "labels",
    "records",
    "recordings",
    "recording",
    "booking",
    "bookings",
    "agency",
    "agencies",
    "promotions",
    "promo",
    "press",
    "publicity",
    "tour",
    "touring",
    "events",
}
_IDENTITY_CORPORATE_TOKENS = {
    "inc",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "group",
    "collective",
    "enterprise",
    "company",
    "co",
}
_IDENTITY_HARMLESS_SUFFIXES = {
    "official",
    "music",
    "band",
    "artist",
    "project",
    "sounds",
    "audio",
    "beats",
    "productions",
    "prod",
    "live",
    "dj",
    "mc",
}


def _identity_compact(name: str) -> str:
    """Remove all non-alphanumeric characters and lowercase."""
    return re.sub(r"[^a-z0-9]", "", normalize_name(name))


def _tokens_in_order(needles: List[str], haystack: List[str]) -> bool:
    """Return True if all needles appear in haystack in the same order."""
    if not needles:
        return True
    if not haystack:
        return False
    idx = 0
    for token in haystack:
        if idx < len(needles) and token == needles[idx]:
            idx += 1
    return idx == len(needles)


def _identity_name_tier(seed_name: str, candidate_name: str) -> Tuple[str, float]:
    """
    Classify display-name match strength.
    Returns (tier, base_score) where tier is one of exact/strong/plausible/weak.
    """
    if not seed_name or not candidate_name:
        return ("weak", 0.0)

    seed_norm = normalize_name(seed_name)
    cand_norm = normalize_name(candidate_name)
    if not seed_norm or not cand_norm:
        return ("weak", 0.0)

    seed_compact = _identity_compact(seed_name)
    cand_compact = _identity_compact(candidate_name)

    # Exact
    if seed_norm == cand_norm:
        # Reject false exact matches caused by aggressive punctuation normalization
        # (e.g. "Artist 1" vs "artist.1" where the dot is replaced with a space).
        # Dots/underscores are structural in usernames and must not create exact identity.
        seed_special = any(c in seed_name for c in "._")
        cand_special = any(c in candidate_name for c in "._")
        if seed_special == cand_special:
            return ("exact", 0.98)
        return ("strong", 0.88)
    if seed_compact and cand_compact and seed_compact == cand_compact:
        return ("exact", 0.97)

    # Strong: fuzz ratio >= 95, or compact variant with harmless suffix
    fuzz_score = fuzz.ratio(seed_norm, cand_norm) if seed_norm and cand_norm else 0
    if fuzz_score >= 95:
        return ("strong", 0.90)

    if seed_compact and cand_compact:
        longer, shorter = (
            (cand_compact, seed_compact)
            if len(cand_compact) > len(seed_compact)
            else (seed_compact, cand_compact)
        )
        if len(shorter) >= 4 and longer.startswith(shorter):
            extra = longer[len(shorter) :]
            extra_tokens = [
                t for t in re.findall(r"[a-z]+", extra) if t and t not in _IDENTITY_HARMLESS_SUFFIXES
            ]
            if not extra_tokens:
                return ("strong", 0.88)

    # Plausible: all seed tokens in order, or fuzz >= 80, or compact substring
    seed_tokens = seed_norm.split()
    cand_tokens = cand_norm.split()
    if _tokens_in_order(seed_tokens, cand_tokens):
        return ("plausible", 0.70)

    if fuzz_score >= 80:
        return ("plausible", 0.65)

    if seed_compact and cand_compact and len(seed_compact) >= 4 and seed_compact in cand_compact:
        return ("plausible", 0.60)

    # Weak: partial token overlap or fuzz >= 60
    shared_tokens = set(seed_tokens) & set(cand_tokens)
    if shared_tokens and len(shared_tokens) >= max(1, len(seed_tokens) // 2):
        return ("weak", 0.35)

    if fuzz_score >= 60:
        return ("weak", 0.30)

    return ("weak", 0.10)


def _identity_handle_tier(seed_name: str, handle: str) -> Tuple[str, float]:
    """
    Classify handle/username match strength.
    Returns (tier, base_score).
    """
    if not seed_name or not handle:
        return ("weak", 0.0)

    seed_norm = normalize_name(seed_name)
    handle_norm = _sc_normalise_text(handle)
    if not seed_norm or not handle_norm:
        return ("weak", 0.0)

    seed_compact = _identity_compact(seed_name)
    handle_compact = _identity_compact(handle)
    seed_tokens = seed_norm.split()

    # Exact
    if seed_norm == handle_norm:
        return ("exact", 0.95)
    if seed_compact and handle_compact and seed_compact == handle_compact:
        return ("exact", 0.94)

    # Strong: handle is compact version or known separator variant
    if handle_compact and seed_compact and handle_compact == seed_compact:
        return ("strong", 0.88)

    # Plausible: handle contains full compact seed
    if len(seed_compact) >= 4 and seed_compact in handle_compact:
        extra = handle_compact.replace(seed_compact, "", 1)
        extra_tokens = [
            t for t in re.findall(r"[a-z]+", extra) if t and t not in _IDENTITY_HARMLESS_SUFFIXES
        ]
        if not extra_tokens:
            return ("plausible", 0.65)
        if not any(t in _IDENTITY_MANAGEMENT_TOKENS or t in _IDENTITY_CORPORATE_TOKENS for t in extra_tokens):
            return ("plausible", 0.55)

    # Weak: shares first token
    seed_first = seed_tokens[0] if seed_tokens else ""
    if seed_first and len(seed_first) >= 3 and handle_norm.startswith(seed_first):
        return ("weak", 0.30)

    # Very weak: any shared token
    shared = set(seed_tokens) & set(handle_norm.split())
    if shared and len(shared) >= max(1, len(seed_tokens) // 2):
        return ("weak", 0.20)

    return ("weak", 0.05)


def _identity_contradiction_penalty(
    seed_name: str, candidate_name: str, handle: str, candidate_context: str = ""
) -> float:
    """Return penalty for identity contradictions (management accounts, etc.)."""
    penalty = 0.0
    texts = [candidate_name, handle, candidate_context]
    combined = " ".join(t for t in texts if t).lower()
    tokens = set(re.findall(r"[a-z]+", combined))
    compact_combined = _identity_compact(combined)

    seed_norm = normalize_name(seed_name)
    cand_norm = normalize_name(candidate_name)
    is_exact = bool(seed_norm and cand_norm and seed_norm == cand_norm)

    # Word-boundary hits
    mgmt_hits = tokens & _IDENTITY_MANAGEMENT_TOKENS
    corp_hits = tokens & _IDENTITY_CORPORATE_TOKENS

    # Compact-form hits (e.g. blackorangemgmt)
    if not mgmt_hits:
        mgmt_hits = {t for t in _IDENTITY_MANAGEMENT_TOKENS if t in compact_combined}
    if not corp_hits:
        corp_hits = {t for t in _IDENTITY_CORPORATE_TOKENS if t in compact_combined}

    if mgmt_hits and not is_exact:
        penalty += 0.40
    elif mgmt_hits and is_exact:
        penalty += 0.15

    if corp_hits and not is_exact:
        penalty += 0.20
    elif corp_hits and is_exact:
        penalty += 0.05

    # Digits mismatch
    seed_compact = _identity_compact(seed_name)
    if seed_compact and not any(ch.isdigit() for ch in seed_compact):
        if any(ch.isdigit() for ch in _identity_compact(handle)):
            penalty += 0.15

    # Generic-only handle
    handle_norm = _sc_normalise_text(handle)
    if handle_norm and handle_norm in _SC_GENERIC_TOKENS:
        penalty += 0.25

    return min(penalty, 0.80)


def _identity_corroboration_boost(
    seed_location: str = "",
    seed_genre: str = "",
    seed_website: str = "",
    candidate_location: str = "",
    candidate_context: str = "",
    candidate_websites: Optional[Set[str]] = None,
) -> float:
    """Return boost for corroborating identity evidence."""
    boost = 0.0
    candidate_websites = candidate_websites or set()

    # Website/domain match
    if seed_website and candidate_websites:
        seed_domain = extract_domain(seed_website)
        if seed_domain and seed_domain not in GENERIC_SOCIAL_ROOT_HOSTS:
            for cand_url in candidate_websites:
                cand_domain = extract_domain(cand_url)
                if cand_domain and (cand_domain == seed_domain or cand_domain.endswith("." + seed_domain)):
                    boost += 0.15
                    break

    # Location match
    if seed_location and candidate_location:
        if _sc_location_match(seed_location, candidate_location):
            boost += 0.08

    # Genre match
    if seed_genre and candidate_context:
        seed_genre_norm = _sc_normalise_text(seed_genre)
        if seed_genre_norm and seed_genre_norm in _sc_normalise_text(candidate_context):
            boost += 0.05

    return min(boost, 0.30)


def _compute_identity_match_score(
    seed_artist: str,
    candidate_display: str,
    candidate_handle: str,
    candidate_url: str = "",
    seed_location: str = "",
    seed_genre: str = "",
    seed_website: str = "",
    candidate_location: str = "",
    candidate_context: str = "",
    candidate_websites: Optional[Set[str]] = None,
) -> Tuple[float, str, Dict[str, float]]:
    """
    Compute conservative identity match score between seed artist and candidate profile.
    Returns (score, classification, debug_dict).
    Classification: exact / strong / plausible / weak/reject.
    """
    debug: Dict[str, float] = {}

    display_tier, display_score = _identity_name_tier(seed_artist, candidate_display)
    handle_tier, handle_score = _identity_handle_tier(seed_artist, candidate_handle)
    debug["display_score"] = round(display_score, 3)
    debug["handle_score"] = round(handle_score, 3)

    # Combine display name and handle: display dominates when present.
    if candidate_display and candidate_display.strip():
        base_score = display_score
        if handle_tier in ("exact", "strong"):
            base_score = max(base_score, base_score + 0.03)
        elif handle_tier == "plausible" and display_tier == "weak":
            base_score = max(base_score, handle_score * 0.5)
    elif candidate_handle and candidate_handle.strip():
        base_score = handle_score * 0.85
    else:
        base_score = 0.0

    debug["base_score"] = round(base_score, 3)

    # Contradictions
    contras = _identity_contradiction_penalty(seed_artist, candidate_display, candidate_handle, candidate_context)
    debug["contradictions"] = round(contras, 3)

    # Corroboration (only if base is at least weakly plausible)
    corro = 0.0
    if base_score >= 0.20:
        corro = _identity_corroboration_boost(
            seed_location, seed_genre, seed_website, candidate_location, candidate_context, candidate_websites
        )
    debug["corroboration"] = round(corro, 3)

    score = base_score + corro - contras
    score = max(0.0, min(score, 1.0))
    debug["final"] = round(score, 3)

    if score >= _IDENTITY_EXACT_THRESHOLD:
        classification = "exact"
    elif score >= _IDENTITY_STRONG_THRESHOLD:
        classification = "strong"
    elif score >= _IDENTITY_PLAUSIBLE_THRESHOLD:
        classification = "plausible"
    else:
        classification = "weak/reject"

    return score, classification, debug


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


def _bandcamp_context_matches(signal: str, candidate_context: str) -> bool:
    signal_norm = normalize_name(signal or "")
    context_norm = normalize_name(candidate_context or "")
    if not signal_norm or not context_norm:
        return False
    if signal_norm in context_norm:
        return True
    signal_tokens = [token for token in signal_norm.split() if len(token) >= 3]
    if len(signal_tokens) < 2:
        return False
    context_tokens = set(context_norm.split())
    return sum(1 for token in signal_tokens if token in context_tokens) >= 2


def _bandcamp_confidence(
    artist_name: str,
    display_name: str,
    profile_url: str,
    song_title: str = "",
    location_hint: str = "",
    genre_hint: str = "",
    candidate_context: str = "",
) -> float:
    """
    Conservative Bandcamp confidence using identity-evidence scoring.
    Preserves subdomain, song-title, location, genre boosts and label penalties.
    """
    artist_norm = normalize_name(artist_name)
    disp_norm = normalize_name(display_name or "")
    if not artist_norm or not disp_norm:
        return 0.0
    context_norm = normalize_name(candidate_context or display_name or "")

    # Identity-based name score
    score, classification, _ = _compute_identity_match_score(
        seed_artist=artist_name,
        candidate_display=display_name or "",
        candidate_handle="",
        candidate_url=profile_url or "",
    )

    # Subdomain boost when it closely matches the artist.
    try:
        parsed = urllib.parse.urlparse(profile_url or "")
        host = (parsed.netloc or "").split(".")[0].lower()
        host_norm = normalize_name(host)
        if host_norm and artist_norm and (host_norm == artist_norm or artist_norm in host_norm or host_norm in artist_norm):
            score = max(score, score + 0.06)
    except Exception:
        pass

    # Song-title boost if provided and appears in display text.
    song_norm = normalize_name(song_title or "")
    if song_norm and song_norm in context_norm:
        score += 0.06

    if score >= 0.45:
        location_signal = _bandcamp_location_signal(location_hint)
        if location_signal and _bandcamp_context_matches(location_signal, context_norm):
            score += 0.07
        genre_signal = _bandcamp_genre_signal(genre_hint)
        if genre_signal and _bandcamp_context_matches(genre_signal, context_norm):
            score += 0.05

    penalty_tokens = {"records", "recordings", "label", "store", "festival", "shop"}
    if any(tok in disp_norm for tok in penalty_tokens):
        score -= 0.1

    return max(0.0, min(score, 1.0))


_BC_SLUG_CONFIRM_ALLOWED_MODIFIERS = {"official", "music", "band"}
_BC_SLUG_CONFIRM_DISQUALIFY_TOKENS = {
    "records",
    "recordings",
    "label",
    "store",
    "shop",
    "festival",
    "dj",
}


def _bc_slug_extract_page_artist_text(html: str, fallback_text: str = "") -> str:
    if not html:
        return _clean_cell(fallback_text)
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return _clean_cell(fallback_text)

    def _clean(value: Any) -> str:
        text = _clean_cell(value)
        if not text:
            return ""
        return re.sub(r"^\s*by\s+", "", text, flags=re.IGNORECASE).strip()

    def _extract_text(node: Any) -> str:
        if not node:
            return ""
        for attr in ("content", "title", "aria-label"):
            candidate = _clean(node.get(attr, "")) if hasattr(node, "get") else ""
            if candidate:
                return candidate
        try:
            name_node = node.select_one("[itemprop='name']")
        except Exception:
            name_node = None
        if name_node:
            nested = _extract_text(name_node)
            if nested:
                return nested
        try:
            return _clean(node.get_text(" ", strip=True))
        except Exception:
            return ""

    try:
        by_artist_nodes = soup.select("[itemprop='byArtist']")
    except Exception:
        by_artist_nodes = []
    for node in by_artist_nodes:
        candidate = _extract_text(node)
        if candidate:
            return candidate

    og_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "og:title"})
    candidate = _extract_text(og_title)
    if candidate:
        return candidate

    title_el = soup.find("title")
    candidate = _extract_text(title_el)
    if candidate:
        return candidate
    return _clean(fallback_text)


def _bc_slug_has_strong_artist_name_confirmation(artist_name: str, page_artist: str) -> bool:
    artist_tokens = [tok for tok in normalize_name(artist_name).split() if tok]
    page_tokens = [tok for tok in normalize_name(page_artist).split() if tok]
    if not artist_tokens or not page_tokens:
        return False
    if any(tok in _BC_SLUG_CONFIRM_DISQUALIFY_TOKENS for tok in page_tokens):
        return False
    if page_tokens == artist_tokens:
        return True
    span = len(artist_tokens)
    for start_idx in range(len(page_tokens) - span + 1):
        if page_tokens[start_idx : start_idx + span] != artist_tokens:
            continue
        extras = page_tokens[:start_idx] + page_tokens[start_idx + span :]
        if extras and all(tok in _BC_SLUG_CONFIRM_ALLOWED_MODIFIERS for tok in extras):
            return True
    return False


def _bc_slug_identity_matches_artist(artist_name: str, identity_text: str) -> bool:
    artist_compact = re.sub(r"\s+", "", normalize_name(artist_name))
    candidate_compact = re.sub(r"\s+", "", normalize_name(identity_text))
    if not artist_compact or not candidate_compact:
        return False
    if any(token in candidate_compact for token in _BC_SLUG_CONFIRM_DISQUALIFY_TOKENS):
        return False
    if candidate_compact == artist_compact:
        return True
    trimmed = candidate_compact
    used_modifier = False
    changed = True
    while changed and trimmed:
        changed = False
        for modifier in _BC_SLUG_CONFIRM_ALLOWED_MODIFIERS:
            if trimmed.startswith(modifier) and len(trimmed) > len(modifier):
                trimmed = trimmed[len(modifier) :]
                used_modifier = True
                changed = True
                break
        if changed:
            continue
        for modifier in _BC_SLUG_CONFIRM_ALLOWED_MODIFIERS:
            if trimmed.endswith(modifier) and len(trimmed) > len(modifier):
                trimmed = trimmed[: -len(modifier)]
                used_modifier = True
                changed = True
                break
    return used_modifier and trimmed == artist_compact


def _bc_slug_outbound_identity_values(url: str) -> List[str]:
    identities: List[str] = []
    normalised = _normalise_url(url) or ""
    if not normalised:
        return identities
    try:
        parsed = urllib.parse.urlparse(normalised)
    except Exception:
        return identities
    host = (parsed.netloc or "").lower()
    if not host:
        return identities
    path_segments = [segment for segment in (parsed.path or "").split("/") if segment]

    canonical_ig = _canonicalize_instagram_profile_url(normalised)
    if canonical_ig:
        identities.append(urllib.parse.urlparse(canonical_ig).path.strip("/"))
        return identities

    canonical_fb = canonicalize_facebook_url(normalised)
    if canonical_fb:
        fb_segment = urllib.parse.urlparse(canonical_fb).path.strip("/").split("/", 1)[0]
        if fb_segment and fb_segment.lower() != "profile.php":
            identities.append(fb_segment)

    host_no_www = host[4:] if host.startswith("www.") else host
    if host_no_www in LINK_HUB_HOSTS and path_segments:
        identities.append(path_segments[0])
    elif host_no_www not in _INSTAGRAM_ALLOWED_HOSTS:
        identities.append(host_no_www.split(".", 1)[0])
    return [identity for identity in identities if identity]


def _bc_slug_has_strong_outbound_confirmation(artist_name: str, html: str, profile_url: str) -> bool:
    socials, websites, _, link_hubs = _extract_links_from_profile(html, "bandcamp", profile_url)
    for url in socials | websites | link_hubs:
        for identity in _bc_slug_outbound_identity_values(url):
            if _bc_slug_identity_matches_artist(artist_name, identity):
                return True
    return False


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


def _spotify_sparse_bandcamp_slug_candidates(artist_name: str) -> List[str]:
    """
    Spotify-local Bandcamp recovery variants: reuse the base slug guesses, then
    add a tiny suffix set on compact artist roots.
    """
    candidates: List[str] = []
    seen: Set[str] = set()

    def _push(slug: str) -> bool:
        cleaned = _clean_cell(slug).strip().lower()
        if not cleaned or cleaned in seen:
            return False
        seen.add(cleaned)
        candidates.append(cleaned)
        return len(candidates) >= SPOTIFY_BC_RECOVERY_MAX_SLUGS

    for slug in _bc_slug_candidates(artist_name):
        if _push(slug):
            return candidates

    norm = normalize_name(artist_name)
    words = [re.sub(r"[^a-z0-9]+", "", w) for w in norm.split() if re.sub(r"[^a-z0-9]+", "", w)]
    if not words:
        return candidates

    suffix_roots: List[str] = []
    if words[0] == "the" and len(words) > 1:
        suffix_roots.append("".join(words[1:]))
    suffix_roots.append("".join(words))

    for root in suffix_roots:
        for suffix in SPOTIFY_BC_RECOVERY_SUFFIXES:
            if _push(f"{root}{suffix}"):
                return candidates
            if _push(f"{root}-{suffix}"):
                return candidates
    return candidates


def _lastfm_confidence(artist_name: str, candidate_name: str) -> float:
    """Conservative Last.fm confidence using identity-evidence scoring."""
    artist_norm = normalize_name(artist_name)
    cand_norm = normalize_name(candidate_name)
    if not artist_norm or not cand_norm:
        return 0.0
    score, classification, _ = _compute_identity_match_score(
        seed_artist=artist_name,
        candidate_display=candidate_name,
        candidate_handle="",
    )
    if classification == "exact":
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


def _sc_title_metadata_boost(
    song_title: str = "",
    track_hint: str = "",
    *texts: str,
) -> float:
    haystack = normalise_track_title(" ".join(text for text in texts if text))
    if not haystack:
        return 0.0
    haystack_tokens = set(haystack.split())
    best = 0.0
    seen: Set[str] = set()
    for raw_signal in (song_title, track_hint):
        signal = normalise_track_title(raw_signal)
        if not signal or signal in seen:
            continue
        seen.add(signal)
        signal_tokens = [token for token in signal.split() if len(token) >= 3]
        if not signal_tokens:
            continue
        if signal in haystack:
            if len(signal_tokens) >= 2:
                best = max(best, 0.07)
            elif len(signal_tokens[0]) >= 6:
                best = max(best, 0.04)
            continue
        if len(signal_tokens) >= 2:
            overlap = sum(1 for token in signal_tokens if token in haystack_tokens)
            if overlap >= 2:
                best = max(best, 0.03)
    return best


def _sc_score_candidate(
    artist_name: str,
    candidate_name: str,
    handle: str,
    location_hint: str = "",
    candidate_location: str = "",
    genre_hint: str = "",
    candidate_context: str = "",
    profile_url: str = "",
    song_title: str = "",
    track_hint: str = "",
) -> float:
    """
    Conservative confidence score for SoundCloud candidates.
    Uses identity-evidence scoring instead of raw string similarity.
    Preserves location/genre/title boosts and label/podcast penalties.
    """
    artist_norm = _sc_normalise_text(artist_name)
    cand_norm = _sc_normalise_text(candidate_name or handle)
    handle_norm = _sc_normalise_text(handle)
    if not artist_norm or not cand_norm:
        return 0.0
    artist_norm_basic = _sc_strip_basic(artist_norm)
    cand_norm_basic = _sc_strip_basic(cand_norm)
    handle_norm_basic = _sc_strip_basic(handle_norm)

    # Core identity score
    score, classification, debug = _compute_identity_match_score(
        seed_artist=artist_name,
        candidate_display=candidate_name or "",
        candidate_handle=handle or "",
        candidate_url=profile_url or "",
        seed_location=location_hint or "",
        seed_genre=genre_hint or "",
        candidate_location=candidate_location or "",
        candidate_context=candidate_context or "",
    )

    # Preserve title/metadata boost for already-plausible candidates
    if score >= 0.40 or (
        artist_norm_basic and artist_norm_basic in {cand_norm_basic, handle_norm_basic}
    ):
        score += _sc_title_metadata_boost(
            song_title,
            track_hint,
            candidate_name,
            handle,
            candidate_context,
            profile_url,
        )

    # Preserve existing label/podcast penalties
    if any(keyword in handle_norm for keyword in _SC_LABEL_PODCAST_KEYWORDS):
        score -= 0.25
    if any(keyword in cand_norm for keyword in _SC_LABEL_PODCAST_KEYWORDS):
        score -= 0.15

    # Generic/short-name penalty unless exact basic match
    if artist_norm_basic in _SC_GENERIC_TOKENS or len(artist_norm_basic) <= 3:
        if not (artist_norm_basic and artist_norm_basic == cand_norm_basic == handle_norm_basic):
            score -= 0.15

    # Penalise digits in candidate when artist name has none
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
    EMAIL_PROVENANCE_JSON_COL,
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


def _apply_fb_promotion_row(
    df: pd.DataFrame,
    row_idx: Any,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> Tuple[bool, bool, str]:
    """Promote Facebook URLs for a single row into authoritative Facebook fields."""
    if df is None or df.empty or row_idx not in df.index:
        return False, False, ""
    if "facebook_url" not in df.columns:
        df["facebook_url"] = ""
    if "Facebook_URL" not in df.columns:
        df["Facebook_URL"] = ""
    if "Facebook URL" not in df.columns:
        df["Facebook URL"] = ""
    if share_resolver is not None:
        try:
            from pipeline_runner import _canonicalize_explicit_fb_share_for_row as _canonicalize_explicit_share_for_row
        except Exception:
            _canonicalize_explicit_share_for_row = None  # type: ignore[assignment]
        if callable(_canonicalize_explicit_share_for_row):
            _canonicalize_explicit_share_for_row(
                df,
                row_idx,
                share_resolver=share_resolver,
                logger=log_fn,
            )
    new_url, source = ensure_canonical_facebook_url(
        df.loc[row_idx],
        set_row=False,
        share_resolver=share_resolver,
    )
    if not new_url:
        return False, False, ""
    wrote = False
    promoted_into_canonical = False
    current_canonical_raw = _coerce_directory_value(df.loc[row_idx, "Facebook_URL"])
    current_canonical = canonicalize_facebook_url(current_canonical_raw)
    if current_canonical and current_canonical_raw != current_canonical:
        df.loc[row_idx, "Facebook_URL"] = current_canonical
    elif not current_canonical:
        df.loc[row_idx, "Facebook_URL"] = new_url
        wrote = True
        promoted_into_canonical = True
    if not canonicalize_facebook_url(df.loc[row_idx, "facebook_url"]):
        df.loc[row_idx, "facebook_url"] = new_url
        wrote = True
    if "Facebook URL" in df.columns and not canonicalize_facebook_url(df.loc[row_idx, "Facebook URL"]):
        df.loc[row_idx, "Facebook URL"] = new_url
        wrote = True
    return wrote, promoted_into_canonical, source


def _apply_fb_promotion_df(
    df: pd.DataFrame,
    log_fn: Optional[Callable[[str], None]] = None,
    *,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> pd.DataFrame:
    """Promote Facebook URLs from generic link fields into facebook_url/Facebook_URL."""
    if df is None or df.empty:
        return df
    populated = 0
    canonical_from_alias = 0
    canonical_from_links = 0
    for idx in df.index:
        wrote, promoted_into_canonical, source = _apply_fb_promotion_row(
            df,
            idx,
            log_fn=log_fn,
            share_resolver=share_resolver,
        )
        if not source:
            continue
        if wrote:
            populated += 1
        if not promoted_into_canonical:
            continue
        if source in {"Social Link", "External Links", "Website", "Websites", "Website URL"}:
            canonical_from_links += 1
        elif source and source != "Facebook_URL":
            canonical_from_alias += 1
    if log_fn and populated:
        _safe_log(log_fn, "[FB Promotion] facebook_url populated for %s rows", populated)
    if log_fn and canonical_from_alias:
        _safe_log(log_fn, "[FB Promotion] canonical Facebook_URL backfilled from alias fields for %s rows", canonical_from_alias)
    if log_fn and canonical_from_links:
        _safe_log(log_fn, "[FB Promotion] canonical Facebook_URL backfilled from Social Link / External Links for %s rows", canonical_from_links)
    return df


def _canonicalize_unearthed_share_rows_df(
    df: pd.DataFrame,
    *,
    row_ids: Optional[Iterable[Any]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    share_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> pd.DataFrame:
    """Resolve explicit Facebook /share wrappers for Unearthed rows before readiness gating."""
    if df is None or df.empty or share_resolver is None:
        return df
    try:
        from pipeline_runner import _canonicalize_explicit_fb_share_for_row as _canonicalize_explicit_share_for_row
    except Exception:
        return df
    selected_rows = set(row_ids) if row_ids is not None else None
    for row_idx in df.index:
        if selected_rows is not None and row_idx not in selected_rows:
            continue
        try:
            row = df.loc[row_idx]
        except Exception:
            continue
        if not _row_is_unearthed_source(row):
            continue
        _canonicalize_explicit_share_for_row(
            df,
            row_idx,
            share_resolver=share_resolver,
            logger=log_fn,
        )
    return df


_UNEARTHED_BC_RESERVED_SUBDOMAINS = frozenset(
    {
        "bandcamp",
        "blog",
        "daily",
        "discover",
        "get",
        "help",
        "music",
    }
)
_UNEARTHED_SC_RESERVED_HANDLES = frozenset(
    {
        "charts",
        "discover",
        "explore",
        "feed",
        "imprint",
        "pages",
        "popular",
        "search",
        "stream",
        "terms-of-use",
        "transparency-reports",
        "upload",
        "you",
    }
)


def _is_valid_unearthed_soundcloud_url(value: str) -> bool:
    normalised = _normalise_url(value or "")
    if not normalised:
        return False
    try:
        parsed = urllib.parse.urlparse(normalised)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "on.soundcloud.com":
        return bool((parsed.path or "").strip("/"))
    handle = _sc_handle_from_profile_url(normalised)
    return bool(handle and handle not in _UNEARTHED_SC_RESERVED_HANDLES)


def _canonicalise_musicbrainz_soundcloud_url(value: str) -> str:
    normalised = _normalise_url(value or "")
    if not normalised:
        return ""
    try:
        parsed = urllib.parse.urlparse(normalised)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "soundcloud.com":
        return ""
    handle = _sc_handle_from_profile_url(normalised)
    if not handle or handle in _UNEARTHED_SC_RESERVED_HANDLES:
        return ""
    return f"https://soundcloud.com/{handle}"


def _is_valid_unearthed_bandcamp_url(value: str) -> bool:
    canonical = _canonicalise_bandcamp_url(value or "")
    if not canonical:
        return False
    try:
        parsed = urllib.parse.urlparse(canonical)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "bandcamp.com" or not host.endswith(".bandcamp.com"):
        return False
    subdomain = host[: -len(".bandcamp.com")]
    if not subdomain or "." in subdomain:
        return False
    return subdomain not in _UNEARTHED_BC_RESERVED_SUBDOMAINS


def _extract_unearthed_platform_url(row: Any, platform: str) -> str:
    if row is None or not _row_is_unearthed_source(row):
        return ""

    if platform == "soundcloud":
        fields = (
            "SoundCloud Link",
            "SoundCloud URL",
            "SoundCloud_URL",
            "Soundcloud Link",
            "soundcloud_url",
            "Social Link",
            "External Links",
        )
        validator = _is_valid_unearthed_soundcloud_url
        canonicalizer = _normalise_url
    elif platform == "bandcamp":
        fields = (
            "Bandcamp_URL",
            "Bandcamp URL",
            "Bandcamp Link",
            "bandcamp_url",
            "Social Link",
            "External Links",
        )
        validator = _is_valid_unearthed_bandcamp_url
        canonicalizer = _canonicalise_bandcamp_url
    else:
        return ""

    for field in fields:
        try:
            raw_value = row.get(field, "") if hasattr(row, "get") else row[field]
        except Exception:
            raw_value = ""
        for token in _split_multi_value(raw_value):
            if not validator(token):
                continue
            canonical = canonicalizer(token)
            return canonical or token
    return ""


def _apply_unearthed_platform_promotion_df(
    df: pd.DataFrame,
    log_fn: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "SoundCloud Link" not in df.columns:
        df["SoundCloud Link"] = ""
    if "Bandcamp_URL" not in df.columns:
        df["Bandcamp_URL"] = ""

    promoted_soundcloud = 0
    promoted_bandcamp = 0
    for idx in df.index:
        row = df.loc[idx]
        soundcloud_url = _extract_unearthed_platform_url(row, "soundcloud")
        if soundcloud_url and not _is_valid_unearthed_soundcloud_url(_coerce_directory_value(df.at[idx, "SoundCloud Link"])):
            df.at[idx, "SoundCloud Link"] = soundcloud_url
            promoted_soundcloud += 1

        bandcamp_url = _extract_unearthed_platform_url(row, "bandcamp")
        if bandcamp_url and not _is_valid_unearthed_bandcamp_url(_coerce_directory_value(df.at[idx, "Bandcamp_URL"])):
            df.at[idx, "Bandcamp_URL"] = bandcamp_url
            promoted_bandcamp += 1

    if log_fn and promoted_soundcloud:
        _safe_log(log_fn, "[Unearthed Links] promoted SoundCloud from source for %s rows", promoted_soundcloud)
    if log_fn and promoted_bandcamp:
        _safe_log(log_fn, "[Unearthed Links] promoted Bandcamp from source for %s rows", promoted_bandcamp)
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
        yield_tracker: Optional[EnrichmentYieldTracker] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Night Mode toggle; default off so daytime behaviour is unchanged.
        self.night_mode: bool = False
        self.night_runtime_reset_interval_rows: Optional[Any] = None
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
        self._fb_discovery_disabled: bool = False
        self._fb_discovery_disabled_reason: str = ""
        self._fb_discovery_disable_logged: bool = False
        self._fb_session_auth_checked: bool = False
        self._fb_session_authenticated: bool = False
        self._initial_fb_session_warmup_complete: bool = False
        self._fb_session_warmup_complete: bool = False
        self._fb_session_auth_reason: str = ""
        self._fb_session_invalid: bool = False
        self._night_fb_share_promotion_resolver: Optional[Callable[[str], Optional[str]]] = None
        self.night_fb_run_state: Optional[NightFBRunState] = None
        self._spotify_discovery_attempted_rows: Set[Any] = set()
        self._spotify_sparse_bandcamp_attempted_rows: Set[Any] = set()
        self._spotify_identity_tier_rows: Set[Any] = set()
        self._spotify_identity_tier_counts: Counter = Counter()
        self._spotify_low_tier_fb_skips: int = 0
        self._spotify_low_tier_recovery_skips: int = 0
        self._spotify_identity_pass_attempted: int = 0
        self._spotify_identity_pass_enriched: int = 0
        self._spotify_identity_pass_no_signal: int = 0
        self._spotify_identity_pass_promotions: Counter = Counter()
        self._spotify_identity_guard_ctx: Dict[str, Any] = {}
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
        self._domain_profile_index: Dict[str, Dict[str, Any]] = {}
        self._domain_email_reuse_rows: Set[Any] = set()
        self._domain_email_reuse_count: int = 0
        self._website_email_cache: Dict[str, Dict[str, Any]] = {}
        self._instagram_hidden_contact_attempt_keys: Set[Tuple[int, int]] = set()
        self._festival_expansion_rows: List[Dict[str, Any]] = []
        self._festival_expansion_existing_keys: Set[str] = set()
        self._festival_expansion_staged_keys: Set[str] = set()
        self._festival_expansion_raw_csv_path: str = _festival_expansion_raw_path(output_csv_path)
        self._domain_org_sidecar_path: str = _domain_org_index_path(output_csv_path)
        self._yield_tracker = yield_tracker or EnrichmentYieldTracker()
        self._resume_checkpoint = None
        self._resume_row_index: int = 0
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

    def _emit_runtime_reset_log(self, message: str) -> None:
        if not message:
            return
        try:
            self.log_message.emit(message)
        except Exception:
            pass

    def _selected_row_ids(self, seed_df: pd.DataFrame, row_ids: Optional[Iterable[Any]] = None) -> List[Any]:
        if seed_df is None:
            return []
        if row_ids is None:
            base_rows = list(seed_df.index)
        else:
            base_rows = []
            index_membership = set(seed_df.index)
            for row_idx in row_ids:
                if row_idx in index_membership:
                    base_rows.append(row_idx)
        try:
            resume_row_index = int(getattr(self, "_resume_row_index", 0) or 0)
        except RuntimeError:
            resume_row_index = 0
        if resume_row_index <= 0:
            return base_rows
        return [row_idx for row_idx in base_rows if isinstance(row_idx, int) and row_idx >= resume_row_index]

    def _checkpoint_row_complete(self, seed_df: pd.DataFrame, row_idx: Any) -> None:
        try:
            checkpoint = getattr(self, "_resume_checkpoint", None)
        except RuntimeError:
            checkpoint = None
        if checkpoint is None:
            return
        if not isinstance(row_idx, int):
            raise RuntimeError(f"resume checkpoint row index must be int, got {type(row_idx).__name__}")
        _ensure_parent_dir(self.output_csv_path)
        seed_df.to_csv(self.output_csv_path, index=False, encoding="utf-8-sig")
        checkpoint.append_completed(row_idx, logger=self.log_message.emit)
        try:
            processed_rows = int(row_idx) + 1
            meta = {
                "phase": "enrichment",
                "current_status": "row_write_complete",
            }
            source_guess = ""
            if self.unearthed_csv_path:
                source_guess = "unearthed"
            elif self.bandcamp_csv_path:
                source_guess = "bandcamp"
            elif self.soundcloud_csv_path:
                source_guess = "soundcloud"
            elif self.lastfm_csv_path:
                source_guess = "lastfm"
            if source_guess:
                meta["current_source"] = source_guess
            update_progress(processed_rows, meta=meta)
        except Exception:
            pass

    def _start_chunk_yield_window(
        self,
        *,
        chunk_index: int,
        active_row_ids: List[Any],
        configured_interval: int,
    ) -> None:
        if not active_row_ids:
            self._active_chunk_yield = None
            return
        self._active_chunk_yield = ChunkYieldWindow(
            chunk_index=chunk_index,
            row_ids=list(active_row_ids),
            configured_interval=configured_interval,
        )

    def _active_chunk_source_row_state(
        self,
        source: str,
        row_idx: Any,
    ) -> Optional[ChunkYieldSourceRowState]:
        chunk = getattr(self, "_active_chunk_yield", None)
        if chunk is None or row_idx not in chunk.row_ids:
            return None
        source_rows = chunk.source_rows.setdefault(source, {})
        row_state = source_rows.get(row_idx)
        if row_state is None:
            row_state = ChunkYieldSourceRowState()
            source_rows[row_idx] = row_state
        return row_state

    def _record_chunk_source_opportunity(self, source: str, row_idx: Any) -> None:
        row_state = self._active_chunk_source_row_state(source, row_idx)
        if row_state is not None:
            row_state.opportunity = True

    def _record_chunk_source_attempt(
        self,
        source: str,
        row_idx: Any,
        *,
        seam: str = "execution",
    ) -> None:
        row_state = self._active_chunk_source_row_state(source, row_idx)
        if row_state is not None:
            seam_name = str(seam or "").strip() or "execution"
            row_state.attempt_seams.add(seam_name)

    def _record_chunk_source_found(self, source: str, row_idx: Any, emails: Iterable[Any]) -> None:
        row_state = self._active_chunk_source_row_state(source, row_idx)
        if row_state is None:
            return
        if _normalized_email_set_from_values(emails):
            row_state.email_found = True

    def _record_chunk_source_written(
        self,
        source: str,
        row_idx: Any,
        *,
        before_row: Any,
        after_row: Any,
        found_emails: Iterable[Any],
    ) -> None:
        row_state = self._active_chunk_source_row_state(source, row_idx)
        if row_state is None:
            return

        found_email_set = _normalized_email_set_from_values(found_emails)
        if not found_email_set:
            return

        if source == "facebook":
            applied_email_set = _normalized_email_set_from_values(
                after_row.get("__fb_emails_applied", "") if hasattr(after_row, "get") else ""
            )
            if applied_email_set & found_email_set:
                row_state.email_written = True
            return

        committed_delta, delta_intersection = _committed_row_email_delta_intersection(
            before_row,
            after_row,
            found_email_set,
        )
        if committed_delta and delta_intersection:
            row_state.email_written = True

    def _emit_chunk_yield_summary(self, *, chunk_end_reason: str) -> None:
        chunk = getattr(self, "_active_chunk_yield", None)
        if chunk is None:
            return

        self.log_message.emit(
            "[Chunk Yield] "
            f"chunk_index={chunk.chunk_index} "
            f"row_start_index={chunk.row_start_index} "
            f"row_end_index={chunk.row_end_index} "
            f"rows_in_chunk={chunk.rows_in_chunk} "
            f"configured_interval={chunk.configured_interval} "
            f"chunk_end_reason={chunk_end_reason}"
        )

        for source, prefix in (("facebook", "fb"), ("instagram", "ig")):
            row_states = chunk.source_rows.get(source, {})
            opportunity_rows = sum(1 for state in row_states.values() if state.opportunity)
            attempted_rows = sum(1 for state in row_states.values() if state.attempted)
            email_found_rows = sum(1 for state in row_states.values() if state.email_found)
            email_written_rows = sum(1 for state in row_states.values() if state.email_written)
            # Derived visibility flag only. This distinguishes zero-opportunity
            # chunks from chunks that had opportunity but produced no yield.
            opportunity_present = 1 if opportunity_rows else 0
            self.log_message.emit(
                f"[Chunk Yield][{prefix.upper()}] "
                f"chunk_index={chunk.chunk_index} "
                f"{prefix}_opportunity_rows={opportunity_rows} "
                f"{prefix}_opportunity_present={opportunity_present} "
                f"{prefix}_attempted_rows={attempted_rows} "
                f"{prefix}_email_found_rows={email_found_rows} "
                f"{prefix}_email_written_rows={email_written_rows}"
            )

        self._active_chunk_yield = None

    def _resolve_night_runtime_reset_interval_rows(self) -> int:
        if not getattr(self, "night_mode", False):
            return 0
        raw_value = getattr(self, "night_runtime_reset_interval_rows", None)
        if raw_value is None:
            return NIGHT_RUNTIME_RESET_INTERVAL_ROWS_DEFAULT
        if isinstance(raw_value, str) and not raw_value.strip():
            return 0
        try:
            interval = int(raw_value)
        except Exception:
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] interval_rows_raw={raw_value!r} normalized_interval_rows=0 reason=invalid_value"
            )
            return 0
        if interval < 0:
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] interval_rows_raw={raw_value!r} normalized_interval_rows=0 reason=negative_value"
            )
            return 0
        return interval

    def _create_fb_runtime_for_current_chunk(self):
        fb_driver = None
        if not ENABLE_FACEBOOK_ENRICHMENT:
            return None
        try:
            if getattr(self, "night_mode", False):
                night_fb_source = (
                    self.night_fb_run_state.session_source
                    if self.night_fb_run_state is not None
                    else normalize_night_fb_session_source()
                )
                if not night_fb_source.can_probe:
                    self._disable_fb_discovery_for_run(night_fb_source.reason)
                    return None
                headless = str(os.environ.get("NIGHT_FB_HEADLESS", "") or "").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
                if self.night_fb_run_state is not None:
                    shared_session = ensure_night_fb_run_session(
                        self.night_fb_run_state,
                        headless=headless,
                        logger=self.log_message.emit,
                        owner="cross_directory_enricher",
                    )
                    fb_driver = getattr(shared_session, "driver", None) if shared_session is not None else None
                else:
                    fb_driver = _get_enricher_facebook_driver(
                        profile_dir=night_fb_source.profile_dir or None
                    )
                if fb_driver is not None:
                    self._ensure_fb_discovery_session(fb_driver, force=True)
                return fb_driver
            fb_driver = _get_enricher_facebook_driver()
            if not enricher_fb_profile_has_cookies():
                try:
                    fb_driver.get("https://www.facebook.com/")
                except Exception as exc:
                    if _fb_exception_is_fatal_session(exc):
                        raise
                message = "[FB Enrich] Please manually log into Facebook in the opened window."
                _safe_log(self.log_message.emit, message)
                try:
                    input("Press ENTER once logged in…")
                except EOFError:
                    pass
                except Exception:
                    pass
                self._ensure_fb_discovery_session(fb_driver, force=True)
            else:
                auth_cookie_state = _fb_driver_has_auth_cookie(fb_driver)
                if auth_cookie_state is False:
                    self._disable_fb_discovery_for_run("not_authenticated")
            return fb_driver
        except Exception as exc:
            if _fb_exception_is_fatal_session(exc):
                self._disable_fb_discovery_for_run("session_invalid", session_invalid=True)
            _safe_log(
                self.log_message.emit,
                "[FB Enrich] Failed to start Facebook driver: %s",
                exc,
            )
            return None

    def _reset_fb_runtime_local_state(self) -> None:
        self._fb_discovery_attempted_rows = set()
        self._fb_discovery_disabled = False
        self._fb_discovery_disabled_reason = ""
        self._fb_discovery_disable_logged = False
        self._fb_session_auth_checked = False
        self._fb_session_authenticated = False
        self._fb_session_warmup_complete = False
        self._initial_fb_session_warmup_complete = False
        self._fb_session_auth_reason = ""
        self._fb_session_invalid = False
        self._night_fb_share_promotion_resolver = None
        if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
            reset_night_fb_run_runtime_state(self.night_fb_run_state)

    def _get_night_fb_share_promotion_resolver(self) -> Optional[Callable[[str], Optional[str]]]:
        if not self.__dict__.get("night_mode", False):
            return None
        cached_resolver = self.__dict__.get("_night_fb_share_promotion_resolver")
        if cached_resolver is not None:
            return cached_resolver
        night_fb_state = self.__dict__.get("night_fb_run_state")
        session_source = getattr(night_fb_state, "session_source", None)
        night_fb_source = session_source if session_source is not None else normalize_night_fb_session_source()
        if not night_fb_source.can_probe:
            return None
        from pipeline_runner import _build_night_fb_share_promotion_resolver

        self._night_fb_share_promotion_resolver = _build_night_fb_share_promotion_resolver(
            fb_username=str(os.environ.get("FB_USERNAME", "") or ""),
            fb_password=str(os.environ.get("FB_PASSWORD", "") or ""),
            night_fb_run_state=night_fb_state,
            logger=getattr(self.log_message, "emit", None),
        )
        return self._night_fb_share_promotion_resolver

    def _reset_ig_runtime_local_state(self) -> None:
        self._instagram_hidden_contact_attempt_keys = set()
        canary_page = getattr(self, "_night_runtime_ig_canary_page", None)
        self._night_runtime_ig_canary_page = None
        if canary_page is not None:
            try:
                canary_page.close()
            except Exception:
                pass
        try:
            html_fetcher.close_job_browser("global")
        except Exception:
            pass

    def _teardown_night_runtime_for_chunk(self) -> None:
        self._reset_ig_runtime_local_state()
        self._reset_fb_runtime_local_state()
        _cleanup_enricher_facebook_driver()

    def _recreate_night_runtime_for_chunk(self):
        try:
            ensure_context = getattr(html_fetcher, "_ensure_context", None)
            if callable(ensure_context):
                ensure_context("global")
        except Exception:
            pass
        self._prepare_ig_runtime_canary_page()
        return self._create_fb_runtime_for_current_chunk()

    def _prepare_ig_runtime_canary_page(self):
        self._night_runtime_ig_canary_page = None
        shared_job_browser = getattr(html_fetcher, "_JOB_BROWSERS", {}).get("global")
        context = getattr(shared_job_browser, "context", None) if shared_job_browser is not None else None
        if context is None:
            return None
        try:
            canary_page = context.new_page()
        except Exception:
            return None
        self._night_runtime_ig_canary_page = canary_page
        return canary_page

    @staticmethod
    def _runtime_canary_exception_reason(exc: Exception, *, source: str) -> str:
        if source == "facebook" and _fb_exception_is_fatal_session(exc):
            return "session_invalid"
        message = cell_to_str(exc).strip().lower()
        if "timeout" in message:
            return "timeout"
        if source == "facebook":
            return "driver_error"
        return "page_error"

    def _fb_runtime_canary_explicitly_disabled(self) -> bool:
        if not ENABLE_FACEBOOK_ENRICHMENT:
            return True
        if not getattr(self, "night_mode", False):
            return False
        run_state = getattr(self, "night_fb_run_state", None)
        if run_state is None:
            return False
        session_source = getattr(run_state, "session_source", None)
        return bool(session_source is not None and not bool(getattr(session_source, "can_probe", True)))

    @staticmethod
    def _normalize_runtime_canary_dom_probe(probe_result: Any) -> Tuple[str, bool]:
        ready_state = ""
        has_body = False
        if isinstance(probe_result, dict):
            ready_state = cell_to_str(
                probe_result.get("readyState", probe_result.get("ready_state", ""))
            ).strip().lower()
            has_body = bool(probe_result.get("hasBody", probe_result.get("has_body", False)))
            return ready_state, has_body
        if isinstance(probe_result, (list, tuple)):
            if probe_result:
                ready_state = cell_to_str(probe_result[0]).strip().lower()
            if len(probe_result) > 1:
                has_body = bool(probe_result[1])
            return ready_state, has_body
        return cell_to_str(probe_result).strip().lower(), False

    def _wait_for_runtime_canary_dom_signal(
        self,
        *,
        source: str,
        state_probe: Callable[[], Dict[str, Any]],
    ) -> Tuple[bool, str]:
        deadline = time.time() + max(float(NIGHT_RUNTIME_CANARY_TIMEOUT_S or 0.0), 0.1)
        while True:
            try:
                probe_state = state_probe() or {}
            except Exception as exc:
                return False, self._runtime_canary_exception_reason(exc, source=source)

            surface_reason = cell_to_str(probe_state.get("surface_reason", "")).strip().lower()
            if surface_reason:
                return False, surface_reason

            ready_state, has_body = self._normalize_runtime_canary_dom_probe(
                probe_state.get(
                    "dom_probe",
                    (
                        probe_state.get("ready_state", ""),
                        probe_state.get("has_body", False),
                    ),
                )
            )
            if ready_state in {"interactive", "complete"} or has_body:
                return True, "ready"
            if time.time() >= deadline:
                return False, "dom_not_ready"
            time.sleep(0.05)

    def _run_fb_runtime_canary(self, fb_driver) -> Tuple[bool, str]:
        if self._fb_runtime_canary_explicitly_disabled():
            return True, "source_disabled"
        if fb_driver is None:
            return False, "no_driver"

        decision = probe_night_fb_session_decision(fb_driver, visit_home=False)
        if not (decision.authenticated and decision.usable):
            return False, decision.reason or decision.state or "session_unusable"

        try:
            set_script_timeout = getattr(fb_driver, "set_script_timeout", None)
            if callable(set_script_timeout):
                set_script_timeout(NIGHT_RUNTIME_CANARY_TIMEOUT_S)
        except Exception as exc:
            return False, self._runtime_canary_exception_reason(exc, source="facebook")

        def _probe_state() -> Dict[str, Any]:
            current_url = cell_to_str(getattr(fb_driver, "current_url", "")).strip()
            page_source = cell_to_str(getattr(fb_driver, "page_source", "")).strip()
            if _is_fb_login_or_security_url(current_url):
                return {"surface_reason": "login_wall"}
            warning_reason = cell_to_str(
                _looks_like_fb_warning_or_block(page_source, current_url)
            ).strip().lower()
            return {
                "surface_reason": warning_reason,
                "dom_probe": fb_driver.execute_script(
                    "return [document.readyState || '', !!document.body];"
                ),
            }

        return self._wait_for_runtime_canary_dom_signal(
            source="facebook",
            state_probe=_probe_state,
        )

    def _run_ig_runtime_canary(self) -> Tuple[bool, str]:
        shared_job_browser = getattr(html_fetcher, "_JOB_BROWSERS", {}).get("global")
        if shared_job_browser is None:
            return False, "no_browser"
        browser = getattr(shared_job_browser, "browser", None)
        context = getattr(shared_job_browser, "context", None)
        page = getattr(self, "_night_runtime_ig_canary_page", None)
        if browser is None:
            return False, "no_browser"
        if context is None:
            return False, "no_context"
        if page is None:
            return False, "no_page"
        try:
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed) and is_closed():
                return False, "page_closed"
        except Exception:
            return False, "page_invalid"

        def _probe_state() -> Dict[str, Any]:
            current_url = cell_to_str(getattr(page, "url", "")).strip()
            page_html = cell_to_str(page.content()).strip()
            current_url_l = current_url.lower()
            page_html_l = page_html.lower()
            surface_reason = ""
            if _detect_soft_block(page_html):
                surface_reason = "blocked_surface"
            elif any(token in current_url_l for token in ("/accounts/login", "/challenge", "/checkpoint", "/consent")):
                surface_reason = "blocked_surface"
            elif any(
                token in page_html_l
                for token in (
                    "security check",
                    "challenge_required",
                    "checkpoint",
                    "consent",
                )
            ):
                surface_reason = "blocked_surface"
            elif any(
                token in page_html_l
                for token in (
                    "sorry, something went wrong",
                    "page isn't available",
                    "sorry, this page isn't available",
                    "account suspended",
                )
            ):
                surface_reason = "error_surface"
            return {
                "surface_reason": surface_reason,
                "dom_probe": page.evaluate(
                    "() => [document.readyState || '', Boolean(document.body)]"
                ),
            }

        return self._wait_for_runtime_canary_dom_signal(
            source="instagram",
            state_probe=_probe_state,
        )

    def _run_post_reset_runtime_canary(
        self,
        *,
        fb_driver,
        attempt_index: int,
    ) -> None:
        self._emit_runtime_reset_log(
            f"[Runtime Canary] phase=start attempt={attempt_index}"
        )

        fb_passed, fb_reason = self._run_fb_runtime_canary(fb_driver)
        fb_log = f"[Runtime Canary][FB] attempt={attempt_index} result={'pass' if fb_passed else 'fail'}"
        if fb_reason and (fb_reason != "ready" or not fb_passed):
            fb_log += f" reason={fb_reason}"
        self._emit_runtime_reset_log(fb_log)

        ig_passed, ig_reason = self._run_ig_runtime_canary()
        ig_log = f"[Runtime Canary][IG] attempt={attempt_index} result={'pass' if ig_passed else 'fail'}"
        if ig_reason and (ig_reason != "ready" or not ig_passed):
            ig_log += f" reason={ig_reason}"
        self._emit_runtime_reset_log(ig_log)
        if not fb_passed:
            raise NightRuntimeCanaryFailure("facebook", fb_reason, attempt_index)
        if not ig_passed:
            raise NightRuntimeCanaryFailure("instagram", ig_reason, attempt_index)

    def _reset_night_runtime_chunk(
        self,
        *,
        interval_rows: int,
        completed_rows: int,
        next_row_id: Any,
        next_row_index: Any,
    ):
        def _teardown_and_recreate(*, canary_attempt: int):
            reset_scope = (
                f"interval_rows={interval_rows} completed_rows={completed_rows} "
                f"next_row_index={next_row_index} next_row_id={next_row_id} canary_attempt={canary_attempt}"
            )
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] {reset_scope} teardown_start=1"
            )
            self._teardown_night_runtime_for_chunk()
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] {reset_scope} teardown_complete=1"
            )
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] {reset_scope} recreate_start=1"
            )
            recreated_fb_driver = self._recreate_night_runtime_for_chunk()
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] {reset_scope} recreate_complete=1 "
                f"fb_driver_ready={1 if recreated_fb_driver is not None else 0}"
            )
            return recreated_fb_driver

        fb_driver = _teardown_and_recreate(canary_attempt=1)
        try:
            self._run_post_reset_runtime_canary(
                fb_driver=fb_driver,
                attempt_index=1,
            )
        except NightRuntimeCanaryFailure as first_failure:
            self._emit_runtime_reset_log(
                f"[Runtime Canary] retry=1 action=recreate_runtime source={first_failure.source} "
                f"reason={first_failure.reason}"
            )
            fb_driver = _teardown_and_recreate(canary_attempt=2)
            try:
                self._run_post_reset_runtime_canary(
                    fb_driver=fb_driver,
                    attempt_index=2,
                )
            except NightRuntimeCanaryFailure as final_failure:
                self._emit_runtime_reset_log(
                    f"[Runtime Canary] final_disposition=block source={final_failure.source} "
                    f"reason={final_failure.reason} attempt={final_failure.attempt_index}"
                )
                raise

        self._emit_runtime_reset_log("[Runtime Canary] final_disposition=resume")
        return fb_driver

    def _run_with_night_runtime_chunks(
        self,
        seed_df: pd.DataFrame,
        directory_indexes,
        priority,
        fb_driver,
        total: int,
        *,
        enrichment_mode: str,
    ) -> None:
        row_ids = self._selected_row_ids(seed_df)

        def _run_chunk(active_row_ids: List[Any], active_fb_driver) -> None:
            if enrichment_mode == "source_phased":
                self._run_source_phased(
                    seed_df,
                    directory_indexes,
                    priority,
                    active_fb_driver,
                    total,
                    row_ids=active_row_ids,
                )
            else:
                self._run_row_linear(
                    seed_df,
                    directory_indexes,
                    priority,
                    active_fb_driver,
                    total,
                    row_ids=active_row_ids,
                )

        if not getattr(self, "night_mode", False):
            _run_chunk(row_ids, fb_driver)
            return

        interval_rows = self._resolve_night_runtime_reset_interval_rows()
        enabled = bool(interval_rows > 0 and row_ids)
        self._emit_runtime_reset_log(
            f"[Night Runtime Reset] interval_rows={interval_rows} completed_rows=0 boundary_reached=0 "
            f"enabled={1 if enabled else 0} total_rows={len(row_ids)}"
        )

        if not enabled:
            _run_chunk(row_ids, fb_driver)
            return

        position_by_row = {
            row_idx: pos
            for pos, row_idx in enumerate(seed_df.index, start=1)
        }
        current_fb_driver = fb_driver
        completed_rows = 0
        for chunk_index, chunk_start in enumerate(range(0, len(row_ids), interval_rows), start=1):
            active_row_ids = row_ids[chunk_start : chunk_start + interval_rows]
            self._start_chunk_yield_window(
                chunk_index=chunk_index,
                active_row_ids=active_row_ids,
                configured_interval=interval_rows,
            )
            _run_chunk(active_row_ids, current_fb_driver)
            completed_rows += len(active_row_ids)
            boundary_reached = completed_rows < len(row_ids)
            next_row_id = row_ids[completed_rows] if boundary_reached else "<none>"
            next_row_index = position_by_row.get(next_row_id, "<none>") if boundary_reached else "<none>"
            self._emit_chunk_yield_summary(
                chunk_end_reason="reset_boundary" if boundary_reached else "end_of_run"
            )
            self._emit_runtime_reset_log(
                f"[Night Runtime Reset] interval_rows={interval_rows} completed_rows={completed_rows} "
                f"boundary_reached={1 if boundary_reached else 0} next_row_index={next_row_index} next_row_id={next_row_id}"
            )
            if boundary_reached:
                current_fb_driver = self._reset_night_runtime_chunk(
                    interval_rows=interval_rows,
                    completed_rows=completed_rows,
                    next_row_id=next_row_id,
                    next_row_index=next_row_index,
                )

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
        output_csv_path = self.output_csv_path.strip() if isinstance(self.output_csv_path, str) else ""
        self._domain_org_sidecar_path = ""
        if output_csv_path and os.path.dirname(output_csv_path):
            self._domain_org_sidecar_path = _domain_org_index_path(output_csv_path)
        try:
            if self._festival_expansion_raw_csv_path and os.path.exists(self._festival_expansion_raw_csv_path):
                os.remove(self._festival_expansion_raw_csv_path)
        except Exception:
            pass
        try:
            if self._domain_org_sidecar_path and os.path.exists(self._domain_org_sidecar_path):
                os.remove(self._domain_org_sidecar_path)
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
        self._fb_discovery_disabled = False
        self._fb_discovery_disabled_reason = ""
        self._fb_discovery_disable_logged = False
        self._fb_session_auth_checked = False
        self._fb_session_authenticated = False
        self._fb_session_warmup_complete = bool(getattr(self, "_initial_fb_session_warmup_complete", False))
        self._fb_session_auth_reason = ""
        self._fb_session_invalid = False
        if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
            self._initial_fb_session_warmup_complete = bool(self.night_fb_run_state.session_warmup_complete)
            self._fb_session_warmup_complete = bool(self.night_fb_run_state.session_warmup_complete)
            if self.night_fb_run_state.disabled_for_run or self.night_fb_run_state.session_invalid:
                self._fb_discovery_disabled = bool(self.night_fb_run_state.disabled_for_run)
                self._fb_discovery_disabled_reason = self.night_fb_run_state.disable_reason or "disabled"
                self._fb_session_auth_checked = True
                self._fb_session_authenticated = bool(self.night_fb_run_state.authenticated)
                self._fb_session_auth_reason = self._fb_discovery_disabled_reason
                self._fb_session_invalid = bool(self.night_fb_run_state.session_invalid)
        self._spotify_discovery_attempted_rows = set()
        self._spotify_sparse_bandcamp_attempted_rows = set()
        self._spotify_identity_tier_rows = set()
        self._spotify_identity_tier_counts = Counter()
        self._spotify_low_tier_fb_skips = 0
        self._spotify_low_tier_recovery_skips = 0
        self._spotify_identity_pass_attempted = 0
        self._spotify_identity_pass_enriched = 0
        self._spotify_identity_pass_no_signal = 0
        self._spotify_identity_pass_promotions = Counter()
        self._spotify_identity_guard_ctx = {}
        self._domain_email_reuse_index = {}
        self._domain_profile_index = {}
        self._domain_email_reuse_rows = set()
        self._domain_email_reuse_count = 0
        self._website_email_cache = {}
        # Bandcamp discover per-run state
        self._bc_discover_cache = {}
        self._bc_discover_fetches = 0
        self._instagram_hidden_contact_attempt_keys = set()
        self._active_chunk_yield = None
        try:
            fb_driver = self._create_fb_runtime_for_current_chunk()
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
            if getattr(self, "night_mode", False):
                seed_df = seed_df.reset_index(drop=True)
            self._festival_expansion_existing_keys = {
                normalise_artist_name(_clean_cell(name))
                for name in seed_df.get("Artist Name", pd.Series(dtype=str)).tolist()
                if normalise_artist_name(_clean_cell(name))
            }
            seed_df = _ensure_email_columns(seed_df)
            self.log_message.emit(
                "[Schema] ensured email columns: Email, Email_All, Email_Type, Email_Source_URL, Email_Source_Type, Email_Extract_Method, Email_Provenance_JSON"
            )
            seed_df = _apply_fb_promotion_df(
                seed_df,
                log_fn=self.log_message.emit,
                share_resolver=self._get_night_fb_share_promotion_resolver(),
            )
            seed_df = ensure_ig_attribution_columns(seed_df)
            if getattr(self, "night_mode", False):
                seed_df = ensure_fb_attribution_columns(seed_df)
                seed_df = apply_fb_opportunity_state_df(seed_df, overwrite=True)
            total = len(seed_df.index)
            self.total_rows = total
            if not getattr(self, "night_mode", False):
                try:
                    init_progress(
                        total,
                        os.path.basename(os.path.abspath(self.output_csv_path or self.seed_csv_path or "enrichment")),
                        meta={"phase": "enrichment", "current_status": "enrichment_start"},
                    )
                except Exception:
                    pass
            if getattr(self, "night_mode", False):
                try:
                    from pipeline_runner import ResumeCheckpointError, build_resume_checkpoint

                    enrichment_mode = os.getenv("ENRICHMENT_MODE", "row_linear")
                    self._resume_checkpoint = build_resume_checkpoint(
                        self.seed_csv_path,
                        os.path.dirname(os.path.abspath(self.output_csv_path)) or ".",
                        total,
                        {
                            "enrichment_mode": enrichment_mode,
                            "enabled_sources": {
                                "bandcamp": bool(self.bandcamp_csv_path),
                                "soundcloud": bool(self.soundcloud_csv_path),
                                "lastfm": bool(self.lastfm_csv_path),
                                "unearthed": bool(self.unearthed_csv_path),
                                "live_search": bool(self.enable_live_search),
                                "facebook": bool(ENABLE_FACEBOOK_ENRICHMENT),
                                "instagram": True,
                                "website": True,
                            },
                            "max_live_searches": int(self.max_live_searches or 0),
                            "bandcamp_csv_path": os.path.abspath(self.bandcamp_csv_path) if self.bandcamp_csv_path else "",
                            "soundcloud_csv_path": os.path.abspath(self.soundcloud_csv_path) if self.soundcloud_csv_path else "",
                            "lastfm_csv_path": os.path.abspath(self.lastfm_csv_path) if self.lastfm_csv_path else "",
                            "unearthed_csv_path": os.path.abspath(self.unearthed_csv_path) if self.unearthed_csv_path else "",
                        },
                        logger=self.log_message.emit,
                    )
                    self._resume_row_index = int(self._resume_checkpoint.resume_row_index)
                    if self._resume_checkpoint.last_completed_row_index >= 0:
                        if not os.path.exists(self.output_csv_path):
                            self.log_message.emit(
                                f'[Resume Error] invalid_checkpoint path="{self._resume_checkpoint.path}" reason="output_missing"'
                            )
                            self.finished.emit("")
                            return
                        previous_df = _read_csv_flexible(self.output_csv_path)
                        if previous_df is None or len(previous_df.index) != total:
                            self.log_message.emit(
                                f'[Resume Error] invalid_checkpoint path="{self._resume_checkpoint.path}" reason="row_count_mismatch"'
                            )
                            self.finished.emit("")
                            return
                        previous_df = previous_df.fillna("").reset_index(drop=True)
                        for col in previous_df.columns:
                            if col not in seed_df.columns:
                                seed_df[col] = ""
                        shared_cols = [col for col in previous_df.columns if col in seed_df.columns]
                        completed_until = self._resume_checkpoint.last_completed_row_index
                        if completed_until >= 0 and shared_cols:
                            seed_df.loc[:completed_until, shared_cols] = previous_df.loc[:completed_until, shared_cols]
                    if self._resume_checkpoint.is_complete:
                        try:
                            update_progress(
                                total,
                                meta={"phase": "enrichment", "current_status": "row_write_complete"},
                            )
                        except Exception:
                            pass
                        self.finished.emit(self.output_csv_path)
                        return
                except ResumeCheckpointError:
                    self.finished.emit("")
                    return
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
            provenance_columns = ("Email_Source_URL", "Email_Source_Type", "Email_Extract_Method", EMAIL_PROVENANCE_JSON_COL)
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
            self._run_with_night_runtime_chunks(
                seed_df,
                directory_indexes,
                priority,
                fb_driver,
                total,
                enrichment_mode=_enrichment_mode,
            )
            self._run_late_domain_email_backfill(seed_df, total)
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
            try:
                email_count = 0
                for column in ("Email", "Email_All"):
                    if column in seed_df.columns:
                        email_count = max(
                            email_count,
                            int(seed_df[column].fillna("").astype(str).str.strip().ne("").sum()),
                        )
                update_progress(
                    total,
                    meta={
                        "phase": "enrichment",
                        "emails_found": email_count,
                        "current_status": "row_write_complete",
                    },
                )
            except Exception:
                pass
            if getattr(self, "_resume_checkpoint", None) is not None:
                self.log_message.emit(f"[Resume] job_complete rows={total}/{total}")
            self.log_message.emit(f"[Enricher] Enriched CSV written to {self.output_csv_path}")
            try:
                _write_domain_org_sidecar(
                    self.output_csv_path,
                    self._domain_profile_index,
                    self._domain_email_reuse_index,
                    log_fn=self.log_message.emit,
                )
            except Exception as exc:
                self.log_message.emit(f"[DomainOrg] failed to write sidecar safely: {exc}")
            self.finished.emit(self.output_csv_path)
        finally:
            _cleanup_enricher_facebook_driver()

    def _record_enrichment_yield(
        self,
        row_idx: Any,
        before_row: Any,
        after_row: Any,
        source_name: Any,
    ) -> None:
        tracker = getattr(self, "_yield_tracker", None)
        if tracker is None:
            return
        try:
            tracker.record_transition(row_idx, before_row, after_row, source_name)
        except Exception:
            pass

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

    def _set_fb_discovery_row_status(
        self,
        seed_df: pd.DataFrame,
        row_idx: Any,
        *,
        status: str,
        reason: str,
    ) -> None:
        if "FB_Status" not in seed_df.columns:
            seed_df["FB_Status"] = ""
        if "FB_Reason" not in seed_df.columns:
            seed_df["FB_Reason"] = ""
        seed_df.at[row_idx, "FB_Status"] = status
        seed_df.at[row_idx, "FB_Reason"] = reason

    def _disable_fb_discovery_for_run(self, reason: str, *, session_invalid: bool = False) -> str:
        reason_code = cell_to_str(reason) or ("session_invalid" if session_invalid else "not_authenticated")
        previous_reason = self._fb_discovery_disabled_reason
        if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
            reason_code = disable_night_fb_run_state(
                self.night_fb_run_state,
                reason_code,
                session_invalid=session_invalid,
                checkpointed=reason_code == "checkpoint",
                close_session=session_invalid,
            )
        self._fb_discovery_disabled = True
        self._fb_discovery_disabled_reason = reason_code
        self._fb_discovery_disable_logged = self._fb_discovery_disable_logged and previous_reason == reason_code
        self._fb_session_auth_checked = True
        self._fb_session_auth_reason = reason_code
        self._fb_session_authenticated = False
        if session_invalid:
            self._fb_session_invalid = True
        if not self._fb_discovery_disable_logged:
            if session_invalid:
                self.log_message.emit(
                    f"[FB Discover] Shared Facebook session became invalid (reason={reason_code}); disabling FB discovery for the remainder of the run."
                )
            else:
                self.log_message.emit(
                    f"[FB Discover] Shared Facebook session is unavailable for discovery (reason={reason_code}); disabling FB discovery for the remainder of the run."
                )
            self._fb_discovery_disable_logged = True
        return reason_code

    def _ensure_fb_discovery_session(
        self,
        fb_driver,
        *,
        force: bool = False,
    ) -> Tuple[bool, str]:
        if not (ENABLE_FACEBOOK_ENRICHMENT and fb_driver):
            return False, "no_driver"
        if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
            if self.night_fb_run_state.disabled_for_run or self.night_fb_run_state.session_invalid:
                reason = self.night_fb_run_state.disable_reason or "disabled"
                self._fb_discovery_disabled = True
                self._fb_discovery_disabled_reason = reason
                self._fb_session_invalid = bool(self.night_fb_run_state.session_invalid)
                self._fb_session_authenticated = False
                self._fb_session_auth_checked = True
                self._fb_session_auth_reason = reason
                return False, reason
        if self._fb_discovery_disabled:
            return False, self._fb_discovery_disabled_reason or "disabled"
        if self._fb_session_invalid:
            reason = self._fb_discovery_disabled_reason or "session_invalid"
            self._disable_fb_discovery_for_run(reason, session_invalid=True)
            return False, reason
        if self._fb_session_auth_checked and not force:
            if self._fb_session_authenticated:
                if not self._fb_session_warmup_complete:
                    if not self._run_fb_session_warmup(fb_driver):
                        reason = self._fb_discovery_disabled_reason or self._fb_session_auth_reason or "session_invalid"
                        return False, reason
                return True, "authenticated"
            reason = self._fb_discovery_disabled_reason or self._fb_session_auth_reason or "not_authenticated"
            self._disable_fb_discovery_for_run(reason, session_invalid=self._fb_session_invalid)
            return False, reason

        if getattr(self, "night_mode", False):
            decision = probe_night_fb_session_decision(
                fb_driver,
                visit_home=force or not self._fb_session_auth_checked,
            )
            update_night_fb_run_state(
                self.night_fb_run_state,
                decision,
                owner="cross_directory_enricher",
            )
            is_authenticated = bool(decision.authenticated and decision.usable)
            if decision.state == "session_invalid":
                reason = "session_invalid"
            elif decision.state == "authenticated_but_checkpointed":
                reason = "checkpoint"
            else:
                reason = decision.reason or decision.state or "not_authenticated"
        else:
            is_authenticated, reason = _probe_fb_session_state(
                fb_driver,
                visit_home=force or not self._fb_session_auth_checked,
            )
        self._fb_session_auth_checked = True
        self._fb_session_auth_reason = reason
        if is_authenticated:
            should_log_authenticated = not self._fb_session_authenticated
            self._fb_session_authenticated = True
            self._fb_session_invalid = False
            if should_log_authenticated:
                self.log_message.emit(
                    "[FB Discover] Shared Facebook session authenticated; discovery enabled for this run."
                )
            if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
                self.night_fb_run_state.authenticated = True
                self.night_fb_run_state.reusable = True
            if not self._fb_session_warmup_complete:
                if not self._run_fb_session_warmup(fb_driver):
                    reason = self._fb_discovery_disabled_reason or self._fb_session_auth_reason or "session_invalid"
                    return False, reason
            return True, "authenticated"
        if reason == "session_invalid":
            reason = self._disable_fb_discovery_for_run(reason, session_invalid=True)
        else:
            reason = self._disable_fb_discovery_for_run(reason)
        return False, reason

    def _run_fb_session_warmup(self, fb_driver) -> bool:
        if not (ENABLE_FACEBOOK_ENRICHMENT and fb_driver):
            return False
        if self._fb_discovery_disabled or self._fb_session_invalid or not self._fb_session_authenticated:
            return False
        if self._fb_session_warmup_complete:
            return True
        self.log_message.emit("[FB Warmup] Running Facebook session warm-up")
        try:
            fb_driver.get("https://www.facebook.com/")
            time.sleep(random.uniform(3.0, 6.0))
            try:
                fb_driver.execute_script("window.scrollBy(0, 180);")
            except Exception as exc:
                if _fb_exception_is_fatal_session(exc):
                    raise
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as exc:
            if _fb_exception_is_fatal_session(exc):
                self._disable_fb_discovery_for_run("session_invalid", session_invalid=True)
                return False
        self._fb_session_warmup_complete = True
        if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
            self.night_fb_run_state.session_warmup_complete = True
        return True

    def _handle_fb_session_failure(
        self,
        seed_df: pd.DataFrame,
        row_idx: Any,
        artist: str,
        exc: Exception,
    ) -> bool:
        if not _fb_exception_is_fatal_session(exc):
            return False
        reason = self._disable_fb_discovery_for_run("session_invalid", session_invalid=True)
        self._set_fb_discovery_row_status(
            seed_df,
            row_idx,
            status="fb_discovery_disabled",
            reason=reason,
        )
        self.log_message.emit(f"[FB Discover] Skipping discovery for '{artist}' (reason={reason}).")
        return True

    def _run_late_domain_email_backfill(self, seed_df: pd.DataFrame, total: int) -> Dict[str, int]:
        stats = {
            "rows_scanned": 0,
            "rows_eligible": 0,
            "rows_backfilled": 0,
            "rows_skipped": 0,
        }
        if seed_df is None or seed_df.empty or not self._domain_email_reuse_index:
            self.log_message.emit(
                "[Enricher] Late domain reuse backfill: "
                f"rows_scanned={stats['rows_scanned']} "
                f"rows_eligible={stats['rows_eligible']} "
                f"rows_backfilled={stats['rows_backfilled']} "
                f"rows_skipped={stats['rows_skipped']}"
            )
            return stats

        for position, row_idx in enumerate(seed_df.index, start=1):
            stats["rows_scanned"] += 1
            row = seed_df.loc[row_idx]
            if _row_has_email(row):
                stats["rows_skipped"] += 1
                continue
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                stats["rows_skipped"] += 1
                continue
            domain_norm = _clean_cell(ctx.get("spotify_domain", "")).lower()
            if not domain_norm or domain_norm not in self._domain_email_reuse_index:
                stats["rows_skipped"] += 1
                continue
            stats["rows_eligible"] += 1
            if self._maybe_apply_domain_email_reuse(seed_df, row_idx, ctx):
                stats["rows_backfilled"] += 1
            else:
                stats["rows_skipped"] += 1

        self.log_message.emit(
            "[Enricher] Late domain reuse backfill: "
            f"rows_scanned={stats['rows_scanned']} "
            f"rows_eligible={stats['rows_eligible']} "
            f"rows_backfilled={stats['rows_backfilled']} "
            f"rows_skipped={stats['rows_skipped']}"
        )
        return stats

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
        if not domain_norm:
            return False

        source_label_norm = _clean_cell(source_label).lower()
        if source_label_norm.startswith("seed directory"):
            return False

        direct_contact = normalize_email_value(email)
        current_contacts = self._collect_same_domain_profile_contacts(
            domain_norm,
            email=email,
            email_all=email_all,
        )
        existing_entry = self._domain_email_reuse_index.get(domain_norm)
        profile = self._domain_profile_index.get(domain_norm)
        best_contact, aggregate_contacts = _select_best_reusable_domain_contact(
            domain_norm,
            current_contacts,
            profile=profile,
            existing_entry=existing_entry,
        )
        if not best_contact or not aggregate_contacts:
            return False

        existing_meta = {}
        if isinstance(existing_entry, dict) and _clean_cell(existing_entry.get("email", "")) == best_contact:
            existing_meta = {
                "source_url": existing_entry.get("source_url", ""),
                "source_type": existing_entry.get("source_type", ""),
                "extract_method": existing_entry.get("extract_method", ""),
                "email_type": existing_entry.get("email_type", ""),
                "role": existing_entry.get("role", ""),
            }
        selected_meta = _merge_domain_reuse_contact_meta(
            existing_meta,
            _domain_profile_contact_meta(profile, best_contact),
        )
        if best_contact and best_contact == direct_contact:
            selected_meta = _merge_domain_reuse_contact_meta(
                selected_meta,
                {
                    "source_url": _clean_cell(source_url),
                    "source_type": _clean_cell(source_type),
                    "extract_method": _clean_cell(extract_method) or "regex",
                    "email_type": _clean_cell(email_type),
                    "role": _classify_contact_role_from_email(best_contact) or "",
                },
            )

        role = _classify_contact_role_from_email(best_contact)
        entry = {
            "email": best_contact,
            "email_all": ";".join(aggregate_contacts),
            "source_url": _clean_cell(selected_meta.get("source_url", "")),
            "source_type": _clean_cell(selected_meta.get("source_type", "")),
            "extract_method": _clean_cell(selected_meta.get("extract_method", "")) or "regex",
            "email_type": _clean_cell(selected_meta.get("email_type", "")),
        }
        if role:
            entry["role"] = role

        changed = entry != (existing_entry or {})
        self._domain_email_reuse_index[domain_norm] = entry
        return changed

    def _propagate_domain_email_to_rows(self, df: pd.DataFrame, source_row_idx, spotify_domain: str) -> int:
        domain_norm = _clean_cell(spotify_domain).lower()
        if df is None or df.empty or not domain_norm:
            return 0

        propagated = 0
        total = len(df.index)
        for position, target_row_idx in enumerate(df.index, start=1):
            if target_row_idx == source_row_idx:
                continue
            ctx = self._build_row_context(df, target_row_idx, position, total)
            if not ctx:
                continue
            if _clean_cell(ctx.get("spotify_domain", "")).lower() != domain_norm:
                continue
            if self._maybe_apply_domain_email_reuse(df, target_row_idx, ctx):
                propagated += 1
        return propagated

    def _index_domain_email_reuse_from_row(self, df: pd.DataFrame, row_idx, spotify_domain: str, source_label: str = "") -> bool:
        if df is None or row_idx not in df.index:
            return False
        email_value = _coerce_directory_value(df.at[row_idx, "Email"]) if "Email" in df.columns else ""
        email_all_value = _coerce_directory_value(df.at[row_idx, "Email_All"]) if "Email_All" in df.columns else ""
        source_url = _coerce_directory_value(df.at[row_idx, "Email_Source_URL"]) if "Email_Source_URL" in df.columns else ""
        source_type = _coerce_directory_value(df.at[row_idx, "Email_Source_Type"]) if "Email_Source_Type" in df.columns else ""
        extract_method = _coerce_directory_value(df.at[row_idx, "Email_Extract_Method"]) if "Email_Extract_Method" in df.columns else ""
        email_type = _coerce_directory_value(df.at[row_idx, "Email_Type"]) if "Email_Type" in df.columns else ""
        row_source_label = _coerce_directory_value(df.at[row_idx, "Email Source"]) if "Email Source" in df.columns else ""
        artist_name = _coerce_directory_value(df.at[row_idx, "Artist Name"]) if "Artist Name" in df.columns else ""
        effective_source_label = source_label or row_source_label
        row_contacts = self._collect_same_domain_profile_contacts(
            _clean_cell(spotify_domain).lower(),
            email=email_value,
            email_all=email_all_value,
        )
        if not row_contacts and not self._domain_profile_index.get(_clean_cell(spotify_domain).lower()):
            return False
        self._record_domain_profile_from_row(
            spotify_domain,
            artist=artist_name,
            email=email_value,
            email_all=email_all_value,
            source_url=source_url,
            source_type=source_type,
            extract_method=extract_method,
            email_type=email_type,
            source_label=effective_source_label,
        )
        changed = self._index_domain_email_reuse(
            spotify_domain,
            email=email_value,
            email_all=email_all_value,
            source_url=source_url,
            source_type=source_type,
            extract_method=extract_method,
            email_type=email_type,
            source_label=effective_source_label,
        )
        if changed:
            self._propagate_domain_email_to_rows(df, row_idx, spotify_domain)
        return changed

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
        filtered_email_all = filter_system_telemetry_emails(
            [entry.get("email_all") or "", entry.get("email") or ""]
        )
        if not filtered_email_all:
            return False
        filtered_email = filtered_email_all[0]

        email_before = _row_email_summary_snapshot(df, row_idx)
        _set_email_with_provenance(
            (df, row_idx),
            filtered_email,
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
                filtered_email_all,
                source="domain_reuse",
                logger=self.log_message.emit,
                source_url=entry.get("source_url", ""),
                source_type=entry.get("source_type", ""),
                method=entry.get("extract_method", "regex") or "regex",
                surface="domain_reuse",
            )
        if entry.get("email_type") and not _coerce_directory_value(df.at[row_idx, "Email_Type"]):
            df.at[row_idx, "Email_Type"] = entry.get("email_type", "")
        if callable(record_email_summary_row_change):
            record_email_summary_row_change(email_before, _row_email_summary_snapshot(df, row_idx))
        self._record_enrichment_yield(
            row_idx,
            email_before,
            _row_email_summary_snapshot(df, row_idx),
            "domain_reuse",
        )
        self._domain_email_reuse_rows.add(row_idx)
        self._domain_email_reuse_count += 1
        return True

    def _upsert_domain_profile(self, domain_norm: str) -> Dict[str, Any]:
        profile = self._domain_profile_index.get(domain_norm)
        if profile is not None:
            profile.setdefault("org_type", "unknown")
            return profile
        profile = {
            "contacts": [],
            "artist_count": 0,
            "artists_sample": [],
            "source_types": [],
            "first_source_url": "",
            "seen_count": 0,
            "contact_counts": {},
            "org_type": "unknown",
            "_artist_keys": set(),
        }
        self._domain_profile_index[domain_norm] = profile
        return profile

    def _recompute_domain_org_type(self, domain_norm: str) -> str:
        domain_key = _clean_cell(domain_norm).lower()
        if not domain_key:
            return "unknown"
        profile = self._domain_profile_index.get(domain_key)
        if profile is None:
            return "unknown"
        org_type = _infer_domain_org_type(domain_key, profile)
        profile["org_type"] = org_type
        return org_type

    def _collect_same_domain_profile_contacts(
        self,
        domain_norm: str,
        *,
        email: str = "",
        email_all: str = "",
    ) -> List[str]:
        return _collect_same_domain_contacts(domain_norm, email, email_all)

    def _record_domain_profile_observation(
        self,
        domain_norm: str,
        *,
        artist: str = "",
        contacts: Optional[List[str]] = None,
        observed_contact: str = "",
        source_type: str = "",
        source_url: str = "",
        extract_method: str = "",
        email_type: str = "",
    ) -> bool:
        domain_norm = _clean_cell(domain_norm).lower()
        contact_list = list(contacts or [])
        if not domain_norm or not contact_list:
            return False

        observed_contact_norm = normalize_email_value(observed_contact)
        profile = self._upsert_domain_profile(domain_norm)
        profile["seen_count"] = int(profile.get("seen_count", 0) or 0) + 1

        stored_contacts = profile.setdefault("contacts", [])
        contact_counts = profile.setdefault("contact_counts", {})
        contact_meta = profile.setdefault(DOMAIN_PROFILE_CONTACT_META_KEY, {})
        for contact in contact_list:
            if contact not in stored_contacts:
                stored_contacts.append(contact)
            contact_counts[contact] = int(contact_counts.get(contact, 0) or 0) + 1
            if contact == observed_contact_norm:
                contact_meta[contact] = _merge_domain_reuse_contact_meta(
                    contact_meta.get(contact),
                    {
                        "source_url": _clean_cell(source_url),
                        "source_type": _clean_cell(source_type),
                        "extract_method": _clean_cell(extract_method) or "regex",
                        "email_type": _clean_cell(email_type),
                        "role": _classify_contact_role_from_email(contact) or "",
                    },
                )

        artist_name = _clean_cell(artist)
        artist_key = normalise_artist_name(artist_name)
        if artist_key:
            artist_keys = profile.setdefault("_artist_keys", set())
            if artist_key not in artist_keys:
                artist_keys.add(artist_key)
                if len(profile["artists_sample"]) < DOMAIN_PROFILE_ARTISTS_SAMPLE_MAX:
                    profile["artists_sample"].append(artist_name)
            profile["artist_count"] = len(artist_keys)

        source_type_clean = _clean_cell(source_type)
        if source_type_clean and source_type_clean not in profile["source_types"]:
            profile["source_types"].append(source_type_clean)

        source_url_clean = _clean_cell(source_url)
        if source_url_clean and not _clean_cell(profile.get("first_source_url", "")):
            profile["first_source_url"] = source_url_clean
        self._recompute_domain_org_type(domain_norm)
        return True

    def _record_domain_profile_from_row(
        self,
        spotify_domain: str,
        *,
        artist: str = "",
        email: str = "",
        email_all: str = "",
        source_url: str = "",
        source_type: str = "",
        extract_method: str = "",
        email_type: str = "",
        source_label: str = "",
    ) -> bool:
        domain_norm = _clean_cell(spotify_domain).lower()
        if not domain_norm:
            return False
        if _clean_cell(source_label).lower().startswith("seed directory"):
            return False
        contacts = self._collect_same_domain_profile_contacts(
            domain_norm,
            email=email,
            email_all=email_all,
        )
        return self._record_domain_profile_observation(
            domain_norm,
            artist=artist,
            contacts=contacts,
            observed_contact=email,
            source_type=source_type,
            source_url=source_url,
            extract_method=extract_method,
            email_type=email_type,
        )

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

    def _build_spotify_runtime_identity(
        self,
        row: Any,
        *,
        spotify_origin: bool = False,
        signal_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if row is None or not spotify_origin:
            return {"score": 0, "tier": None, "reasons": ()}

        row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        snapshot = dict(signal_snapshot or {})
        if not snapshot:
            snapshot = self._build_row_signal_snapshot(row)

        score = 0
        reasons: List[str] = []

        def _add(points: int, reason: str) -> None:
            nonlocal score
            if points <= 0 or not reason or reason in reasons:
                return
            score += points
            reasons.append(reason)

        bandcamp_url = _coerce_directory_value(row_dict.get("Bandcamp_URL", ""))
        soundcloud_url = _clean_cell(row_dict.get("SoundCloud Link", "")) or _clean_cell(row_dict.get("SoundCloud URL", ""))
        canonical_fb_url = _clean_cell(snapshot.get("canonical_fb_url", ""))
        website_candidates = tuple(snapshot.get("website_candidates") or ())
        seed_links_by_source = snapshot.get("seed_links_by_source") or {}
        source_url_source = _clean_cell(snapshot.get("source_url_source", "")).lower()
        location = _clean_cell(row_dict.get("Location", ""))
        primary_genre = _coerce_directory_value(row_dict.get("Primary Genre", ""))
        spotify_genres = _clean_cell(row_dict.get("Spotify_Genres", ""))
        try:
            match_score = float(snapshot.get("match_score") or 0.0)
        except Exception:
            match_score = 0.0

        if bandcamp_url:
            _add(3, "bandcamp_url")
        if soundcloud_url or seed_links_by_source.get("soundcloud"):
            _add(3, "soundcloud_link")
        if canonical_fb_url:
            _add(3, "facebook_url")
        if website_candidates:
            _add(2, "website_candidate")
        if seed_links_by_source.get("lastfm") or source_url_source == "lastfm":
            _add(1, "lastfm_clue")
        if source_url_source in {"facebook", "soundcloud"}:
            _add(1, f"source_url_{source_url_source}")
        if location:
            _add(1, "location")
        if primary_genre or spotify_genres:
            _add(1, "genre")
        if match_score >= 0.7:
            _add(1, "match_score")

        if score >= 5:
            tier = 1
        elif score >= 2:
            tier = 2
        else:
            tier = 3

        return {"score": score, "tier": tier, "reasons": tuple(reasons)}

    def _note_spotify_runtime_identity(self, row_idx: Any, spotify_identity: Optional[Dict[str, Any]]) -> None:
        if row_idx in getattr(self, "_spotify_identity_tier_rows", set()):
            return
        tier = None
        if isinstance(spotify_identity, dict):
            tier = spotify_identity.get("tier")
        if tier is None:
            return
        self._spotify_identity_tier_rows.add(row_idx)
        self._spotify_identity_tier_counts[f"tier_{int(tier)}"] += 1

    def _log_spotify_discovery_summary(self, prefix: str) -> None:
        counts = getattr(self, "_spotify_identity_tier_counts", Counter()) or Counter()
        if (
            not counts
            and not getattr(self, "_spotify_low_tier_fb_skips", 0)
            and not getattr(self, "_spotify_low_tier_recovery_skips", 0)
            and not getattr(self, "_spotify_identity_pass_attempted", 0)
            and not getattr(self, "_spotify_identity_pass_enriched", 0)
            and not getattr(self, "_spotify_identity_pass_no_signal", 0)
            and not (getattr(self, "_spotify_identity_pass_promotions", Counter()) or Counter())
        ):
            return
        parts = [
            f"tier_1={int(counts.get('tier_1', 0))}",
            f"tier_2={int(counts.get('tier_2', 0))}",
            f"tier_3={int(counts.get('tier_3', 0))}",
            f"low_tier_fb_skips={int(getattr(self, '_spotify_low_tier_fb_skips', 0))}",
            f"low_tier_live_skips={int(getattr(self, '_spotify_low_tier_recovery_skips', 0))}",
        ]
        self.log_message.emit(f"{prefix} Runtime tiers: " + " ".join(parts))
        identity_parts = []
        promotion_counts = getattr(self, "_spotify_identity_pass_promotions", Counter()) or Counter()
        for surface in ("soundcloud", "bandcamp", "website", "facebook", "instagram"):
            count = int(promotion_counts.get(surface, 0))
            if count:
                identity_parts.append(f"{surface}={count}")
        promoted_summary = " ".join(identity_parts) if identity_parts else "none"
        self.log_message.emit(
            f"{prefix} [Spotify Identity Pass] attempted={int(getattr(self, '_spotify_identity_pass_attempted', 0))} "
            f"enriched={int(getattr(self, '_spotify_identity_pass_enriched', 0))} "
            f"no_signal={int(getattr(self, '_spotify_identity_pass_no_signal', 0))} "
            f"promoted: {promoted_summary}"
        )

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
        spotify_url = _clean_cell(row.get("Spotify_URL"))
        spotify_origin = bool(is_spotify_origin_row(row))
        spotify_domain = extract_domain(_clean_cell(row.get("Spotify_Website_URL", "")))
        seed_links_by_source = _extract_seed_links_by_source(row)
        signal_snapshot = self._build_row_signal_snapshot(
            row,
            spotify_domain=spotify_domain,
            seed_links_by_source=seed_links_by_source,
        )
        spotify_identity = self._build_spotify_runtime_identity(
            row,
            spotify_origin=spotify_origin,
            signal_snapshot=signal_snapshot,
        )
        self._live_context = {
            "artist": artist,
            "location": _clean_cell(row.get("Location")),
            "track": track_key,
            "genre": _coerce_directory_value(row.get("Primary Genre")) if "Primary Genre" in row else "",
            "song_title": seed_song_title,
            "spotify_url": spotify_url,
            "spotify_domain": spotify_domain,
            "spotify_id": _clean_cell(row.get("Spotify_Artist_ID")),
            "spotify_origin": spotify_origin,
            "seed_lastfm_urls": seed_links_by_source.get("lastfm", set()),
            "signal_snapshot": signal_snapshot,
            "spotify_identity": spotify_identity,
            "spotify_identity_score": spotify_identity.get("score", 0),
            "spotify_identity_tier": spotify_identity.get("tier"),
        }
        spotify_id = self._live_context.get("spotify_id", "")
        return {
            "artist": artist,
            "key": key,
            "track_key": track_key,
            "spotify_url": spotify_url,
            "spotify_domain": spotify_domain,
            "spotify_id": spotify_id,
            "spotify_origin": spotify_origin,
            "seed_links_by_source": seed_links_by_source,
            "had_email_from_seed": had_email_from_seed,
            "position": position,
            "total": total,
            "signal_snapshot": signal_snapshot,
            "spotify_identity": spotify_identity,
            "spotify_identity_score": spotify_identity.get("score", 0),
            "spotify_identity_tier": spotify_identity.get("tier"),
        }

    def _row_is_spotify_origin(self, row: Optional[Any] = None, ctx: Optional[Dict[str, Any]] = None) -> bool:
        if ctx is not None and "spotify_origin" in ctx:
            return bool(ctx.get("spotify_origin"))
        if row is not None:
            try:
                return bool(is_spotify_origin_row(row))
            except Exception:
                return False
        return False

    def _should_short_circuit_after_domain_reuse(
        self,
        df: pd.DataFrame,
        row_idx,
        ctx: Optional[Dict[str, Any]],
    ) -> bool:
        spotify_origin = False
        try:
            if df is not None and row_idx in df.index:
                spotify_origin = self._row_is_spotify_origin(df.loc[row_idx], ctx)
            else:
                spotify_origin = self._row_is_spotify_origin(None, ctx)
        except Exception:
            spotify_origin = self._row_is_spotify_origin(None, ctx)
        if row_idx in self._domain_email_reuse_rows:
            return not spotify_origin
        if self._maybe_apply_domain_email_reuse(df, row_idx, ctx):
            return not spotify_origin
        return False

    def _spotify_identity_surface_snapshot(self, row: Any) -> Dict[str, Any]:
        row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        socials, websites, _, link_hubs = _extract_directory_fields(row_dict)
        identity_links = set(socials) | set(websites)
        return {
            "has_bandcamp": bool(_coerce_directory_value(row_dict.get("Bandcamp_URL", ""))),
            "has_soundcloud": bool(_coerce_directory_value(row_dict.get("SoundCloud Link", ""))),
            "has_facebook": bool(_get_canonical_fb_url(row_dict)),
            "has_website": bool(_collect_website_enrich_candidate_urls(row_dict)),
            "has_instagram": bool(_get_canonical_instagram_url(row_dict)),
            "identity_link_count": len(identity_links),
            "link_hubs": set(link_hubs),
        }

    def _spotify_snapshot_gained_identity_surface(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
    ) -> bool:
        return bool(
            (after.get("has_bandcamp") and not before.get("has_bandcamp"))
            or (after.get("has_soundcloud") and not before.get("has_soundcloud"))
            or (after.get("has_facebook") and not before.get("has_facebook"))
            or int(after.get("identity_link_count", 0) or 0) > int(before.get("identity_link_count", 0) or 0)
        )

    def _ensure_row_enrichment_state_platforms(self, *platforms: str) -> None:
        state = getattr(self, "_row_enrichment_state", None)
        if not isinstance(state, dict):
            state = {}
            self._row_enrichment_state = state
        for platform in platforms:
            if platform and platform not in state:
                state[platform] = "pending"
        if not hasattr(self, "_sc_blocked_for_row"):
            self._sc_blocked_for_row = False
        if not isinstance(getattr(self, "_last_bc_row_stats", None), dict):
            self._last_bc_row_stats = {}

    def _refresh_spotify_runtime_context(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        ctx: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        row = seed_df.loc[row_idx]
        working_ctx = ctx if isinstance(ctx, dict) else {}
        spotify_domain = _clean_cell(working_ctx.get("spotify_domain", "")) or extract_domain(
            _clean_cell(row.get("Spotify_Website_URL", ""))
        )
        seed_links_by_source = _extract_seed_links_by_source(row)
        signal_snapshot = self._build_row_signal_snapshot(
            row,
            spotify_domain=spotify_domain,
            seed_links_by_source=seed_links_by_source,
        )
        spotify_identity = self._build_spotify_runtime_identity(
            row,
            spotify_origin=self._row_is_spotify_origin(row, working_ctx),
            signal_snapshot=signal_snapshot,
        )
        refreshed = {
            "spotify_domain": spotify_domain,
            "seed_links_by_source": seed_links_by_source,
            "seed_lastfm_urls": seed_links_by_source.get("lastfm", set()),
            "signal_snapshot": signal_snapshot,
            "spotify_identity": spotify_identity,
            "spotify_identity_score": spotify_identity.get("score", 0),
            "spotify_identity_tier": spotify_identity.get("tier"),
        }
        if isinstance(ctx, dict):
            ctx.update(refreshed)
        live_ctx = getattr(self, "_live_context", None)
        if isinstance(live_ctx, dict) and getattr(self, "_live_row_idx", None) == row_idx:
            live_ctx.update(refreshed)
        return spotify_identity

    def _run_spotify_instagram_identity_recovery(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        artist: str,
    ) -> bool:
        row = seed_df.loc[row_idx]
        if _get_canonical_instagram_url(row):
            return False

        website_candidate = _spotify_instagram_identity_website_candidate(row)
        if not website_candidate:
            return False

        spotify_instagram_url = _canonicalize_instagram_profile_url(website_candidate)
        if not spotify_instagram_url:
            result = _fetch_website_html_bounded(
                self.session,
                website_candidate,
                timeout_s=WEBSITE_EMAIL_TIMEOUT,
                max_bytes=WEBSITE_EMAIL_MAX_BYTES,
            )
            if not (result.is_html and result.html):
                return False
            socials, _, _, _ = _extract_links_from_profile(
                result.html,
                "website",
                result.final_url or website_candidate,
            )
            instagram_candidates = sorted(
                {
                    candidate
                    for candidate in (
                        _canonicalize_instagram_profile_url(url)
                        for url in socials
                    )
                    if candidate
                }
            )
            if not instagram_candidates:
                return False
            spotify_instagram_url = instagram_candidates[0]
        self.log_message.emit(
            f"[Spotify IG Seed] candidate_upgraded reason=external_source url={spotify_instagram_url}"
        )

        before_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
        self._apply_payload(
            seed_df,
            row_idx,
            EnrichmentPayload(
                socials={spotify_instagram_url},
                source_dir="spotify_instagram_recovery",
                source_url=spotify_instagram_url,
                source_detail="Spotify Instagram Recovery",
            ),
        )
        after_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
        return before_social != after_social

    def _run_spotify_seed_instagram_identity_recovery(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        artist: str,
        spotify_id: str = "",
    ) -> bool:
        row = seed_df.loc[row_idx]
        if _get_canonical_instagram_url(row):
            return False

        instagram_candidates = _spotify_seed_instagram_candidate_urls(
            row,
            artist,
            spotify_id=spotify_id,
        )
        if not instagram_candidates:
            return False

        before_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
        accepted_reason_priority = {
            "external_source": 0,
            "external_corroboration": 1,
            "identity_validated": 2,
        }
        accepted_candidate_url = ""
        accepted_candidate_reason = ""
        accepted_candidate_rank = None

        for spotify_instagram_url in instagram_candidates:
            reason = _spotify_seed_instagram_identity_acceptance_reason(
                row,
                spotify_instagram_url,
                artist,
                spotify_id=spotify_id,
            )
            if not reason:
                continue
            rank = accepted_reason_priority.get(reason, 999)
            if accepted_candidate_rank is None or rank < accepted_candidate_rank:
                accepted_candidate_url = spotify_instagram_url
                accepted_candidate_reason = reason
                accepted_candidate_rank = rank

        if accepted_candidate_reason:
            spotify_instagram_url = accepted_candidate_url
            reason = accepted_candidate_reason
            admitted, validation_result = _spotify_seed_instagram_admission_profile_validation(
                self.session,
                row,
                spotify_instagram_url,
                artist,
            )
            if admitted:
                self.log_message.emit(
                    f"[Spotify IG Seed] candidate_accepted reason={reason} validation={validation_result} url={spotify_instagram_url}"
                )
                self._apply_payload(
                    seed_df,
                    row_idx,
                    EnrichmentPayload(
                        socials={spotify_instagram_url},
                        source_dir="spotify_instagram_seed",
                        source_url=spotify_instagram_url,
                        source_detail="Spotify Instagram Seed",
                    ),
                )
                after_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
                return before_social != after_social
            self.log_message.emit(
                f"[Spotify IG Seed] candidate_blocked reason={reason} validation={validation_result} url={spotify_instagram_url}"
            )

        if _row_has_email(row):
            return False

        spotify_instagram_url = instagram_candidates[0]
        self.log_message.emit(f"[Spotify IG Seed] candidate_selected url={spotify_instagram_url}")
        direct_emails = _spotify_seed_instagram_probe_direct_emails(self.session, spotify_instagram_url)
        if not direct_emails:
            self.log_message.emit(
                f"[Spotify IG Seed] candidate_failed reason=no_direct_proof url={spotify_instagram_url}"
            )
            return False

        email_before = _row_email_summary_snapshot(seed_df, row_idx)
        self._apply_payload(
            seed_df,
            row_idx,
            EnrichmentPayload(
                socials={spotify_instagram_url},
                source_dir="spotify_instagram_seed",
                source_url=spotify_instagram_url,
                source_detail="Spotify Instagram Seed",
            ),
        )
        found_email = direct_emails[0]
        if not cell_to_str(seed_df.at[row_idx, "Email"]):
            seed_df.at[row_idx, "Email"] = found_email
        seed_df.at[row_idx, "Email_All"] = _merge_email_all(seed_df.at[row_idx, "Email_All"], direct_emails)
        merge_email_provenance_into_target(
            (seed_df, row_idx),
            direct_emails,
            source_url=spotify_instagram_url,
            source_type="instagram_enrich",
            method="regex",
            surface="instagram_profile",
        )
        seed_df.at[row_idx, "Email_Type"] = "ig_enrich"
        if not cell_to_str(seed_df.at[row_idx, "Email_Source_URL"]):
            seed_df.at[row_idx, "Email_Source_URL"] = spotify_instagram_url
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
        self._record_enrichment_yield(
            row_idx,
            email_before,
            _row_email_summary_snapshot(seed_df, row_idx),
            "instagram",
        )
        self.log_message.emit(
            f"[Spotify IG Seed] candidate_promoted reason=direct_email_found url={spotify_instagram_url}"
        )
        after_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
        return before_social != after_social

    def _run_spotify_live_identity_recovery(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        artist: str,
        spotify_id: str,
        recovery_snapshot: Dict[str, Any],
    ) -> bool:
        previous_guard_ctx = dict(getattr(self, "_spotify_identity_guard_ctx", {}) or {})
        self._spotify_identity_guard_ctx = {
            "active": True,
            "row_idx": row_idx,
            "artist": artist,
            "spotify_id": spotify_id,
        }
        try:
            enriched = False
            self._ensure_row_enrichment_state_platforms("bandcamp", "soundcloud", "lastfm")

            bc_payload = self._live_search_bandcamp(artist)
            if bc_payload:
                bc_applied = self._apply_payload_guarded(
                    seed_df, row_idx, bc_payload, artist, spotify_id=spotify_id
                )
                enriched |= bc_applied
                updated_snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
                if self._spotify_snapshot_gained_identity_surface(recovery_snapshot, updated_snapshot):
                    return enriched

            sc_applied = self._night_sc_attempt_row(seed_df, row_idx, artist, spotify_id=spotify_id)
            enriched |= sc_applied
            updated_snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
            if self._spotify_snapshot_gained_identity_surface(recovery_snapshot, updated_snapshot):
                return enriched

            seed_ig_applied = self._run_spotify_seed_instagram_identity_recovery(
                seed_df,
                row_idx,
                artist,
                spotify_id=spotify_id,
            )
            enriched |= seed_ig_applied
            updated_snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
            if self._spotify_snapshot_gained_identity_surface(recovery_snapshot, updated_snapshot):
                return enriched

            ig_applied = self._run_spotify_instagram_identity_recovery(seed_df, row_idx, artist)
            enriched |= ig_applied
            updated_snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
            if self._spotify_snapshot_gained_identity_surface(recovery_snapshot, updated_snapshot):
                return enriched

            lf_payload = self._live_search_lastfm(artist)
            if lf_payload:
                lf_applied = self._apply_payload_guarded(
                    seed_df, row_idx, lf_payload, artist, spotify_id=spotify_id
                )
                enriched |= lf_applied
            return enriched
        finally:
            self._spotify_identity_guard_ctx = previous_guard_ctx

    def _run_spotify_sparse_bandcamp_recovery(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        ctx: Optional[Dict[str, Any]],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        row = seed_df.loc[row_idx]
        if not (self.enable_live_search and self._row_is_spotify_origin(row, ctx)):
            return False
        attempted_rows = getattr(self, "_spotify_sparse_bandcamp_attempted_rows", None)
        if not isinstance(attempted_rows, set):
            attempted_rows = set()
            self._spotify_sparse_bandcamp_attempted_rows = attempted_rows
        if row_idx in attempted_rows:
            return False

        current_snapshot = dict(snapshot or self._spotify_identity_surface_snapshot(row))
        if current_snapshot.get("has_bandcamp"):
            return False

        attempted_rows.add(row_idx)
        artist = (
            ctx.get("artist")
            if isinstance(ctx, dict)
            else _clean_cell(row.get("Artist Name", ""))
        ) or "<unknown>"
        slug_candidates = _spotify_sparse_bandcamp_slug_candidates(artist)
        if not slug_candidates:
            return False
        if not self._increment_live_counter():
            return False

        self._ensure_row_enrichment_state_platforms("bandcamp", "soundcloud", "lastfm")
        payload = self._bc_slug_fallback(
            artist,
            _extract_seed_track_text(row),
            slug_candidates=slug_candidates,
        )
        if not payload:
            return False

        applied = self._apply_payload_guarded(
            seed_df,
            row_idx,
            payload,
            artist,
            spotify_id=ctx.get("spotify_id") if isinstance(ctx, dict) else "",
        )
        if applied:
            self._set_platform_state("bandcamp", "matched")
        return applied

    def _expand_spotify_link_hubs(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        ctx: Optional[Dict[str, Any]],
    ) -> bool:
        if MAX_LINK_HUB_HOPS_PER_ROW <= 0:
            return False
        row = seed_df.loc[row_idx]
        snapshot = self._spotify_identity_surface_snapshot(row)
        link_hubs = list(sorted(snapshot.get("link_hubs") or set()))
        if not link_hubs:
            return False

        artist = (
            ctx.get("artist")
            if isinstance(ctx, dict)
            else _clean_cell(row.get("Artist Name", ""))
        ) or "<unknown>"
        before_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
        before_external = cell_to_str(seed_df.at[row_idx, "External Links"]) if "External Links" in seed_df.columns else ""
        before_email = cell_to_str(seed_df.at[row_idx, "Email"]) if "Email" in seed_df.columns else ""

        discovered_socials: Set[str] = set()
        discovered_websites: Set[str] = set()
        discovered_emails: Set[str] = set()
        hops = 0
        for hub_url in link_hubs:
            if hops >= MAX_LINK_HUB_HOPS_PER_ROW:
                break
            hops += 1
            self.log_message.emit(f"[Spotify Discovery] Expanding link hub for '{artist}': {hub_url}")
            try:
                response = self.session.get(hub_url, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
            except Exception as exc:
                self.log_message.emit(
                    f"[Spotify Discovery] Link hub fetch failed for '{artist}' url={hub_url} err={exc}"
                )
                continue
            hub_socials, hub_websites, hub_emails, _ = _extract_links_from_profile(
                getattr(response, "text", "") or "",
                "spotify",
                hub_url,
            )
            discovered_socials |= hub_socials
            discovered_websites |= hub_websites
            discovered_emails |= hub_emails

        if not (discovered_socials or discovered_websites or discovered_emails):
            return False

        self._apply_payload(
            seed_df,
            row_idx,
            EnrichmentPayload(
                socials=discovered_socials,
                websites=discovered_websites,
                emails=discovered_emails,
                source_detail="Spotify Discovery",
            ),
        )
        after_social = cell_to_str(seed_df.at[row_idx, "Social Link"]) if "Social Link" in seed_df.columns else ""
        after_external = cell_to_str(seed_df.at[row_idx, "External Links"]) if "External Links" in seed_df.columns else ""
        after_email = cell_to_str(seed_df.at[row_idx, "Email"]) if "Email" in seed_df.columns else ""
        return (before_social, before_external, before_email) != (after_social, after_external, after_email)

    def _expand_bio_link_hubs_for_website_enrich(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        ctx: Optional[Dict[str, Any]],
    ) -> bool:
        if MAX_LINK_HUB_HOPS_PER_ROW <= 0:
            return False
        row = seed_df.loc[row_idx]
        hub_urls = _collect_website_enrich_link_hubs(row)
        if not hub_urls:
            return False

        artist = (
            ctx.get("artist")
            if isinstance(ctx, dict)
            else _clean_cell(row.get("Artist Name", ""))
        ) or "<unknown>"
        if "External Links" not in seed_df.columns:
            seed_df["External Links"] = ""
        before_external = cell_to_str(seed_df.at[row_idx, "External Links"])
        existing_candidates = _collect_website_enrich_candidate_urls(row)
        seen_domains = {
            domain
            for domain in (_website_cache_key(url) for url in existing_candidates)
            if domain
        }

        existing_links: List[str] = []
        seen_links: Set[str] = set()
        for token in _split_multi_value(before_external):
            normalised = _normalise_url(token)
            if not normalised or _is_noise_url(normalised) or normalised in seen_links:
                continue
            seen_links.add(normalised)
            existing_links.append(normalised)

        discovered_websites: List[str] = []
        hops = 0
        for hub_url in hub_urls:
            if hops >= MAX_LINK_HUB_HOPS_PER_ROW:
                break
            hops += 1
            result = _fetch_website_html_bounded(
                self.session,
                hub_url,
                timeout_s=WEBSITE_EMAIL_TIMEOUT,
                max_bytes=WEBSITE_EMAIL_MAX_BYTES,
            )
            hub_final_url = result.final_url or hub_url
            hub_ok = bool(result.is_html and result.html)
            self.log_message.emit(
                f"[Web] bio link hub fetched ok={hub_ok} artist='{artist}' url={hub_final_url}"
            )
            if not hub_ok:
                continue
            _, hub_websites, _, _ = _extract_links_from_profile(
                result.html,
                "website",
                hub_final_url,
            )
            if not hub_websites:
                continue
            candidate_row = {
                "Spotify_Website_URL": "",
                "Bandcamp_URL": "",
                "External Links": MULTI_VALUE_SEPARATOR.join(sorted(hub_websites)),
            }
            for website_url in _collect_website_enrich_candidate_urls(candidate_row):
                website_domain = _website_cache_key(website_url)
                if not website_domain or website_domain in seen_domains:
                    continue
                seen_domains.add(website_domain)
                discovered_websites.append(website_url)

        if not discovered_websites:
            return False

        merged_links = discovered_websites + existing_links
        deduped_links: List[str] = []
        seen_merged: Set[str] = set()
        for url in merged_links:
            if url in seen_merged:
                continue
            seen_merged.add(url)
            deduped_links.append(url)
        seed_df.at[row_idx, "External Links"] = MULTI_VALUE_SEPARATOR.join(deduped_links)
        return before_external != cell_to_str(seed_df.at[row_idx, "External Links"])

    def _discover_facebook_identity(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        fb_driver,
        ctx: Optional[Dict[str, Any]],
    ) -> bool:
        if not (ENABLE_FACEBOOK_ENRICHMENT and fb_driver):
            return False
        row = seed_df.loc[row_idx]
        artist = (
            ctx.get("artist")
            if isinstance(ctx, dict)
            else _clean_cell(row.get("Artist Name", ""))
        ) or "<unknown>"
        for col in ("facebook_url", "Facebook_URL", "Facebook URL"):
            if col not in seed_df.columns:
                seed_df[col] = ""
        promoted_fb = promote_facebook_url(row, set_row=False)
        promoted_norm = _canonicalize_fb_url(promoted_fb)
        if promoted_norm:
            if not cell_to_str(seed_df.at[row_idx, "facebook_url"]):
                seed_df.at[row_idx, "facebook_url"] = promoted_norm
            if "Facebook_URL" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Facebook_URL"]):
                seed_df.at[row_idx, "Facebook_URL"] = promoted_norm
            elif "Facebook URL" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "Facebook URL"]):
                seed_df.at[row_idx, "Facebook URL"] = promoted_norm

        existing_fb_url = _get_canonical_fb_url(seed_df.loc[row_idx])
        if existing_fb_url:
            return False

        row_payload = seed_df.loc[row_idx].to_dict()
        explicit_fb_entrypoints = explicit_fb_entrypoint_urls_for_row(row_payload)
        share_runtime_fallback_urls = fb_share_runtime_fallback_urls_for_row(row_payload)
        if explicit_fb_entrypoint_present_for_row(
            row_payload,
            accepted_urls=explicit_fb_entrypoints,
            share_runtime_fallback_urls=share_runtime_fallback_urls,
        ):
            self.log_message.emit(
                f"[FB Discover] Explicit FB entrypoint already present for '{artist}'; skipping bounded discovery."
            )
            return False

        if _row_has_fb_discovery_attempt_flag(seed_df.loc[row_idx]) or row_idx in getattr(self, "_fb_discovery_attempted_rows", set()):
            self.log_message.emit(
                f"[FB Discover] Skipping discovery for '{artist}' (already attempted this run)"
            )
            if "FB_Status" not in seed_df.columns:
                seed_df["FB_Status"] = ""
            seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or "no_fb_url"
            return False

        decision = self._row_allows_heavy_enricher(seed_df.loc[row_idx], ctx, "facebook")
        if not decision.allowed:
            self._log_low_confidence_skip("fb", artist, decision)
            return False
        fb_ready, fb_reason = self._ensure_fb_discovery_session(fb_driver)
        if not fb_ready:
            self._set_fb_discovery_row_status(
                seed_df,
                row_idx,
                status="fb_discovery_disabled",
                reason=fb_reason,
            )
            self.log_message.emit(
                f"[FB Discover] Skipping discovery for '{artist}' (reason={fb_reason})."
            )
            return False

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
        song_title = _extract_seed_track_text(row)
        _fb_query, extra_signal = _build_fb_discovery_query(
            artist,
            location=location,
            song_title=song_title,
            row=row,
        )
        self.log_message.emit(
            f"[FB Discover] No explicit facebook url for '{artist}'; attempting bounded discovery "
            f"(explicit FB intake outcome='{intake.outcome}' source='{source_summary}' sample='{sample}')."
        )
        if FB_DISCOVERY_ATTEMPT_FLAG_COL not in seed_df.columns:
            seed_df[FB_DISCOVERY_ATTEMPT_FLAG_COL] = ""
        seed_df.at[row_idx, FB_DISCOVERY_ATTEMPT_FLAG_COL] = "1"
        self._fb_discovery_attempted_rows.add(row_idx)
        self._record_chunk_source_attempt(
            "facebook",
            row_idx,
            seam="discovery_execution",
        )
        try:
            discovered_fb_url = _discover_facebook_url_bounded(
                fb_driver, artist, extra_signal, self.log_message.emit
            )
        except Exception as exc:
            if self._handle_fb_session_failure(seed_df, row_idx, artist, exc):
                return False
            raise
        if not discovered_fb_url:
            self.log_message.emit(f"[FB Discover] No safe candidate found for '{artist}'")
            self.log_message.emit(
                f"[FB Discover] Discovery failed for '{artist}'; row will not retry discovery this run"
            )
            if "FB_Status" not in seed_df.columns:
                seed_df["FB_Status"] = ""
            seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or "no_fb_url"
            return False

        self.log_message.emit(
            f"[FB Discover] Candidate accepted for '{artist}': {discovered_fb_url}"
        )
        for col in ("facebook_url", "Facebook_URL", "Facebook URL"):
            if col in seed_df.columns and not cell_to_str(seed_df.at[row_idx, col]):
                seed_df.at[row_idx, col] = discovered_fb_url
        self.log_message.emit(
            f"[FB Discover] Canonical facebook_url populated via discovery for '{artist}'"
        )
        return True

    def _run_spotify_discovery_pass(
        self,
        seed_df: pd.DataFrame,
        row_idx,
        ctx: Optional[Dict[str, Any]],
        fb_driver=None,
    ) -> bool:
        row = seed_df.loc[row_idx]
        if not self._row_is_spotify_origin(row, ctx):
            return False
        if row_idx in getattr(self, "_spotify_discovery_attempted_rows", set()):
            return False
        spotify_identity = {}
        if isinstance(ctx, dict):
            spotify_identity = dict(ctx.get("spotify_identity") or {})
        if not spotify_identity:
            spotify_identity = self._build_spotify_runtime_identity(
                row,
                spotify_origin=True,
                signal_snapshot=(ctx or {}).get("signal_snapshot") if isinstance(ctx, dict) else None,
            )
        self._note_spotify_runtime_identity(row_idx, spotify_identity)

        snapshot = self._spotify_identity_surface_snapshot(row)
        sparse_identity = (
            not snapshot["has_bandcamp"]
            or not snapshot["has_soundcloud"]
            or not snapshot["has_facebook"]
            or snapshot["identity_link_count"] < 2
            or bool(snapshot["link_hubs"])
        )
        if not sparse_identity:
            return False
        self._ensure_row_enrichment_state_platforms("bandcamp")
        # Spotify discovery gets one Bandcamp recovery chance even if an earlier
        # generic Bandcamp no-match marked the row as skipped.
        if (
            self.enable_live_search
            and self._row_enrichment_state.get("bandcamp") == "skipped"
            and not snapshot["has_bandcamp"]
        ):
            self._set_platform_state("bandcamp", "pending")

        self._spotify_discovery_attempted_rows.add(row_idx)
        enriched = False
        identity_pass_snapshot = None
        identity_pass_applied = spotify_identity.get("tier") == 3
        if identity_pass_applied:
            identity_pass_snapshot = dict(snapshot)
            self._spotify_identity_pass_attempted += 1
        if snapshot["link_hubs"]:
            enriched |= self._expand_spotify_link_hubs(seed_df, row_idx, ctx)
            snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
        if identity_pass_applied and self.enable_live_search:
            artist = (
                ctx.get("artist")
                if isinstance(ctx, dict)
                else _clean_cell(seed_df.at[row_idx, "Artist Name"])
            ) or "<unknown>"
            spotify_id = ctx.get("spotify_id") if isinstance(ctx, dict) else ""
            enriched |= self._run_spotify_live_identity_recovery(
                seed_df,
                row_idx,
                artist,
                spotify_id,
                snapshot,
            )
        if identity_pass_applied:
            spotify_identity = self._refresh_spotify_runtime_context(seed_df, row_idx, ctx)
            snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
            if self._spotify_snapshot_gained_identity_surface(identity_pass_snapshot or {}, snapshot):
                self._spotify_identity_pass_enriched += 1
                for surface_key, counter_key in (
                    ("has_soundcloud", "soundcloud"),
                    ("has_bandcamp", "bandcamp"),
                    ("has_website", "website"),
                    ("has_facebook", "facebook"),
                    ("has_instagram", "instagram"),
                ):
                    if snapshot.get(surface_key) and not (identity_pass_snapshot or {}).get(surface_key):
                        self._spotify_identity_pass_promotions[counter_key] += 1
            else:
                self._spotify_identity_pass_no_signal += 1
        spotify_bc_recovered = self._run_spotify_sparse_bandcamp_recovery(
            seed_df,
            row_idx,
            ctx,
            snapshot=snapshot,
        )
        enriched |= spotify_bc_recovered
        if spotify_bc_recovered:
            spotify_identity = self._refresh_spotify_runtime_context(seed_df, row_idx, ctx)
            snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
        if not snapshot["has_facebook"]:
            spotify_tier = spotify_identity.get("tier")
            low_tier_fb_eligible = (
                spotify_tier == 3
                and "location" in set(spotify_identity.get("reasons") or ())
            )
            if spotify_tier == 3 and not low_tier_fb_eligible:
                self._spotify_low_tier_fb_skips += 1
                artist = (
                    ctx.get("artist")
                    if isinstance(ctx, dict)
                    else _clean_cell(seed_df.at[row_idx, "Artist Name"])
                ) or "<unknown>"
                reasons = ",".join(spotify_identity.get("reasons") or ()) or "none"
                self.log_message.emit(
                    f"[Spotify Discovery] Skipping facebook discovery for '{artist}' "
                    f"(tier=tier_3 score={int(spotify_identity.get('score') or 0)} reasons={reasons})"
                )
            else:
                fb_enriched = self._discover_facebook_identity(seed_df, row_idx, fb_driver, ctx)
                enriched |= fb_enriched
                if fb_enriched:
                    spotify_identity = self._refresh_spotify_runtime_context(seed_df, row_idx, ctx)
        snapshot = self._spotify_identity_surface_snapshot(seed_df.loc[row_idx])
        sparse_identity = (
            not snapshot["has_bandcamp"]
            or not snapshot["has_soundcloud"]
            or not snapshot["has_facebook"]
            or snapshot["identity_link_count"] < 2
            or bool(snapshot["link_hubs"])
        )
        if identity_pass_applied:
            if self.enable_live_search and sparse_identity and spotify_identity.get("tier") == 3:
                self._spotify_low_tier_recovery_skips += 1
                artist = (
                    ctx.get("artist")
                    if isinstance(ctx, dict)
                    else _clean_cell(seed_df.at[row_idx, "Artist Name"])
                ) or "<unknown>"
                reasons = ",".join(spotify_identity.get("reasons") or ()) or "none"
                self.log_message.emit(
                    f"[Spotify Discovery] Skipping additional live recovery for '{artist}' "
                    f"(tier=tier_3 score={int(spotify_identity.get('score') or 0)} reasons={reasons})"
                )
            return enriched
        if not (self.enable_live_search and sparse_identity):
            return enriched

        artist = (
            ctx.get("artist")
            if isinstance(ctx, dict)
            else _clean_cell(seed_df.at[row_idx, "Artist Name"])
        ) or "<unknown>"
        spotify_tier = spotify_identity.get("tier")
        if spotify_tier == 3:
            self._spotify_low_tier_recovery_skips += 1
            reasons = ",".join(spotify_identity.get("reasons") or ()) or "none"
            self.log_message.emit(
                f"[Spotify Discovery] Skipping live recovery for '{artist}' "
                f"(tier=tier_3 score={int(spotify_identity.get('score') or 0)} reasons={reasons})"
            )
            return enriched
        spotify_id = ctx.get("spotify_id") if isinstance(ctx, dict) else ""
        return enriched | self._run_spotify_live_identity_recovery(
            seed_df,
            row_idx,
            artist,
            spotify_id,
            snapshot,
        )

    def _enrich_row_directories(self, seed_df, row_idx, directory_indexes, priority, ctx):
        """Directory matching for a single row. Returns True if any enrichment applied."""
        artist = ctx["artist"]
        key = ctx["key"]
        track_key = ctx["track_key"]
        spotify_id = ctx["spotify_id"]
        seed_links_by_source = ctx["seed_links_by_source"]
        position = ctx["position"]
        total = ctx["total"]
        row = seed_df.loc[row_idx]
        unearthed_soundcloud_url = _extract_unearthed_platform_url(row, "soundcloud")
        unearthed_bandcamp_url = _extract_unearthed_platform_url(row, "bandcamp")
        enriched = False
        matches_used: List[Tuple[str, Dict[str, Any]]] = []
        sources_logged: List[str] = []
        for source in priority:
            directory_index = directory_indexes.get(source)
            if not directory_index:
                continue
            if source == "soundcloud" and unearthed_soundcloud_url:
                self.log_message.emit("[SoundCloud] skipping discovery (Unearthed URL present)")
                continue
            if source == "bandcamp" and unearthed_bandcamp_url:
                self.log_message.emit("[Bandcamp] skipping discovery (Unearthed URL present)")
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

    def _fetch_musicbrainz_known_profile(
        self,
        platform: str,
        profile_url: str,
        artist_name: str,
        ctx: Dict[str, Any],
    ) -> KnownProfileFetchResult:
        """Fetch a known URL through the existing platform parser with identity validation."""
        if not self._increment_live_counter():
            return KnownProfileFetchResult(
                KNOWN_PROFILE_ERROR,
                reason="live_search_budget_exhausted",
            )
        payload = self._fetch_profile_and_build(
            profile_url,
            platform,
            identity_artist_name=artist_name,
            identity_song_title=_clean_cell(ctx.get("song_title", "")),
        )
        status = getattr(self, "_last_known_profile_status", "")
        reason = getattr(self, "_last_known_profile_reason", "")
        if payload and status != KNOWN_PROFILE_ACCEPTED:
            status = KNOWN_PROFILE_ACCEPTED
        if not status:
            status = KNOWN_PROFILE_ERROR
        return KnownProfileFetchResult(status, payload=payload, reason=reason)

    def _enrich_row_musicbrainz_relationships(self, seed_df, row_idx, ctx) -> bool:
        if not musicbrainz_relationship_bridge_enabled():
            return False
        row = seed_df.loc[row_idx]
        if not self._row_is_spotify_origin(row, ctx):
            return False
        plan = build_relationship_bridge_plan(
            row,
            normalize_name=normalise_artist_name,
            canonicalize_bandcamp=_canonicalise_musicbrainz_bandcamp_url,
            canonicalize_soundcloud=_canonicalise_musicbrainz_soundcloud_url,
            valid_bandcamp=_is_valid_unearthed_bandcamp_url,
            valid_soundcloud=lambda value: bool(_canonicalise_musicbrainz_soundcloud_url(value)),
        )
        if not plan.eligible:
            self.log_message.emit(
                f"[MusicBrainz Bridge] skipped artist={ctx['artist']!r} reason={plan.reason}"
            )
            return False

        enriched = False
        platform_candidates = (
            ("bandcamp", plan.bandcamp_urls, "Bandcamp_URL"),
            ("soundcloud", plan.soundcloud_urls, "SoundCloud Link"),
        )
        for platform, candidates, target_column in platform_candidates:
            if not candidates:
                continue
            if target_column in seed_df.columns and _coerce_directory_value(seed_df.at[row_idx, target_column]):
                continue
            accepted = []
            for candidate_url in candidates:
                result = self._fetch_musicbrainz_known_profile(
                    platform,
                    candidate_url,
                    ctx["artist"],
                    ctx,
                )
                if result.status == KNOWN_PROFILE_ACCEPTED and result.payload:
                    accepted.append(result.payload)
                elif result.status == KNOWN_PROFILE_CHALLENGE_UNAVAILABLE:
                    self.log_message.emit(
                        f"[MusicBrainz Bridge] {platform} candidate unavailable "
                        f"artist={ctx['artist']!r} reason={result.reason or 'challenge'}"
                    )
            if len(accepted) != 1:
                if len(accepted) > 1:
                    self.log_message.emit(
                        f"[MusicBrainz Bridge] unresolved {platform} candidates "
                        f"artist={ctx['artist']!r} accepted={len(accepted)}"
                    )
                continue
            applied = self._apply_payload_guarded(
                seed_df,
                row_idx,
                accepted[0],
                ctx["artist"],
                spotify_id=ctx.get("spotify_id", ""),
            )
            if applied:
                self._set_platform_state(platform, "matched")
                enriched = True
        return enriched

    def _enrich_row_sc_live(self, seed_df, row_idx, ctx):
        """Dedicated SoundCloud live check for a single row.

        Returns (enriched: bool, skip_rest: bool).
        skip_rest=True means the SC blocked flag fired and the caller should
        skip remaining enrichment for this row.
        """
        artist = ctx["artist"]
        spotify_id = ctx["spotify_id"]
        unearthed_soundcloud_url = _extract_unearthed_platform_url(seed_df.loc[row_idx], "soundcloud")
        decision = self._row_allows_heavy_enricher(seed_df.loc[row_idx], ctx, "soundcloud")
        if not decision.allowed:
            self._log_low_confidence_skip("soundcloud", artist, decision)
            self._set_platform_state("soundcloud", "skipped")
            return (False, False)
        current_sc_link = _coerce_directory_value(seed_df.at[row_idx, "SoundCloud Link"])
        if current_sc_link:
            if unearthed_soundcloud_url:
                self.log_message.emit("[SoundCloud] skipping discovery (Unearthed URL present)")
            return (False, False)
        if not current_sc_link:
            if getattr(self, "_sc_live_enrich_disabled", False):
                reason = self._sc_live_enrich_disabled_reason or "first_challenge_page"
                self.log_message.emit(
                    f"[Enricher][SC] Live enrichment disabled (reason={reason}); skipping live SC check for '{artist}'."
                )
            else:
                if _row_is_unearthed_source(seed_df.loc[row_idx]) and not unearthed_soundcloud_url:
                    self.log_message.emit("[SoundCloud] using discovery fallback (no valid Unearthed URL)")
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

    def _run_instagram_row(
        self,
        seed_df,
        row_idx,
        ctx=None,
        *,
        bypass_shared: bool = False,
        on_domain_reuse_gate=None,
        set_platform_state_on_domain_reuse: bool = False,
    ):
        completed = False
        try:
            if bypass_shared:
                _mark_instagram_not_attempted(
                    seed_df,
                    row_idx,
                    terminal_reason="ig_opportunity_not_attempted_platform_bypass",
                )
                completed = True
                return False

            if ctx is None:
                return False

            force_unearthed_ig = _should_force_unearthed_platform_enrichment(seed_df.loc[row_idx], "instagram")
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx) and not force_unearthed_ig:
                _mark_instagram_not_attempted(
                    seed_df,
                    row_idx,
                    terminal_reason="ig_opportunity_not_attempted_domain_reuse_gate",
                )
                completed = True
                if callable(on_domain_reuse_gate):
                    on_domain_reuse_gate()
                if set_platform_state_on_domain_reuse:
                    self._set_platform_state("instagram", "skipped")
                return False

            matched = self._enrich_row_instagram_email(seed_df, row_idx, ctx)
            completed = True
            return matched
        finally:
            if completed:
                # Normalize from the row's committed IG-native terminal state, once.
                finalize_instagram_row_attribution(seed_df, row_idx)

    def _enrich_row_instagram_email(self, seed_df, row_idx, ctx):
        """Extract email from the canonical Instagram profile HTML in a single fetch."""
        row = seed_df.loc[row_idx]
        _ensure_instagram_row_attribution(seed_df, row_idx)
        seed_df.at[row_idx, IG_SURFACE_REASON_COL] = ""
        force_unearthed_ig = _should_force_unearthed_platform_enrichment(row, "instagram")
        ig_url = _get_canonical_instagram_url(row)
        if not ig_url:
            _mark_instagram_not_attempted(seed_df, row_idx, terminal_reason="no_ig_opportunity")
            self._set_platform_state("instagram", "skipped")
            return False
        if row_has_successful_source_url_provenance(
            row,
            source_type="instagram",
            source_url=ig_url,
            canonicalize_url=_canonicalize_instagram_profile_url,
        ) and not force_unearthed_ig:
            _mark_instagram_not_attempted(
                seed_df,
                row_idx,
                terminal_reason="ig_opportunity_not_attempted_same_source_url_success",
            )
            self._set_platform_state("instagram", "skipped")
            return False
        ig_truth_enabled = _instagram_truth_logging_enabled(ig_url)

        email_before = _row_email_summary_snapshot(seed_df, row_idx)
        path_tracker = InstagramExecutionPathTracker()
        path_tracker.mark_attempt("direct_profile")
        seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "attempted_ig"
        seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = "direct_profile"
        hidden_surface_attempt_keys = getattr(self, "_instagram_hidden_contact_attempt_keys", None)
        if hidden_surface_attempt_keys is None:
            hidden_surface_attempt_keys = set()
            self._instagram_hidden_contact_attempt_keys = hidden_surface_attempt_keys
        hidden_surface_attempt_key = (id(seed_df), int(row_idx))
        self._record_chunk_source_opportunity("instagram", row_idx)
        self._record_chunk_source_attempt("instagram", row_idx, seam="profile_fetch")
        self.log_message.emit(f"[IG Email] Visiting {ig_url}")
        with _instagram_profile_fetch_scope(self.session, ig_url, retain_live_page=False) as profile_fetch:
            html = profile_fetch.html
            status = profile_fetch.status
            if status != 200:
                self.log_message.emit(f"[IG Email] fetch_failed status={status}")
                seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "attempted_ig_blocked_or_unavailable"
                seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_extract_blocked_or_unavailable"
                seed_df.at[row_idx, IG_WRITE_STATE_COL] = "ig_no_email_written"
                seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "ig_blocked_or_unavailable"
                seed_df.at[row_idx, IG_SURFACE_REASON_COL] = (
                    f"profile_fetch_http_{int(status)}"
                    if status is not None
                    else "profile_fetch_http_error"
                )
                seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
                self._set_platform_state("instagram", "skipped")
                return False
            if not _instagram_profile_fetch_usable(status, html):
                html_chars = len((html or "").strip())
                self.log_message.emit(f"[IG Email] blocked_or_empty status={status} chars={html_chars}")
                seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "attempted_ig_blocked_or_unavailable"
                seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_extract_blocked_or_unavailable"
                seed_df.at[row_idx, IG_WRITE_STATE_COL] = "ig_no_email_written"
                seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "ig_blocked_or_unavailable"
                seed_df.at[row_idx, IG_SURFACE_REASON_COL] = (
                    "profile_fetch_soft_block" if _detect_soft_block(html or "") else "profile_fetch_unusable_surface"
                )
                seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
                self._set_platform_state("instagram", "skipped")
                return False

            soup = BeautifulSoup(html, "html.parser")
            all_ig_emails = _extract_instagram_direct_profile_candidate_emails(html, soup=soup)
            selected_source_url = ig_url
            selected_extract_method = "regex"
            selected_surface = "instagram_profile"
            selected_path = "direct_profile"
            onehop_target_attempted = ""
            shared_live_page = None
            shared_live_page_attempted = False
            shared_live_html = ""
            runtime_structured_payloads: List[Any] = []
            static_structured_payloads: List[Any] = []
            shared_runtime_structured_payloads_attempted = False
            shared_live_clickable_bio_link_raw_values: List[str] = []
            shared_live_clickable_bio_link_raw_values_attempted = False
            shared_live_clickable_bio_link_urls: List[str] = []
            shared_live_clickable_bio_link_urls_attempted = False
            for script_tag in soup.find_all("script"):
                payload = _load_instagram_bio_link_structured_script_payload(script_tag)
                if payload is not None:
                    static_structured_payloads.append(payload)

            def _get_shared_live_page():
                nonlocal shared_live_page, shared_live_page_attempted
                if not shared_live_page_attempted:
                    shared_live_page_attempted = True
                    path_tracker.mark_attempt("live_bridge")
                    seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
                    shared_live_page = _open_instagram_live_page_bridge(ig_url, timeout_s=HTTP_TIMEOUT)
                return shared_live_page

            def _get_shared_runtime_structured_payloads():
                nonlocal runtime_structured_payloads, shared_runtime_structured_payloads_attempted
                live_page = _get_shared_live_page()
                if live_page is None:
                    return runtime_structured_payloads
                if not shared_runtime_structured_payloads_attempted:
                    shared_runtime_structured_payloads_attempted = True
                    runtime_structured_payloads = _wait_for_instagram_runtime_bio_link_structured_payloads(
                        live_page.page,
                        timeout_s=min(HTTP_TIMEOUT, _INSTAGRAM_RENDER_READY_TIMEOUT_MS / 1000.0),
                    )
                return runtime_structured_payloads

            def _get_shared_live_clickable_bio_link_urls():
                nonlocal shared_live_clickable_bio_link_urls, shared_live_clickable_bio_link_urls_attempted
                live_page = _get_shared_live_page()
                if live_page is None:
                    return []
                _get_shared_runtime_structured_payloads()
                if not shared_live_clickable_bio_link_urls_attempted:
                    shared_live_clickable_bio_link_urls_attempted = True
                    shared_live_clickable_bio_link_raw_values = _get_shared_live_clickable_bio_link_raw_values()
                    if ig_truth_enabled:
                        _emit_instagram_truth(
                            f"[IG Truth] callsite bio_link_urls profile_url={ig_url} "
                            f"raw_control_values count={len(shared_live_clickable_bio_link_raw_values)} "
                            f"sample={_instagram_bio_link_log_sample(shared_live_clickable_bio_link_raw_values)}",
                            emit=self.log_message.emit,
                        )
                    shared_live_clickable_bio_link_urls = _collect_instagram_live_profile_clickable_bio_link_urls(
                        live_page.page,
                        profile_url=ig_url,
                        raw_control_values=shared_live_clickable_bio_link_raw_values,
                    )
                return shared_live_clickable_bio_link_urls

            def _get_shared_live_clickable_bio_link_raw_values():
                nonlocal shared_live_clickable_bio_link_raw_values, shared_live_clickable_bio_link_raw_values_attempted
                live_page = _get_shared_live_page()
                if live_page is None:
                    return []
                _get_shared_runtime_structured_payloads()
                if not shared_live_clickable_bio_link_raw_values_attempted:
                    shared_live_clickable_bio_link_raw_values_attempted = True
                    if ig_truth_enabled:
                        _emit_instagram_truth(
                            f"[IG Truth] callsite detector profile_url={ig_url} live_page=1",
                            emit=self.log_message.emit,
                        )
                    shared_live_clickable_bio_link_raw_values = _collect_instagram_live_profile_clickable_control_values(
                        live_page.page
                    )
                return shared_live_clickable_bio_link_raw_values

            def _select_rendered_live_text_surface(*surface_candidates):
                for _surface_name, surface_text in surface_candidates:
                    normalized_surface_text = cell_to_str(surface_text)
                    if normalized_surface_text:
                        return normalized_surface_text, _surface_name
                return "", ""

            def _extract_direct_emails_from_text_surfaces(*surface_candidates):
                for _surface_name, surface_text in surface_candidates:
                    normalized_surface_text = unicodedata.normalize(
                        "NFKC",
                        cell_to_str(surface_text),
                    )
                    if not normalized_surface_text:
                        continue
                    surface_emails = _extract_instagram_profile_candidate_emails(
                        normalized_surface_text
                    )
                    if surface_emails:
                        return surface_emails, _surface_name
                return [], ""

            try:
                if not all_ig_emails:
                    live_page = _get_shared_live_page()
                    if live_page is None:
                        seed_df.at[row_idx, IG_SURFACE_REASON_COL] = "bridge_not_profile_surface_or_unavailable"
                        self.log_message.emit(
                            "[IG Bridge Gate] live_bridge=0 action=skip_live_onehop reason=bridge_failed"
                        )
                        self.log_message.emit(
                            "[IG Bridge Gate] live_bridge=0 action=static_only_path"
                        )
                    else:
                        self.log_message.emit(
                            "[IG Bridge Gate] live_bridge=1 action=proceed_live_onehop"
                        )
                        path_tracker.mark_attempt("one_hop")
                        seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
                        (
                            all_ig_emails,
                            selected_source_url,
                            selected_extract_method,
                            onehop_target_attempted,
                        ) = _instagram_onehop_emails_from_surface(
                            self.session,
                            html,
                            profile_url=ig_url,
                            log=self.log_message.emit,
                            live_raw_control_values=_get_shared_live_clickable_bio_link_raw_values(),
                            live_rendered_bio_link_urls=_get_shared_live_clickable_bio_link_urls(),
                        )
                    if all_ig_emails:
                        selected_surface = "instagram_bio_link_one_hop"
                        selected_path = "one_hop"
                if (
                    not all_ig_emails
                    and hidden_surface_attempt_key not in hidden_surface_attempt_keys
                ):
                    live_page = _get_shared_live_page()
                    if live_page is not None:
                        runtime_structured_payloads = _get_shared_runtime_structured_payloads()
                        live_html = live_page.snapshot_html()
                        if _instagram_profile_fetch_usable(200, live_html):
                            shared_live_html = live_html
                            print("DEBUG IG: shared_live_html length =", len(shared_live_html or ""))
                            print("DEBUG IG: running live HTML direct extraction")
                            print("DEBUG IG: contains exact email =", "lacedupmgmt@gmail.com" in (shared_live_html or "").lower())
                            print("DEBUG IG: contains @gmail.com =", "@gmail.com" in (shared_live_html or "").lower())
                            live_soup = BeautifulSoup(shared_live_html, "html.parser")
                            all_ig_emails = _extract_instagram_direct_profile_candidate_emails(
                                shared_live_html,
                                soup=live_soup,
                            )
                            if all_ig_emails:
                                selected_path = "live_bridge"
                            if not all_ig_emails:
                                print("DEBUG IG: entering rendered text fallback")
                                print("DEBUG IG: shared_live_page is None =", shared_live_page is None)
                                try:
                                    rendered_live_text = cell_to_str(
                                        shared_live_page.page.evaluate(
                                            "() => document.body ? (document.body.innerText || '') : ''"
                                        )
                                    )
                                    print("DEBUG IG: rendered_live_text length =", len(rendered_live_text or ""))
                                    print(
                                        "DEBUG IG: rendered contains exact email =",
                                        "LacedupMGMT@gmail.com" in (rendered_live_text or ""),
                                    )
                                    print(
                                        "DEBUG IG: rendered contains @gmail.com =",
                                        "@gmail.com" in (rendered_live_text or "").lower(),
                                    )
                                    body_text_content = cell_to_str(
                                        shared_live_page.page.evaluate(
                                            "document.body ? (document.body.textContent || '') : ''"
                                        )
                                    )
                                    print("DEBUG IG: body.textContent length =", len(body_text_content or ""))
                                    main_text = cell_to_str(shared_live_page.page.evaluate(
                                        "(() => { const el = document.querySelector('main'); return el ? (el.innerText || el.textContent || '') : ''; })()"
                                    ))
                                    print("DEBUG IG: main.innerText length =", len(main_text or ""))
                                    print("DEBUG IG: main contains @gmail.com =", "@gmail.com" in (main_text or "").lower())
                                    all_text = cell_to_str(shared_live_page.page.evaluate(
                                        "(() => document.documentElement ? (document.documentElement.textContent || '') : '')()"
                                    ))
                                    print("DEBUG IG: document.textContent length =", len(all_text or ""))
                                    print("DEBUG IG: document contains @gmail.com =", "@gmail.com" in (all_text or "").lower())
                                    bio_text = cell_to_str(shared_live_page.page.evaluate(
                                        "(() => { const nodes = Array.from(document.querySelectorAll('h1, span, div')); return nodes.map(n => n.innerText || '').join(' '); })()"
                                    ))
                                    print("DEBUG IG: aggregated node text length =", len(bio_text or ""))
                                    print("DEBUG IG: aggregated contains @gmail.com =", "@gmail.com" in (bio_text or "").lower())
                                    structured_bio_text = " ".join(
                                        _collect_instagram_bio_equivalent_structured_texts(runtime_structured_payloads)
                                    )
                                    print(
                                        "DEBUG IG: structured bio text length =",
                                        len(structured_bio_text or ""),
                                    )
                                    print(
                                        "DEBUG IG: structured bio contains @gmail.com =",
                                        "@gmail.com" in (structured_bio_text or "").lower(),
                                    )
                                    rendered_live_text, rendered_live_surface = _select_rendered_live_text_surface(
                                        ("document.body.innerText", rendered_live_text),
                                        ("main.innerText_or_textContent", main_text),
                                        ("aggregated_visible_node_text", bio_text),
                                        ("document.body.textContent", body_text_content),
                                        ("document.documentElement.textContent", all_text),
                                        ("runtime_structured_bio_text", structured_bio_text),
                                    )
                                    print(
                                        "DEBUG IG: chosen rendered surface =",
                                        rendered_live_surface or "none",
                                    )
                                except Exception as exc:
                                    print(f"DEBUG IG: rendered text evaluate failed: {repr(exc)}")
                                    rendered_live_text = ""
                                if rendered_live_text:
                                    print("DEBUG IG: running rendered text direct extraction")
                                    normalized_rendered_live_text = unicodedata.normalize("NFKC", rendered_live_text)
                                    all_ig_emails = _extract_instagram_profile_candidate_emails(normalized_rendered_live_text)
                                    if all_ig_emails:
                                        selected_path = "live_bridge"
                            if not all_ig_emails:
                                runtime_payload_candidate_strings = (
                                    _collect_instagram_direct_runtime_candidate_strings(
                                        runtime_structured_payloads
                                    )
                                )
                                if runtime_payload_candidate_strings:
                                    self.log_message.emit(
                                        "[IG Email] runtime_payload_surface "
                                        f"state=non_empty count={len(runtime_payload_candidate_strings)}"
                                    )
                                    all_ig_emails, _ = _extract_direct_emails_from_text_surfaces(
                                        *[
                                            (f"runtime_payload_candidate_{idx}", candidate_text)
                                            for idx, candidate_text in enumerate(
                                                runtime_payload_candidate_strings
                                            )
                                        ]
                                    )
                                    if all_ig_emails:
                                        selected_path = "live_bridge"
                            if not all_ig_emails:
                                live_onehop_html = _extract_instagram_onehop_profile_surface_html(live_html)
                                path_tracker.mark_attempt("one_hop")
                                seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
                                (
                                    all_ig_emails,
                                    selected_source_url,
                                    selected_extract_method,
                                    onehop_target_attempted,
                                ) = _instagram_onehop_emails_from_surface(
                                    self.session,
                                    live_onehop_html,
                                    profile_url=ig_url,
                                    log=self.log_message.emit,
                                    state_label="live_surface_bio_link_urls",
                                    runtime_structured_payloads=runtime_structured_payloads,
                                    live_raw_control_values=_get_shared_live_clickable_bio_link_raw_values(),
                                    live_rendered_bio_link_urls=_get_shared_live_clickable_bio_link_urls(),
                                )
                                if all_ig_emails:
                                    selected_surface = "instagram_bio_link_one_hop"
                                    selected_path = "one_hop"
                bridge_failed = shared_live_page_attempted and shared_live_page is None
                if (
                    not all_ig_emails
                    and bridge_failed
                ):
                    static_main_text = ""
                    static_body_text = ""
                    static_document_text = ""
                    main_node = soup.find("main")
                    if main_node is not None:
                        static_main_text = main_node.get_text(" ", strip=True)
                    body_node = soup.find("body")
                    if body_node is not None:
                        static_body_text = body_node.get_text(" ", strip=True)
                    static_document_text = soup.get_text(" ", strip=True)
                    static_structured_bio_text = " ".join(
                        _collect_instagram_bio_equivalent_structured_texts(static_structured_payloads)
                    )
                    all_ig_emails, _ = _extract_direct_emails_from_text_surfaces(
                        ("static_main_text", static_main_text),
                        ("static_body_text", static_body_text),
                        ("static_document_text", static_document_text),
                        ("static_structured_bio_text", static_structured_bio_text),
                    )
                    if all_ig_emails:
                        selected_source_url = ig_url
                        selected_extract_method = "regex"
                        selected_path = "direct_profile"
                if (
                    not all_ig_emails
                    and hidden_surface_attempt_key not in hidden_surface_attempt_keys
                ):
                    live_page = _get_shared_live_page()
                    if live_page is not None:
                        candidate = next(
                            iter(_collect_instagram_hidden_contact_candidates(live_page.page)),
                            None,
                        )
                        if candidate is not None:
                            path_tracker.mark_attempt("other")
                            seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
                            hidden_surface_attempt_keys.add(hidden_surface_attempt_key)
                            if _click_instagram_hidden_contact_candidate(live_page.page, candidate):
                                rescanned_html = live_page.snapshot_html()
                                hidden_surface_emails, hidden_extract_method = (
                                    _extract_instagram_hidden_contact_emails(rescanned_html)
                                )
                                if hidden_surface_emails:
                                    all_ig_emails = hidden_surface_emails
                                    selected_source_url = ig_url
                                    selected_extract_method = hidden_extract_method
                                    selected_surface = "instagram_hidden_contact_one_action"
                                    selected_path = "other"
            finally:
                if shared_live_page is not None:
                    shared_live_page.close()
        all_ig_emails = _filter_instagram_email_candidates_for_acceptance(
            all_ig_emails,
            log=self.log_message.emit,
        )
        if not all_ig_emails:
            self.log_message.emit("[IG Email] no_email_visible")
            seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "attempted_ig_no_email_found"
            seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_no_usable_email_found"
            seed_df.at[row_idx, IG_WRITE_STATE_COL] = "ig_no_email_written"
            seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "ig_no_email_found"
            seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
            self._set_platform_state("instagram", "skipped")
            return False

        found_email = all_ig_emails[0]
        self.log_message.emit(f"[IG Email] Found email: {found_email}")
        path_tracker.mark_found(selected_path)
        seed_df.at[row_idx, IG_ATTEMPT_STATE_COL] = "attempted_ig_found_email"
        seed_df.at[row_idx, IG_EXTRACT_STATE_COL] = "ig_found_usable_email"
        self._record_chunk_source_found("instagram", row_idx, all_ig_emails)
        if not cell_to_str(seed_df.at[row_idx, "Email"]):
            seed_df.at[row_idx, "Email"] = found_email
        seed_df.at[row_idx, "Email_All"] = _merge_email_all(seed_df.at[row_idx, "Email_All"], all_ig_emails)
        merge_email_provenance_into_target(
            (seed_df, row_idx),
            all_ig_emails,
            source_url=selected_source_url,
            source_type="instagram_enrich",
            method=selected_extract_method,
            surface=selected_surface,
        )
        seed_df.at[row_idx, "Email_Type"] = "ig_enrich"
        if not cell_to_str(seed_df.at[row_idx, "Email_Source_URL"]):
            seed_df.at[row_idx, "Email_Source_URL"] = selected_source_url
        if not cell_to_str(seed_df.at[row_idx, "Email_Source_Type"]):
            seed_df.at[row_idx, "Email_Source_Type"] = "instagram_enrich"
        if not cell_to_str(seed_df.at[row_idx, "Email_Extract_Method"]):
            seed_df.at[row_idx, "Email_Extract_Method"] = selected_extract_method
        self._record_chunk_source_written(
            "instagram",
            row_idx,
            before_row=email_before,
            after_row=_row_email_summary_snapshot(seed_df, row_idx),
            found_emails=all_ig_emails,
        )
        ig_write_state = _classify_instagram_write_state(
            email_before,
            _row_email_summary_snapshot(seed_df, row_idx),
            all_ig_emails,
        )
        seed_df.at[row_idx, IG_WRITE_STATE_COL] = ig_write_state
        if ig_write_state in {"ig_wrote_email", "ig_wrote_email_all_only"}:
            path_tracker.mark_written(selected_path)
            seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "ig_email_written"
        elif ig_write_state == "ig_found_email_not_applied":
            seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "ig_found_email_not_written"
        else:
            seed_df.at[row_idx, IG_TERMINAL_REASON_COL] = "ig_indeterminate"
        seed_df.at[row_idx, IG_EXECUTION_PATH_COL] = path_tracker.terminal_path()
        try:
            from pipeline_runner import record_email_summary_row_change

            record_email_summary_row_change(
                email_before,
                _row_email_summary_snapshot(seed_df, row_idx),
            )
        except Exception:
            pass
        self._record_enrichment_yield(
            row_idx,
            email_before,
            _row_email_summary_snapshot(seed_df, row_idx),
            "instagram",
        )
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
        spotify_origin = self._row_is_spotify_origin(row, ctx)
        if _row_has_email(row) and not spotify_origin:
            self._set_platform_state("website", "skipped")
            return False

        self._expand_bio_link_hubs_for_website_enrich(seed_df, row_idx, ctx)
        row = seed_df.loc[row_idx]
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

            normalized_emails = filter_platform_support_emails(filter_system_telemetry_emails(_normalize_emails(";".join(emails_found))))
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
                source_url=selected_source_url,
                source_type="website_enrich",
                method=selected_extract_method,
            )
            record_email_summary_row_change(
                email_before,
                _row_email_summary_snapshot(seed_df, row_idx),
            )
        except Exception:
            seed_df.at[row_idx, "Email_All"] = ";".join(normalized_emails)
            merge_email_provenance_into_target(
                (seed_df, row_idx),
                normalized_emails,
                source_url=selected_source_url,
                source_type="website_enrich",
                method=selected_extract_method,
            )
        self._record_enrichment_yield(
            row_idx,
            email_before,
            _row_email_summary_snapshot(seed_df, row_idx),
            "website_enrich",
        )
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
        seed_df = ensure_fb_attribution_columns(seed_df)
        share_resolver = self._get_night_fb_share_promotion_resolver()
        _apply_fb_promotion_row(seed_df, row_idx, log_fn=self.log_message.emit, share_resolver=share_resolver)
        if _get_canonical_fb_url(seed_df.loc[row_idx]):
            if cell_to_str(seed_df.at[row_idx, FB_OPPORTUNITY_STATE_COL]) in {"", "no_fb_opportunity"}:
                seed_df.at[row_idx, FB_OPPORTUNITY_STATE_COL] = "fb_opportunity_present"
        apply_fb_opportunity_state_df(seed_df, overwrite=False)
        try:
            from pipeline_runner import finalize_fb_row_attribution
        except Exception:
            finalize_fb_row_attribution = None  # type: ignore[assignment]

        def _finalize_fb_row() -> None:
            if callable(finalize_fb_row_attribution):
                finalize_fb_row_attribution(seed_df, row_idx)

        spotify_origin = self._row_is_spotify_origin(seed_df.loc[row_idx], ctx)
        email_before = _row_email_summary_snapshot(seed_df, row_idx)
        fb_attempted = False
        fb_matched = False
        if not self._platform_attempt_allowed("facebook", artist, "Facebook Enrich"):
            fb_attempted = False
            if not cell_to_str(seed_df.at[row_idx, FB_GATE_STATE_COL]):
                seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_other_gate"
            _finalize_fb_row()
        else:
            fb_attempted = True
            if True:
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
                explicit_fb_entrypoints = explicit_fb_entrypoint_urls_for_row(row.to_dict())
                share_runtime_fallback_urls = fb_share_runtime_fallback_urls_for_row(row.to_dict())
                seen_fb_links: Set[str] = set()

                def _add_existing_fb_link(raw_url: str) -> None:
                    normalised = _normalise_fb_url(normalize_external_url(raw_url))
                    if not normalised or normalised in seen_fb_links:
                        return
                    seen_fb_links.add(normalised)
                    existing_fb_links.append(normalised)

                if fb_url_val:
                    parts = [part.strip() for part in str(fb_url_val).split(",") if part.strip()]
                    for part in parts:
                        if "facebook.com" in part.lower():
                            _add_existing_fb_link(part)
                for explicit_fb_url in explicit_fb_entrypoints:
                    _add_existing_fb_link(explicit_fb_url)
                for share_runtime_fb_url in share_runtime_fallback_urls:
                    _add_existing_fb_link(share_runtime_fb_url)
                is_unearthed = _row_is_unearthed_source(seed_df.loc[row_idx])
                if is_unearthed and not existing_fb_links:
                    self.log_message.emit(
                        "[Unearthed Path] strict explicit-only FB mode; skipping Night FB discovery"
                    )
                    seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_no_canonical_facebook_url"
                    _finalize_fb_row()
                    self._set_platform_state("facebook", "skipped")
                    return False

                decision = self._row_allows_heavy_enricher(seed_df.loc[row_idx], ctx, "facebook")
                if not decision.allowed:
                    self._log_low_confidence_skip("fb", artist, decision)
                    seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_other_gate"
                    _finalize_fb_row()
                    self._set_platform_state("facebook", "skipped")
                    return False
                fb_ready, fb_reason = self._ensure_fb_discovery_session(fb_driver)
                if not fb_ready:
                    self._set_fb_discovery_row_status(
                        seed_df,
                        row_idx,
                        status="fb_discovery_disabled",
                        reason=fb_reason,
                    )
                    self.log_message.emit(
                        f"[FB Enrich] Skipping Facebook enrichment for '{artist}' (reason={fb_reason})."
                    )
                    seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_other_gate"
                    _finalize_fb_row()
                    self._set_platform_state("facebook", "skipped")
                    return False

                can_attempt_fb_path = bool(existing_fb_links)
                discovery_row = None
                has_seeded_fb = False
                if not existing_fb_links:
                    discovery_row = seed_df.loc[row_idx]
                    seeded_fb_url, _ = ensure_canonical_facebook_url(discovery_row, set_row=False)
                    has_seeded_fb = bool(seeded_fb_url)
                    if is_unearthed and not has_seeded_fb:
                        self.log_message.emit(
                            "[FB Discovery][Skip] Unearthed row without seeded Facebook_URL"
                        )
                    else:
                        can_attempt_fb_path = True
                if can_attempt_fb_path:
                    self._record_chunk_source_opportunity("facebook", row_idx)
                if not existing_fb_links:
                    if is_unearthed and not has_seeded_fb:
                        pass
                    elif self._discover_facebook_identity(seed_df, row_idx, fb_driver, ctx):
                        discovered_fb_url = _get_canonical_fb_url(seed_df.loc[row_idx])
                        if discovered_fb_url:
                            existing_fb_links = [discovered_fb_url]
                if existing_fb_links:
                    fb_emails: List[str] = []
                    page_url_used = ""
                    fb_status_reason = ""
                    fb_session = None
                    if getattr(self, "night_mode", False) and self.night_fb_run_state is not None:
                        fb_session = getattr(self.night_fb_run_state, "session", None)

                    _st_row_label = str(artist or "")[:60]
                    self._record_chunk_source_attempt(
                        "facebook",
                        row_idx,
                        seam="page_fetch_execution",
                    )
                    seed_df.at[row_idx, FB_ATTEMPT_STATE_COL] = "attempted_fb"
                    try:
                        for candidate in existing_fb_links:
                            if row_has_successful_source_url_provenance(
                                seed_df.loc[row_idx],
                                source_type="facebook",
                                source_url=candidate,
                                canonicalize_url=canonicalize_facebook_url,
                            ):
                                self.log_message.emit(
                                    f"[FB Enrich] Skipping previously successful Facebook URL: {candidate}"
                                )
                                continue
                            if fb_session is not None:
                                fb_emails, resolved_url, fb_status_reason = _extract_fb_emails_bounded(
                                    fb_driver,
                                    candidate,
                                    log_fn=self.log_message.emit,
                                    fb_session=fb_session,
                                    _stalltrace_row_label=_st_row_label,
                                )
                            else:
                                fb_emails, resolved_url, fb_status_reason = _extract_fb_emails_bounded(
                                    fb_driver,
                                    candidate,
                                    log_fn=self.log_message.emit,
                                    _stalltrace_row_label=_st_row_label,
                                )
                            fb_emails = filter_system_telemetry_emails(fb_emails)
                            page_url_used = resolved_url or candidate
                            if fb_emails:
                                break
                            if fb_status_reason in {"login_wall", "warning_interstitial", "checkpoint"}:
                                break
                    except Exception as exc:  # pragma: no cover - defensive
                        if self._handle_fb_session_failure(seed_df, row_idx, artist, exc):
                            self._set_platform_state("facebook", "skipped")
                            return False
                        self.log_message.emit(
                            f"[FB Enrich] Error enriching row {position}/{total} ({artist}): {exc}"
                        )
                    if "FB_Status" not in seed_df.columns:
                        seed_df["FB_Status"] = ""
                    if fb_emails:
                        self._record_chunk_source_found("facebook", row_idx, fb_emails)
                        fb_status_val = str(seed_df.at[row_idx, "FB_Status"] or "")
                        if _fb_status_is_rejected(fb_status_val):
                            artist_label = cell_to_str(seed_df.at[row_idx, "Artist Name"]) or "<unknown>"
                            page_label = page_url_used or (existing_fb_links[0] if existing_fb_links else "<unknown>")
                            self.log_message.emit(
                                f"[FB Guard] Discarding emails from rejected FB page '{page_label}' for '{artist_label}' (reason={fb_status_val})"
                            )
                        else:
                            seed_df = ensure_fb_attribution_columns(seed_df)
                            try:
                                from pipeline_runner import (
                                    _classify_fb_debug_reason,
                                    _classify_fb_write_state,
                                    _fb_write_surface_snapshot,
                                )

                                fb_write_before = _fb_write_surface_snapshot(seed_df.loc[row_idx])
                            except Exception:
                                _classify_fb_debug_reason = None
                                _classify_fb_write_state = None
                                _fb_write_surface_snapshot = None
                                fb_write_before = None
                            current_email = cell_to_str(seed_df.at[row_idx, "Email"])
                            if not current_email:
                                seed_df.at[row_idx, "Email"] = fb_emails[0]
                            if page_url_used and not cell_to_str(seed_df.at[row_idx, "Social Link"]):
                                seed_df.at[row_idx, "Social Link"] = page_url_used
                            canonical_page_url_used = canonicalize_facebook_url(page_url_used)
                            if canonical_page_url_used and "facebook_url" in seed_df.columns and not cell_to_str(seed_df.at[row_idx, "facebook_url"]):
                                seed_df.at[row_idx, "facebook_url"] = canonical_page_url_used
                            if canonical_page_url_used and not cell_to_str(seed_df.at[row_idx, "Facebook_URL"]):
                                seed_df.at[row_idx, "Facebook_URL"] = canonical_page_url_used
                            seed_df.at[row_idx, "Email_All"] = _merge_email_all(
                                seed_df.at[row_idx, "Email_All"], fb_emails
                            )
                            merge_email_provenance_into_target(
                                (seed_df, row_idx),
                                fb_emails,
                                source_url=page_url_used or "",
                                source_type="facebook_enrich",
                                method="regex",
                                surface="facebook_about" if "/about" in (page_url_used or "").lower() else "facebook_main",
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
                            self._record_chunk_source_written(
                                "facebook",
                                row_idx,
                                before_row=email_before,
                                after_row=seed_df.loc[row_idx],
                                found_emails=fb_emails,
                            )
                            existing_fb_status = cell_to_str(seed_df.at[row_idx, "FB_Status"])
                            if existing_fb_status in {"pass_a_no_email_on_page", "pass_a_skipped_no_fb_url"}:
                                normalized_fb_status = "pass_a_found_email"
                            elif existing_fb_status in {"", "no_email_on_page", "no_fb_url"}:
                                normalized_fb_status = "found_email"
                            else:
                                normalized_fb_status = existing_fb_status
                            seed_df.at[row_idx, "FB_Status"] = normalized_fb_status
                            seed_df.at[row_idx, FB_ATTEMPT_STATE_COL] = "attempted_fb_found_email"
                            if (
                                _classify_fb_write_state is not None
                                and _classify_fb_debug_reason is not None
                                and _fb_write_surface_snapshot is not None
                                and fb_write_before is not None
                            ):
                                fb_write_after = _fb_write_surface_snapshot(seed_df.loc[row_idx])
                                seed_df.at[row_idx, FB_WRITE_STATE_COL] = _classify_fb_write_state(
                                    fb_write_before,
                                    fb_write_after,
                                    seed_df.at[row_idx, FB_ATTEMPT_STATE_COL],
                                )
                                seed_df.at[row_idx, FB_DEBUG_REASON_COL] = _classify_fb_debug_reason(
                                    seed_df.loc[row_idx]
                                )
                                _finalize_fb_row()
                            try:
                                from pipeline_runner import record_email_summary_row_change

                                record_email_summary_row_change(
                                    email_before,
                                    _row_email_summary_snapshot(seed_df, row_idx),
                                )
                            except Exception:
                                pass
                            self._record_enrichment_yield(
                                row_idx,
                                email_before,
                                _row_email_summary_snapshot(seed_df, row_idx),
                                "facebook_enrich",
                            )
                            self._index_domain_email_reuse_from_row(
                                seed_df,
                                row_idx,
                                _clean_cell(ctx.get("spotify_domain", "")),
                            )
                            _finalize_fb_row()
                            fb_matched = True
                    else:
                        fallback_status = fb_status_reason or "no_email_on_page"
                        seed_df.at[row_idx, "FB_Status"] = seed_df.at[row_idx, "FB_Status"] or fallback_status
                        _finalize_fb_row()
        if fb_attempted and not fb_matched:
            _finalize_fb_row()
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
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
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

    def _run_source_phased(self, seed_df, directory_indexes, priority, fb_driver, total, row_ids: Optional[Iterable[Any]] = None):
        """Run enrichment in source-phased mode: one source across all rows at a time."""
        use_scheduler = (
            os.getenv("SOURCE_DIVERSITY_SCHEDULER", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        share_resolver = self._get_night_fb_share_promotion_resolver()
        seed_df = _canonicalize_unearthed_share_rows_df(
            seed_df,
            row_ids=row_ids,
            log_fn=self.log_message.emit,
            share_resolver=share_resolver,
        )
        self._unearthed_fb_first_row_ids = self._collect_unearthed_fb_first_row_ids(seed_df, row_ids=row_ids)
        seed_df = _apply_unearthed_platform_promotion_df(seed_df, log_fn=self.log_message.emit)
        if use_scheduler:
            self.log_message.emit("[Enricher] Source diversity scheduler=ON (round-robin)")
        try:
            # Phase 0: Directory matching (fast, no network)
            self._phase_directory_matching(seed_df, directory_indexes, priority, total, row_ids=row_ids)
            if self.enable_live_search:
                self._phase_musicbrainz_relationships(seed_df, total, row_ids=row_ids)
            if use_scheduler:
                # Keep IG extraction outside the scheduler as a bounded single-page pass.
                self._phase_spotify_discovery(seed_df, total, fb_driver=fb_driver, row_ids=row_ids)
                self._phase_instagram_email(seed_df, total, row_ids=row_ids)
                self._phase_website_email(seed_df, total, row_ids=row_ids)
                self._run_interleaved_sources(seed_df, fb_driver, total, row_ids=row_ids)
                for completed_row_idx in self._selected_row_ids(seed_df, row_ids=row_ids):
                    self._checkpoint_row_complete(seed_df, completed_row_idx)
                return
            sc_deferred_rows: Dict[Any, Dict[str, Any]] = {}
            # Phase 1: Dedicated SoundCloud live check
            if self.enable_live_search:
                sc_deferred_rows = self._phase_soundcloud(seed_df, total, row_ids=row_ids)
            # Phase 2: General live lookup (BC + LF; SC mostly skipped since Phase 1 populated it)
            if self.enable_live_search:
                self._phase_live_lookup(seed_df, total, row_ids=row_ids)
            # Phase 3: Spotify seed identity fan-out before contact stages.
            self._phase_spotify_discovery(seed_df, total, fb_driver=fb_driver, row_ids=row_ids)
            # Phase 3: Instagram profile HTML email extraction (single fetch only)
            self._phase_instagram_email(seed_df, total, row_ids=row_ids)
            # Phase 4: bounded website contact crawl from canonical website field.
            self._phase_website_email(seed_df, total, row_ids=row_ids)
            if self.enable_live_search and sc_deferred_rows:
                sc_deferred_rows = self._retry_deferred_soundcloud_rows(
                    seed_df,
                    total,
                    sc_deferred_rows,
                    phase_label="post_website",
                )
            # Refresh Facebook promotion after live/directory phases so newly discovered FB links are usable.
            seed_df = _apply_fb_promotion_df(
                seed_df,
                log_fn=self.log_message.emit,
                share_resolver=share_resolver,
            )
            # Phase 5: Facebook
            if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:
                self._phase_facebook(seed_df, fb_driver, total, row_ids=row_ids)
                if self.enable_live_search and sc_deferred_rows:
                    self._retry_deferred_soundcloud_rows(
                        seed_df,
                        total,
                        sc_deferred_rows,
                        phase_label="final_window",
                    )
            for completed_row_idx in self._selected_row_ids(seed_df, row_ids=row_ids):
                self._checkpoint_row_complete(seed_df, completed_row_idx)
        finally:
            self._unearthed_fb_first_row_ids = set()

    def _run_row_linear(self, seed_df, directory_indexes, priority, fb_driver, total, row_ids: Optional[Iterable[Any]] = None):
        """Run row-linear enrichment while preserving the Unearthed explicit-FB fast path."""
        share_resolver = self._get_night_fb_share_promotion_resolver()
        seed_df = _canonicalize_unearthed_share_rows_df(
            seed_df,
            row_ids=row_ids,
            log_fn=self.log_message.emit,
            share_resolver=share_resolver,
        )
        self._unearthed_fb_first_row_ids = self._collect_unearthed_fb_first_row_ids(seed_df, row_ids=row_ids)
        seed_df = _apply_unearthed_platform_promotion_df(seed_df, log_fn=self.log_message.emit)
        selected_row_ids = self._selected_row_ids(seed_df, row_ids)
        position_by_row = {row_idx: pos for pos, row_idx in enumerate(seed_df.index, start=1)}
        try:
            for row_idx in selected_row_ids:
                position = position_by_row.get(row_idx, 0)
                ctx = self._build_row_context(seed_df, row_idx, position, total)
                if not ctx:
                    self._update_progress(position, total)
                    self._checkpoint_row_complete(seed_df, row_idx)
                    continue
                bypass_shared = self._should_bypass_unearthed_shared_enrichers(row_idx)
                if (not bypass_shared) and self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
                    def _finalize_fb_domain_reuse_gate() -> None:
                        try:
                            from pipeline_runner import finalize_fb_row_attribution

                            ensure_fb_attribution_columns(seed_df)
                            apply_fb_opportunity_state_df(seed_df, overwrite=False)
                            if not cell_to_str(seed_df.at[row_idx, FB_GATE_STATE_COL]):
                                seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_existing_usable_email"
                            finalize_fb_row_attribution(seed_df, row_idx)
                        except Exception:
                            pass

                    self._run_instagram_row(
                        seed_df,
                        row_idx,
                        ctx,
                        on_domain_reuse_gate=_finalize_fb_domain_reuse_gate,
                    )
                    self._update_progress(position, total)
                    self._checkpoint_row_complete(seed_df, row_idx)
                    continue
                self._init_row_enrichment_state()
                enriched = False
                if not bypass_shared:
                    enriched = self._enrich_row_directories(seed_df, row_idx, directory_indexes, priority, ctx)
                    if self.enable_live_search:
                        enriched |= self._enrich_row_musicbrainz_relationships(seed_df, row_idx, ctx)
                        sc_enriched, skip_rest = self._enrich_row_sc_live(seed_df, row_idx, ctx)
                        enriched |= sc_enriched
                        if skip_rest:
                            self._update_progress(position, total)
                            self._checkpoint_row_complete(seed_df, row_idx)
                            continue
                        ll_enriched, skip_rest = self._enrich_row_live_lookup(seed_df, row_idx, ctx)
                        enriched |= ll_enriched
                        if skip_rest:
                            self._update_progress(position, total)
                            self._checkpoint_row_complete(seed_df, row_idx)
                            continue
                    enriched |= self._run_spotify_discovery_pass(seed_df, row_idx, ctx, fb_driver=fb_driver)
                enriched |= self._run_instagram_row(
                    seed_df,
                    row_idx,
                    ctx,
                    bypass_shared=bypass_shared,
                )
                if not bypass_shared:
                    enriched |= self._enrich_row_website_email(seed_df, row_idx, ctx)
                if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:
                    enriched |= self._enrich_row_facebook(seed_df, row_idx, fb_driver, ctx)
                if not enriched:
                    self.log_message.emit(
                        f"[Enricher] Row {position}/{total}: no enrichment for {ctx['artist']!r}."
                    )
                self._update_progress(position, total)
                self._checkpoint_row_complete(seed_df, row_idx)
            self._log_spotify_discovery_summary("[Enricher][Spotify Discovery]")
        finally:
            self._unearthed_fb_first_row_ids = set()

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

    def _collect_unearthed_fb_first_row_ids(self, seed_df: pd.DataFrame, row_ids: Optional[Iterable[Any]] = None) -> Set[Any]:
        streamlined_rows: Set[Any] = set()
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            row = seed_df.loc[row_idx]
            if not _row_is_unearthed_source(row):
                continue
            artist = cell_to_str(row.get("Artist Name", "")) or "<unknown>"
            row_payload = row.to_dict()
            canonical_url, _ = ensure_canonical_facebook_url(row_payload, set_row=False)
            canonical_present = bool(canonicalize_facebook_url(canonical_url))
            explicit_fb_entrypoints = explicit_fb_entrypoint_urls_for_row(row_payload)
            share_runtime_fallback_urls = fb_share_runtime_fallback_urls_for_row(row_payload)
            share_runtime_fallback = bool(share_runtime_fallback_urls)
            fb_entrypoint_present = explicit_fb_entrypoint_present_for_row(
                row_payload,
                accepted_urls=explicit_fb_entrypoints,
                share_runtime_fallback_urls=share_runtime_fallback_urls,
            )
            self.log_message.emit(
                f"[Unearthed Path][FB Readiness] artist='{artist}' row={row_idx} "
                f"canonical_present={int(canonical_present)} share_runtime_fallback={int(share_runtime_fallback)} "
                f"fb_url_present={canonical_present} fb_entrypoint_present={fb_entrypoint_present}"
            )
            if canonical_present or fb_entrypoint_present:
                streamlined_rows.add(row_idx)
                self.log_message.emit(
                    f"[Unearthed Path] activated artist='{artist}' row={row_idx}"
                )
                self.log_message.emit(
                    f"[Unearthed Path] skipping non-essential enrichers artist='{artist}' row={row_idx}"
                )
            else:
                self.log_message.emit(
                    f"[Unearthed Path] no usable FB URL, resuming standard path artist='{artist}' row={row_idx}"
                )
        return streamlined_rows

    def _should_bypass_unearthed_shared_enrichers(self, row_idx: Any, *, platform: str = "") -> bool:
        streamlined_rows = self.__dict__.get("_unearthed_fb_first_row_ids")
        if not (streamlined_rows and row_idx in streamlined_rows):
            return False
        if (platform or "").strip().lower() in {"instagram", "facebook"}:
            return False
        return True

    def _ordered_interleaved_row_ids(self, seed_df: pd.DataFrame, row_ids: Optional[Iterable[Any]] = None) -> List[Any]:
        rows = self._selected_row_ids(seed_df, row_ids)
        return sorted(rows, key=lambda row_idx: self._festival_seed_priority_tier(seed_df.loc[row_idx]))

    def _run_interleaved_sources(self, seed_df, fb_driver, total, row_ids: Optional[Iterable[Any]] = None):
        """Interleave SC, LF (live lookup), and FB across rows to avoid bursts."""

        seed_df = _apply_unearthed_platform_promotion_df(seed_df, log_fn=self.log_message.emit)
        seed_df = _apply_fb_promotion_df(
            seed_df,
            log_fn=self.log_message.emit,
            share_resolver=self._get_night_fb_share_promotion_resolver(),
        )
        rows = [
            row_idx
            for row_idx in self._ordered_interleaved_row_ids(seed_df, row_ids=row_ids)
            if not self._should_bypass_unearthed_shared_enrichers(row_idx)
        ]
        fb_rows = [
            row_idx
            for row_idx in self._ordered_interleaved_row_ids(seed_df, row_ids=row_ids)
            if not self._should_bypass_unearthed_shared_enrichers(row_idx, platform="facebook")
        ]
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

        def _lf_cooldown_ready_at(*, include_search: bool, include_profile: bool) -> Optional[float]:
            now_mono = self._lf_now()
            ready_candidates: List[float] = []
            if include_search and self._lf_endpoint_in_cooldown("search", now_mono):
                ready_candidates.append(float(self.__dict__.get("_lf_search_cooldown_until", 0.0) or 0.0))
            if include_profile and self._lf_endpoint_in_cooldown("profile", now_mono):
                ready_candidates.append(float(self.__dict__.get("_lf_profile_cooldown_until", 0.0) or 0.0))
            if not ready_candidates:
                return None
            return min(ready_candidates)

        def _lf_timed_retry(*, include_search: bool, include_profile: bool) -> Optional[TimedRetry]:
            ready_at = _lf_cooldown_ready_at(
                include_search=include_search,
                include_profile=include_profile,
            )
            if ready_at is None:
                return None
            return TimedRetry(ready_at=ready_at, max_attempts=2)

        def row_getter(rid):
            row_data = seed_df.loc[rid]
            force_unearthed_fb = _should_force_unearthed_platform_enrichment(row_data, "facebook")
            if rid not in getattr(self, "_fb_discovery_attempted_rows", set()) and not force_unearthed_fb:
                return row_data
            try:
                row_copy = row_data.copy()
            except Exception:
                row_copy = dict(row_data) if hasattr(row_data, "items") else row_data
            try:
                row_copy["__fb_discovery_attempted_this_run"] = "1"
            except Exception:
                pass
            if force_unearthed_fb:
                for email_col in ("Email", "Email_All", "Email All"):
                    try:
                        row_copy[email_col] = ""
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
                if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
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
                if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
                    return SourceResult()
                self._init_row_enrichment_state()
                if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
                    if not self._notified_limit:
                        self.log_message.emit(
                            "[Scheduler] live search limit reached; skipping remaining live lookups."
                        )
                        self._notified_limit = True
                    return SourceResult()
                search_skipped_before = int(self.__dict__.get("_lf_search_skipped_cooldown", 0) or 0)
                profile_skipped_before = int(self.__dict__.get("_lf_profile_skipped_cooldown", 0) or 0)
                ll_enriched, ll_retry_later = self._enrich_row_live_lookup(seed_df, row_idx, ctx)
                search_skipped = int(self.__dict__.get("_lf_search_skipped_cooldown", 0) or 0) > search_skipped_before
                profile_skipped = int(self.__dict__.get("_lf_profile_skipped_cooldown", 0) or 0) > profile_skipped_before
                timed_retry = None
                if not ll_enriched and (search_skipped or profile_skipped):
                    timed_retry = _lf_timed_retry(
                        include_search=search_skipped,
                        include_profile=profile_skipped,
                    )
                return SourceResult(
                    attempted=True,
                    enriched=bool(ll_enriched),
                    retry_later=bool(ll_retry_later or timed_retry is not None),
                    timed_retry=timed_retry,
                )

            sources.append(
                SourceSpec(
                    name="LF",
                    rows=rows,
                    run_row=lf_run,
                    is_available=lf_available,
                    row_getter=row_getter,
                    retry_now=self._lf_now,
                    unavailable_retry=lambda row_idx, reason, retry_count: (
                        _lf_timed_retry(include_search=True, include_profile=True)
                        if reason == "cooldown"
                        else None
                    ),
                )
            )

        if ENABLE_FACEBOOK_ENRICHMENT and fb_driver:

            def fb_available() -> Tuple[bool, Optional[str]]:
                return (True, None)

            def fb_run(row_idx: int) -> SourceResult:
                ctx = self._build_row_context(seed_df, row_idx, position_by_row[row_idx], total)
                if not ctx:
                    return SourceResult()
                force_unearthed_fb = _should_force_unearthed_platform_enrichment(seed_df.loc[row_idx], "facebook")
                if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx) and not force_unearthed_fb:
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
                    rows=fb_rows,
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

    def _phase_directory_matching(self, seed_df, directory_indexes, priority, total, row_ids: Optional[Iterable[Any]] = None):
        self.log_message.emit("[Enricher][Directory Phase] Starting...")
        enriched_count = 0
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            if self._should_bypass_unearthed_shared_enrichers(row_idx):
                self._update_progress(position, total)
                continue
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                self._update_progress(position, total)
                continue
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
                self._update_progress(position, total)
                continue
            self._init_row_enrichment_state()
            if self._enrich_row_directories(seed_df, row_idx, directory_indexes, priority, ctx):
                enriched_count += 1
            self._update_progress(position, total)
        self.log_message.emit(f"[Enricher][Directory Phase] Completed {total} rows (enriched={enriched_count})")

    def _phase_soundcloud(self, seed_df, total, row_ids: Optional[Iterable[Any]] = None):
        self.log_message.emit("[Enricher][SC Phase] Starting...")
        enriched_count = 0
        skipped_cooldown = 0
        skipped_disabled = 0
        processed_rows = 0
        cooldown_remaining_hint = 0
        deferred_rows: Dict[Any, Dict[str, Any]] = {}
        stopped_max_live = False
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            if self._should_bypass_unearthed_shared_enrichers(row_idx):
                continue
            if self.max_live_searches > 0 and self.live_search_attempts >= self.max_live_searches:
                stopped_max_live = True
                break
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            processed_rows += 1
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
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

    def _phase_musicbrainz_relationships(
        self,
        seed_df,
        total,
        row_ids: Optional[Iterable[Any]] = None,
    ) -> None:
        if not musicbrainz_relationship_bridge_enabled():
            return
        enriched_count = 0
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            if self._should_bypass_unearthed_shared_enrichers(row_idx):
                continue
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx or self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
                continue
            self._init_row_enrichment_state()
            if self._enrich_row_musicbrainz_relationships(seed_df, row_idx, ctx):
                enriched_count += 1
        self.log_message.emit(
            f"[MusicBrainz Bridge] completed rows={len(self._selected_row_ids(seed_df, row_ids))} "
            f"enriched={enriched_count}"
        )

    def _phase_spotify_discovery(self, seed_df, total, fb_driver=None, row_ids: Optional[Iterable[Any]] = None):
        self.log_message.emit("[Enricher][Spotify Discovery] Starting...")
        eligible_rows = 0
        enriched_count = 0
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            if not self._row_is_spotify_origin(seed_df.loc[row_idx], ctx):
                continue
            eligible_rows += 1
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
                continue
            if self._run_spotify_discovery_pass(seed_df, row_idx, ctx, fb_driver=fb_driver):
                enriched_count += 1
        self.log_message.emit(
            f"[Enricher][Spotify Discovery] Completed {eligible_rows} spotify rows (enriched={enriched_count})"
        )
        self._log_spotify_discovery_summary("[Enricher][Spotify Discovery]")

    def _phase_instagram_email(self, seed_df, total, row_ids: Optional[Iterable[Any]] = None):
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            if self._should_bypass_unearthed_shared_enrichers(row_idx, platform="instagram"):
                self._run_instagram_row(seed_df, row_idx, None, bypass_shared=True)
                continue
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            self._init_row_enrichment_state()
            self._run_instagram_row(
                seed_df,
                row_idx,
                ctx,
                set_platform_state_on_domain_reuse=True,
            )

    def _phase_website_email(self, seed_df, total, row_ids: Optional[Iterable[Any]] = None):
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            if self._should_bypass_unearthed_shared_enrichers(row_idx):
                continue
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
                self._set_platform_state("website", "skipped")
                continue
            self._init_row_enrichment_state()
            self._enrich_row_website_email(seed_df, row_idx, ctx)

    def _phase_live_lookup(self, seed_df, total, row_ids: Optional[Iterable[Any]] = None):
        self.log_message.emit("[Enricher][LF Phase] Starting...")
        self._reset_live_lookup_bclf_stats()
        self._live_lookup_bclf_adaptive_enabled = True
        try:
            enriched_count = 0
            processed_rows = 0
            skipped_search_cooldown = 0
            skipped_profile_cooldown = 0
            stopped_max_live = False
            for row_idx in self._selected_row_ids(seed_df, row_ids):
                position = seed_df.index.get_loc(row_idx) + 1
                if self._should_bypass_unearthed_shared_enrichers(row_idx):
                    continue
                ctx = self._build_row_context(seed_df, row_idx, position, total)
                if not ctx:
                    continue
                processed_rows += 1
                if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx):
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

    def _phase_facebook(self, seed_df, fb_driver, total, row_ids: Optional[Iterable[Any]] = None):
        self.log_message.emit("[Enricher][FB Phase] Starting...")
        enriched_count = 0
        skipped_count = 0
        processed_rows = 0
        stop_reason = ""
        for row_idx in self._selected_row_ids(seed_df, row_ids):
            position = seed_df.index.get_loc(row_idx) + 1
            if self._should_bypass_unearthed_shared_enrichers(row_idx, platform="facebook"):
                try:
                    from pipeline_runner import finalize_fb_row_attribution

                    seed_df = ensure_fb_attribution_columns(seed_df)
                    apply_fb_opportunity_state_df(seed_df, overwrite=False)
                    if not cell_to_str(seed_df.at[row_idx, FB_GATE_STATE_COL]):
                        seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_other_gate"
                    finalize_fb_row_attribution(seed_df, row_idx)
                except Exception:
                    pass
                continue
            ctx = self._build_row_context(seed_df, row_idx, position, total)
            if not ctx:
                continue
            processed_rows += 1
            force_unearthed_fb = _should_force_unearthed_platform_enrichment(seed_df.loc[row_idx], "facebook")
            if self._should_short_circuit_after_domain_reuse(seed_df, row_idx, ctx) and not force_unearthed_fb:
                self._set_platform_state("facebook", "skipped")
                try:
                    from pipeline_runner import finalize_fb_row_attribution

                    seed_df = ensure_fb_attribution_columns(seed_df)
                    apply_fb_opportunity_state_df(seed_df, overwrite=False)
                    seed_df.at[row_idx, FB_GATE_STATE_COL] = "skipped_existing_usable_email"
                    finalize_fb_row_attribution(seed_df, row_idx)
                except Exception:
                    pass
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

    def _sc_engine_surfaces_unstable(self, flags: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        if flags is None:
            try:
                flags = _SC_SHARED_ENGINE.get_run_flags()
            except Exception:
                flags = {}
        root_fetch_disabled = int((flags or {}).get("root_fetch_disabled", 0) or 0)
        if root_fetch_disabled:
            return (True, "root_fetch_disabled")
        tracks_api_blocked = int((flags or {}).get("tracks_api_blocked", 0) or 0)
        if tracks_api_blocked:
            return (True, "tracks_api_blocked")
        last_challenge_at = float(getattr(self, "_sc_last_challenge_at", 0.0) or 0.0)
        challenge_count = int(getattr(self, "_sc_html_challenge_count", 0) or 0)
        if challenge_count and last_challenge_at:
            try:
                if (time.time() - last_challenge_at) <= SC_CHALLENGE_ACTIVE_SECONDS:
                    return (True, "challenge_window_active")
            except Exception:
                pass
        return (False, "")

    def _sc_maybe_exit_rss_only(self, row_idx: Optional[int] = None) -> None:
        if not getattr(self, "_sc_rss_only_mode", False):
            return
        elapsed = time.time() - (getattr(self, "_sc_rss_only_entered_at", 0.0) or 0.0)
        rows = getattr(self, "_sc_rss_only_rows", 0)
        successes = getattr(self, "_sc_rss_successes", 0)
        exit_ready = (
            elapsed >= SC_RSS_ONLY_COOLDOWN_SECONDS
            or rows >= SC_RSS_ONLY_COOLDOWN_ROWS
            or successes >= SC_RSS_ONLY_SUCCESS_RESET
        )
        if not exit_ready:
            return
        unstable, unstable_reason = self._sc_engine_surfaces_unstable()
        if unstable:
            try:
                self.log_message.emit(
                    "[Night SC] RSS-only exit deferred (reason=%s row=%s rss_successes=%d rows_since=%d)"
                    % (
                        unstable_reason,
                        row_idx if row_idx is not None else "<unknown>",
                        int(successes),
                        int(rows),
                    )
                )
            except Exception:
                pass
            return
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
            "Lead_Source": origin,
            "Source_Directory": origin,
            "Source Directory": origin,
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
        bypass_strict_guard = False
        if STRICT_MATCHING and score < MATCH_THRESHOLD:
            guard_ctx = getattr(self, "_spotify_identity_guard_ctx", {}) or {}
            live_ctx = getattr(self, "_live_context", {}) or {}
            live_row = df.loc[row_idx] if df is not None and row_idx in getattr(df, "index", ()) else None
            source_key = _clean_cell(getattr(payload, "source_dir", "")).lower()
            source_url = _clean_cell(getattr(payload, "source_url", ""))
            seed_artist = _clean_cell(artist_name) or _clean_cell(live_ctx.get("artist", ""))
            candidate_name = _clean_cell(getattr(payload, "candidate_name", ""))
            conservative_name_match = False
            if seed_artist and candidate_name:
                conservative_name_match = normalise_artist_name(seed_artist) == normalise_artist_name(candidate_name)
                if not conservative_name_match and source_key.startswith("bandcamp"):
                    conservative_name_match = _bc_slug_has_strong_artist_name_confirmation(
                        seed_artist,
                        candidate_name,
                    )
            seed_artist_compact = re.sub(r"[^a-z0-9]+", "", normalize_name(seed_artist))
            source_identity = ""
            if source_url:
                try:
                    parsed_source_url = urllib.parse.urlparse(source_url)
                except Exception:
                    parsed_source_url = None
                host = ((parsed_source_url.netloc if parsed_source_url else "") or "").lower()
                host = host[4:] if host.startswith("www.") else host
                path_segments = [
                    urllib.parse.unquote(segment).replace("+", " ")
                    for segment in ((parsed_source_url.path if parsed_source_url else "") or "").split("/")
                    if segment
                ]
                if source_key.startswith("bandcamp") and host.endswith(".bandcamp.com"):
                    source_identity = host[: -len(".bandcamp.com")]
                elif source_key.startswith("soundcloud") and path_segments:
                    source_identity = path_segments[0]
                elif source_key.startswith("lastfm") and len(path_segments) >= 2 and path_segments[0].lower() == "music":
                    source_identity = path_segments[1]
            spotify_identity_pass_context = bool(
                guard_ctx.get("active")
                and guard_ctx.get("row_idx") == row_idx
                and self._row_is_spotify_origin(live_row, live_ctx)
                and int(live_ctx.get("spotify_identity_tier") or 0) == 3
            )
            # Hardened borderline: require higher floor and genuine identity evidence.
            borderline_score = score >= max(0.0, MATCH_THRESHOLD - 0.05)
            source_identity_compact = re.sub(r"[^a-z0-9]+", "", normalize_name(source_identity))

            # Stricter source-identity support: exact match or harmless variant only.
            # Prefix/suffix containment (e.g. blackorangemgmt vs blackorange) is no longer enough.
            def _is_harmless_slug_variant(compact_seed: str, compact_src: str) -> bool:
                if not compact_seed or not compact_src:
                    return False
                if compact_seed == compact_src:
                    return True
                longer, shorter = (
                    (compact_src, compact_seed)
                    if len(compact_src) > len(compact_seed)
                    else (compact_seed, compact_src)
                )
                if not longer.startswith(shorter):
                    return False
                extra = longer[len(shorter):]
                extra_tokens = [t for t in re.findall(r"[a-z]+", extra) if t]
                return bool(extra_tokens) and all(t in _IDENTITY_HARMLESS_SUFFIXES for t in extra_tokens)

            source_identity_support = bool(
                seed_artist_compact
                and source_identity_compact
                and _is_harmless_slug_variant(seed_artist_compact, source_identity_compact)
            )

            # Explicitly block management/corporate accounts from bypass unless name is exact.
            mgmt_corp_tokens = _IDENTITY_MANAGEMENT_TOKENS | _IDENTITY_CORPORATE_TOKENS
            source_has_mgmt_corp = any(t in source_identity_compact for t in mgmt_corp_tokens)
            cand_has_mgmt_corp = any(t in _identity_compact(candidate_name) for t in mgmt_corp_tokens)
            blocked_by_org_tokens = (source_has_mgmt_corp or cand_has_mgmt_corp) and not conservative_name_match

            bypass_strict_guard = bool(
                spotify_identity_pass_context
                and source_key.startswith(("bandcamp", "soundcloud", "lastfm"))
                and borderline_score
                and not blocked_by_org_tokens
                and (conservative_name_match or source_identity_support)
                and (_payload_actionable(payload) or source_url)
            )
            if bypass_strict_guard:
                self.log_message.emit(
                    f"[Enricher] allowing spotify identity-pass scoped recovery for '{artist_name}' "
                    f"(Spotify ID {spotify_id or '<unknown>'}) – score={score:.2f}, candidate={payload.summary() or '<none>'}"
                )
            else:
                self.log_message.emit(
                    f"[Enricher] low-confidence match skipped for '{artist_name}' (Spotify ID {spotify_id or '<unknown>'}) – "
                    f"score={score:.2f}, candidate={payload.summary() or '<none>'}"
                )
                return False
        self._update_row_match_score(df, row_idx, score)
        self._apply_payload(df, row_idx, payload)
        _promote_payload_facebook_url(df, row_idx, payload)
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
        emails_all = set(filter_platform_support_emails(filter_system_telemetry_emails([*existing_emails, *new_emails])))
        new_emails = set(filter_platform_support_emails(filter_system_telemetry_emails(list(new_emails))))
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
        # Only attach provenance for emails that actually originated from this payload.
        if new_emails:
            provenance_url = payload.source_url or ""
            provenance_type = payload.source_dir or (payload.source_detail or "cross_directory_enricher")
            merge_email_provenance_into_target(
                (df, row_idx),
                new_emails,
                source_url=provenance_url,
                source_type=provenance_type,
                method="regex",
            )
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
        # Enrichment provenance is captured in Email_Source_Type/Email_Source_URL above.
        # Lead origin fields are immutable after ingest.
        try:
            from pipeline_runner import record_email_summary_row_change

            record_email_summary_row_change(
                email_before,
                _row_email_summary_snapshot(df, row_idx),
            )
        except Exception:
            pass
        if self is not None and hasattr(self, "_record_enrichment_yield"):
            self._record_enrichment_yield(
                row_idx,
                email_before,
                _row_email_summary_snapshot(df, row_idx),
                payload.source_dir,
            )
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
            queries = build_bandcamp_queries(
                artist_name,
                song_title,
                location_hint=location_hint,
                primary_genre_hint=primary_genre_hint,
            )

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
                candidate_context = ""
                if parent_li:
                    candidate_context = parent_li.get_text(" ", strip=True)
                    name_el = parent_li.select_one(".heading") or parent_li.select_one("div.heading")
                    if name_el:
                        display_name = name_el.get_text(" ", strip=True)
                    if not display_name:
                        display_name = candidate_context
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
                    artist_name,
                    display_name,
                    profile_url,
                    song_title=song_title if query != artist_name else "",
                    location_hint=location_hint,
                    genre_hint=primary_genre_hint,
                    candidate_context=candidate_context,
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
                if best_payload and best_score >= MIN_BC_CONFIDENCE and '"' in query:
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

        spotify_guard_ctx = getattr(self, "_spotify_identity_guard_ctx", {}) or {}
        spotify_guard_active = bool(spotify_guard_ctx.get("active"))
        discover_attempted = False
        if spotify_guard_active and not search_allowed:
            discover_attempted = True
            discover_payload, _ = self._bc_discover_enrich(
                artist_name,
                song_title,
                location_hint,
                primary_genre_hint,
            )
            if discover_payload:
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
                return discover_payload

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

        if spotify_guard_active and not discover_attempted:
            discover_payload, _ = self._bc_discover_enrich(
                artist_name,
                song_title,
                location_hint,
                primary_genre_hint,
            )
            if discover_payload:
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
                return discover_payload

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

    def _bc_current_live_row(self) -> Optional[Any]:
        live_df = getattr(self, "_live_seed_df", None)
        live_row_idx = getattr(self, "_live_row_idx", None)
        if live_df is None or live_row_idx is None:
            return None
        try:
            if hasattr(live_df, "index") and live_row_idx in live_df.index:
                return live_df.loc[live_row_idx]
        except Exception:
            return None
        return None

    def _bc_should_expand_sparse_slug_fallback(self) -> bool:
        if bool((getattr(self, "_spotify_identity_guard_ctx", {}) or {}).get("active")):
            return True
        row = self._bc_current_live_row()
        if row is None or _row_is_unearthed_source(row):
            return False
        snapshot = self._spotify_identity_surface_snapshot(row)
        return (
            not snapshot.get("has_bandcamp")
            and int(snapshot.get("identity_link_count", 0) or 0) < 2
        )

    def _bc_slug_fallback(
        self,
        artist_name: str,
        song_title: str,
        slug_candidates: Optional[Iterable[str]] = None,
    ) -> Optional[EnrichmentPayload]:
        """
        Conservative fallback: test a few band subdomain guesses and verify with confidence gate.
        """
        if slug_candidates is None:
            slugs = _bc_slug_candidates(artist_name)
            if self._bc_should_expand_sparse_slug_fallback():
                recovery_slugs: List[str] = []
                seen_slugs: Set[str] = set()
                for slug in _spotify_sparse_bandcamp_slug_candidates(artist_name):
                    cleaned = _clean_cell(slug).strip().lower()
                    if not cleaned or cleaned in seen_slugs:
                        continue
                    seen_slugs.add(cleaned)
                    recovery_slugs.append(cleaned)
                    if len(recovery_slugs) >= BC_FALLBACK_MAX_SLUGS:
                        break
                if recovery_slugs:
                    slugs = recovery_slugs
        else:
            slugs = []
            seen_slugs: Set[str] = set()
            for slug in slug_candidates:
                cleaned = _clean_cell(slug).strip().lower()
                if not cleaned or cleaned in seen_slugs:
                    continue
                seen_slugs.add(cleaned)
                slugs.append(cleaned)
        if not slugs:
            return None
        sparse_slug_lookup = slug_candidates is not None
        debug_attempts = bool(os.getenv("BC_DEBUG_ATTEMPTS"))
        for slug in slugs:
            if self._bc_fallback_used >= BC_FALLBACK_MAX_PER_RUN:
                break
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
                page_artist = _bc_slug_extract_page_artist_text(html or "", fallback_text=title_text)
                artist_confirmed = _bc_slug_has_strong_artist_name_confirmation(artist_name, page_artist)
                outbound_confirmed = False
                sparse_slug_confirmed = False
                if not artist_confirmed and html:
                    outbound_confirmed = _bc_slug_has_strong_outbound_confirmation(artist_name, html, url)
                if not (artist_confirmed or outbound_confirmed) and sparse_slug_lookup:
                    artist_compact = re.sub(r"[^a-z0-9]+", "", normalize_name(artist_name))
                    slug_compact = re.sub(r"[^a-z0-9]+", "", normalize_name(slug))
                    page_artist_tokens = set(normalize_name(page_artist).split())
                    sparse_negative_tokens = _BC_SLUG_CONFIRM_DISQUALIFY_TOKENS | {
                        "collective",
                        "collectives",
                        "radio",
                        "station",
                        "venue",
                    }
                    sparse_slug_confirmed = (
                        slug_compact != artist_compact
                        and (
                            _bc_slug_identity_matches_artist(artist_name, slug)
                            or _bc_slug_identity_matches_artist(artist_name, slug_compact)
                        )
                        and _bc_slug_has_strong_artist_name_confirmation(artist_name, display_name)
                        and not bool(page_artist_tokens & sparse_negative_tokens)
                    )
                if not (artist_confirmed or outbound_confirmed or sparse_slug_confirmed):
                    if debug_attempts:
                        try:
                            self.log_message.emit(
                                "[BC Debug] slug_fallback: supplemental_failed slug='%s' confidence=%.2f page_artist='%s' url=%s"
                                % (slug, confidence, page_artist or "", url)
                            )
                        except Exception:
                            pass
                    continue
                if debug_attempts:
                    try:
                        reasons = []
                        if artist_confirmed:
                            reasons.append("artist_name")
                        if outbound_confirmed:
                            reasons.append("outbound")
                        if sparse_slug_confirmed:
                            reasons.append("sparse_slug")
                        self.log_message.emit(
                            "[BC Debug] slug_fallback: supplemental_pass slug='%s' confidence=%.2f reasons=%s page_artist='%s' url=%s"
                            % (slug, confidence, ",".join(reasons) or "unknown", page_artist or "", url)
                        )
                    except Exception:
                        pass
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
        song_title: str = "",
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
                    artist_name,
                    api_candidates,
                    location_hint,
                    genre_hint,
                    song_title=song_title,
                    track_hint=track_hint,
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
                artist_name,
                candidates,
                location_hint,
                genre_hint,
                song_title=song_title,
                track_hint=track_hint,
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
                    artist_name,
                    candidates,
                    location_hint,
                    genre_hint,
                    song_title=song_title,
                    track_hint=track_hint,
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
                    song_title=song_title,
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
                identity_score, identity_class, identity_debug = _compute_identity_match_score(
                    seed_artist=artist_name,
                    candidate_display=best_candidate.get("display_name") if best_candidate else "",
                    candidate_handle=attempt.handle or "",
                    candidate_url=attempt.profile_url or "",
                )
                payload.match_score = identity_score
                payload.candidate_name = best_candidate.get("display_name") if best_candidate else artist_name
                attempt.confidence = identity_score
                attempt.match_score = identity_score
                if os.getenv("NIGHT_SC_DEBUG"):
                    try:
                        self.log_message.emit(
                            f"[Night SC] identity debug artist={artist_name} handle={attempt.handle} "
                            f"class={identity_class} score={identity_score:.2f} debug={identity_debug}"
                        )
                    except Exception:
                        pass
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
                    identity_score, identity_class, identity_debug = _compute_identity_match_score(
                        seed_artist=artist_name,
                        candidate_display="",
                        candidate_handle=handle_for_rss or "",
                        candidate_url=attempt.profile_url or "",
                    )
                    # Explicit seed link gets small trust bonus but never automatic 1.0
                    identity_score = min(1.0, identity_score + 0.08)
                    rss_payload.match_score = identity_score
                    rss_payload.candidate_name = artist_name
                    attempt.confidence = identity_score
                    attempt.match_score = identity_score
                    if os.getenv("NIGHT_SC_DEBUG"):
                        try:
                            self.log_message.emit(
                                f"[Night SC] seed-link identity debug artist={artist_name} handle={handle_for_rss} "
                                f"class={identity_class} score={identity_score:.2f} debug={identity_debug}"
                            )
                        except Exception:
                            pass
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
            engine_unstable, engine_unstable_reason = self._sc_engine_surfaces_unstable(flags)
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
                        "[Night SC] fallback_blocked=1 reason=%s root_fetch_disabled=%d tracks_api_blocked=%d html_challenges=%d"
                        % (
                            engine_unstable_reason or "engine_unstable",
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
                    identity_score, identity_class, identity_debug = _compute_identity_match_score(
                        seed_artist=artist_name,
                        candidate_display="",
                        candidate_handle=reroute_handle or "",
                        candidate_url=attempt.profile_url or "",
                    )
                    reroute_payload.match_score = identity_score
                    reroute_payload.candidate_name = artist_name
                    attempt.confidence = identity_score
                    attempt.match_score = identity_score
                    if os.getenv("NIGHT_SC_DEBUG"):
                        try:
                            self.log_message.emit(
                                f"[Night SC] reroute identity debug artist={artist_name} handle={reroute_handle} "
                                f"class={identity_class} score={identity_score:.2f} debug={identity_debug}"
                            )
                        except Exception:
                            pass
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
                identity_score, identity_class, identity_debug = _compute_identity_match_score(
                    seed_artist=artist_name,
                    candidate_display=getattr(payload, "candidate_name", "") or "",
                    candidate_handle=attempt.handle or "",
                    candidate_url=attempt.profile_url or "",
                )
                payload.match_score = identity_score
                payload.candidate_name = getattr(payload, "candidate_name", "") or artist_name
                attempt.confidence = identity_score
                attempt.match_score = identity_score
                if os.getenv("NIGHT_SC_DEBUG"):
                    try:
                        self.log_message.emit(
                            f"[Night SC] profile identity debug artist={artist_name} handle={attempt.handle} "
                            f"class={identity_class} score={identity_score:.2f} debug={identity_debug}"
                        )
                    except Exception:
                        pass
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
            song_title=song_title,
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
        engine_unstable, engine_unstable_reason = self._sc_engine_surfaces_unstable(flags)
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
                    "[Night SC] fallback_blocked=1 reason=%s root_fetch_disabled=%d tracks_api_blocked=%d html_challenges=%d"
                    % (
                        engine_unstable_reason or "engine_unstable",
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
                identity_score, identity_class, identity_debug = _compute_identity_match_score(
                    seed_artist=artist_name,
                    candidate_display="",
                    candidate_handle=reroute_handle or "",
                    candidate_url=attempt.profile_url or "",
                )
                reroute_payload.match_score = identity_score
                reroute_payload.candidate_name = artist_name
                attempt.confidence = identity_score
                attempt.match_score = identity_score
                if os.getenv("NIGHT_SC_DEBUG"):
                    try:
                        self.log_message.emit(
                            f"[Night SC] reroute2 identity debug artist={artist_name} handle={reroute_handle} "
                            f"class={identity_class} score={identity_score:.2f} debug={identity_debug}"
                        )
                    except Exception:
                        pass
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
        self,
        profile_url: str,
        source_dir: str,
        confidence: Optional[float] = None,
        identity_artist_name: str = "",
        identity_song_title: str = "",
    ) -> Optional[EnrichmentPayload]:
        known_profile_attempt = bool(
            identity_artist_name and source_dir in {"bandcamp", "soundcloud"}
        )
        if known_profile_attempt:
            self._last_known_profile_status = KNOWN_PROFILE_ERROR
            self._last_known_profile_reason = "profile_fetch_failed"
        self.log_message.emit(f"[Enricher] Fetching {source_dir} profile: {profile_url}")
        self._last_resolved_profile_url = profile_url
        attempts = LF_SEARCH_RETRY_MAX if source_dir == "lastfm" else 2
        lf_endpoint = "profile" if source_dir == "lastfm" else None
        html = self._fetch_url(profile_url, label=f"{source_dir} profile", max_attempts=attempts, endpoint=lf_endpoint)
        fetched_ok = bool(html)
        if not fetched_ok:
            return None
        identity_candidate_name = ""
        identity_match_score = 0.0
        if identity_artist_name and source_dir == "bandcamp":
            challenge_reason = _bandcamp_challenge_reason(html)
            if challenge_reason:
                self._last_fetch_ok = False
                self._last_known_profile_status = KNOWN_PROFILE_CHALLENGE_UNAVAILABLE
                self._last_known_profile_reason = challenge_reason
                self.log_message.emit(
                    f"[MusicBrainz Bridge] Bandcamp candidate unavailable url={profile_url} "
                    f"reason={challenge_reason}"
                )
                return None
            identity_candidate_name = _bc_slug_extract_page_artist_text(html)
            identity_match_score = _bandcamp_confidence(
                identity_artist_name,
                identity_candidate_name,
                profile_url,
                song_title=identity_song_title,
            )
            if (
                identity_match_score < MIN_BC_CONFIDENCE
                or not _bc_slug_has_strong_artist_name_confirmation(
                    identity_artist_name,
                    identity_candidate_name,
                )
            ):
                self._last_known_profile_status = KNOWN_PROFILE_IDENTITY_REJECTED
                self._last_known_profile_reason = "artist_identity_contradiction"
                self.log_message.emit(
                    f"[MusicBrainz Bridge] rejected Bandcamp identity url={profile_url}"
                )
                return None
            confidence = identity_match_score
        elif identity_artist_name and source_dir == "soundcloud":
            try:
                identity_soup = BeautifulSoup(html, "html.parser")
                identity_meta = identity_soup.find("meta", attrs={"property": "og:title"})
                identity_candidate_name = _clean_cell(identity_meta.get("content", "")) if identity_meta else ""
                if not identity_candidate_name:
                    identity_title = identity_soup.find("title")
                    identity_candidate_name = identity_title.get_text(" ", strip=True) if identity_title else ""
                identity_candidate_name = re.split(r"\s+[|\u2013\u2014]\s+", identity_candidate_name, maxsplit=1)[0].strip()
            except Exception:
                identity_candidate_name = ""
            handle = _sc_handle_from_profile_url(profile_url) or ""
            identity_match_score = _sc_score_candidate(
                identity_artist_name,
                identity_candidate_name,
                handle,
                profile_url=profile_url,
                song_title=identity_song_title,
            )
            if identity_match_score < MIN_SC_CONFIDENCE:
                self._last_known_profile_status = KNOWN_PROFILE_IDENTITY_REJECTED
                self._last_known_profile_reason = "artist_identity_contradiction"
                self.log_message.emit(
                    f"[MusicBrainz Bridge] rejected SoundCloud identity url={profile_url}"
                )
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
                        match_score=identity_match_score,
                        candidate_name=identity_candidate_name,
                    )
                    self.log_message.emit(
                        f"[Enricher] Bandcamp: safe match but no actionable fields; returning url-only payload url={canonical_url}"
                        f"{_format_outcome_suffix(fetch_ok=fetch_ok_flag, actionable=False, http_status=http_status)}"
                    )
                    if known_profile_attempt:
                        self._last_known_profile_status = KNOWN_PROFILE_ACCEPTED
                        self._last_known_profile_reason = ""
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
            match_score=identity_match_score,
            candidate_name=identity_candidate_name,
        )
        if known_profile_attempt:
            self._last_known_profile_status = KNOWN_PROFILE_ACCEPTED
            self._last_known_profile_reason = ""
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
        song_title: str = "",
        track_hint: str = "",
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        artist_norm_basic = _sc_strip_basic(_sc_normalise_text(artist_name))
        for candidate in candidates:
            handle = candidate.get("handle") or ""
            display = candidate.get("display_name") or ""
            location_text = candidate.get("location") or ""
            context_text = candidate.get("context") or ""
            profile_url = candidate.get("profile_url") or ""
            score = _sc_score_candidate(
                artist_name,
                display,
                handle,
                location_hint=location_hint,
                candidate_location=location_text,
                genre_hint=genre_hint,
                candidate_context=context_text,
                profile_url=profile_url,
                song_title=song_title,
                track_hint=track_hint,
            )
            rank_score = _locale_rank_score(score, location_text, context_text)
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
    yield_tracker: Optional[EnrichmentYieldTracker] = None,
    state_source: Optional[Dict[str, Any]] = None,
    state_sink: Optional[Dict[str, Any]] = None,
    night_fb_run_state: Optional[NightFBRunState] = None,
    night_runtime_reset_interval_rows: Optional[Any] = None,
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
        yield_tracker=yield_tracker,
    )
    worker.night_mode = bool(night_mode)
    worker.night_fb_run_state = night_fb_run_state
    worker.night_runtime_reset_interval_rows = night_runtime_reset_interval_rows
    if isinstance(state_source, dict):
        worker._initial_fb_session_warmup_complete = bool(state_source.get("fb_session_warmup_complete"))
    if night_fb_run_state is not None:
        worker._initial_fb_session_warmup_complete = bool(night_fb_run_state.session_warmup_complete)

    # Bypass Qt event loop by providing simple emit stubs.
    worker.log_message = type("obj", (), {"emit": _log})
    worker.progress = type("obj", (), {"emit": lambda *args, **kwargs: None})
    worker.finished = type("obj", (), {"emit": lambda *args, **kwargs: None})

    worker._run_impl()
    if night_fb_run_state is not None:
        night_fb_run_state.session_warmup_complete = bool(getattr(worker, "_fb_session_warmup_complete", False))
    if isinstance(state_sink, dict):
        state_sink["fb_session_warmup_complete"] = bool(getattr(worker, "_fb_session_warmup_complete", False))
        state_sink["domain_profile_index"] = copy.deepcopy(getattr(worker, "_domain_profile_index", {}) or {})
        state_sink["domain_email_reuse_index"] = copy.deepcopy(getattr(worker, "_domain_email_reuse_index", {}) or {})
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
