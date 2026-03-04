"""Spotify About tab scraper using lightweight HTTP requests.

Collects social links and basic metadata (location, genres) from the Spotify
artist About page via the embedded __NEXT_DATA__ payload, falling back to HTML
anchors when the JSON payload is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from html_fetcher import fetch_html
from bs4 import BeautifulSoup

Row = Dict[str, str]
LoggerFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

SOCIAL_FIELDS = ("Spotify_Website_URL",)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}
HTTP_TIMEOUT = 10
HTTP_REQUEST_DELAY = 0.7  # polite gap between artist fetches
SOCIAL_DOMAIN_MAP = {
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "facebook.com": "facebook",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "official.fm": "website",
}
SOCIAL_LABEL_MAP = {
    "instagram": "instagram",
    "facebook": "facebook",
    "twitter": "twitter",
    "x": "twitter",
    "website": "website",
    "homepage": "website",
    "home page": "website",
    "official site": "website",
    "official website": "website",
}
NON_WEBSITE_DOMAINS = {
    "spotify.com",
    "open.spotify.com",
    "spoti.fi",
    "scdn.co",
    "apple.com",
    "music.apple.com",
    "itunes.apple.com",
    "linktr.ee",
    "beacons.ai",
}
FALLBACK_SOCIAL_DOMAINS = {
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "facebook.com": "facebook",
    "twitter.com": "twitter",
    "x.com": "twitter",
}
LOCATION_KEYS = ("city", "hometown", "origin", "location")
GENRE_KEYS = ("genres", "genreNames", "genre_names")

_HTTP_SESSION: Optional[requests.Session] = None
_ABOUT_CACHE: Dict[str, Dict[str, Any]] = {}
def _env_true(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


DEBUG_SPOTIFY_ABOUT = _env_true("SPOTIFY_ABOUT_DEBUG", False)
_DEBUG_SPOTIFY_ABOUT_ONCE = False
_SPOTIFY_ABOUT_ALWAYS_ARTIFACTS = _env_true("SPOTIFY_ABOUT_ALWAYS_ARTIFACTS", False)


# Spotify-only persistent Playwright profile (opt-in, safe default inside repo)
_SPOTIFY_PW_PROFILE_DIR = os.environ.get("SPOTIFY_PW_PROFILE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".cache", "spotify_pw_profile")
)

# Opt-in artifacts (HTML snippet / screenshot) when debugging consent walls
_SPOTIFY_ABOUT_DEBUG_ARTIFACTS = _env_true("SPOTIFY_ABOUT_DEBUG_ARTIFACTS", False)
_SPOTIFY_ABOUT_ARTIFACT_DIR = os.environ.get("SPOTIFY_ABOUT_ARTIFACT_DIR")
CONSENT_TIMEOUT_MS = 1800
_DEFAULT_NEXTDATA_WAIT_MS = 2500
try:
    _parsed_wait = int(os.environ.get("SPOTIFY_NEXTDATA_WAIT_MS", "") or 0)
    if _parsed_wait > 0:
        _DEFAULT_NEXTDATA_WAIT_MS = max(500, min(_parsed_wait, 6000))
except Exception:
    pass

SPOTIFY_NEXTDATA_WAIT_MS = _DEFAULT_NEXTDATA_WAIT_MS
SPOTIFY_NEXTDATA_MAX_RETRIES = 1


def _log(logger: Optional[LoggerFn], message: str) -> None:
    if not logger or not message:
        return
    try:
        logger(message)
    except Exception:
        pass


def _get_http_session() -> requests.Session:
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        session = requests.Session()
        session.headers.update(HTTP_HEADERS)
        _HTTP_SESSION = session
    return _HTTP_SESSION


def _empty_socials() -> Dict[str, str]:
    return {
        "instagram": "",
        "facebook": "",
        "twitter": "",
        "website": "",
    }


def _url_path_endswith_handle(url: str, handle_set: set) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        parts = [p for p in (parsed.path or "").split("/") if p]
        if not parts:
            return False
        first = parts[0].lower()
        return first in handle_set
    except Exception:
        return False


def _is_spotify_generic_socials(socials: Dict[str, str]) -> bool:
    if not socials:
        return False
    generic_domains = ("spotify.com", "open.spotify.com", "spotifyforvendors.com")
    insta_handles = {"spotify", "spotifyartists", "spotifyaunz"}
    fb_handles = {"spotify", "spotifyau", "spotifyaunz"}
    tw_handles = {"spotify", "spotifyau", "spotifyartists"}

    insta = socials.get("instagram") or ""
    fb = socials.get("facebook") or ""
    tw = socials.get("twitter") or ""
    web = (socials.get("website") or "").lower()

    if ("instagram.com" in insta.lower()) and _url_path_endswith_handle(insta, insta_handles):
        return True
    if ("facebook.com" in fb.lower()) and _url_path_endswith_handle(fb, fb_handles):
        return True
    if ("twitter.com" in tw.lower() or "x.com" in tw.lower()) and _url_path_endswith_handle(tw, tw_handles):
        return True
    if any(dom in web for dom in generic_domains):
        return True
    return False


def _maybe_save_artifact(artist_id: str, html: str, fetch_info: Dict[str, Any]) -> None:
    always = _SPOTIFY_ABOUT_ALWAYS_ARTIFACTS
    debug_flag = _SPOTIFY_ABOUT_DEBUG_ARTIFACTS
    if not (_SPOTIFY_ABOUT_ARTIFACT_DIR and (debug_flag or always or fetch_info.get("force_artifacts"))):
        return
    try:
        os.makedirs(_SPOTIFY_ABOUT_ARTIFACT_DIR, exist_ok=True)
        snippet_path = os.path.join(_SPOTIFY_ABOUT_ARTIFACT_DIR, f"about_{artist_id}_snippet.txt")
        with open(snippet_path, "w", encoding="utf-8") as fh:
            fh.write(f"final_url={fetch_info.get('final_url','')}\n")
            fh.write(html[:5000])
    except Exception:
        pass


def _save_missing_nextdata_artifacts(
    artist_id: str,
    page_html: str,
    document_html: str,
    fetch_info: Dict[str, Any],
    *,
    page_title: str,
    about_used: bool,
) -> None:
    if not _SPOTIFY_ABOUT_ARTIFACT_DIR:
        return
    if not (_SPOTIFY_ABOUT_DEBUG_ARTIFACTS or _SPOTIFY_ABOUT_ALWAYS_ARTIFACTS):
        return
    try:
        os.makedirs(_SPOTIFY_ABOUT_ARTIFACT_DIR, exist_ok=True)
        base = os.path.join(_SPOTIFY_ABOUT_ARTIFACT_DIR, f"about_{artist_id}_missing_nextdata")
        with open(base + ".txt", "w", encoding="utf-8") as fh:
            fh.write(f"final_url={fetch_info.get('final_url','')}\n")
            fh.write(f"mode={fetch_info.get('mode')}\n")
            fh.write(f"consent_clicked={fetch_info.get('consent_clicked')}\n")
            fh.write(f"title={page_title or ''}\n")
            fh.write(f"about_used={about_used}\n")
            snippet = (document_html or "")[:500]
            fh.write("document_snippet:\n")
            fh.write(snippet)
            fh.write("\n----\n")
            fh.write("page_content_snippet:\n")
            fh.write((page_html or "")[:500])
        # Save full document response separately for forensic parsing.
        if document_html:
            with open(base + "_document.html", "w", encoding="utf-8") as fh:
                fh.write(document_html)
        if page_html:
            with open(base + "_pagecontent.html", "w", encoding="utf-8") as fh:
                fh.write(page_html)
    except Exception:
        pass


def _log_social_summary(logger: Optional[LoggerFn], artist_id: str, socials: Dict[str, str], cached: bool = False) -> None:
    if not logger:
        return
    prefix = "[Spotify About] (cache) " if cached else "[Spotify About] "
    if socials and any(socials.values()):
        _log(
            logger,
            f"{prefix}Found socials for artist {artist_id}: "
            f"IG={socials.get('instagram') or ''} "
            f"FB={socials.get('facebook') or ''} "
            f"TW={socials.get('twitter') or ''} "
            f"WEB={socials.get('website') or ''}",
        )
    else:
        _log(logger, f"{prefix}No socials found for artist {artist_id}")


def _debug_spotify_about(url: str) -> None:
    """Temporary helper to inspect anchors on an artist page when debugging."""
    global _DEBUG_SPOTIFY_ABOUT_ONCE
    if _DEBUG_SPOTIFY_ABOUT_ONCE:
        return
    _DEBUG_SPOTIFY_ABOUT_ONCE = True
    session = _get_http_session()
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print(f"[Spotify Debug] Failed to fetch {url}: {exc}")
        return
    print(f"[Spotify Debug] HTTP {resp.status_code} for {url}")
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.find("body")
    body_class = body.get("class") if body else None
    print(f"[Spotify Debug] <body class=> {body_class}")
    anchors = soup.find_all("a", href=True)
    print(f"[Spotify Debug] Found {len(anchors)} anchors.")
    for anchor in anchors[:50]:
        href = (anchor.get("href") or "").strip()
        text = (anchor.get_text(strip=True) or "")
        print(f"  - {href} | {text}")


def _spotify_consent_handler(page, meta: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort click through Spotify's consent wall (page + iframes)."""
    meta = meta if isinstance(meta, dict) else {}
    meta.setdefault("consent_found", False)
    meta.setdefault("consent_clicked", False)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except Exception:
        class PlaywrightTimeoutError(Exception):
            pass

    selectors = [
        "button:has-text('Accept Cookies')",
        "button:has-text('Accept all')",
        "button:has-text('Allow all')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button:has-text('Continue')",
        "button:has-text('OK')",
        "div[role=button]:has-text('Accept')",
        "div[role=button]:has-text('Agree')",
        "div[role=button]:has-text('Allow')",
        "[aria-label*='accept' i]",
        "[aria-label*='agree' i]",
        "[aria-label*='allow' i]",
        "button[data-testid*='accept' i]",
        "button[data-testid*='agree' i]",
        "button[data-testid*='allow' i]",
        "[data-testid*='consent' i]",
        "[data-testid*='cookie' i]",
        "[id*='onetrust' i] button",
        "[class*='onetrust' i] button",
    ]

    def _try_frame(frame) -> bool:
        for sel in selectors:
            try:
                locator_obj = frame.locator(sel)
                locator = getattr(locator_obj, "first", locator_obj)
                locator.wait_for(state="visible", timeout=CONSENT_TIMEOUT_MS)
                meta["consent_found"] = True
                try:
                    locator.click(timeout=CONSENT_TIMEOUT_MS)
                    meta["consent_clicked"] = True
                except PlaywrightTimeoutError:
                    pass
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    frames: List[Any] = []
    try:
        frames.append(page)
    except Exception:
        pass
    try:
        frames.extend(list(getattr(page, "frames", []) or []))
    except Exception:
        pass

    for frame in frames:
        if _try_frame(frame):
            break

    if meta.get("consent_clicked"):
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass


def _spotify_wait_for_next_data(page, meta: Dict[str, Any], timeout_ms: int) -> bool:
    meta.setdefault("next_data_waited", False)
    meta.setdefault("next_data_found", False)
    meta["next_data_waited"] = True
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except Exception:
        class PlaywrightTimeoutError(Exception):
            pass

    try:
        page.wait_for_selector("script#__NEXT_DATA__", timeout=timeout_ms)
        meta["next_data_found"] = True
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False


def _spotify_maybe_screenshot(page, artist_id: str) -> None:
    if not _SPOTIFY_ABOUT_ARTIFACT_DIR:
        return
    if not (_SPOTIFY_ABOUT_DEBUG_ARTIFACTS or _SPOTIFY_ABOUT_ALWAYS_ARTIFACTS):
        return
    try:
        os.makedirs(_SPOTIFY_ABOUT_ARTIFACT_DIR, exist_ok=True)
        screenshot_path = os.path.join(
            _SPOTIFY_ABOUT_ARTIFACT_DIR, f"about_{artist_id}_screenshot.png"
        )
        page.screenshot(path=screenshot_path, full_page=True)
    except Exception:
        # Never let screenshot failure break pipeline.
        pass


def _spotify_page_handler(artist_id: str, meta: Dict[str, Any]) -> Callable[[Any], None]:
    def _handler(page: Any) -> None:
        _spotify_consent_handler(page, meta)
        found = _spotify_wait_for_next_data(page, meta, SPOTIFY_NEXTDATA_WAIT_MS)
        if found:
            return

        meta["next_data_retry"] = True
        try:
            _spotify_consent_handler(page, meta)
        except Exception:
            pass
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        found_retry = _spotify_wait_for_next_data(page, meta, SPOTIFY_NEXTDATA_WAIT_MS)
        if not found_retry:
            try:
                meta["evaluate_attempted"] = True
                eval_result = page.evaluate("() => window.__NEXT_DATA__ || window.__PRELOADED_STATE__ || null")
                if eval_result:
                    meta["evaluated_next_data_json"] = json.dumps(eval_result)
                    meta["next_data_found"] = True
            except Exception:
                meta["evaluate_error"] = True
            _spotify_maybe_screenshot(page, artist_id)

    return _handler


def extract_social_links_from_page(page_html: str) -> Dict[str, str]:
    """Return detected social URLs from the About page HTML anchors."""
    if not page_html:
        return _empty_socials()
    soup = BeautifulSoup(page_html, "html.parser")
    return _extract_socials_from_soup(soup)


def _extract_socials_from_soup(soup: Optional[BeautifulSoup]) -> Dict[str, str]:
    results = _empty_socials()
    if soup is None:
        return results
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        normalized = href.split("?")[0].strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered.startswith("javascript:") or lowered.startswith("#") or lowered.startswith("mailto:"):
            continue
        if "spotify.com" in lowered or lowered.endswith("scdn.co"):
            continue

        parsed = urlparse(normalized)
        domain = (parsed.netloc or "").lower()
        if not parsed.scheme or not domain:
            continue

        if "instagram.com" in lowered or "instagr.am" in lowered:
            if not results["instagram"]:
                results["instagram"] = normalized
            continue
        if "facebook.com" in lowered:
            if not results["facebook"]:
                results["facebook"] = normalized
            continue
        if "twitter.com" in lowered or "x.com" in lowered:
            if not results["twitter"]:
                results["twitter"] = normalized
            continue
        if domain and domain not in NON_WEBSITE_DOMAINS and parsed.scheme in ("http", "https"):
            if not results["website"]:
                results["website"] = normalized

    return results


def _fallback_socials_from_html(page_html: str, existing: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    socials = dict(existing or _empty_socials())
    if not page_html:
        return socials
    soup = BeautifulSoup(page_html, "html.parser")
    anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        normalized = href.split("?")[0].strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered.startswith("javascript:") or lowered.startswith("#") or lowered.startswith("mailto:"):
            continue
        parsed = urlparse(normalized)
        domain = (parsed.netloc or "").lower()
        if not parsed.scheme or not domain:
            continue
        mapped = None
        for key, target in FALLBACK_SOCIAL_DOMAINS.items():
            if key in domain:
                mapped = target
                break
        if not mapped:
            continue
        if mapped == "instagram" and not socials.get("instagram"):
            socials["instagram"] = normalized
        elif mapped == "facebook" and not socials.get("facebook"):
            socials["facebook"] = normalized
        elif mapped == "twitter" and not socials.get("twitter"):
            socials["twitter"] = normalized
        elif mapped == "website" and not socials.get("website"):
            socials["website"] = normalized
    return socials


def _detect_cookie_wall(html: str) -> Optional[str]:
    if not html:
        return None
    lowered = html.lower()
    tokens = (
        "enable cookies",
        "please enable cookies",
        "enable javascript",
        "turn on javascript",
        "javascript is required",
        "verify you are human",
        "are you human",
        "unusual traffic",
        "robot check",
        "captcha",
        "cloudflare",
        "bot detection",
    )
    if any(tok in lowered for tok in tokens):
        return "cookie_wall_or_bot"
    return None


# Updated to drive enrichment from the base artist page JSON instead of /about.
def enrich_spotify_rows_with_about_links(
    rows: List[Row],
    logger: Optional[LoggerFn] = None,
    progress_callback: Optional[ProgressFn] = None,
) -> List[Row]:
    """Visit the Spotify About tab for each artist and extract social links."""

    if not rows:
        return rows

    session = _get_http_session()
    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        _populate_defaults(row)
        if _row_is_fully_enriched(row):
            _log(logger, "[Spotify About] Row already has contacts + location/genre; skipping fetch.")
            _emit_progress(progress_callback, idx, total)
            continue
        artist_id = _resolve_artist_id(row)
        if not artist_id:
            _log(logger, "[Spotify About] Missing artist ID; skipping row.")
            _apply_socials(row, {})
            _emit_progress(progress_callback, idx, total)
            continue

        cached = _ABOUT_CACHE.get(artist_id)
        if cached:
            _log_social_summary(logger, artist_id, cached.get("socials") or {}, cached=True)
            _apply_enrichment(row, cached)
            _emit_progress(progress_callback, idx, total)
            continue

        payload = _fetch_about_payload(session, artist_id, logger)
        if payload is None:
            if HTTP_REQUEST_DELAY:
                time.sleep(HTTP_REQUEST_DELAY)
            _emit_progress(progress_callback, idx, total)
            continue

        _ABOUT_CACHE[artist_id] = payload
        if payload:
            _apply_enrichment(row, payload)
        else:
            _apply_socials(row, {})
        if HTTP_REQUEST_DELAY:
            time.sleep(HTTP_REQUEST_DELAY)
        _emit_progress(progress_callback, idx, total)

    return rows


# Updated to parse __NEXT_DATA__ from the base artist page (no /about) and pull socials/location/genres.
def _fetch_about_payload(
    session: requests.Session,
    artist_id: str,
    logger: Optional[LoggerFn],
) -> Dict[str, Any]:
    artist_url = f"https://open.spotify.com/artist/{artist_id}/about"
    html, fetch_info = _fetch_artist_html(session, artist_url, logger)
    document_html = fetch_info.get("document_html") or ""
    if not html and not document_html and not fetch_info.get("evaluated_next_data_json"):
        return {}

    if DEBUG_SPOTIFY_ABOUT:
        _debug_spotify_about(artist_url)

    payload: Dict[str, Any] = {
        "socials": _empty_socials(),
        "location": "",
        "primary_genre": "",
    }

    next_data = _extract_next_data(document_html or "") or _extract_next_data(html or "")
    if not next_data:
        eval_json = fetch_info.get("evaluated_next_data_json")
        if eval_json:
            try:
                next_data = json.loads(eval_json)
            except Exception:
                next_data = None
    if not next_data:
        wall_reason = _detect_cookie_wall(document_html or html)
        payload["status"] = "non_actionable"
        page_title = _extract_title(document_html or html)
        if wall_reason and fetch_info.get("consent_clicked") and page_title:
            wall_reason = None
        if fetch_info.get("consent_clicked") and page_title:
            payload["reason"] = "next_data_missing_after_consent"
        elif wall_reason:
            payload["reason"] = wall_reason
        elif fetch_info.get("mode") == "playwright":
            payload["reason"] = "spotify_bot_variant_no_nextdata"
        else:
            payload["reason"] = "about_payload_unavailable"
        fetch_info["force_artifacts"] = True
        _log(
            logger,
            (
                f"[Spotify About] __NEXT_DATA__ missing for artist {artist_id} "
                f"reason={payload['reason']} mode={fetch_info.get('mode')} final_url={fetch_info.get('final_url')} "
                f"consent_found={fetch_info.get('consent_found')} consent_clicked={fetch_info.get('consent_clicked')} "
                f"next_data_waited={fetch_info.get('next_data_waited')} next_data_found={fetch_info.get('next_data_found')} "
                f"next_data_retry={fetch_info.get('next_data_retry')} "
                f"title={page_title or ''}"
            ),
        )
        _save_missing_nextdata_artifacts(
            artist_id,
            html,
            document_html,
            fetch_info,
            page_title=page_title,
            about_used=True,
        )
    else:
        profile = _extract_profile_blob(next_data)
        if not profile:
            _log(logger, f"[Spotify About] Unable to locate profile JSON for artist {artist_id}")
        else:
            profile_socials = _extract_socials_from_profile(profile)
            for key, value in profile_socials.items():
                if value and not payload["socials"].get(key):
                    payload["socials"][key] = value
            payload["location"] = _extract_location_from_profile(profile)
            payload["primary_genre"] = _extract_primary_genre(profile)
            if _is_spotify_generic_socials(payload["socials"]):
                payload["socials"] = _empty_socials()
                payload["reason"] = payload.get("reason") or "generic_spotify_socials_filtered"
                _log(
                    logger,
                    f"[Spotify About] filtered generic Spotify socials for artist {artist_id} "
                    f"mode={fetch_info.get('mode')} title={_extract_title(document_html or html) or ''}",
                )

    _log_social_summary(logger, artist_id, payload.get("socials") or {})
    _maybe_save_artifact(artist_id, document_html or html, fetch_info)

    return payload


# Updated to fetch the canonical artist page once (no /about) with graceful logging.
def _fetch_artist_html(
    session: requests.Session,
    artist_url: str,
    logger: Optional[LoggerFn],
) -> Tuple[str, Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "consent_found": False,
        "consent_clicked": False,
        "next_data_waited": False,
        "next_data_found": False,
        "next_data_retry": False,
        "about_path_used": artist_url.endswith("/about"),
    }
    try:
        result = fetch_html(
            artist_url,
            session=session,
            directory="spotify",
            required_selectors=["script#__NEXT_DATA__"],
            allow_browser_fallback=True,
            timeout_s=HTTP_TIMEOUT,
            persistent_profile_dir=_SPOTIFY_PW_PROFILE_DIR,
            page_handler=_spotify_page_handler(artist_url.split("/")[-1], meta),
        )
    except Exception as exc:
        _log(logger, f"[Spotify About] HTTP error for {artist_url}: {exc}")
        return "", {"mode": "requests", "reason": "exception", "final_url": artist_url, **meta}

    status = result.get("status")
    mode_used = result.get("mode_used")
    html = result.get("html") or ""
    document_html = result.get("document_html") or ""
    reason = result.get("reason")
    final_url = result.get("final_url") or artist_url

    if mode_used == "requests":
        if status and status != 200:
            _log(logger, f"[Spotify About] HTTP {status} for {artist_url}")
            return "", {
                "mode": mode_used,
                "reason": f"http_{status}",
                "final_url": final_url,
                "document_html": document_html,
                **meta,
            }
        return html, {"mode": mode_used, "reason": reason, "final_url": final_url, "document_html": document_html, **meta}

    if mode_used == "playwright":
        _log(logger, f"[Spotify About] Playwright fallback used for {artist_url} reason={reason}")
        return html if html else "", {
            "mode": mode_used,
            "reason": reason,
            "final_url": final_url,
            "document_html": document_html,
            **meta,
        }

    return html, {"mode": mode_used or "unknown", "reason": reason, "final_url": final_url, "document_html": document_html, **meta}


def _safe_parse_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    raw = raw.strip()
    # Trim trailing semicolons or assignment wrappers.
    raw = raw.rstrip(";")
    # Some blobs come as \"window.__NEXT_DATA__ = {...}\"; try to strip leading assignment.
    assign_match = re.search(r"=\s*(\{.*\})", raw, re.DOTALL)
    if assign_match:
        raw = assign_match.group(1)
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_next_data(html: str) -> Optional[Dict[str, Any]]:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    # 1) canonical id match
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        parsed = _safe_parse_json(script.string)
        if parsed:
            return parsed
    # 2) any script that looks like Next.js payload
    for node in soup.find_all("script"):
        content = (node.string or node.text or "").strip()
        if not content:
            continue
        if '"props":{"pageProps"' in content or "pageProps" in content:
            parsed = _safe_parse_json(content)
            if parsed:
                return parsed
        if "Spotify.Entity" in content or "__PRELOADED_STATE__" in content:
            parsed = _safe_parse_json(content)
            if parsed:
                return parsed
    # 3) regex sweep for inline assignment patterns
    patterns = [
        r"__NEXT_DATA__\s*=\s*(\{.*?\})\s*<",
        r"Spotify\.Entity\s*=\s*(\{.*?\})\s*;",
        r"__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;",
    ]
    for pat in patterns:
        match = re.search(pat, html, re.DOTALL)
        if match:
            parsed = _safe_parse_json(match.group(1))
            if parsed:
                return parsed
    return None


def _extract_profile_blob(next_data: Dict[str, Any]) -> Dict[str, Any]:
    props = next_data.get("props") or {}
    page_props = props.get("pageProps") or {}
    candidates: List[Any] = []
    for key in (
        "artistProfile",
        "artist",
        "data",
        "profile",
        "state",
        "pageData",
    ):
        value = page_props.get(key)
        if value:
            candidates.append(value)
    candidates.append(page_props)
    for candidate in candidates:
        profile = _resolve_profile_candidate(candidate)
        if profile:
            return profile
    return _search_for_profile(page_props)


def _resolve_profile_candidate(candidate: Any) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    if isinstance(candidate.get("profile"), dict):
        return candidate["profile"]
    if isinstance(candidate.get("artist"), dict):
        return _resolve_profile_candidate(candidate["artist"])
    if any(key in candidate for key in ("externalLinks", "city", "genres", "location")):
        return candidate
    if isinstance(candidate.get("data"), dict):
        return _resolve_profile_candidate(candidate["data"])
    return {}


def _search_for_profile(blob: Any) -> Dict[str, Any]:
    stack = [blob]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(key in item for key in ("externalLinks", "city", "location", "profile")):
                if isinstance(item.get("profile"), dict):
                    return item["profile"]
                return item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return {}


def _extract_socials_from_profile(profile: Dict[str, Any]) -> Dict[str, str]:
    socials = _empty_socials()
    external_links = (
        ((profile.get("externalLinks") or {}).get("items"))
        if isinstance(profile.get("externalLinks"), dict)
        else profile.get("externalLinks")
    )
    if not isinstance(external_links, list):
        external_links = []

    for item in external_links:
        if not isinstance(item, dict):
            continue
        raw_url = (item.get("url") or item.get("uri") or "").strip()
        if not raw_url:
            continue
        normalized = raw_url.split("?")[0]
        label = (item.get("name") or item.get("title") or item.get("type") or "").strip().lower()
        mapped = SOCIAL_LABEL_MAP.get(label)
        if not mapped:
            domain = urlparse(normalized).netloc.lower()
            for domain_key, target in SOCIAL_DOMAIN_MAP.items():
                if domain_key in domain:
                    mapped = target
                    break
        if not mapped:
            continue
        if mapped == "instagram" and not socials["instagram"]:
            socials["instagram"] = normalized
        elif mapped == "facebook" and not socials["facebook"]:
            socials["facebook"] = normalized
        elif mapped == "twitter" and not socials["twitter"]:
            socials["twitter"] = normalized
        elif mapped == "website" and not socials["website"]:
            socials["website"] = normalized
    return socials


def _extract_title(html: str) -> str:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        title = soup.title.string if soup.title else ""
        return (title or "").strip()
    except Exception:
        return ""


def _extract_location_from_profile(profile: Dict[str, Any]) -> str:
    def _normalize_entry(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("name", "displayName", "label", "value"):
                candidate = (value.get(key) or "").strip()
                if candidate:
                    return candidate
        elif isinstance(value, str):
            return value.strip()
        return ""

    city = ""
    for key in LOCATION_KEYS:
        if city:
            break
        city = _normalize_entry(profile.get(key))
    country = _normalize_entry(profile.get("country"))
    if not country and isinstance(profile.get("city"), dict):
        country = _normalize_entry(profile["city"].get("country"))
    if city and country:
        if country.lower() in city.lower():
            return city
        return f"{city}, {country}"
    return city or country


def _extract_primary_genre(profile: Dict[str, Any]) -> str:
    for key in GENRE_KEYS:
        value = profile.get(key)
        if isinstance(value, list):
            dedup: List[str] = []
            for entry in value:
                entry = (entry or "").strip()
                if entry and entry not in dedup:
                    dedup.append(entry)
            if dedup:
                return ", ".join(dedup[:3])
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _apply_enrichment(row: Row, payload: Dict[str, Any]) -> None:
    socials = payload.get("socials") or {}
    _apply_socials(row, socials)
    location = (payload.get("location") or "").strip()
    if location and not (row.get("Location") or "").strip():
        row["Location"] = location
    genre = (payload.get("primary_genre") or "").strip()
    if genre and not (row.get("Primary Genre") or "").strip():
        row["Primary Genre"] = genre


def _populate_defaults(row: Row) -> None:
    for key in SOCIAL_FIELDS:
        row.setdefault(key, "")
    row.setdefault("Social Link", "")
    row.setdefault("Location", "")
    row.setdefault("Primary Genre", "")


def _resolve_artist_id(row: Row) -> str:
    artist_id = (row.get("Spotify_Artist_ID") or "").strip()
    if artist_id:
        return artist_id
    url = (row.get("Spotify_URL") or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "artist":
        return path_parts[1]
    return ""

def _row_is_fully_enriched(row: Row) -> bool:
    contact_fields = ("Email", "Social Link", "External Links", "Spotify_Website_URL")
    has_contacts = any((row.get(field) or "").strip() for field in contact_fields)
    has_location = bool((row.get("Location") or "").strip())
    has_genre = bool((row.get("Primary Genre") or "").strip())
    return has_contacts and has_location and has_genre


def _apply_socials(row: Row, data: Dict[str, str]) -> None:
    website = (data.get("website") or "").strip()
    if website.startswith("//"):
        website = f"https:{website}"
    normalized_website = website.split("?")[0].strip() if website else ""
    if normalized_website and "spotify.com" in normalized_website.lower():
        normalized_website = ""

    social_urls: List[str] = []
    for key in ("instagram", "facebook", "twitter", "website"):
        value = (data.get(key) or "").strip()
        if not value:
            continue
        if value.startswith("//"):
            value = f"https:{value}"
        normalized = value.split("?")[0].strip()
        if not normalized or "spotify.com" in normalized.lower():
            continue
        if normalized not in social_urls:
            social_urls.append(normalized)

    if social_urls:
        row["Social Link"] = " | ".join(social_urls)
    elif not (row.get("Social Link") or ""):
        row["Social Link"] = ""

    if normalized_website:
        row["Spotify_Website_URL"] = normalized_website
        row["External Links"] = normalized_website
    else:
        row.setdefault("External Links", row.get("External Links", ""))


def _emit_progress(callback: Optional[ProgressFn], current: int, total: int) -> None:
    if not callback:
        return
    try:
        callback(current, total)
    except Exception:
        pass
