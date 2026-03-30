"""Night-Mode-only Facebook enrichment helpers.

This module isolates Night Mode tweaks so daytime paths stay unchanged.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
import shutil
import json
import unicodedata
from pathlib import Path
import sys
import atexit
import weakref
import subprocess
import tempfile
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
import logging

from fb_email_override import should_accept_email_override
from fb_attribution import FB_ATTEMPT_STATE_COL

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from source_scheduler import (
    canonicalize_facebook_url,
    ensure_canonical_facebook_url,
    preferred_upstream_identity_hint,
)
from email_provenance import merge_email_provenance_into_target
from email_normalizer import filter_system_telemetry_emails, normalize_email_value

try:
    import facebook_enrich  # type: ignore
    from facebook_enrich import (
        is_fb_login_redirect,
        is_junk_fb_candidate_url,
        fb_reason_code_split,
        fb_is_allowed_profile_candidate_url,
        _fb_is_candidate_url_allowed,
    )  # type: ignore
except Exception:  # pragma: no cover - defensive
    facebook_enrich = None  # type: ignore

    def is_fb_login_redirect(*args, **kwargs):  # type: ignore
        return False

    def is_junk_fb_candidate_url(*args, **kwargs):  # type: ignore
        return False

    def fb_reason_code_split(url: str, existing_reason: str) -> str:  # type: ignore
        return existing_reason

    def fb_is_allowed_profile_candidate_url(url: str) -> bool:  # type: ignore
        return False

    def _fb_is_candidate_url_allowed(url: str) -> bool:  # type: ignore
        return True


LoggerFn = Optional[Union[Callable[[str], None], logging.Logger]]

_PROFILE_SESSION_SENTINELS = {"profile_session"}

EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_FB_SPLIT_PATTERN = re.compile(r"[,\s;|]+")
_FB_LOW_QUALITY_FILE_EXTENSIONS: Tuple[str, ...] = ("jpg", "jpeg", "png", "gif", "wav", "mp3", "mp4", "pdf", "zip")
_FB_LOW_QUALITY_SHORT_LOCAL_PARTS = frozenset({"to", "by", "at"})
_CONTACT_QUALITY_REJECT_LOCAL_PARTS = frozenset({"and", "with", "by", "in", "at", "tagging"})
_CONTACT_QUALITY_REJECT_DOMAIN_SUFFIXES = (".jpg", ".png", ".mp3", ".wav", ".pdf")
_FB_GENERIC_EMAIL_PROVIDER_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
    }
)
_FB_ROLE_EMAIL_PREFIXES: Tuple[str, ...] = ("info", "contact", "admin", "hello")
_FB_BOOKING_EMAIL_TOKENS: Tuple[str, ...] = ("booking", "bookings", "mgmt", "management")
_FB_REVEAL_CONTROL_TERMS: Tuple[str, ...] = (
    "contact info",
    "see more",
    "contact",
    "about",
)
FB_CLUE_FIELDS = [
    "Social Link",
    "External Links",
    "Spotify_Website_URL",
    "Spotify Website URL",
    "Facebook_URL",
    "Facebook URL",
]
_EXPLICIT_FB_INTAKE_FIELDS = (
    "Facebook_URL",
    "Facebook URL",
    "Social Link",
    "External Links",
    "Spotify_Website_URL",
    "Spotify Website URL",
    "facebook_url",
    "facebook url",
    "social link",
    "external links",
)
_EXPLICIT_FB_ALLOWED_HOSTS = ("facebook.com", "m.facebook.com", "web.facebook.com", "touch.facebook.com", "fb.com", "fb.me")
_EXPLICIT_FB_PREFILTER_PATHS = ("/r.php", "/login", "/share.php", "/l.php", "/dialog/")
_DIRECT_FB_ROW_FIELDS = ("Facebook_URL", "Facebook URL", "facebook_url", "facebook url")
_FB_HOME_SEARCH_INPUT_SELECTORS: Tuple[Tuple[str, str], ...] = (
    (By.CSS_SELECTOR, 'input[aria-label="Search Facebook"]'),
    (By.CSS_SELECTOR, 'input[placeholder="Search Facebook"]'),
    (By.CSS_SELECTOR, 'input[type="search"]'),
    (By.CSS_SELECTOR, 'input[role="combobox"]'),
    (
        By.XPATH,
        "//input[contains(@aria-label, 'Search') or contains(@placeholder, 'Search')]",
    ),
)
_FB_HOMEPAGE_JUNK_SEGMENTS = frozenset(
    {
        "about",
        "accessibility",
        "cookies",
        "help",
        "lite",
        "login",
        "meta",
        "policies",
        "privacy",
        "reg",
        "terms",
    }
)
_FB_HOMEPAGE_JUNK_TEXT_EXACT = frozenset(
    {
        "about",
        "facebook lite",
        "log in",
        "privacy",
        "sign up",
        "terms",
    }
)
_FB_HOMEPAGE_JUNK_TEXT_TOKENS = (
    "accessibility",
    "cookie",
    "cookies",
    "help center",
    "meta",
    "policy",
    "privacy",
    "sign up",
    "log in",
    "facebook lite",
)


@dataclass
class ExplicitFbIntakeDecision:
    outcome: str
    source_fields: List[str]
    accepted_urls: List[str]
    rejected_invalid: List[str]
    rejected_guard: List[str]
    promotion_expected_missing_canonical: bool
    canonical_value_present: bool
    promotion_source: str = ""
    invalid_reason: str = ""
    guard_reason: str = ""
    message: str = ""

_FB_JUNK_HOSTS = (
    "l.facebook.com",
    "developers.facebook.com",
    "about.meta.com",
    "meta.com",
)
_FB_JUNK_PATH_TOKENS = (
    "/events/",
    "/watch",
    "/reel",
    "/afad",
    "/notifications",
    "/latest/composer",
    "/business/",
)

_FB_RANK_WEIGHTS = {
    "primary_music": 70,
    "descriptor_music": 40,
    "any_music_token": 30,
    "exact_name": 20,
    "near_name": 10,
    "name_mismatch": -25,
    "profile_penalty": -15,
    "profile_penalty_music": -10,
    "page_bonus_music": 5,
    "service_only": -45,
    "service_mixed": -15,
}

# Conservative acceptance floor for FB V2 selection; env override via MIN_FB_ACCEPT_SCORE or FB_MIN_ACCEPT_SCORE.
_DEFAULT_MIN_FB_ACCEPT_SCORE = 0

# Hard reject tokens for clearly non-music institutions/services.
_FB_NON_MUSIC_DENY_TOKENS = (
    "dental",
    "dentist",
    "clinic",
    "hospital",
    "medical",
    "health",
    "doctor",
    "laboratory",
    "pharmacy",
    "institute",
    "academy",
    "university",
    "college",
    "school",
    "education",
    "training",
    "course",
    "institution",
    "government",
)

_FB_SERVICE_CATEGORY_TOKENS = (
    "product/service",
    "local service",
    "business service",
    "digital creator",
    "content creator",
    "public figure",
    "store",
    "shop",
    "boutique",
    "restaurant",
    "cafe",
    "bar",
    "clinic",
    "salon",
    "spa",
    "real estate",
    "agency",
    "marketing",
    "consulting",
    "school",
    "university",
    "foundation",
    "government",
    "church",
    "media company",
)

_MUSIC_ROLE_TOKENS = (
    "musician/band",
    "musician",
    "band",
    "artist",
    "music artist",
    "music",
    "singer",
    "vocalist",
    "rapper",
    "mc",
    "dj",
    "producer",
    "beatmaker",
    "songwriter",
    "composer",
    "recording artist",
    "performer",
    "entertainer",
    "record label",
    "music group",
    "orchestra",
    "choir",
    "collective",
)


def _pick_descriptor_from_candidates(name: str, candidates: List[str], aria_label: str, primary_category: str) -> str:
    """
    Prefer descriptor/subtitle text that carries music hints.
    Falls back to the first non-name candidate if no music tokens are present.
    """
    for text in candidates or []:
        if not text:
            continue
        if primary_category and text == primary_category:
            continue
        if _category_looks_like_name(name, text):
            continue
        if _text_has_music_tokens(text):
            return text

    if _text_has_music_tokens(aria_label):
        return aria_label

    for text in candidates or []:
        if not text:
            continue
        if primary_category and text == primary_category:
            continue
        if _category_looks_like_name(name, text):
            continue
        return text
    return ""


class FacebookDriverError(RuntimeError):
    """Raised when the Facebook driver/session is unavailable or dead."""


def _log(logger: LoggerFn, message: str) -> None:
    """Emit message once via callable/logger.info or fallback to print."""
    if not message:
        return
    try:
        if logger:
            if callable(logger):
                logger(message)
                return
            info_fn = getattr(logger, "info", None)
            if callable(info_fn):
                info_fn(message)
                return
    except Exception:
        pass
    try:
        print(message)
    except Exception:
        pass


def _classify_night_fb_attempt_state(status: str, existing: str = "") -> str:
    existing_clean = str(existing or "").strip()
    if existing_clean and existing_clean != "attempted_fb":
        return existing_clean
    status_norm = str(status or "").strip().lower()
    if "reject" in status_norm or "blocked" in status_norm:
        return "attempted_fb_rejected_by_acceptance_guard"
    if any(token in status_norm for token in ("login_wall", "login_redirect", "checkpoint", "warning")):
        return "attempted_fb_login_wall_or_checkpoint"
    if "content_unavailable" in status_norm:
        return "attempted_fb_content_unavailable"
    if any(token in status_norm for token in ("timeout", "fetch_error", "driver_error", "no_display")):
        return "attempted_fb_timeout_or_fetch_error"
    if status_norm:
        return "attempted_fb_no_email_on_page"
    return existing_clean or "attempted_fb"


def _find_first(driver, css_selector: str):
    """Best-effort single element lookup; never raises."""
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, css_selector)
        return elements[0] if elements else None
    except Exception:
        return None


def _find_all(driver, css_selector: str) -> List:
    """Best-effort multi element lookup; never raises."""
    try:
        return driver.find_elements(By.CSS_SELECTOR, css_selector)
    except Exception:
        return []


def _load_fb_page_with_timeout(
    driver,
    url: str,
    timeout_s: float = 20.0,
    logger: LoggerFn = None,
    unblock_on_ready: bool = False,
) -> Tuple[str, str, bool]:
    """Navigate with a hard timeout; recover via window.stop().

    Returns (html, current_url, timed_out flag). Does not raise on
    TimeoutException; other webdriver errors bubble up.
    """
    if not driver or not url:
        return "", url or "", False

    timed_out = False
    baseline_url = ""
    baseline_html = ""

    try:
        driver.set_page_load_timeout(timeout_s)
    except Exception:
        pass

    if unblock_on_ready:
        try:
            baseline_url = getattr(driver, "current_url", "") or ""
        except Exception:
            baseline_url = ""
        try:
            baseline_html = getattr(driver, "page_source", "") or ""
        except Exception:
            baseline_html = ""

    try:
        if unblock_on_ready:
            nav_started = False
            try:
                driver.execute_script(
                    """
                    window.setTimeout(function(targetUrl) {
                        try {
                            window.location.assign(targetUrl);
                        } catch (navError) {
                            window.location.href = targetUrl;
                        }
                    }, 0, arguments[0]);
                    return true;
                    """,
                    url,
                )
                # Treat a non-exception kickoff as started even if the return
                # value is lost during the async navigation handoff.
                nav_started = True
            except Exception:
                nav_started = False

            if nav_started:
                deadline = time.time() + max(float(timeout_s or 0.0), 0.1)
                while time.time() < deadline:
                    try:
                        current_url = getattr(driver, "current_url", "") or ""
                    except Exception:
                        current_url = ""
                    try:
                        current_html = getattr(driver, "page_source", "") or ""
                    except Exception:
                        current_html = ""
                    try:
                        ready_state = str(driver.execute_script("return document.readyState") or "").strip().lower()
                    except Exception:
                        ready_state = ""

                    surface_changed = bool(
                        (current_url and current_url != baseline_url)
                        or (current_html and current_html != baseline_html)
                    )
                    url_ready = bool(current_url and current_url != "about:blank")
                    html_ready = bool(current_html and (current_html != baseline_html or not baseline_html))
                    if surface_changed and ((ready_state in {"interactive", "complete"} and url_ready) or html_ready):
                        break
                    time.sleep(0.1)
                else:
                    timed_out = True
            else:
                driver.get(url)
        else:
            driver.get(url)
    except TimeoutException:
        timed_out = True
    except WebDriverException:
        # Non-timeout driver errors should surface to callers.
        raise
    except Exception:
        # Preserve previous behaviour for non-timeout errors.
        raise

    if timed_out:
        _log(logger, f"[FB Enrich] Timeout loading {url} ({int(timeout_s)}s)")
        try:
            driver.execute_script("window.stop();")
            _log(logger, "[FB Enrich] Recovered from timeout using window.stop()")
        except Exception:
            pass

    try:
        current_url = getattr(driver, "current_url", "") or url
    except Exception:
        current_url = url

    try:
        html = getattr(driver, "page_source", "") or ""
    except Exception:
        html = ""

    if unblock_on_ready and timed_out and current_url == baseline_url and html == baseline_html:
        current_url = url
        html = ""

    return html, current_url, timed_out


def _normalize_fb_href(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    href_lower = href.lower()
    if href_lower.startswith("javascript:") or href.startswith("#"):
        return ""
    try:
        href = urllib.parse.urljoin("https://www.facebook.com", href)
    except Exception:
        pass
    href = href.split("#", 1)[0]
    return href


def _is_candidate_usable(href: str) -> bool:
    href = _normalize_fb_href(href)
    if not href or "facebook.com" not in href:
        return False
    try:
        if is_junk_fb_candidate_url(href):
            return False
    except Exception:
        pass
    try:
        return _fb_is_candidate_url_allowed(href)
    except Exception:
        return False


def _is_role_link_url_plausible(href: str) -> bool:
    """Light pre-filter for URLs harvested from non-anchor role-link elements."""
    if not href:
        return False
    lowered = href.lower()
    if "/pages/" in lowered or "/people/" in lowered or "/profile.php" in lowered:
        return True
    slug_match = re.match(r"https?://(?:www\\.)?facebook\\.com/[^/?#]+/?$", lowered)
    return bool(slug_match)


def _extract_role_link_candidates(el, apply_prefilter: bool = True) -> List[str]:
    """
    Extract plausible URLs from [role="link"] elements.
    Prefers descendant anchors, then href-like attributes.
    """
    if el is None:
        return []

    candidates: List[str] = []

    # Descendant anchors first (common case).
    try:
        for anchor in el.find_elements(By.CSS_SELECTOR, "a[href]"):
            href = _normalize_fb_href(anchor.get_attribute("href") or "")
            if href and "facebook.com" in href:
                candidates.append(href)
                break  # first hit is enough
    except Exception:
        pass

    attr_names = (
        "href",
        "data-href",
        "data-url",
        "data-lynx-uri",
        "data-target-href",
        "data-redirect",
        "data-redirect-url",
        "data-uri",
        "data-store",
    )
    for name in attr_names:
        try:
            raw = el.get_attribute(name) or ""
        except Exception:
            raw = ""
        href = _normalize_fb_href(raw)
        if href and "facebook.com" in href:
            candidates.append(href)

    deduped: List[str] = []
    seen: Set[str] = set()
    for href in candidates:
        if href and href not in seen:
            seen.add(href)
            deduped.append(href)

    if not apply_prefilter:
        return deduped
    return [href for href in deduped if _is_role_link_url_plausible(href)]


def _safe_attribute_map(el) -> Dict[str, str]:
    """
    Best-effort attribute map for href-like scanning.
    Only used in debug/low-yield fallback; keep lightweight.
    """
    attrs: Dict[str, str] = {}
    if el is None:
        return attrs
    try:
        raw_attrs = el.get_property("attributes")
        if isinstance(raw_attrs, (list, tuple)):
            for item in raw_attrs:
                try:
                    name = item.get("name") if hasattr(item, "get") else getattr(item, "name", None)
                    value = item.get("value") if hasattr(item, "get") else getattr(item, "value", None)
                except Exception:
                    name = None
                    value = None
                if name and value:
                    attrs[str(name)] = str(value)
        elif isinstance(raw_attrs, dict):
            for name, value in raw_attrs.items():
                if name and value:
                    attrs[str(name)] = str(value)
    except Exception:
        pass

    # Fallback to common attributes if the NamedNodeMap path failed.
    if not attrs:
        for name in (
            "href",
            "data-href",
            "data-url",
            "data-lynx-uri",
            "data-target-href",
            "data-redirect",
            "data-redirect-url",
            "data-uri",
            "data-store",
            "rel",
            "aria-label",
        ):
            try:
                value = el.get_attribute(name)
            except Exception:
                value = None
            if value:
                attrs[name] = str(value)
    return attrs


def _extract_href_like_strings(value: str) -> List[str]:
    """Conservative extractor for href-ish strings within an attribute value."""
    results: List[str] = []
    text = str(value or "")
    if not text:
        return results

    url_pattern = re.compile(r"https?://[^\"'\\s>]+", re.IGNORECASE)
    for match in url_pattern.findall(text):
        if "facebook.com" in match:
            results.append(match)

    if text.startswith("/"):
        results.append(text)
    return results


def _scan_href_like_attributes(container, max_nodes: int = 150, max_candidates: int = 50) -> List[str]:
    """
    Debug-only, low-yield fallback: scan early DOM nodes for href-like attribute values.
    """
    results: List[str] = []
    if container is None or max_nodes <= 0 or max_candidates <= 0:
        return results
    try:
        nodes = container.find_elements(By.CSS_SELECTOR, "*")
    except Exception:
        nodes = []

    seen: Set[str] = set()
    for el in nodes[:max_nodes]:
        attrs = _safe_attribute_map(el)
        for val in attrs.values():
            for hrefish in _extract_href_like_strings(val):
                href = _normalize_fb_href(hrefish)
                if not href or "facebook.com" not in href:
                    continue
                if href in seen:
                    continue
                seen.add(href)
                results.append(href)
                if len(results) >= max_candidates:
                    return results
    return results


def _sanitize_fs_key(text: str, max_len: int = 80) -> str:
    """
    Filesystem-safe, bounded key for debug artifacts.
    """
    val = (text or "unknown").strip().lower()
    val = unicodedata.normalize("NFKD", val)
    val = re.sub(r"[^a-z0-9._-]+", "_", val)
    val = val.strip("_") or "unknown"
    if len(val) > max_len:
        val = val[:max_len]
    return val


def _write_night_fb_fail_evidence_debug(
    payload: Dict[str, str],
    *,
    row_index: Optional[int],
    collector_state: Optional[Dict[str, Any]],
    logger: LoggerFn = None,
) -> Optional[str]:
    if os.getenv("NIGHT_FB_EVIDENCE_DEBUG") != "1":
        return None
    if not isinstance(payload, dict):
        return None

    status = str(payload.get("FB_Status", "") or "").strip()
    status_norm = status.lower()
    qualifying_exact = {
        "pass_a_no_email_on_page",
        "pass_a_content_unavailable",
        "pass_a_fetch_error",
        "content_unavailable",
        "no_candidates",
    }
    if status_norm not in qualifying_exact and not (
        status_norm.endswith("_no_email_on_page")
        or status_norm.endswith("_content_unavailable")
        or status_norm.endswith("_fetch_error")
    ):
        return None
    if str(payload.get("Email", "") or "").strip() or str(payload.get("Email_All", "") or "").strip():
        return None

    run_dir = os.getenv("RUN_DIR") or os.getenv("NIGHT_RUN_DIR")
    if not run_dir:
        return None

    state = dict(collector_state or {})
    html_available = bool(state.get("html_available"))
    text_available = bool(state.get("visible_text_available"))
    anchor_available = bool(state.get("anchor_values_available"))
    reveal_available = bool(state.get("reveal_actions_available"))
    raw_html = state.get("html")
    visible_text = state.get("visible_text")
    anchor_values = state.get("anchor_values")
    reveal_actions = state.get("reveal_actions")
    resolved_url = str(
        state.get("resolved_url")
        or payload.get("Facebook_URL")
        or payload.get("Facebook URL")
        or ""
    ).strip()

    if html_available:
        html_source = str(raw_html or "")
        emails_from_html, _ = _extract_emails_from_html(html_source)
        html_contains_at: Optional[bool] = "@" in html_source
    else:
        html_source = ""
        emails_from_html = None
        html_contains_at = None

    if text_available:
        text_source = str(visible_text or "")
        emails_from_text, _ = _extract_emails_from_html("", rendered_text=text_source)
        visible_text_contains_at: Optional[bool] = "@" in text_source
    else:
        text_source = ""
        emails_from_text = None
        visible_text_contains_at = None

    if anchor_available:
        anchor_source = [str(value or "").strip() for value in (anchor_values or []) if str(value or "").strip()]
        emails_from_anchors, _ = _extract_emails_from_html("", anchor_values=anchor_source)
        anchor_count: Optional[int] = len(anchor_source)
        anchor_has_mailto: Optional[bool] = any(value.lower().startswith("mailto:") for value in anchor_source)
    else:
        anchor_source = []
        emails_from_anchors = None
        anchor_count = None
        anchor_has_mailto = None

    if html_available or text_available or anchor_available:
        raw_merged_candidates, _ = _extract_emails_from_html(
            html_source,
            rendered_text=text_source,
            anchor_values=anchor_source,
        )
    else:
        raw_merged_candidates = None

    if resolved_url or html_available:
        health = _night_fb_page_health_snapshot(resolved_url, html_source or None)
        captcha: Optional[bool] = bool(health.get("captcha"))
        checkpoint: Optional[bool] = bool(health.get("checkpoint"))
        login_wall: Optional[bool] = bool(health.get("login_wall"))
        warning_flag: Optional[bool] = bool(health.get("warning_reason"))
    else:
        captcha = None
        checkpoint = None
        login_wall = None
        warning_flag = None

    artist = str(payload.get("Artist Name", "") or payload.get("Artist", "") or "").strip()
    artist_key = _sanitize_fs_key(artist or "unknown")
    row_key = f"row_{int(row_index):06d}" if isinstance(row_index, int) else "row_unknown"
    output_dir = Path(run_dir) / "fb_evidence_debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{row_key}__{artist_key}.json"

    evidence = {
        "identity": {
            "artist": artist or None,
            "row_index": row_index,
            "path": "explicit_pass_a" if status_norm.startswith("pass_a_") else "pass_b",
            "resolved_url": resolved_url or None,
            "FB_Status": status or None,
            "FB_Reason": str(payload.get("FB_Reason", "") or "") or None,
            "FB_Attempt_State": str(payload.get(FB_ATTEMPT_STATE_COL, "") or "") or None,
            "FB_Write_State": str(payload.get("FB_Write_State", "") or "") or None,
        },
        "render_health": {
            "render_invalid_reason": str(state.get("render_invalid_reason", "") or "") or None,
            "captcha": captcha,
            "checkpoint": checkpoint,
            "login_wall": login_wall,
            "warning": warning_flag,
            "driver_kind": str(state.get("driver_kind", "") or "") or None,
        },
        "collector": {
            "html_contains_at": html_contains_at,
            "visible_text_contains_at": visible_text_contains_at,
            "anchor_count": anchor_count,
            "anchor_has_mailto": anchor_has_mailto,
            "reveal_actions": list(reveal_actions or []) if reveal_available else None,
        },
        "extraction": {
            "emails_from_html": emails_from_html,
            "emails_from_text": emails_from_text,
            "emails_from_anchors": emails_from_anchors,
            "raw_merged_candidates": raw_merged_candidates,
        },
        "writeback": {
            "final_email": str(payload.get("Email", "") or "") or None,
            "final_email_all": str(payload.get("Email_All", "") or "") or None,
            "had_upstream_email_candidate": bool(raw_merged_candidates),
        },
    }

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2, sort_keys=True)
    _log(logger, f"[Night FB][EvidenceDebug] wrote fail-case evidence pack: {output_path}")
    return str(output_path)


def _maybe_dump_dom_gate_debug(
    run_dir: Optional[str],
    safe_key: str,
    chosen_selector: str,
    selector_stats: List[Dict[str, Any]],
    sample_hrefs: List[str],
    container_html: str,
    logger: LoggerFn = None,
    max_bytes: int = 50_000,
) -> None:
    """
    Best-effort, bounded debug dump for zero-anchor/candidate situations.
    """
    if not run_dir:
        return
    try:
        base = os.path.join(run_dir, "fb_search_debug")
        os.makedirs(base, exist_ok=True)
        html_path = os.path.join(base, f"{safe_key}__search.html")
        meta_path = os.path.join(base, f"{safe_key}__meta.json")

        html = container_html or ""
        if len(html) > max_bytes:
            html = html[:max_bytes] + "...(truncated)"

        meta_stats: List[Dict[str, Any]] = []
        for entry in selector_stats:
            meta_stats.append(
                {
                    "selector": entry.get("selector"),
                    "containers_found": entry.get("containers_found", 0),
                    "anchors_in_scope": entry.get("anchors_in_scope", 0),
                    "role_links_in_scope": entry.get("role_links_in_scope", 0),
                    "links_in_scope_total": entry.get("links_in_scope_total", 0),
                    "usable_count": entry.get("usable_count", 0),
                    "href_samples": (entry.get("hrefs") or [])[:5],
                }
            )

        meta = {
            "chosen_selector": chosen_selector,
            "sample_hrefs": sample_hrefs[:25],
            "selectors": meta_stats,
        }

        try:
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(html)
        except Exception:
            return

        try:
            import json

            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
        except Exception:
            return

        _log(logger, f"[Night FB][DOM Gate V2][debug_dump] html={html_path} meta={meta_path}")
    except Exception:
        return


def _collect_container_candidates_v2(container, apply_role_link_prefilter: bool = True) -> Dict[str, Any]:
    """
    Expanded candidate extraction for Night FB DOM Gate v2.
    Returns dict with hrefs + counts to keep the caller lightweight.
    """
    try:
        max_scan = int(os.getenv("FB_DOM_GATE_MAX_SCAN", "500") or "500")
    except Exception:
        max_scan = 500
    if max_scan <= 0:
        max_scan = 500
    data: Dict[str, Any] = {
        "hrefs": [],
        "anchors_in_scope": 0,
        "role_links_in_scope": 0,
        "links_in_scope_total": 0,
        "count_a_href": 0,
        "count_a_role_link_href": 0,
        "count_role_link": 0,
        "usable_count": 0,
    }
    if container is None:
        return data

    try:
        anchor_elements = container.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        anchor_elements = []
    try:
        role_anchor_elements = container.find_elements(By.CSS_SELECTOR, 'a[role="link"][href]')
    except Exception:
        role_anchor_elements = []
    try:
        role_link_elements = container.find_elements(By.CSS_SELECTOR, '[role="link"]')
    except Exception:
        role_link_elements = []

    anchor_ids: Set[str] = set()
    for el in (anchor_elements + role_anchor_elements)[:max_scan]:
        try:
            anchor_ids.add(el.id)
        except Exception:
            anchor_ids.add(str(id(el)))

    data["count_a_href"] = len(anchor_elements)
    data["count_a_role_link_href"] = len(role_anchor_elements)
    data["count_role_link"] = len(role_link_elements)
    # Cap scope counts to processed subset to reflect what we actually scan for performance.
    data["anchors_in_scope"] = min(len(anchor_ids), max_scan)
    data["role_links_in_scope"] = min(len(role_link_elements), max_scan)

    seen_urls: Set[str] = set()
    hrefs: List[str] = []

    for el in anchor_elements[:max_scan]:
        try:
            href = _normalize_fb_href(el.get_attribute("href") or "")
        except Exception:
            href = ""
        if not href or "facebook.com" not in href:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        hrefs.append(href)
        if len(hrefs) >= max_scan:
            break

    if len(hrefs) < max_scan:
        for el in role_link_elements[:max_scan]:
            for href in _extract_role_link_candidates(el, apply_prefilter=apply_role_link_prefilter):
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)
                hrefs.append(href)
                if len(hrefs) >= max_scan:
                    break
            if len(hrefs) >= max_scan:
                break

    data["hrefs"] = hrefs[:max_scan]
    try:
        data["usable_count"] = sum(1 for href in hrefs if _is_candidate_usable(href))
    except Exception:
        data["usable_count"] = 0
    data["links_in_scope_total"] = data["anchors_in_scope"] + data["role_links_in_scope"]
    return data


def _collect_search_candidates_from_html_v2(html: str) -> Tuple[str, int, int, List[str]]:
    """
    Lightweight, non-Selenium mirror of the V2 selector strategy.
    Used in tests with synthetic HTML fixtures.
    """
    if not html:
        return "NONE", 0, 0, []

    soup = BeautifulSoup(html, "html.parser")
    selector_order = [
        'div[role="main"] div[role="feed"]',
        'div[role="main"] div[aria-label="Search results"]',
        'div[aria-label="Search results"]',
        'div[role="main"] section[aria-label*="Search results"]',
        'div[role="main"] [data-pagelet^="SearchResults"]',
        'div[role="main"] div[aria-label*="Search results"]',
        'div[role="main"] div[role="article"]',
        'div[role="main"]',
    ]

    stats: List[Dict[str, Any]] = []
    for sel in selector_order:
        try:
            containers = soup.select(sel)
        except Exception:
            containers = []
        container = containers[0] if containers else None
        anchors_in_scope = 0
        role_links_in_scope = 0
        hrefs: List[str] = []
        if container is not None:
            try:
                anchor_elements = container.select("a[href]")
            except Exception:
                anchor_elements = []
            try:
                role_link_elements = container.select('[role="link"]')
            except Exception:
                role_link_elements = []
            anchors_in_scope = len(anchor_elements)
            role_links_in_scope = len(role_link_elements)

            seen: Set[str] = set()
            for el in anchor_elements:
                try:
                    href = _normalize_fb_href(el.get("href") or "")
                except Exception:
                    href = ""
                if not href or "facebook.com" not in href:
                    continue
                if href in seen:
                    continue
                seen.add(href)
                hrefs.append(href)
            for el in role_link_elements:
                try:
                    href = _normalize_fb_href(el.get("href") or "")
                except Exception:
                    href = ""
                if not href or href in seen:
                    continue
                seen.add(href)
                hrefs.append(href)

        usable_count = 0
        try:
            usable_count = sum(1 for h in hrefs if _is_candidate_usable(h))
        except Exception:
            usable_count = 0

        stats.append(
            {
                "selector": sel,
                "anchors_in_scope": anchors_in_scope,
                "role_links_in_scope": role_links_in_scope,
                "links_in_scope_total": anchors_in_scope + role_links_in_scope,
                "hrefs": hrefs,
                "usable_count": usable_count,
            }
        )

    def _score_entry(entry: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
        return (
            entry.get("usable_count", 0),
            entry.get("anchors_in_scope", 0),
            entry.get("role_links_in_scope", 0),
            entry.get("links_in_scope_total", 0),
            len(entry.get("hrefs", []) or []),
        )

    chosen_entry = max(stats, key=_score_entry) if stats else {}
    chosen_selector = chosen_entry.get("selector", "NONE")
    anchors_in_scope = chosen_entry.get("anchors_in_scope", 0)
    candidates_pre_url_gate = len(chosen_entry.get("hrefs", []) or [])
    return chosen_selector, anchors_in_scope, candidates_pre_url_gate, chosen_entry.get("hrefs", []) or []


def _find_fb_home_search_input(driver, timeout: float = 12.0, poll_seconds: float = 0.25):
    if driver is None:
        return None
    deadline = time.time() + max(float(timeout or 0.0), 0.0)
    while True:
        for by, selector in _FB_HOME_SEARCH_INPUT_SELECTORS:
            try:
                elements = driver.find_elements(by, selector)
            except Exception:
                continue
            for element in elements or []:
                if element is None:
                    continue
                try:
                    if hasattr(element, "is_displayed") and not element.is_displayed():
                        continue
                except Exception:
                    pass
                try:
                    if hasattr(element, "is_enabled") and not element.is_enabled():
                        continue
                except Exception:
                    pass
                return element
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(max(float(poll_seconds or 0.0), 0.05), remaining))


def _run_fb_homepage_search(
    driver,
    query: str,
    *,
    logger: LoggerFn = None,
    log_prefix: str = "[Night FB]",
) -> Tuple[str, str, bool]:
    if driver is None:
        return "", "", False

    html, current_url, timed_out = _load_fb_page_with_timeout(
        driver,
        "https://www.facebook.com/",
        timeout_s=20.0,
        logger=logger,
    )
    if timed_out:
        return html or "", current_url or _safe_current_url(driver), True

    search_input = _find_fb_home_search_input(driver)
    if search_input is None:
        return getattr(driver, "page_source", "") or html or "", _safe_current_url(driver) or current_url, False

    before_url = current_url or _safe_current_url(driver)
    try:
        search_input.click()
        try:
            search_input.send_keys(Keys.COMMAND, "a")
        except Exception:
            search_input.send_keys(Keys.CONTROL, "a")
        search_input.send_keys(Keys.DELETE)
        search_input.send_keys(query)
        search_input.send_keys(Keys.ENTER)
    except Exception as exc:
        _log(logger, f"{log_prefix} search_method=homepage_ui submit_failed={exc.__class__.__name__}")
        return getattr(driver, "page_source", "") or "", _safe_current_url(driver) or before_url, False

    timed_out_after_submit = False

    def _search_surface_ready(drv) -> bool:
        current = (_safe_current_url(drv) or "").lower()
        if "/search/" in current:
            return True
        try:
            surface_html = getattr(drv, "page_source", "") or ""
        except Exception:
            surface_html = ""
        selector, anchors_in_scope, candidates_pre_url_gate, hrefs = _collect_search_candidates_from_html_v2(surface_html)
        return bool(
            selector != "NONE"
            or anchors_in_scope > 0
            or candidates_pre_url_gate > 0
            or hrefs
            or (_safe_current_url(drv) or "") != before_url
        )

    try:
        WebDriverWait(driver, 12.0).until(_search_surface_ready)
    except TimeoutException:
        timed_out_after_submit = True
    except Exception:
        timed_out_after_submit = True

    time.sleep(1.0)
    return getattr(driver, "page_source", "") or "", _safe_current_url(driver) or before_url, timed_out_after_submit


def _fb_homepage_junk_href(href: str) -> bool:
    href_clean = (href or "").strip()
    if not href_clean:
        return False
    if is_junk_fb_candidate_url(href_clean):
        return True
    try:
        parsed = urllib.parse.urlparse(href_clean)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if host and "facebook.com" not in host:
        return False
    path = (parsed.path or "").strip("/").lower()
    if not path:
        return False
    first_segment = path.split("/", 1)[0]
    if first_segment in _FB_HOMEPAGE_JUNK_SEGMENTS:
        return True
    return False


def _fb_homepage_junk_candidate(candidate: Any) -> bool:
    if candidate is None:
        return False
    url = (getattr(candidate, "url", "") or "").strip()
    if _fb_homepage_junk_href(url):
        return True

    primary_texts = [
        (getattr(candidate, "name", "") or "").strip().lower(),
        (getattr(candidate, "aria_label", "") or "").strip().lower(),
    ]
    for text in primary_texts:
        if not text:
            continue
        if text in _FB_HOMEPAGE_JUNK_TEXT_EXACT:
            return True
        if any(token in text for token in _FB_HOMEPAGE_JUNK_TEXT_TOKENS):
            return True
    return False


def _fb_selector_has_search_results_signal(selector: str) -> bool:
    selector_l = (selector or "").strip().lower()
    if not selector_l or selector_l == "none":
        return False
    return "search results" in selector_l or "searchresults" in selector_l


def _guard_homepage_fb_search_candidates(
    candidates: Optional[List[Any]],
    *,
    page_html: str,
    current_url: str,
    logger: LoggerFn = None,
    log_prefix: str = "[Night FB]",
    query: str = "",
) -> Tuple[List[Any], str]:
    current_url_l = (current_url or "").lower()
    selector, _anchors_in_scope, _pre_gate_count, hrefs = _collect_search_candidates_from_html_v2(page_html or "")
    usable_hrefs = [href for href in hrefs if _is_candidate_usable(href)]
    junk_hrefs = [href for href in usable_hrefs if _fb_homepage_junk_href(href)]
    filtered = [cand for cand in (candidates or []) if not _fb_homepage_junk_candidate(cand)]
    junk_count = max(len(candidates or []) - len(filtered), len(junk_hrefs))

    if junk_count:
        _log(
            logger,
            f"{log_prefix} search_method=homepage_ui junk_candidates_filtered={junk_count} query='{query}'",
        )

    if "/search/" not in current_url_l and not _fb_selector_has_search_results_signal(selector):
        return [], "generic_auth_surface"
    if not filtered and junk_hrefs:
        return [], "only_junk_candidates"
    return filtered, ""


def _fb_search_surface_miss_reason(
    html: str,
    *,
    driver=None,
    current_url: str = "",
    timed_out: bool = False,
) -> str:
    page_html = html or ""
    resolved_url = current_url or _safe_current_url(driver)
    resolved_url_l = (resolved_url or "").lower()

    if resolved_url and (is_fb_login_redirect(resolved_url) or _is_fb_login_or_security_url(resolved_url)):
        return ""
    if _looks_like_fb_warning_or_block(page_html, resolved_url_l):
        return ""
    if timed_out and not page_html.strip():
        return "timeout_no_results"
    if not page_html.strip():
        return "blank_html"

    selector, anchors_in_scope, _pre_gate_count, hrefs = _collect_search_candidates_from_html_v2(page_html)
    usable_hrefs = [href for href in hrefs if _is_candidate_usable(href)]

    overlay_present = False
    if driver is not None:
        try:
            overlay_present = bool(_has_checkpoint_overlay(driver))
        except Exception:
            overlay_present = False

    if timed_out and (selector == "NONE" or not usable_hrefs):
        return "timeout_no_results"
    if selector == "NONE":
        return "dom_container_missing"
    if anchors_in_scope <= 0:
        return "overlay_zero_anchors" if overlay_present else "zero_anchors"
    if not usable_hrefs:
        return "overlay_zero_anchors" if overlay_present else "zero_usable_hrefs"
    return ""


def _extract_anchor_hrefs(container) -> Tuple[List[str], List]:
    """
    Extract raw anchor hrefs inside a container (dedup by element id + href).
    Returns (hrefs, elements).
    """
    if container is None:
        return [], []
    anchors = []
    seen_ids = set()
    for selector in ("a", 'a[role=\"link\"]'):
        try:
            found = container.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            found = []
        for el in found:
            try:
                el_id = el.id
            except Exception:
                el_id = id(el)
            if el_id in seen_ids:
                continue
            seen_ids.add(el_id)
            anchors.append(el)

    hrefs = []
    href_seen = set()
    for el in anchors:
        try:
            raw_href = el.get_attribute("href") or ""
        except Exception:
            raw_href = ""
        href = _normalize_fb_href(raw_href)
        if not href or "facebook.com" not in href:
            continue
        if href in href_seen:
            continue
        href_seen.add(href)
        hrefs.append(href)
    return hrefs, anchors


def _count_usable(container) -> int:
    hrefs, _ = _extract_anchor_hrefs(container)
    usable = 0
    for href in hrefs:
        if _is_candidate_usable(href):
            usable += 1
    return usable


def _safe_current_url(driver) -> str:
    try:
        return driver.current_url
    except Exception:
        return ""


def _container_html_preview(container, max_chars: int = 500) -> tuple[int, str]:
    if container is None:
        return 0, ""
    try:
        html = container.get_attribute("innerHTML") or ""
    except Exception:
        return 0, ""
    html = html.strip()
    if not html:
        return 0, ""
    if len(html) > max_chars:
        return len(html), html[:max_chars] + "...(truncated)"
    return len(html), html


def _has_checkpoint_overlay(driver) -> bool:
    try:
        html = (driver.page_source or "").lower()
    except Exception:
        return False
    tokens = ("checkpoint", "consent", "security check")
    return any(tok in html for tok in tokens)


def _wait_for_anchor_population(
    driver,
    container_selector: str,
    min_anchors: int = 1,
    timeout: float = 6.0,
    poll_seconds: float = 0.4,
    logger: LoggerFn = None,
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Wait for anchors to populate inside a container; returns bool for success.
    """
    started = time.time()
    container = _find_first(driver, container_selector)
    _, anchors_initial = _extract_anchor_hrefs(container)
    deadline = started + max(timeout, 0.1)
    anchors_after = anchors_initial
    while time.time() < deadline:
        container = _find_first(driver, container_selector)
        _, anchors = _extract_anchor_hrefs(container)
        if len(anchors) >= max(min_anchors, 1):
            elapsed_ms = (time.time() - started) * 1000.0
            _log(
                logger,
                f"[FB AnchorWait] anchor_waited=1 selector='{container_selector}' anchors_before={len(anchors_initial)} "
                f"anchors_after={len(anchors)} waited_ms={elapsed_ms:.0f} ctx={context or {}}",
            )
            if logger is None and context is None:
                return True
            return True, len(anchors_initial), len(anchors), elapsed_ms
        anchors_after = anchors
        time.sleep(max(poll_seconds, 0.1))
    elapsed_ms = (time.time() - started) * 1000.0
    if logger is None and context is None:
        return False
    return False, len(anchors_initial), len(anchors_after), elapsed_ms


def _slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").strip().lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _split_multi(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[;,|]", value)
    return [p.strip() for p in parts if p and p.strip()]


def _sanitize_fb_category_text(cat: Optional[str]) -> Optional[str]:
    """
    Phase 1: sanitize noisy FB category strings.
    """
    if not cat:
        return None
    try:
        cleaned = str(cat).strip()
    except Exception:
        cleaned = ""
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if len(cleaned) > 80:
        return None
    if lowered.startswith("reminder"):
        return None
    if lowered.startswith("you have an event"):
        return None
    return cleaned


def _normalize_name_like(text: str) -> str:
    """
    Normalize text for loose name/category comparisons: lowercase + strip punctuation/spaces.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower().strip())


def _min_fb_accept_score() -> int:
    try:
        return int(os.getenv("MIN_FB_ACCEPT_SCORE") or os.getenv("FB_MIN_ACCEPT_SCORE") or _DEFAULT_MIN_FB_ACCEPT_SCORE)
    except Exception:
        return _DEFAULT_MIN_FB_ACCEPT_SCORE


def _candidate_has_non_music_deny(features: Dict[str, Any]) -> bool:
    """
    Hard reject for obvious non-music categories/services when there are no music hints.
    Requires both a deny token and the absence of music signals to avoid blocking legit bands.
    """
    blob_parts = [
        features.get("category") or "",
        features.get("descriptor") or "",
        features.get("aria_label") or "",
        features.get("secondary_text") or "",
        " ".join(features.get("category_tokens") or []) if isinstance(features.get("category_tokens"), (list, tuple, set)) else "",
    ]
    blob = " ".join(blob_parts).lower()
    has_deny = any(tok in blob for tok in _FB_NON_MUSIC_DENY_TOKENS)
    has_music = bool(features.get("music_any"))
    service_only = bool(features.get("service_only"))
    return bool(has_deny and (not has_music) and service_only)


def _candidate_corporate_risk(features: Dict[str, Any]) -> Tuple[bool, str, bool]:
    """
    Reuse shared FB business/corporate detection across pre-fetch candidate fields.
    Returns (has_corporate_risk, token, has_artist_like_signal).
    """
    name = str(features.get("name") or "")
    url = str(features.get("url") or "")
    meta_parts = [
        str(features.get("category") or ""),
        str(features.get("descriptor") or ""),
        str(features.get("aria_label") or ""),
        str(features.get("secondary_text") or ""),
    ]
    category_tokens = features.get("category_tokens") or []
    if isinstance(category_tokens, (list, tuple, set)):
        meta_parts.extend(str(token or "") for token in category_tokens)
    meta_blob = " ".join(part for part in meta_parts if part)

    has_corporate_risk = False
    corporate_token = ""
    has_artist_like_signal = False

    if facebook_enrich is not None:
        detect_token = getattr(facebook_enrich, "detect_corporate_token", None)
        if callable(detect_token):
            try:
                has_corporate_risk, detected_token = detect_token(url, name, meta_blob)
                corporate_token = str(detected_token or "")
            except Exception:
                has_corporate_risk = False
                corporate_token = ""

        classify_signals = getattr(facebook_enrich, "classify_corporate_signals", None)
        if callable(classify_signals):
            try:
                signals = classify_signals(name, url, meta_blob)
                has_artist_like_signal = bool(getattr(signals, "has_artist", False))
                if bool(getattr(signals, "has_hard", False)):
                    has_corporate_risk = True
                if (not corporate_token) and has_corporate_risk:
                    corporate_token = "corporate"
            except Exception:
                pass

    return has_corporate_risk, corporate_token, has_artist_like_signal


def _candidate_is_safe_enough(item: Dict[str, Any], min_accept_score: int) -> Tuple[bool, str]:
    """
    Apply pre-selection guards to avoid scraping mismatches/non-music pages.
    Returns (is_safe, reason_if_rejected).
    """
    score = int(item.get("score") or 0)
    features = item.get("features") or {}
    name = str(features.get("name") or "")
    match_level = features.get("match_level") or "none"
    music_any = bool(features.get("music_any"))
    music_positive = bool(features.get("music_any") or features.get("music_primary") or features.get("music_descriptor"))
    strong_identity = match_level in {"exact", "near"}
    is_profile = bool(features.get("is_profile"))
    name_norm = re.sub(r"\s+", " ", name).strip().lower()

    if _candidate_has_non_music_deny(features):
        return False, "non_music_category"
    if is_profile and match_level in {"weak", "mismatch", "none"} and (not music_positive):
        return False, "profile_no_music_signal"
    corporate_risk, _corporate_token, artist_like_signal = _candidate_corporate_risk(features)
    if corporate_risk and (not strong_identity) and (not music_positive) and (not artist_like_signal):
        return False, "corporate_no_music_signal"
    if name_norm.startswith("profile photo of ") or name_norm.endswith(" profile photo"):
        return False, "placeholder_label"
    if match_level == "none":
        return False, "identity_none"
    if score < min_accept_score:
        return False, "rank_below_threshold"
    # Require at least some artist-identity signal before a music-looking page can
    # enter the Night Mode candidate pipeline.
    if match_level == "mismatch":
        return False, "identity_mismatch"
    return True, ""


def _category_looks_like_name(name: Optional[str], category: Optional[str]) -> bool:
    """
    Return True when the category text is effectively the same as the page name.
    Intended to drop bogus categories such as `category='Sofia Ly'`.
    """
    if not name or not category:
        return False
    name_norm = _normalize_name_like(name)
    cat_norm = _normalize_name_like(category)
    if not name_norm or not cat_norm:
        return False
    if name_norm == cat_norm:
        return True
    if name_norm.startswith(cat_norm) or cat_norm.startswith(name_norm):
        return True
    return False


def _unpack_fb_candidate(candidate):
    """
    Normalizes candidate return from _scrape_single_fb_candidate.
    Supports old 2-tuple and new 4-tuple shapes, and tolerates None.
    Returns: (night_result, emails, driver_kind, outcome)
    """
    if not candidate:
        return None, [], "unknown", "fetch_error"
    try:
        if len(candidate) == 2:
            night_result, emails = candidate
            return night_result, emails, "unknown", "fetch_error"
        night_result, emails, driver_kind, outcome = candidate
        return night_result, emails, driver_kind, outcome
    except Exception:
        return None, [], "unknown", "fetch_error"


def _fb_status_is_rejected(status: str) -> bool:
    """Return True when FB_Status denotes a rejected/mismatched/blocked candidate."""
    status_norm = (status or "").lower()
    tokens = ("reject", "blocked")
    return any(tok in status_norm for tok in tokens)


def _is_invalid_fb_value(value: object) -> bool:
    """Detect placeholder / missing FB values before any canonicalisation."""
    if value is None:
        return True
    try:
        v = str(value).strip().lower()
    except Exception:
        return True
    return v in {"", "nan", "none", "null"}


def _is_valid_fb_url_value(value: object) -> bool:
    """
    Reject obvious placeholder / missing values before normalisation.
    Keeps scope narrow to explicit FB URLs feeding PASS A.
    """
    if _is_invalid_fb_value(value):
        return False
    try:
        text = str(value)
    except Exception:
        return False
    cleaned = text.strip()

    lowered = cleaned.lower()

    # Strip scheme and leading slashes for placeholder detection.
    stripped = lowered
    if stripped.startswith("http://"):
        stripped = stripped[7:]
    elif stripped.startswith("https://"):
        stripped = stripped[8:]
    stripped = stripped.lstrip("/")
    if stripped.startswith("www."):
        stripped = stripped[4:]

    # Placeholder host/path combinations.
    placeholder_hosts = ("facebook.com", "m.facebook.com", "web.facebook.com", "touch.facebook.com", "fb.com", "fb.me")
    for host in placeholder_hosts:
        prefix = f"{host}/"
        if stripped == host or (
            stripped.startswith(prefix)
            and stripped[len(prefix):] in ("nan", "null", "none", "")
        ):
            return False

    # Placeholder path-only after stripping.
    if stripped in {"nan", "null", "none"}:
        return False

    has_allowed_host = any(h in stripped for h in ("facebook.com", "fb.com", "fb.me"))
    starts_with_slash = isinstance(value, str) and value.strip().startswith("/")

    # Require either an allowed host with a path, or a path-only value.
    if has_allowed_host:
        try:
            parsed = urllib.parse.urlparse(cleaned if lowered.startswith("http") else f"https://{stripped}")
            path = parsed.path or ""
        except Exception:
            return False
        if not path or path == "/":
            return False
        first_segment = path.lstrip("/").split("/", 1)[0]
        if first_segment in {"nan", "null", "none", ""}:
            return False
        return True

    if starts_with_slash:
        first_segment = stripped.split("/", 1)[0]
        if first_segment in {"nan", "null", "none", ""}:
            return False
        return True

    return False


def _clean_row_string(value: Any) -> str:
    try:
        import pandas as _pd  # type: ignore

        if _pd.isna(value):
            return ""
    except Exception:
        pass
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _row_has_usable_email_for_fb_skip(row: Dict[str, Any]) -> Tuple[bool, str]:
    email_all = _clean_row_string((row or {}).get("Email_All", ""))
    email_source = _clean_row_string((row or {}).get("Email Source", "")).lower()
    suspect_email = _clean_row_string((row or {}).get("Suspect_Email", ""))
    suspect_email_all = _clean_row_string((row or {}).get("Suspect_Email_All", ""))

    quarantined_repeat = email_source == "quarantined (repeat email)"
    suspect_present = bool(suspect_email or suspect_email_all)
    has_email_effective = bool(email_all) and not quarantined_repeat and not suspect_present
    return has_email_effective, email_all


def _raw_fb_value_for_log(value: Any) -> str:
    if value is None:
        return "None"
    try:
        import pandas as _pd  # type: ignore

        if _pd.isna(value):
            return "nan"
    except Exception:
        pass
    try:
        text = str(value)
    except Exception:
        return "<unprintable>"
    return text if text.strip() else "<blank>"


def _find_invalid_direct_fb_row_value(row: Dict[str, Any]) -> Tuple[str, str]:
    direct_fields = {field.lower() for field in _DIRECT_FB_ROW_FIELDS}
    seen: Set[str] = set()

    for key, value in (row or {}).items():
        try:
            key_text = str(key)
        except Exception:
            continue
        key_norm = key_text.lower()
        if key_norm not in direct_fields or key_norm in seen:
            continue
        seen.add(key_norm)

        if value is None:
            return key_text, _raw_fb_value_for_log(value)
        try:
            import pandas as _pd  # type: ignore

            if _pd.isna(value):
                return key_text, _raw_fb_value_for_log(value)
        except Exception:
            pass

        if isinstance(value, str):
            if value != "" and (not value.strip() or value.strip().lower() in {"nan", "none", "null"}):
                return key_text, _raw_fb_value_for_log(value)

    return "", ""


def _extract_fb_urls_for_night_mode(row):
    fields = _EXPLICIT_FB_INTAKE_FIELDS
    allowed_hosts = ("facebook.com", "m.facebook.com", "fb.com", "fb.me")

    def _clean_value(val: Any) -> str:
        try:
            import pandas as _pd  # type: ignore
            if _pd.isna(val):
                return ""
        except Exception:
            pass
        try:
            return str(val or "").strip()
        except Exception:
            return ""

    urls: List[str] = []
    seen: Set[str] = set()
    for field in fields:
        value = None
        for key, val in (row or {}).items():
            try:
                if str(key).lower() == field.lower():
                    value = val
                    break
            except Exception:
                continue
        raw = _clean_value(value)
        if not raw:
            continue
        parts = _FB_SPLIT_PATTERN.split(raw)
        for part in parts:
            candidate = _clean_value(part)
            if not candidate:
                continue
            if not _is_valid_fb_url_value(candidate):
                continue
            lowered = candidate.lower()
            if not any(host in lowered for host in allowed_hosts):
                continue
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            elif not candidate.startswith("http"):
                candidate = "https://" + candidate
            try:
                path = urllib.parse.urlparse(candidate).path.lower()
            except Exception:
                path = ""
            if any(path.startswith(b) for b in _EXPLICIT_FB_PREFILTER_PATHS):
                continue
            candidate = candidate.strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)

    return urls


_FB_LOW_INFO_SONG_TITLE_TOKENS = frozenset(
    {
        "acoustic",
        "bootleg",
        "demo",
        "dub",
        "edit",
        "instrumental",
        "interlude",
        "intro",
        "live",
        "mix",
        "original",
        "outro",
        "radio",
        "remaster",
        "remastered",
        "remix",
        "reprise",
        "session",
        "sessions",
        "snippet",
        "song",
        "tba",
        "teaser",
        "title",
        "track",
        "untitled",
        "unknown",
        "version",
        "vip",
    }
)


def _fb_song_title_is_usable(title: str) -> bool:
    if len(title) < 3:
        return False

    tokens = [token for token in re.split(r"[\W_]+", title.lower()) if token]
    if not tokens:
        return False

    alpha_tokens = [token for token in tokens if any(ch.isalpha() for ch in token)]
    if not alpha_tokens:
        return False

    if all(token in _FB_LOW_INFO_SONG_TITLE_TOKENS for token in alpha_tokens):
        return False

    if all(token.isdigit() or token in _FB_LOW_INFO_SONG_TITLE_TOKENS for token in tokens):
        return False

    return True


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
    working = working.strip()
    if not _fb_song_title_is_usable(working):
        return ""
    return working


def _normalize_fb_location_query(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"[,|/]+", raw) if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[:2])
    return raw


def _build_fb_discovery_query(
    artist: str,
    location: str = "",
    song_title: str = "",
    row: Any = None,
) -> Tuple[str, str]:
    secondary_signal = preferred_upstream_identity_hint(row)
    if not secondary_signal:
        secondary_signal = _sanitize_fb_song_title(song_title)
    if not secondary_signal:
        secondary_signal = _normalize_fb_location_query(location)
    query = " ".join(part for part in ((artist or "").strip(), secondary_signal) if part).strip()
    return query, secondary_signal


def _extract_fb_urls_from_row(row: Dict[str, str]) -> List[str]:
    """
    Collect explicit Facebook URLs from common clue fields in a row.
    Splits on commas/semicolons/pipes/whitespace and removes obvious non-page endpoints.
    """
    return _extract_fb_urls_for_night_mode(row)


def _parse_existing_fb_url(row: Dict[str, str]) -> str:
    for key in ("Facebook_URL", "Social Link", "External Links"):
        raw = str(row.get(key, "") or "").strip()
        if not raw:
            continue
        for candidate in _split_multi(raw):
            lowered = candidate.lower()
            if "facebook.com" in lowered:
                if candidate.startswith("/"):
                    return "https://www.facebook.com" + candidate
                return candidate
    return ""


def _normalise_fb_url(url: str) -> str:
    if _is_invalid_fb_value(url):
        return ""
    if not url:
        return ""
    cleaned = url.strip()
    if cleaned.startswith("/"):
        cleaned = "https://www.facebook.com" + cleaned
    elif "://" not in cleaned:
        lowered_cleaned = cleaned.lower()
        for host_prefix in ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.com", "fb.me"):
            if lowered_cleaned.startswith(host_prefix):
                cleaned = "https://" + cleaned
                break
    try:
        parsed = urllib.parse.urlparse(cleaned)
    except Exception:
        return ""

    scheme = parsed.scheme or "https"
    host = (parsed.netloc or "www.facebook.com").lower()
    if host in ("facebook.com", "web.facebook.com", "m.facebook.com", "touch.facebook.com"):
        host = "www.facebook.com"
    path = parsed.path or ""
    query = parsed.query or ""

    # Canonicalize /pg/<name>/... to /<name>
    if path.lower().startswith("/pg/"):
        parts = [p for p in path.split("/") if p]
        slug = parts[1] if len(parts) >= 2 else ""
        path = f"/{slug}" if slug else "/"

    if path.lower() == "/profile.php":
        qs = urllib.parse.parse_qs(query, keep_blank_values=False)
        ids = qs.get("id", [])
        if not ids:
            return ""
        profile_id = (ids[0] or "").strip().rstrip("/")
        if not profile_id.isdigit():
            return ""
        return urllib.parse.urlunparse((scheme, host, "/profile.php", "", f"id={profile_id}", ""))

    path = path.rstrip("/").lower()
    path_stripped = path.strip("/")
    if path_stripped in {"", "nan", "none", "null"}:
        return ""
    first_segment = path_stripped.split("/", 1)[0]
    if first_segment in {"nan", "none", "null"}:
        return ""
    if not path:
        return ""

    return urllib.parse.urlunparse((scheme, host, path, "", "", ""))


def _canonicalize_fb_pages_category_url(url: str) -> Optional[str]:
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 4 or parts[0].lower() != "pages" or parts[1].lower() != "category":
        return None
    if len(parts) > 4:
        tail = [part.lower() for part in parts[4:]]
        if len(tail) != 1 or tail[0] not in {"about", "posts", "photos"}:
            return None
    page = (parts[3] or "").strip()
    if not page or page.lower() in {"nan", "none", "null"}:
        return None
    page_name, sep, numeric_suffix = page.rpartition("-")
    if sep and numeric_suffix.isdigit() and len(numeric_suffix) >= 10:
        page = page_name.strip()
        if not page or page.lower() in {"nan", "none", "null"}:
            return None
    return urllib.parse.urlunparse(("https", "www.facebook.com", f"/{page}", "", "", ""))


def _canonicalize_fb_p_url(url: str) -> Optional[str]:
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "p":
        return None
    tail_parts = parts[2:]
    tail = [part.lower() for part in tail_parts]
    if tail:
        allowed_tail = {"about", "posts", "photos"}
        if len(tail) > 2:
            return None
        if not all(part.isdigit() or part in allowed_tail for part in tail):
            return None
        if len(tail) == 2 and (not tail[0].isdigit() or tail[1] not in allowed_tail):
            return None
    page = (parts[1] or "").strip()
    if not page or page.lower() in {"nan", "none", "null"}:
        return None
    numeric_id = ""
    if tail_parts:
        tail_id = (tail_parts[0] or "").strip()
        if tail_id.isdigit() and len(tail_id) >= 10:
            numeric_id = tail_id
    page_name, sep, numeric_suffix = page.rpartition("-")
    if sep and numeric_suffix.isdigit() and len(numeric_suffix) >= 10:
        page = page_name.strip()
        if not page or page.lower() in {"nan", "none", "null"}:
            return None
        numeric_id = numeric_suffix
    if numeric_id:
        return urllib.parse.urlunparse(("https", "www.facebook.com", f"/people/{page}/{numeric_id}", "", "", ""))
    return urllib.parse.urlunparse(("https", "www.facebook.com", f"/{page}", "", "", ""))


def _canonicalize_explicit_fb_entrypoint_url(url: str) -> Optional[str]:
    norm = _normalise_fb_url(url)
    if not norm:
        return None
    try:
        path_parts = [part for part in (urllib.parse.urlsplit(norm).path or "").split("/") if part]
    except Exception:
        path_parts = []
    if path_parts and path_parts[0].lower() in {"share", "photo.php", "permalink.php", "watch", "reel", "reels"}:
        return None
    for canonicalizer in (_canonicalize_fb_pages_category_url, _canonicalize_fb_p_url):
        rewritten = canonicalizer(norm)
        if rewritten:
            norm = _normalise_fb_url(rewritten) or rewritten
    return norm


def _canonicalize_and_dedupe_explicit_fb_urls(
    urls: Sequence[str], logger: LoggerFn = None, debug: bool = False
) -> List[str]:
    """Normalize and dedupe explicit FB URLs while preserving order."""

    before_count = len(urls or [])
    seen: Set[str] = set()
    canonical: List[str] = []

    for raw in urls or []:
        if _is_invalid_fb_value(raw):
            if debug and logger:
                _log(logger, f"[Night FB] Skipping invalid facebook_url value: {raw}")
            continue
        norm = _canonicalize_explicit_fb_entrypoint_url(raw)
        if not norm:
            continue
        try:
            parsed = urllib.parse.urlsplit(norm)
            path = parsed.path.rstrip("/") or "/"
            keep_query = parsed.query if parsed.path.lower() == "/profile.php" else ""
            parsed = parsed._replace(path=path, query=keep_query, fragment="")
            norm = urllib.parse.urlunsplit(parsed)
        except Exception:
            pass

        try:
            parsed_for_key = urllib.parse.urlsplit(norm)
            key = urllib.parse.urlunsplit(
                (
                    (parsed_for_key.scheme or "https").lower(),
                    (parsed_for_key.netloc or "").lower(),
                    parsed_for_key.path,
                    parsed_for_key.query,
                    "",
                )
            )
        except Exception:
            key = norm

        if key in seen:
            continue
        seen.add(key)
        canonical.append(norm)

    after_count = len(canonical)
    if debug and logger and before_count and before_count != after_count:
        _log(logger, f"[Night FB] Deduplicated explicit FB URLs: {before_count} -> {after_count}")

    return canonical


def _explicit_fb_row_value(row: Dict[str, str], field: str) -> str:
    for key, value in (row or {}).items():
        try:
            if str(key).lower() != field.lower():
                continue
        except Exception:
            continue
        try:
            import pandas as _pd  # type: ignore

            if _pd.isna(value):
                return ""
        except Exception:
            pass
        try:
            return str(value or "").strip()
        except Exception:
            return ""
    return ""


def _explicit_fb_row_lookup(row: Dict[str, str], field: str) -> Tuple[str, str]:
    for key, value in (row or {}).items():
        try:
            if str(key).lower() != field.lower():
                continue
        except Exception:
            continue
        try:
            import pandas as _pd  # type: ignore

            if _pd.isna(value):
                return str(key), ""
        except Exception:
            pass
        try:
            return str(key), str(value or "").strip()
        except Exception:
            return str(key), ""
    return "", ""


def _looks_like_explicit_fb_candidate(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("/"):
        return True
    if lowered in {"facebook", "facebook.com", "www.facebook.com", "m.facebook.com", "fb.com", "fb.me"}:
        return True
    return any(host in lowered for host in _EXPLICIT_FB_ALLOWED_HOSTS)


def _explicit_fb_prefilter_reason(candidate: str) -> str:
    cleaned = (candidate or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    elif not cleaned.startswith("http"):
        cleaned = "https://" + cleaned
    try:
        path = urllib.parse.urlparse(cleaned).path.lower()
    except Exception:
        return ""
    if not path:
        return ""
    if path.startswith("/r.php") or path.startswith("/login"):
        return "login_redirect_surface"
    if path.startswith("/share.php") or path.startswith("/dialog/"):
        return "share_surface"
    if path.startswith("/l.php"):
        return "link_shim_surface"
    return ""


def _explicit_fb_pre_scrape_guard_reason(url: str) -> Tuple[str, str]:
    raw_fb_url = (url or "").strip()
    if raw_fb_url.startswith("/"):
        raw_fb_url = "https://www.facebook.com" + raw_fb_url
    if _is_invalid_fb_value(raw_fb_url):
        return "invalid_placeholder", raw_fb_url or "<blank>"
    if _is_allowed_fb_share_entrypoint_url(raw_fb_url):
        return "", _normalise_fb_url(raw_fb_url) or raw_fb_url
    canonical_candidate_url = _canonicalize_explicit_fb_entrypoint_url(raw_fb_url)
    guard_input = canonical_candidate_url or raw_fb_url
    if not fb_is_allowed_profile_candidate_url(guard_input):
        return "shape_disallowed", raw_fb_url or "<blank>"
    candidate_url = canonical_candidate_url or _normalise_fb_url(raw_fb_url or "")
    if not candidate_url:
        return "normalize_failed", raw_fb_url or "<blank>"
    if _is_junk_fb_candidate(candidate_url):
        return "junk_surface", candidate_url
    url_lower = candidate_url.lower()
    if "/r.php" in url_lower or "/login" in url_lower or "/register" in url_lower:
        return "login_redirect", candidate_url
    return "", candidate_url or raw_fb_url


def _compact_explicit_fb_values(values: Sequence[str], limit: int = 2, width: int = 96) -> str:
    compacted: List[str] = []
    seen: Set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if len(text) > width:
            text = text[: width - 3] + "..."
        compacted.append(text)
    if not compacted:
        return ""
    if len(compacted) <= limit:
        return ",".join(compacted)
    return f"{','.join(compacted[:limit])},+{len(compacted) - limit}"


def classify_explicit_fb_intake(
    row: Dict[str, str],
    *,
    accepted_urls: Optional[Sequence[str]] = None,
) -> ExplicitFbIntakeDecision:
    accepted_urls = list(accepted_urls) if accepted_urls is not None else _canonicalize_and_dedupe_explicit_fb_urls(_extract_fb_urls_for_night_mode(row))

    source_fields_seen: List[str] = []
    source_fields_set: Set[str] = set()
    scanned_actual_fields: Set[str] = set()
    rejected_invalid: List[str] = []
    rejected_invalid_seen: Set[str] = set()
    rejected_guard: List[str] = []
    rejected_guard_seen: Set[str] = set()
    invalid_reason = ""
    guard_reason = ""
    accepted_sources_by_url: Dict[str, str] = {}

    for field in _EXPLICIT_FB_INTAKE_FIELDS:
        actual_field, raw = _explicit_fb_row_lookup(row, field)
        actual_field_key = (actual_field or field).lower()
        if actual_field and actual_field_key in scanned_actual_fields:
            continue
        if actual_field:
            scanned_actual_fields.add(actual_field_key)
        if not raw:
            continue
        parts = _FB_SPLIT_PATTERN.split(raw)
        is_direct_fb_field = actual_field_key in {"facebook_url", "facebook url"}
        for part in parts:
            candidate = str(part or "").strip()
            if not candidate or (not is_direct_fb_field and not _looks_like_explicit_fb_candidate(candidate)):
                continue
            source_label = actual_field or field
            if source_label not in source_fields_set:
                source_fields_seen.append(source_label)
                source_fields_set.add(source_label)
            if not _is_valid_fb_url_value(candidate):
                if candidate not in rejected_invalid_seen:
                    rejected_invalid.append(candidate)
                    rejected_invalid_seen.add(candidate)
                if not invalid_reason:
                    invalid_reason = "invalid_placeholder_or_malformed"
                continue
            if not any(host in candidate.lower() for host in _EXPLICIT_FB_ALLOWED_HOSTS):
                if candidate not in rejected_invalid_seen:
                    rejected_invalid.append(candidate)
                    rejected_invalid_seen.add(candidate)
                if not invalid_reason:
                    invalid_reason = "missing_fb_host"
                continue
            prefilter_reason = _explicit_fb_prefilter_reason(candidate)
            if prefilter_reason:
                if candidate not in rejected_guard_seen:
                    rejected_guard.append(candidate)
                    rejected_guard_seen.add(candidate)
                if not guard_reason:
                    guard_reason = prefilter_reason
                continue
            canonical = _normalise_fb_url(candidate)
            if not canonical:
                if candidate not in rejected_invalid_seen:
                    rejected_invalid.append(candidate)
                    rejected_invalid_seen.add(candidate)
                if not invalid_reason:
                    invalid_reason = "canonicalization_dropped"
                continue
            canonical = _canonicalize_explicit_fb_entrypoint_url(canonical)
            if not canonical:
                if candidate not in rejected_invalid_seen:
                    rejected_invalid.append(candidate)
                    rejected_invalid_seen.add(candidate)
                if not invalid_reason:
                    invalid_reason = "canonicalization_dropped"
                continue
            accepted_sources_by_url.setdefault(canonical, source_label)
            guard_reject_reason, guard_sample = _explicit_fb_pre_scrape_guard_reason(canonical)
            if guard_reject_reason:
                if guard_sample not in rejected_guard_seen:
                    rejected_guard.append(guard_sample)
                    rejected_guard_seen.add(guard_sample)
                if not guard_reason:
                    guard_reason = guard_reject_reason

    source_fields = []
    source_seen: Set[str] = set()
    for url in accepted_urls:
        source = accepted_sources_by_url.get(url, "")
        if source and source not in source_seen:
            source_fields.append(source)
            source_seen.add(source)
    for field in source_fields_seen:
        if field not in source_seen:
            source_fields.append(field)
            source_seen.add(field)

    canonical_value_present = bool(canonicalize_facebook_url(_explicit_fb_row_value(row, "Facebook_URL")))
    promoted_url, promotion_source = ensure_canonical_facebook_url(row, set_row=False)
    promotion_expected_missing_canonical = bool(promoted_url and not canonical_value_present)

    if accepted_urls:
        outcome = "attempt"
    elif rejected_invalid:
        outcome = "reject_invalid"
    elif rejected_guard:
        outcome = "reject_guard"
    elif promotion_expected_missing_canonical:
        outcome = "promotion_expected_missing_canonical"
    else:
        outcome = "no_explicit_url"

    message = outcome
    if outcome == "attempt":
        message = "explicit URL present and queued for PASS A"
    elif outcome == "reject_invalid":
        message = "explicit URL present but rejected as invalid"
    elif outcome == "reject_guard":
        message = "explicit URL present but filtered by guard"
    elif outcome == "promotion_expected_missing_canonical":
        message = "promotion source had FB URL but canonical field was blank at intake"
    elif outcome == "no_explicit_url":
        message = "no explicit FB URL detected on row"

    return ExplicitFbIntakeDecision(
        outcome=outcome,
        source_fields=source_fields,
        accepted_urls=list(accepted_urls),
        rejected_invalid=rejected_invalid,
        rejected_guard=rejected_guard,
        promotion_expected_missing_canonical=promotion_expected_missing_canonical,
        canonical_value_present=canonical_value_present,
        promotion_source=promotion_source or "",
        invalid_reason=invalid_reason,
        guard_reason=guard_reason,
        message=message,
    )


def explicit_fb_entrypoint_urls_for_row(
    row: Dict[str, str],
    *,
    accepted_urls: Optional[Sequence[str]] = None,
) -> List[str]:
    urls = list(accepted_urls) if accepted_urls is not None else _canonicalize_and_dedupe_explicit_fb_urls(_extract_fb_urls_for_night_mode(row))
    routed: List[str] = []
    seen: Set[str] = set()
    for url in urls:
        guard_reason, _ = _explicit_fb_pre_scrape_guard_reason(url)
        if guard_reason:
            continue
        key = _normalise_fb_url(url) or str(url or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        routed.append(url)
    return routed


def _log_explicit_fb_intake(logger: LoggerFn, artist_name: str, decision: ExplicitFbIntakeDecision) -> None:
    parts = [
        '[Night FB][Explicit Intake]',
        f'artist="{artist_name or "<unknown>"}"',
        f'outcome="{decision.outcome}"',
    ]
    if decision.source_fields:
        parts.append(f'source="{_compact_explicit_fb_values(decision.source_fields, limit=3, width=48)}"')
    if decision.accepted_urls:
        parts.append(f'urls="{_compact_explicit_fb_values(decision.accepted_urls)}"')
    if decision.rejected_invalid:
        parts.append(f'invalid="{_compact_explicit_fb_values(decision.rejected_invalid, limit=1)}"')
    if decision.invalid_reason:
        parts.append(f'invalid_reason="{decision.invalid_reason}"')
    if decision.rejected_guard:
        parts.append(f'guard="{_compact_explicit_fb_values(decision.rejected_guard, limit=1)}"')
    if decision.guard_reason:
        parts.append(f'guard_reason="{decision.guard_reason}"')
    if decision.canonical_value_present:
        parts.append('canonical_field="present"')
    elif decision.promotion_expected_missing_canonical:
        parts.append('canonical_field="blank"')
    if decision.promotion_expected_missing_canonical:
        parts.append('promotion_expected="1"')
        if decision.promotion_source:
            parts.append(f'promotion_source="{decision.promotion_source}"')
    _log(logger, " ".join(parts))


def _purge_wdm_cache(driver_path: Optional[str]) -> None:
    """
    Remove webdriver_manager's cache folder for a given driver path to self-heal bad downloads.
    """
    if not driver_path:
        return
    try:
        path = Path(driver_path).resolve()
        cache_dir = path.parent.parent
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:
        pass


_ACTIVE_DRIVERS = weakref.WeakSet()


def _register_driver_cleanup(driver) -> None:
    try:
        _ACTIVE_DRIVERS.add(driver)
    except Exception:
        pass


def _shutdown_all_drivers() -> None:
    for drv in list(_ACTIVE_DRIVERS):
        try:
            drv.quit()
        except Exception:
            pass
    _ACTIVE_DRIVERS.clear()


atexit.register(_shutdown_all_drivers)


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() not in ("0", "false", "no", "off", "")


def _is_linux() -> bool:
    try:
        return sys.platform.startswith("linux")
    except Exception:
        return False


def _display_env_present() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _binary_version(binary_path: str) -> Optional[str]:
    try:
        out = subprocess.check_output([binary_path, "--version"], stderr=subprocess.STDOUT, text=True, timeout=5)
        return (out or "").strip()
    except Exception:
        return None


def _detect_chrome_binary() -> Tuple[Optional[str], Optional[str]]:
    """Best-effort detection of Chrome binary path + version (platform-aware, non-fatal)."""
    candidates = []
    env_override = os.environ.get("CHROME_BINARY") or os.environ.get("GOOGLE_CHROME_BIN") or os.environ.get("GOOGLE_CHROME_SHIM")
    if env_override:
        candidates.append(env_override)

    # Common macOS locations first (since this code runs on macOS).
    candidates.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
        ]
    )

    # Standard CLI binaries (Linux-style or custom PATH entries).
    candidates.extend(
        [
            "google-chrome",
            "google-chrome-stable",
            "chrome",
            "chromium",
            "chromium-browser",
        ]
    )

    for candidate in candidates:
        if not candidate:
            continue
        path = candidate
        if not os.path.isabs(candidate):
            path = shutil.which(candidate) or candidate
        if not os.path.exists(path):
            continue
        return path, _binary_version(path)
    return None, None


def _profile_dir_state(profile_dir: str) -> Tuple[bool, List[str]]:
    exists = os.path.isdir(profile_dir)
    lock_files = [
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "Lock",
        "lockfile",
    ]
    present = [name for name in lock_files if os.path.exists(os.path.join(profile_dir, name))]
    return exists, present


def _chromedriver_version(driver_path: Optional[str]) -> Optional[str]:
    if not driver_path:
        return "<selenium_manager>"
    try:
        if not os.path.exists(driver_path):
            return "<selenium_manager>"
        out = subprocess.check_output([driver_path, "--version"], stderr=subprocess.STDOUT, text=True, timeout=5)
        return (out or "").strip()
    except Exception:
        return None


def _clone_chrome_options(chrome_options: ChromeOptions, override_profile_dir: Optional[str] = None) -> ChromeOptions:
    """Lightweight clone helper to swap profile dirs without mutating the original."""
    new_opts = ChromeOptions()
    try:
        for arg in getattr(chrome_options, "arguments", []) or []:
            if override_profile_dir and (arg.startswith("--user-data-dir=") or arg.startswith("--profile-directory=")):
                continue
            new_opts.add_argument(arg)
    except Exception:
        pass

    if override_profile_dir:
        new_opts.add_argument(f"--user-data-dir={override_profile_dir}")
        new_opts.add_argument("--profile-directory=Default")

    try:
        experimental = getattr(chrome_options, "_experimental_options", {}) or {}
        for key, value in experimental.items():
            new_opts.add_experimental_option(key, value)
    except Exception:
        pass

    try:
        new_opts.page_load_strategy = chrome_options.page_load_strategy
    except Exception:
        pass
    return new_opts


def _chrome_images_pref(chrome_options: ChromeOptions) -> Tuple[str, Optional[Any]]:
    """Inspect the effective Chrome image-loading pref carried in experimental options."""
    try:
        experimental = getattr(chrome_options, "_experimental_options", {}) or {}
        prefs = experimental.get("prefs", {}) or {}
    except Exception:
        return "inherit", None

    managed_key = "profile.managed_default_content_settings.images"
    default_key = "profile.default_content_setting_values.images"
    if managed_key in prefs:
        return managed_key, prefs.get(managed_key)
    if default_key in prefs:
        return default_key, prefs.get(default_key)
    return "inherit", None


def _chrome_images_policy_label(chrome_options: ChromeOptions) -> str:
    pref_key, pref_value = _chrome_images_pref(chrome_options)
    if pref_key == "inherit":
        return "inherit"
    scope = "managed" if "managed" in pref_key else "default"
    if pref_value == 1:
        state = "allow"
    elif pref_value == 2:
        state = "block"
    else:
        state = f"value_{pref_value}"
    return f"{scope}_{state}"


_night_fb_profile_dir_logged: bool = False


@dataclass(frozen=True)
class NightFBSessionSource:
    mode: str
    reason: str
    can_probe: bool
    profile_dir: str = ""
    explicit_profile: bool = False
    has_credentials: bool = False
    uses_profile: bool = False


@dataclass(frozen=True)
class NightFBSessionDecision:
    state: str
    reason: str
    authenticated: bool
    usable: bool
    checkpointed: bool = False
    disabled_for_run: bool = False
    session_invalid: bool = False


@dataclass
class NightFBRunState:
    session_source: NightFBSessionSource
    session: Optional["NightPersistentFacebookSession"] = None
    authenticated: bool = False
    latest_session_decision: Optional[NightFBSessionDecision] = None
    disabled_for_run: bool = False
    disable_reason: str = ""
    checkpointed: bool = False
    session_unhealthy: bool = False
    session_invalid: bool = False
    reusable: bool = False
    session_owner: str = ""
    session_warmup_complete: bool = False
    trust_score: int = 0
    search_disabled_for_run: bool = False
    search_disable_reason: str = ""


def _is_profile_session_sentinel(value: str) -> bool:
    return str(value or "").strip().lower() in _PROFILE_SESSION_SENTINELS


def _normalize_profile_path(path: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path))
    try:
        return str(Path(expanded).resolve())
    except Exception:
        return os.path.abspath(expanded)


def _infer_night_fb_profile_dir() -> str:
    """Infer the Night FB profile directory without reading the env."""
    base_file = Path(__file__).resolve()

    def _profile_dir_candidates(from_file: Path) -> List[str]:
        """Generate plausible profile dir candidates while walking ancestors."""
        candidates: List[str] = []
        seen: set[str] = set()
        for ancestor in from_file.parents:
            # Look for sibling "Lead Machine Code" at each level.
            sibling_root = ancestor.parent / "Lead Machine Code"
            cand = sibling_root / "night_fb_profile"
            cand_str = str(cand)
            if cand_str not in seen:
                candidates.append(cand_str)
                seen.add(cand_str)
            # If we're already inside "Lead Machine Code", also consider its own night_fb_profile.
            if ancestor.name == "Lead Machine Code":
                self_cand = ancestor / "night_fb_profile"
                self_cand_str = str(self_cand)
                if self_cand_str not in seen:
                    candidates.append(self_cand_str)
                    seen.add(self_cand_str)
        return candidates

    def _pick_existing_profile_dir(candidates: Iterable[str]) -> Tuple[Optional[str], List[str]]:
        existing = [c for c in candidates if os.path.isdir(c)]
        return (existing[0] if existing else None, existing)

    candidates = _profile_dir_candidates(base_file)
    selected, _existing_candidates = _pick_existing_profile_dir(candidates)

    if selected:
        return selected

    # Fallback: local directory beside this file.
    return str(base_file.parent / "night_fb_profile")


def _resolve_night_fb_profile_dir(logger: LoggerFn = None) -> str:
    """Resolve and log the Night FB Chrome profile directory once per run."""
    global _night_fb_profile_dir_logged
    env_raw = os.environ.get("NIGHT_FB_PROFILE_DIR") or ""

    if env_raw.strip():
        chosen_raw = env_raw.strip()
        env_set = True
    else:
        chosen_raw = _infer_night_fb_profile_dir()
        env_set = False

    resolved_path = _normalize_profile_path(chosen_raw)
    try:
        os.makedirs(resolved_path, exist_ok=True)
    except Exception:
        pass

    if not _night_fb_profile_dir_logged:
        if env_set:
            _log(logger, f"[Night FB] Using Chrome profile dir: {resolved_path}")
        else:
            _log(logger, f"[Night FB][WARN] NIGHT_FB_PROFILE_DIR not set; using inferred path: {resolved_path}")
        _night_fb_profile_dir_logged = True

    return resolved_path


def _night_fb_profile_dir_self_check(logger: LoggerFn = None) -> str:
    """Debug helper to print resolved profile dir + Default/lock presence."""
    profile_dir = _resolve_night_fb_profile_dir(logger)
    default_exists = os.path.isdir(os.path.join(profile_dir, "Default"))
    singleton_lock = os.path.exists(os.path.join(profile_dir, "SingletonLock"))
    _log(
        logger,
        f"[Night FB] Profile dir self-check: dir={profile_dir} Default_exists={default_exists} SingletonLock_present={singleton_lock}",
    )
    return profile_dir


def normalize_night_fb_session_source(username: str = "", password: str = "") -> NightFBSessionSource:
    username = str(username or "").strip()
    password = str(password or "").strip()
    username_is_sentinel = _is_profile_session_sentinel(username)
    password_is_sentinel = _is_profile_session_sentinel(password)
    explicit_profile_raw = str(os.environ.get("NIGHT_FB_PROFILE_DIR", "") or "").strip()
    explicit_profile_dir = _normalize_profile_path(explicit_profile_raw) if explicit_profile_raw else ""
    explicit_profile_exists = bool(explicit_profile_dir and os.path.isdir(explicit_profile_dir))
    has_username = bool(username)
    has_password = bool(password)
    has_credentials = bool(
        username
        and password
        and not username_is_sentinel
        and not password_is_sentinel
    )
    sentinel_only = (
        (username_is_sentinel or not username)
        and (password_is_sentinel or not password)
        and (username_is_sentinel or password_is_sentinel)
    )

    if (username_is_sentinel and password and not password_is_sentinel) or (
        password_is_sentinel and username and not username_is_sentinel
    ):
        return NightFBSessionSource(
            mode="invalid",
            reason="mixed_profile_session_credentials",
            can_probe=False,
            profile_dir=explicit_profile_dir,
            explicit_profile=bool(explicit_profile_raw),
            has_credentials=False,
            uses_profile=bool(explicit_profile_raw),
        )

    if sentinel_only:
        return NightFBSessionSource(
            mode="profile",
            reason="profile_session",
            can_probe=True,
            profile_dir=_resolve_night_fb_profile_dir(None),
            explicit_profile=bool(explicit_profile_raw),
            has_credentials=False,
            uses_profile=True,
        )

    if (has_username and not has_password) or (has_password and not has_username):
        return NightFBSessionSource(
            mode="invalid",
            reason="partial_credentials",
            can_probe=False,
            profile_dir=explicit_profile_dir,
            explicit_profile=bool(explicit_profile_raw),
            has_credentials=False,
            uses_profile=explicit_profile_exists,
        )

    if explicit_profile_exists:
        return NightFBSessionSource(
            mode="profile",
            reason="profile_dir",
            can_probe=True,
            profile_dir=explicit_profile_dir,
            explicit_profile=True,
            has_credentials=has_credentials,
            uses_profile=True,
        )

    if has_credentials:
        return NightFBSessionSource(
            mode="credentials",
            reason="credentials",
            can_probe=True,
            profile_dir=_resolve_night_fb_profile_dir(None),
            explicit_profile=bool(explicit_profile_raw),
            has_credentials=True,
            uses_profile=True,
        )

    if explicit_profile_raw and not explicit_profile_exists:
        return NightFBSessionSource(
            mode="invalid",
            reason="missing_profile_dir",
            can_probe=False,
            profile_dir=explicit_profile_dir,
            explicit_profile=True,
            has_credentials=False,
            uses_profile=False,
        )

    return NightFBSessionSource(
        mode="none",
        reason="missing_session_source",
        can_probe=False,
        profile_dir="",
        explicit_profile=False,
        has_credentials=False,
        uses_profile=False,
    )


def create_night_fb_run_state(username: str = "", password: str = "") -> NightFBRunState:
    return NightFBRunState(session_source=normalize_night_fb_session_source(username, password))


def update_night_fb_run_state(
    run_state: Optional[NightFBRunState],
    decision: NightFBSessionDecision,
    *,
    owner: str = "",
) -> NightFBSessionDecision:
    if run_state is None:
        return decision
    run_state.latest_session_decision = decision
    run_state.authenticated = bool(decision.authenticated and decision.usable)
    run_state.checkpointed = bool(decision.checkpointed)
    run_state.session_invalid = bool(decision.session_invalid)
    run_state.session_unhealthy = bool(decision.checkpointed or (decision.authenticated and not decision.usable))
    if owner:
        run_state.session_owner = owner
    run_state.reusable = bool(
        run_state.session
        and decision.authenticated
        and decision.usable
        and not run_state.disabled_for_run
        and not run_state.session_invalid
    )
    return decision


def disable_night_fb_run_state(
    run_state: Optional[NightFBRunState],
    reason: str,
    *,
    session_invalid: bool = False,
    checkpointed: bool = False,
    session_unhealthy: bool = True,
    close_session: bool = False,
) -> str:
    reason_code = str(reason or "").strip() or ("session_invalid" if session_invalid else "not_authenticated")
    if run_state is None:
        return reason_code
    run_state.disabled_for_run = True
    run_state.disable_reason = reason_code
    run_state.checkpointed = bool(run_state.checkpointed or checkpointed or reason_code == "checkpoint")
    run_state.session_invalid = bool(run_state.session_invalid or session_invalid)
    run_state.session_unhealthy = bool(
        run_state.session_unhealthy or session_unhealthy or run_state.checkpointed or run_state.session_invalid
    )
    run_state.authenticated = False
    run_state.reusable = False
    if close_session and run_state.session is not None:
        try:
            run_state.session.close()
        except Exception:
            pass
        run_state.session = None
    return reason_code


def disable_night_fb_search_run_state(
    run_state: Optional[NightFBRunState],
    reason: str,
) -> str:
    reason_code = str(reason or "").strip() or "session_unhealthy"
    if run_state is None:
        return reason_code
    run_state.search_disabled_for_run = True
    run_state.search_disable_reason = reason_code
    return reason_code


def close_night_fb_run_state(run_state: Optional[NightFBRunState]) -> None:
    if run_state is None:
        return
    if run_state.session is not None:
        try:
            run_state.session.close()
        except Exception:
            pass
    run_state.session = None
    run_state.authenticated = False
    run_state.reusable = False
    run_state.session_owner = ""


def _night_fb_page_health_snapshot(
    current_url: str,
    page_html: Optional[str],
    *,
    search_miss_reason: str = "",
    warning_reason: str = "",
) -> Dict[str, Any]:
    resolved_url = str(current_url or "").strip()
    page_source = page_html or ""
    auth_surface = _classify_fb_auth_surface_from_page(resolved_url, page_source)
    warning = warning_reason or (_looks_like_fb_warning_or_block(page_source, resolved_url) or "")
    captcha = auth_surface == "captcha"
    checkpoint = auth_surface == "checkpoint"
    login_wall = auth_surface in {"redirect_login", "recover", "two_factor", "consent"}
    degraded_search = str(search_miss_reason or "").strip()
    return {
        "url": resolved_url,
        "auth_surface": auth_surface,
        "warning_reason": warning,
        "search_miss_reason": degraded_search,
        "captcha": captcha,
        "checkpoint": checkpoint,
        "login_wall": login_wall,
    }


def _build_night_fb_session_decision(
    *,
    authenticated: bool,
    reason: str = "",
    session_invalid: bool = False,
) -> NightFBSessionDecision:
    reason = str(reason or "").strip().lower()
    checkpointed = "checkpoint" in reason
    if session_invalid:
        return NightFBSessionDecision(
            state="session_invalid",
            reason=reason or "session_invalid",
            authenticated=False,
            usable=False,
            checkpointed=False,
            disabled_for_run=True,
            session_invalid=True,
        )
    if authenticated and not reason:
        return NightFBSessionDecision(
            state="authenticated_and_usable",
            reason="authenticated",
            authenticated=True,
            usable=True,
        )
    if authenticated and checkpointed:
        return NightFBSessionDecision(
            state="authenticated_but_checkpointed",
            reason=reason or "checkpoint",
            authenticated=True,
            usable=False,
            checkpointed=True,
        )
    if authenticated:
        return NightFBSessionDecision(
            state="disabled_for_run",
            reason=reason or "session_unhealthy",
            authenticated=True,
            usable=False,
            checkpointed=checkpointed,
            disabled_for_run=True,
        )
    if checkpointed:
        return NightFBSessionDecision(
            state="disabled_for_run",
            reason=reason or "checkpoint",
            authenticated=False,
            usable=False,
            checkpointed=True,
            disabled_for_run=True,
        )
    return NightFBSessionDecision(
        state="unauthenticated",
        reason=reason or "not_authenticated",
        authenticated=False,
        usable=False,
    )


def probe_night_fb_session_decision(
    driver,
    *,
    visit_home: bool = True,
) -> NightFBSessionDecision:
    if driver is None:
        return NightFBSessionDecision(
            state="disabled_for_run",
            reason="no_driver",
            authenticated=False,
            usable=False,
            disabled_for_run=True,
        )
    try:
        if visit_home:
            driver.get("https://www.facebook.com/")
        current_url = (getattr(driver, "current_url", "") or "").lower()
        page_source = (getattr(driver, "page_source", "") or "").lower()
        authenticated = _is_driver_authenticated(driver)
    except Exception as exc:
        return _build_night_fb_session_decision(
            authenticated=False,
            reason="session_invalid" if _is_session_death_exc(exc) else "driver_error",
            session_invalid=_is_session_death_exc(exc),
        )
    auth_surface = _classify_fb_auth_surface_from_page(current_url, page_source)
    if authenticated and auth_surface and not _classify_fb_auth_surface_from_url(current_url):
        try:
            time.sleep(1.0)
            current_url = (getattr(driver, "current_url", "") or "").lower()
            page_source = (getattr(driver, "page_source", "") or "").lower()
        except Exception:
            pass
        auth_surface = _classify_fb_auth_surface_from_page(current_url, page_source)
    return _build_night_fb_session_decision(
        authenticated=authenticated,
        reason=auth_surface,
    )

# Manual smoke test (run outside prod jobs):
# NIGHT_FB_CHROMEDRIVER_LOG=/tmp/night_fb_chromedriver.log python3 - <<'PY'
# from night_mode_fb import _night_fb_profile_dir_self_check, _create_fb_driver_night_mode
# _night_fb_profile_dir_self_check(print)
# try:
#     d = _create_fb_driver_night_mode(headless=False, logger=print)
#     print("driver_ready", bool(d))
# finally:
#     try:
#         d.quit()
#     except Exception:
#         pass
# PY


def _has_cookie(driver, name: str) -> bool:
    try:
        return bool(driver.get_cookie(name))
    except Exception:
        return False


def _is_driver_authenticated(driver) -> bool:
    """
    Treat presence of c_user cookie as authenticated FB session.
    """
    return _has_cookie(driver, "c_user")


def _is_fb_hard_consent_url(current_url: str) -> bool:
    parsed = urllib.parse.urlparse((current_url or "").lower())
    path = parsed.path or ""
    return (
        path == "/consent"
        or path == "/consent.php"
        or path == "/legal/consent"
        or path.startswith("/consent/")
        or path.startswith("/legal/consent/")
    )


def _classify_fb_auth_surface_from_url(current_url: str) -> str:
    current_url = (current_url or "").lower()
    if _is_fb_hard_consent_url(current_url):
        return "consent"
    bad_url_tokens = (
        ("login", "redirect_login"),
        ("checkpoint", "checkpoint"),
        ("recover", "recover"),
        ("two_factor", "two_factor"),
        ("two-factor", "two_factor"),
        ("mfa", "two_factor"),
    )
    for token, reason in bad_url_tokens:
        if token in current_url:
            return reason
    return ""


def _page_contains_all(text: str, tokens: Tuple[str, ...]) -> bool:
    lower = (text or "").lower()
    return all(token in lower for token in tokens)


def _classify_fb_auth_surface_from_page(current_url: str, page_source: str) -> str:
    url_reason = _classify_fb_auth_surface_from_url(current_url)
    if url_reason:
        return url_reason

    page_source = (page_source or "").lower()
    html_reason_patterns = (
        (("security check",), "checkpoint"),
        (("confirm it's you",), "checkpoint"),
        (("confirm it’s you",), "checkpoint"),
        (("review recent login",), "checkpoint"),
        (("help us confirm", "account"), "checkpoint"),
        (("checkpoint/",), "checkpoint"),
        (("checkpoint?next",), "checkpoint"),
        (("before you continue to facebook",), "consent"),
        (("please review and accept",), "consent"),
        (("accept cookies to continue",), "consent"),
        (("manage your data settings",), "consent"),
        (("agree and continue",), "consent"),
        (("log in to facebook",), "redirect_login"),
        (("email or phone", "password"), "redirect_login"),
        (("two-factor",), "two_factor"),
        (("help us confirm", "captcha"), "captcha"),
        (("complete the captcha",), "captcha"),
        (("verify you are human",), "captcha"),
        (("enter the characters you see",), "captcha"),
        (("i'm not a robot",), "captcha"),
        (("i’m not a robot",), "captcha"),
    )
    for tokens, reason in html_reason_patterns:
        if _page_contains_all(page_source, tokens):
            return reason

    return ""


def _classify_fb_auth_surface(driver) -> str:
    """Classify the current FB landing surface for early auth/session diagnostics."""
    try:
        current_url = getattr(driver, "current_url", "") or ""
    except Exception:
        current_url = ""
    try:
        page_source = getattr(driver, "page_source", "") or ""
    except Exception:
        page_source = ""
    return _classify_fb_auth_surface_from_page(current_url, page_source)


def _is_fb_content_unavailable_page(page_html: Optional[str]) -> bool:
    lower_html = (page_html or "").lower()
    not_found_phrases = (
        "page isn\u2019t available",
        "page isn't available",
        "content isn't available",
        "not available right now",
    )
    return any(phrase in lower_html for phrase in not_found_phrases)


def _explicit_fb_render_state_invalid_reason(
    page_html: Optional[str],
    *,
    resolved_url: str = "",
    rendered_text: str = "",
    anchor_values: Optional[Sequence[str]] = None,
) -> str:
    combined_text = "\n".join(
        value for value in (str(page_html or ""), str(rendered_text or "")) if value
    )
    if _is_fb_content_unavailable_page(combined_text):
        return "content_unavailable"

    anchor_values = [str(value or "").strip() for value in (anchor_values or []) if str(value or "").strip()]
    if anchor_values:
        return ""

    html_text = str(page_html or "")
    rendered_text = str(rendered_text or "").strip()
    if rendered_text:
        return ""
    if "mailto:" in html_text.lower() or EMAIL_REGEX.search(html_text):
        return ""

    soup = BeautifulSoup(html_text, "html.parser") if html_text else None
    if soup and _pick_fb_contact_link(soup, resolved_url or ""):
        return ""

    dom_text = ""
    if soup:
        try:
            dom_text = " ".join(soup.stripped_strings)
        except Exception:
            dom_text = ""

    if len(dom_text) < 24:
        return "empty_shell"
    return ""


def _session_looks_healthy(driver) -> Tuple[bool, str]:
    """
    Quick FB session health probe to catch login/verification walls.
    Returns (healthy, reason_code).
    """
    try:
        driver.get("https://www.facebook.com/")
        time.sleep(0.6)
        _ = (getattr(driver, "current_url", "") or "").lower()
        _ = (getattr(driver, "page_source", "") or "").lower()
        time.sleep(0.4)
    except Exception:
        return False, "exception"

    reason = _classify_fb_auth_surface(driver)
    if reason:
        return False, reason

    return True, ""


def _is_session_death_exc(exc: BaseException) -> bool:
    """Lightweight detector for dead Selenium sessions/windows."""
    try:
        msg = (str(exc) or "").lower()
    except Exception:
        return False
    death_tokens = (
        "no such window",
        "web view not found",
        "invalid session id",
        "disconnected",
        "not connected to devtools",
        "chrome not reachable",
        "target window already closed",
        "session deleted because of page crash",
        "browser has disconnected",
    )
    return any(tok in msg for tok in death_tokens)


def _looks_like_fb_warning_or_block(html: Optional[str], url: str = "") -> Optional[str]:
    """
    Conservative detector for FB warning/interstitial pages that should trip a run-level breaker.
    Returns a short reason string or None.
    """
    if not html:
        return None
    lower = (html or "").lower()
    patterns = (
        ("suspicious", "activity"),
        ("temporarily blocked",),
        ("you're blocked",),
        ("you’re blocked",),
        ("try again later",),
        ("security check",),
        ("confirm it's you",),
        ("confirm it’s you",),
        ("unusual activity",),
        ("help us confirm", "captcha"),
        ("help us confirm", "security"),
    )
    for parts in patterns:
        if all(p in lower for p in parts):
            return "warning_interstitial"
    return None


def _harvest_search_candidates_v2(
    driver,
    logger: LoggerFn = None,
    search_name: str = "",
    session_unhealthy: Optional[bool] = None,
    session_reason: str = "",
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List["facebook_enrich.FbCandidate"]:
    """
    Resilient FB search candidate harvester (feature-flagged).
    - Prefers feed container, falls back to search results, then main.
    - Waits for usable anchors, retries with small scrolls, applies existing URL gate.
    - Returns candidates enriched via existing parser but scoped to chosen container URLs.
    """
    if driver is None:
        return []

    session_unhealthy_flag = 1 if session_unhealthy else 0
    session_reason_clean = (session_reason or "").strip()
    debug_dom_gate = bool(int(os.getenv("FB_DEBUG_DOM_GATE", "0") or "0"))
    role_link_prefilter = bool(int(os.getenv("FB_ROLE_LINK_PREFILTER", "1") or "1"))
    attr_scan_allowed = debug_dom_gate or bool(int(os.getenv("FB_DOM_ATTR_SCAN", "0") or "0"))
    attr_scan_nodes = int(os.getenv("FB_DOM_ATTR_SCAN_N", "150") or "150")
    attr_scan_max = int(os.getenv("FB_DOM_ATTR_SCAN_MAX", "50") or "50")
    low_yield_threshold = 3
    run_dir_env = os.getenv("RUN_DIR") or os.getenv("NIGHT_RUN_DIR")
    try:
        page_html = getattr(driver, "page_source", "") or ""
    except Exception:
        page_html = ""

    def _shared_zero_result_fallback() -> List["facebook_enrich.FbCandidate"]:
        if not page_html:
            return []
        shared_candidates = _parse_search_candidates(page_html, logger=logger, search_name=search_name)
        if shared_candidates:
            _log(
                logger,
                f"[Night FB][DOM Gate V2] zero_result_shared_fallback=1 recovered_candidates={len(shared_candidates)} "
                f"search_name='{search_name or ''}'",
            )
        return shared_candidates

    try:
        selector_order = [
            'div[role="main"] div[role="feed"]',
            'div[role="main"] div[aria-label="Search results"]',
            'div[aria-label="Search results"]',
            'div[role="main"] section[aria-label*="Search results"]',
            'div[role="main"] [data-pagelet^="SearchResults"]',
            'div[role="main"] div[aria-label*="Search results"]',
            'div[role="main"] div[role="article"]',
            'div[role="main"]',
        ]
        feed_selector = selector_order[0]
        search_selector = selector_order[2]
        main_selector = selector_order[-1]

        wait_hit = "timeout"
        scroll_retries = 0

        def _wait_condition(drv):
            try:
                feed_el = _find_first(drv, feed_selector)
                search_el = _find_first(drv, search_selector)
                usable_feed = _count_usable(feed_el)
                usable_search = _count_usable(search_el)
                if usable_feed > 0:
                    return "feed"
                if usable_search > 0:
                    return "search_results"
                return False
            except StaleElementReferenceException:
                return False

        wait_seconds = random.uniform(8.0, 12.0)
        try:
            wait_hit = WebDriverWait(driver, wait_seconds).until(_wait_condition)
        except TimeoutException:
            wait_hit = "timeout"
        except Exception:
            wait_hit = "timeout"

        def _collect_selector_stats() -> List[Dict[str, Any]]:
            stats: List[Dict[str, Any]] = []
            for sel in selector_order:
                containers = _find_all(driver, sel)
                container = containers[0] if containers else None
                data = _collect_container_candidates_v2(container, apply_role_link_prefilter=role_link_prefilter)
                data.update(
                    {
                        "selector": sel,
                        "containers_found": len(containers),
                        "container": container,
                    }
                )
                stats.append(data)
            return stats

        def _refresh_counts():
            selector_stats = _collect_selector_stats()
            selector_index = {entry.get("selector"): entry for entry in selector_stats}
            empty_entry = {
                "container": None,
                "hrefs": [],
                "usable_count": 0,
                "anchors_in_scope": 0,
                "role_links_in_scope": 0,
                "links_in_scope_total": 0,
                "containers_found": 0,
            }
            feed_data = selector_index.get(feed_selector, empty_entry)
            search_data = selector_index.get(search_selector, empty_entry)

            return {
                "selector_stats": selector_stats,
                "feed_container": feed_data.get("container"),
                "search_container": search_data.get("container"),
                "feed_hrefs": feed_data.get("hrefs", []),
                "search_hrefs": search_data.get("hrefs", []),
                "usable_feed": feed_data.get("usable_count", 0),
                "usable_search": search_data.get("usable_count", 0),
                "anchors_in_scope_feed": feed_data.get("anchors_in_scope", 0),
                "anchors_in_scope_search": search_data.get("anchors_in_scope", 0),
                "links_in_scope_total_feed": feed_data.get("links_in_scope_total", 0),
                "links_in_scope_total_search": search_data.get("links_in_scope_total", 0),
                "containers_found_feed": feed_data.get("containers_found", 0),
                "containers_found_search": search_data.get("containers_found", 0),
            }

        counts = _refresh_counts()
        overlay_present = 1 if _has_checkpoint_overlay(driver) else 0

        waited_for_population = False
        anchor_wait_meta: Dict[str, Any] = {}
        if counts["anchors_in_scope_search"] == 0 and counts["containers_found_search"] > 0:
            wait_result = _wait_for_anchor_population(
                driver,
                search_selector,
                min_anchors=2,
                timeout=float(os.getenv("FB_ANCHOR_WAIT_S", 6)),
                poll_seconds=0.5,
                logger=logger,
                context={"path": "search", "artist": search_name},
            )
            if isinstance(wait_result, tuple):
                success, before_ct, after_ct, waited_ms = wait_result
                anchor_wait_meta = {"waited_ms": waited_ms, "anchors_before": before_ct, "anchors_after": after_ct}
            else:
                success = wait_result
            if success:
                counts = _refresh_counts()
            waited_for_population = waited_for_population or success

        if counts["anchors_in_scope_feed"] == 0 and counts["containers_found_feed"] > 0:
            wait_result = _wait_for_anchor_population(
                driver,
                feed_selector,
                min_anchors=2,
                timeout=float(os.getenv("FB_ANCHOR_WAIT_S", 6)),
                poll_seconds=0.5,
                logger=logger,
                context={"path": "feed", "artist": search_name},
            )
            if isinstance(wait_result, tuple):
                success, before_ct, after_ct, waited_ms = wait_result
                if not anchor_wait_meta:
                    anchor_wait_meta = {"waited_ms": waited_ms, "anchors_before": before_ct, "anchors_after": after_ct}
            else:
                success = wait_result
            if success:
                counts = _refresh_counts()
            waited_for_population = waited_for_population or success

        if counts["usable_feed"] == 0 and counts["usable_search"] == 0:
            for attempt in range(2):
                try:
                    driver.execute_script("window.scrollBy(0, 700);")
                except Exception:
                    pass
                time.sleep(random.uniform(0.3, 0.6))
                scroll_retries = attempt + 1
                counts = _refresh_counts()
                if counts["usable_feed"] > 0 or counts["usable_search"] > 0:
                    break

        selector_stats = counts.get("selector_stats", [])

        def _score_entry(entry: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
            return (
                entry.get("usable_count", 0),
                entry.get("anchors_in_scope", 0),
                entry.get("role_links_in_scope", 0),
                entry.get("links_in_scope_total", 0),
                len(entry.get("hrefs", []) or []),
            )

        chosen_entry = max(selector_stats, key=_score_entry) if selector_stats else {}
        chosen_selector = chosen_entry.get("selector", "NONE")
        chosen_container = chosen_entry.get("container")
        chosen_data: Dict[str, Any] = chosen_entry or {}

        # Low-yield recovery: prefer any selector that meets the threshold, else richest by href count.
        if (chosen_data.get("hrefs") or []) and len(chosen_data.get("hrefs", [])) < low_yield_threshold and selector_stats:
            rich_entries = [e for e in selector_stats if len(e.get("hrefs", [])) >= low_yield_threshold]
            if rich_entries:
                best_rich = max(rich_entries, key=lambda e: len(e.get("hrefs", [])))
                chosen_selector = best_rich.get("selector", chosen_selector)
                chosen_container = best_rich.get("container")
                chosen_data = best_rich
            else:
                richest = max(selector_stats, key=lambda e: len(e.get("hrefs", [])))
                chosen_selector = richest.get("selector", chosen_selector)
                chosen_container = richest.get("container")
                chosen_data = richest

        if chosen_container is None:
            total_anchors = len(_find_all(driver, "a"))
            search_len, search_preview = _container_html_preview(counts["search_container"])
            feed_len, feed_preview = _container_html_preview(counts["feed_container"])
            preview = search_preview or feed_preview
            if not preview:
                for entry in selector_stats:
                    if entry.get("container"):
                        _, preview = _container_html_preview(entry.get("container"))
                        if preview:
                            break
            page_url = _safe_current_url(driver)
            _log(
                logger,
                "[Night FB][DOM Gate V2] "
                f"harvest_version=v2 wait_hit={wait_hit} containers_found_feed={counts['containers_found_feed']} "
                f"containers_found_search={counts['containers_found_search']} scroll_retries={scroll_retries} "
                f"anchors_in_scope_feed={counts['anchors_in_scope_feed']} anchors_in_scope_search={counts['anchors_in_scope_search']} "
                f"links_in_scope_total_feed={counts.get('links_in_scope_total_feed', 0)} "
                f"links_in_scope_total_search={counts.get('links_in_scope_total_search', 0)} "
                f"candidates_pre_url_gate=0 candidates_post_url_gate=0 dropped_by_dom_gate={total_anchors} "
                f"url_gate_rejected=0 chosen_container_selector=NONE search_name='{search_name or ''}' "
                f"session_unhealthy={session_unhealthy_flag} session_reason={session_reason_clean} "
                f"anchor_waited={int(waited_for_population)} search_html_len={search_len} feed_html_len={feed_len} "
                f"overlay_present={overlay_present} page_url='{page_url}' preview='{preview}' "
                f"anchor_wait_meta={anchor_wait_meta}"
            )
            run_dir_env = os.getenv("RUN_DIR") or os.getenv("NIGHT_RUN_DIR")
            container_html = ""
            for entry in selector_stats:
                if entry.get("container"):
                    try:
                        container_html = entry["container"].get_attribute("outerHTML") or ""
                    except Exception:
                        container_html = ""
                    if container_html:
                        break
            if not container_html:
                try:
                    container_html = driver.page_source or ""
                except Exception:
                    container_html = ""
            _maybe_dump_dom_gate_debug(
                run_dir_env,
                _sanitize_fs_key(search_name or "unknown"),
                "NONE",
                selector_stats,
                [],
                container_html,
                logger=logger,
            )
            if overlay_present and counts["anchors_in_scope_feed"] == 0 and counts["anchors_in_scope_search"] == 0:
                logged_already = False
                if diagnostics is not None:
                    logged_already = bool(diagnostics.get("overlay_soft_block_logged"))
                    diagnostics["overlay_soft_block"] = True
                    diagnostics["overlay_soft_block_logged"] = True
                if not logged_already:
                    _log(logger, "[Night FB] Overlay/zero-anchors detected; treating as soft block and slowing down.")
            shared_candidates = _shared_zero_result_fallback()
            if shared_candidates:
                return shared_candidates
            return []

        chosen_hrefs: List[str] = chosen_data.get("hrefs", [])[:]
        anchors_in_scope_chosen = chosen_data.get("anchors_in_scope", 0)

        # Debug-only, low-yield attribute scan to pull extra href-like strings.
        if len(chosen_hrefs) < low_yield_threshold and attr_scan_allowed:
            extra_hrefs = _scan_href_like_attributes(
                chosen_container, max_nodes=attr_scan_nodes, max_candidates=attr_scan_max
            )
            seen = set(chosen_hrefs)
            for href in extra_hrefs:
                if href in seen:
                    continue
                seen.add(href)
                chosen_hrefs.append(href)

        deduped_pre_gate: List[str] = []
        pre_seen: Set[str] = set()
        for href in chosen_hrefs:
            if href in pre_seen:
                continue
            pre_seen.add(href)
            deduped_pre_gate.append(href)
        chosen_hrefs = deduped_pre_gate

        candidates_pre_url_gate = len(chosen_hrefs)
        if anchors_in_scope_chosen == 0 or candidates_pre_url_gate == 0:
            container_html = ""
            try:
                if chosen_container is not None:
                    container_html = chosen_container.get_attribute("outerHTML") or ""
            except Exception:
                container_html = ""
            if not container_html:
                try:
                    container_html = driver.page_source or ""
                except Exception:
                    container_html = ""
            _maybe_dump_dom_gate_debug(
                run_dir_env,
                _sanitize_fs_key(search_name or "unknown"),
                chosen_selector,
                selector_stats,
                chosen_hrefs,
                container_html,
                logger=logger,
            )
        filtered_hrefs: List[str] = []
        gate_reject = 0
        rejected_samples: List[str] = []
        seen_href: set = set()
        for href in chosen_hrefs:
            if href in seen_href:
                continue
            seen_href.add(href)
            if not _is_candidate_usable(href):
                gate_reject += 1
                if debug_dom_gate and len(rejected_samples) < 10:
                    rejected_samples.append(href)
                continue
            filtered_hrefs.append(href)

        candidates_post_url_gate = len(filtered_hrefs)

        total_anchors_all = len(_find_all(driver, "a"))
        dropped_by_dom_gate = max(0, total_anchors_all - anchors_in_scope_chosen)

        # Reuse existing parser for enrichment, then scope to selected hrefs for parity with v1 objects.
        enriched_candidates = _parse_search_candidates(getattr(driver, "page_source", ""), logger=logger, search_name=search_name)
        href_set = { _normalize_fb_href(h) for h in filtered_hrefs }
        filtered_candidates = [
            cand for cand in enriched_candidates if _normalize_fb_href(getattr(cand, "url", "")) in href_set
        ]

        if debug_dom_gate and (candidates_pre_url_gate < low_yield_threshold or candidates_post_url_gate == 0):
            sample_urls = chosen_hrefs[:10]
            rejected_sample_urls = rejected_samples[:10]
            _log(
                logger,
                "[Night FB][DOM Gate V2][debug] "
                f"chosen_container_selector={chosen_selector} "
                f"count_a_href={chosen_data.get('count_a_href', 0)} "
                f"count_a_role_link_href={chosen_data.get('count_a_role_link_href', 0)} "
                f"count_role_link={chosen_data.get('count_role_link', 0)} "
                f"anchors_in_scope={chosen_data.get('anchors_in_scope', 0)} "
                f"role_links_in_scope={chosen_data.get('role_links_in_scope', 0)} "
                f"links_in_scope_total={chosen_data.get('links_in_scope_total', 0)} "
                f"candidates_pre_url_gate={candidates_pre_url_gate} "
                f"candidates_post_url_gate={candidates_post_url_gate} "
                f"sample_pre_url={sample_urls} "
                f"sample_rejected={rejected_sample_urls}"
            )

        _log(
            logger,
            "[Night FB][DOM Gate V2] "
            f"harvest_version=v2 wait_hit={wait_hit} containers_found_feed={counts['containers_found_feed']} "
            f"containers_found_search={counts['containers_found_search']} scroll_retries={scroll_retries} "
            f"anchors_in_scope_feed={counts['anchors_in_scope_feed']} anchors_in_scope_search={counts['anchors_in_scope_search']} "
            f"links_in_scope_total_feed={counts.get('links_in_scope_total_feed', 0)} "
            f"links_in_scope_total_search={counts.get('links_in_scope_total_search', 0)} "
            f"candidates_pre_url_gate={candidates_pre_url_gate} candidates_post_url_gate={candidates_post_url_gate} "
            f"dropped_by_dom_gate={dropped_by_dom_gate} url_gate_rejected={gate_reject} "
            f"chosen_container_selector={chosen_selector} search_name='{search_name or ''}' "
            f"session_unhealthy={session_unhealthy_flag} session_reason={session_reason_clean} "
            f"anchor_waited={int(waited_for_population)}"
        )

        if candidates_post_url_gate == 0:
            shared_candidates = _shared_zero_result_fallback()
            if shared_candidates:
                return shared_candidates

        return filtered_candidates
    except Exception as exc:
        _log(
            logger,
            "[Night FB][DOM Gate V2] "
            f"harvest_version=v2 wait_hit=exception containers_found_feed=0 containers_found_search=0 "
            f"scroll_retries=0 anchors_in_scope_feed=0 anchors_in_scope_search=0 "
            f"candidates_pre_url_gate=0 candidates_post_url_gate=0 dropped_by_dom_gate=0 url_gate_rejected=0 "
            f"chosen_container_selector=NONE search_name='{search_name or ''}' "
            f"session_unhealthy={session_unhealthy_flag} session_reason={session_reason_clean} error={exc}"
        )
        raise


def _create_fb_driver_night_mode(headless: bool, logger: LoggerFn = None):
    """
    Night-Mode-only Chrome driver with persistent profile to reuse FB auth.
    """
    profile_dir = _resolve_night_fb_profile_dir(logger)
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except Exception:
        pass

    chrome_options = ChromeOptions()
    if headless:
        try:
            chrome_options.add_argument("--headless=new")
        except Exception:
            chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")
    chrome_options.page_load_strategy = "eager"
    # Force images on for the persistent Night FB profile so manual login/challenge
    # recovery remains possible even if a previous run left the shared profile blocked.
    prefs = {"profile.managed_default_content_settings.images": 1}
    chrome_options.add_experimental_option("prefs", prefs)
    driver = _start_chromedriver_with_retry(
        chrome_options,
        logger=logger,
        profile_dir=profile_dir,
        enable_temp_profile_fallback=False,
    )
    webdriver_mask_applied = _apply_repo_aligned_webdriver_mask(driver, logger=logger)
    requested_profile = str(getattr(driver, "_night_fb_requested_profile_dir", profile_dir) or "")
    active_profile = str(getattr(driver, "_night_fb_active_profile_dir", requested_profile) or "")
    used_temp_profile = bool(getattr(driver, "_night_fb_used_temp_profile", False))
    persistent_profile_used = bool(requested_profile and active_profile == requested_profile and not used_temp_profile)
    _log(
        logger,
        "[Night FB][driver] "
        f"requested_profile={requested_profile or '<none>'} "
        f"active_profile={active_profile or '<none>'} "
        f"persistent_profile={1 if persistent_profile_used else 0} "
        f"temp_fallback={1 if used_temp_profile else 0} "
        f"webdriver_mask={1 if webdriver_mask_applied else 0}",
    )
    return driver


class NightPersistentFacebookSession:
    """
    Minimal session wrapper that relies on persistent Chrome profile auth (c_user cookie).
    Avoids legacy login flows.
    """

    def __init__(self, driver_factory, headless: bool, logger: LoggerFn = None, wait_seconds: float = 90.0):
        self.driver_factory = driver_factory
        self.headless = headless
        self.logger = logger
        self.wait_seconds = wait_seconds
        self.driver = None
        self.last_health_reason: str = ""
        self.last_health_ok: bool = True
        # Checkpoint resilience
        self.checkpoint_restart_count: int = 0
        self.checkpoint_cooldown_done: bool = False
        # Last navigation metadata
        self.last_nav_timed_out: bool = False
        self.last_nav_page_source: str = ""
        self.last_nav_current_url: str = ""

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None

    def _wait_for_manual_login(self, driver) -> None:
        deadline = time.time() + max(self.wait_seconds, 1.0)
        while time.time() < deadline:
            if _is_driver_authenticated(driver):
                return
            time.sleep(3.0)
        raise FacebookDriverError("Manual login timed out; no c_user cookie detected.")

    def _ensure_driver_for_direct_navigation(self):
        if self.driver:
            try:
                _ = self.driver.current_url
                return self.driver
            except Exception:
                self.driver = None
        self.driver = self.driver_factory()
        return self.driver

    def ensure_logged_in(self):
        if self.driver:
            try:
                _ = self.driver.current_url
            except Exception:
                self.driver = None
        if self.driver and _is_driver_authenticated(self.driver):
            return self.driver

        self.driver = self.driver_factory()
        try:
            self.driver.get("https://www.facebook.com/")
        except Exception as exc:
            raise FacebookDriverError(f"Failed to load Facebook homepage: {exc}")

        authed = _is_driver_authenticated(self.driver)
        auth_surface = _classify_fb_auth_surface(self.driver) or ("authenticated" if authed else "unauthenticated")
        mode_label = "headless" if self.headless else "headed"
        requested_profile = str(getattr(self.driver, "_night_fb_requested_profile_dir", "") or "")
        active_profile = str(getattr(self.driver, "_night_fb_active_profile_dir", requested_profile) or "")
        used_temp_profile = bool(getattr(self.driver, "_night_fb_used_temp_profile", False))
        _log(
            self.logger,
            "[Night FB][auth_probe] "
            f"mode={mode_label} "
            f"requested_profile={requested_profile or '<none>'} "
            f"active_profile={active_profile or '<none>'} "
            f"temp_fallback={1 if used_temp_profile else 0} "
            f"authed={1 if authed else 0} "
            f"reason={auth_surface}",
        )

        if authed:
            old_driver = self.driver
            try:
                healthy, reason = _session_looks_healthy(self.driver)
            except Exception:
                healthy, reason = False, "exception"
            self.last_health_ok = healthy
            self.last_health_reason = reason or ""
            if not healthy:
                # Try a neutral authed page before restarting.
                try:
                    self.driver.get("https://www.facebook.com/me")
                    time.sleep(1.0)
                    healthy, reason = _session_looks_healthy(self.driver)
                except Exception:
                    healthy, reason = False, reason or "exception"
                self.last_health_ok = healthy
                self.last_health_reason = reason or ""
            if not healthy:
                if (reason or "").lower() in {"checkpoint", "captcha", "redirect_login", "recover", "two_factor", "consent"}:
                    _log(
                        self.logger,
                        f"[Night FB][WARN] Persistent FB session needs manual recovery (reason={reason or 'unknown'}); image loading is enabled for login/captcha recovery.",
                    )
                if (reason or "").lower() == "checkpoint" and authed:
                    if self.checkpoint_restart_count == 0:
                        self.checkpoint_restart_count += 1
                        try:
                            self.driver.quit()
                        except Exception:
                            pass
                        try:
                            self.driver = self.driver_factory()
                            try:
                                healthy_retry, reason_retry = _session_looks_healthy(self.driver)
                            except Exception:
                                healthy_retry, reason_retry = False, "exception"
                            self.last_health_ok = healthy_retry
                            self.last_health_reason = reason_retry or ""
                        except Exception:
                            _log(self.logger, "[Night FB] Session restart failed; continuing but FB results may be limited.")
                            self.driver = old_driver
                        if not self.checkpoint_cooldown_done:
                            cooldown_s = random.uniform(30.0, 60.0)
                            _log(self.logger, f"[Night FB] Session checkpoint restart limited to 1 per run; entering cooldown for {cooldown_s:.0f}s.")
                            time.sleep(cooldown_s)
                            _log(self.logger, "[Night FB] Cooldown complete after checkpoint restart; continuing at reduced pace.")
                            self.checkpoint_cooldown_done = True
                    else:
                        # Do not restart again; allow downstream protection to act
                        self.last_health_ok = False
                        self.last_health_reason = reason or "checkpoint"
                else:
                    _log(self.logger, f"[Night FB] Session unhealthy; restarting driver once... reason={reason}")
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
                    try:
                        self.driver = self.driver_factory()
                        try:
                            healthy_retry, reason_retry = _session_looks_healthy(self.driver)
                        except Exception:
                            healthy_retry, reason_retry = False, "exception"
                        self.last_health_ok = healthy_retry
                        self.last_health_reason = reason_retry or ""
                        if not healthy_retry:
                            _log(self.logger, f"[Night FB] Session still unhealthy; continuing but FB results may be limited. reason={reason_retry}")
                    except Exception:
                        _log(self.logger, "[Night FB] Session restart failed; continuing but FB results may be limited.")
                        self.driver = old_driver
            return self.driver

        self.last_health_ok = False
        self.last_health_reason = auth_surface
        _log(
            self.logger,
            "[Night FB][WARN] "
            f"Persistent FB profile launched without auth (reason={auth_surface}); manual login is required. "
            "Image loading is enabled for login/captcha recovery.",
        )
        if self.headless:
            raise FacebookDriverError(
                f"Persistent FB profile launched but auth is missing (reason={auth_surface}; no c_user cookie present)."
            )

        _log(self.logger, "[Night FB] Awaiting manual login to establish session (headed mode)...")
        self._wait_for_manual_login(self.driver)
        self.last_health_ok = True
        self.last_health_reason = ""
        return self.driver

    def navigate(self, url: str, logger: LoggerFn = None, unblock_on_ready: bool = False, validate_session: bool = True):
        log_target = logger if logger is not None else self.logger
        driver = self.ensure_logged_in() if validate_session else self._ensure_driver_for_direct_navigation()
        try:
            html, current_url, timed_out = _load_fb_page_with_timeout(
                driver,
                url,
                timeout_s=20.0,
                logger=log_target,
                unblock_on_ready=unblock_on_ready,
            )
            self.last_nav_timed_out = timed_out
            self.last_nav_page_source = html
            self.last_nav_current_url = current_url
            return driver
        except Exception as exc:
            if _is_session_death_exc(exc):
                _log(log_target, f"[Night FB] Driver session died during navigate; refreshing and retrying url={url!r} error={exc}")
                try:
                    driver = self.refresh_session()
                    html, current_url, timed_out = _load_fb_page_with_timeout(
                        driver,
                        url,
                        timeout_s=20.0,
                        logger=log_target,
                        unblock_on_ready=unblock_on_ready,
                    )
                    self.last_nav_timed_out = timed_out
                    self.last_nav_page_source = html
                    self.last_nav_current_url = current_url
                    return driver
                except Exception as exc2:
                    _log(log_target, f"[Night FB] Driver retry after refresh failed url={url!r}; err={exc2}")
                    raise FacebookDriverError(f"driver_session_died url={url!r}: {exc2}")
            raise

    def refresh_session(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        return self.ensure_logged_in()


def ensure_night_fb_run_session(
    run_state: Optional[NightFBRunState],
    *,
    headless: bool,
    logger: LoggerFn = None,
    owner: str = "",
    prewarm_session: bool = True,
) -> Optional[NightPersistentFacebookSession]:
    if run_state is None:
        return None
    if run_state.disabled_for_run or run_state.session_invalid:
        return None
    if not run_state.session_source.can_probe:
        return None

    session = run_state.session
    if isinstance(session, NightPersistentFacebookSession):
        if not prewarm_session:
            if owner:
                run_state.session_owner = owner
            return session
        try:
            driver = session.ensure_logged_in()
            decision = update_night_fb_run_state(
                run_state,
                probe_night_fb_session_decision(driver, visit_home=False),
                owner=owner or run_state.session_owner,
            )
            if decision.state == "session_invalid":
                disable_night_fb_run_state(
                    run_state,
                    decision.reason or "session_invalid",
                    session_invalid=True,
                    close_session=True,
                )
                return None
            if decision.state == "authenticated_but_checkpointed":
                disable_night_fb_run_state(
                    run_state,
                    decision.reason or "checkpoint",
                    checkpointed=True,
                )
                return None
            if decision.authenticated and decision.usable:
                if owner:
                    run_state.session_owner = owner
                return session
            disable_night_fb_run_state(
                run_state,
                decision.reason or decision.state or "not_authenticated",
                session_unhealthy=True,
                close_session=not decision.authenticated,
            )
            return None
        except FacebookDriverError as exc:
            reason = str(exc) or "session_invalid"
            disable_night_fb_run_state(
                run_state,
                reason,
                session_invalid="invalid" in reason.lower() or "died" in reason.lower(),
                checkpointed="checkpoint" in reason.lower(),
                close_session=True,
            )
            return None
        except Exception as exc:
            reason = "session_invalid" if _is_session_death_exc(exc) else "driver_error"
            disable_night_fb_run_state(
                run_state,
                reason,
                session_invalid=_is_session_death_exc(exc),
                close_session=True,
            )
            return None

    driver_factory = lambda: _create_fb_driver_night_mode(headless, logger=logger)
    session = NightPersistentFacebookSession(driver_factory, headless=headless, logger=logger)
    if not prewarm_session:
        run_state.session = session
        if owner:
            run_state.session_owner = owner
        return session
    try:
        driver = session.ensure_logged_in()
    except FacebookDriverError as exc:
        reason = str(exc) or "session_start_failed"
        run_state.latest_session_decision = _build_night_fb_session_decision(
            authenticated=False,
            reason=reason,
            session_invalid="invalid" in reason.lower(),
        )
        disable_night_fb_run_state(
            run_state,
            reason,
            session_invalid="invalid" in reason.lower(),
            checkpointed="checkpoint" in reason.lower(),
            close_session=True,
        )
        return None
    except Exception as exc:
        reason = "session_invalid" if _is_session_death_exc(exc) else "driver_error"
        run_state.latest_session_decision = _build_night_fb_session_decision(
            authenticated=False,
            reason=reason,
            session_invalid=_is_session_death_exc(exc),
        )
        disable_night_fb_run_state(
            run_state,
            reason,
            session_invalid=_is_session_death_exc(exc),
            close_session=True,
        )
        return None

    run_state.session = session
    if owner:
        run_state.session_owner = owner
    decision = update_night_fb_run_state(
        run_state,
        probe_night_fb_session_decision(driver, visit_home=False),
        owner=owner or run_state.session_owner,
    )
    if decision.state == "authenticated_but_checkpointed":
        disable_night_fb_run_state(
            run_state,
            decision.reason or "checkpoint",
            checkpointed=True,
        )
        return None
    if decision.state == "session_invalid":
        disable_night_fb_run_state(
            run_state,
            decision.reason or "session_invalid",
            session_invalid=True,
            close_session=True,
        )
        return None
    if decision.authenticated and decision.usable:
        return session
    disable_night_fb_run_state(
        run_state,
        decision.reason or decision.state or "not_authenticated",
        session_unhealthy=True,
        close_session=not decision.authenticated,
    )
    return None


def _apply_repo_aligned_webdriver_mask(driver, logger: LoggerFn = None) -> bool:
    """Reuse the existing repo CDP webdriver mask for the authenticated Night FB launch."""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
            },
        )
        return True
    except Exception as exc:
        _log(logger, f"[Night FB][WARN] Failed to apply repo webdriver mask: {exc}")
        return False


def _start_chromedriver_with_retry(
    chrome_options: ChromeOptions,
    logger: LoggerFn = None,
    profile_dir: Optional[str] = None,
    enable_temp_profile_fallback: bool = False,
):
    """
    Start ChromeDriver with a one-time reinstall; optionally retry with a temp profile when startup flaps.
    """

    def _log_preflight(driver_path: Optional[str], opts: ChromeOptions, profile: Optional[str]) -> None:
        chrome_bin, chrome_version = _detect_chrome_binary()
        driver_version = _chromedriver_version(driver_path)
        driver_display = driver_path or "<selenium_manager>"
        profile_exists, profile_locks = _profile_dir_state(profile or "") if profile else (False, [])
        image_policy = _chrome_images_policy_label(opts)
        _log(
            logger,
            "[Night FB][preflight] "
            f"chrome_binary={chrome_bin or '<auto>'} "
            f"chrome_version={chrome_version or '<unknown>'} "
            f"chromedriver={driver_display} "
            f"chromedriver_version={driver_version or '<unknown>'} "
            f"profile_dir={profile or '<none>'} "
            f"profile_exists={profile_exists} profile_locks={profile_locks} "
            f"image_policy={image_policy} "
            f"args={getattr(opts, 'arguments', [])}"
        )

    def _should_temp_recover(exc: BaseException) -> bool:
        msg = (str(exc) or "").lower()
        tokens = (
            "user data directory is already in use",
            "chrome failed to start",
            "devtoolsactiveport",
            "chrome instance exited",
        )
        return any(tok in msg for tok in tokens)

    last_exc: Optional[Exception] = None
    temp_profile_dir: Optional[str] = None
    recovery_attempted = False

    attempt_opts: ChromeOptions = chrome_options
    attempt_profile = profile_dir

    attempt = 0
    max_attempts = 2  # two standard tries; a third is added only if temp recovery triggers

    while attempt < max_attempts:
        attempt += 1
        driver_path: Optional[str] = None

        log_path = os.environ.get("NIGHT_FB_CHROMEDRIVER_LOG")
        try:
            if log_path:
                _log(logger, f"[Night FB][preflight] chromedriver verbose log -> {log_path}")
                service = ChromeService(log_output=log_path)
            else:
                service = ChromeService()
        except TypeError:
            # Older selenium versions may not support log_output; fall back silently.
            service = ChromeService()

        _log_preflight(driver_path, attempt_opts, attempt_profile)

        try:
            driver = webdriver.Chrome(service=service, options=attempt_opts)
            _register_driver_cleanup(driver)
            try:
                driver._night_fb_requested_profile_dir = profile_dir or ""  # type: ignore[attr-defined]
                driver._night_fb_active_profile_dir = attempt_profile or ""  # type: ignore[attr-defined]
                driver._night_fb_used_temp_profile = bool(temp_profile_dir and attempt_profile == temp_profile_dir)  # type: ignore[attr-defined]
                driver._night_fb_temp_profile_attempted = bool(recovery_attempted)  # type: ignore[attr-defined]
            except Exception:
                pass
            if temp_profile_dir:
                atexit.register(lambda: shutil.rmtree(temp_profile_dir, ignore_errors=True))
            return driver
        except Exception as exc:
            last_exc = exc
            _log(logger, f"[Night FB] ChromeDriver launch failed (attempt {attempt}): {exc}")

            if enable_temp_profile_fallback and (not recovery_attempted) and _should_temp_recover(exc):
                recovery_attempted = True
                temp_profile_dir = tempfile.mkdtemp(prefix="night_fb_profile_")
                _log(logger, f"[Night FB] Retrying Chrome startup with temporary profile dir: {temp_profile_dir}")
                attempt_opts = _clone_chrome_options(chrome_options, override_profile_dir=temp_profile_dir)
                attempt_profile = temp_profile_dir
                max_attempts = attempt + 1  # allow one extra attempt for the temp profile
                continue

    if profile_dir and (not enable_temp_profile_fallback) and last_exc and _should_temp_recover(last_exc):
        _log(
            logger,
            "[Night FB][profile] "
            f"requested_profile={profile_dir} "
            "persistent_profile=0 "
            "temp_fallback=blocked "
            f"reason={last_exc}",
        )
        raise FacebookDriverError(
            f"Persistent FB profile could not be used ({profile_dir}); temp-profile fallback blocked for authenticated Night FB: {last_exc}"
        )

    reason = str(last_exc) if last_exc else "unknown_error"
    raise FacebookDriverError(f"Failed to start ChromeDriver after {attempt} attempts: {reason}")


def _create_fb_driver_public(headless: bool = True):
    """
    Create a clean, no-login Chrome driver for public FB scraping.
    Mirrors the legacy Unearthed driver (no cookies/session).
    """
    chrome_options = ChromeOptions()
    if headless:
        try:
            chrome_options.add_argument("--headless=new")
        except Exception:
            chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--incognito")
    chrome_options.page_load_strategy = "eager"
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    return _start_chromedriver_with_retry(chrome_options, logger=None, profile_dir=None, enable_temp_profile_fallback=False)


_FB_BOUNDED_RENDERED_TEXT_SCRIPT = """
    /* fb_rendered_visible_text_bounded */
    const maxChars = Math.max(512, Number(arguments[0] || 6000));
    const maxNodes = Math.max(24, Number(arguments[1] || 180));
    const rootSelectors = ['div[role="main"]', 'div[role="complementary"]', 'aside'];
    const leafTags = new Set(['A', 'BUTTON', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'P', 'SPAN']);
    const semanticTags = new Set(['A', 'BUTTON', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'P']);
    const interestingPattern = /@|contact|email|book|booking|mgmt|management|business|about|intro/i;
    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
    const isVisible = (el) => {
      if (!el || !el.isConnected) return false;
      const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
      if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
      const rects = typeof el.getClientRects === 'function' ? el.getClientRects() : null;
      return !!(rects && rects.length);
    };

    const roots = [];
    const seenRoots = new Set();
    for (const selector of rootSelectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (!isVisible(el) || seenRoots.has(el)) continue;
        seenRoots.add(el);
        roots.push(el);
      }
    }
    if (!roots.length && document.body) {
      roots.push(document.body);
    }

    const seenTexts = new Set();
    const results = [];
    let totalChars = 0;
    let visitedNodes = 0;

    const pushText = (value) => {
      const text = normalize(value);
      if (!text || seenTexts.has(text)) return false;
      const remaining = maxChars - totalChars;
      if (remaining <= 0) return true;
      const clipped = remaining >= text.length ? text : text.slice(0, remaining);
      if (!clipped) return totalChars >= maxChars;
      seenTexts.add(text);
      results.push(clipped);
      totalChars += clipped.length;
      return totalChars >= maxChars;
    };

    const collectRoot = (root) => {
      if (!root || totalChars >= maxChars || visitedNodes >= maxNodes) return;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
        acceptNode(node) {
          if (!node || node === root) return NodeFilter.FILTER_SKIP;
          if (!leafTags.has(node.tagName)) return NodeFilter.FILTER_SKIP;
          if (!isVisible(node)) return NodeFilter.FILTER_SKIP;
          if (node.children && node.children.length && !semanticTags.has(node.tagName)) {
            return NodeFilter.FILTER_SKIP;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      });

      let node = walker.nextNode();
      while (node && totalChars < maxChars && visitedNodes < maxNodes) {
        visitedNodes += 1;
        const text = normalize(node.innerText || node.textContent || '');
        if (text && (interestingPattern.test(text) || results.length < 8)) {
          if (pushText(text)) return;
        }
        node = walker.nextNode();
      }
    };

    for (const root of roots) {
      collectRoot(root);
      if (totalChars >= maxChars || visitedNodes >= maxNodes) break;
    }

    return results.join('\\n');
"""


def _execute_fb_bounded_rendered_text_snapshot(driver, max_chars: int, max_nodes: int):
    try:
        return driver.execute_script(_FB_BOUNDED_RENDERED_TEXT_SCRIPT, max_chars, max_nodes)
    except TypeError:
        return driver.execute_script(_FB_BOUNDED_RENDERED_TEXT_SCRIPT)


def _extract_rendered_visible_text_from_driver(driver) -> str:
    if driver is None:
        return ""

    try:
        max_chars = int(os.getenv("FB_RENDERED_TEXT_MAX_CHARS", "6000") or "6000")
    except Exception:
        max_chars = 6000
    max_chars = max(512, min(max_chars, 24000))

    try:
        max_nodes = int(os.getenv("FB_RENDERED_TEXT_MAX_NODES", "180") or "180")
    except Exception:
        max_nodes = 180
    max_nodes = max(24, min(max_nodes, 600))

    def _read_snapshot(drv) -> str:
        try:
            text = _execute_fb_bounded_rendered_text_snapshot(drv, max_chars, max_nodes) or ""
        except Exception:
            text = ""
        if isinstance(text, list):
            text = "\n".join(str(part or "").strip() for part in text if str(part or "").strip())
        return str(text).strip()

    initial_text = _read_snapshot(driver)
    if EMAIL_REGEX.search(initial_text):
        return initial_text

    try:
        timeout_s = float(os.getenv("FB_RENDERED_TEXT_WAIT_S", "1.5") or "1.5")
    except Exception:
        timeout_s = 1.5
    if timeout_s <= 0:
        return initial_text

    state = {
        "last_text": initial_text,
        "stable_hits": 0,
        "saw_change": False,
    }
    baseline_len = len(initial_text)

    def _rendered_text_ready(drv):
        text = _read_snapshot(drv)
        if EMAIL_REGEX.search(text):
            state["last_text"] = text
            return text

        previous = state["last_text"] or ""
        if text != previous:
            state["saw_change"] = True
            state["stable_hits"] = 0
            state["last_text"] = text
            return False

        state["last_text"] = text or previous
        if not text:
            return False

        state["stable_hits"] += 1
        grew_materially = len(text) >= max(160, baseline_len + 40)
        if (not initial_text) and state["stable_hits"] >= 1:
            return text
        if state["saw_change"] and state["stable_hits"] >= 2:
            return text
        if grew_materially and state["stable_hits"] >= 1:
            return text
        return False

    try:
        return WebDriverWait(driver, timeout_s, poll_frequency=0.2).until(_rendered_text_ready)
    except TimeoutException:
        return str(state.get("last_text") or initial_text or "").strip()


def _extract_fb_visible_text_with_container_fallback(driver) -> str:
    """
    Facebook-only supplement for already-open pages.
    Starts with the bounded rendered-text helper and adds a tiny semantic
    fallback for visible main/sidebar regions.
    """
    base_text = _extract_rendered_visible_text_from_driver(driver)
    if driver is None:
        return base_text

    try:
        blocks = driver.execute_script(
            """
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (el) => {
              if (!el || !el.isConnected) return false;
              const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
              if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
              const rects = typeof el.getClientRects === 'function' ? el.getClientRects() : null;
              return !!(rects && rects.length);
            };

            const selectors = ['div[role="main"]', 'div[role="complementary"]', 'aside'];
            const seen = new Set();
            const results = [];

            for (const selector of selectors) {
              for (const el of document.querySelectorAll(selector)) {
                if (!isVisible(el)) continue;
                const text = normalize(el.innerText || el.textContent || '');
                if (!text || seen.has(text)) continue;
                seen.add(text);
                results.push(text);
              }
            }

            return results;
            """
        ) or []
    except Exception:
        blocks = []

    if isinstance(blocks, str):
        blocks = [blocks]

    seen_blocks = set()
    normalized_base = " ".join(str(base_text or "").split())
    if normalized_base:
        seen_blocks.add(normalized_base)

    extra_blocks: List[str] = []
    for block in blocks:
        normalized = " ".join(str(block or "").split())
        if not normalized or normalized in seen_blocks:
            continue
        if normalized_base and normalized in normalized_base:
            continue
        seen_blocks.add(normalized)
        extra_blocks.append(normalized)

    if not extra_blocks:
        return base_text
    if not base_text:
        return "\n".join(extra_blocks)
    return "\n".join([str(base_text).strip()] + extra_blocks)


def _reveal_fb_contact_controls(driver, logger: LoggerFn = None, max_clicks: int = 2) -> List[str]:
    """Bounded reveal pass for obvious, non-navigational contact expanders."""
    if driver is None or max_clicks <= 0:
        return []

    try:
        clicked = driver.execute_script(
            """
            /* fb_reveal_controls */
            const terms = Array.isArray(arguments[0]) ? arguments[0] : [];
            const maxClicks = Math.max(0, Number(arguments[1] || 0));
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const isVisible = (el) => {
              if (!el || !el.isConnected) return false;
              const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
              if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
              const rects = typeof el.getClientRects === 'function' ? el.getClientRects() : null;
              return !!(rects && rects.length);
            };
            const getRevealPriority = (el) => {
              const mainRoot = typeof el.closest === 'function' ? el.closest('div[role="main"]') : null;
              if (!mainRoot) return 0;
              let hasContactInfo = false;
              let hasDetails = false;
              let hasContact = false;
              let node = el;
              for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                const text = normalize(node.getAttribute('aria-label') || node.innerText || node.textContent || '');
                if (text.includes('contact info')) hasContactInfo = true;
                if (text.includes('details')) hasDetails = true;
                if (text.includes('contact')) hasContact = true;
              }
              if (hasContactInfo) return 300;
              if (hasDetails) return 200;
              if (hasContact) return 100;
              return 10;
            };
            const selectors = [
              'button',
              'div[role="button"]',
              'span[role="button"]',
              'a[role="button"]',
              'a[href="#"]',
              'a[href=""]',
            ];
            const ordered = [];
            const seen = new Set();
            for (const selector of selectors) {
              for (const el of document.querySelectorAll(selector)) {
                if (!isVisible(el) || seen.has(el)) continue;
                seen.add(el);
                ordered.push({ el, priority: getRevealPriority(el), index: ordered.length });
              }
            }
            ordered.sort((left, right) => {
              if (right.priority !== left.priority) return right.priority - left.priority;
              return left.index - right.index;
            });
            const clicked = [];
            const used = new Set();
            for (const term of terms) {
              if (clicked.length >= maxClicks) break;
              for (const candidate of ordered) {
                if (clicked.length >= maxClicks) break;
                const el = candidate.el;
                if (used.has(el)) continue;
                const text = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                const href = normalize(el.getAttribute('href') || '');
                const tag = String(el.tagName || '').toLowerCase();
                const isNavigationalAnchor = tag === 'a' && href && href !== '#' && !href.startsWith('javascript:');
                if (isNavigationalAnchor) continue;
                if (!text || (!text.includes(term) && text !== term)) continue;
                try {
                  el.click();
                  used.add(el);
                  clicked.push(text || term);
                } catch (_err) {}
              }
            }
            return clicked.slice(0, maxClicks);
            """,
            list(_FB_REVEAL_CONTROL_TERMS),
            int(max_clicks),
        ) or []
    except Exception:
        clicked = []

    if isinstance(clicked, str):
        clicked = [clicked]

    normalized = [str(item or "").strip() for item in clicked if str(item or "").strip()][:max_clicks]
    if normalized:
        _log(logger, f"[FB Email] Reveal pass clicked: {normalized}")
        time.sleep(0.25)
    return normalized


def _collect_fb_live_anchor_targets(driver, limit: int = 200) -> List[str]:
    """Capture visible live anchor hrefs that look email-bearing."""
    if driver is None or limit <= 0:
        return []

    try:
        values = driver.execute_script(
            """
            /* fb_collect_anchor_hrefs */
            const limit = Math.max(1, Number(arguments[0] || 200));
            const isVisible = (el) => {
              if (!el || !el.isConnected) return false;
              const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
              if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
              const rects = typeof el.getClientRects === 'function' ? el.getClientRects() : null;
              return !!(rects && rects.length);
            };
            const out = [];
            const seen = new Set();
            for (const el of document.querySelectorAll('a[href]')) {
              if (out.length >= limit) break;
              if (!isVisible(el)) continue;
              const href = String(el.getAttribute('href') || el.href || '').trim();
              if (!href || seen.has(href)) continue;
              const lowered = href.toLowerCase();
              const looksEmailBearing = (
                lowered.startsWith('mailto:')
                || lowered.includes('@')
                || lowered.includes('%40')
                || lowered.includes('email=')
                || lowered.includes('email%3d')
              );
              if (!looksEmailBearing) continue;
              seen.add(href);
              out.push(href);
            }
            return out;
            """,
            int(limit),
        ) or []
    except Exception:
        values = []

    if isinstance(values, str):
        values = [values]
    return [str(value or "").strip() for value in values if str(value or "").strip()]


def _collect_fb_email_surface_state(
    driver,
    logger: LoggerFn = None,
) -> Tuple[str, str, List[str], List[str]]:
    reveal_actions = _reveal_fb_contact_controls(driver, logger=logger)
    try:
        scrolled = driver.execute_script(
            """
            /* fb_email_surface_post_reveal_scroll */
            const scrollRoot = document.scrollingElement || document.documentElement || document.body;
            const viewportHeight = Math.max(0, Number(window.innerHeight || 0));
            const maxScrollTop = Math.max(0, Number((scrollRoot && scrollRoot.scrollHeight) || 0) - viewportHeight);
            const targetOffset = Math.max(320, Math.min(900, Math.round(Number((scrollRoot && scrollRoot.scrollHeight) || 0) * 0.25)));
            const currentOffset = Math.max(0, Number(window.pageYOffset || window.scrollY || 0));
            const nextOffset = Math.min(maxScrollTop, targetOffset);
            const delta = Math.max(0, nextOffset - currentOffset);
            if (delta > 0) {
              window.scrollBy(0, delta);
            }
            return delta;
            """
        ) or 0
    except Exception:
        scrolled = 0
    if scrolled:
        time.sleep(0.15)
    try:
        page_source = getattr(driver, "page_source", "") or ""
    except Exception:
        page_source = ""
    rendered_text = _extract_fb_visible_text_with_container_fallback(driver)
    anchor_values = _collect_fb_live_anchor_targets(driver)
    return page_source, rendered_text, anchor_values, reveal_actions


def _log_fb_email_surface_debug(logger: LoggerFn, label: str, page_source: str, rendered_text: str) -> None:
    if os.getenv("FB_DEBUG_EMAIL_SURFACES") != "1":
        return

    page_source = str(page_source or "")
    rendered_text = str(rendered_text or "")
    rendered_preview = " ".join(rendered_text.split())[:500]
    rendered_match = bool(EMAIL_REGEX.search(rendered_text))
    source_match = bool(EMAIL_REGEX.search(page_source))
    _log(
        logger,
        "[FB Email][debug] "
        f"surface={label} rendered_len={len(rendered_text)} "
        f"rendered_match={1 if rendered_match else 0} "
        f"source_match={1 if source_match else 0} "
        f"rendered_preview={rendered_preview!r}",
    )


_FB_EMAIL_AT_TOKEN = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\bat\b|@)"
_FB_EMAIL_DOT_TOKEN = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\bdot\b|\.)"
_FB_OBFUSCATED_EMAIL_PATTERN = re.compile(
    rf"([A-Z0-9._%+-]+)\s*{_FB_EMAIL_AT_TOKEN}\s*([A-Z0-9-]+(?:\s*{_FB_EMAIL_DOT_TOKEN}\s*[A-Z0-9-]+)+)",
    re.IGNORECASE,
)


def _normalize_fb_obfuscated_email_text(text: str) -> Tuple[str, int]:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    replacements = 0
    try:
        from email_normalizer import normalize_obfuscated_email_patterns

        normalized, base_replacements = normalize_obfuscated_email_patterns(normalized)
        replacements += base_replacements
    except Exception:
        pass

    def _repl(match: re.Match) -> str:
        nonlocal replacements
        local = (match.group(1) or "").strip()
        domain = re.sub(_FB_EMAIL_DOT_TOKEN, ".", match.group(2) or "", flags=re.IGNORECASE)
        domain = re.sub(r"\s+", "", domain)
        domain = re.sub(r"\.{2,}", ".", domain).strip(".")
        candidate = f"{local}@{domain}" if local and domain else match.group(0)
        if candidate != match.group(0):
            replacements += 1
        return candidate

    normalized = _FB_OBFUSCATED_EMAIL_PATTERN.sub(_repl, normalized)
    return normalized, replacements


def _fb_sample_needs_obfuscation_normalization(sample: str) -> bool:
    sample = str(sample or "")
    if not sample:
        return False

    normalized = unicodedata.normalize("NFKC", sample)
    lowered = normalized.lower()

    if any(token in lowered for token in ("[at]", "(at)", " at ", "[dot]", "(dot)", " dot ", "%40")):
        return True

    if re.search(r"[A-Z0-9._%+-]+\s+@\s+[A-Z0-9.-]+\s*\.\s*[A-Z]{2,}", normalized, re.IGNORECASE):
        return True

    return False


def _extract_fb_emails_from_text_sample(sample: str) -> List[str]:
    sample = str(sample or "")
    if not sample:
        return []
    replacements = 0
    normalized = sample
    if _fb_sample_needs_obfuscation_normalization(sample):
        try:
            normalized, replacements = _normalize_fb_obfuscated_email_text(sample)
        except Exception:
            normalized = sample
            replacements = 0
    if replacements:
        try:
            from pipeline_runner import increment_pattern_emails

            increment_pattern_emails(replacements)
        except Exception:
            pass
    return [match.group(0) for match in EMAIL_REGEX.finditer(normalized)]


def _extract_emails_from_html(
    html: str,
    soup: Optional[BeautifulSoup] = None,
    rendered_text: str = "",
    anchor_values: Optional[Sequence[str]] = None,
    stop_after_first_filtered: bool = False,
) -> Tuple[List[str], bool]:
    mailto_used = False
    raw_html = html if isinstance(html, str) else str(html or "")
    if not raw_html and not rendered_text and not anchor_values:
        return [], mailto_used

    soup_input_char_limit = 262144

    def _href_samples_from_html(source_html: str) -> List[str]:
        if not source_html:
            return []
        href_samples: List[str] = []
        href_pattern = re.compile(
            r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>]+))""",
            re.IGNORECASE,
        )
        for match in href_pattern.finditer(source_html):
            href = next((group for group in match.groups() if group), "")
            href = str(href or "").strip()
            if href:
                href_samples.append(href)
        return href_samples

    def _extract_anchor_candidates(values: Sequence[str]) -> Tuple[List[str], bool]:
        candidates: List[str] = []
        used_mailto = False
        for raw_value in values or ():
            raw_value = str(raw_value or "").strip()
            if not raw_value:
                continue
            samples = [raw_value]
            try:
                decoded = urllib.parse.unquote(raw_value)
            except Exception:
                decoded = raw_value
            if decoded and decoded not in samples:
                samples.append(decoded)
            for sample in samples:
                lowered = sample.lower()
                if lowered.startswith("mailto:"):
                    addr = sample.split("mailto:", 1)[-1].split("?", 1)[0]
                    if addr:
                        candidates.append(addr)
                        used_mailto = True
                    continue
                candidates.extend(_extract_fb_emails_from_text_sample(sample))
        return candidates, used_mailto

    def _finalize_emails(candidates: Sequence[str]) -> List[str]:
        filtered_emails = _filter_low_quality_fb_emails(list(candidates or []))
        unique: List[str] = []
        seen: Set[str] = set()
        for email in filtered_emails:
            normalized = normalize_email_value(str(email or "").strip())
            if normalized and _is_contact_quality_email(normalized) and normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique

    cheap_candidates: List[str] = []
    anchor_samples = [str(value or "").strip() for value in (anchor_values or []) if str(value or "").strip()]
    if raw_html:
        anchor_samples.extend(_href_samples_from_html(raw_html))
    if anchor_samples:
        mailto_anchor_samples = [sample for sample in anchor_samples if sample.lower().startswith("mailto:")]
        non_mailto_anchor_samples = [sample for sample in anchor_samples if not sample.lower().startswith("mailto:")]
        anchor_samples = [*mailto_anchor_samples, *non_mailto_anchor_samples]

    if anchor_samples:
        if stop_after_first_filtered:
            for anchor_sample in anchor_samples:
                anchor_candidates, anchor_mailto = _extract_anchor_candidates([anchor_sample])
                cheap_candidates.extend(anchor_candidates)
                mailto_used = mailto_used or anchor_mailto
                filtered_emails = _finalize_emails(cheap_candidates)
                if filtered_emails:
                    return filtered_emails, mailto_used
        else:
            anchor_candidates, anchor_mailto = _extract_anchor_candidates(anchor_samples)
            cheap_candidates.extend(anchor_candidates)
            mailto_used = mailto_used or anchor_mailto

    if raw_html:
        cheap_candidates.extend(_extract_fb_emails_from_text_sample(raw_html))
        filtered_emails = _finalize_emails(cheap_candidates)
        if filtered_emails:
            return filtered_emails, mailto_used

    if raw_html:
        bounded_html = raw_html[:soup_input_char_limit]
        if soup is not None and (not raw_html or len(raw_html) <= soup_input_char_limit):
            parsed_soup = soup
        else:
            parsed_soup = BeautifulSoup(bounded_html, "html.parser")
        text_blob = parsed_soup.get_text(" ", strip=True) if parsed_soup else ""
        if text_blob:
            filtered_emails = _finalize_emails(_extract_fb_emails_from_text_sample(text_blob))
            if filtered_emails:
                return filtered_emails, mailto_used

    visible_text = str(rendered_text or "")
    if visible_text:
        filtered_emails = _finalize_emails(_extract_fb_emails_from_text_sample(visible_text))
        if filtered_emails:
            return filtered_emails, mailto_used

    return [], mailto_used


def _is_contact_quality_email(email: str) -> bool:
    normalized = normalize_email_value(email)
    if not normalized:
        return False
    if any(char.isspace() for char in normalized):
        return False

    local, domain = normalized.split("@", 1)
    if not local or not domain:
        return False
    if not local[0].isalnum():
        return False
    if not any(char.isalpha() for char in local):
        return False
    if local in _CONTACT_QUALITY_REJECT_LOCAL_PARTS:
        return False

    domain_lower = domain.lower().rstrip(".")
    if not domain_lower:
        return False
    if any(domain_lower.endswith(suffix) for suffix in _CONTACT_QUALITY_REJECT_DOMAIN_SUFFIXES):
        return False

    return True


def _fb_domain_has_file_like_artifact(domain: str) -> bool:
    domain = str(domain or "").strip().lower().strip(".")
    if not domain:
        return False
    dotted_domain = f".{domain}"
    for extension in _FB_LOW_QUALITY_FILE_EXTENSIONS:
        token = f".{extension}"
        if dotted_domain.endswith(token) or f"{token}." in dotted_domain:
            return True
    return False


def _fb_email_looks_like_artifact_text(local: str, domain: str) -> bool:
    local = str(local or "").strip().lower()
    if local not in _FB_LOW_QUALITY_SHORT_LOCAL_PARTS:
        return False
    labels = [label for label in str(domain or "").strip().lower().split(".") if label]
    if len(labels) != 2:
        return False
    registrable, tld = labels
    if not (registrable.isalpha() and tld.isalpha()):
        return False
    return len(registrable) >= 6 and len(tld) >= 8


def _filter_low_quality_fb_emails(emails: List[str]) -> List[str]:
    filtered: List[str] = []
    seen: Set[str] = set()
    for raw_email in emails or []:
        normalized = normalize_email_value(raw_email)
        if not normalized or normalized in seen:
            continue
        local, domain = normalized.split("@", 1)
        if _fb_domain_has_file_like_artifact(domain):
            continue
        if _fb_email_looks_like_artifact_text(local, domain):
            continue
        seen.add(normalized)
        filtered.append(normalized)
    return filtered


def _choose_primary_email(
    emails: Sequence[str],
    artist_slug: str,
    source_context: Optional[Dict] = None,
) -> Optional[str]:
    if not emails:
        return None
    ranked = _rank_fb_email_candidates(
        list(emails),
        artist_slug=artist_slug,
        source_context=source_context,
    )
    return ranked[0] if ranked else None


def _fb_email_surface_bonus(email: str, source_context: Optional[Dict]) -> int:
    if not source_context:
        return 0

    normalized = normalize_email_value(email)
    raw_surface: Any = None

    if isinstance(source_context.get("surfaces"), dict):
        raw_surface = source_context["surfaces"].get(normalized) or source_context["surfaces"].get(email)

    if raw_surface is None:
        raw_surface = source_context.get(normalized) or source_context.get(email) or source_context.get("surface")

    if isinstance(raw_surface, dict):
        raw_surface = raw_surface.get("surface") or raw_surface.get("email_source") or raw_surface.get("source")

    surface = str(raw_surface or "").strip().lower()
    if not surface:
        return 0
    if "about" in surface or "contact" in surface:
        return 3
    if "mailto" in surface:
        return 1
    return 0


def _fb_email_surface_label(surface: str, *, used_mailto: bool = False) -> str:
    normalized_surface = str(surface or "").strip().lower() or "main"
    if not used_mailto:
        return normalized_surface
    if normalized_surface in {"about", "contact"}:
        return f"{normalized_surface}_mailto"
    return "mailto"


def _fb_contact_surface_label(contact_url: str) -> str:
    lowered = str(contact_url or "").strip().lower()
    if "contact" in lowered:
        return "contact"
    return "about"


@dataclass
class FacebookAcceptedPageFetchResult:
    requested_url: str = ""
    resolved_url: str = ""
    html: str = ""
    rendered_text: str = ""
    anchor_values: List[str] = field(default_factory=list)
    status_reason: str = ""


@dataclass
class FacebookAcceptedPageSweepResult:
    main_surface: Optional[FacebookAcceptedPageFetchResult] = None
    secondary_surface: Optional[FacebookAcceptedPageFetchResult] = None
    main_emails: List[str] = field(default_factory=list)
    secondary_emails: List[str] = field(default_factory=list)
    combined_emails: List[str] = field(default_factory=list)
    main_mailto: bool = False
    secondary_mailto: bool = False
    secondary_attempted: bool = False
    status_reason: str = ""
    secondary_status_reason: str = ""
    final_resolved_url: str = ""


def _run_bounded_fb_accepted_page_sweep(
    fb_url: str,
    fetch_surface,
    *,
    select_secondary_url=None,
    fallback_secondary_urls=None,
    refresh_main_surface=None,
    continue_after_main_email: bool = False,
    stop_after_first_filtered: bool = False,
    on_secondary_selected=None,
    on_secondary_fallback=None,
    on_no_secondary=None,
) -> FacebookAcceptedPageSweepResult:
    """
    Neutral accepted-page sweep shared by daytime and Night wrappers.

    The helper stays bounded to at most two fetches:
      - main page
      - one selected/fallback contact/about page
    """

    result = FacebookAcceptedPageSweepResult()
    target_url = str(fb_url or "").strip()
    if not target_url:
        result.status_reason = "no_fb_url"
        return result

    visited: Set[str] = set()

    def _visit_key(url: str) -> str:
        return str(url or "").split("#", 1)[0].strip()

    def _extract_surface_emails(surface: Optional[FacebookAcceptedPageFetchResult]) -> Tuple[List[str], bool]:
        if surface is None:
            return [], False
        return _extract_emails_from_html(
            surface.html or "",
            rendered_text=surface.rendered_text or "",
            anchor_values=surface.anchor_values or [],
            stop_after_first_filtered=stop_after_first_filtered,
        )

    def _dedupe(values: Sequence[str]) -> List[str]:
        deduped: List[str] = []
        seen: Set[str] = set()
        for value in values or ():
            normalized = normalize_email_value(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    main_surface = fetch_surface(target_url)
    result.main_surface = main_surface
    if main_surface is None:
        result.status_reason = "fetch_error"
        return result

    main_key = _visit_key(main_surface.resolved_url or main_surface.requested_url or target_url)
    if main_key:
        visited.add(main_key)
    result.status_reason = str(main_surface.status_reason or "")
    result.final_resolved_url = main_surface.resolved_url or target_url

    if result.status_reason and not (main_surface.html or main_surface.rendered_text or main_surface.anchor_values):
        return result

    main_emails, main_mailto = _extract_surface_emails(main_surface)
    result.main_emails = list(main_emails or [])
    result.main_mailto = bool(main_mailto)

    if refresh_main_surface and not result.main_emails and not result.status_reason:
        refreshed_surface = refresh_main_surface(main_surface, list(result.main_emails))
        if refreshed_surface is not None:
            result.main_surface = main_surface = refreshed_surface
            refreshed_key = _visit_key(main_surface.resolved_url or main_surface.requested_url or target_url)
            if refreshed_key:
                visited.add(refreshed_key)
            result.status_reason = str(main_surface.status_reason or "")
            result.final_resolved_url = main_surface.resolved_url or result.final_resolved_url or target_url
            if result.status_reason and not (main_surface.html or main_surface.rendered_text or main_surface.anchor_values):
                return result
            main_emails, main_mailto = _extract_surface_emails(main_surface)
            result.main_emails = list(main_emails or [])
            result.main_mailto = bool(main_mailto)

    result.combined_emails = _dedupe(result.main_emails)
    if result.main_emails and not continue_after_main_email:
        return result

    main_html = main_surface.html or ""
    base_url = main_surface.resolved_url or target_url
    secondary_url = ""
    fallback_used = False

    if select_secondary_url and main_html:
        try:
            secondary_url = str(select_secondary_url(base_url, main_html) or "").strip()
        except Exception:
            secondary_url = ""

    if not secondary_url and fallback_secondary_urls:
        try:
            fallback_candidates = list(fallback_secondary_urls(base_url) or [])
        except Exception:
            fallback_candidates = []
        for candidate in fallback_candidates:
            candidate_url = str(candidate or "").strip()
            if not candidate_url:
                continue
            if _visit_key(candidate_url) in visited:
                continue
            secondary_url = candidate_url
            fallback_used = True
            break

    if not secondary_url:
        if callable(on_no_secondary):
            on_no_secondary()
        return result

    if fallback_used:
        if callable(on_secondary_fallback):
            on_secondary_fallback(secondary_url)
    elif callable(on_secondary_selected):
        on_secondary_selected(secondary_url)

    secondary_surface = fetch_surface(secondary_url)
    result.secondary_attempted = True
    result.secondary_surface = secondary_surface
    if secondary_surface is None:
        result.secondary_status_reason = "fetch_error"
        return result

    result.secondary_status_reason = str(secondary_surface.status_reason or "")
    result.final_resolved_url = secondary_surface.resolved_url or result.final_resolved_url or secondary_url
    if result.secondary_status_reason and not (
        secondary_surface.html or secondary_surface.rendered_text or secondary_surface.anchor_values
    ):
        return result

    secondary_emails, secondary_mailto = _extract_surface_emails(secondary_surface)
    result.secondary_emails = list(secondary_emails or [])
    result.secondary_mailto = bool(secondary_mailto)
    result.combined_emails = _dedupe([*result.main_emails, *result.secondary_emails])
    return result


def _fb_email_domain_quality_bonus(domain: str, artist_slug: str = "") -> int:
    cleaned = str(domain or "").strip().lower().strip(".")
    if not cleaned or cleaned in _FB_GENERIC_EMAIL_PROVIDER_DOMAINS:
        return 0
    labels = [label for label in cleaned.split(".") if label]
    if len(labels) < 2:
        return 0
    if _fb_domain_has_file_like_artifact(cleaned):
        return 0
    if any(not label.replace("-", "").isalnum() for label in labels):
        return 0
    tld = labels[-1]
    if not tld.isalpha() or len(tld) < 2:
        return 0
    slug = _slugify(artist_slug)
    if slug and slug not in _slugify(cleaned):
        return 0
    return 3


def _rank_fb_email_candidates(
    emails: List[str],
    *,
    artist_slug: str,
    source_context: Optional[Dict] = None,
) -> List[str]:
    slug = _slugify(artist_slug)
    scored: List[Tuple[int, int, str]] = []

    for index, email in enumerate(emails or []):
        normalized = normalize_email_value(email)
        if "@" not in normalized:
            scored.append((0, index, email))
            continue

        local, domain = normalized.split("@", 1)
        score = 0

        if slug and slug in _slugify(normalized):
            score += 5
        score += _fb_email_domain_quality_bonus(domain, artist_slug)
        if any(local.startswith(prefix) for prefix in _FB_ROLE_EMAIL_PREFIXES):
            score -= 2
        if any(token in local for token in _FB_BOOKING_EMAIL_TOKENS):
            score += 2
        score += _fb_email_surface_bonus(normalized, source_context)

        scored.append((score, index, email))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [email for _score, _index, email in scored]


def _merge_email_all(existing: str, new_emails: Sequence[str]) -> str:
    merged = filter_system_telemetry_emails([*list(_split_multi(existing)), *list(new_emails)])
    return ";".join(merged)


def _scrape_fb_page_unearthed_legacy(driver, fb_url: str, logger: LoggerFn = None) -> Tuple[List[str], str, str]:
    """
    Legacy-style public scrape: navigate directly to a FB URL and regex emails from span nodes.
    Returns (emails, status, resolved_url).
    """
    url_raw = (fb_url or "").strip()
    if not url_raw or "facebook.com" not in url_raw.lower():
        return [], "no_fb_url", url_raw
    url = _normalise_fb_url(url_raw)
    if not url:
        return [], "no_fb_url", url_raw
    normalized = url.rstrip("/").lower()
    excluded = {
        "https://www.facebook.com/triplejunearthed",
        "https://www.facebook.com/abc",
    }
    if normalized in excluded:
        _log(logger, f"[Night FB][Unearthed] Skipping excluded FB URL: {url}")
        return [], "excluded_url", url

    _log(logger, f"[Night FB][Unearthed] Legacy-style scrape for FB page: {url}")
    try:
        driver.get(url)
        try:
            about_button = WebDriverWait(driver, 0.5).until(
                EC.element_to_be_clickable((By.XPATH, '//a[@href="#about"]'))
            )
            _log(logger, "[Night FB][Unearthed] 'About' button found; clicking.")
            about_button.click()
        except Exception:
            _log(logger, "[Night FB][Unearthed] 'About' button not clickable; continuing on main page.")
        try:
            WebDriverWait(driver, 0.5).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            pass
        resolved_url = _normalise_fb_url(getattr(driver, "current_url", "") or url)
        if _is_fb_login_or_security_url(resolved_url):
            return [], "login_redirect", resolved_url or url
        soup = BeautifulSoup(driver.page_source, "html.parser")
        raw_emails: List[str] = []
        for span in soup.find_all("span", class_=re.compile(".*x193iq5w.*")):
            txt = span.get_text(strip=True)
            if not txt:
                continue
            raw_emails.extend(match.group(0) for match in EMAIL_REGEX.finditer(txt))
        emails = sorted(set(raw_emails))
        if not emails:
            _log(logger, f"[Night FB][Unearthed] No emails found on {resolved_url}")
            return [], "no_emails", resolved_url or url
        _log(logger, f"[Night FB][Unearthed] Found {len(emails)} email(s) on {resolved_url}")
        return emails, "ok", resolved_url or url
    except Exception as exc:
        try:
            _log(logger, f"[Night FB][Unearthed] Error scraping {url}: {exc}")
        except Exception:
            pass
        return [], "error", url


def _is_fb_share_url_str(url: str) -> bool:
    if not url:
        return False
    u = str(url).strip().lower()
    return any(tok in u for tok in (
        "facebook.com/share/",
        "m.facebook.com/share/",
        "web.facebook.com/share/",
        "touch.facebook.com/share/",
    ))


def _is_allowed_fb_share_entrypoint_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = "https://www.facebook.com" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return False

    host = (parsed.netloc or "").lower()
    if host not in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "touch.facebook.com"}:
        return False

    path = (parsed.path or "").rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) != 2 or segments[0].lower() != "share":
        return False

    token = (segments[1] or "").strip()
    return bool(token and not token.lower().endswith(".php"))


def _dedupe_candidates(candidates: Iterable["facebook_enrich.FbCandidate"]) -> List["facebook_enrich.FbCandidate"]:
    seen = set()
    deduped: List["facebook_enrich.FbCandidate"] = []
    for cand in candidates or []:
        key = _normalise_fb_url(getattr(cand, "url", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(cand)
    return deduped


def _candidate_url(cand) -> str:
    """
    Safely extract URL from FbCandidate or dict-like candidate.
    Strips whitespace/newlines; returns '' if missing.
    """
    url = ""
    if hasattr(cand, "url"):
        try:
            url = getattr(cand, "url", "") or ""
        except Exception:
            url = ""
    elif isinstance(cand, dict):
        for key in ("url", "href", "link"):
            if key in cand and cand.get(key):
                url = cand.get(key) or ""
                break
    url = (url or "").replace("\n", "").replace("\t", "").strip()
    return url


if os.getenv("FB_DEBUG_CAND_URL_GATE") == "1":
    assert _candidate_url(type("X", (), {"url": "https://www.facebook.com/someband"})()) == "https://www.facebook.com/someband"
    assert _candidate_url({"url": "https://www.facebook.com/someband"}) == "https://www.facebook.com/someband"
    try:
        assert _fb_is_candidate_url_allowed("https://www.facebook.com/someband?__tn__=%2Cd")
        assert not _fb_is_candidate_url_allowed("https://www.facebook.com/groups/foo")
        assert not _fb_is_candidate_url_allowed("https://www.facebook.com/someband/about")
        assert _fb_is_candidate_url_allowed("https://www.facebook.com/profile.php?id=12&foo=1")
        assert _normalise_fb_url("https://www.facebook.com/shelaibd")
        assert _normalise_fb_url("https://www.facebook.com/profile.php?id=61554027368639")
        assert _normalise_fb_url("https://web.facebook.com/someband")
        assert _normalise_fb_url("https://www.facebook.com/profile.php?id=61554027368639&__tn__=%3C") == "https://www.facebook.com/profile.php?id=61554027368639"
        assert _fb_is_candidate_url_allowed("https://www.facebook.com/profile.php?id=61554027368639") is True
        assert _is_junk_fb_candidate("https://www.facebook.com/shelaibd") is False
        assert _is_junk_fb_candidate("https://www.facebook.com/watch") is True
        assert _is_junk_fb_candidate("https://www.facebook.com/notifications/?notif_id=123") is True
    except Exception:
        pass


_FB_NAME_NOISE_TOKENS = {
    "account",
    "band",
    "music",
    "musician",
    "official",
    "page",
    "record",
    "records",
    "verified",
}


def _candidate_tokens(text: str) -> List[str]:
    normalized = unicodedata.normalize("NFKD", (text or "").strip().lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return [tok for tok in tokens if tok not in _FB_NAME_NOISE_TOKENS]


def _is_profile_url(url: str) -> bool:
    url_l = (url or "").lower()
    if "profile.php" in url_l:
        return True
    try:
        parsed = urllib.parse.urlparse(url_l)
        path = parsed.path or ""
        if re.match(r"^/people/[^/]+/\d+(?:/.*)?$", path):
            return True
        if path.startswith("/people/"):
            return True
    except Exception:
        return False
    return False


def _is_page_style_url(url: str) -> bool:
    if not url:
        return False
    if _is_profile_url(url):
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path or ""
        parts = [p for p in path.split("/") if p]
        if not parts:
            return False
        if parts[0] in ("people",):
            return False
        # /<slug>
        if len(parts) == 1:
            return True
        # /p/<slug> or /pages/<name>/<id>
        if parts[0] in ("p", "pages"):
            return True
        return False
    except Exception:
        return False


def _is_page_like_category(category: str, descriptor: str, category_tokens: Iterable[str], music_hint: bool = False) -> bool:
    """
    Heuristic: treat certain category/descriptor/token blobs as page-like even when the URL uses profile.php.
    """
    pieces = [category or "", descriptor or ""]
    pieces.extend([t or "" for t in category_tokens or []])
    blobs = " ".join(p.strip().lower() for p in pieces if p)
    page_tokens = (
        "musician/band",
        "musician",
        "artist",
        "band",
        "music",
        "record label",
        "music artist",
        "public figure",
        "official page",
        "fan page",
    )
    if any(tok in blobs for tok in page_tokens):
        return True
    if music_hint and blobs:
        return True
    return False


def _candidate_name_match(artist_name: str, cand_name: str) -> str:
    artist_slug = _slugify(artist_name)
    name_slug = _slugify(cand_name)
    if artist_slug and name_slug and artist_slug == name_slug:
        return "exact"
    artist_tokens = set(_candidate_tokens(artist_name))
    cand_tokens = set(_candidate_tokens(cand_name))
    if not artist_tokens or not cand_tokens:
        return "none"
    overlap = len(artist_tokens & cand_tokens)
    union = len(artist_tokens | cand_tokens) or 1
    jaccard = overlap / union
    # Keep "near" limited to strong token overlap. Raw slug containment is too
    # permissive and upgrades fan/tribute/location variants that should stay weak.
    if jaccard >= 0.75:
        return "near"
    if jaccard <= 0.25:
        return "mismatch"
    return "weak"


def _candidate_category_flags(
    category: Optional[str],
    aria_label: str = "",
    secondary_text: str = "",
    descriptor: str = "",
    category_tokens: Optional[Iterable[str]] = None,
) -> Dict[str, bool]:
    cat_clean = _sanitize_fb_category_text(category) or ""
    desc_clean = _sanitize_fb_category_text(descriptor) or _sanitize_fb_category_text(secondary_text) or ""
    cat = cat_clean.lower()
    desc = desc_clean.lower()
    aria = (aria_label or "").lower()
    secondary = (secondary_text or "").lower()
    token_blob = " ".join(t.lower() for t in (category_tokens or []) if t)
    blob = " ".join(part for part in (cat, desc, aria, secondary, token_blob) if part)

    def _has_music(val: str) -> bool:
        return _text_has_music_tokens(val)

    def _has_service(val: str) -> bool:
        v = (val or "").lower()
        return any(tok in v for tok in _FB_SERVICE_CATEGORY_TOKENS)

    music_primary = _has_music(cat_clean)
    music_descriptor = _has_music(desc_clean)
    music_blob = _has_music(blob)
    service_any = _has_service(blob)

    return {
        "music_primary": music_primary,
        "music_descriptor": music_descriptor,
        "music_any": music_blob,
        "service_any": service_any,
        "service_only": service_any and not music_blob,
    }


def _score_fb_candidate_night(artist_name: str, cand) -> Tuple[int, List[str], Dict[str, Any]]:
    """
    Compute simple, controllable score for a candidate.
    Returns (score, breakdown_tokens, feature_dict).
    """
    name = getattr(cand, "name", "") or (cand.get("name") if isinstance(cand, dict) else "") or ""
    url = _candidate_url(cand)
    raw_category = getattr(cand, "category", "") or (cand.get("category") if isinstance(cand, dict) else "") or ""
    category = _sanitize_fb_category_text(raw_category) or ""
    aria_label = getattr(cand, "aria_label", "") or (cand.get("aria_label") if isinstance(cand, dict) else "") or ""
    secondary_text = getattr(cand, "secondary_text", "") or (cand.get("secondary_text") if isinstance(cand, dict) else "") or ""
    descriptor = getattr(cand, "descriptor", "") or ""
    category_tokens = getattr(cand, "category_tokens", None) or []

    flags = _candidate_category_flags(category, aria_label, secondary_text, descriptor=descriptor, category_tokens=category_tokens)
    page_like = _is_page_like_category(category, descriptor, category_tokens, music_hint=flags["music_any"])
    is_profile = _is_profile_url(url)
    page_style_url = _is_page_style_url(url)
    is_page = page_style_url
    if page_like and is_profile:
        is_profile = False
        is_page = True
    elif page_like:
        is_page = True
    match_level = _candidate_name_match(artist_name, name)
    has_music_signals = flags["music_any"]

    score = 0
    breakdown: List[str] = []
    weights = _FB_RANK_WEIGHTS

    if flags["music_primary"]:
        score += weights["primary_music"]
        breakdown.append("+primary_music")
    if flags["music_descriptor"]:
        score += weights["descriptor_music"]
        breakdown.append("+descriptor_music")
    if not (flags["music_primary"] or flags["music_descriptor"]) and flags["music_any"]:
        score += weights["any_music_token"]
        breakdown.append("+music_token")

    if is_profile:
        profile_penalty = weights["profile_penalty_music"] if has_music_signals else weights["profile_penalty"]
        breakdown.append("-profile_music" if has_music_signals else "-profile")
        score += profile_penalty
    elif has_music_signals and is_page:
        score += weights["page_bonus_music"]
        breakdown.append("+page_music")

    if flags["service_only"]:
        score += weights["service_only"]
        breakdown.append("-service_only")
    elif flags["service_any"] and flags["music_any"]:
        score += weights["service_mixed"]
        breakdown.append("-service_mixed")

    if match_level == "exact":
        score += weights["exact_name"]
        breakdown.append("+exact_match")
    elif match_level == "near":
        score += weights["near_name"]
        breakdown.append("+near_match")
    elif match_level == "mismatch":
        score += weights["name_mismatch"]
        breakdown.append("-name_mismatch")

    features = {
        "name": name,
        "url": url,
        "category": category,
        "category_raw": raw_category,
        "descriptor": descriptor,
        "aria_label": aria_label,
        "secondary_text": secondary_text,
        "category_tokens": list(category_tokens),
        "is_profile": is_profile,
        "is_page": is_page,
        "is_page_style_url": page_style_url,
        "match_level": match_level,
        "music_primary": flags["music_primary"],
        "music_descriptor": flags["music_descriptor"],
        "music_any": flags["music_any"],
        "service_any": flags["service_any"],
        "service_only": flags["service_only"],
    }
    return score, breakdown, features


def _score_candidate_min_quality(artist: str, cand) -> Tuple[int, List[str], Dict[str, Any]]:
    """
    Lightweight, explainable heuristic to catch obviously-bad top candidates before scraping.
    Returns (score, reasons, meta).
    """
    name = getattr(cand, "name", "") or (cand.get("name") if isinstance(cand, dict) else "") or ""
    url = _candidate_url(cand)
    category = _sanitize_fb_category_text(getattr(cand, "category", "") or (cand.get("category") if isinstance(cand, dict) else "") or "") or ""
    descriptor = getattr(cand, "descriptor", "") or (cand.get("descriptor") if isinstance(cand, dict) else "") or ""
    aria_label = getattr(cand, "aria_label", "") or (cand.get("aria_label") if isinstance(cand, dict) else "") or ""
    category_tokens = getattr(cand, "category_tokens", None) or (cand.get("category_tokens") if isinstance(cand, dict) else []) or []

    score = 0
    reasons: List[str] = []

    try:
        if _is_junk_fb_candidate(url):
            score -= 50
            reasons.append("-junk_surface")
    except Exception:
        pass

    if _is_page_style_url(url):
        score += 10
        reasons.append("+page_style_url")
    if _is_profile_url(url):
        score -= 5
        reasons.append("-profile_url")

    match_level = _candidate_name_match(artist, name)
    if match_level == "exact":
        score += 25
        reasons.append("+exact_match")
    elif match_level == "near":
        score += 15
        reasons.append("+near_match")
    elif match_level == "weak":
        score += 5
        reasons.append("+weak_match")
    elif match_level == "mismatch":
        score -= 25
        reasons.append("-name_mismatch")

    music_hint_fields = " ".join([category, descriptor, aria_label])
    has_music_hint = _text_has_music_tokens(music_hint_fields)
    category_tokens_blob = " ".join(category_tokens) if isinstance(category_tokens, (list, tuple, set)) else str(category_tokens)
    has_music_tokens_in_category_tokens = _text_has_music_tokens(category_tokens_blob)
    if has_music_hint:
        score += 20
        reasons.append("+music_hint")
    if has_music_tokens_in_category_tokens:
        score += 10
        reasons.append("+music_token_category")

    blob = " ".join([category, descriptor, aria_label, category_tokens_blob]).lower()
    has_service = any(tok in blob for tok in _FB_SERVICE_CATEGORY_TOKENS)
    if has_service and not (has_music_hint or has_music_tokens_in_category_tokens):
        score -= 15
        reasons.append("-service_only")

    meta = {
        "name": name,
        "url": url,
        "category": category,
        "descriptor": descriptor,
        "aria_label": aria_label,
        "category_tokens": list(category_tokens) if isinstance(category_tokens, (list, tuple, set)) else category_tokens,
        "match_level": match_level,
        "music_hint": has_music_hint,
        "music_tokens_in_category_tokens": has_music_tokens_in_category_tokens,
        "service_only": has_service and not (has_music_hint or has_music_tokens_in_category_tokens),
    }
    return score, reasons, meta


def _rank_candidates_for_preview(artist_name: str, candidates: List["facebook_enrich.FbCandidate"]) -> List[Dict[str, Any]]:
    ranked = []
    for idx, cand in enumerate(candidates or []):
        score, breakdown, features = _score_fb_candidate_night(artist_name, cand)
        ranked.append(
            {
                "idx": idx,
                "candidate": cand,
                "score": score,
                "breakdown": breakdown,
                "features": features,
            }
        )

    def _tie_key(item: Dict[str, Any]):
        feat = item["features"]
        match_rank = {"exact": 0, "near": 1, "weak": 2, "mismatch": 3, "none": 4}.get(feat.get("match_level") or "none", 4)
        return (
            -int(item["score"]),
            match_rank,
            -int(feat.get("music_primary") or False),
            -int(feat.get("music_descriptor") or False),
            -int(feat.get("music_any") or False),
            -int(feat.get("is_page_style_url") or False),
            -int(feat.get("is_page") or False),
            int(feat.get("is_profile") or False),
            item["idx"],
        )

    ranked.sort(key=_tie_key)
    return ranked


def _order_candidates_for_selection(
    primary_candidate,
    candidates: List["facebook_enrich.FbCandidate"],
    ranked_candidates: List["facebook_enrich.FbCandidate"],
    ranking_enabled: bool,
) -> List["facebook_enrich.FbCandidate"]:
    """
    Build a deterministic, duplicate-free candidate order for post-gate evaluation.
    """
    ordered: List["facebook_enrich.FbCandidate"] = []
    seen_ids = set()

    def _add(c):
        cid = id(c)
        if cid in seen_ids:
            return
        seen_ids.add(cid)
        ordered.append(c)

    if ranking_enabled and ranked_candidates:
        for c in ranked_candidates:
            _add(c)
        for c in candidates or []:
            _add(c)
    else:
        if primary_candidate:
            _add(primary_candidate)
        for c in candidates or []:
            if c is primary_candidate:
                continue
            _add(c)
    return ordered


def _maybe_log_rank_preview(
    artist: str,
    candidates: List["facebook_enrich.FbCandidate"],
    chosen_current: Optional["facebook_enrich.FbCandidate"],
    logger: LoggerFn = None,
    selected_by: str = "",
) -> None:
    if candidates is None:
        return
    try:
        preview_n = int(os.getenv("FB_CANDIDATE_RANKING_PREVIEW_N") or 5)
    except Exception:
        preview_n = 5
    ranked = _rank_candidates_for_preview(artist, candidates)
    if not ranked:
        return
    flag_enabled = _bool_env("FB_CANDIDATE_RANKING", default=False)
    debug = _bool_env("FB_CANDIDATE_RANKING_DEBUG", default=False)
    chosen_label = getattr(chosen_current, "name", None) or getattr(chosen_current, "url", None) if chosen_current else ""

    log_selected_by = selected_by or ("ranked_sort" if flag_enabled else "")
    _log(logger, f"[Night FB][Rank Preview] query=\"{artist}\" top={preview_n} flag={int(flag_enabled)} selected_by=\"{log_selected_by}\"")
    for i, item in enumerate(ranked[:preview_n], start=1):
        feat = item["features"]
        breakdown = " ".join(item["breakdown"]) or "-"
        name = (feat.get("name") or "").strip() or feat.get("url") or ""
        cat = feat.get("category") or ""
        raw_cat = feat.get("category_raw") or cat
        match = feat.get("match_level") or "none"
        is_profile = feat.get("is_profile")
        is_page = feat.get("is_page")
        music_primary = feat.get("music_primary")
        music_hint = bool(feat.get("music_any"))
        score = item["score"]
        line = (
            f"{i}) name=\"{name}\" cat=\"{cat}\" cat_raw=\"{raw_cat}\" is_profile={bool(is_profile)} page_url={bool(is_page)} "
            f"music_hint={music_hint} music_primary={bool(music_primary)} name_match={match} score={score} ({breakdown})"
        )
        if debug:
            line += f" url={feat.get('url')}"
        _log(logger, f"[Night FB][Rank Preview] {line}")
        if raw_cat and (len(raw_cat) > 80 or raw_cat.lower().startswith("reminder") or raw_cat.lower().startswith("you have an event")):
            _log(logger, f"[Night FB][Rank Preview][warn] suspicious category text len={len(raw_cat)} cat={raw_cat!r}")
    if chosen_label:
        _log(logger, f'[Night FB][Rank Preview] chosen_current="{chosen_label}"')

def _is_junk_fb_candidate(url: str) -> bool:
    if not url:
        return True
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return True
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()

    if any(host == h or host.endswith(h) for h in _FB_JUNK_HOSTS):
        return True
    if host.startswith("business.facebook.com"):
        return True
    if path in ("", "/", "/home"):
        return True
    if any(path.startswith(tok) for tok in _FB_JUNK_PATH_TOKENS):
        return True
    if "l.php" in path:
        return True
    if "ref=notif" in query or "notif_id=" in query or "notif_t=" in query:
        return True
    return False


def _is_fb_login_or_security_url(url: str) -> bool:
    if not url:
        return False
    url_lower = (url or "").lower()
    if any(tok in url_lower for tok in ("/r.php", "/login", "/register")):
        return True
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if "facebook.com" not in host:
        return False
    bad_paths = ("/r.php", "/login", "/checkpoint", "/security", "/register")
    return any(path.startswith(p) for p in bad_paths)


def _category_is_music_like(category: Optional[str]) -> bool:
    """
    Lightweight allowlist for categories that clearly indicate a music page.
    """
    cat_clean = _sanitize_fb_category_text(category)
    if not cat_clean:
        return False
    cat = cat_clean.lower()
    return any(tok in cat for tok in _MUSIC_ROLE_TOKENS)


def _text_has_music_tokens(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(tok in (text or "").lower() for tok in _MUSIC_ROLE_TOKENS)


def _try_explicit_fb(driver, url: str, logger: LoggerFn = None) -> Tuple[List[str], Optional[str], str]:
    """
    Returns: (emails, reason, extract_method)
    reason is None if emails found, else one of:
      redirect_login, checkpoint_or_consent, not_found, no_email_found
    """
    if not driver or not url:
        return [], "no_email_found", ""

    try:
        page_source_raw, current_url, timed_out = _load_fb_page_with_timeout(
            driver, url, timeout_s=20.0, logger=logger
        )
    except Exception:
        try:
            _log(logger, f"[Night FB] Explicit FB navigation failed for {url}")
        except Exception:
            pass
        return [], "no_email_found", ""

    try:
        final_url = (current_url or getattr(driver, "current_url", "") or "").lower()
    except Exception:
        final_url = (url or "").lower()

    try:
        page_source_raw = page_source_raw or getattr(driver, "page_source", "") or ""
    except Exception:
        page_source_raw = page_source_raw or ""
    page_source = page_source_raw.lower()

    if timed_out and not page_source_raw:
        return [], "timeout", ""

    if ("login" in final_url) or ("/login.php" in final_url) or ("device-based" in page_source) or ("log in" in page_source and "facebook" in page_source):
        return [], "redirect_login", ""

    if any(tok in final_url for tok in ("checkpoint", "consent")) or any(tok in page_source for tok in ("checkpoint", "consent", "cookie", "privacy")):
        return [], "checkpoint_or_consent", ""

    not_found_phrases = (
        "page isn\u2019t available",
        "page isn't available",
        "content isn't available",
        "not available right now",
    )
    if any(phrase in page_source for phrase in not_found_phrases):
        return [], "not_found", ""

    rendered_text = _extract_fb_visible_text_with_container_fallback(driver)
    anchor_values = _collect_fb_live_anchor_targets(driver)
    _log_fb_email_surface_debug(logger, f"explicit:{current_url or url}", page_source_raw, rendered_text)
    emails, used_mailto = _extract_emails_from_html(
        page_source_raw,
        rendered_text=rendered_text,
        anchor_values=anchor_values,
    )
    if emails:
        method = "mailto" if used_mailto else "regex"
        return emails, None, method

    return [], "no_email_found", ""


def _fetch_fb_about_variants(base_url: str) -> List[str]:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        return []
    parsed = urllib.parse.urlparse(normalized)
    if parsed.path == "/profile.php":
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        fb_id = (qs.get("id") or [None])[0]
        if fb_id and str(fb_id).isdigit():
            return [
                f"https://www.facebook.com/profile.php?id={fb_id}&sk=about_contact_and_basic_info",
                f"https://www.facebook.com/profile.php?id={fb_id}&sk=about_details",
                f"https://www.facebook.com/profile.php?id={fb_id}&sk=about",
            ]
        return []
    variants = [
        f"{normalized}/directory_contact_info",
        f"{normalized}/about_contact_and_basic_info",
        f"{normalized}/about_details",
        f"{normalized}/about",
    ]
    return variants


def _pick_fb_contact_link(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """
    Choose a single valid Facebook contact/about surface from the main page.
    Prioritises the strongest contact/about surfaces within the single
    secondary-fetch budget.
    """
    if not soup:
        return None

    base = base_url or "https://www.facebook.com/"
    allowed_hosts = {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "web.facebook.com",
        "touch.facebook.com",
    }
    alias_hosts = {"facebook.com", "m.facebook.com", "web.facebook.com", "touch.facebook.com"}
    rejected_surface_tokens = {
        "events",
        "birthdays",
        "groups",
        "posts",
        "permalink",
        "photos",
        "watch",
        "videos",
    }
    allowed_sk = {"about", "about_contact_and_basic_info", "about_details"}

    def _canonicalize_resolved(candidate_url: str) -> Optional[Tuple[str, urllib.parse.ParseResult]]:
        resolved = urllib.parse.urljoin(base, candidate_url or "").split("#", 1)[0]
        try:
            parsed = urllib.parse.urlparse(resolved)
        except Exception:
            return None
        scheme = (parsed.scheme or "https").lower()
        if scheme not in {"http", "https"}:
            return None
        host = (parsed.netloc or "").lower()
        if host not in allowed_hosts:
            return None
        if host in alias_hosts:
            host = "www.facebook.com"
        normalized = urllib.parse.urlunparse(
            (scheme, host, parsed.path or "", "", parsed.query or "", "")
        )
        return normalized, urllib.parse.urlparse(normalized)

    try:
        base_parsed = urllib.parse.urlparse(base)
    except Exception:
        return None
    base_path = (base_parsed.path or "").rstrip("/").lower() or "/"

    candidates: List[Tuple[int, int, int, str]] = []
    seen: Set[str] = set()
    for index, anchor in enumerate(soup.find_all("a", href=True)):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue

        canonical = _canonicalize_resolved(href)
        if not canonical:
            continue
        resolved, parsed = canonical
        path = (parsed.path or "").rstrip("/").lower() or "/"
        path_segments = [segment for segment in path.split("/") if segment]
        if any(segment in rejected_surface_tokens for segment in path_segments):
            continue
        if resolved in seen:
            continue

        priority: Optional[int] = None
        if path.endswith("/about_contact_and_basic_info"):
            priority = 0
        elif path.endswith("/contact_and_basic_info"):
            priority = 1
        elif path.endswith("/about_details"):
            priority = 2
        elif path.endswith("/about"):
            priority = 3
        elif path.endswith("/contact"):
            priority = 4
        else:
            qs = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=False)
            sk_value = ((qs.get("sk") or [""])[0] or "").strip().lower()
            if sk_value in allowed_sk and (path == base_path or path == "/profile.php"):
                if sk_value == "about_contact_and_basic_info":
                    priority = 0
                elif sk_value == "about_details":
                    priority = 2
                else:
                    priority = 3

        if priority is None:
            continue

        text = " ".join(anchor.get_text(" ", strip=True).lower().split())
        text_rank = 1
        if priority == 0 and ("contact" in text or "about" in text):
            text_rank = 0
        elif priority in {1, 2} and ("contact" in text or "about" in text):
            text_rank = 0

        seen.add(resolved)
        candidates.append((priority, text_rank, index, resolved))

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][3]


def _night_fb_has_music_signals(soup: BeautifulSoup, meta: Optional[Dict[str, str]] = None) -> bool:
    if soup is None:
        return False

    def _log_music(signals: List[str], score: int) -> None:
        if os.getenv("FB_DEBUG_MUSIC_SIGNALS") != "1":
            return
        url = ""
        if meta:
            url = str(meta.get("url") or "")
        _log(None, f"[Night FB][music] url='{url}' signals={','.join(signals) or '-'} score={score}")

    def _link_looks_music(anchor) -> bool:
        text = (anchor.get_text(" ", strip=True) or "").lower()
        if any(tok in text for tok in ("music", "song", "single", "album", "ep", "dj", "producer", "official video", "official audio")):
            return True
        parent_text = (anchor.parent.get_text(" ", strip=True) or "").lower() if anchor and anchor.parent else ""
        return any(tok in parent_text for tok in ("music", "song", "single", "album", "ep", "dj", "producer"))

    aria_values = [(el.get("aria-label") or "") for el in soup.select("[aria-label]")]
    if facebook_enrich is not None:
        for aria_val in aria_values:
            try:
                if facebook_enrich.is_musician_page(aria_val, None):
                    return True
            except Exception:
                pass
    # Check aria-labels for DJ / Producer (fallback)
    aria_labels = " ".join(val.lower() for val in aria_values)
    if any(term in aria_labels for term in ["artist", "musician", "band", "singer", "producer", "dj"]):
        return True

    meta_bits: List[str] = []
    for selector in ('meta[name="description"]', 'meta[property="og:description"]', 'meta[property="og:title"]'):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            meta_bits.append(tag.get("content", ""))
    if meta:
        for key in ("category", "title", "description", "url"):
            val = meta.get(key)
            if val:
                meta_bits.append(str(val))
    if facebook_enrich is not None:
        for meta_text in meta_bits:
            try:
                if facebook_enrich.is_music_like_category(meta_text):
                    return True
            except Exception:
                pass
    meta_blob = " ".join(meta_bits).lower()
    if any(token in meta_blob for token in ("musician", "artist", "band", "music", "singer", "producer", "dj")):
        return True

    page_text = (soup.get_text(" ", strip=True) or "").lower()
    keyword_hit = any(token in page_text for token in ("musician", "artist", "band", "music", "singer", "producer", "dj"))
    if keyword_hit:
        return True

    score = 0
    signals: List[str] = []

    music_platform_patterns = (
        "spotify.com/artist",
        "spotify.com/track",
        "soundcloud.com",
        "bandcamp.com",
        "music.apple.com",
    )
    youtube_patterns = ("youtube.com/channel", "youtube.com/watch", "youtu.be/")

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").lower()
        if any(pat in href for pat in music_platform_patterns):
            score += 3
            signals.append("platform_link")
            break
        if any(pat in href for pat in youtube_patterns):
            if _link_looks_music(anchor):
                score += 3
                signals.append("platform_link")
                break

    aria_music_hits = [val for val in aria_values if "music" in (val or "").lower() or "song" in (val or "").lower()]
    text_music_sections = []
    if page_text:
        for phrase in ("top songs", "popular songs", "singles & eps", "singles and eps"):
            if phrase in page_text:
                text_music_sections.append(phrase)
    if aria_music_hits or text_music_sections:
        score += 2
        signals.append("fb_music_module")

    platform_keywords_present = "platform_link" in signals or any(tok in page_text for tok in ("spotify", "soundcloud", "bandcamp", "apple music", "youtube"))
    release_phrases = (
        "new single",
        "new ep",
        "new album",
        "debut single",
        "out now",
        "streaming now",
        "listen on spotify",
    )
    if page_text:
        title_slug = _slugify(str(meta.get("title") or "")) if meta else ""
        for phrase in release_phrases:
            if phrase in page_text:
                if platform_keywords_present or (title_slug and title_slug in page_text):
                    score += 1
                    signals.append("release_language")
                    break

    try:
        import json  # local import to avoid global dependency cost

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw_json = script.string or script.get_text() or ""
            if not raw_json:
                continue
            parsed = json.loads(raw_json)
            objs = parsed if isinstance(parsed, list) else [parsed]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                obj_type = obj.get("@type") or obj.get("type")
                if isinstance(obj_type, list):
                    type_vals = [str(t).lower() for t in obj_type]
                else:
                    type_vals = [str(obj_type).lower()] if obj_type else []
                if any(t in ("musicgroup", "musicrecording", "musicalbum") for t in type_vals):
                    score += 2
                    signals.append("ld_json_music")
                    raise StopIteration
    except StopIteration:
        pass
    except Exception:
        pass

    _log_music(signals, score)
    return score >= 2


def _is_garbage_fb_candidate(title: str, url: str, category: Optional[str]) -> bool:
    """
    Filter out business/notification-style FB search results that are not real pages.
    """
    title_l = (title or "").lower()
    url_l = (url or "").lower()
    category_clean = _sanitize_fb_category_text(category)
    category_l = (category_clean or "").lower()

    if "business.facebook.com" in url_l:
        return True
    if "/latest/composer" in url_l:
        return True
    if "/composer" in url_l:
        try:
            parsed = urllib.parse.urlparse(url_l)
            netloc_l = (parsed.netloc or "").lower()
            if netloc_l.startswith("business.") or "business." in netloc_l:
                return True
        except Exception:
            return True

    junk_title_tokens = (
        "unread",
        "mark as read",
        "followers are most active",
        "scheduling a post",
        "page's visibility",
        "page visibility",
        "notifications",
    )
    if any(tok in title_l for tok in junk_title_tokens):
        return True
    if "insights" in title_l and "business.facebook.com" in url_l:
        return True
    if "insights" in title_l and any(tok in title_l for tok in junk_title_tokens):
        return True

    if re.fullmatch(r"\d+[hdm]?", category_l or "") and any(tok in title_l for tok in ("unread", "followers", "notifications", "mark as read")):
        return True

    return False


def _parse_search_candidates(html: str, logger: LoggerFn = None, search_name: Optional[str] = None) -> List["facebook_enrich.FbCandidate"]:
    if facebook_enrich is None or not html:
        return []

    candidates = facebook_enrich._fb_extract_candidates_from_search_dom(
        html,
        logger=logger,
        debug=os.getenv("FB_DEBUG_DOM_GATE") == "1",
        search_name=search_name or "",
    )

    raw_candidates = list(candidates)

    # Add lightweight music hint for stable sorting (no scoring change).
    for cand in raw_candidates:
        name = getattr(cand, "name", "") or ""
        aria = getattr(cand, "aria_label", "") or ""
        raw_cat = getattr(cand, "category", "") or ""
        raw_cat_candidates = getattr(cand, "category_candidates", None) or getattr(cand, "_raw_category_candidates", []) or []
        secondary_text = getattr(cand, "secondary_text", "") or ""
        raw_descriptor = getattr(cand, "descriptor", "") or secondary_text

        sanitized_cat = _sanitize_fb_category_text(raw_cat)
        if raw_cat and sanitized_cat is None:
            # Phase 1: sanitize noisy FB category strings.
            _log(logger, f"[Night FB][CategorySanitize] dropped noisy category len={len(raw_cat)} raw={raw_cat!r}")
        cat = sanitized_cat or ""
        if cat and _category_looks_like_name(name, cat):
            cat = ""
        if cat and cat.lower() in ("profile", "timeline"):
            cat = ""

        descriptor = raw_descriptor or _pick_descriptor_from_candidates(name, raw_cat_candidates, aria, cat)
        descriptor_sanitized = _sanitize_fb_category_text(descriptor) or ""

        def _has_service(text: str) -> bool:
            return any(tok in (text or "").lower() for tok in _FB_SERVICE_CATEGORY_TOKENS)

        if descriptor_sanitized and _category_looks_like_name(name, descriptor_sanitized) and not _has_service(descriptor_sanitized):
            descriptor_sanitized = ""

        if cat and _category_looks_like_name(name, cat):
            if not _has_service(descriptor_sanitized):
                cat = ""

        category_tokens = list(raw_cat_candidates)

        try:
            setattr(cand, "_raw_category", raw_cat)
            setattr(cand, "_raw_category_candidates", raw_cat_candidates)
            setattr(cand, "category", cat)
            setattr(cand, "descriptor", descriptor_sanitized or descriptor or "")
            setattr(cand, "secondary_text", descriptor_sanitized or secondary_text or "")
            setattr(cand, "category_tokens", category_tokens)
        except Exception:
            pass
        flags = _candidate_category_flags(cat, aria, descriptor_sanitized or secondary_text or "", descriptor=descriptor_sanitized or descriptor, category_tokens=category_tokens)
        music_hint = flags["music_any"]
        try:
            setattr(cand, "music_hint", bool(music_hint))
        except Exception:
            pass

    deduped = _dedupe_candidates(raw_candidates)
    deduped = sorted(deduped, key=lambda c: 0 if getattr(c, "music_hint", False) else 1)

    if os.getenv("FB_DEBUG_CAND_META") == "1":
        try:
            debug_n = int(os.getenv("FB_DEBUG_CAND_META_N") or 5)
        except Exception:
            debug_n = 5
        for idx, c in enumerate(deduped[:debug_n], start=1):
            _log(
                logger,
                "[Night FB][cand_meta] %d) name=%r raw_cat=%r final_cat=%r descriptor=%r aria_label=%r secondary=%r tokens=%r music_hint=%s"
                % (
                    idx,
                    getattr(c, "name", ""),
                    getattr(c, "_raw_category", None),
                    getattr(c, "category", None),
                    getattr(c, "descriptor", None),
                    getattr(c, "aria_label", None),
                    getattr(c, "secondary_text", None),
                    (getattr(c, "category_tokens", None) or [])[:8],
                    bool(getattr(c, "music_hint", False)),
                ),
            )

    if os.getenv("FB_DEBUG_CANDIDATES") == "1":
        preview = deduped[:5]
        lines = [
            f"[Night FB][debug] cand name={c.name!r} url={c.url!r} category={getattr(c, 'category', '')!r} music_hint={bool(getattr(c, 'music_hint', False))}"
            for c in preview
        ]
        for line in lines:
            _log(logger, line)

    return deduped


def _harvest_candidates(
    html_val: Optional[str],
    driver_obj,
    search_label: str,
    *,
    logger: LoggerFn = None,
    v2_enabled: bool = False,
    session_unhealthy: Optional[bool] = None,
    session_reason: str = "",
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List["facebook_enrich.FbCandidate"]:
    """
    Unified search harvester that prefers V2 when enabled and a driver is present,
    with automatic fallback to the shared DOM parser.
    """
    if v2_enabled and driver_obj is not None:
        try:
            return _harvest_search_candidates_v2(
                driver_obj,
                logger=logger,
                search_name=search_label,
                session_unhealthy=session_unhealthy,
                session_reason=session_reason,
                diagnostics=diagnostics,
            )
        except Exception:
            # Defensive: V2 already logged; fall back to V1 parser.
            pass
    return _parse_search_candidates(html_val or "", logger=logger, search_name=search_label)


def _select_best_candidate_loose(artist_name: str, candidates: List["facebook_enrich.FbCandidate"]) -> Optional["facebook_enrich.FbCandidate"]:
    if facebook_enrich is None or not candidates:
        return candidates[0] if candidates else None
    selector = getattr(facebook_enrich, "select_best_facebook_candidate", None)
    if callable(selector):
        try:
            best = selector(candidates, artist_name, logger=None, suppress_console=True)
            if best:
                return best
        except Exception:
            pass
    try:
        best, *_ = facebook_enrich.select_best_fb_candidate(
            artist_name, candidates, suppress_console=True, logger=None
        )
        if best:
            return best
    except Exception:
        pass
    for cand in candidates:
        cat_clean = _sanitize_fb_category_text(getattr(cand, "category", "") or "")
        cat = (cat_clean or "").lower()
        if any(tok in cat for tok in ("artist", "musician", "musician/band", "band", "music", "dj", "producer")):
            return cand
    return candidates[0] if candidates else None


def _pick_best_candidate_music_bias(artist_name: str, candidates: List["facebook_enrich.FbCandidate"]) -> Optional["facebook_enrich.FbCandidate"]:
    if not candidates:
        return None
    best = None
    best_score = float("-inf")
    for cand in candidates:
        score = _night_candidate_score(artist_name, cand)
        if score > best_score:
            best_score = score
            best = cand
    return best


@dataclass
class NightModeFacebookResult:
    email: Optional[str] = None
    email_all: str = ""
    email_type: str = "fb_night"
    facebook_url: str = ""
    email_source: str = ""
    email_source_url: str = ""
    email_extract_method: str = ""
    about_attempted: str = "no"
    about_result: str = ""
    accepted: bool = True
    reject_reason: str = ""
    candidate_url: str = ""
    match_level: str = ""
    selected_by: str = ""
    name_consistency_flag: Optional[int] = None
    review_reason: str = ""
    source_context: Optional[Dict] = None


class NightModeFacebookEnricher:
    """
    Night-Mode-only Facebook enricher that:
      - Prefers direct URLs when present.
      - Uses a looser music gate for search candidates.
      - Extracts emails via regex + mailto scanning.
    """

    def __init__(
        self,
        legacy_module,
        username: str,
        password: str,
        logger: LoggerFn = None,
        use_shared_session: bool = True,
        run_state: Optional[NightFBRunState] = None,
    ) -> None:
        self.legacy = legacy_module
        self.username = username
        self.password = password
        self.logger = self._coerce_logger(logger)
        self._run_state = run_state
        self._shared_run_state = run_state is not None
        self._session_source = run_state.session_source if run_state is not None else normalize_night_fb_session_source(username, password)
        self.session = None
        self._owns_session = False
        self.use_shared_session = use_shared_session
        self._anon_driver = None
        self._unearthed_driver = None
        self._headless_env_raw = os.environ.get("NIGHT_FB_HEADLESS")
        self.headless = _bool_env("NIGHT_FB_HEADLESS", default=False)
        self.allow_headed_recovery = _bool_env("NIGHT_FB_HEADED_RECOVERY", default=False)
        self.skip_on_checkpoint = _bool_env("NIGHT_FB_SKIP_ON_CHECKPOINT", default=True)
        self.require_display = _bool_env("NIGHT_FB_REQUIRE_DISPLAY", default=False)
        self._headed_recovery_attempted = False
        self._skip_fb_due_to_checkpoint = False
        self._checkpoint_guard_enabled = _bool_env("NIGHT_FB_CHECKPOINT_GUARD", default=False)
        self._search_disabled_due_to_checkpoint = False
        self._checkpoint_warned_this_row = False
        self._checkpoint_limited_active = False
        self._skip_fb_due_to_display = False
        self._skip_fb_due_to_warning = False
        self._skip_fb_due_to_warning_reason = ""
        self._session_failed = False
        self._session_failed_reason = ""
        self._skip_fb_due_to_session_failure = False
        self._skip_fb_due_to_session_failure_reason = ""
        self._last_selected_candidate_context: Optional[Dict[str, Any]] = None
        self._last_search_candidates: List[Dict[str, Any]] = []
        self._last_search_reject_reason: str = ""
        self._last_search_reject_score: Optional[int] = None
        self._last_explicit_guard_reason: str = ""
        self._pass_a_counts = {
            "attempted": 0,
            "found_email": 0,
            "no_email_on_page": 0,
            "login_wall": 0,
            "fetch_error": 0,
            "skipped_no_fb_url": 0,
        }
        self.fb_email_pages_visited: int = 0
        self.fb_emails_found: int = 0
        self.fb_rows_skipped: Dict[str, int] = {
            "challenge": 0,
            "checkpoint": 0,
            "cooldown": 0,
            "no_opportunity": 0,
        }
        # Per-row budget: main page + at most one contact/about page.
        self._page_budget_remaining: int = 2
        # Per-run reject cache to avoid re-selecting clearly bad pages (name mismatch, non-music, etc.).
        self._fb_reject_cache: Dict[str, Set[str]] = {}
        self._fb_reject_cache_global: Set[str] = set()
        # Track FB URLs already accepted for a given artist to avoid cross-artist bleed.
        self._fb_url_owner: Dict[str, str] = {}
        self._fb_owner_skip_count: int = 0
        self._session_state_logged = False
        # Slow mode / resilience
        self.slow_mode_active: bool = False
        self.slow_mode_multiplier: float = 1.0
        self.slow_mode_reason: str = ""
        self.session_unhealthy_count: int = 0
        self.checkpoint_events: int = 0
        self.login_wall_events: int = 0
        # Navigation timeout tracking
        self._last_fb_timeout: bool = False
        self._last_fb_timeout_url: str = ""
        self._last_fb_visible_text: str = ""
        self._last_fb_live_anchor_values: List[str] = []
        self._last_fb_reveal_actions: List[str] = []
        self._last_fb_surface_html: Optional[str] = None
        self._last_fb_surface_url: str = ""
        self._last_fb_surface_driver_kind: str = ""
        self._last_fb_render_invalid_reason: str = ""
        self._last_fb_surface_html_available: bool = False
        self._last_fb_visible_text_available: bool = False
        self._last_fb_anchor_values_available: bool = False
        self._last_fb_reveal_actions_available: bool = False
        self.protective_shutdown: bool = False
        # Debug-only FB URL flow tracing; defaults to off.
        self._debug_fb_url_flow: bool = _bool_env("DEBUG_FB_URL_FLOW", default=False)
        self._debug_fb_url_flow_limit: int = int(os.getenv("DEBUG_FB_URL_FLOW_N") or 25)
        self._debug_fb_url_flow_seen: int = 0
        self._debug_fb_url_flow_with_urls: int = 0
        self._debug_fb_url_flow_skipped: int = 0
        self._debug_fb_url_flow_summary_logged: bool = False
        if self._debug_fb_url_flow:
            try:
                atexit.register(self._emit_fb_url_flow_summary)
            except Exception:
                pass
        self._sync_search_disable_from_run_state()

        if (not self.headless) and self.require_display and _is_linux() and (not _display_env_present()):
            self._skip_fb_due_to_display = True
            _log(
                self.logger,
                "[Night FB] Skipping FB: headed mode requires display but DISPLAY/WAYLAND missing; set NIGHT_FB_HEADLESS=1 or disable NIGHT_FB_REQUIRE_DISPLAY.",
            )

        mode_label = "headless" if self.headless else "headed"
        profile_dir = _resolve_night_fb_profile_dir(self.logger)
        headless_env_label = self._headless_env_raw if self._headless_env_raw is not None else "<unset>"
        _log(
            self.logger,
            f"[Night FB] Starting session mode={mode_label} headless_env={headless_env_label} require_display={self.require_display} os={sys.platform}",
        )
        if _bool_env("NIGHT_FB_DEBUG_MODE", default=False):
            _log(
                self.logger,
                f"[Night FB][debug] resolved_headless={self.headless} headed_recovery={self.allow_headed_recovery} skip_on_checkpoint={self.skip_on_checkpoint} require_display={self.require_display}",
            )

    def __enter__(self) -> "NightModeFacebookEnricher":
        try:
            if self._skip_fb_due_to_display:
                return self
            self._ensure_session()
        except FacebookDriverError as exc:
            self._session_failed = True
            self._session_failed_reason = str(exc)
            self._skip_fb_due_to_session_failure = True
            self._skip_fb_due_to_session_failure_reason = str(exc)
            _log(self.logger, f"[Night FB] Failed to start FB session: {exc}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _coerce_logger(logger: LoggerFn) -> LoggerFn:
        try:
            if logger and callable(logger):
                return logger
            info_fn = getattr(logger, "info", None) if logger else None
            if callable(info_fn):
                return info_fn
        except Exception:
            pass
        return logger

    def get_session_failure(self) -> Tuple[bool, str]:
        """Return (failed, reason) for outer orchestrators to decide whether to skip FB entirely."""
        return bool(self._skip_fb_due_to_session_failure or self._session_failed), self._session_failed_reason or self._skip_fb_due_to_session_failure_reason or ""

    def _session_state_snapshot(self, session) -> Tuple[bool, bool, str]:
        authed = False
        unhealthy = False
        reason = ""
        if session is None:
            return authed, unhealthy, reason
        driver = getattr(session, "driver", None)
        try:
            authed = _is_driver_authenticated(driver) if driver else False
        except Exception:
            authed = False
        try:
            unhealthy = not bool(getattr(session, "last_health_ok", True))
        except Exception:
            unhealthy = False
        try:
            reason = str(getattr(session, "last_health_reason", "") or "")
        except Exception:
            reason = ""
        if unhealthy:
            self.session_unhealthy_count += 1
            # Enter slow mode gently on first unhealthy detection.
            self._enter_slow_mode(reason or "session_unhealthy", max(self.slow_mode_multiplier, 1.5))
        return authed, unhealthy, reason

    def _sync_search_disable_from_run_state(self) -> None:
        if self._run_state is None:
            return
        if self._run_state.search_disabled_for_run:
            self._search_disabled_due_to_checkpoint = True

    def _current_trust_score(self) -> int:
        if self._run_state is None:
            return 0
        try:
            return int(getattr(self._run_state, "trust_score", 0) or 0)
        except Exception:
            return 0

    def _set_trust_score(self, value: int) -> int:
        if self._run_state is None:
            return 0
        try:
            self._run_state.trust_score = int(value)
        except Exception:
            self._run_state.trust_score = 0
        return int(self._run_state.trust_score or 0)

    def _apply_trust_budget_health(
        self,
        health: Dict[str, Any],
        *,
        context: str,
    ) -> None:
        run_state = self._run_state
        if run_state is None:
            return

        search_miss_reason = str(health.get("search_miss_reason") or "").strip()
        auth_surface = str(health.get("auth_surface") or "").strip()
        warning_reason = str(health.get("warning_reason") or "").strip()
        delta = 0
        event_reason = ""

        if health.get("captcha") or auth_surface == "captcha":
            delta = -3
            event_reason = "captcha"
        elif health.get("checkpoint") or auth_surface == "checkpoint":
            delta = -3
            event_reason = "checkpoint"
        elif health.get("login_wall"):
            delta = -2
            event_reason = auth_surface or "login_wall"
        elif warning_reason:
            delta = -2
            event_reason = warning_reason
        elif search_miss_reason in {
            "blank_html",
            "dom_container_missing",
            "overlay_zero_anchors",
            "timeout_no_results",
            "zero_anchors",
            "zero_usable_hrefs",
        }:
            delta = -1
            event_reason = search_miss_reason
        elif self._current_trust_score() < 0:
            delta = 1
            event_reason = "healthy_page"

        if delta == 0:
            return

        old_score = self._current_trust_score()
        new_score = self._set_trust_score(old_score + delta)
        action = ""

        if new_score <= -5 and not run_state.disabled_for_run:
            action = "disable_fb"
            self._skip_fb_due_to_checkpoint = True
            self._search_disabled_due_to_checkpoint = True
            self.protective_shutdown = True
            disable_night_fb_search_run_state(run_state, event_reason or "session_unhealthy")
            disable_night_fb_run_state(
                run_state,
                event_reason or "session_unhealthy",
                checkpointed=(event_reason == "checkpoint"),
                session_unhealthy=True,
            )
        elif new_score <= -3 and not run_state.search_disabled_for_run:
            action = "disable_search"
            disable_night_fb_search_run_state(run_state, event_reason or "session_unhealthy")
            self._search_disabled_due_to_checkpoint = True

        self._sync_search_disable_from_run_state()
        _log(
            self.logger,
            f"[Night FB][Trust Budget] score={new_score} delta={delta} reason={event_reason or 'unknown'} context={context}"
            + (f" action={action}" if action else ""),
        )

    def _log_page_health(
        self,
        current_url: str,
        page_html: Optional[str],
        *,
        context: str,
        search_miss_reason: str = "",
        warning_reason: str = "",
    ) -> Dict[str, Any]:
        health = _night_fb_page_health_snapshot(
            current_url,
            page_html,
            search_miss_reason=search_miss_reason,
            warning_reason=warning_reason,
        )
        parts = [
            "[Night FB][Health]",
            f"url={health.get('url') or '<unknown>'}",
            f"captcha={1 if health.get('captcha') else 0}",
            f"checkpoint={1 if health.get('checkpoint') else 0}",
            f"login_wall={1 if health.get('login_wall') else 0}",
        ]
        if health.get("warning_reason"):
            parts.append(f"warning={health.get('warning_reason')}")
        if health.get("search_miss_reason"):
            parts.append(f"search_miss={health.get('search_miss_reason')}")
        parts.append(f"context={context}")
        _log(self.logger, " ".join(parts))
        self._apply_trust_budget_health(health, context=context)
        return health

    def _log_session_state_once(self, session) -> None:
        if self._session_state_logged:
            return
        if session is None:
            return
        authed, unhealthy, reason = self._session_state_snapshot(session)
        decision = _build_night_fb_session_decision(
            authenticated=authed,
            reason=reason if unhealthy else "",
        )
        v2_enabled = _bool_env("FB_SEARCH_HARVEST_V2", default=False)
        _log(
            self.logger,
            f"[Night FB][session_state] state={decision.state} authed={1 if authed else 0} unhealthy={1 if unhealthy else 0} reason={decision.reason or ''} v2={1 if v2_enabled else 0}",
        )
        self._session_state_logged = True

    def _fb_reject_key(self, artist: str) -> str:
        return _slugify(artist)

    def _fb_mark_rejected(self, artist: str, url: str, reason: str = "") -> None:
        norm_url = _normalise_fb_url(url or "")
        if not norm_url:
            return
        key = self._fb_reject_key(artist)
        cache = self._fb_reject_cache.setdefault(key, set())
        cache.add(norm_url)
        self._fb_reject_cache_global.add(norm_url)
        if reason:
            _log(self.logger, f"[Night FB] Cached reject url='{norm_url}' artist='{artist}' reason='{reason}'")

    def _fb_is_rejected(self, artist: str, url: str) -> bool:
        norm_url = _normalise_fb_url(url or "")
        if not norm_url:
            return False
        key = self._fb_reject_key(artist)
        return norm_url in self._fb_reject_cache.get(key, set()) or norm_url in self._fb_reject_cache_global

    def _choose_ranked_candidate(
        self, artist: str, ranked_items: List[Dict[str, Any]], min_accept_score: Optional[int] = None
    ) -> Tuple[Optional["facebook_enrich.FbCandidate"], str]:
        """
        Pick the first viable ranked candidate, skipping any cached rejects and
        preferring name-matching pages over clear mismatches.
        """
        fallback_mismatch = None
        artist_norm = _normalize_name_like(artist)
        min_accept_score = _min_fb_accept_score() if min_accept_score is None else int(min_accept_score)
        # Reset per-call reject context so stale reasons do not leak.
        self._last_search_reject_reason = ""
        self._last_search_reject_score = None
        best_rejected_score: Optional[int] = None
        best_rejected_reason: str = ""
        for item in ranked_items or []:
            cand = item.get("candidate")
            raw_url = _candidate_url(cand)
            norm_url = _normalise_fb_url(raw_url)
            if norm_url and self._fb_is_rejected(artist, norm_url):
                _log(self.logger, f"[Night FB] Skipping cached rejected FB candidate url='{norm_url}' for '{artist}'.")
                continue
            if norm_url:
                owner = self._fb_url_owner.get(norm_url)
                if owner and owner != artist_norm:
                    _log(
                        self.logger,
                        f"[Night FB] Skipping FB candidate url='{norm_url}' for '{artist}' (already used by '{owner}').",
                    )
                    self._fb_owner_skip_count += 1
                    continue
            match_level = item.get("features", {}).get("match_level") or "none"
            is_safe, reject_reason = _candidate_is_safe_enough(item, min_accept_score)
            if not is_safe:
                try:
                    score_val = int(item.get("score") or 0)
                except Exception:
                    score_val = None
                if score_val is not None:
                    if best_rejected_score is None or score_val > best_rejected_score:
                        best_rejected_score = score_val
                        best_rejected_reason = reject_reason or "no_safe_match"
                elif best_rejected_score is None:
                    best_rejected_reason = reject_reason or "no_safe_match"
                continue
            if match_level != "mismatch":
                return cand, "ranked_sort"
            if fallback_mismatch is None:
                fallback_mismatch = cand
        if fallback_mismatch:
            return fallback_mismatch, "mismatch_fallback"
        if best_rejected_score is not None:
            self._last_search_reject_reason = best_rejected_reason or "no_safe_match"
            self._last_search_reject_score = best_rejected_score
            _log(
                self.logger,
                f"[Night FB][NoSafeMatch] '{artist}' no safe FB candidate (best_score={best_rejected_score}) reason={best_rejected_reason}",
            )
            return None, "no_safe_match"
        return None, "no_viable_candidate"

    def _select_candidate_url(
        self,
        artist: str,
        primary_candidate,
        candidates: List["facebook_enrich.FbCandidate"],
        ranked_candidates: List["facebook_enrich.FbCandidate"],
        ranked_for_preview: List[Dict[str, Any]],
        selected_by: str,
        min_accept_score: int,
    ) -> Optional[str]:
        """
        Finalize candidate selection with safety/allowlist gates before navigation.
        Returns the normalized URL of the first safe, allowed candidate.
        """
        ranking_enabled = True
        ordered_candidates = _order_candidates_for_selection(primary_candidate, candidates, ranked_candidates, ranking_enabled)

        gate_debug = os.getenv("FB_DEBUG_CANDIDATES") == "1"
        url_flow_debug = gate_debug or os.getenv("FB_DEBUG_CAND_URL_FLOW") == "1"
        debug_detail = url_flow_debug

        ranked_lookup = {id(item["candidate"]): item for item in (ranked_for_preview or [])}

        collected_contexts: List[Tuple[Dict[str, Any], Any]] = []
        chosen_url_norm = _normalise_fb_url(_candidate_url(primary_candidate)) if primary_candidate else ""
        selected_ctx: Optional[Dict[str, Any]] = None

        for cand in ordered_candidates:
            ranked_item = ranked_lookup.get(id(cand))
            if ranked_item is None:
                try:
                    score_val, _, computed_features = _score_fb_candidate_night(artist, cand)
                except Exception:
                    score_val, computed_features = 0, {}
                ranked_item = {"candidate": cand, "score": score_val, "features": computed_features}

            is_safe, reject_reason = _candidate_is_safe_enough(ranked_item, min_accept_score)
            if not is_safe:
                if debug_detail:
                    _log(self.logger, f"[Night FB] Skipping unsafe FB candidate url={_candidate_url(cand)!r} reason={reject_reason}")
                try:
                    score_val = int(ranked_item.get("score") or 0)
                except Exception:
                    score_val = None
                if score_val is not None:
                    if self._last_search_reject_score is None or score_val > self._last_search_reject_score:
                        self._last_search_reject_score = score_val
                        if reject_reason:
                            self._last_search_reject_reason = reject_reason
                        elif not self._last_search_reject_reason:
                            self._last_search_reject_reason = "no_safe_match"
                elif reject_reason and (not self._last_search_reject_reason):
                    self._last_search_reject_reason = reject_reason
                continue

            raw_url = _candidate_url(cand)
            norm_url = _normalise_fb_url(raw_url or "")

            if norm_url and self._fb_is_rejected(artist, norm_url):
                if debug_detail:
                    _log(self.logger, f"[Night FB] Skipping cached rejected FB candidate url={norm_url!r} for '{artist}'.")
                continue

            is_junk = bool(norm_url) and _is_junk_fb_candidate(norm_url)

            allowlisted_probe = None
            if url_flow_debug:
                try:
                    allowlisted_probe = _fb_is_candidate_url_allowed(norm_url) if norm_url else False
                except Exception as exc:
                    allowlisted_probe = f"error:{exc}"
                _log(
                    self.logger,
                    f"[Night FB][debug] cand_url_flow raw={raw_url!r} norm={norm_url!r} junk={is_junk} allowlisted={allowlisted_probe}"
                )

            if not raw_url:
                debug_payload = f" cand={cand!r}" if debug_detail else ""
                _log(self.logger, f"[Night FB] Candidate URL missing for '{artist}', skipping.{debug_payload}")
                continue

            if not norm_url:
                debug_payload = f" raw_url={raw_url!r} cand={cand!r}" if debug_detail else ""
                _log(self.logger, f"[Night FB] Candidate URL normalize_failed for '{artist}', skipping.{debug_payload}")
                continue

            if is_junk:
                reason = fb_reason_code_split(norm_url, "business_notif")
                _log(self.logger, f"[Night FB] Dropped junk FB candidate url={norm_url!r} reason={reason} (post-select)")
                if debug_detail:
                    _log(self.logger, f"[Night FB] Candidate URL junk for '{artist}', skipping. reason={reason} raw_url={raw_url!r} cand={cand!r}")
                continue

            try:
                allowlisted_result = allowlisted_probe if isinstance(allowlisted_probe, bool) else _fb_is_candidate_url_allowed(norm_url)
                if not allowlisted_result:
                    msg = f"[Night FB] Rejected FB candidate url={norm_url!r} due to allowlist."
                    if debug_detail:
                        msg += f" raw_url={raw_url!r} cand={cand!r}"
                    _log(self.logger, msg)
                    continue
            except Exception as exc:
                if debug_detail:
                    _log(self.logger, f"[Night FB] Allowlist check errored for url={norm_url!r}, skipping. error={exc} raw_url={raw_url!r} cand={cand!r}")
                continue

            name = getattr(cand, "name", None)
            raw_category = getattr(cand, "_raw_category", None) or getattr(cand, "category", None)
            if isinstance(cand, dict):
                name = name or cand.get("name")
                raw_category = raw_category or cand.get("category")
            category = _sanitize_fb_category_text(raw_category) or ""

            base_score = 0.0
            try:
                if facebook_enrich is not None and callable(getattr(facebook_enrich, "score_fb_candidate", None)):
                    scored = facebook_enrich.score_fb_candidate(artist, name, norm_url, category)
                    if scored:
                        _, base_score, _ = scored
            except Exception:
                base_score = 0.0
            # Derive lightweight identity consistency signals for downstream review flagging.
            try:
                match_level_ctx = _candidate_name_match(artist, name or "")
            except Exception:
                match_level_ctx = ""
            context = {
                "url": norm_url,
                "name": name or "",
                "category": category or "",
                "category_raw": raw_category or "",
                "base_score": base_score,
                "match_level": match_level_ctx,
                "selected_by": selected_by,
                "search_discovery_accepted": True,
            }
            if chosen_url_norm and norm_url == chosen_url_norm and selected_ctx is None:
                selected_ctx = context
            collected_contexts.append((context, cand))

        if collected_contexts:
            first_ctx, first_cand = collected_contexts[0]
            selected_ctx = selected_ctx or first_ctx
            selected_ctx["selected_by"] = selected_by
            self._last_selected_candidate_context = selected_ctx
            self._last_search_candidates = [ctx for ctx, _ in collected_contexts]
            _maybe_log_rank_preview(artist, candidates, first_cand, logger=self.logger, selected_by=selected_by)
            raw_suffix = ""
            if first_ctx.get("category_raw") and first_ctx.get("category") != first_ctx.get("category_raw"):
                raw_suffix = f" raw_cat={first_ctx.get('category_raw')!r}"
            _log(
                self.logger,
                f"[Night FB] Selected FB candidate '{first_ctx.get('name') or first_ctx.get('url')}' -> {first_ctx.get('url')} (category='{first_ctx.get('category') or ''}'){raw_suffix} selected_by={selected_by}",
            )
            return first_ctx.get("url")

        _log(self.logger, f"[Night FB] No usable FB candidates for '{artist}' after URL validation.")
        _maybe_log_rank_preview(artist, candidates, None, logger=self.logger, selected_by=selected_by)
        return None

    def _enter_slow_mode(self, reason: str, multiplier: float) -> None:
        try:
            multiplier = float(multiplier)
        except Exception:
            multiplier = self.slow_mode_multiplier or 1.0
        if multiplier <= (self.slow_mode_multiplier or 1.0) + 1e-9:
            return
        self.slow_mode_multiplier = multiplier
        self.slow_mode_active = True
        self.slow_mode_reason = reason or self.slow_mode_reason
        _log(self.logger, f"[Night FB] Entering slow mode (reason={reason}) multiplier={multiplier:.1f}.")

    def _register_checkpoint_event(self) -> None:
        self.checkpoint_events += 1
        self._enter_slow_mode("checkpoint", max(self.slow_mode_multiplier, 1.5))
        if not self.protective_shutdown and self.checkpoint_events >= 2:
            self.protective_shutdown = True
            self._skip_fb_due_to_checkpoint = True
            self._search_disabled_due_to_checkpoint = True
            _log(self.logger, "[Night FB] Entering protective shutdown mode (checkpoint persistence detected).")

    def _register_login_wall(self) -> None:
        self.login_wall_events += 1
        if not self.protective_shutdown:
            self.protective_shutdown = True
            self._skip_fb_due_to_checkpoint = True
            self._search_disabled_due_to_checkpoint = True
            disable_night_fb_run_state(
                self._run_state,
                "login_wall",
                session_unhealthy=True,
            )
            _log(self.logger, "[Night FB] Protective shutdown triggered by login wall; skipping FB for remainder of run.")

    def _ensure_session(self, prewarm_session: bool = True):
        self._sync_search_disable_from_run_state()
        if self._session_failed:
            raise FacebookDriverError(self._session_failed_reason or "Facebook session previously failed.")
        if self.use_shared_session:
            raise FacebookDriverError("Legacy/shared Facebook session is not allowed for Night Mode.")
        if self._run_state is not None:
            if self._run_state.disabled_for_run or self._run_state.session_invalid:
                raise FacebookDriverError(self._run_state.disable_reason or "Facebook disabled for this Night run.")
            if not self._session_source.can_probe:
                _log(
                    self.logger,
                    f"[Night FB] No usable Night FB session source; running without live session (reason={self._session_source.reason}).",
                )
                return None
            session = ensure_night_fb_run_session(
                self._run_state,
                headless=self.headless,
                logger=self.logger,
                owner="night_mode_fb",
                prewarm_session=prewarm_session,
            )
            self.session = session
            self._owns_session = False
            self._session_state_logged = False
            if session is not None:
                if prewarm_session:
                    self._log_session_state_once(session)
                return session
            if self._run_state.disabled_for_run or self._run_state.session_invalid:
                raise FacebookDriverError(self._run_state.disable_reason or "Facebook disabled for this Night run.")
            return None

        if isinstance(self.session, NightPersistentFacebookSession):
            driver = getattr(self.session, "driver", None)
            try:
                if driver:
                    _ = driver.current_url
                    if _is_driver_authenticated(driver):
                        try:
                            self.session.last_health_ok = True  # type: ignore[attr-defined]
                            self.session.last_health_reason = ""  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        _log(self.logger, "[Night FB] Reusing existing authenticated driver (persistent profile).")
                        self._log_session_state_once(self.session)
                        return self.session
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
            self._session_state_logged = False
        elif self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
            self._session_state_logged = False

        if not self._session_source.can_probe:
            _log(
                self.logger,
                f"[Night FB] No usable Night FB session source; running without live session (reason={self._session_source.reason}).",
            )
            return None
        try:
            driver_factory = lambda: _create_fb_driver_night_mode(self.headless, logger=self.logger)
            self.session = NightPersistentFacebookSession(driver_factory, headless=self.headless, logger=self.logger)
            self._owns_session = True
            if prewarm_session:
                self.session.ensure_logged_in()
                self._log_session_state_once(self.session)
            return self.session
        except FacebookDriverError as exc:
            self._session_failed = True
            self._session_failed_reason = str(exc)
            self._skip_fb_due_to_session_failure = True
            self._skip_fb_due_to_session_failure_reason = str(exc)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._session_failed = True
            self._session_failed_reason = str(exc)
            self._skip_fb_due_to_session_failure = True
            self._skip_fb_due_to_session_failure_reason = str(exc)
            _log(self.logger, f"[Night FB] Failed to start Facebook session: {exc}")
            self.session = None

    def close(self) -> None:
        if self.session and self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        try:
            if self._anon_driver:
                self._anon_driver.quit()
        except Exception:
            pass
        self._anon_driver = None
        try:
            if self._unearthed_driver:
                self._unearthed_driver.quit()
        except Exception:
            pass
        self._unearthed_driver = None
        if self._fb_owner_skip_count > 0:
            _log(self.logger, f"[Night FB] Owner guard skipped {self._fb_owner_skip_count} candidate(s) due to prior artist usage.")
        self._session_state_logged = False

    def _get_anon_driver(self):
        if self._anon_driver:
            return self._anon_driver
        driver_factory = getattr(self.legacy, "setup_facebook_driver", None)
        if not callable(driver_factory):
            raise FacebookDriverError("Anonymous FB driver not available.")
        self._anon_driver = driver_factory()
        return self._anon_driver

    def _get_unearthed_driver(self):
        if self._unearthed_driver:
            return self._unearthed_driver
        self._unearthed_driver = _create_fb_driver_public(headless=True)
        return self._unearthed_driver

    def _pass_a_bump(self, key: str) -> None:
        if key in self._pass_a_counts:
            self._pass_a_counts[key] += 1

    def _pass_a_log_row(
        self,
        artist: str,
        url: str,
        driver_kind: str,
        outcome: str,
        reason: str,
        mode: str = "legacy_anon_probe",
    ) -> None:
        safe_artist = artist or "<unknown>"
        safe_url = url or "<none>"
        safe_driver = driver_kind or "unknown"
        safe_outcome = outcome or "unknown"
        safe_reason = reason or ""
        _log(
            self.logger,
            f'[Night FB][PASS A] artist="{safe_artist}" url="{safe_url}" mode="{mode}" driver="{safe_driver}" outcome="{safe_outcome}" reason="{safe_reason}"',
        )

    def _has_authenticated_session(self) -> bool:
        """
        Lightweight check to see if an authenticated Night FB session is available.
        Returns False on any error to keep legacy fallbacks intact.
        """
        try:
            session = self._ensure_session()
            if not session:
                return False
            self._ensure_driver_alive(session)
            return True
        except Exception:
            return False

    def _resolve_pass_a_explicit_scrape_url(self, direct_url: str, *, authed_session_available: bool) -> str:
        """
        Resolve explicit PASS A share-style entrypoints once via the existing
        authenticated Night FB session and return a canonical scrape URL.
        """
        target_url = str(direct_url or "").strip()
        if not target_url:
            return target_url
        if not _is_allowed_fb_share_entrypoint_url(target_url):
            return target_url
        if not authed_session_available:
            return target_url

        try:
            session = self._ensure_session()
        except Exception as exc:
            _log(self.logger, f"[Night FB][PASS A] share wrapper resolution skipped for '{target_url}': {exc}")
            return target_url
        if not session:
            return target_url

        try:
            driver = session.navigate(target_url, logger=self.logger)
            resolved_url = (
                str(getattr(session, "last_nav_current_url", "") or "").strip()
                or _safe_current_url(driver)
                or target_url
            )
        except Exception as exc:
            _log(self.logger, f"[Night FB][PASS A] share wrapper resolution failed for '{target_url}': {exc}")
            return target_url

        canonical_resolved = _normalise_fb_url(resolved_url)
        if not canonical_resolved or _is_allowed_fb_share_entrypoint_url(canonical_resolved):
            return target_url

        if canonical_resolved != target_url:
            _log(
                self.logger,
                f"[Night FB][PASS A] resolved explicit wrapper '{target_url}' -> '{canonical_resolved}'",
            )
        return canonical_resolved

    def get_pass_a_counts(self) -> Dict[str, int]:
        return dict(self._pass_a_counts)

    def get_email_stats(self) -> Dict[str, int]:
        return {
            "fb_email_pages_visited": int(self.fb_email_pages_visited),
            "fb_emails_found": int(self.fb_emails_found),
            "fb_rows_skipped_reason_challenge": int(self.fb_rows_skipped.get("challenge", 0)),
            "fb_rows_skipped_reason_checkpoint": int(self.fb_rows_skipped.get("checkpoint", 0)),
            "fb_rows_skipped_reason_cooldown": int(self.fb_rows_skipped.get("cooldown", 0)),
            "fb_rows_skipped_reason_no_opportunity": int(self.fb_rows_skipped.get("no_opportunity", 0)),
        }

    def get_slow_mode_multiplier(self) -> float:
        try:
            return float(self.slow_mode_multiplier or 1.0)
        except Exception:
            return 1.0

    def _refresh_driver(self, session) -> None:
        _log(self.logger, "[FB] Driver appears dead, recreating a fresh instance...")
        refresh = getattr(session, "refresh_session", None)
        if callable(refresh):
            try:
                refresh()
                driver = session.ensure_logged_in() if hasattr(session, "ensure_logged_in") else session.navigate("about:blank")
                try:
                    _ = driver.current_url
                except Exception as exc:
                    raise FacebookDriverError(f"Refreshed driver is still unavailable: {exc}")
            except Exception as exc:
                self._session_failed = True
                self._session_failed_reason = str(exc)
                raise FacebookDriverError(f"Failed to refresh FB session: {exc}")
        else:
            raise FacebookDriverError("No refresh_session available to recreate driver.")

    def _ensure_driver_alive(self, session):
        if session is None:
            raise FacebookDriverError("Facebook session not initialized.")
        if isinstance(session, NightPersistentFacebookSession):
            try:
                driver = session.ensure_logged_in()
                _ = driver.current_url
                if not _is_driver_authenticated(driver):
                    raise FacebookDriverError("Facebook session unauthenticated (missing c_user cookie).")
                return session
            except Exception as exc:
                raise FacebookDriverError(f"Failed to ensure Facebook login: {exc}")
        try:
            driver = session.ensure_logged_in() if hasattr(session, "ensure_logged_in") else session.navigate("about:blank")
        except Exception as exc:
            raise FacebookDriverError(f"Failed to ensure Facebook login: {exc}")
        try:
            _ = driver.current_url
            if not _is_driver_authenticated(driver):
                raise FacebookDriverError("Facebook session unauthenticated (missing c_user cookie).")
        except Exception:
            self._refresh_driver(session)
        return session

    def _clear_last_fb_email_surface_state(self) -> None:
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

    def _collect_current_fb_email_surface_state(self, driver_kind: str = "session") -> Tuple[str, str, List[str], List[str]]:
        self._clear_last_fb_email_surface_state()
        driver = None
        if str(driver_kind or "").startswith("anon"):
            driver = getattr(self, "_anon_driver", None)
        else:
            session = getattr(self, "session", None)
            driver = getattr(session, "driver", None)
        if driver is None:
            return "", "", [], []
        page_source, rendered_text, anchor_values, reveal_actions = _collect_fb_email_surface_state(
            driver,
            logger=self.logger,
        )
        self._last_fb_surface_html = page_source
        self._last_fb_surface_url = str(getattr(driver, "current_url", "") or "")
        self._last_fb_surface_driver_kind = str(driver_kind or "")
        self._last_fb_surface_html_available = True
        self._last_fb_visible_text_available = True
        self._last_fb_anchor_values_available = True
        self._last_fb_reveal_actions_available = True
        self._last_fb_visible_text = rendered_text
        self._last_fb_live_anchor_values = list(anchor_values or [])
        self._last_fb_reveal_actions = list(reveal_actions or [])
        return page_source, rendered_text, list(anchor_values or []), list(reveal_actions or [])

    @staticmethod
    def _explicit_main_page_surface_capture_is_weak(
        page_html: Optional[str],
        rendered_text: Optional[str],
        anchor_values: Optional[List[str]],
    ) -> bool:
        visible_text = str(rendered_text or "").strip()
        live_anchor_values = [str(value or "").strip() for value in list(anchor_values or []) if str(value or "").strip()]
        if visible_text or live_anchor_values:
            return False

        html_text = str(page_html or "")
        if not html_text.strip():
            return True

        soup = BeautifulSoup(html_text, "html.parser")
        try:
            dom_text = " ".join(soup.stripped_strings)
        except Exception:
            dom_text = ""
        return len(dom_text) < 24

    def _restabilize_explicit_main_page_surface_capture(
        self,
        driver_kind: str = "session",
    ) -> Tuple[str, str, List[str], List[str]]:
        driver = None
        if str(driver_kind or "").startswith("anon"):
            driver = getattr(self, "_anon_driver", None)
        else:
            session = getattr(self, "session", None)
            driver = getattr(session, "driver", None)
        if driver is None:
            return "", "", [], []
        time.sleep(0.8)
        return self._collect_current_fb_email_surface_state(driver_kind=driver_kind)

    def _targeted_explicit_content_unavailable_restabilization(
        self,
        driver_kind: str = "session",
    ) -> Tuple[str, str, List[str], List[str]]:
        driver = None
        if not str(driver_kind or "").startswith("anon"):
            session = getattr(self, "session", None)
            driver = getattr(session, "driver", None)
        if driver is not None:
            try:
                _reveal_fb_contact_controls(driver, logger=self.logger)
            except Exception:
                pass
            try:
                driver.execute_script(
                    """
                    /* fb_content_unavailable_targeted_restabilization */
                    const delta = 400;
                    window.scrollBy(0, delta);
                    window.scrollTo(0, 0);
                    return delta;
                    """
                )
            except Exception:
                pass
            time.sleep(1.2)
        return self._collect_current_fb_email_surface_state(driver_kind=driver_kind)

    def _fetch_html_with_url(
        self,
        url: str,
        goto_about: bool = True,
        collect_surfaces: bool = True,
        skip_pre_nav_session_validation: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        budget = getattr(self, "_page_budget_remaining", 2)
        self._clear_last_fb_email_surface_state()
        if budget <= 0:
            _log(self.logger, "[FB Email] Skipped: page budget exhausted")
            return None, None
        self._page_budget_remaining = budget - 1
        self.fb_email_pages_visited += 1
        self._last_fb_timeout = False
        self._last_fb_timeout_url = ""
        if skip_pre_nav_session_validation:
            try:
                session = self._ensure_session(prewarm_session=False)
            except TypeError as exc:
                if "prewarm_session" not in str(exc):
                    raise
                session = self._ensure_session()
        else:
            session = self._ensure_session()
        if not session:
            return None, None
        if not skip_pre_nav_session_validation:
            self._ensure_driver_alive(session)
        def _navigate_once() -> Tuple[Optional[str], Optional[str]]:
            if skip_pre_nav_session_validation:
                try:
                    driver = session.navigate(
                        url,
                        logger=self.logger,
                        unblock_on_ready=True,
                        validate_session=False,
                    )
                except TypeError as exc:
                    if "validate_session" not in str(exc):
                        raise
                    try:
                        driver = session.navigate(url, logger=self.logger, unblock_on_ready=True)
                    except TypeError:
                        try:
                            driver = session.navigate(url, logger=self.logger)
                        except TypeError:
                            driver = session.navigate(url)
            else:
                try:
                    driver = session.navigate(url, logger=self.logger, unblock_on_ready=True)
                except TypeError:
                    try:
                        driver = session.navigate(url, logger=self.logger)
                    except TypeError:
                        driver = session.navigate(url)
            timed_out = bool(getattr(session, "last_nav_timed_out", False))
            if timed_out:
                self._last_fb_timeout = True
                self._last_fb_timeout_url = url
            if goto_about:
                goto_about_fn = getattr(self.legacy, "_goto_facebook_about", None)
                if callable(goto_about_fn):
                    try:
                        goto_about_fn(driver, url, timeout=5.0)
                    except Exception:
                        pass
            page_source = getattr(driver, "page_source", "") or ""
            if collect_surfaces:
                page_source, rendered_text, anchor_values, reveal_actions = _collect_fb_email_surface_state(
                    driver,
                    logger=self.logger,
                )
                self._last_fb_surface_html = page_source
                self._last_fb_surface_url = str(getattr(driver, "current_url", None) or url)
                self._last_fb_surface_driver_kind = "session"
                self._last_fb_surface_html_available = True
                self._last_fb_visible_text_available = True
                self._last_fb_anchor_values_available = True
                self._last_fb_reveal_actions_available = True
                self._last_fb_visible_text = rendered_text
                self._last_fb_live_anchor_values = list(anchor_values or [])
                self._last_fb_reveal_actions = list(reveal_actions or [])
            else:
                self._last_fb_surface_html = page_source or html
                self._last_fb_surface_url = str(getattr(driver, "current_url", None) or url)
                self._last_fb_surface_driver_kind = "session"
                self._last_fb_surface_html_available = True
            current_url = getattr(driver, "current_url", None) or url
            return page_source or driver.page_source, current_url

        html: Optional[str] = None
        current_url: Optional[str] = None

        try:
            html, current_url = _navigate_once()
        except Exception as exc:  # pragma: no cover - defensive
            _log(self.logger, f"[Night FB] Fetch failed (will refresh session) for {url}: {exc}")
            try:
                self._refresh_driver(session)
                html, current_url = _navigate_once()
            except FacebookDriverError as exc2:
                raise exc2
            except Exception as exc2:  # pragma: no cover - defensive
                _log(self.logger, f"[Night FB] Failed to fetch FB HTML after refresh for {url}: {exc2}")
                raise FacebookDriverError(str(exc2))

        current_url = current_url or url
        warning_reason = _looks_like_fb_warning_or_block(html, current_url)
        self._log_page_health(
            current_url,
            html,
            context="page",
            warning_reason=warning_reason,
        )
        if is_fb_login_redirect(current_url) or _is_fb_login_or_security_url(current_url):
            _log(self.logger, f"[Night FB] Ignoring login/redirect page: {current_url}")
            try:
                self._refresh_driver(session)
            except Exception:
                pass
            return None, current_url

        if warning_reason:
            self._skip_fb_due_to_warning = True
            self._skip_fb_due_to_warning_reason = warning_reason
            _log(
                self.logger,
                f"[Night FB] Circuit breaker: detected {warning_reason} page; skipping FB for remainder of run. url={current_url!r}"
            )
            raise FacebookDriverError(f"fb_circuit_breaker:{warning_reason}")

        return html, current_url

    def _fetch_html_with_url_anon(self, url: str, goto_about: bool = True, collect_surfaces: bool = True) -> Tuple[Optional[str], Optional[str]]:
        budget = getattr(self, "_page_budget_remaining", 2)
        self._clear_last_fb_email_surface_state()
        if budget <= 0:
            _log(self.logger, "[FB Email] Skipped: page budget exhausted")
            return None, None
        self._page_budget_remaining = budget - 1
        self.fb_email_pages_visited += 1
        self._last_fb_timeout = False
        self._last_fb_timeout_url = ""
        try:
            driver = self._get_anon_driver()
        except Exception as exc:
            _log(self.logger, f"[Night FB] Anonymous driver unavailable: {exc}")
            return None, None
        try:
            html, current_url, timed_out = _load_fb_page_with_timeout(driver, url, timeout_s=20.0, logger=self.logger)
            if timed_out:
                self._last_fb_timeout = True
                self._last_fb_timeout_url = url
            if goto_about:
                goto_about_fn = getattr(self.legacy, "_goto_facebook_about", None)
                if callable(goto_about_fn):
                    try:
                        goto_about_fn(driver, url, timeout=5.0)
                    except Exception:
                        pass
            page_source = getattr(driver, "page_source", "") or ""
            if collect_surfaces:
                page_source, rendered_text, anchor_values, reveal_actions = _collect_fb_email_surface_state(
                    driver,
                    logger=self.logger,
                )
                self._last_fb_surface_html = page_source
                self._last_fb_surface_url = str(getattr(driver, "current_url", None) or current_url or url)
                self._last_fb_surface_driver_kind = "anon_fallback"
                self._last_fb_surface_html_available = True
                self._last_fb_visible_text_available = True
                self._last_fb_anchor_values_available = True
                self._last_fb_reveal_actions_available = True
                self._last_fb_visible_text = rendered_text
                self._last_fb_live_anchor_values = list(anchor_values or [])
                self._last_fb_reveal_actions = list(reveal_actions or [])
            else:
                self._last_fb_surface_html = page_source or html
                self._last_fb_surface_url = str(getattr(driver, "current_url", None) or current_url or url)
                self._last_fb_surface_driver_kind = "anon_fallback"
                self._last_fb_surface_html_available = True
            current_url = getattr(driver, "current_url", None) or current_url or url
            self._log_page_health(current_url, html, context="page")
            if is_fb_login_redirect(current_url) or _is_fb_login_or_security_url(current_url):
                return None, current_url
            return page_source or driver.page_source, current_url
        except Exception as exc:
            _log(self.logger, f"[Night FB] Anonymous fetch failed for {url}: {exc}")
            return None, None

    def _fetch_html(self, url: str, collect_surfaces: bool = True) -> Optional[str]:
        html, _ = self._fetch_html_with_url(url, goto_about=True, collect_surfaces=collect_surfaces)
        return html

    def _should_allow_anonymous(self, row: Dict[str, str]) -> bool:
        source_dir = str(row.get("Source Directory", "") or row.get("Source Directory ".strip(), "") or "").lower()
        source_job = str(row.get("__source_job", "") or "").lower()
        return ("unearthed" in source_dir) or ("unearthed" in source_job)

    def _mark_row_checkpoint(self, row: Dict[str, str]) -> Dict[str, str]:
        """Mark a row as skipped due to FB checkpoint."""
        result = dict(row or {})
        result["FB_Status"] = "skipped_checkpoint"
        result["FB_Reason"] = "checkpoint"
        return result

    def _emit_fb_url_flow_summary(self, prefix: str = "[Night FB][URLFLOW] summary") -> None:
        if self._debug_fb_url_flow_summary_logged:
            return
        self._debug_fb_url_flow_summary_logged = True
        if not self._debug_fb_url_flow:
            return
        _log(
            self.logger,
            f"{prefix}: rows_seen={self._debug_fb_url_flow_seen} rows_with_fb_url_extracted={self._debug_fb_url_flow_with_urls} "
            f"rows_skipped_no_fb_url={self._debug_fb_url_flow_skipped}",
        )

    def _is_unearthed_source(self, row: Dict[str, str]) -> bool:
        source_dir = str(row.get("Source Directory", "") or "").strip().lower()
        source_tag = str(row.get("Source Tag", "") or "").strip().lower()
        source_job = str(row.get("__source_job", "") or "").strip().lower()
        return any("unearthed" in val for val in (source_dir, source_tag, source_job))

    def _maybe_recover_or_skip_on_checkpoint(self) -> bool:
        """
        Detect checkpoint blocks; optionally attempt one headed recovery; otherwise fail-fast.
        Returns True if it's safe to proceed with FB enrichment, False if the run should skip FB.
        """
        if self._skip_fb_due_to_checkpoint:
            return False
        self._sync_search_disable_from_run_state()

        reason = ""
        healthy = True
        session = None
        try:
            session = self._ensure_session()
            if session:
                healthy = bool(getattr(session, "last_health_ok", True))
                reason = str(getattr(session, "last_health_reason", "") or "")
        except FacebookDriverError as exc:
            reason = self._session_failed_reason or str(exc)
            if "checkpoint" not in reason.lower():
                # Not a checkpoint; propagate to let normal error handling occur.
                raise
            healthy = False

        if session is None:
            # Anonymous/legacy path; no checkpoint state available.
            return True

        if healthy or reason.lower() != "checkpoint":
            return True

        # Checkpoint detected.
        self._register_checkpoint_event()  # register once per detected checkpoint
        if self._checkpoint_guard_enabled and not self._search_disabled_due_to_checkpoint:
            self._search_disabled_due_to_checkpoint = True
            _log(self.logger, "[Night FB] search disabled due to checkpoint")
            return True

        if not self.skip_on_checkpoint:
            _log(self.logger, "[Night FB] Checkpoint detected but skip is disabled; continuing cautiously.")
            return True

        if self.allow_headed_recovery and (not self._headed_recovery_attempted):
            self._headed_recovery_attempted = True
            _log(self.logger, "[Night FB] Checkpoint detected; attempting headed recovery once...")
            try:
                if self.session and self._owns_session:
                    try:
                        self.session.close()
                    except Exception:
                        pass
                self.session = None
                self._session_state_logged = False
                self._owns_session = False
                self.headless = False  # force headed retry using same profile
                self._session_failed = False
                self._session_failed_reason = ""
                session = self._ensure_session()
                healthy = bool(getattr(session, "last_health_ok", True)) if session else False
                reason = str(getattr(session, "last_health_reason", "") or "")
                if healthy or reason.lower() != "checkpoint":
                    _log(self.logger, "[Night FB] Headed recovery succeeded; continuing with FB enrichment.")
                    return True
            except FacebookDriverError:
                # fall through to skip
                reason = "checkpoint"
            except Exception:
                reason = "checkpoint"

        if self.allow_headed_recovery and self._headed_recovery_attempted:
            _log(self.logger, "[Night FB] Checkpoint persists after headed recovery; skipping FB for remainder of run.")
        else:
            _log(self.logger, "[Night FB] Checkpoint detected; skipping FB for remainder of run (headed recovery disabled).")
        self._skip_fb_due_to_checkpoint = True
        disable_night_fb_run_state(
            self._run_state,
            "checkpoint",
            checkpointed=True,
        )
        return False

    def _fetch_search_surface(
        self,
        query_str: str,
        *,
        search_method: str,
        session=None,
    ) -> Tuple[str, Optional[Any], bool, str]:
        if search_method == "direct_route":
            encoded_q = urllib.parse.quote_plus(query_str)
            search_url = f"https://www.facebook.com/search/pages/?q={encoded_q}"
            _log(self.logger, f"[Night FB] search_method=direct_route query='{query_str}' url='{search_url}'")

            def _nav_with_session() -> Tuple[str, Any, bool, str]:
                drv = session.navigate(search_url)
                time.sleep(1.5)
                return (
                    getattr(drv, "page_source", "") or "",
                    drv,
                    bool(getattr(session, "last_nav_timed_out", False)),
                    getattr(session, "last_nav_current_url", "") or _safe_current_url(drv) or search_url,
                )

            def _nav_anon() -> Tuple[str, Any, bool, str]:
                driver = self._get_anon_driver()
                driver.get(search_url)
                time.sleep(1.5)
                return getattr(driver, "page_source", "") or "", driver, False, _safe_current_url(driver) or search_url

            nav_fn = _nav_with_session if session else _nav_anon
            try:
                html, driver, timed_out, current_url = nav_fn()
            except Exception as exc:  # pragma: no cover - defensive
                _log(self.logger, f"[Night FB] Search navigation failed (will refresh session): {exc}")
                if session:
                    try:
                        self._refresh_driver(session)
                    except FacebookDriverError as exc2:
                        raise exc2
                nav_fn = _nav_with_session if session else _nav_anon
                try:
                    html, driver, timed_out, current_url = nav_fn()
                except FacebookDriverError as exc2:
                    raise exc2
                except Exception as exc2:  # pragma: no cover - defensive
                    _log(self.logger, f"[Night FB] Search navigation failed after refresh: {exc2}")
                    raise FacebookDriverError(str(exc2))
            miss_reason = _fb_search_surface_miss_reason(
                html,
                driver=driver,
                current_url=current_url,
                timed_out=timed_out,
            )
            self._log_page_health(
                current_url,
                html,
                context="search",
                search_miss_reason=miss_reason,
            )
            return html, driver, timed_out, current_url

        if search_method == "homepage_ui":
            if session is None:
                return "", None, False, ""
            _log(self.logger, f"[Night FB] search_method=homepage_ui query='{query_str}'")
            driver = session.ensure_logged_in() if hasattr(session, "ensure_logged_in") else getattr(session, "driver", None)
            if driver is None:
                return "", None, False, ""
            html, current_url, timed_out = _run_fb_homepage_search(
                driver,
                query_str,
                logger=self.logger,
                log_prefix="[Night FB]",
            )
            miss_reason = _fb_search_surface_miss_reason(
                html,
                driver=driver,
                current_url=current_url,
                timed_out=timed_out,
            )
            self._log_page_health(
                current_url,
                html,
                context="search",
                search_miss_reason=miss_reason,
            )
            return html, driver, timed_out, current_url

        raise ValueError(f"Unsupported search_method: {search_method}")

    def _search_for_page(
        self,
        artist: str,
        location: str,
        allow_anon: bool = False,
        song_title: str = "",
        row: Any = None,
    ) -> Optional[str]:
        self._sync_search_disable_from_run_state()
        self._last_selected_candidate_context = None
        self._last_search_candidates = []
        self._last_search_reject_reason = ""
        self._last_search_reject_score = None
        self._checkpoint_limited_active = False
        if self._search_disabled_due_to_checkpoint:
            _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
            return None
        session = self._ensure_session()
        if not session and not allow_anon:
            return None
        if session:
            self._ensure_driver_alive(session)
        _, session_unhealthy, session_reason = self._session_state_snapshot(session)
        refine_enabled = os.getenv("FB_REFINE_QUERY") == "1"
        quality_gate_enabled = _bool_env("NIGHT_FB_MIN_QUALITY_GATE", default=False)
        try:
            quality_threshold = int(os.getenv("NIGHT_FB_MIN_QUALITY_SCORE") or 25)
        except Exception:
            quality_threshold = 25
        v2_enabled = _bool_env("FB_SEARCH_HARVEST_V2", default=False)

        def _build_slug_fallback_context() -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
            slug = _slugify(artist)
            fallback_url = f"https://www.facebook.com/{slug}" if slug else ""
            if not fallback_url or (not _fb_is_candidate_url_allowed(fallback_url)):
                return None, None
            try:
                fb_cand_cls = getattr(facebook_enrich, "FbCandidate", None)
                fallback_candidate = fb_cand_cls(name=artist, url=fallback_url, category="slug_fallback") if fb_cand_cls else {"name": artist, "url": fallback_url, "category": "slug_fallback"}
            except Exception:
                fallback_candidate = {"name": artist, "url": fallback_url, "category": "slug_fallback"}
            if quality_gate_enabled:
                try:
                    fallback_score, _fallback_reasons, _ = _score_candidate_min_quality(artist, fallback_candidate)
                    if fallback_score < quality_threshold:
                        return None, None
                except Exception:
                    return None, None
            base_score = 0.0
            try:
                if facebook_enrich is not None and callable(getattr(facebook_enrich, "score_fb_candidate", None)):
                    scored = facebook_enrich.score_fb_candidate(artist, artist, fallback_url, "slug_fallback")
                    if scored:
                        _, base_score, _ = scored
            except Exception:
                base_score = 0.0
            context = {
                "url": fallback_url,
                "name": artist,
                "category": "slug_fallback",
                "category_raw": "slug_fallback",
                "base_score": base_score,
            }
            return fallback_url, context

        checkpoint_limited = bool(session_unhealthy and "checkpoint" in (session_reason or "").lower())
        if checkpoint_limited:
            self._checkpoint_limited_active = True
            self._register_checkpoint_event()
            if not self._checkpoint_warned_this_row:
                _log(self.logger, "[Night FB][WARN] checkpoint active; skipping search/refine; using fallback only.")
                self._checkpoint_warned_this_row = True
            fallback_url, fallback_context = _build_slug_fallback_context()
            if fallback_context:
                self._last_selected_candidate_context = fallback_context
                self._last_search_candidates = [fallback_context]
            return fallback_url

        legacy_secondary_signal = _sanitize_fb_song_title(song_title)
        if not legacy_secondary_signal:
            legacy_secondary_signal = _normalize_fb_location_query(location)

        primary_query, secondary_signal = _build_fb_discovery_query(
            artist,
            location=location,
            song_title=song_title,
            row=row,
        )
        has_secondary_signal = bool(legacy_secondary_signal)
        if not primary_query:
            return None
        html, nav_driver, search_timed_out, current_search_url = self._fetch_search_surface(
            primary_query,
            search_method="direct_route",
            session=session,
        )
        self._sync_search_disable_from_run_state()
        if self._search_disabled_due_to_checkpoint:
            _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
            return None

        # Optional self-check: confirm shared URL gate predicate.
        if _bool_env("FB_DEBUG_CAND_GATE_ASSERT", default=False):
            try:
                assert _fb_is_candidate_url_allowed is facebook_enrich._fb_is_candidate_url_allowed  # type: ignore
            except Exception:
                pass

        refine_query_list = [f"{artist} musician", f"{artist} band"]
        if self.slow_mode_active:
            refine_query_list = refine_query_list[:1]
        refine_allowed = refine_enabled and not has_secondary_signal

        def _run_refine_queries(diagnostics: Optional[Dict[str, Any]] = None) -> List["facebook_enrich.FbCandidate"]:
            refine_candidates: List["facebook_enrich.FbCandidate"] = []
            for refine_query in refine_query_list:
                html_refined, drv_refined, _timed_out_refined, _current_refined_url = self._fetch_search_surface(
                    refine_query,
                    search_method="direct_route",
                    session=session,
                )
                refine_candidates.extend(
                    _harvest_candidates(
                        html_refined,
                        drv_refined,
                        artist,
                        logger=self.logger,
                        v2_enabled=v2_enabled,
                        session_unhealthy=session_unhealthy,
                        session_reason=session_reason,
                        diagnostics=diagnostics,
                    )
                )
            return refine_candidates

        diagnostics: Dict[str, Any] = {}
        candidates = _harvest_candidates(
            html,
            nav_driver,
            artist,
            logger=self.logger,
            v2_enabled=v2_enabled,
            session_unhealthy=session_unhealthy,
            session_reason=session_reason,
            diagnostics=diagnostics,
        )
        suppress_refine_queries = False
        if session and not candidates:
            miss_reason = _fb_search_surface_miss_reason(
                getattr(nav_driver, "page_source", "") or html or "",
                driver=nav_driver,
                current_url=current_search_url,
                timed_out=search_timed_out,
            )
            if miss_reason:
                _log(
                    self.logger,
                    f"[Night FB] search_method=direct_route failure_mode={miss_reason} query='{primary_query}'",
                )
                suppress_refine_queries = True
                diagnostics = {}
                html, nav_driver, search_timed_out, current_search_url = self._fetch_search_surface(
                    primary_query,
                    search_method="homepage_ui",
                    session=session,
                )
                self._sync_search_disable_from_run_state()
                if self._search_disabled_due_to_checkpoint:
                    _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
                    return None
                candidates = _harvest_candidates(
                    html,
                    nav_driver,
                    artist,
                    logger=self.logger,
                    v2_enabled=v2_enabled,
                    session_unhealthy=session_unhealthy,
                    session_reason=session_reason,
                    diagnostics=diagnostics,
                )
                candidates, homepage_failure_mode = _guard_homepage_fb_search_candidates(
                    candidates,
                    page_html=getattr(nav_driver, "page_source", "") or html or "",
                    current_url=current_search_url,
                    logger=self.logger,
                    log_prefix="[Night FB]",
                    query=primary_query,
                )
                if homepage_failure_mode:
                    _log(
                        self.logger,
                        f"[Night FB] search_method=homepage_ui failure_mode={homepage_failure_mode} query='{primary_query}'",
                    )
                if not candidates:
                    if not homepage_failure_mode:
                        homepage_miss_reason = _fb_search_surface_miss_reason(
                            getattr(nav_driver, "page_source", "") or html or "",
                            driver=nav_driver,
                            current_url=current_search_url,
                            timed_out=search_timed_out,
                        )
                        if homepage_miss_reason:
                            _log(
                                self.logger,
                                f"[Night FB] search_method=homepage_ui failure_mode={homepage_miss_reason} query='{primary_query}'",
                            )
        soft_blocked = bool(diagnostics.get("overlay_soft_block"))
        ranked_for_preview = _rank_candidates_for_preview(artist, candidates)

        need_refine = False
        if soft_blocked:
            self._enter_slow_mode("overlay_zero_anchors", max(self.slow_mode_multiplier, 1.5))
            # Skip refine cascade when soft-blocked; rely on slug/candidate fallback.
            need_refine = False
        elif refine_allowed and not suppress_refine_queries:
            top_score = ranked_for_preview[0]["score"] if ranked_for_preview else 0
            music_present = any(item["features"].get("music_any") for item in ranked_for_preview)
            if (not music_present) and top_score <= 0:
                need_refine = True

        if need_refine:
            refine_candidates = _run_refine_queries(diagnostics=diagnostics)
            if refine_candidates:
                candidates = _dedupe_candidates(list(candidates) + refine_candidates)
            ranked_for_preview = _rank_candidates_for_preview(artist, candidates)
            if refine_candidates:
                _log(self.logger, f"[Night FB] Refine query enabled; merged {len(refine_candidates)} refined candidates.")

        soft_blocked = soft_blocked or bool(diagnostics.get("overlay_soft_block"))

        min_accept_score = _min_fb_accept_score()
        ranked_candidates: List["facebook_enrich.FbCandidate"] = [item["candidate"] for item in ranked_for_preview]
        candidate, selected_by = self._choose_ranked_candidate(artist, ranked_for_preview, min_accept_score=min_accept_score)
        if not candidate:
            _log(self.logger, f"[Night FB] No viable FB candidates for '{artist}' after reject-cache and mismatch guard.")
            _maybe_log_rank_preview(artist, candidates, None, logger=self.logger, selected_by=selected_by)
            return None

        if quality_gate_enabled and candidate:
            refine_forced = False
            overlay_skip_logged = False
            while True:
                score_val, reasons, _meta = _score_candidate_min_quality(artist, candidate)
                if score_val >= quality_threshold:
                    break
                label = getattr(candidate, "name", None) or getattr(candidate, "url", None) or "<unknown>"
                _log(self.logger, f"[Night FB][QualityGate] rejected '{label}' score={score_val} reasons={' '.join(reasons) or '-'} threshold={quality_threshold}")
                if soft_blocked:
                    if not overlay_skip_logged:
                        _log(self.logger, "[Night FB] Skipping refine due to overlay soft block.")
                        overlay_skip_logged = True
                    refine_forced = True
                elif refine_allowed and (not refine_forced) and (not suppress_refine_queries):
                    refine_forced = True
                    forced_refine_candidates = _run_refine_queries(diagnostics=diagnostics)
                    soft_blocked = soft_blocked or bool(diagnostics.get("overlay_soft_block"))
                    if soft_blocked:
                        if not overlay_skip_logged:
                            _log(self.logger, "[Night FB] Skipping refine due to overlay soft block.")
                            overlay_skip_logged = True
                    else:
                        _log(self.logger, "[Night FB][QualityGate] forcing refine queries due to low top-candidate score.")
                    if forced_refine_candidates:
                        candidates = _dedupe_candidates(list(candidates) + forced_refine_candidates)
                    ranked_for_preview = _rank_candidates_for_preview(artist, candidates)
                    ranked_candidates = [item["candidate"] for item in ranked_for_preview]
                    candidate = ranked_candidates[0] if ranked_candidates else None
                    soft_blocked = soft_blocked or bool(diagnostics.get("overlay_soft_block"))
                    if soft_blocked and not overlay_skip_logged:
                        _log(self.logger, "[Night FB] Skipping refine due to overlay soft block.")
                        overlay_skip_logged = True
                    if candidate:
                        continue
                if self.slow_mode_active and not soft_blocked:
                    # In slow mode, avoid additional refine cascades; fall through to slug/skip logic.
                    break
                slug = _slugify(artist)
                fallback_url = f"https://www.facebook.com/{slug}" if slug else ""
                fallback_candidate = None
                if fallback_url and _fb_is_candidate_url_allowed(fallback_url):
                    try:
                        fb_cand_cls = getattr(facebook_enrich, "FbCandidate", None)
                        fallback_candidate = fb_cand_cls(name=artist, url=fallback_url, category="slug_fallback") if fb_cand_cls else {"name": artist, "url": fallback_url, "category": "slug_fallback"}
                    except Exception:
                        fallback_candidate = {"name": artist, "url": fallback_url, "category": "slug_fallback"}
                if fallback_candidate is not None:
                    fallback_score, fallback_reasons, _ = _score_candidate_min_quality(artist, fallback_candidate)
                    if fallback_score >= quality_threshold:
                        _log(self.logger, f"[Night FB][QualityGate] using slug fallback '{fallback_url}' score={fallback_score} reasons={' '.join(fallback_reasons) or '-'} threshold={quality_threshold}")
                        candidate = fallback_candidate
                        candidates = [fallback_candidate]
                        ranked_for_preview = _rank_candidates_for_preview(artist, candidates)
                        ranked_candidates = [fallback_candidate]
                        break
                _log(self.logger, "[Night FB][QualityGate] No acceptable FB candidates after quality gate; skipping FB search.")
                return None

        if not candidate:
            _log(self.logger, f"[Night FB] No non-junk FB candidates for '{artist}', skipping Facebook.")
            _maybe_log_rank_preview(artist, candidates, None, logger=self.logger, selected_by=selected_by)
            return None

        return self._select_candidate_url(
            artist,
            candidate,
            candidates,
            ranked_candidates,
            ranked_for_preview,
            selected_by,
            min_accept_score,
        )

    def _can_identity_soft_pass(self, artist_name: str, page_name: str, resolved_url: str, base_score: float) -> bool:
        """
        Conservative identity soft-pass for already-selected candidates.
        Requirements:
          - base_score >= 1.0
          - page/title closely matches artist name
          - URL is a simple page slug or profile.php?id=<digits>
          - URL is not a business/junk UI path
        """
        if base_score < 1.0:
            return False
        artist_slug = _slugify(artist_name)
        page_slug = _slugify(page_name)
        if not artist_slug or not page_slug:
            return False
        if not (artist_slug == page_slug or artist_slug in page_slug or page_slug in artist_slug):
            return False
        try:
            parsed = urllib.parse.urlparse(resolved_url or "")
        except Exception:
            return False
        path = (parsed.path or "").rstrip("/")
        if path.startswith("/profile.php"):
            qs = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=False)
            profile_id = (qs.get("id") or [""])[0]
            if not profile_id.isdigit():
                return False
        else:
            parts = [p for p in path.split("/") if p]
            if len(parts) != 1:
                return False
        if _is_junk_fb_candidate(resolved_url):
            return False
        return True

    def _scrape_single_fb_candidate(
        self,
        fb_url: str,
        row: Dict[str, str],
        artist_name: str,
        allow_anon: bool = False,
        candidate_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple[NightModeFacebookResult, List[str], str, str]]:
        raw_fb_url = fb_url or ""
        if raw_fb_url.startswith("/"):
            raw_fb_url = "https://www.facebook.com" + raw_fb_url
        gate_debug = os.getenv("FB_DEBUG_CAND_GATE") == "1"
        self._last_explicit_guard_reason = ""
        guard_reason, guard_url = _explicit_fb_pre_scrape_guard_reason(raw_fb_url)
        if guard_reason:
            self._last_explicit_guard_reason = guard_reason
            _log(
                self.logger,
                f'[Night FB][Explicit Guard] artist="{artist_name or "<unknown>"}" url="{guard_url or raw_fb_url or "<blank>"}" reason="{guard_reason}"',
            )
            if gate_debug:
                _log(self.logger, f"[Night FB][Gate] rejected url={raw_fb_url!r} before scrape reason={guard_reason}")
            return None
        candidate_url = _normalise_fb_url(raw_fb_url or "")
        _log(
            self.logger,
            f'[Night FB] Starting FB scrape for artist="{artist_name or "<unknown>"}" url="{candidate_url or raw_fb_url or "<blank>"}"',
        )
        _log(self.logger, f"[FB Email] Visiting {candidate_url}")

        used_driver_kind = "session"
        self._last_fb_surface_driver_kind = used_driver_kind
        outcome_hint = "fetch_error"
        reject_reason = ""
        timed_out_flag = False
        is_discovery = bool(candidate_context and candidate_context.get("search_discovery_accepted"))
        explicit_pass_a = bool(candidate_context and candidate_context.get("explicit_accepted_url"))
        accepted_page_visit = bool(is_discovery or explicit_pass_a)
        prefer_fast_accepted_loader = bool(
            explicit_pass_a
            and candidate_context
            and candidate_context.get("accepted_page_fast_loader_safe")
        )
        staged_main_page_surfaces = bool(candidate_context and candidate_context.get("search_discovery_accepted"))

        def _fetch_candidate_surface(
            requested_url: str,
            *,
            collect_surfaces: bool,
        ) -> Tuple[Optional[str], Optional[str]]:
            nonlocal used_driver_kind, timed_out_flag, outcome_hint
            requested_url = str(requested_url or "").strip()

            def _fetch_with_session() -> Tuple[Optional[str], Optional[str]]:
                fetch_kwargs: Dict[str, Any] = {"goto_about": False}
                if collect_surfaces:
                    pass
                else:
                    fetch_kwargs["collect_surfaces"] = False
                if accepted_page_visit:
                    try:
                        return self._fetch_html_with_url(
                            requested_url,
                            skip_pre_nav_session_validation=True,
                            **fetch_kwargs,
                        )
                    except TypeError as exc:
                        if "skip_pre_nav_session_validation" not in str(exc):
                            raise
                return self._fetch_html_with_url(requested_url, **fetch_kwargs)

            def _fetch_with_anon() -> Tuple[Optional[str], Optional[str]]:
                if collect_surfaces:
                    return self._fetch_html_with_url_anon(requested_url, goto_about=False)
                return self._fetch_html_with_url_anon(
                    requested_url,
                    goto_about=False,
                    collect_surfaces=False,
                )

            if prefer_fast_accepted_loader:
                budget_before = getattr(self, "_page_budget_remaining", 0)
                pages_visited_before = self.fb_email_pages_visited
                timeout_before = bool(getattr(self, "_last_fb_timeout", False))
                timeout_url_before = str(getattr(self, "_last_fb_timeout_url", "") or "")
                fast_html, fast_resolved = _fetch_with_anon()
                timed_out_flag = timed_out_flag or bool(getattr(self, "_last_fb_timeout", False))
                if fast_html:
                    used_driver_kind = "anon_fast"
                    self._last_fb_surface_driver_kind = used_driver_kind
                    outcome_hint = "fetched"
                    _log(self.logger, f"[Night FB][AcceptedPage] using fast loader url='{requested_url}'")
                    return fast_html, fast_resolved
                self._page_budget_remaining = budget_before
                self.fb_email_pages_visited = pages_visited_before
                self._last_fb_timeout = timeout_before
                self._last_fb_timeout_url = timeout_url_before
                _log(
                    self.logger,
                    f"[Night FB][AcceptedPage] fast loader fallback -> session url='{requested_url}'",
                )

            session_html, session_resolved = _fetch_with_session()
            timed_out_flag = timed_out_flag or bool(getattr(self, "_last_fb_timeout", False))
            if session_html:
                used_driver_kind = "session"
                self._last_fb_surface_driver_kind = used_driver_kind
                outcome_hint = "fetched"
                return session_html, session_resolved

            if allow_anon and not prefer_fast_accepted_loader:
                anon_html, anon_resolved = _fetch_with_anon()
                timed_out_flag = timed_out_flag or bool(getattr(self, "_last_fb_timeout", False))
                if anon_html:
                    used_driver_kind = "anon_fallback"
                    self._last_fb_surface_driver_kind = used_driver_kind
                    outcome_hint = "fetched"
                return anon_html, anon_resolved

            return session_html, session_resolved

        html, resolved_url = _fetch_candidate_surface(
            candidate_url,
            collect_surfaces=True,
        )
        if (not html) and timed_out_flag:
            return None, [], used_driver_kind, "timeout"
        if not html:
            return None
        resolved_url = _normalise_fb_url(resolved_url or candidate_url)
        if _is_fb_login_or_security_url(resolved_url):
            _log(self.logger, f"[Night FB] Ignoring login/redirect page: {resolved_url}")
            self.fb_rows_skipped["challenge"] += 1
            return None, [], used_driver_kind, "login_wall"

        if explicit_pass_a:
            targeted_restabilization_attempted = False
            main_render_reason = _explicit_fb_render_state_invalid_reason(
                html,
                resolved_url=resolved_url,
                rendered_text=getattr(self, "_last_fb_visible_text", "") or "",
                anchor_values=getattr(self, "_last_fb_live_anchor_values", []) or [],
            )
            self._last_fb_render_invalid_reason = str(main_render_reason or "")
            if main_render_reason:
                warning_reason = _looks_like_fb_warning_or_block(html, resolved_url) or ""
                health = _night_fb_page_health_snapshot(
                    resolved_url,
                    html,
                    warning_reason=warning_reason,
                )
                targeted_restabilization = (
                    main_render_reason == "content_unavailable"
                    and used_driver_kind == "session"
                    and not warning_reason
                    and not health.get("captcha")
                    and not health.get("checkpoint")
                    and not health.get("login_wall")
                )
                if targeted_restabilization:
                    targeted_restabilization_attempted = True
                    _log(
                        self.logger,
                        "[Night FB][RenderGate] content_unavailable -> targeted restabilization",
                    )
                    refreshed_html, refreshed_text, refreshed_anchor_values, _ = self._targeted_explicit_content_unavailable_restabilization(
                        driver_kind=used_driver_kind,
                    )
                else:
                    _log(
                        self.logger,
                        f"[Night FB][RenderGate] explicit page state invalid; recollecting current surface once url='{resolved_url}' reason='{main_render_reason}'",
                    )
                    refreshed_html, refreshed_text, refreshed_anchor_values, _ = self._collect_current_fb_email_surface_state(
                        driver_kind=used_driver_kind,
                    )
                if refreshed_html:
                    html = refreshed_html
                main_render_reason = _explicit_fb_render_state_invalid_reason(
                    html,
                    resolved_url=resolved_url,
                    rendered_text=refreshed_text,
                    anchor_values=refreshed_anchor_values,
                )
                self._last_fb_render_invalid_reason = str(main_render_reason or "")
                if main_render_reason:
                    about_fallback_applied = False
                    about_fallback_warning_reason = _looks_like_fb_warning_or_block(html, resolved_url) or ""
                    about_fallback_health = _night_fb_page_health_snapshot(
                        resolved_url,
                        html,
                        warning_reason=about_fallback_warning_reason,
                    )
                    about_fallback_eligible = (
                        main_render_reason == "content_unavailable"
                        and targeted_restabilization_attempted
                        and self._page_budget_remaining > 0
                        and not about_fallback_warning_reason
                        and not about_fallback_health.get("captcha")
                        and not about_fallback_health.get("checkpoint")
                        and not about_fallback_health.get("login_wall")
                    )
                    if about_fallback_eligible:
                        fallback_variants = _fetch_fb_about_variants(resolved_url)
                        about_fallback_url = fallback_variants[0] if fallback_variants else ""
                        if about_fallback_url:
                            _log(
                                self.logger,
                                "[FB Night][RenderGate] content_unavailable -> about fallback",
                            )
                            try:
                                about_html, about_resolved = _fetch_candidate_surface(
                                    about_fallback_url,
                                    collect_surfaces=True,
                                )
                            except Exception:
                                about_html, about_resolved = None, about_fallback_url

                            about_surface_url = _normalise_fb_url(about_resolved or about_fallback_url)
                            if about_html and not _is_fb_login_or_security_url(about_surface_url):
                                about_warning_reason = _looks_like_fb_warning_or_block(
                                    about_html,
                                    about_surface_url,
                                ) or ""
                                about_health = _night_fb_page_health_snapshot(
                                    about_surface_url,
                                    about_html,
                                    warning_reason=about_warning_reason,
                                )
                                about_render_reason = _explicit_fb_render_state_invalid_reason(
                                    about_html,
                                    resolved_url=about_surface_url,
                                    rendered_text=getattr(self, "_last_fb_visible_text", "") or "",
                                    anchor_values=getattr(self, "_last_fb_live_anchor_values", []) or [],
                                )
                                if (
                                    not about_render_reason
                                    and not about_warning_reason
                                    and not about_health.get("captcha")
                                    and not about_health.get("checkpoint")
                                    and not about_health.get("login_wall")
                                ):
                                    html = about_html
                                    self._last_fb_render_invalid_reason = ""
                                    about_fallback_applied = True
                    if not about_fallback_applied:
                        self._last_fb_render_invalid_reason = "content_unavailable"
                        _log(
                            self.logger,
                            f"[Night FB][PageUnavailable] {resolved_url} (render_state={main_render_reason})",
                        )
                        return None, [], used_driver_kind, "content_unavailable"

        if _is_fb_content_unavailable_page(html):
            self._last_fb_render_invalid_reason = "content_unavailable"
            _log(self.logger, f"[Night FB][PageUnavailable] {resolved_url}")
            return None, [], used_driver_kind, "content_unavailable"

        soup = BeautifulSoup(html, "html.parser")
        meta_category = ""
        page_title = ""
        try:
            meta_tag = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
            meta_category = (meta_tag.get("content") or "").strip()
        except Exception:
            meta_category = ""
        try:
            title_tag = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
            page_title = (title_tag.get("content") if title_tag and title_tag.has_attr("content") else title_tag.get_text()) if title_tag else ""
        except Exception:
            page_title = ""

        _log(self.logger, f"[FB Email] Scanning main page HTML for emails: {resolved_url}")
        has_music_signals_main = _night_fb_has_music_signals(soup, {"url": resolved_url})
        main_visible_text = getattr(self, "_last_fb_visible_text", "") or ""
        main_anchor_values = list(getattr(self, "_last_fb_live_anchor_values", []) or [])
        if explicit_pass_a and self._explicit_main_page_surface_capture_is_weak(html, main_visible_text, main_anchor_values):
            _log(
                self.logger,
                f"[Night FB][Explicit Stabilize] weak main-page capture detected; recollecting once url='{resolved_url}'",
            )
            refreshed_html, refreshed_text, refreshed_anchor_values, _ = self._restabilize_explicit_main_page_surface_capture(
                driver_kind=used_driver_kind,
            )
            if refreshed_html:
                html = refreshed_html
                soup = BeautifulSoup(html, "html.parser")
            main_visible_text = refreshed_text
            main_anchor_values = list(refreshed_anchor_values or [])
        _log_fb_email_surface_debug(self.logger, f"main:{resolved_url}", html or "", main_visible_text)

        main_surface_url = resolved_url
        main_surface_html = html or ""
        main_surface_visible_text = main_visible_text
        main_surface_anchor_values = list(main_anchor_values or [])
        main_surface_served = False

        def _fetch_shared_surface(target_url: str) -> FacebookAcceptedPageFetchResult:
            nonlocal used_driver_kind, main_surface_served
            requested_url = str(target_url or "").strip()
            if not main_surface_served:
                main_surface_served = True
                return FacebookAcceptedPageFetchResult(
                    requested_url=requested_url,
                    resolved_url=main_surface_url,
                    html=main_surface_html,
                    rendered_text=main_surface_visible_text,
                    anchor_values=list(main_surface_anchor_values or []),
                )

            fetched_html = ""
            fetched_resolved = requested_url
            try:
                fetched_html, fetched_resolved = _fetch_candidate_surface(
                    requested_url,
                    collect_surfaces=True,
                )
            except Exception:
                fetched_html, fetched_resolved = "", requested_url

            final_resolved = str(fetched_resolved or requested_url).strip() or requested_url
            if _is_fb_login_or_security_url(final_resolved):
                return FacebookAcceptedPageFetchResult(
                    requested_url=requested_url,
                    resolved_url=final_resolved,
                    status_reason="login_wall",
                )

            return FacebookAcceptedPageFetchResult(
                requested_url=requested_url,
                resolved_url=final_resolved,
                html=fetched_html or "",
                rendered_text=getattr(self, "_last_fb_visible_text", "") or "",
                anchor_values=list(getattr(self, "_last_fb_live_anchor_values", []) or []),
            )

        def _refresh_shared_main_surface(
            _surface: FacebookAcceptedPageFetchResult,
            extracted_emails: List[str],
        ) -> Optional[FacebookAcceptedPageFetchResult]:
            nonlocal html, soup, main_surface_html, main_surface_visible_text, main_surface_anchor_values
            if not staged_main_page_surfaces or _filter_low_quality_fb_emails(extracted_emails):
                return None
            refreshed_html, refreshed_text, refreshed_anchor_values, _ = self._collect_current_fb_email_surface_state(
                driver_kind=used_driver_kind,
            )
            if refreshed_html:
                html = refreshed_html
                soup = BeautifulSoup(html, "html.parser")
            main_surface_html = html or refreshed_html or ""
            main_surface_visible_text = refreshed_text
            main_surface_anchor_values = list(refreshed_anchor_values or [])
            _log_fb_email_surface_debug(self.logger, f"main:{resolved_url}", main_surface_html, main_surface_visible_text)
            return FacebookAcceptedPageFetchResult(
                requested_url=candidate_url,
                resolved_url=resolved_url,
                html=main_surface_html,
                rendered_text=main_surface_visible_text,
                anchor_values=list(main_surface_anchor_values or []),
            )

        def _select_shared_secondary_url(base_url: str, page_html: str) -> Optional[str]:
            if not page_html:
                return None
            return _pick_fb_contact_link(BeautifulSoup(page_html, "html.parser"), base_url)

        continue_after_main_email = bool(self._page_budget_remaining > 0 and not explicit_pass_a)
        if is_discovery:
            continue_after_main_email = False

        sweep_result = _run_bounded_fb_accepted_page_sweep(
            candidate_url,
            _fetch_shared_surface,
            select_secondary_url=_select_shared_secondary_url,
            fallback_secondary_urls=_fetch_fb_about_variants,
            refresh_main_surface=None if staged_main_page_surfaces else _refresh_shared_main_surface,
            continue_after_main_email=continue_after_main_email,
            stop_after_first_filtered=explicit_pass_a,
            on_secondary_selected=lambda target: _log(self.logger, f"[FB Email] Visiting {target}"),
            on_secondary_fallback=lambda target: (
                _log(self.logger, f"[FB Email] No valid contact surface found; trying direct fallback {target}"),
                _log(self.logger, f"[FB Email] Visiting {target}"),
            ),
            on_no_secondary=lambda: _log(self.logger, "[FB Email] No valid contact surface found"),
        )

        emails_raw = list(sweep_result.main_emails or [])
        main_mailto = bool(sweep_result.main_mailto)
        emails = _filter_low_quality_fb_emails(emails_raw)
        main_surface = _fb_email_surface_label("main", used_mailto=main_mailto)
        main_surface_map = {
            email: {
                "surface": main_surface,
                "extract_method": "mailto" if main_mailto else "regex",
            }
            for email in emails
        }
        about_surface_map: Dict[str, str] = {}
        about_emails: List[str] = []
        about_mailto = False
        email_method = "mailto" if emails and main_mailto else ("regex" if emails else "")
        need_about_fetch = bool(sweep_result.secondary_attempted or continue_after_main_email)
        if emails:
            for email in emails:
                _log(self.logger, f"[FB Email] Found email on main page: {email}")
            if need_about_fetch:
                _log(self.logger, "[FB Email] Main page email found; About/Contact fetch remains enabled under the current page budget.")
            elif self._page_budget_remaining <= 0:
                _log(self.logger, "[FB Email] Skipping contact/about fetch because the page budget is exhausted")
        else:
            _log(self.logger, "[FB Email] No email found on main page; evaluating contact/about fetch")

        about_attempted = "no"
        about_result = ""
        email_source = "main" if emails else ""
        has_music_signals = has_music_signals_main

        def _coerce_str(val) -> str:
            try:
                return str(val or "").strip()
            except Exception:
                return ""

        seed_fb_raw = row.get("Facebook_URL") or row.get("Facebook URL") or row.get("Facebook Url") or row.get("FB_URL")
        seed_fb_norm = _normalise_fb_url(_coerce_str(seed_fb_raw)) if seed_fb_raw else ""
        seed_url_match = bool(seed_fb_norm and resolved_url and _normalise_fb_url(resolved_url) == seed_fb_norm)
        artist_location = _coerce_str(row.get("Country_Derived") or row.get("Country") or row.get("Location"))

        contact_url: Optional[str] = None
        if sweep_result.secondary_attempted:
            if not has_music_signals:
                _log(self.logger, f"[Night FB] No music signals on main page {resolved_url}, checking About/Contact...")
            contact_url = sweep_result.secondary_surface.requested_url if sweep_result.secondary_surface else ""
            about_attempted = "yes"
            about_html = sweep_result.secondary_surface.html if sweep_result.secondary_surface else ""
            about_resolved = sweep_result.secondary_surface.resolved_url if sweep_result.secondary_surface else contact_url
            final_about = _normalise_fb_url(about_resolved or contact_url)
            if sweep_result.secondary_status_reason == "login_wall" or _is_fb_login_or_security_url(final_about):
                about_result = "blocked_login"
                self.fb_rows_skipped["challenge"] += 1
            else:
                lower_html = (about_html or "").lower()
                if any(tok in lower_html for tok in ("checkpoint", "consent", "cookie", "privacy")):
                    about_result = "checkpoint"
                    self.fb_rows_skipped["checkpoint"] += 1
                else:
                    not_found_phrases = ("page isn’t available", "page isn't available", "content isn't available", "not available right now")
                    if any(p in lower_html for p in not_found_phrases):
                        about_result = "not_found"
                    else:
                        about_soup = BeautifulSoup(about_html or "", "html.parser") if about_html else None
                        if (not has_music_signals) and about_soup:
                            if _night_fb_has_music_signals(about_soup, {"url": final_about}):
                                has_music_signals = True
                                about_result = "music_signals"
                                _log(self.logger, f"[Night FB] Music signals found on About tab {final_about}.")
                        about_visible_text = sweep_result.secondary_surface.rendered_text if sweep_result.secondary_surface else ""
                        _log_fb_email_surface_debug(
                            self.logger,
                            f"about:{final_about}",
                            about_html or "",
                            about_visible_text,
                        )
                        about_emails = _filter_low_quality_fb_emails(sweep_result.secondary_emails)
                        about_mailto = bool(sweep_result.secondary_mailto)
                        about_surface = _fb_email_surface_label(
                            _fb_contact_surface_label(contact_url),
                            used_mailto=about_mailto,
                        )
                        about_surface_map = {
                            email: {
                                "surface": about_surface,
                                "extract_method": "mailto" if about_mailto else "regex",
                            }
                            for email in about_emails
                        }
                        if about_emails:
                            email_method = "mailto" if about_mailto else "regex"
                            email_source = contact_url.rsplit("/", 1)[-1] or "about"
                            about_result = "emails_found"
                        if not about_result:
                            about_result = "no_email"
        elif need_about_fetch and not sweep_result.secondary_attempted:
            about_result = "no_contact_link"

        if about_result == "not_found" and not emails and not has_music_signals:
            _log(self.logger, f"[Night FB][PageUnavailable] {resolved_url} (about tab)")
            outcome_hint = "content_unavailable"

        combined_emails = filter_system_telemetry_emails([*emails, *about_emails])
        keep_explicit_pass_a_emails = bool(
            combined_emails and candidate_context and candidate_context.get("explicit_accepted_url")
        )
        # Email override gating when music signals are missing.
        email_override_decision = True
        email_override_reason = ""
        if combined_emails and not has_music_signals and not keep_explicit_pass_a_emails:
            override_score = candidate_context.get("base_score") if candidate_context else 0.0
            try:
                override_score = float(override_score or 0.0)
            except Exception:
                override_score = 0.0
            if (
                override_score < 1.0
                and seed_url_match
                and self._can_identity_soft_pass(artist_name, page_title, resolved_url, 1.0)
            ):
                override_score = 1.0
            extracted = {
                "has_music_signals": has_music_signals,
                "category": meta_category,
                "descriptor": page_title,
                "music_hint": bool(candidate_context and candidate_context.get("category") and _category_is_music_like(candidate_context.get("category"))),
                "score": override_score,
                "seed_url_match": seed_url_match,
                "artist_location": artist_location,
            }
            email_override_decision, email_override_reason = should_accept_email_override(
                artist_name,
                {
                    "name": page_title,
                    "category": meta_category,
                    "raw_category": meta_category,
                    "base_score": override_score,
                },
                extracted_signals=extracted,
            )
            if email_override_decision:
                _log(self.logger, f"[Night FB][EmailOverrideAccept] url='{resolved_url}' reason='{email_override_reason}' emails={len(combined_emails)} category='{meta_category}' name='{page_title}'")
            else:
                _log(self.logger, f"[Night FB][EmailOverrideReject] url='{resolved_url}' reason='{email_override_reason}' emails={len(combined_emails)} category='{meta_category}' name='{page_title}'")
                emails = []
                about_emails = []
                combined_emails = []
                reject_reason = email_override_reason or "email_override_reject"
        elif keep_explicit_pass_a_emails:
            _log(
                self.logger,
                f"[Night FB][PASS A] Preserving extracted email(s) from explicit accepted URL despite weak post-fetch music signals: {resolved_url}",
            )

        gate_soft_pass_category = False
        gate_soft_pass_identity = False
        if not has_music_signals and not combined_emails:
            if _category_is_music_like(meta_category):
                gate_soft_pass_category = True
                _log(self.logger, f"[Night FB] Soft-pass music gate by category allowlist: category='{meta_category}' url='{resolved_url}'")
            else:
                ctx = candidate_context or {}
                ctx_url = _normalise_fb_url(str(ctx.get("url") or ""))
                base_score = float(ctx.get("base_score") or 0.0)
                if ctx_url and ctx_url == resolved_url:
                    page_name_for_identity = page_title or str(ctx.get("name") or "")
                    if self._can_identity_soft_pass(artist_name, page_name_for_identity, resolved_url, base_score):
                        gate_soft_pass_identity = True
                        _log(self.logger, "[FB Enrich] Soft-pass music gate: strong identity match, no explicit anti-signals")
                if not gate_soft_pass_identity:
                    _log(self.logger, f"[Night FB] No music signals detected on FB page {resolved_url}, skipping.")
                    reject_reason = reject_reason or "no_music_signals"

        accepted = not bool(reject_reason) and bool(has_music_signals or combined_emails or gate_soft_pass_category or gate_soft_pass_identity)
        if not accepted:
            # On rejection, strip any collected emails and cache the reject for this run.
            emails = []
            about_emails = []
            combined_emails = []
            self._fb_mark_rejected(artist_name, resolved_url or candidate_url, reject_reason or outcome_hint)

        if combined_emails:
            unique_emails = sorted(set(combined_emails))
            for email in unique_emails:
                _log(self.logger, f"[FB Email] Found email: {email}")
            self.fb_emails_found += len(unique_emails)
        else:
            _log(self.logger, "[FB Email] No email found")

        persisted_facebook_url = canonicalize_facebook_url(resolved_url)
        if persisted_facebook_url:
            persisted_facebook_url = _normalise_fb_url(persisted_facebook_url) or persisted_facebook_url

        source_context = {
            "surfaces": {
                **main_surface_map,
                **about_surface_map,
            }
        }

        night_result = self._build_result(
            combined_emails,
            str(row.get("Email_All", "") or ""),
            persisted_facebook_url,
            artist_name,
            source_context=source_context,
            allow_empty=True if not accepted else has_music_signals or combined_emails or gate_soft_pass_category or gate_soft_pass_identity,
            accepted=accepted,
            reject_reason=reject_reason,
            candidate_url=resolved_url,
            email_extract_method=email_method or "regex",
        )
        if not night_result:
            return None
        night_result.source_context = source_context
        night_result.accepted = accepted
        night_result.reject_reason = reject_reason or ""
        night_result.candidate_url = resolved_url
        night_result.about_attempted = about_attempted
        night_result.about_result = about_result or ("soft_pass_identity" if gate_soft_pass_identity else "soft_pass_category" if gate_soft_pass_category else "")
        # Propagate confidence metadata for downstream review flagging.
        match_level_ctx = ""
        if candidate_context:
            match_level_ctx = str(candidate_context.get("match_level") or "")
        name_consistency_flag_ctx: Optional[int] = None
        try:
            raw_flag = row.get("name_consistency_flag")
            if raw_flag is not None and raw_flag != "":
                name_consistency_flag_ctx = int(raw_flag)
        except Exception:
            name_consistency_flag_ctx = None
        if name_consistency_flag_ctx is None:
            if match_level_ctx == "mismatch":
                name_consistency_flag_ctx = 0
            elif match_level_ctx in ("exact", "near"):
                name_consistency_flag_ctx = 1
        selected_by_ctx = ""
        try:
            selected_by_ctx = str(candidate_context.get("selected_by") or "")
        except Exception:
            selected_by_ctx = ""
        review_reason = ""
        if match_level_ctx == "mismatch":
            review_reason = "fb_low_confidence:mismatch"
        elif selected_by_ctx == "mismatch_fallback":
            review_reason = "fb_low_confidence:mismatch_fallback"
        night_result.match_level = match_level_ctx
        night_result.selected_by = selected_by_ctx
        night_result.name_consistency_flag = name_consistency_flag_ctx
        night_result.review_reason = review_reason
        if gate_soft_pass_category:
            row["FB_Gate"] = "soft_pass_category"
        if gate_soft_pass_identity:
            row["FB_Gate"] = "soft_pass_identity"
        outcome_hint = "found_email" if combined_emails else "no_email_on_page"
        if night_result is None:
            if reject_reason:
                self._fb_mark_rejected(artist_name, resolved_url or candidate_url, reject_reason)
            return None, combined_emails, used_driver_kind, reject_reason or outcome_hint
        selected_surface = ""
        if isinstance(source_context.get("surfaces"), dict) and night_result.email:
            normalized_selected = normalize_email_value(night_result.email)
            selected_meta = source_context["surfaces"].get(normalized_selected) or source_context["surfaces"].get(night_result.email) or ""
            if isinstance(selected_meta, dict):
                selected_surface = str(
                    selected_meta.get("surface")
                    or selected_meta.get("email_source")
                    or selected_meta.get("source")
                    or ""
                ).strip().lower()
            else:
                selected_surface = str(selected_meta or "").strip().lower()
        if selected_surface:
            night_result.email_source = "about" if ("about" in selected_surface or "contact" in selected_surface) else "main"
            if night_result.email_source == "about" and about_emails and night_result.email in about_emails:
                night_result.email_extract_method = "mailto" if about_mailto else "regex"
            elif night_result.email_source == "main" and emails and night_result.email in emails:
                night_result.email_extract_method = "mailto" if main_mailto else "regex"
        else:
            night_result.email_source = email_source
        return night_result, combined_emails, used_driver_kind, outcome_hint

    def _build_result(
        self,
        emails: List[str],
        email_all_existing: str,
        facebook_url: str,
        artist_name: str,
        source_context: Optional[Dict] = None,
        email_source: str = "main",
        about_attempted: str = "no",
        about_result: str = "",
        allow_empty: bool = False,
        accepted: bool = True,
        reject_reason: str = "",
        candidate_url: str = "",
        email_extract_method: str = "",
    ) -> Optional[NightModeFacebookResult]:
        emails = filter_system_telemetry_emails(emails)
        if not emails and not allow_empty:
            return None
        primary = _choose_primary_email(emails, artist_name, source_context=source_context) if emails else None
        merged_all = _merge_email_all(email_all_existing, emails)
        email_type = "fb_night"
        return NightModeFacebookResult(
            email=primary,
            email_all=merged_all,
            email_type=email_type,
            facebook_url=facebook_url,
            email_source=email_source,
            email_source_url=facebook_url,
            email_extract_method=email_extract_method or "regex",
            about_attempted=about_attempted,
            about_result=about_result,
            accepted=accepted,
            reject_reason=reject_reason,
            candidate_url=candidate_url,
        )

    def _apply_night_fb_result(
        self,
        target_row: Dict[str, str],
        night_result: NightModeFacebookResult,
        emails: List[str],
        page_url: str,
        fb_status_hint: str = "",
        fb_reason_hint: str = "",
    ) -> Dict[str, str]:
        if not night_result:
            return target_row
        if hasattr(night_result, "accepted") and not getattr(night_result, "accepted", True):
            reason = getattr(night_result, "reject_reason", "") or fb_reason_hint or fb_status_hint or "reject"
            if reason and not target_row.get("FB_Reason"):
                target_row["FB_Reason"] = reason
            if not target_row.get("FB_Status"):
                target_row["FB_Status"] = "rejected"
            target_row[FB_ATTEMPT_STATE_COL] = "attempted_fb_rejected_by_acceptance_guard"
            _log(
                self.logger,
                f"[FB Guard] Discarding emails from rejected FB page '{page_url or night_result.facebook_url or '<unknown>'}' for '{target_row.get('Artist Name', '') or target_row.get('Artist', '') or '<unknown>'}' (reason={reason})",
            )
            return target_row
        fb_status_raw = str(fb_status_hint or target_row.get("FB_Status", "") or "")
        fb_reason = str(fb_reason_hint or target_row.get("FB_Reason", "") or "")
        artist_name = str(target_row.get("Artist Name", "") or target_row.get("Artist", "") or "").strip() or "<unknown>"
        if _fb_status_is_rejected(fb_status_raw) or _fb_status_is_rejected(fb_reason):
            reason = fb_reason or fb_status_raw or "reject"
            page_label = page_url or "<unknown>"
            target_row[FB_ATTEMPT_STATE_COL] = "attempted_fb_rejected_by_acceptance_guard"
            _log(
                self.logger,
                f"[FB Guard] Discarding emails from rejected FB page '{page_label}' for '{artist_name}' (reason={reason})",
            )
            # Do not mutate email fields; preserve status/reason already set.
            return target_row

        def _fb_status_is_terminal(status: str) -> bool:
            status_norm = (status or "").lower()
            tokens = (
                "login_wall",
                "checkpoint",
                "rate_limit",
                "rate-limited",
                "rate_limited",
                "driver_error",
                "captcha",
                "cooldown",
                "redirect",
            )
            return any(tok in status_norm for tok in tokens)

        target_row["Email"] = night_result.email or target_row.get("Email", "")
        target_row["Email_All"] = night_result.email_all
        target_row["Email_Type"] = night_result.email_type
        canonical_fb_url = canonicalize_facebook_url(night_result.facebook_url)
        if canonical_fb_url:
            target_row["Facebook_URL"] = canonical_fb_url
        provenance_emails = emails or night_result.email_all or night_result.email
        provenance_surface = "facebook_about" if (night_result.email_source or "").strip().lower() == "about" else "facebook_main"
        provenance_source_context = getattr(night_result, "source_context", None)
        if isinstance(provenance_source_context, dict) and isinstance(provenance_source_context.get("surfaces"), dict):
            grouped_emails: Dict[str, List[Tuple[str, str]]] = {}
            for email in filter_system_telemetry_emails(provenance_emails):
                normalized_email = normalize_email_value(email)
                raw_surface = provenance_source_context["surfaces"].get(normalized_email) or provenance_source_context["surfaces"].get(email) or ""
                if isinstance(raw_surface, dict):
                    surface_norm = str(
                        raw_surface.get("surface")
                        or raw_surface.get("email_source")
                        or raw_surface.get("source")
                        or ""
                    ).strip().lower()
                    extract_method = str(raw_surface.get("extract_method") or night_result.email_extract_method or "regex")
                else:
                    surface_norm = str(raw_surface or "").strip().lower()
                    extract_method = night_result.email_extract_method or "regex"
                surface = "facebook_about" if ("about" in surface_norm or "contact" in surface_norm) else "facebook_main"
                grouped_emails.setdefault(surface, []).append((email, extract_method))
            for surface, bucket in grouped_emails.items():
                bucket_emails = [email for email, _method in bucket]
                bucket_method = bucket[0][1] if bucket else night_result.email_extract_method or "regex"
                merge_email_provenance_into_target(
                    target_row,
                    bucket_emails,
                    source_url=(
                        page_url
                        or night_result.email_source_url
                        or night_result.facebook_url
                        or target_row.get("Facebook_URL", "")
                    ),
                    source_type="facebook_enrich",
                    method=bucket_method,
                    surface=surface,
                )
        else:
            merge_email_provenance_into_target(
                target_row,
                provenance_emails,
                source_url=(
                    page_url
                    or night_result.email_source_url
                    or night_result.facebook_url
                    or target_row.get("Facebook_URL", "")
                ),
                source_type="facebook_enrich",
                method=night_result.email_extract_method or "regex",
                surface=provenance_surface,
            )
        if night_result.email_source:
            target_row["FB_Email_Source"] = night_result.email_source
        if night_result.about_attempted:
            target_row["FB_About_Attempted"] = night_result.about_attempted
        if night_result.about_result:
            target_row["FB_About_Result"] = night_result.about_result
        # Confidence metadata
        if getattr(night_result, "match_level", None):
            target_row["FB_Match_Level"] = night_result.match_level
        if getattr(night_result, "selected_by", None):
            target_row["FB_Selected_By"] = night_result.selected_by
        if getattr(night_result, "name_consistency_flag", None) is not None:
            target_row["FB_Name_Consistency_Flag"] = night_result.name_consistency_flag
        if getattr(night_result, "review_reason", None):
            target_row["FB_Review_Reason"] = night_result.review_reason
        # Email provenance
        def _coerce(val: Any) -> str:
            try:
                return str(val or "").strip()
            except Exception:
                return ""
        if _coerce(target_row.get("Email")):
            if _coerce(target_row.get("Email_Source_URL")) == "":
                target_row["Email_Source_URL"] = (
                    page_url
                    or night_result.email_source_url
                    or night_result.facebook_url
                    or target_row.get("Facebook_URL", "")
                )
            if _coerce(target_row.get("Email_Source_Type")) == "":
                target_row["Email_Source_Type"] = "facebook_enrich"
            if _coerce(target_row.get("Email_Extract_Method")) == "":
                method = night_result.email_extract_method or "regex"
                target_row["Email_Extract_Method"] = method
        email_found = bool((night_result.email or "").strip() or emails)
        if emails:
            # Track FB-applied emails for downstream defensive stripping.
            normalized_emails = []
            seen = set()
            for e in emails:
                email_norm = (e or "").strip().lower()
                if email_norm and email_norm not in seen:
                    seen.add(email_norm)
                    normalized_emails.append(email_norm)
            if normalized_emails:
                target_row["__fb_emails_applied"] = ";".join(sorted(normalized_emails))
        if email_found:
            target_row[FB_ATTEMPT_STATE_COL] = "attempted_fb_found_email"
        # Record ownership of the FB URL to prevent cross-artist reuse of the same page.
        artist_norm = _normalize_name_like(target_row.get("Artist Name", "") or target_row.get("Artist", "") or "")
        fb_url_norm = _normalise_fb_url(page_url or night_result.facebook_url or "")
        if artist_norm and fb_url_norm:
            self._fb_url_owner.setdefault(fb_url_norm, artist_norm)
        status_locked = _fb_status_is_terminal(fb_status_raw) or _fb_status_is_terminal(fb_reason)
        missing_url_statuses = {"no_fb_url", "pass_a_skipped_no_fb_url", "pass_a_no_email_on_page"}
        fb_url_now = _coerce(target_row.get("Facebook_URL"))
        if not status_locked:
            if email_found and _coerce(target_row.get("Email")):
                target_row["FB_Status"] = fb_status_raw if fb_status_raw else "ok"
            elif fb_url_now and fb_status_raw in missing_url_statuses:
                target_row["FB_Status"] = "ok"
        if not target_row.get("FB_Status"):
            target_row["FB_Status"] = "ok"
        _log(self.logger, f"[Night FB] extracted email(s) {emails} from {page_url}")
        return target_row

    def _diagnose_explicit_fb_failure(self, url: str, allow_anon: bool) -> Tuple[List[str], Optional[str], str]:
        driver = None
        try:
            session = self._ensure_session()
        except FacebookDriverError:
            session = None
        except Exception:
            session = None

        if session:
            try:
                self._ensure_driver_alive(session)

                maybe_driver = None
                if hasattr(session, "ensure_logged_in"):
                    try:
                        maybe_driver = session.ensure_logged_in()
                    except Exception:
                        maybe_driver = None

                # Some session managers return None from ensure_logged_in(); fetch the actual driver handle.
                driver = (
                    maybe_driver
                    or getattr(session, "driver", None)
                    or getattr(session, "_driver", None)
                    or (session if hasattr(session, "get") else None)
                )
            except Exception:
                driver = None

        if (not driver) and allow_anon:
            try:
                driver = self._get_anon_driver()
            except Exception:
                driver = None

        if not driver:
            return [], "no_email_found", ""

        try:
            return _try_explicit_fb(driver, url, logger=self.logger)
        except Exception:
            return [], "no_email_found", ""

    def _enrich_row_unearthed_legacy(
        self,
        result: Dict[str, str],
        artist_name: str,
        fb_urls: List[str],
    ) -> Dict[str, str]:
        def _night_classify_unearthed_final_page(
            driver,
            fallback_url: str,
            status_hint: str,
            resolved_url_hint: str = "",
        ) -> Tuple[str, str, str]:
            resolved_url = _normalise_fb_url(
                (getattr(driver, "current_url", "") or resolved_url_hint or fallback_url)
            ) or _normalise_fb_url(resolved_url_hint or fallback_url) or (resolved_url_hint or fallback_url)
            try:
                page_html = getattr(driver, "page_source", "") or ""
            except Exception:
                page_html = ""

            warning_reason = _looks_like_fb_warning_or_block(page_html, resolved_url) or ""
            health = _night_fb_page_health_snapshot(
                resolved_url,
                page_html,
                warning_reason=warning_reason,
            )
            health_parts = [
                "[Night FB][Health]",
                f"url={health.get('url') or '<unknown>'}",
                f"captcha={1 if health.get('captcha') else 0}",
                f"checkpoint={1 if health.get('checkpoint') else 0}",
                f"login_wall={1 if health.get('login_wall') else 0}",
            ]
            if health.get("warning_reason"):
                health_parts.append(f"warning={health.get('warning_reason')}")
            health_parts.append("context=unearthed_legacy_final_page")
            _log(self.logger, " ".join(health_parts))

            auth_surface = str(health.get("auth_surface") or "").strip()
            status_norm = str(status_hint or "").strip().lower()
            if health.get("captcha") or auth_surface == "captcha":
                return "login_wall", "captcha", resolved_url
            if health.get("checkpoint") or auth_surface == "checkpoint":
                return "checkpoint", "checkpoint", resolved_url
            if health.get("login_wall"):
                return "login_wall", auth_surface or "login_wall", resolved_url
            if warning_reason:
                return "warning_interstitial", warning_reason, resolved_url
            if _is_fb_content_unavailable_page(page_html):
                _log(self.logger, f"[Night FB][PageUnavailable] {resolved_url}")
                return "content_unavailable", "content_unavailable", resolved_url
            if status_norm == "login_redirect":
                return "login_wall", "login_redirect", resolved_url
            if status_norm == "checkpoint":
                return "checkpoint", "checkpoint", resolved_url
            return status_hint, "", resolved_url

        def _map_unearthed_outcome(emails: List[str], status: str, reason: str = "") -> Tuple[str, str]:
            """
            Map legacy Unearthed scrape status to PASS A counters/log reasons.
            Outcome values must match PASS A summary buckets.
            """
            if emails:
                return "found_email", "explicit_url"
            status_norm = (status or "").lower()
            reason_norm = (reason or "").lower()
            if status_norm in ("login_redirect", "login_wall", "checkpoint", "warning_interstitial"):
                return "login_wall", reason_norm or ("login_redirect" if status_norm == "login_wall" else status_norm)
            if status_norm in ("error", "fetch_error"):
                return "fetch_error", "legacy_error"
            if status_norm == "content_unavailable":
                return "no_email_on_page", "content_unavailable"
            return "no_email_on_page", "legacy_no_email"

        # Always prefer explicit URLs first.
        if fb_urls:
            try:
                driver = self._get_unearthed_driver()
            except Exception as exc:
                result["FB_Status"] = "unearthed_driver_error"
                _log(self.logger, f"[Night FB][Unearthed] Could not start public FB driver: {exc}")
                return result
            last_status = "no_emails"
            for fb_url in fb_urls:
                self._pass_a_bump("attempted")
                emails, status, resolved_url = _scrape_fb_page_unearthed_legacy(driver, fb_url, logger=self.logger)
                last_status = status or "no_emails"
                outcome, base_reason = _map_unearthed_outcome(emails, status)
                reason = "share_url" if _is_fb_share_url_str(fb_url) else base_reason
                self._pass_a_bump(outcome)
                self._pass_a_log_row(artist_name, resolved_url or fb_url, "legacy_unearthed_anon", outcome, reason)
                if emails:
                    night_result = self._build_result(
                        emails,
                        str(result.get("Email_All", "") or ""),
                        resolved_url or fb_url,
                        artist_name,
                        email_extract_method="regex",
                    )
                    if night_result:
                        result = self._apply_night_fb_result(
                            result,
                            night_result,
                            emails,
                            resolved_url or fb_url,
                            fb_status_hint=result.get("FB_Status", ""),
                            fb_reason_hint=result.get("FB_Reason", ""),
                        )
                        result["FB_Status"] = "ok_unearthed_legacy"
                    else:
                        result["FB_Status"] = "unearthed_no_emails"
                    return result
            if not result.get("FB_Status"):
                result["FB_Status"] = f"unearthed_{last_status}"
            return result

        # If no FB URL present, fall back to a cautious blind search (previously we skipped).
        if not fb_urls:
            query = artist_name.strip()
            if not query:
                result["FB_Status"] = "unearthed_no_emails"
                _log(self.logger, "[Night FB] Unearthed row without FB URL -> FB_Status='unearthed_no_emails' (empty artist).")
                return result
            _log(self.logger, "[Unearthed Path] entering Unearthed no-URL FB discovery")
            page_url = self._search_for_page(query, location="", allow_anon=True) or ""
            if not page_url:
                result["FB_Status"] = "unearthed_no_candidates"
                _log(self.logger, "[Unearthed Path] discovery yielded no candidate")
                return result
            _log(self.logger, f"[Unearthed Path] discovery yielded candidate url='{page_url}'")
            try:
                driver = self._get_unearthed_driver()
            except Exception as exc:
                result["FB_Status"] = "unearthed_driver_error"
                _log(self.logger, f"[Night FB][Unearthed] Could not start public FB driver for blind search: {exc}")
                return result
            self._pass_a_bump("attempted")
            emails, status, resolved_url = _scrape_fb_page_unearthed_legacy(driver, page_url, logger=self.logger)
            status, status_reason, resolved_url = _night_classify_unearthed_final_page(
                driver,
                page_url,
                status,
                resolved_url,
            )
            outcome, base_reason = _map_unearthed_outcome(emails, status, status_reason)
            reason = "share_url" if _is_fb_share_url_str(page_url) else base_reason
            self._pass_a_bump(outcome)
            self._pass_a_log_row(artist_name, resolved_url or page_url, "legacy_unearthed_anon", outcome, reason)
            if emails:
                night_result = self._build_result(
                    emails,
                    str(result.get("Email_All", "") or ""),
                    resolved_url or page_url,
                    artist_name,
                    email_extract_method="regex",
                )
                if night_result:
                    result = self._apply_night_fb_result(
                        result,
                        night_result,
                        emails,
                        resolved_url or page_url,
                        fb_status_hint=result.get("FB_Status", ""),
                        fb_reason_hint=result.get("FB_Reason", ""),
                    )
                    result["FB_Status"] = "ok_unearthed_blind"
                    return result
            result["FB_Status"] = status or "unearthed_no_emails"
            if status_reason:
                result["FB_Reason"] = status_reason
            return result

    def enrich_row_with_facebook_night(self, row: Dict[str, str], row_index: Optional[int] = None) -> Dict[str, str]:
        """Night-Mode-only FB enrichment for a single row."""
        self._sync_search_disable_from_run_state()
        original_row = dict(row or {})
        result = dict(original_row)
        result["FB_Status"] = result.get("FB_Status", "") or ""
        self._checkpoint_warned_this_row = False
        self._page_budget_remaining = 2
        self._clear_last_fb_email_surface_state()

        def _clean_val(value: str) -> str:
            try:
                import pandas as _pd  # local import to avoid hard dep during tests
                if _pd.isna(value):
                    return ""
            except Exception:
                pass
            return str(value or "").strip()

        def _finish(payload: Dict[str, str], *, attempted: bool = True) -> Dict[str, str]:
            if payload is None:
                return payload
            if attempted:
                payload[FB_ATTEMPT_STATE_COL] = _classify_night_fb_attempt_state(
                    payload.get("FB_Status", ""),
                    payload.get(FB_ATTEMPT_STATE_COL, ""),
                )
            _write_night_fb_fail_evidence_debug(
                payload,
                row_index=row_index,
                collector_state={
                    "html": self._last_fb_surface_html,
                    "html_available": self._last_fb_surface_html_available,
                    "visible_text": self._last_fb_visible_text,
                    "visible_text_available": self._last_fb_visible_text_available,
                    "anchor_values": list(self._last_fb_live_anchor_values or []),
                    "anchor_values_available": self._last_fb_anchor_values_available,
                    "reveal_actions": list(self._last_fb_reveal_actions or []),
                    "reveal_actions_available": self._last_fb_reveal_actions_available,
                    "resolved_url": self._last_fb_surface_url,
                    "driver_kind": self._last_fb_surface_driver_kind,
                    "render_invalid_reason": self._last_fb_render_invalid_reason,
                },
                logger=self.logger,
            )
            return payload

        def _finalize_explicit_content_unavailable(payload: Dict[str, str]) -> Dict[str, str]:
            if payload is None or not explicit_content_unavailable_unrecovered:
                return payload
            if _clean_val(payload.get("Email", "")) or _clean_val(payload.get("Email_All", "")):
                return payload
            status_norm = _clean_val(payload.get("FB_Status", "")).lower()
            if status_norm not in {"", "no_candidates", "content_unavailable", "pass_a_no_email_on_page"}:
                return payload
            payload["FB_Status"] = "pass_a_content_unavailable"
            if not payload.get("FB_Reason"):
                payload["FB_Reason"] = "content_unavailable"
            return payload

        if self._skip_fb_due_to_session_failure or self._session_failed:
            result["FB_Status"] = result.get("FB_Status", "") or "driver_error"
            result["FB_Reason"] = self._session_failed_reason or self._skip_fb_due_to_session_failure_reason or "session_start_failed"
            return _finish(result)

        if self._skip_fb_due_to_display:
            result["FB_Status"] = result.get("FB_Status", "") or "skipped_no_display"
            result["FB_Reason"] = "no_display_env"
            return _finish(result)

        if self._skip_fb_due_to_warning:
            result["FB_Status"] = result.get("FB_Status", "") or "skipped_warning"
            result["FB_Reason"] = self._skip_fb_due_to_warning_reason or "warning_interstitial"
            return _finish(result)

        if self.protective_shutdown:
            result["FB_Status"] = result.get("FB_Status", "") or "skipped_checkpoint"
            result["FB_Reason"] = result.get("FB_Reason", "") or "checkpoint"
            self.fb_rows_skipped["checkpoint"] += 1
            return _finish(result)

        if self._skip_fb_due_to_checkpoint:
            self.fb_rows_skipped["checkpoint"] += 1
            return _finish(self._mark_row_checkpoint(result))

        artist_name = _clean_val(result.get("Artist Name", ""))
        is_unearthed = self._is_unearthed_source(result)
        explicit_fb_entrypoints = explicit_fb_entrypoint_urls_for_row(result)
        promoted_fb_url, _ = ensure_canonical_facebook_url(result, set_row=False)
        has_seeded_fb = bool(promoted_fb_url)
        unearthed_fb_first_active = bool(
            is_unearthed and (promoted_fb_url or explicit_fb_entrypoints)
        )
        skip_due_to_email, email_all_clean = _row_has_usable_email_for_fb_skip(result)
        if skip_due_to_email and not unearthed_fb_first_active:
            self.fb_rows_skipped["no_opportunity"] += 1
            _log(
                self.logger,
                f"[Night FB] Skipping row before FB scrape for artist='{artist_name or '<unknown>'}' because Email_All is already populated ({email_all_clean!r}).",
            )
            if not result.get("FB_Status"):
                result["FB_Status"] = "ok"
            return _finish(result, attempted=False)
        if skip_due_to_email and unearthed_fb_first_active:
            _log(
                self.logger,
                f"[Unearthed Path] forcing FB extraction despite existing email artist='{artist_name or '<unknown>'}'",
            )
        location = _clean_val(result.get("Location", ""))
        song_title = _clean_val(
            result.get("Song Title", "")
            or result.get("song_title", "")
            or result.get("Track Title", "")
            or result.get("track_title", "")
        )
        raw_fb_url = _clean_val(result.get("Facebook_URL", ""))
        if raw_fb_url and _is_invalid_fb_value(raw_fb_url) and self._debug_fb_url_flow:
            _log(self.logger, f"[Night FB] Skipping invalid facebook_url value: {raw_fb_url}")
        facebook_url = _normalise_fb_url(raw_fb_url) if _is_valid_fb_url_value(raw_fb_url) else ""
        fb_urls = _extract_fb_urls_for_night_mode(result)
        if facebook_url and facebook_url not in fb_urls:
            fb_urls.insert(0, facebook_url)
        fb_urls = _canonicalize_and_dedupe_explicit_fb_urls(
            fb_urls, logger=self.logger, debug=self._debug_fb_url_flow
        )
        explicit_intake = classify_explicit_fb_intake(result, accepted_urls=fb_urls)
        _log_explicit_fb_intake(self.logger, artist_name, explicit_intake)
        if not fb_urls:
            invalid_field, invalid_value = _find_invalid_direct_fb_row_value(result)
            if invalid_field and invalid_value:
                _log(
                    self.logger,
                    f"[Night FB] Skipping row - invalid facebook_url value: {invalid_value} artist='{artist_name or '<unknown>'}' field='{invalid_field}'",
                )
            elif explicit_intake.rejected_invalid:
                _log(
                    self.logger,
                    f"[Night FB] Skipping row - invalid facebook_url value: {explicit_intake.rejected_invalid[0]} artist='{artist_name or '<unknown>'}'",
                )

        if self._debug_fb_url_flow and self._debug_fb_url_flow_seen < self._debug_fb_url_flow_limit:
            self._debug_fb_url_flow_seen += 1
            if fb_urls:
                self._debug_fb_url_flow_with_urls += 1
            raw_social = _clean_val(result.get("Social Link", ""))
            raw_external = _clean_val(result.get("External Links", ""))
            raw_fb = _clean_val(result.get("Facebook_URL", ""))
            _log(
                self.logger,
                "[Night FB][URLFLOW] artist="
                f"\"{artist_name}\" src_dir=\"{_clean_val(result.get('Source Directory', ''))}\""
                f" src_job=\"{_clean_val(result.get('__source_job', ''))}\""
                f" fb_url_raw=\"{raw_fb}\" social_raw=\"{raw_social}\" external_raw=\"{raw_external}\""
                f" extracted={fb_urls}",
            )
            if self._debug_fb_url_flow_seen == self._debug_fb_url_flow_limit:
                self._emit_fb_url_flow_summary(prefix="[Night FB][URLFLOW] limit reached")

        try:
            if not self._maybe_recover_or_skip_on_checkpoint():
                self.fb_rows_skipped["checkpoint"] += 1
                return _finish(self._mark_row_checkpoint(result))
            page_url = ""
            emails: List[str] = []
            # Guard for downstream reject_reason usage during PASS B apply phase.
            reject_reason = ""
            if not fb_urls and self._search_disabled_due_to_checkpoint:
                result["FB_Status"] = "checkpoint_search_disabled"
                result["FB_Reason"] = "checkpoint"
                _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
                self.fb_rows_skipped["checkpoint"] += 1
                return _finish(result)
            # Unearthed rows with an explicit accepted FB URL should use the
            # same PASS A path as other explicit rows. Keep legacy fallback
            # only for true no-URL Unearthed rows.
            rejected_seeded_fb = bool(
                explicit_intake.outcome == "reject_invalid" and explicit_intake.rejected_invalid
            )
            if is_unearthed and not has_seeded_fb and not rejected_seeded_fb:
                _log(self.logger, "[Unearthed Path] no usable FB URL; skipping Night FB discovery")
                return _finish(result, attempted=False)
            if is_unearthed and not fb_urls:
                _log(self.logger, "[Unearthed Path] no usable FB URL; allowing bounded FB discovery")
                _log(self.logger, "[Night FB] Detected Unearthed row -> using legacy no-login FB scrape.")
                return _finish(self._enrich_row_unearthed_legacy(result, artist_name, fb_urls))

            allow_anon = self._should_allow_anonymous(result)
            # PASS A: explicit URL attempts (instrumentation only)
            outcome_rank = {"found_email": 0, "login_wall": 1, "timeout": 2, "fetch_error": 3, "no_email_on_page": 4}
            best_outcome = None
            best_reason = ""
            best_driver = ""
            best_page_url = ""
            explicit_content_unavailable_unrecovered = False

            if not fb_urls:
                if not result.get("FB_Status"):
                    result["FB_Status"] = "pass_a_skipped_no_fb_url"
                if not result.get("FB_Reason"):
                    result["FB_Reason"] = "skipped_no_fb_url"
                if self._debug_fb_url_flow:
                    self._debug_fb_url_flow_skipped += 1
                _log(self.logger, "[Night FB][PASS A] skipped (no explicit FB URL); proceeding to v2 search")
            else:
                authed_session_available = self._has_authenticated_session()
                pass_a_mode = "session" if authed_session_available else "legacy_anon_probe"
                if authed_session_available:
                    _log(self.logger, f"[Night FB] Using explicit FB URLs with authenticated session: {fb_urls}")
                else:
                    _log(self.logger, f"[Night FB] Falling back to legacy anon probe for explicit FB URLs: {fb_urls}")
                _log(self.logger, f"[Night FB] Using explicit FB URLs: {fb_urls}")
                allow_anon_for_explicit = False if authed_session_available else allow_anon
                for direct_url in fb_urls:
                    scrape_target_url = self._resolve_pass_a_explicit_scrape_url(
                        direct_url,
                        authed_session_available=authed_session_available,
                    )
                    normalized_scrape_target_url = _normalise_fb_url(scrape_target_url)
                    driver_kind = "session"
                    outcome_for_log = "fetch_error"
                    reason_for_log = ""
                    explicit_candidate_context = {
                        "url": normalized_scrape_target_url,
                        "explicit_accepted_url": True,
                        "accepted_page_fast_loader_safe": bool(
                            allow_anon
                            and normalized_scrape_target_url
                            and normalized_scrape_target_url == scrape_target_url
                            and not _is_allowed_fb_share_entrypoint_url(scrape_target_url)
                        ),
                    }
                    self._pass_a_bump("attempted")
                    self._last_explicit_guard_reason = ""
                    try:
                        candidate = self._scrape_single_fb_candidate(
                            scrape_target_url,
                            result,
                            artist_name,
                            allow_anon=allow_anon_for_explicit,
                            candidate_context=explicit_candidate_context,
                        )
                    except Exception as exc:
                        candidate = None
                        driver_kind = "unknown"
                        outcome_for_log = "fetch_error"
                        reason_for_log = f"session_exception:{exc.__class__.__name__}"
                    night_result, emails, driver_kind, candidate_outcome = _unpack_fb_candidate(candidate)
                    if night_result is not None or candidate_outcome in ("login_wall", "content_unavailable", "timeout"):
                        if night_result is None:
                            outcome_for_log = candidate_outcome
                            if candidate_outcome == "login_wall":
                                reason_for_log = "session_login_wall" if not driver_kind.startswith("anon") else "anon_login_wall"
                                current_rank = outcome_rank.get("login_wall", 99)
                                if best_outcome is None or current_rank < outcome_rank.get(best_outcome, 99):
                                    best_outcome = "login_wall"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(scrape_target_url)
                            elif candidate_outcome == "content_unavailable":
                                reason_for_log = "content_unavailable"
                                if best_outcome is None:
                                    best_outcome = "content_unavailable"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(scrape_target_url)
                            elif candidate_outcome == "timeout":
                                reason_for_log = "timeout"
                                self._pass_a_bump("fetch_error")
                                current_rank = outcome_rank.get("timeout", 99)
                                if best_outcome is None or current_rank < outcome_rank.get(best_outcome, 99):
                                    best_outcome = "timeout"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(scrape_target_url)
                            else:
                                reason_for_log = f"{driver_kind}_exception:unknown"
                                self._pass_a_bump("fetch_error")
                                if best_outcome is None:
                                    best_outcome = "fetch_error"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(scrape_target_url)
                        else:
                            outcome_for_log = candidate_outcome
                            if candidate_outcome == "found_email":
                                reason_for_log = "explicit_url"
                            elif candidate_outcome == "no_email_on_page":
                                reason_for_log = "session_fetch_ok_no_email" if not driver_kind.startswith("anon") else "anon_fetch_ok_no_email"
                            elif candidate_outcome == "login_wall":
                                reason_for_log = "session_login_wall" if not driver_kind.startswith("anon") else "anon_login_wall"
                            elif candidate_outcome == "timeout":
                                reason_for_log = "timeout"
                            else:
                                reason_for_log = f"{driver_kind}_exception:unknown"
                            if emails:
                                page_url = night_result.facebook_url or _normalise_fb_url(direct_url)
                                result = self._apply_night_fb_result(
                                    result,
                                    night_result,
                                    emails,
                                    page_url,
                                    fb_status_hint=result.get("FB_Status", ""),
                                    fb_reason_hint=result.get("FB_Reason", ""),
                                )
                                result["FB_Status"] = "pass_a_found_email"
                                result["FB_Reason"] = "explicit_url"
                                self._pass_a_bump("found_email")
                                self._pass_a_log_row(artist_name, page_url, driver_kind, "found_email", reason_for_log, mode=pass_a_mode)
                                return _finish(result)
                            else:
                                page_url = night_result.facebook_url or _normalise_fb_url(direct_url)
                                current_rank = outcome_rank.get(candidate_outcome, 99)
                                if best_outcome is None or current_rank < outcome_rank.get(best_outcome, 99):
                                    best_outcome = candidate_outcome
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = page_url
                    else:
                        if self._last_explicit_guard_reason:
                            driver_kind = "pre_scrape_guard"
                            outcome_for_log = "guard_reject"
                            reason_for_log = self._last_explicit_guard_reason
                        elif not reason_for_log:
                            reason_for_log = "session_exception:unknown" if driver_kind == "session" else f"{driver_kind}_exception:unknown"
                        self._pass_a_bump("fetch_error")
                        if best_outcome is None:
                            best_outcome = "fetch_error"
                            best_reason = reason_for_log
                            best_driver = driver_kind
                            best_page_url = _normalise_fb_url(scrape_target_url)
                    self._pass_a_log_row(artist_name, scrape_target_url, driver_kind, outcome_for_log, reason_for_log, mode=pass_a_mode)

                if best_outcome:
                    if best_outcome == "login_wall":
                        result["FB_Status"] = "pass_a_login_wall"
                        result["FB_Reason"] = best_reason or ("anon_login_wall" if best_driver.startswith("anon") else "session_login_wall")
                        self._register_login_wall()
                        self._pass_a_bump("login_wall")
                    elif best_outcome == "timeout":
                        result["FB_Status"] = "pass_a_timeout"
                        result["FB_Reason"] = best_reason or "timeout"
                        self._pass_a_bump("fetch_error")
                    elif best_outcome == "fetch_error":
                        result["FB_Status"] = "pass_a_fetch_error"
                        result["FB_Reason"] = best_reason or ("anon_exception:unknown" if best_driver.startswith("anon") else "session_exception:unknown")
                        self._pass_a_bump("fetch_error")
                    elif best_outcome == "content_unavailable":
                        explicit_content_unavailable_unrecovered = True
                    else:
                        result["FB_Status"] = "pass_a_no_email_on_page"
                        result["FB_Reason"] = best_reason or ("anon_fetch_ok_no_email" if best_driver.startswith("anon") else "session_fetch_ok_no_email")
                        self._pass_a_bump("no_email_on_page")
                    # Only a retained unavailable explicit outcome should allow the
                    # row's single bounded PASS B discovery fallback.
                    page_url = "" if best_outcome == "content_unavailable" else (best_page_url or page_url)
                else:
                    # Diagnostics fallback for first URL
                    reason_code: Optional[str] = None
                    diag_emails: List[str] = []
                    probe_url = fb_urls[0] if fb_urls else ""
                    if probe_url:
                        diag_emails, reason_code, diag_method = self._diagnose_explicit_fb_failure(probe_url, allow_anon=allow_anon)
                    if diag_emails:
                        page_url = _normalise_fb_url(probe_url)
                        night_result = self._build_result(
                            diag_emails,
                            str(result.get("Email_All", "") or ""),
                            page_url,
                            artist_name,
                            email_extract_method=diag_method or "regex",
                        )
                        if night_result:
                            result = self._apply_night_fb_result(
                                result,
                                night_result,
                                diag_emails,
                                page_url,
                                fb_status_hint=result.get("FB_Status", ""),
                                fb_reason_hint=result.get("FB_Reason", ""),
                            )
                            result["FB_Status"] = "pass_a_found_email"
                            result["FB_Reason"] = "explicit_url"
                            self._pass_a_bump("found_email")
                            self._pass_a_log_row(
                                artist_name,
                                page_url,
                                "anon" if allow_anon else "session",
                                "found_email",
                                "explicit_url",
                                mode=pass_a_mode,
                            )
                            return _finish(result)
                    if reason_code:
                        if reason_code == "timeout":
                            result["FB_Status"] = "pass_a_timeout"
                            result["FB_Reason"] = "timeout"
                            self._pass_a_bump("fetch_error")
                            _log(self.logger, f"[Night FB] Explicit FB URL timed out; falling back to search.")
                        else:
                            result["FB_Status"] = "pass_a_login_wall" if reason_code.startswith("redirect") else "pass_a_no_email_on_page"
                            result["FB_Reason"] = "anon_login_wall" if "login" in reason_code else "anon_fetch_ok_no_email"
                            self._pass_a_bump("login_wall" if "login" in reason_code else "no_email_on_page")
                            _log(self.logger, f"[Night FB] Explicit FB URL had no emails (reason={reason_code}); falling back to search.")
                    elif not result.get("FB_Status"):
                        _log(self.logger, "[Night FB] Explicit FB URLs produced no results; falling back to search.")
                        if not result.get("FB_Status"):
                            result["FB_Status"] = "pass_a_no_email_on_page"
                            result["FB_Reason"] = "session_fetch_ok_no_email"
                            self._pass_a_bump("no_email_on_page")
                    _log(self.logger, "[Night FB] Falling back to PASS B (search) after PASS A.")

            if not page_url:
                session = self._ensure_session()
                if (not session) and not allow_anon:
                    return _finish(_finalize_explicit_content_unavailable(result))
            if not page_url:
                page_url = self._search_for_page(
                    artist_name,
                    location,
                    allow_anon=allow_anon,
                    song_title=song_title,
                    row=result,
                ) or ""
                if self._search_disabled_due_to_checkpoint:
                    if not result.get("FB_Status"):
                        result["FB_Status"] = "checkpoint_search_disabled"
                        result["FB_Reason"] = "checkpoint"
                    _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
                    return _finish(result)
                if self._checkpoint_limited_active and not page_url:
                    if not result.get("FB_Status") or result.get("FB_Status") == "no_candidates":
                        result["FB_Status"] = "checkpoint_limited"
                    if not result.get("FB_Reason"):
                        result["FB_Reason"] = "checkpoint"
                    return _finish(result)
                if page_url:
                    candidate = self._scrape_single_fb_candidate(
                        page_url,
                        result,
                        artist_name,
                        allow_anon=allow_anon,
                        candidate_context=self._last_selected_candidate_context,
                    )
                    night_result, emails, _, candidate_outcome = _unpack_fb_candidate(candidate)
                    if night_result:
                        page_url = night_result.facebook_url or page_url
                        # Defensive: ensure reject_reason defined before use in fb_reason_hint.
                        reject_reason = reject_reason or result.get("FB_Reason", "")
                        result = self._apply_night_fb_result(
                            result,
                            night_result,
                            emails,
                            page_url,
                            fb_status_hint=result.get("FB_Status", ""),
                            fb_reason_hint=reject_reason or result.get("FB_Reason", ""),
                        )
                        if self._checkpoint_limited_active and not result.get("FB_Reason"):
                            result["FB_Reason"] = "checkpoint"
                        return _finish(result)
                    if candidate_outcome == "content_unavailable":
                        fallback_candidates = self._last_search_candidates[1:] if len(self._last_search_candidates) > 1 else []
                        for idx, ctx in enumerate(fallback_candidates, start=2):
                            alt_url = ctx.get("url")
                            if not alt_url:
                                continue
                            _log(self.logger, f"[Night FB][PageUnavailable] primary candidate unavailable; trying fallback {idx} -> {alt_url}")
                            alt_candidate = self._scrape_single_fb_candidate(
                                alt_url,
                                result,
                                artist_name,
                                allow_anon=allow_anon,
                                candidate_context=ctx,
                            )
                            alt_result, alt_emails, _, alt_outcome = _unpack_fb_candidate(alt_candidate)
                            if alt_result:
                                page_url = alt_result.facebook_url or alt_url
                                result = self._apply_night_fb_result(
                                    result,
                                    alt_result,
                                    alt_emails,
                                    page_url,
                                    fb_status_hint=result.get("FB_Status", ""),
                                    fb_reason_hint=result.get("FB_Reason", ""),
                                )
                                if self._checkpoint_limited_active and not result.get("FB_Reason"):
                                    result["FB_Reason"] = "checkpoint"
                                return _finish(result)
                            if alt_outcome != "content_unavailable":
                                break
                        if not result.get("FB_Status"):
                            result["FB_Status"] = "content_unavailable"
                            result["FB_Reason"] = "content_unavailable"
                            _log(self.logger, f"[Night FB][PageUnavailable] No usable FB candidates for '{artist_name}' after content-unavailable fallbacks.")
                            return _finish(result)
                if not result.get("FB_Status"):
                    result["FB_Status"] = "checkpoint_limited" if self._checkpoint_limited_active else "no_candidates"
                if self._last_search_reject_reason:
                    result["FB_Reason"] = self._last_search_reject_reason
                    if str(result.get("Needs_Review", "")).strip() == "":
                        result["Needs_Review"] = "FALSE"
                    _log(
                        self.logger,
                        f"[Night FB] No safe FB candidate for '{artist_name}' (best_score={self._last_search_reject_score if self._last_search_reject_score is not None else '<unknown>'}) reason={self._last_search_reject_reason}",
                    )
                if self._checkpoint_limited_active and not result.get("FB_Reason"):
                    result["FB_Reason"] = "checkpoint"
                _log(self.logger, f"[Night FB] No usable FB candidates for '{artist_name}', marking FB_Status='{result.get('FB_Status')}'.")
                return _finish(_finalize_explicit_content_unavailable(result))
            night_result = self._build_result(
                emails,
                str(result.get("Email_All", "") or ""),
                page_url,
                artist_name,
                email_extract_method="regex",
            )
            if night_result:
                result = self._apply_night_fb_result(
                    result,
                    night_result,
                    emails,
                    page_url,
                    fb_status_hint=result.get("FB_Status", ""),
                    fb_reason_hint=result.get("FB_Reason", ""),
                )
            else:
                # Page reached but no emails extracted.
                if _is_fb_login_or_security_url(page_url):
                    result["FB_Status"] = "login_redirect"
                    result["Facebook_URL"] = ""
                    self._register_login_wall()
                    _log(self.logger, f"[Night FB] Detected login redirect for '{artist_name}' -> {page_url}, marking FB_Status='login_redirect'.")
                else:
                    if not result.get("FB_Status"):
                        result["FB_Status"] = "ok"
            return _finish(result)
        except FacebookDriverError as exc:
            exc_msg = str(exc) or ""
            if exc_msg.startswith("fb_circuit_breaker:"):
                reason = exc_msg.split("fb_circuit_breaker:", 1)[-1] or "warning_interstitial"
                self._skip_fb_due_to_warning = True
                self._skip_fb_due_to_warning_reason = reason
                result["FB_Status"] = "skipped_warning"
                result["FB_Reason"] = reason
                _log(self.logger, f"[Night FB] Circuit breaker tripped ({reason}); skipping FB for remainder of run.")
                return _finish(result)
            result["FB_Status"] = "driver_error"
            _log(self.logger, f"[Night FB] Driver error while enriching '{result.get('Artist Name', '') or '<unknown>'}': {exc}")
            return _finish(result)
        except Exception as exc:  # pragma: no cover - defensive
            prefix = f"[FB Night] Night FB enrich failed at row {row_index}: {exc}" if row_index is not None else f"[FB Night] Night FB enrich failed: {exc}"
            _log(self.logger, prefix)
            return _finish(original_row)
