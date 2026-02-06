"""Night-Mode-only Facebook enrichment helpers.

This module isolates Night Mode tweaks so daytime paths stay unchanged.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
import shutil
from pathlib import Path
import atexit
import weakref
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import logging
from typing import Union

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

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


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").strip().lower())


def _split_multi(value: str) -> List[str]:
    if not value:
        return []
    parts = re.split(r"[;,|]", value)
    return [p.strip() for p in parts if p and p.strip()]


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


def _purge_wdm_cache(driver_path: str) -> None:
    """
    Remove webdriver_manager's cache folder for a given driver path to self-heal bad downloads.
    """
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


def _get_night_fb_profile_dir() -> str:
    """
    Resolve a stable Chrome profile directory for Night FB.
    Preference: env NIGHT_FB_PROFILE_DIR; fallback to sibling 'Lead Machine Code/night_fb_profile'.
    """
    env_path = os.environ.get("NIGHT_FB_PROFILE_DIR")
    if env_path:
        return os.path.abspath(env_path)
    try:
        # Repo layout has siblings: "Lead Machine VS Code" (assets) and "Lead Machine Code" (shared code/venv).
        base = Path(__file__).resolve().parent.parent / "Lead Machine Code" / "night_fb_profile"
        return str(base)
    except Exception:
        return str(Path(__file__).resolve().parent / "night_fb_profile")


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


def _create_fb_driver_night_mode(headless: bool, logger: LoggerFn = None):
    """
    Night-Mode-only Chrome driver with persistent profile to reuse FB auth.
    """
    profile_dir = _get_night_fb_profile_dir()
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

    _log(logger, f"[Night FB] Using Chrome profile dir: {profile_dir} (profile 'Default', headless={headless})")
    driver = _start_chromedriver_with_retry(chrome_options)
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
            if not healthy:
                # Try a neutral authed page before restarting.
                try:
                    self.driver.get("https://www.facebook.com/me")
                    time.sleep(1.0)
                    healthy, reason = _session_looks_healthy(self.driver)
                except Exception:
                    healthy, reason = False, reason or "exception"
            if not healthy:
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
                    if not healthy_retry:
                        _log(self.logger, f"[Night FB] Session still unhealthy; continuing but FB results may be limited. reason={reason_retry}")
                except Exception:
                    _log(self.logger, "[Night FB] Session restart failed; continuing but FB results may be limited.")
                    self.driver = old_driver
            return self.driver

        if self.headless:
            raise FacebookDriverError("Headless session unauthenticated (no c_user cookie present).")

        _log(self.logger, "[Night FB] Awaiting manual login to establish session (headed mode)...")
        self._wait_for_manual_login(self.driver)
        return self.driver

    def navigate(self, url: str):
        driver = self.ensure_logged_in()
        driver.get(url)
        return driver

    def refresh_session(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        return self.ensure_logged_in()


def _start_chromedriver_with_retry(chrome_options):
    """
    Start ChromeDriver with a one-time reinstall if the first launch fails.
    """
    last_exc: Optional[Exception] = None
    for _ in range(2):
        driver_path = ChromeDriverManager().install()
        try:
            service = ChromeService(driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
            _register_driver_cleanup(driver)
            return driver
        except Exception as exc:
            last_exc = exc
            _purge_wdm_cache(driver_path)
    if last_exc:
        raise last_exc
    raise FacebookDriverError("Failed to start ChromeDriver.")


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
    return _start_chromedriver_with_retry(chrome_options)


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
    if not category:
        return False
    cat = (category or "").strip().lower()
    tokens = ("musician", "musician/band", "band", "artist", "singer", "producer", "dj", "music")
    return any(tok in cat for tok in tokens)


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
    return any(token in page_text for token in ("musician", "artist", "band", "music", "singer", "producer", "dj"))


def _is_garbage_fb_candidate(title: str, url: str, category: Optional[str]) -> bool:
    """
    Filter out business/notification-style FB search results that are not real pages.
    """
    title_l = (title or "").lower()
    url_l = (url or "").lower()
    category_l = (category or "").lower() if category else ""

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
        aria = getattr(cand, "aria_label", "") or ""
        cat = getattr(cand, "category", "") or ""
        music_hint = _category_is_music_like(cat) or _category_is_music_like(aria)
        try:
            setattr(cand, "music_hint", bool(music_hint))
        except Exception:
            pass

    deduped = _dedupe_candidates(raw_candidates)
    deduped = sorted(deduped, key=lambda c: 0 if getattr(c, "music_hint", False) else 1)

    if os.getenv("FB_DEBUG_CANDIDATES") == "1":
        preview = deduped[:5]
        lines = [
            f"[Night FB][debug] cand name={c.name!r} url={c.url!r} category={getattr(c, 'category', '')!r} music_hint={bool(getattr(c, 'music_hint', False))}"
            for c in preview
        ]
        for line in lines:
            _log(logger, line)

    return deduped


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
        cat = (cand.category or "").lower()
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
        self.headless = _bool_env("NIGHT_FB_HEADLESS", default=False)
        self._session_failed = False
        self._session_failed_reason = ""

    def __enter__(self) -> "NightModeFacebookEnricher":
        try:
            self._ensure_session()
        except FacebookDriverError as exc:
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
                        _log(self.logger, "[Night FB] Reusing existing authenticated driver (persistent profile).")
                        return self.session
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
        elif self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

        if not self.username or not self.password:
            _log(self.logger, "[Night FB] Missing FB credentials; running without live session.")
            return None
        try:
            driver_factory = lambda: _create_fb_driver_night_mode(self.headless, logger=self.logger)
            self.session = NightPersistentFacebookSession(driver_factory, headless=self.headless, logger=self.logger)
            self._owns_session = True
            self.session.ensure_logged_in()
            return self.session
        except FacebookDriverError as exc:
            self._session_failed = True
            self._session_failed_reason = str(exc)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._session_failed = True
            self._session_failed_reason = str(exc)
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

    def _fetch_html_with_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        session = self._ensure_session()
        if not session:
            return None, None
        self._ensure_driver_alive(session)
        def _navigate_once() -> Tuple[Optional[str], Optional[str]]:
            driver = session.navigate(url)
            goto_about = getattr(self.legacy, "_goto_facebook_about", None)
            if callable(goto_about):
                try:
                    goto_about(driver, url, timeout=5.0)
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

        return html, current_url

    def _fetch_html_with_url_anon(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            driver = self._get_anon_driver()
        except Exception as exc:
            _log(self.logger, f"[Night FB] Anonymous driver unavailable: {exc}")
            return None, None
        try:
            driver.get(url)
            goto_about = getattr(self.legacy, "_goto_facebook_about", None)
            if callable(goto_about):
                try:
                    goto_about(driver, url, timeout=5.0)
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
        html, _ = self._fetch_html_with_url(url)
        return html

    def _should_allow_anonymous(self, row: Dict[str, str]) -> bool:
        source_dir = str(row.get("Source Directory", "") or row.get("Source Directory ".strip(), "") or "").lower()
        source_job = str(row.get("__source_job", "") or "").lower()
        return ("unearthed" in source_dir) or ("unearthed" in source_job)

    def _is_unearthed_source(self, row: Dict[str, str]) -> bool:
        source_dir = str(row.get("Source Directory", "") or "").strip().lower()
        source_tag = str(row.get("Source Tag", "") or "").strip().lower()
        source_job = str(row.get("__source_job", "") or "").strip().lower()
        return any("unearthed" in val for val in (source_dir, source_tag, source_job))

    def _search_for_page(self, artist: str, location: str, allow_anon: bool = False) -> Optional[str]:
        session = self._ensure_session()
        if not session and not allow_anon:
            return None
        if session:
            self._ensure_driver_alive(session)
        query = " ".join(part for part in (artist, location) if part).strip()
        if not query:
            return None
        encoded = urllib.parse.quote_plus(query)
        search_url = f"https://www.facebook.com/search/pages/?q={encoded}"
        _log(self.logger, f"[Night FB] Searching Facebook for '{query}' -> {search_url}")
        html = None

        def _do_nav():
            drv = session.navigate(search_url) if session else self._get_anon_driver().get(search_url) or self._get_anon_driver()
            time.sleep(1.5)
            return drv.page_source if hasattr(drv, "page_source") else self._anon_driver.page_source

        try:
            if session:
                html = _do_nav()
            else:
                driver = self._get_anon_driver()
                driver.get(search_url)
                time.sleep(1.5)
                html = driver.page_source
        except Exception as exc:  # pragma: no cover - defensive
            _log(self.logger, f"[Night FB] Search navigation failed (will refresh session): {exc}")
            try:
                if session:
                    self._refresh_driver(session)
                    html = _do_nav()
                else:
                    driver = self._get_anon_driver()
                    driver.get(search_url)
                    time.sleep(1.5)
                    html = driver.page_source
            except FacebookDriverError as exc2:
                raise exc2
            except Exception as exc2:  # pragma: no cover - defensive
                _log(self.logger, f"[Night FB] Search navigation failed after refresh: {exc2}")
                raise FacebookDriverError(str(exc2))
        candidates = _parse_search_candidates(html, logger=self.logger, search_name=artist)
        candidate = None
        selector = getattr(facebook_enrich, "select_best_facebook_candidate", None) if facebook_enrich is not None else None
        if callable(selector):
            try:
                candidate = selector(candidates, artist, logger=self.logger, suppress_console=True)
            except Exception:
                candidate = None
        if not candidate:
            search_result = _select_best_candidate_loose(artist, candidates)
            candidate = search_result
            if isinstance(search_result, list):
                candidate = next((c for c in search_result if c), None)
        if not candidate:
            _log(self.logger, f"[Night FB] No non-junk FB candidates for '{artist}', skipping Facebook.")
            return None

        ordered_candidates = []
        if candidate:
            ordered_candidates.append(candidate)
        for cand in candidates:
            if cand is candidate:
                continue
            ordered_candidates.append(cand)

        gate_debug = os.getenv("FB_DEBUG_CANDIDATES") == "1"
        url_flow_debug = gate_debug or os.getenv("FB_DEBUG_CAND_URL_FLOW") == "1"
        debug_detail = url_flow_debug

        for cand in ordered_candidates:
            raw_url = _candidate_url(cand)
            norm_url = _normalise_fb_url(raw_url or "")

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
            category = getattr(cand, "category", None)
            if isinstance(cand, dict):
                name = name or cand.get("name")
                category = category or cand.get("category")

            _log(self.logger, f"[Night FB] Selected FB candidate '{name or norm_url}' -> {norm_url} (category='{category or ''}')")
            return norm_url

        _log(self.logger, f"[Night FB] No usable FB candidates for '{artist}' after URL validation.")
        return None

    def _scrape_single_fb_candidate(
        self, fb_url: str, row: Dict[str, str], artist_name: str, allow_anon: bool = False
    ) -> Optional[Tuple[NightModeFacebookResult, List[str]]]:
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

        html, resolved_url = self._fetch_html_with_url(candidate_url)
        if (not html) and allow_anon:
            html, resolved_url = self._fetch_html_with_url_anon(candidate_url)
        if not html:
            return None
        resolved_url = _normalise_fb_url(resolved_url or candidate_url)
        if _is_fb_login_or_security_url(resolved_url):
            _log(self.logger, f"[Night FB] Ignoring login/redirect page: {resolved_url}")
            return None

        soup = BeautifulSoup(html, "html.parser")
        meta_category = ""
        try:
            meta_tag = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
            meta_category = (meta_tag.get("content") or "").strip()
        except Exception:
            meta_category = ""
        has_music_signals = _night_fb_has_music_signals(soup, {"url": resolved_url})
        emails = _extract_emails_from_html(html or "")
        gate_soft_pass = False
        if not has_music_signals and not emails:
            if _category_is_music_like(meta_category):
                gate_soft_pass = True
                _log(self.logger, f"[Night FB] Soft-pass music gate by category allowlist: category='{meta_category}' url='{resolved_url}'")
            else:
                _log(self.logger, f"[Night FB] No music signals detected on FB page {resolved_url}, skipping.")
                night_result = self._build_result(
                    [],
                    str(row.get("Email_All", "") or ""),
                    resolved_url,
                    artist_name,
                    allow_empty=True,
                )
                if night_result:
                    night_result.email_source = ""
                    night_result.about_attempted = "no"
                    night_result.about_result = "no_music_signals"
                return night_result, []

        about_attempted = "no"
        about_result = ""
        email_source = "main" if emails else ""
        if not emails:
            about_attempted = "yes"
            for about_url in _fetch_fb_about_variants(resolved_url):
                try:
                    about_html, about_resolved = self._fetch_html_with_url(about_url)
                    if (not about_html) and allow_anon:
                        about_html, about_resolved = self._fetch_html_with_url_anon(about_url)
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
                about_emails = _extract_emails_from_html(about_html or "")
                if about_emails:
                    emails = about_emails
                    email_source = about_url.rsplit("/", 1)[-1] or "about"
                    about_result = "emails_found"
                    break
                about_result = "no_email" if about_result == "" else about_result
            if not about_result:
                about_result = "fetch_error" if not emails else "emails_found"

        night_result = self._build_result(emails, str(row.get("Email_All", "") or ""), resolved_url, artist_name)
        night_result.email_source = email_source
        night_result.about_attempted = about_attempted
        night_result.about_result = about_result
        if gate_soft_pass:
            row["FB_Gate"] = "soft_pass_category"
        if night_result:
            return night_result, emails
        return None

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
        # Always prefer explicit URLs first.
        if fb_urls:
            try:
                driver = self._get_unearthed_driver()
            except Exception as exc:
                result["FB_Status"] = "unearthed_driver_error"
                _log(self.logger, f"[Night FB][Unearthed] Could not start public FB driver: {exc}")
                return result
            for fb_url in fb_urls:
                emails, status, resolved_url = _scrape_fb_page_unearthed_legacy(driver, fb_url, logger=self.logger)
                if emails:
                    night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), resolved_url or fb_url, artist_name)
                    if night_result:
                        result = self._apply_night_fb_result(result, night_result, emails, resolved_url or fb_url)
                        result["FB_Status"] = "ok_unearthed_legacy"
                    else:
                        result["FB_Status"] = "unearthed_no_emails"
                    return result
            # If explicit URLs failed, fall back to search.

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
            emails, status, resolved_url = _scrape_fb_page_unearthed_legacy(driver, page_url, logger=self.logger)
            if emails:
                night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), resolved_url or page_url, artist_name)
                if night_result:
                    result = self._apply_night_fb_result(result, night_result, emails, resolved_url or page_url)
                    result["FB_Status"] = "ok_unearthed_blind"
                    return result
            result["FB_Status"] = status or "unearthed_no_emails"
            return result

        try:
            driver = self._get_unearthed_driver()
        except Exception as exc:
            result["FB_Status"] = "unearthed_driver_error"
            _log(self.logger, f"[Night FB][Unearthed] Could not start public FB driver: {exc}")
            return result

        last_status = "no_emails"
        for fb_url in fb_urls:
            emails, status, resolved_url = _scrape_fb_page_unearthed_legacy(driver, fb_url, logger=self.logger)
            last_status = status or "no_emails"
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

    def enrich_row_with_facebook_night(self, row: Dict[str, str], row_index: Optional[int] = None) -> Dict[str, str]:
        """Night-Mode-only FB enrichment for a single row."""
        original_row = dict(row or {})
        result = dict(original_row)
        result["FB_Status"] = result.get("FB_Status", "") or ""

        def _clean_val(value: str) -> str:
            try:
                import pandas as _pd  # local import to avoid hard dep during tests
                if _pd.isna(value):
                    return ""
            except Exception:
                pass
            return str(value or "").strip()

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
            page_url = ""
            emails: List[str] = []
            if is_unearthed:
                _log(self.logger, "[Night FB] Detected Unearthed row -> using legacy no-login FB scrape.")
                return self._enrich_row_unearthed_legacy(result, artist_name, fb_urls)

            allow_anon = self._should_allow_anonymous(result)
            if fb_urls:
                _log(self.logger, f"[Night FB] Using explicit FB URLs: {fb_urls}")
                candidates: List[Tuple[NightModeFacebookResult, List[str]]] = []
                for direct_url in fb_urls:
                    try:
                        candidate = self._scrape_single_fb_candidate(direct_url, result, artist_name, allow_anon=allow_anon)
                    except Exception:
                        candidate = None
                    if candidate:
                        candidates.append(candidate)
                if candidates:
                    best_result, emails = candidates[0]
                    page_url = best_result.facebook_url or (fb_urls[0] if fb_urls else "")
                    result = self._apply_night_fb_result(result, best_result, emails, page_url)
                    return result
                else:
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
                            return result
                    if reason_code:
                        result["FB_Status"] = f"explicit_fail:{reason_code}"
                        _log(self.logger, f"[Night FB] Explicit FB URL had no emails (reason={reason_code}); falling back to search.")
                    else:
                        _log(self.logger, "[Night FB] Explicit FB URLs produced no results; falling back to search.")

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
                    if page_url:
                        candidate = self._scrape_single_fb_candidate(page_url, result, artist_name, allow_anon=allow_anon)
                        if candidate:
                            night_result, emails = candidate
                            page_url = night_result.facebook_url or page_url
                            result = self._apply_night_fb_result(result, night_result, emails, page_url)
                            return result
                    if not result.get("FB_Status"):
                        result["FB_Status"] = "no_candidates"
                    _log(self.logger, f"[Night FB] No usable FB candidates for '{artist_name}', marking FB_Status='no_candidates'.")
                    return result
            night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), page_url, artist_name)
            if night_result:
                result = self._apply_night_fb_result(result, night_result, emails, page_url)
            else:
                # Page reached but no emails extracted.
                if _is_fb_login_or_security_url(page_url):
                    result["FB_Status"] = "login_redirect"
                    result["Facebook_URL"] = ""
                    _log(self.logger, f"[Night FB] Detected login redirect for '{artist_name}' -> {page_url}, marking FB_Status='login_redirect'.")
                else:
                    if not result.get("FB_Status"):
                        result["FB_Status"] = "ok"
            return result
        except FacebookDriverError as exc:
            result["FB_Status"] = "driver_error"
            _log(self.logger, f"[Night FB] Driver error while enriching '{result.get('Artist Name', '') or '<unknown>'}': {exc}")
            return result
        except Exception as exc:  # pragma: no cover - defensive
            prefix = f"[FB Night] Night FB enrich failed at row {row_index}: {exc}" if row_index is not None else f"[FB Night] Night FB enrich failed: {exc}"
            _log(self.logger, prefix)
            return original_row
