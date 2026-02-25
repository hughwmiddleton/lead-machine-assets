"""Night-Mode-only Facebook enrichment helpers.

This module isolates Night Mode tweaks so daytime paths stay unchanged.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
import shutil
import unicodedata
from pathlib import Path
import sys
import atexit
import weakref
import subprocess
import tempfile
import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
import logging

from fb_email_override import should_accept_email_override

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException

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

EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_FB_SPLIT_PATTERN = re.compile(r"[,\s;|]+")
FB_CLUE_FIELDS = [
    "Social Link",
    "External Links",
    "Spotify_Website_URL",
    "Spotify Website URL",
    "Facebook_URL",
    "Facebook URL",
]

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
    "profile_penalty_music": -5,
    "service_only": -45,
    "service_mixed": -15,
}

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


def _collect_container_candidates_v2(container, apply_role_link_prefilter: bool = True) -> Dict[str, Any]:
    """
    Expanded candidate extraction for Night FB DOM Gate v2.
    Returns dict with hrefs + counts to keep the caller lightweight.
    """
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
    for el in anchor_elements + role_anchor_elements:
        try:
            anchor_ids.add(el.id)
        except Exception:
            anchor_ids.add(str(id(el)))

    data["count_a_href"] = len(anchor_elements)
    data["count_a_role_link_href"] = len(role_anchor_elements)
    data["count_role_link"] = len(role_link_elements)
    data["anchors_in_scope"] = len(anchor_ids)
    data["role_links_in_scope"] = len(role_link_elements)

    seen_urls: Set[str] = set()
    hrefs: List[str] = []

    for el in anchor_elements:
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

    for el in role_link_elements:
        for href in _extract_role_link_candidates(el, apply_prefilter=apply_role_link_prefilter):
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            hrefs.append(href)

    data["hrefs"] = hrefs
    try:
        data["usable_count"] = sum(1 for href in hrefs if _is_candidate_usable(href))
    except Exception:
        data["usable_count"] = 0
    data["links_in_scope_total"] = data["anchors_in_scope"] + data["role_links_in_scope"]
    return data


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


def _extract_fb_urls_for_night_mode(row):
    fields = [
        "Social Link",
        "External Links",
        "Facebook_URL",
        "Facebook URL",
        "Spotify_Website_URL",
        "Spotify Website URL",
    ]
    urls = []
    for f in fields:
        raw = (row.get(f) or "").strip()
        if not raw:
            continue
        parts = re.split(r"[,\s;|]+", raw)
        for p in parts:
            if "facebook.com" in p.lower() or "fb.me" in p.lower():
                urls.append(p)

    clean = []
    for url in urls:
        u = url.strip()
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http"):
            u = "https://" + u
        # filter login redirects
        bad = ("/r.php", "/login", "/share.php", "/l.php", "/dialog/")
        path = urllib.parse.urlparse(u).path.lower()
        if any(path.startswith(b) for b in bad):
            continue
        clean.append(u)

    return list(dict.fromkeys(clean))


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
    if not url:
        return ""
    cleaned = url.strip()
    if cleaned.startswith("/"):
        cleaned = "https://www.facebook.com" + cleaned
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

    if path.lower() == "/profile.php":
        qs = urllib.parse.parse_qs(query, keep_blank_values=False)
        ids = qs.get("id", [])
        if not ids:
            return ""
        profile_id = (ids[0] or "").strip()
        if not profile_id.isdigit():
            return ""
        return urllib.parse.urlunparse((scheme, host, "/profile.php", "", f"id={profile_id}", ""))

    path = path.rstrip("/")
    if not path:
        return ""

    return urllib.parse.urlunparse((scheme, host, path, "", "", ""))


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


_night_fb_profile_dir_logged: bool = False


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


def _session_looks_healthy(driver) -> Tuple[bool, str]:
    """
    Quick FB session health probe to catch login/verification walls.
    Returns (healthy, reason_code).
    """
    try:
        driver.get("https://www.facebook.com/")
        time.sleep(0.6)
        current_url = (getattr(driver, "current_url", "") or "").lower()
        page_source = (getattr(driver, "page_source", "") or "").lower()
        time.sleep(0.4)
        # second sampling to reduce transient blanks
        current_url = (getattr(driver, "current_url", "") or "").lower() or current_url
        page_source = (getattr(driver, "page_source", "") or "").lower() or page_source
    except Exception:
        return False, "exception"

    bad_url_tokens = (
        ("login", "redirect_login"),
        ("checkpoint", "checkpoint"),
        ("consent", "consent"),
        ("recover", "recover"),
        ("two_factor", "two_factor"),
        ("two-factor", "two_factor"),
        ("mfa", "two_factor"),
    )
    for token, reason in bad_url_tokens:
        if token in current_url:
            return False, reason

    bad_html_tokens = (
        ("checkpoint", "checkpoint"),
        ("consent", "consent"),
        ("log in", "redirect_login"),
        ("two-factor", "two_factor"),
        ("security check", "checkpoint"),
        ("captcha", "captcha"),
    )
    for token, reason in bad_html_tokens:
        if token in page_source:
            return False, reason

    return True, ""


def _is_session_death_exc(exc: BaseException) -> bool:
    """Lightweight detector for dead Selenium sessions/windows."""
    try:
        msg = (str(exc) or "").lower()
    except Exception:
        return False
    death_tokens = ("no such window", "web view not found", "invalid session id")
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

    try:
        feed_selector = 'div[role="main"] div[role="feed"]'
        search_selector = 'div[aria-label="Search results"]'
        main_selector = 'div[role="main"]'

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

        def _refresh_counts():
            feed_containers = _find_all(driver, feed_selector)
            search_containers = _find_all(driver, search_selector)
            feed_container = feed_containers[0] if feed_containers else None
            search_container = search_containers[0] if search_containers else None
            feed_data = _collect_container_candidates_v2(feed_container, apply_role_link_prefilter=role_link_prefilter)
            search_data = _collect_container_candidates_v2(search_container, apply_role_link_prefilter=role_link_prefilter)

            return {
                "feed_container": feed_container,
                "search_container": search_container,
                "feed_hrefs": feed_data.get("hrefs", []),
                "search_hrefs": search_data.get("hrefs", []),
                "usable_feed": feed_data.get("usable_count", 0),
                "usable_search": search_data.get("usable_count", 0),
                "anchors_in_scope_feed": feed_data.get("anchors_in_scope", 0),
                "anchors_in_scope_search": search_data.get("anchors_in_scope", 0),
                "links_in_scope_total_feed": feed_data.get("links_in_scope_total", 0),
                "links_in_scope_total_search": search_data.get("links_in_scope_total", 0),
                "containers_found_feed": len(feed_containers),
                "containers_found_search": len(search_containers),
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

        chosen_selector = "NONE"
        chosen_container = None
        chosen_data: Dict[str, Any] = {}

        feed_data = _collect_container_candidates_v2(counts["feed_container"], apply_role_link_prefilter=role_link_prefilter)
        search_data = _collect_container_candidates_v2(counts["search_container"], apply_role_link_prefilter=role_link_prefilter)
        main_container = None
        main_data: Dict[str, Any] = {}

        if feed_data.get("usable_count", 0) > 0 or feed_data.get("hrefs"):
            chosen_selector = feed_selector
            chosen_container = counts["feed_container"]
            chosen_data = feed_data
        elif search_data.get("usable_count", 0) > 0 or search_data.get("hrefs"):
            chosen_selector = search_selector
            chosen_container = counts["search_container"]
            chosen_data = search_data
        else:
            main_container = _find_first(driver, main_selector)
            main_data = _collect_container_candidates_v2(main_container, apply_role_link_prefilter=role_link_prefilter)
            if main_data.get("hrefs"):
                chosen_selector = main_selector
                chosen_container = main_container
                chosen_data = main_data

        # If initial pick is sparse, try alternates: search results, main, feed.
        if (chosen_data.get("hrefs") or []) and len(chosen_data.get("hrefs", [])) < low_yield_threshold:
            if main_container is None:
                main_container = _find_first(driver, main_selector)
                if not main_data:
                    main_data = _collect_container_candidates_v2(main_container, apply_role_link_prefilter=role_link_prefilter)
            fallback_order = [
                (search_selector, counts["search_container"], search_data),
                (main_selector, main_container, main_data),
                (feed_selector, counts["feed_container"], feed_data),
            ]
            for selector_name, container_obj, data_obj in fallback_order:
                if data_obj and len(data_obj.get("hrefs", [])) >= low_yield_threshold:
                    chosen_selector = selector_name
                    chosen_container = container_obj
                    chosen_data = data_obj
                    break
            # If still low, pick the richest container available.
            if len(chosen_data.get("hrefs", [])) < low_yield_threshold:
                best_entry = None
                best_len = -1
                for selector_name, container_obj, data_obj in fallback_order:
                    href_len = len(data_obj.get("hrefs", [])) if data_obj else 0
                    if href_len > best_len:
                        best_len = href_len
                        best_entry = (selector_name, container_obj, data_obj)
                if best_entry is not None and best_entry[2] is not None:
                    chosen_selector, chosen_container, chosen_data = best_entry

        if chosen_container is None:
            total_anchors = len(_find_all(driver, "a"))
            search_len, search_preview = _container_html_preview(counts["search_container"])
            feed_len, feed_preview = _container_html_preview(counts["feed_container"])
            page_url = _safe_current_url(driver)
            preview = search_preview or feed_preview
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
            if overlay_present and counts["anchors_in_scope_feed"] == 0 and counts["anchors_in_scope_search"] == 0:
                logged_already = False
                if diagnostics is not None:
                    logged_already = bool(diagnostics.get("overlay_soft_block_logged"))
                    diagnostics["overlay_soft_block"] = True
                    diagnostics["overlay_soft_block_logged"] = True
                if not logged_already:
                    _log(logger, "[Night FB] Overlay/zero-anchors detected; treating as soft block and slowing down.")
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
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    driver = _start_chromedriver_with_retry(
        chrome_options,
        logger=logger,
        profile_dir=profile_dir,
        enable_temp_profile_fallback=True,
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
        mode_label = "headless" if self.headless else "headed"
        _log(self.logger, f"[Night FB] Auth check after FB homepage ({mode_label}): authed={authed}")

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

        if self.headless:
            self.last_health_ok = False
            self.last_health_reason = "unauthenticated"
            raise FacebookDriverError("Headless session unauthenticated (no c_user cookie present).")

        _log(self.logger, "[Night FB] Awaiting manual login to establish session (headed mode)...")
        self._wait_for_manual_login(self.driver)
        self.last_health_ok = True
        self.last_health_reason = ""
        return self.driver

    def navigate(self, url: str, logger: LoggerFn = None):
        log_target = logger if logger is not None else self.logger
        driver = self.ensure_logged_in()
        try:
            driver.get(url)
            return driver
        except Exception as exc:
            if _is_session_death_exc(exc):
                _log(log_target, f"[Night FB] Driver session died during navigate; refreshing and retrying url={url!r} error={exc}")
                try:
                    driver = self.refresh_session()
                    driver.get(url)
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
        _log(
            logger,
            "[Night FB][preflight] "
            f"chrome_binary={chrome_bin or '<auto>'} "
            f"chrome_version={chrome_version or '<unknown>'} "
            f"chromedriver={driver_display} "
            f"chromedriver_version={driver_version or '<unknown>'} "
            f"profile_dir={profile or '<none>'} "
            f"profile_exists={profile_exists} profile_locks={profile_locks} "
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


def _extract_emails_from_html(html: str) -> List[str]:
    emails: List[str] = []
    if not html:
        return emails
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select('a[href^="mailto:"]'):
        href = anchor.get("href") or ""
        addr = href.split("mailto:", 1)[-1].split("?", 1)[0]
        if addr:
            emails.append(addr)
    text_blob = soup.get_text(" ", strip=True) if soup else ""
    if text_blob:
        emails.extend(match.group(0) for match in EMAIL_REGEX.finditer(text_blob))
    seen = set()
    unique: List[str] = []
    for email in emails:
        cleaned = email.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def _choose_primary_email(emails: Sequence[str], artist_slug: str) -> Optional[str]:
    if not emails:
        return None
    slug = _slugify(artist_slug)
    if slug:
        for email in emails:
            if slug in _slugify(email):
                return email
    return emails[0]


def _merge_email_all(existing: str, new_emails: Sequence[str]) -> str:
    merged: List[str] = []
    seen = set()
    for value in list(_split_multi(existing)) + list(new_emails):
        cleaned = (value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            merged.append(cleaned)
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
        assert not _fb_is_candidate_url_allowed("https://www.facebook.com/someband?__tn__=%2Cd")
        assert not _fb_is_candidate_url_allowed("https://www.facebook.com/groups/foo")
        assert not _fb_is_candidate_url_allowed("https://www.facebook.com/someband/about")
        assert not _fb_is_candidate_url_allowed("https://www.facebook.com/profile.php?id=12&foo=1")
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


def _candidate_tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


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
    if jaccard >= 0.75 or artist_slug and artist_slug in name_slug or name_slug and name_slug in artist_slug:
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


def _try_explicit_fb(driver, url: str, logger: LoggerFn = None) -> Tuple[List[str], Optional[str]]:
    """
    Returns: (emails, reason)
    reason is None if emails found, else one of:
      redirect_login, checkpoint_or_consent, not_found, no_email_found
    """
    if not driver or not url:
        return [], "no_email_found"

    try:
        driver.get(url)
    except Exception:
        try:
            _log(logger, f"[Night FB] Explicit FB navigation failed for {url}")
        except Exception:
            pass
        return [], "no_email_found"

    try:
        final_url = (getattr(driver, "current_url", "") or "").lower()
    except Exception:
        final_url = (url or "").lower()

    try:
        page_source_raw = getattr(driver, "page_source", "") or ""
    except Exception:
        page_source_raw = ""
    page_source = page_source_raw.lower()

    if ("login" in final_url) or ("/login.php" in final_url) or ("device-based" in page_source) or ("log in" in page_source and "facebook" in page_source):
        return [], "redirect_login"

    if any(tok in final_url for tok in ("checkpoint", "consent")) or any(tok in page_source for tok in ("checkpoint", "consent", "cookie", "privacy")):
        return [], "checkpoint_or_consent"

    not_found_phrases = (
        "page isn\u2019t available",
        "page isn't available",
        "content isn't available",
        "not available right now",
    )
    if any(phrase in page_source for phrase in not_found_phrases):
        return [], "not_found"

    emails = _extract_emails_from_html(page_source_raw)
    if emails:
        return emails, None

    return [], "no_email_found"


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
                f"https://www.facebook.com/profile.php?id={fb_id}&sk=about",
            ]
        return []
    variants = [
        f"{normalized}/about_contact_and_basic_info",
        f"{normalized}/about_details",
        f"{normalized}/about",
    ]
    return variants


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
    about_attempted: str = "no"
    about_result: str = ""


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
    ) -> None:
        self.legacy = legacy_module
        self.username = username
        self.password = password
        self.logger = self._coerce_logger(logger)
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
        self._pass_a_counts = {
            "attempted": 0,
            "found_email": 0,
            "no_email_on_page": 0,
            "login_wall": 0,
            "fetch_error": 0,
            "skipped_no_fb_url": 0,
        }
        # Per-run reject cache to avoid re-selecting clearly bad pages (name mismatch, non-music, etc.).
        self._fb_reject_cache: Dict[str, Set[str]] = {}
        self._fb_reject_cache_global: Set[str] = set()
        self._session_state_logged = False
        # Slow mode / resilience
        self.slow_mode_active: bool = False
        self.slow_mode_multiplier: float = 1.0
        self.slow_mode_reason: str = ""
        self.session_unhealthy_count: int = 0
        self.checkpoint_events: int = 0
        self.login_wall_events: int = 0
        self.protective_shutdown: bool = False

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

    def _log_session_state_once(self, session) -> None:
        if self._session_state_logged:
            return
        if session is None:
            return
        authed, unhealthy, reason = self._session_state_snapshot(session)
        v2_enabled = _bool_env("FB_SEARCH_HARVEST_V2", default=False)
        _log(
            self.logger,
            f"[Night FB][session_state] authed={1 if authed else 0} unhealthy={1 if unhealthy else 0} reason={reason or ''} v2={1 if v2_enabled else 0}",
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
        self, artist: str, ranked_items: List[Dict[str, Any]]
    ) -> Tuple[Optional["facebook_enrich.FbCandidate"], str]:
        """
        Pick the first viable ranked candidate, skipping any cached rejects and
        preferring name-matching pages over clear mismatches.
        """
        fallback_mismatch = None
        for item in ranked_items or []:
            cand = item.get("candidate")
            raw_url = _candidate_url(cand)
            norm_url = _normalise_fb_url(raw_url)
            if norm_url and self._fb_is_rejected(artist, norm_url):
                _log(self.logger, f"[Night FB] Skipping cached rejected FB candidate url='{norm_url}' for '{artist}'.")
                continue
            match_level = item.get("features", {}).get("match_level") or "none"
            if match_level != "mismatch":
                return cand, "ranked_sort"
            if fallback_mismatch is None:
                fallback_mismatch = cand
        if fallback_mismatch:
            return fallback_mismatch, "mismatch_fallback"
        return None, "no_viable_candidate"

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
            _log(self.logger, "[Night FB] Protective shutdown triggered by login wall; skipping FB for remainder of run.")

    def _ensure_session(self):
        if self._session_failed:
            raise FacebookDriverError(self._session_failed_reason or "Facebook session previously failed.")
        if self.use_shared_session:
            raise FacebookDriverError("Legacy/shared Facebook session is not allowed for Night Mode.")

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

        if not self.username or not self.password:
            _log(self.logger, "[Night FB] Missing FB credentials; running without live session.")
            return None
        try:
            driver_factory = lambda: _create_fb_driver_night_mode(self.headless, logger=self.logger)
            self.session = NightPersistentFacebookSession(driver_factory, headless=self.headless, logger=self.logger)
            self._owns_session = True
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

    def _pass_a_log_row(self, artist: str, url: str, driver_kind: str, outcome: str, reason: str) -> None:
        safe_artist = artist or "<unknown>"
        safe_url = url or "<none>"
        safe_driver = driver_kind or "unknown"
        safe_outcome = outcome or "unknown"
        safe_reason = reason or ""
        _log(
            self.logger,
            f'[Night FB][PASS A] artist="{safe_artist}" url="{safe_url}" mode="legacy_anon_probe" driver="{safe_driver}" outcome="{safe_outcome}" reason="{safe_reason}"',
        )

    def get_pass_a_counts(self) -> Dict[str, int]:
        return dict(self._pass_a_counts)

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

    def _fetch_html_with_url(self, url: str, goto_about: bool = True) -> Tuple[Optional[str], Optional[str]]:
        session = self._ensure_session()
        if not session:
            return None, None
        self._ensure_driver_alive(session)
        def _navigate_once() -> Tuple[Optional[str], Optional[str]]:
            driver = session.navigate(url)
            if goto_about:
                goto_about_fn = getattr(self.legacy, "_goto_facebook_about", None)
                if callable(goto_about_fn):
                    try:
                        goto_about_fn(driver, url, timeout=5.0)
                    except Exception:
                        pass
            time.sleep(1.0)
            current_url = getattr(driver, "current_url", None) or url
            return driver.page_source, current_url

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
        if is_fb_login_redirect(current_url) or _is_fb_login_or_security_url(current_url):
            _log(self.logger, f"[Night FB] Ignoring login/redirect page: {current_url}")
            try:
                self._refresh_driver(session)
            except Exception:
                pass
            return None, current_url

        warning_reason = _looks_like_fb_warning_or_block(html, current_url)
        if warning_reason:
            self._skip_fb_due_to_warning = True
            self._skip_fb_due_to_warning_reason = warning_reason
            _log(
                self.logger,
                f"[Night FB] Circuit breaker: detected {warning_reason} page; skipping FB for remainder of run. url={current_url!r}"
            )
            raise FacebookDriverError(f"fb_circuit_breaker:{warning_reason}")

        return html, current_url

    def _fetch_html_with_url_anon(self, url: str, goto_about: bool = True) -> Tuple[Optional[str], Optional[str]]:
        try:
            driver = self._get_anon_driver()
        except Exception as exc:
            _log(self.logger, f"[Night FB] Anonymous driver unavailable: {exc}")
            return None, None
        try:
            driver.get(url)
            if goto_about:
                goto_about_fn = getattr(self.legacy, "_goto_facebook_about", None)
                if callable(goto_about_fn):
                    try:
                        goto_about_fn(driver, url, timeout=5.0)
                    except Exception:
                        pass
            time.sleep(1.0)
            current_url = getattr(driver, "current_url", None) or url
            if is_fb_login_redirect(current_url) or _is_fb_login_or_security_url(current_url):
                return None, current_url
            return driver.page_source, current_url
        except Exception as exc:
            _log(self.logger, f"[Night FB] Anonymous fetch failed for {url}: {exc}")
            return None, None

    def _fetch_html(self, url: str) -> Optional[str]:
        html, _ = self._fetch_html_with_url(url, goto_about=True)
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
        return False

    def _search_for_page(self, artist: str, location: str, allow_anon: bool = False) -> Optional[str]:
        self._last_selected_candidate_context = None
        self._last_search_candidates = []
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

        def _fetch_search_html(query_str: str) -> Tuple[Optional[str], Optional[Any]]:
            encoded_q = urllib.parse.quote_plus(query_str)
            search_url = f"https://www.facebook.com/search/pages/?q={encoded_q}"
            _log(self.logger, f"[Night FB] Searching Facebook for '{query_str}' -> {search_url}")
            def _nav_with_session() -> Tuple[str, Any]:
                drv = session.navigate(search_url)
                time.sleep(1.5)
                return getattr(drv, "page_source", ""), drv

            def _nav_anon() -> Tuple[str, Any]:
                driver = self._get_anon_driver()
                driver.get(search_url)
                time.sleep(1.5)
                return getattr(driver, "page_source", ""), driver

            nav_fn = _nav_with_session if session else _nav_anon
            try:
                return nav_fn()
            except Exception as exc:  # pragma: no cover - defensive
                _log(self.logger, f"[Night FB] Search navigation failed (will refresh session): {exc}")
                if session:
                    try:
                        self._refresh_driver(session)
                    except FacebookDriverError as exc2:
                        raise exc2
            nav_fn = _nav_with_session if session else _nav_anon
            try:
                return nav_fn()
            except FacebookDriverError as exc2:
                raise exc2
            except Exception as exc2:  # pragma: no cover - defensive
                _log(self.logger, f"[Night FB] Search navigation failed after refresh: {exc2}")
                raise FacebookDriverError(str(exc2))

        def _normalize_location_for_query(raw: str) -> str:
            raw = (raw or "").strip()
            if not raw:
                return ""
            parts = [p.strip() for p in re.split(r"[,|/]+", raw) if p.strip()]
            if len(parts) >= 2:
                return " ".join(parts[:2])
            return raw

        location_query = _normalize_location_for_query(location)
        primary_query = " ".join(part for part in (artist, location_query) if part).strip()
        if not primary_query:
            return None
        html, nav_driver = _fetch_search_html(primary_query)

        # Optional self-check: confirm shared URL gate predicate.
        if _bool_env("FB_DEBUG_CAND_GATE_ASSERT", default=False):
            try:
                assert _fb_is_candidate_url_allowed is facebook_enrich._fb_is_candidate_url_allowed  # type: ignore
            except Exception:
                pass

        refine_query_list = [f"{artist} musician", f"{artist} band"]
        if location_query:
            refine_query_list.insert(0, f"{artist} {location_query}")
        if self.slow_mode_active:
            refine_query_list = refine_query_list[:1]

        def _run_refine_queries(diagnostics: Optional[Dict[str, Any]] = None) -> List["facebook_enrich.FbCandidate"]:
            refine_candidates: List["facebook_enrich.FbCandidate"] = []
            for refine_query in refine_query_list:
                html_refined, drv_refined = _fetch_search_html(refine_query)
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
        soft_blocked = bool(diagnostics.get("overlay_soft_block"))
        ranked_for_preview = _rank_candidates_for_preview(artist, candidates)

        need_refine = False
        if soft_blocked:
            self._enter_slow_mode("overlay_zero_anchors", max(self.slow_mode_multiplier, 1.5))
            # Skip refine cascade when soft-blocked; rely on slug/candidate fallback.
            need_refine = False
        elif refine_enabled:
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

        ranked_candidates: List["facebook_enrich.FbCandidate"] = [item["candidate"] for item in ranked_for_preview]
        candidate, selected_by = self._choose_ranked_candidate(artist, ranked_for_preview)
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
                elif not refine_forced:
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

        ranking_enabled = True
        ordered_candidates = _order_candidates_for_selection(candidate, candidates, ranked_candidates, ranking_enabled)

        gate_debug = os.getenv("FB_DEBUG_CANDIDATES") == "1"
        url_flow_debug = gate_debug or os.getenv("FB_DEBUG_CAND_URL_FLOW") == "1"
        debug_detail = url_flow_debug

        collected_contexts: List[Tuple[Dict[str, Any], Any]] = []

        for cand in ordered_candidates:
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
            context = {
                "url": norm_url,
                "name": name or "",
                "category": category or "",
                "category_raw": raw_category or "",
                "base_score": base_score,
            }
            collected_contexts.append((context, cand))

        if collected_contexts:
            first_ctx, first_cand = collected_contexts[0]
            self._last_selected_candidate_context = first_ctx
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
        if not fb_is_allowed_profile_candidate_url(raw_fb_url):
            if gate_debug:
                _log(self.logger, f"[Night FB][Gate] rejected url={raw_fb_url!r} before scrape")
            return None
        candidate_url = _normalise_fb_url(raw_fb_url or "")
        if not candidate_url:
            return None
        if _is_junk_fb_candidate(candidate_url):
            return None
        url_lower = candidate_url.lower()
        if "/r.php" in url_lower or "/login" in url_lower or "/register" in url_lower:
            _log(self.logger, f"[Night FB] Ignoring login/redirect page: {candidate_url}")
            return None

        used_driver_kind = "session"
        outcome_hint = "fetch_error"
        reject_reason = ""

        html, resolved_url = self._fetch_html_with_url(candidate_url, goto_about=False)
        if html:
            outcome_hint = "fetched"
        if (not html) and allow_anon:
            html, resolved_url = self._fetch_html_with_url_anon(candidate_url, goto_about=False)
            used_driver_kind = "anon_fallback"
            if html and outcome_hint != "fetched":
                outcome_hint = "fetched"
        if not html:
            return None
        resolved_url = _normalise_fb_url(resolved_url or candidate_url)
        if _is_fb_login_or_security_url(resolved_url):
            _log(self.logger, f"[Night FB] Ignoring login/redirect page: {resolved_url}")
            return None, [], used_driver_kind, "login_wall"

        lower_html = (html or "").lower()
        not_found_phrases = ("page isn\u2019t available", "page isn't available", "content isn't available", "not available right now")
        if any(p in lower_html for p in not_found_phrases):
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

        has_music_signals_main = _night_fb_has_music_signals(soup, {"url": resolved_url})
        emails = _extract_emails_from_html(html or "")

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

        need_about_fetch = (not has_music_signals) or (not emails)
        if need_about_fetch:
            if not has_music_signals:
                _log(self.logger, f"[Night FB] No music signals on main page {resolved_url}, checking About tab...")
            about_attempted = "yes"
            for about_url in _fetch_fb_about_variants(resolved_url):
                try:
                    about_html, about_resolved = self._fetch_html_with_url(about_url, goto_about=False)
                    if (not about_html) and allow_anon:
                        about_html, about_resolved = self._fetch_html_with_url_anon(about_url, goto_about=False)
                except Exception:
                    about_html, about_resolved = "", about_url
                final_about = _normalise_fb_url(about_resolved or about_url)
                if _is_fb_login_or_security_url(final_about):
                    about_result = "blocked_login"
                    break
                lower_html = (about_html or "").lower()
                if any(tok in lower_html for tok in ("checkpoint", "consent", "cookie", "privacy")):
                    about_result = "checkpoint"
                    break
                not_found_phrases = ("page isn’t available", "page isn't available", "content isn't available", "not available right now")
                if any(p in lower_html for p in not_found_phrases):
                    about_result = "not_found"
                    continue

                about_soup = BeautifulSoup(about_html or "", "html.parser") if about_html else None
                if (not has_music_signals) and about_soup:
                    if _night_fb_has_music_signals(about_soup, {"url": final_about}):
                        has_music_signals = True
                        about_result = "music_signals"
                        _log(self.logger, f"[Night FB] Music signals found on About tab {final_about}.")
                if not emails:
                    about_emails = _extract_emails_from_html(about_html or "")
                    if about_emails:
                        emails = about_emails
                        email_source = about_url.rsplit("/", 1)[-1] or "about"
                        about_result = "emails_found"
                if has_music_signals and emails:
                    break
                if not about_result:
                    about_result = "no_email"
            if not about_result:
                about_result = "fetch_error" if not emails else "emails_found"

        if about_result == "not_found" and not emails and not has_music_signals:
            _log(self.logger, f"[Night FB][PageUnavailable] {resolved_url} (about tab)")
            outcome_hint = "content_unavailable"

        # Email override gating when music signals are missing.
        email_override_decision = True
        email_override_reason = ""
        if emails and not has_music_signals:
            extracted = {
                "has_music_signals": has_music_signals,
                "category": meta_category,
                "descriptor": page_title,
                "music_hint": bool(candidate_context and candidate_context.get("category") and _category_is_music_like(candidate_context.get("category"))),
                "score": candidate_context.get("base_score") if candidate_context else 0.0,
                "seed_url_match": seed_url_match,
                "artist_location": artist_location,
            }
            email_override_decision, email_override_reason = should_accept_email_override(
                artist_name,
                {
                    "name": page_title,
                    "category": meta_category,
                    "raw_category": meta_category,
                    "base_score": candidate_context.get("base_score") if candidate_context else 0.0,
                },
                extracted_signals=extracted,
            )
            if email_override_decision:
                _log(self.logger, f"[Night FB][EmailOverrideAccept] url='{resolved_url}' reason='{email_override_reason}' emails={len(emails)} category='{meta_category}' name='{page_title}'")
            else:
                _log(self.logger, f"[Night FB][EmailOverrideReject] url='{resolved_url}' reason='{email_override_reason}' emails={len(emails)} category='{meta_category}' name='{page_title}'")
                emails = []
                reject_reason = email_override_reason or "email_override_reject"

        gate_soft_pass_category = False
        gate_soft_pass_identity = False
        if not has_music_signals and not emails:
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

        night_result = self._build_result(
            emails,
            str(row.get("Email_All", "") or ""),
            resolved_url,
            artist_name,
            allow_empty=has_music_signals or emails or gate_soft_pass_category or gate_soft_pass_identity,
        )
        if not night_result:
            return None
        night_result.email_source = email_source
        night_result.about_attempted = about_attempted
        night_result.about_result = about_result or ("soft_pass_identity" if gate_soft_pass_identity else "soft_pass_category" if gate_soft_pass_category else "")
        if gate_soft_pass_category:
            row["FB_Gate"] = "soft_pass_category"
        if gate_soft_pass_identity:
            row["FB_Gate"] = "soft_pass_identity"
        outcome_hint = "found_email" if emails else "no_email_on_page"
        if night_result is None:
            if reject_reason:
                self._fb_mark_rejected(artist_name, resolved_url or candidate_url, reject_reason)
            return None, emails, used_driver_kind, reject_reason or outcome_hint
        return night_result, emails, used_driver_kind, outcome_hint

    def _build_result(
        self,
        emails: List[str],
        email_all_existing: str,
        facebook_url: str,
        artist_name: str,
        email_source: str = "main",
        about_attempted: str = "no",
        about_result: str = "",
        allow_empty: bool = False,
    ) -> Optional[NightModeFacebookResult]:
        if not emails and not allow_empty:
            return None
        primary = _choose_primary_email(emails, artist_name) if emails else None
        merged_all = _merge_email_all(email_all_existing, emails)
        email_type = "fb_night"
        return NightModeFacebookResult(
            email=primary,
            email_all=merged_all,
            email_type=email_type,
            facebook_url=facebook_url,
            email_source=email_source,
            about_attempted=about_attempted,
            about_result=about_result,
        )

    def _apply_night_fb_result(
        self, target_row: Dict[str, str], night_result: NightModeFacebookResult, emails: List[str], page_url: str
    ) -> Dict[str, str]:
        if not night_result:
            return target_row
        target_row["Email"] = night_result.email or target_row.get("Email", "")
        target_row["Email_All"] = night_result.email_all
        target_row["Email_Type"] = night_result.email_type
        if night_result.facebook_url:
            target_row["Facebook_URL"] = night_result.facebook_url
        if night_result.email_source:
            target_row["FB_Email_Source"] = night_result.email_source
        if night_result.about_attempted:
            target_row["FB_About_Attempted"] = night_result.about_attempted
        if night_result.about_result:
            target_row["FB_About_Result"] = night_result.about_result
        if not target_row.get("FB_Status"):
            target_row["FB_Status"] = "ok"
        _log(self.logger, f"[Night FB] extracted email(s) {emails} from {page_url}")
        return target_row

    def _diagnose_explicit_fb_failure(self, url: str, allow_anon: bool) -> Tuple[List[str], Optional[str]]:
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
            return [], "no_email_found"

        try:
            return _try_explicit_fb(driver, url, logger=self.logger)
        except Exception:
            return [], "no_email_found"

    def _enrich_row_unearthed_legacy(
        self,
        result: Dict[str, str],
        artist_name: str,
        fb_urls: List[str],
    ) -> Dict[str, str]:
        def _map_unearthed_outcome(emails: List[str], status: str) -> Tuple[str, str]:
            """
            Map legacy Unearthed scrape status to PASS A counters/log reasons.
            Outcome values must match PASS A summary buckets.
            """
            if emails:
                return "found_email", "explicit_url"
            status_norm = (status or "").lower()
            if status_norm in ("login_redirect", "checkpoint"):
                return "login_wall", "anon_login_wall"
            if status_norm in ("error", "fetch_error"):
                return "fetch_error", "legacy_error"
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
                    night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), resolved_url or fb_url, artist_name)
                    if night_result:
                        result = self._apply_night_fb_result(result, night_result, emails, resolved_url or fb_url)
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
            page_url = self._search_for_page(query, location="", allow_anon=True) or ""
            if not page_url:
                result["FB_Status"] = "unearthed_no_candidates"
                return result
            try:
                driver = self._get_unearthed_driver()
            except Exception as exc:
                result["FB_Status"] = "unearthed_driver_error"
                _log(self.logger, f"[Night FB][Unearthed] Could not start public FB driver for blind search: {exc}")
                return result
            self._pass_a_bump("attempted")
            emails, status, resolved_url = _scrape_fb_page_unearthed_legacy(driver, page_url, logger=self.logger)
            outcome, base_reason = _map_unearthed_outcome(emails, status)
            reason = "share_url" if _is_fb_share_url_str(page_url) else base_reason
            self._pass_a_bump(outcome)
            self._pass_a_log_row(artist_name, resolved_url or page_url, "legacy_unearthed_anon", outcome, reason)
            if emails:
                night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), resolved_url or page_url, artist_name)
                if night_result:
                    result = self._apply_night_fb_result(result, night_result, emails, resolved_url or page_url)
                    result["FB_Status"] = "ok_unearthed_blind"
                    return result
            result["FB_Status"] = status or "unearthed_no_emails"
            return result

    def enrich_row_with_facebook_night(self, row: Dict[str, str], row_index: Optional[int] = None) -> Dict[str, str]:
        """Night-Mode-only FB enrichment for a single row."""
        original_row = dict(row or {})
        result = dict(original_row)
        result["FB_Status"] = result.get("FB_Status", "") or ""
        self._checkpoint_warned_this_row = False

        def _clean_val(value: str) -> str:
            try:
                import pandas as _pd  # local import to avoid hard dep during tests
                if _pd.isna(value):
                    return ""
            except Exception:
                pass
            return str(value or "").strip()

        if self._skip_fb_due_to_session_failure or self._session_failed:
            result["FB_Status"] = result.get("FB_Status", "") or "driver_error"
            result["FB_Reason"] = self._session_failed_reason or self._skip_fb_due_to_session_failure_reason or "session_start_failed"
            return result

        if self._skip_fb_due_to_display:
            result["FB_Status"] = result.get("FB_Status", "") or "skipped_no_display"
            result["FB_Reason"] = "no_display_env"
            return result

        if self._skip_fb_due_to_warning:
            result["FB_Status"] = result.get("FB_Status", "") or "skipped_warning"
            result["FB_Reason"] = self._skip_fb_due_to_warning_reason or "warning_interstitial"
            return result

        if self.protective_shutdown:
            result["FB_Status"] = result.get("FB_Status", "") or "skipped_checkpoint"
            result["FB_Reason"] = result.get("FB_Reason", "") or "checkpoint"
            return result

        if self._skip_fb_due_to_checkpoint:
            return self._mark_row_checkpoint(result)

        existing_email = _clean_val(result.get("Email", ""))
        if existing_email:
            if not result.get("FB_Status"):
                result["FB_Status"] = "ok"
            return result
        artist_name = _clean_val(result.get("Artist Name", ""))
        location = _clean_val(result.get("Location", ""))
        facebook_url = _normalise_fb_url(_clean_val(result.get("Facebook_URL", "")))
        fb_urls = _extract_fb_urls_for_night_mode(result)
        if facebook_url and facebook_url not in fb_urls:
            fb_urls.insert(0, facebook_url)
        is_unearthed = self._is_unearthed_source(result)

        try:
            if not self._maybe_recover_or_skip_on_checkpoint():
                return self._mark_row_checkpoint(result)
            page_url = ""
            emails: List[str] = []
            if not fb_urls and self._search_disabled_due_to_checkpoint:
                result["FB_Status"] = "checkpoint_search_disabled"
                result["FB_Reason"] = "checkpoint"
                _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
                return result
            if is_unearthed:
                _log(self.logger, "[Night FB] Detected Unearthed row -> using legacy no-login FB scrape.")
                return self._enrich_row_unearthed_legacy(result, artist_name, fb_urls)

            allow_anon = self._should_allow_anonymous(result)
            # PASS A: explicit URL attempts (instrumentation only)
            outcome_rank = {"found_email": 0, "login_wall": 1, "fetch_error": 2, "no_email_on_page": 3}
            best_outcome = None
            best_reason = ""
            best_driver = ""
            best_page_url = ""

            if not fb_urls:
                if not result.get("FB_Status"):
                    result["FB_Status"] = "pass_a_skipped_no_fb_url"
                if not result.get("FB_Reason"):
                    result["FB_Reason"] = "skipped_no_fb_url"
                _log(self.logger, "[Night FB][PASS A] skipped (no explicit FB URL); proceeding to v2 search")
            else:
                _log(self.logger, f"[Night FB] Using explicit FB URLs: {fb_urls}")
                for direct_url in fb_urls:
                    driver_kind = "session"
                    outcome_for_log = "fetch_error"
                    reason_for_log = ""
                    self._pass_a_bump("attempted")
                    try:
                        candidate = self._scrape_single_fb_candidate(direct_url, result, artist_name, allow_anon=allow_anon)
                    except Exception as exc:
                        candidate = None
                        driver_kind = "unknown"
                        outcome_for_log = "fetch_error"
                        reason_for_log = f"session_exception:{exc.__class__.__name__}"
                    night_result, emails, driver_kind, candidate_outcome = _unpack_fb_candidate(candidate)
                    if night_result is not None or candidate_outcome == "login_wall":
                        if night_result is None:
                            outcome_for_log = candidate_outcome
                            if candidate_outcome == "login_wall":
                                reason_for_log = "session_login_wall" if not driver_kind.startswith("anon") else "anon_login_wall"
                                current_rank = outcome_rank.get("login_wall", 99)
                                if best_outcome is None or current_rank < outcome_rank.get(best_outcome, 99):
                                    best_outcome = "login_wall"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(direct_url)
                            elif candidate_outcome == "content_unavailable":
                                reason_for_log = "content_unavailable"
                                if best_outcome is None:
                                    best_outcome = "content_unavailable"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(direct_url)
                            else:
                                reason_for_log = f"{driver_kind}_exception:unknown"
                                self._pass_a_bump("fetch_error")
                                if best_outcome is None:
                                    best_outcome = "fetch_error"
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = _normalise_fb_url(direct_url)
                        else:
                            outcome_for_log = candidate_outcome
                            if candidate_outcome == "found_email":
                                reason_for_log = "explicit_url"
                            elif candidate_outcome == "no_email_on_page":
                                reason_for_log = "session_fetch_ok_no_email" if not driver_kind.startswith("anon") else "anon_fetch_ok_no_email"
                            elif candidate_outcome == "login_wall":
                                reason_for_log = "session_login_wall" if not driver_kind.startswith("anon") else "anon_login_wall"
                            else:
                                reason_for_log = f"{driver_kind}_exception:unknown"
                            if emails:
                                page_url = night_result.facebook_url or _normalise_fb_url(direct_url)
                                result = self._apply_night_fb_result(result, night_result, emails, page_url)
                                result["FB_Status"] = "pass_a_found_email"
                                result["FB_Reason"] = "explicit_url"
                                self._pass_a_bump("found_email")
                                self._pass_a_log_row(artist_name, page_url, driver_kind, "found_email", reason_for_log)
                                return result
                            else:
                                page_url = night_result.facebook_url or _normalise_fb_url(direct_url)
                                current_rank = outcome_rank.get(candidate_outcome, 99)
                                if best_outcome is None or current_rank < outcome_rank.get(best_outcome, 99):
                                    best_outcome = candidate_outcome
                                    best_reason = reason_for_log
                                    best_driver = driver_kind
                                    best_page_url = page_url
                    else:
                        if not reason_for_log:
                            reason_for_log = "session_exception:unknown" if driver_kind == "session" else f"{driver_kind}_exception:unknown"
                        self._pass_a_bump("fetch_error")
                        if best_outcome is None:
                            best_outcome = "fetch_error"
                            best_reason = reason_for_log
                            best_driver = driver_kind
                            best_page_url = _normalise_fb_url(direct_url)
                    self._pass_a_log_row(artist_name, direct_url, driver_kind, outcome_for_log, reason_for_log)

                if best_outcome:
                    if best_outcome == "login_wall":
                        result["FB_Status"] = "pass_a_login_wall"
                        result["FB_Reason"] = best_reason or ("anon_login_wall" if best_driver.startswith("anon") else "session_login_wall")
                        self._register_login_wall()
                        self._pass_a_bump("login_wall")
                    elif best_outcome == "fetch_error":
                        result["FB_Status"] = "pass_a_fetch_error"
                        result["FB_Reason"] = best_reason or ("anon_exception:unknown" if best_driver.startswith("anon") else "session_exception:unknown")
                        self._pass_a_bump("fetch_error")
                    else:
                        result["FB_Status"] = "pass_a_no_email_on_page"
                        result["FB_Reason"] = best_reason or ("anon_fetch_ok_no_email" if best_driver.startswith("anon") else "session_fetch_ok_no_email")
                        self._pass_a_bump("no_email_on_page")
                    page_url = best_page_url or page_url
                else:
                    # Diagnostics fallback for first URL
                    reason_code: Optional[str] = None
                    diag_emails: List[str] = []
                    probe_url = fb_urls[0] if fb_urls else ""
                    if probe_url:
                        diag_emails, reason_code = self._diagnose_explicit_fb_failure(probe_url, allow_anon=allow_anon)
                    if diag_emails:
                        page_url = _normalise_fb_url(probe_url)
                        night_result = self._build_result(diag_emails, str(result.get("Email_All", "") or ""), page_url, artist_name)
                        if night_result:
                            result = self._apply_night_fb_result(result, night_result, diag_emails, page_url)
                            result["FB_Status"] = "pass_a_found_email"
                            result["FB_Reason"] = "explicit_url"
                            self._pass_a_bump("found_email")
                            self._pass_a_log_row(artist_name, page_url, "anon" if allow_anon else "session", "found_email", "explicit_url")
                            return result
                    if reason_code:
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
                    return result
                if session and hasattr(self.legacy, "fb_find_page_and_emails_by_name"):
                    try:
                        driver = session.ensure_logged_in() if hasattr(session, "ensure_logged_in") else session.navigate("about:blank")
                    except Exception:
                        driver = None
                    if driver:
                        page_url, emails = self.legacy.fb_find_page_and_emails_by_name(
                            driver,
                            artist_name,
                            location,
                            log_fn=self.logger,
                            log_prefix="[Night FB]",
                            suppress_console=True,
                            allow_soft_pass_category=True,
                        )
            if not page_url:
                page_url = self._search_for_page(artist_name, location, allow_anon=allow_anon) or ""
                if self._search_disabled_due_to_checkpoint:
                    if not result.get("FB_Status"):
                        result["FB_Status"] = "checkpoint_search_disabled"
                        result["FB_Reason"] = "checkpoint"
                    _log(self.logger, "[Night FB] search disabled due to checkpoint; skipping FB search.")
                    return result
                if self._checkpoint_limited_active and not page_url:
                    if not result.get("FB_Status") or result.get("FB_Status") == "no_candidates":
                        result["FB_Status"] = "checkpoint_limited"
                    if not result.get("FB_Reason"):
                        result["FB_Reason"] = "checkpoint"
                    return result
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
                        result = self._apply_night_fb_result(result, night_result, emails, page_url)
                        if self._checkpoint_limited_active and not result.get("FB_Reason"):
                            result["FB_Reason"] = "checkpoint"
                        return result
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
                                result = self._apply_night_fb_result(result, alt_result, alt_emails, page_url)
                                if self._checkpoint_limited_active and not result.get("FB_Reason"):
                                    result["FB_Reason"] = "checkpoint"
                                return result
                            if alt_outcome != "content_unavailable":
                                break
                        if not result.get("FB_Status"):
                            result["FB_Status"] = "content_unavailable"
                            result["FB_Reason"] = "content_unavailable"
                            _log(self.logger, f"[Night FB][PageUnavailable] No usable FB candidates for '{artist_name}' after content-unavailable fallbacks.")
                            return result
                if not result.get("FB_Status"):
                    result["FB_Status"] = "checkpoint_limited" if self._checkpoint_limited_active else "no_candidates"
                if self._checkpoint_limited_active and not result.get("FB_Reason"):
                    result["FB_Reason"] = "checkpoint"
                _log(self.logger, f"[Night FB] No usable FB candidates for '{artist_name}', marking FB_Status='{result.get('FB_Status')}'.")
                return result
            night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), page_url, artist_name)
            if night_result:
                result = self._apply_night_fb_result(result, night_result, emails, page_url)
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
            return result
        except FacebookDriverError as exc:
            exc_msg = str(exc) or ""
            if exc_msg.startswith("fb_circuit_breaker:"):
                reason = exc_msg.split("fb_circuit_breaker:", 1)[-1] or "warning_interstitial"
                self._skip_fb_due_to_warning = True
                self._skip_fb_due_to_warning_reason = reason
                result["FB_Status"] = "skipped_warning"
                result["FB_Reason"] = reason
                _log(self.logger, f"[Night FB] Circuit breaker tripped ({reason}); skipping FB for remainder of run.")
                return result
            result["FB_Status"] = "driver_error"
            _log(self.logger, f"[Night FB] Driver error while enriching '{result.get('Artist Name', '') or '<unknown>'}': {exc}")
            return result
        except Exception as exc:  # pragma: no cover - defensive
            prefix = f"[FB Night] Night FB enrich failed at row {row_index}: {exc}" if row_index is not None else f"[FB Night] Night FB enrich failed: {exc}"
            _log(self.logger, prefix)
            return original_row
