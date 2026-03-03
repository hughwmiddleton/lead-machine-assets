"""Unified HTML fetcher with Playwright fallback (requests-first).

Exposes a single entrypoint:
    fetch_html(url, *, allow_browser_fallback=True, directory=None, job_id=None,
               required_selectors=None, session=None, timeout_s=20,
               persistent_profile_dir=None, page_handler=None) -> dict

Return schema:
    {
        "status": int | None,
        "final_url": str,
        "html": str,
        "mode_used": "requests" | "playwright",
        "reason": "ok" | "status_403" | "status_406" | "status_429" |
                   "status_503" | "soft_block" | "missing_selectors" |
                   "requests_error" | "playwright_error",
        "elapsed_ms": int,
        "domain": str,
    }

Notes:
- requests is always attempted first; Playwright is fallback-only and limited by
  per-job page budgets and one attempt per URL.
- Playwright is lazily imported; if unavailable the fetch gracefully degrades
  with reason="playwright_error".
"""

from __future__ import annotations

import atexit
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

# Environment defaults
_PW_ENABLED = str(os.getenv("PLAYWRIGHT_FALLBACK_ENABLED", "1")).lower() not in {"0", "false", "off"}
_PW_MAX_PAGES = int(os.getenv("PLAYWRIGHT_MAX_PAGES_PER_JOB", "20") or 20)
_PW_HEADLESS = str(os.getenv("PLAYWRIGHT_HEADLESS", "1")).lower() not in {"0", "false", "off"}

_SOFT_BLOCK_TOKENS = (
    "enable javascript",
    "verify you are human",
    "access denied",
)


class PlaywrightUnavailable(RuntimeError):
    """Raised when Playwright is not installed or cannot start."""


@dataclass
class _JobBrowser:
    playwright: object
    browser: object  # None when using launch_persistent_context
    context: object
    pages_used: int = 0


_JOB_BROWSERS: Dict[Tuple[str, str], _JobBrowser] = {}


def _log_fetch(
    directory: Optional[str],
    mode: str,
    reason: str,
    domain: str,
    elapsed_ms: int,
    url: str,
    final_url: str,
) -> None:
    try:
        LOGGER.info(
            "[Fetch] directory=%s mode=%s reason=%s domain=%s ms=%d url=%s final_url=%s",
            directory or "",
            mode,
            reason,
            domain,
            elapsed_ms,
            url,
            final_url,
        )
    except Exception:
        # Never let logging raise.
        pass


def _build_default_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _detect_soft_block(html: str) -> bool:
    if not html:
        return False
    lower = html.lower()
    return any(tok in lower for tok in _SOFT_BLOCK_TOKENS)


def _missing_required(html: str, selectors: Optional[List[str]]) -> bool:
    if not selectors:
        return False
    soup = BeautifulSoup(html or "", "html.parser")
    for sel in selectors:
        if soup.select_one(sel):
            continue
        return True
    return False


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:  # pragma: no cover - exercised via tests
        raise PlaywrightUnavailable(str(exc)) from exc
    return sync_playwright, PlaywrightTimeoutError


def _ensure_context(job_id: str, persistent_profile_dir: Optional[str] = None) -> _JobBrowser:
    job_key = job_id or "global"
    cache_key = (job_key, persistent_profile_dir or "volatile")
    cached = _JOB_BROWSERS.get(cache_key)
    if cached:
        return cached

    sync_playwright, _ = _load_playwright()
    pw = sync_playwright().start()

    if persistent_profile_dir:
        os.makedirs(persistent_profile_dir, exist_ok=True)
        context = pw.chromium.launch_persistent_context(
            user_data_dir=persistent_profile_dir,
            headless=_PW_HEADLESS,
        )
        jb = _JobBrowser(playwright=pw, browser=None, context=context, pages_used=0)
    else:
        browser = pw.chromium.launch(headless=_PW_HEADLESS)
        context = browser.new_context()
        jb = _JobBrowser(playwright=pw, browser=browser, context=context, pages_used=0)

    _JOB_BROWSERS[cache_key] = jb
    if os.getenv("HTML_FETCHER_DEBUG_PERSIST") == "1" and persistent_profile_dir:
        try:
            print(
                f"[html_fetcher] persistent context initialized dir={persistent_profile_dir} job={job_key}"
            )
        except Exception:
            pass
    return jb


def close_job_browser(job_id: Optional[str]) -> None:
    job_key = job_id or "global"
    keys = [key for key in list(_JOB_BROWSERS.keys()) if key[0] == job_key]
    for key in keys:
        jb = _JOB_BROWSERS.pop(key, None)
        if not jb:
            continue
        try:
            jb.context.close()
        except Exception:
            pass
        try:
            if jb.browser:
                jb.browser.close()
        except Exception:
            pass
        try:
            jb.playwright.stop()
        except Exception:
            pass


def close_all_browsers() -> None:
    job_ids = {key[0] for key in list(_JOB_BROWSERS.keys())}
    for job_id in list(job_ids):
        close_job_browser(job_id)


atexit.register(close_all_browsers)


def _playwright_fetch_impl(
    url: str,
    job_id: Optional[str],
    timeout_s: float,
    *,
    persistent_profile_dir: Optional[str] = None,
    page_handler: Optional[object] = None,
) -> Dict[str, Optional[str]]:
    jb = _ensure_context(job_id, persistent_profile_dir)
    if jb.pages_used >= _PW_MAX_PAGES:
        raise PlaywrightUnavailable("page_budget_exhausted")

    page = jb.context.new_page()
    jb.pages_used += 1
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        if page_handler:
            try:
                page_handler(page)
            except Exception:
                # Do not fail fetch on handler issues; proceed with current page state.
                pass
        html = page.content()
        final_url = page.url
        return {"html": html, "final_url": final_url}
    finally:
        try:
            page.close()
        except Exception:
            pass


def _playwright_fetch(
    url: str,
    job_id: Optional[str],
    timeout_s: float,
    *args,
    **kwargs,
) -> Dict[str, Optional[str]]:
    """
    Backward-compatible wrapper to tolerate legacy monkeypatches that pass only
    (url, job_id, timeout_s) without keyword args.
    """
    try:
        return _playwright_fetch_impl(url, job_id, timeout_s, *args, **kwargs)
    except TypeError:
        # Likely a monkeypatch with older signature; retry without kwargs.
        return _playwright_fetch_impl(url, job_id, timeout_s)


def fetch_html(
    url: str,
    *,
    allow_browser_fallback: bool = True,
    directory: Optional[str] = None,
    job_id: Optional[str] = None,
    required_selectors: Optional[List[str]] = None,
    session: Optional[requests.Session] = None,
    timeout_s: float = 20,
    persistent_profile_dir: Optional[str] = None,
    page_handler: Optional[object] = None,
) -> Dict[str, object]:
    if not url:
        return {
            "status": None,
            "final_url": "",
            "html": "",
            "mode_used": "requests",
            "reason": "requests_error",
            "elapsed_ms": 0,
            "domain": "",
        }

    sess = session or _build_default_session()
    t0 = time.perf_counter()
    status = None
    html = ""
    final_url = url
    reason = "ok"
    mode_used = "requests"
    domain = urlparse(url).netloc.lower()

    try:
        resp = sess.get(url, timeout=timeout_s, allow_redirects=True)
        status = getattr(resp, "status_code", None)
        final_url = getattr(resp, "url", url)
        html = resp.text or ""
    except Exception:
        reason = "requests_error"
        status = None
        html = ""

    trigger_reason = None
    if status in {403, 406, 429, 503}:
        trigger_reason = f"status_{status}"
    elif _detect_soft_block(html):
        trigger_reason = "soft_block"
    elif _missing_required(html, required_selectors):
        trigger_reason = "missing_selectors"
    elif reason == "requests_error":
        trigger_reason = "requests_error"

    # If we got a successful requests fetch without triggers, short-circuit.
    if not trigger_reason:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _log_fetch(directory, mode_used, reason, domain, elapsed_ms, url, final_url)
        return {
            "status": status,
            "final_url": final_url,
            "html": html,
            "mode_used": mode_used,
            "reason": reason,
            "elapsed_ms": elapsed_ms,
            "domain": domain,
        }

    # Attempt Playwright fallback if enabled.
    if allow_browser_fallback and _PW_ENABLED:
        try:
            try:
                pw_result = _playwright_fetch(
                    url,
                    job_id,
                    timeout_s,
                    persistent_profile_dir=persistent_profile_dir,
                    page_handler=page_handler,
                )
            except TypeError:
                # Support legacy monkeypatches without kwarg support.
                pw_result = _playwright_fetch(url, job_id, timeout_s)
            html = pw_result.get("html", "")
            final_url = pw_result.get("final_url", final_url)
            mode_used = "playwright"
            reason = trigger_reason
            status = status  # keep original status for transparency
        except PlaywrightUnavailable:
            reason = "playwright_error"
            mode_used = "playwright"
        except Exception:
            reason = "playwright_error"
            mode_used = "playwright"

    if trigger_reason and reason == "ok":
        reason = trigger_reason

    if mode_used == "playwright" and html:
        status = 200
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    _log_fetch(directory, mode_used, reason, domain, elapsed_ms, url, final_url)
    if mode_used == "playwright" and os.getenv("PLAYWRIGHT_FALLBACK_DEBUG") == "1":
        print(
            f"[Playwright Fallback] directory={directory or ''} domain={domain} "
            f"reason={reason} status={status} ms={elapsed_ms} url={url}"
        )
    return {
        "status": status,
        "final_url": final_url,
        "html": html or "",
        "mode_used": mode_used,
        "reason": reason,
        "elapsed_ms": elapsed_ms,
        "domain": domain,
    }
