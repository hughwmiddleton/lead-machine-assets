"""Night-Mode-only Facebook enrichment helpers.

This module isolates Night Mode tweaks so daytime paths stay unchanged.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup

try:
    import facebook_enrich
except Exception:  # pragma: no cover - defensive import
    facebook_enrich = None  # type: ignore

LoggerFn = Optional[Callable[[str], None]]

EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def _log(logger: LoggerFn, message: str) -> None:
    if not message:
        return
    if logger:
        try:
            logger(message)
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
    cleaned = cleaned.split("?", 1)[0].rstrip("/")
    return cleaned


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


def _parse_search_candidates(html: str) -> List["facebook_enrich.FbCandidate"]:
    if facebook_enrich is None or not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("a[href]") if soup else []
    candidates: List[facebook_enrich.FbCandidate] = []
    for anchor in anchors:
        href = anchor.get("href") or ""
        if "facebook.com" not in href:
            continue
        if "search/" in href and "facebook.com/search/" in href:
            continue
        name = anchor.get_text(" ", strip=True) or href
        category = facebook_enrich.extract_fb_category(anchor, page_name=name) if hasattr(facebook_enrich, "extract_fb_category") else ""
        candidates.append(facebook_enrich.FbCandidate(name=name, url=href, category=category or ""))
    return _dedupe_candidates(candidates)


def _select_best_candidate_loose(artist_name: str, candidates: List["facebook_enrich.FbCandidate"]) -> Optional["facebook_enrich.FbCandidate"]:
    if facebook_enrich is None or not candidates:
        return candidates[0] if candidates else None
    primary, *_ = candidates, None
    try:
        best, *_ = facebook_enrich.select_best_fb_candidate(artist_name, candidates)
    except Exception:
        best = None
    if best:
        return best
    for cand in candidates:
        cat = (cand.category or "").lower()
        if any(tok in cat for tok in ("artist", "musician", "musician/band", "band", "music", "dj", "producer")):
            return cand
    return primary


@dataclass
class NightModeFacebookResult:
    email: Optional[str] = None
    email_all: str = ""
    email_type: str = "fb_night"
    facebook_url: str = ""


class NightModeFacebookEnricher:
    """
    Night-Mode-only Facebook enricher that:
      - Prefers direct URLs when present.
      - Uses a looser music gate for search candidates.
      - Extracts emails via regex + mailto scanning.
    """

    def __init__(self, legacy_module, username: str, password: str, logger: LoggerFn = None) -> None:
        self.legacy = legacy_module
        self.username = username
        self.password = password
        self.logger = logger
        self.session = None
        self._owns_session = False

    def __enter__(self) -> "NightModeFacebookEnricher":
        self._ensure_session()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_session(self):
        if self.session is not None:
            return self.session
        if not self.username or not self.password:
            _log(self.logger, "[Night FB] Missing FB credentials; running without live session.")
            return None
        try:
            get_shared = getattr(self.legacy, "get_shared_facebook_session", None)
            if callable(get_shared):
                self.session = get_shared(self.username, self.password, logger=self.logger)
                self._owns_session = False
            else:
                manager_cls = getattr(self.legacy, "FacebookSessionManager", None)
                driver_factory = getattr(self.legacy, "setup_facebook_driver", None)
                if manager_cls and driver_factory:
                    self.session = manager_cls(self.username, self.password, driver_factory, logger=self.logger)
                    self._owns_session = True
        except Exception as exc:  # pragma: no cover - defensive
            _log(self.logger, f"[Night FB] Failed to start Facebook session: {exc}")
            self.session = None
        return self.session

    def close(self) -> None:
        if self.session and self._owns_session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None

    def _fetch_html(self, url: str) -> Optional[str]:
        session = self._ensure_session()
        if not session:
            return None
        try:
            driver = session.navigate(url)
            goto_about = getattr(self.legacy, "_goto_facebook_about", None)
            if callable(goto_about):
                try:
                    goto_about(driver, url, timeout=5.0)
                except Exception:
                    pass
            time.sleep(1.0)
            return driver.page_source
        except Exception as exc:  # pragma: no cover - defensive
            _log(self.logger, f"[Night FB] Failed to fetch FB HTML for {url}: {exc}")
            return None

    def _search_for_page(self, artist: str, location: str) -> Optional[str]:
        session = self._ensure_session()
        if not session:
            return None
        query = " ".join(part for part in (artist, location) if part).strip()
        if not query:
            return None
        encoded = urllib.parse.quote_plus(query)
        search_url = f"https://www.facebook.com/search/pages/?q={encoded}"
        _log(self.logger, f"[Night FB] Searching Facebook for '{query}' -> {search_url}")
        try:
            driver = session.navigate(search_url)
            time.sleep(1.5)
            html = driver.page_source
        except Exception as exc:  # pragma: no cover - defensive
            _log(self.logger, f"[Night FB] Search navigation failed: {exc}")
            return None
        candidates = _parse_search_candidates(html)
        best = _select_best_candidate_loose(artist, candidates)
        if best:
            url = _normalise_fb_url(best.url)
            _log(self.logger, f"[Night FB] Selected FB candidate '{best.name}' -> {url} (category='{best.category}')")
            return url
        _log(self.logger, "[Night FB] No viable FB candidates from search.")
        return None

    def _build_result(self, emails: List[str], email_all_existing: str, facebook_url: str, artist_name: str) -> Optional[NightModeFacebookResult]:
        if not emails:
            return None
        primary = _choose_primary_email(emails, artist_name)
        merged_all = _merge_email_all(email_all_existing, emails)
        email_type = "fb_night"
        return NightModeFacebookResult(
            email=primary,
            email_all=merged_all,
            email_type=email_type,
            facebook_url=facebook_url,
        )

    def enrich_row_with_facebook_night(self, row: Dict[str, str]) -> Dict[str, str]:
        """Night-Mode-only FB enrichment for a single row."""
        result = dict(row or {})

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
            return result
        artist_name = _clean_val(result.get("Artist Name", ""))
        location = _clean_val(result.get("Location", ""))
        facebook_url = _normalise_fb_url(_clean_val(result.get("Facebook_URL", "")))
        if not facebook_url:
            facebook_url = _parse_existing_fb_url(result)
            facebook_url = _normalise_fb_url(facebook_url)
        page_url = facebook_url or self._search_for_page(artist_name, location)
        if not page_url:
            return result
        if facebook_url:
            _log(self.logger, f"[Night FB] using direct Facebook_URL for {artist_name or '<unknown>'} -> {page_url}")
        html = self._fetch_html(page_url)
        emails = _extract_emails_from_html(html or "")
        night_result = self._build_result(emails, str(result.get("Email_All", "") or ""), page_url, artist_name)
        if night_result:
            result["Email"] = night_result.email or result.get("Email", "")
            result["Email_All"] = night_result.email_all
            result["Email_Type"] = night_result.email_type
            if night_result.facebook_url:
                result["Facebook_URL"] = night_result.facebook_url
            _log(self.logger, f"[Night FB] extracted email(s) {emails} from {page_url}")
        return result
