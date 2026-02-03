"""Generic website -> email enrichment for Lead Machine rows."""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

Row = Dict[str, str]
LoggerFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]

DEBUG_EMAIL_SMEAR = False  # Set True to log suspected repeat-email smearing.
_email_seen_counter: Dict[str, int] = {}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SOCIAL_DOMAINS = {
    "instagram.com",
    "instagr.am",
    "facebook.com",
    "fb.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "spotify.com",
    "music.apple.com",
    "soundcloud.com",
    "bandcamp.com",
    "linktr.ee",
    "beacons.ai",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 10
CONTACT_KEYWORDS = ("contact", "about", "connect", "book", "booking")
MAX_CONTACT_FOLLOWUPS = 2
GENERIC_PATH_KEYWORDS = ("/contact", "/about", "/support", "/privacy", "/terms", "/imprint", "/legal")


def enrich_rows_with_website_emails(
    rows: List[Row],
    logger: Optional[LoggerFn] = None,
    progress_callback: Optional[ProgressFn] = None,
) -> List[Row]:
    if not rows:
        return rows

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        _ensure_email_fields(row)
        websites = _collect_candidate_websites(row)
        if logger:
            logger(f"[WebsiteEmail] Row {idx}/{total}: {len(websites)} website candidates")
        emails_found: List[str] = []
        email_types: Dict[str, str] = {}
        email_source_url = ""
        seen_urls: Set[str] = set()
        for site in websites:
            normalized = _normalize_url(site)
            if not normalized or normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            try:
                html = _fetch_html(session, normalized)
            except Exception as exc:
                if logger:
                    logger(f"[WebsiteEmail] Failed to fetch {normalized}: {exc}")
                continue
            emails = _extract_emails_from_html(html)
            if not emails:
                emails, follow_url = _follow_contact_links_and_extract(session, html, normalized, logger)
                if emails and follow_url:
                    email_source_url = follow_url
            for email in emails:
                if email not in emails_found:
                    emails_found.append(email)
                    email_types[email] = _classify_email(email)
            if emails_found:
                if not email_source_url:
                    email_source_url = normalized
                break  # stop at first successful site

        if emails_found and not row.get("Email"):
            row["Email_Source_URL"] = email_source_url
            path_lower = urlparse(email_source_url or "").path.lower()
            generic_hit = any(token in path_lower for token in GENERIC_PATH_KEYWORDS)
            if generic_hit:
                row["Email"] = ""
                row["Email_All"] = ";".join(emails_found)
                row["Email_Type"] = email_types.get(emails_found[0], "")
                row["Email Source"] = "Seed directory (generic contact page)"
                row["Needs_Review"] = "TRUE"
            else:
                primary = _choose_primary_email(emails_found, email_types)
                row["Email"] = primary
                row["Email_All"] = ";".join(emails_found)
                row["Email_Type"] = email_types.get(primary, "")
                row["Email Source"] = "Seed directory (site/email scrape)"

            # Debug/logging for repeated emails across different artists to catch smearing.
            for email in emails_found:
                normalized_email = email.strip().lower()
                if not normalized_email:
                    continue
                _email_seen_counter[normalized_email] = _email_seen_counter.get(normalized_email, 0) + 1
                if DEBUG_EMAIL_SMEAR and _email_seen_counter[normalized_email] > 1:
                    artist_name = str(row.get("Artist Name") or row.get("artist_name") or "").strip()
                    source_url = email_source_url or normalized
                    logger_fn = logger or (lambda _: None)
                    logger_fn(
                        f"[EmailSmear?] repeat_email={normalized_email} artist={artist_name!r} "
                        f"source_url={source_url} email_source=seed_directory "
                        f"emails_found={emails_found}"
                    )

        _emit_progress(progress_callback, idx, total)

    return rows


def _ensure_email_fields(row: Row) -> None:
    row.setdefault("Email", "")
    row.setdefault("Email_All", "")
    row.setdefault("Email_Type", "")
    row.setdefault("Email_Source_URL", "")
    row.setdefault("Needs_Review", "")


def _collect_candidate_websites(row: Row) -> List[str]:
    candidates: List[str] = []
    preferred = row.get("Spotify_Website_URL") or row.get("Spotify_URL")
    if preferred and not _is_social_url(preferred):
        candidates.append(preferred)
    fields = [row.get("External Links"), row.get("Social Link"), row.get("Website")]
    for field in fields:
        if not field:
            continue
        parts = re.split(r"[;,\s]+", field)
        for part in parts:
            url = part.strip()
            if not url or _is_social_url(url):
                continue
            candidates.append(url)
    return candidates


def _is_social_url(url: str) -> bool:
    parsed = urlparse(url)
    domain = (parsed.netloc or parsed.path or "").lower()
    domain = domain.strip()
    return any(domain.endswith(block) for block in SOCIAL_DOMAINS)


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    elif not urlparse(url).scheme:
        url = f"https://{url}"
    return url


def _fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text or ""


def _extract_emails_from_html(html: str) -> List[str]:
    if not html:
        return []
    emails = EMAIL_RE.findall(html)
    deduped: List[str] = []
    seen: Set[str] = set()
    for email in emails:
        normalized = email.strip().lower()
        if normalized not in seen:
            seen.add(normalized)
            deduped.append(email.strip())
    return deduped


def _follow_contact_links_and_extract(
    session: requests.Session,
    html: str,
    base_url: str,
    logger: Optional[LoggerFn],
) -> Tuple[List[str], str]:
    if not html:
        return ([], "")
    emails: List[str] = []
    source_url = ""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        text = (anchor.get_text(" ", strip=True) or "").lower()
        if not href:
            continue
        candidate = href.lower()
        if not any(keyword in candidate for keyword in CONTACT_KEYWORDS) and not any(keyword in text for keyword in CONTACT_KEYWORDS):
            continue
        normalized = urljoin(base_url, href)
        if urlparse(normalized).netloc != base_domain:
            continue
        links.append(normalized)
        if len(links) >= MAX_CONTACT_FOLLOWUPS:
            break

    for link in links:
        try:
            follow_html = _fetch_html(session, link)
        except Exception as exc:
            if logger:
                logger(f"[WebsiteEmail] Contact fetch failed {link}: {exc}")
            continue
        new_emails = _extract_emails_from_html(follow_html)
        for email in new_emails:
            if email not in emails:
                emails.append(email)
        if new_emails and not source_url:
            source_url = link
        if emails:
            break
    return (emails, source_url)


def _classify_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    if any(token in local for token in ("book", "booking")):
        return "booking"
    if any(token in local for token in ("mgmt", "manager", "management")):
        return "management"
    if any(token in local for token in ("press", "pr", "media")):
        return "press"
    if "label" in local:
        return "label"
    return "general"


def _choose_primary_email(emails: Sequence[str], email_types: Dict[str, str]) -> str:
    priority = {"booking": 0, "management": 1, "press": 2, "label": 3, "general": 4}
    best = emails[0]
    best_score = priority.get(email_types.get(best, "general"), 5)
    for email in emails[1:]:
        score = priority.get(email_types.get(email, "general"), 5)
        if score < best_score:
            best = email
            best_score = score
    return best


def _emit_progress(callback: Optional[ProgressFn], current: int, total: int) -> None:
    if not callback:
        return
    try:
        callback(current, total)
    except Exception:
        pass
