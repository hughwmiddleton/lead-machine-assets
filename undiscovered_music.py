"""Undiscovered Music discovery source for Lead Machine.

Public contract:
- scrape_undiscovered_music(target_count, params, logger) -> list[dict]
    Returns artist row dicts matching the existing raw-seed pipeline schema.

This module intentionally contains no Night Mode, CSV, GUI, or enrichment
concerns.  It produces rows; downstream machinery handles origin locking,
dedupe, enrichment, validation, and export.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from email_provenance import merge_email_provenance_json
from html_fetcher import fetch_html
from spotify_client import SpotifyClient
from spotify_latest_release import (
    SpotifyLatestReleaseEnricher,
    extract_spotify_artist_identity,
)

logger = logging.getLogger(__name__)

Row = Dict[str, Any]
LoggerFn = Optional[Callable[[str], None]]

SOURCE_KEY = "undiscovered_music"
USER_FACING_LABEL = "Undiscovered Music"
BASE_URL = "https://undiscovered.music"
DIRECTORY_URL = f"{BASE_URL}/artists"
PROFILE_URL_RE = re.compile(r"^https?://undiscovered\.music/artists/([^/?#]+)/?$")

# Conservative bounds
DEFAULT_MAX_RESULTS = 200
REQUEST_TIMEOUT_S = 20

# Signals used for qualification
_IDENTITY_SIGNALS = (
    "artist_name",
    "hometown_raw",
    "genre_primary",
    "website_url",
    "bio_text",
    "booking_contact_name",
    "booking_email",
    "upcoming_show_count",
)

# Junk heuristics
_JUNK_NAME_PATTERNS = (
    # Street addresses starting with numbers
    re.compile(r"^\d+\s+([a-z0-9]+\s+){0,3}(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|way|ct|court|pl|place|route|hwy|highway|pkwy|parkway|cir|circle|loop|trail)\b", re.I),
    # Suite/apartment/unit patterns
    re.compile(r"^\d+\s+([a-z]+\s+){0,2}(suite|unit|apt|apartment|#)\b", re.I),
    # Business entity shells
    re.compile(r"\b(llc|ltd|inc\b|corp\b|company|enterprises|group\s+llc)\b", re.I),
    # Venue/business types
    re.compile(r"\b(venue|hall|theater|theatre|arena|stadium|club|bar\s+&\s+grill|restaurant|cafe|pub)\b", re.I),
)

# Minimum credible identity threshold
_MIN_CREDIBLE_SIGNALS = 1

_NATIVE_SOCIAL_HOSTS = (
    "bandcamp.com",
    "facebook.com",
    "instagram.com",
    "soundcloud.com",
    "spotify.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtu.be",
    "youtube.com",
)

_SOCIAL_SHARE_PATH_PREFIXES = (
    "/dialog/share",
    "/intent/",
    "/share",
    "/sharer",
)


def _log(fn: LoggerFn, message: str) -> None:
    if not fn or not message:
        return
    try:
        fn(message)
    except Exception:
        pass


def canonicalize_undiscovered_profile_url(value: str) -> str:
    """Return a canonical Undiscovered Music artist profile URL, or empty string."""
    candidate = (value or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate.lstrip("/")
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "undiscovered.music":
        return ""
    path = (parsed.path or "").rstrip("/")
    if not path.startswith("/artists/"):
        return ""
    if parsed.query or parsed.fragment:
        return ""
    segments = [s for s in path.split("/") if s]
    if len(segments) != 2:
        return ""
    slug = segments[1]
    if not slug or slug in {"artists", "search", "tags", "genres", "events", "venues"}:
        return ""
    return f"https://undiscovered.music/artists/{slug}"


def is_valid_artist_profile_url(url: str) -> bool:
    """Return True only for canonical artist profile URLs."""
    return bool(canonicalize_undiscovered_profile_url(url))


def _extract_profile_links(html: str, base: str = BASE_URL) -> List[str]:
    """Extract and normalize artist profile links from directory HTML."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen: Set[str] = set()
    results: List[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if not href:
            continue
        absolute = urljoin(base, href)
        canonical = canonicalize_undiscovered_profile_url(absolute)
        if not canonical:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        results.append(canonical)
    return results


def discover_artist_urls(max_results: int = DEFAULT_MAX_RESULTS, logger_fn: LoggerFn = None) -> List[str]:
    """Fetch the /artists directory and return deduped canonical profile URLs."""
    _log(logger_fn, f"[Undiscovered Music] Fetching directory: {DIRECTORY_URL}")
    try:
        result = fetch_html(DIRECTORY_URL, directory=SOURCE_KEY, timeout_s=REQUEST_TIMEOUT_S)
    except Exception as exc:
        _log(logger_fn, f"[Undiscovered Music] Directory fetch failed: {exc}")
        return []

    html = result.get("html") or ""
    reason = result.get("reason", "unknown")
    if not html or result.get("status") in (403, 429, 503):
        _log(logger_fn, f"[Undiscovered Music] Directory blocked or empty (reason={reason}, status={result.get('status')})")
        return []

    urls = _extract_profile_links(html)
    _log(logger_fn, f"[Undiscovered Music] Discovered {len(urls)} profile URLs")
    if max_results and len(urls) > max_results:
        urls = urls[:max_results]
        _log(logger_fn, f"[Undiscovered Music] Trimmed to max_results={max_results}")
    return urls


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _extract_artist_name(soup: BeautifulSoup, fallback_slug: str = "") -> str:
    # Prefer og:title or h1
    for sel in ["meta[property='og:title']", "meta[name='twitter:title']", "meta[name='title']"]:
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            name = _norm_text(tag["content"])
            if name:
                # og:title may be "Artist Name | Undiscovered Music"
                if "|" in name:
                    name = name.split("|")[0].strip()
                return name
    h1 = soup.find("h1")
    if h1:
        name = _norm_text(h1.get_text(" ", strip=True))
        if name:
            return name
    # JSON-LD fallback
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = __import__("json").loads(script.string or "")
            if isinstance(data, dict):
                candidate = data.get("name") or data.get("headline", "")
                if candidate:
                    return _norm_text(candidate)
        except Exception:
            pass
    if fallback_slug:
        return fallback_slug.replace("-", " ").title()
    return ""


def _extract_label_value(soup: BeautifulSoup, label_text: str) -> str:
    """Find a <label> or <strong> with label_text and return the adjacent text value."""
    for tag in soup.find_all(["label", "strong", "span", "div", "p"]):
        tag_text = _norm_text(tag.get_text(" ", strip=True)).rstrip(":").lower()
        if tag_text == label_text.lower():
            # If tag itself is a container (div/p), use its text directly
            if tag.name in {"div", "p", "section", "li"}:
                full_text = _norm_text(tag.get_text(" ", strip=True))
                cleaned = re.sub(r"(?i)\b" + re.escape(label_text) + r"[:\s]*", "", full_text).strip(", ")
                if cleaned and cleaned.lower() != label_text.lower():
                    return cleaned
            else:
                # For inline tags (label/strong/span), look at parent
                parent = tag.parent
                if parent and parent.name in {"div", "p", "section", "li"}:
                    full_text = _norm_text(parent.get_text(" ", strip=True))
                    cleaned = re.sub(r"(?i)\b" + re.escape(label_text) + r"[:\s]*", "", full_text).strip(", ")
                    if cleaned and cleaned.lower() != label_text.lower():
                        return cleaned
                # Fallback: next sibling
                nxt = tag.find_next_sibling()
                if nxt:
                    val = _norm_text(nxt.get_text(" ", strip=True) if hasattr(nxt, "get_text") else str(nxt))
                    if val:
                        return val
    return ""


def _extract_hometown(soup: BeautifulSoup) -> str:
    val = _extract_label_value(soup, "hometown")
    if val and 2 < len(val) < 120:
        return val
    for label in ["location", "based in", "from"]:
        val = _extract_label_value(soup, label)
        if val and 2 < len(val) < 120:
            return val
    return ""


def _extract_genres(soup: BeautifulSoup) -> Tuple[str, str]:
    """Return (primary_genre, all_genres_string)."""
    val = _extract_label_value(soup, "genres")
    if not val:
        val = _extract_label_value(soup, "genre")
    if val:
        # Guard: if value contains other known labels, it's likely cross-contamination
        if any(kw in val.lower() for kw in ("hometown:", "website:", "booking contact")):
            return "", ""
        parts = [p.strip() for p in re.split(r"[/,•\\-]", val) if p.strip()]
        primary = parts[0].title() if parts else ""
        return primary, val
    return "", ""


def _extract_website(soup: BeautifulSoup) -> str:
    val = _extract_label_value(soup, "website")
    if val:
        # Guard against cross-contamination
        if any(kw in val.lower() for kw in ("hometown:", "genres:", "booking contact")):
            val = val.split("Hometown:")[0].split("Genres:")[0].split("Booking Contact")[0].strip()
        # val may be raw text like "www.example.com" or may contain an <a> tag text
        url_match = re.search(r"https?://[^\s<>\"']+", val)
        if url_match:
            return url_match.group(0)
        if val.startswith("www."):
            return "https://" + val
    # Fallback: any external link that isn't social
    social_hosts = ("facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
                    "youtube.com", "spotify.com", "soundcloud.com", "bandcamp.com")
    for tag in soup.find_all("a", href=True):
        href = _norm_text(tag["href"])
        if not href or not href.startswith("http"):
            continue
        host = urlparse(href).hostname or ""
        if any(h in host for h in social_hosts):
            continue
        if "/undiscovered.music" not in href:
            return href
    return ""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _extract_native_social_links(soup: BeautifulSoup) -> Tuple[List[str], str]:
    """Return source-native profile links and the first SoundCloud URL.

    Undiscovered renders artist-owned social fields inside ``#social-links``.
    Restricting extraction to that field prevents the site's own social link,
    navigation, and sharing controls from being attributed to the artist.
    """
    links: List[str] = []
    soundcloud_url = ""

    for tag in soup.select("#social-links a[href]"):
        href = _norm_text(tag.get("href"))
        if not href:
            continue
        absolute = urljoin(BASE_URL, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not host or _host_matches(host, "undiscovered.music"):
            continue
        if not any(_host_matches(host, domain) for domain in _NATIVE_SOCIAL_HOSTS):
            continue
        path = (parsed.path or "").lower()
        if any(path.startswith(prefix) for prefix in _SOCIAL_SHARE_PATH_PREFIXES):
            continue
        if absolute in links:
            continue
        links.append(absolute)
        if not soundcloud_url and _host_matches(host, "soundcloud.com"):
            soundcloud_url = absolute

    return links, soundcloud_url


def _is_valid_email(candidate: str) -> bool:
    if not candidate:
        return False
    lower = candidate.strip().lower()
    if "[email" in lower or "email protected" in lower:
        return False
    if lower.endswith("@example.com") or lower.endswith("@test.com"):
        return False
    return bool(re.fullmatch(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+", lower, re.I))


def _decode_cloudflare_email(encoded: str) -> str:
    """Decode standard Cloudflare email-protection hex locally."""
    value = _norm_text(encoded)
    if len(value) < 4 or len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return ""
    try:
        key = int(value[:2], 16)
        decoded = bytes(int(value[idx:idx + 2], 16) ^ key for idx in range(2, len(value), 2)).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""
    decoded = decoded.strip().lower()
    return decoded if _is_valid_email(decoded) else ""


def _booking_contact_surfaces(soup: BeautifulSoup) -> List[Any]:
    """Return only native booking/management containers, never page chrome."""
    surfaces: List[Any] = []

    # Verified live structure: .card > .card-header + .card-body.
    for card in soup.select(".card"):
        header = card.find(class_="card-header", recursive=False)
        if header and _norm_text(header.get_text(" ", strip=True)).lower() == "booking contact":
            surfaces.append(card)

    # Retain the older inline form without reopening page-wide email scraping.
    for label_tag in soup.find_all(["strong", "label"]):
        label = _norm_text(label_tag.get_text(" ", strip=True)).rstrip(":").lower()
        if label not in {"booking", "bookings", "management", "booking contact"}:
            continue
        container = label_tag.find_parent(["p", "li", "section", "div"])
        if container and container not in surfaces:
            surfaces.append(container)

    return surfaces


def _extract_booking_info(soup: BeautifulSoup) -> Tuple[str, List[str]]:
    """Return (contact_name, emails) from artist booking surfaces only."""
    emails: List[str] = []
    contact_name = ""

    def _remember_email(candidate: str) -> None:
        normalized = (candidate or "").strip().lower()
        if _is_valid_email(normalized) and normalized not in emails:
            emails.append(normalized)

    for surface in _booking_contact_surfaces(soup):
        body = surface.select_one(":scope > .card-body") if "card" in (surface.get("class") or []) else None
        scoped = body or surface

        for tag in scoped.select("[data-cfemail]"):
            _remember_email(_decode_cloudflare_email(tag.get("data-cfemail", "")))

        for tag in scoped.select('a[href*="/cdn-cgi/l/email-protection#"]'):
            fragment = (tag.get("href") or "").split("#", 1)
            if len(fragment) == 2:
                _remember_email(_decode_cloudflare_email(fragment[1]))

        for tag in scoped.find_all("a", href=re.compile(r"^mailto:", re.I)):
            match = re.search(r"^mailto:([^?\s]+)", tag.get("href") or "", re.I)
            if match:
                _remember_email(match.group(1))

        scoped_text = scoped.get_text(" ", strip=True)
        for candidate in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", scoped_text):
            _remember_email(candidate)

        if contact_name:
            continue
        if body:
            for child in body.find_all("div", recursive=False):
                if child.select_one("[data-cfemail], a[href^='mailto:'], a[href*='/cdn-cgi/l/email-protection']"):
                    continue
                candidate = _norm_text(child.get_text(" ", strip=True))
                if candidate and not re.search(r"@|email\s*protected", candidate, re.I):
                    contact_name = candidate
                    break
        else:
            candidate = _norm_text(scoped_text)
            label_text = _norm_text(surface.find(["strong", "label"]).get_text(" ", strip=True))
            candidate = re.sub(r"^" + re.escape(label_text) + r"\s*", "", candidate, flags=re.I)
            candidate = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "", candidate)
            candidate = re.sub(r"\[email\s+protected\]", "", candidate, flags=re.I)
            candidate = re.sub(r"^[\s:–—-]+|[\s:–—-]+$", "", candidate).strip(", ")
            if candidate:
                contact_name = candidate

    return contact_name, emails


def _extract_bio(soup: BeautifulSoup) -> str:
    for sel in ["meta[property='og:description']", "meta[name='description']", "meta[name='twitter:description']"]:
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            return _norm_text(tag["content"])
    for sel in [".bio", ".about", "[class*='bio']", "[class*='about']", "[class*='description']"]:
        tag = soup.select_one(sel)
        if tag:
            return _norm_text(tag.get_text(" ", strip=True))
    # Fallback: first substantial paragraph after h1
    h1 = soup.find("h1")
    if h1:
        for nxt in h1.find_all_next("p"):
            text = _norm_text(nxt.get_text(" ", strip=True))
            if len(text) > 15:
                return text
    return ""


def _extract_upcoming_shows(soup: BeautifulSoup) -> Tuple[int, str]:
    """Return (count, next_date_iso_or_empty)."""
    count = 0
    next_date = ""
    _NO_SHOW_TOKENS = ("don't currently know", "no upcoming", "no shows", "none scheduled")
    # Look for sections labeled upcoming/shows/tour
    for label in ["upcoming shows", "shows", "tour dates", "gigs", "events"]:
        for heading in soup.find_all(["h2", "h3", "h4"]):
            h_text = _norm_text(heading.get_text(" ", strip=True)).lower()
            if label in h_text:
                # Gather siblings between this heading and the next heading
                siblings: List[Any] = []
                for nxt in heading.find_next_siblings():
                    if getattr(nxt, "name", "") in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                        break
                    siblings.append(nxt)
                # Check for explicit "no shows" message
                sibling_text = " ".join(_norm_text(s.get_text(" ", strip=True)) for s in siblings)
                if any(tok in sibling_text.lower() for tok in _NO_SHOW_TOKENS):
                    count = 0
                    break
                # Count actual show items
                items = []
                for sib in siblings:
                    items.extend(sib.find_all("li", recursive=True))
                count = len(items)
                # Try to extract first date from sibling text
                date_match = re.search(r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b)", sibling_text, re.I)
                if date_match:
                    next_date = date_match.group(1)
                break
        if count > 0:
            break
    return count, next_date


def parse_artist_profile(html: str, profile_url: str) -> Dict[str, Any]:
    """Parse an Undiscovered Music artist profile page.

    Returns a dict with all requested fields.  Missing optional values are empty
    strings so downstream consumers see neutral absence rather than None.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    slug_match = PROFILE_URL_RE.match(profile_url or "")
    fallback_slug = slug_match.group(1) if slug_match else ""

    artist_name = _extract_artist_name(soup, fallback_slug)
    hometown_raw = _extract_hometown(soup)
    genre_primary, genres = _extract_genres(soup)
    website_url = _extract_website(soup)
    social_urls, soundcloud_url = _extract_native_social_links(soup)
    spotify_values = [url for url in social_urls if "spotify.com" in url.lower()]
    spotify_artist_id = ""
    spotify_url = ""
    for value in spotify_values:
        spotify_artist_id, spotify_url = extract_spotify_artist_identity(value)
        if spotify_artist_id:
            break
    bio_text = _extract_bio(soup)
    booking_contact_name, booking_emails = _extract_booking_info(soup)
    booking_email = booking_emails[0] if booking_emails else ""
    upcoming_show_count, next_upcoming_show_date = _extract_upcoming_shows(soup)

    return {
        "artist_name": artist_name,
        "hometown_raw": hometown_raw,
        "genre_primary": genre_primary,
        "genres": genres,
        "website_url": website_url,
        "social_urls": social_urls,
        "soundcloud_url": soundcloud_url,
        "spotify_artist_id": spotify_artist_id,
        "spotify_url": spotify_url,
        "spotify_identity_status": (
            "valid" if spotify_artist_id else "malformed" if spotify_values else "missing"
        ),
        "profile_url": profile_url,
        "booking_contact_name": booking_contact_name,
        "booking_email": booking_email,
        "booking_emails": booking_emails,
        "upcoming_show_count": upcoming_show_count,
        "next_upcoming_show_date": next_upcoming_show_date,
        "bio_text": bio_text,
    }


def _is_junk_name(name: str) -> bool:
    if not name:
        return True
    for pattern in _JUNK_NAME_PATTERNS:
        if pattern.search(name):
            return True
    return False


def qualify_artist_profile(profile: Dict[str, Any]) -> bool:
    """Return True if the profile represents a credible artist lead.

    Requires at least one meaningful music-specific signal and must not match
    obvious junk patterns (street address, venue shell, etc.).
    """
    name = (profile.get("artist_name") or "").strip()
    if not name or len(name) < 1:
        return False
    if _is_junk_name(name):
        return False

    signals = 0
    if profile.get("hometown_raw"):
        signals += 1
    if profile.get("genre_primary"):
        signals += 1
    if profile.get("website_url"):
        signals += 1
    if profile.get("bio_text"):
        signals += 1
    if profile.get("booking_contact_name") or profile.get("booking_email"):
        signals += 1
    if profile.get("upcoming_show_count", 0) > 0:
        signals += 1

    return signals >= _MIN_CREDIBLE_SIGNALS


def _build_row(profile: Dict[str, Any], timestamp: str) -> Row:
    """Map a parsed profile dict into the existing Lead Machine raw-row schema."""
    artist_name = profile.get("artist_name") or ""
    hometown = profile.get("hometown_raw") or ""
    genre_primary = profile.get("genre_primary") or ""
    genres = profile.get("genres") or ""
    website = profile.get("website_url") or ""
    social_urls = profile.get("social_urls") or []
    soundcloud_url = profile.get("soundcloud_url") or ""
    profile_url = profile.get("profile_url") or ""
    booking_name = profile.get("booking_contact_name") or ""
    booking_email = profile.get("booking_email") or ""
    booking_emails = profile.get("booking_emails") or ([booking_email] if booking_email else [])
    upcoming_count = profile.get("upcoming_show_count", 0)
    next_show = profile.get("next_upcoming_show_date") or ""

    # External Links: start with website if present
    external_links = website
    social_link = " | ".join(social_urls)

    # Email provenance fields
    email = ""
    email_source_url = ""
    email_source_type = ""
    email_extract_method = ""
    email_provenance_json = ""

    if booking_email:
        email = booking_email
        email_source_url = profile_url
        email_source_type = "undiscovered_music_profile"
        email_extract_method = "profile_direct"
        email_provenance_json = merge_email_provenance_json(
            "",
            booking_emails,
            source_url=profile_url,
            source_type=email_source_type,
            method=email_extract_method,
            surface="undiscovered_music_profile",
        )

    row: Row = {
        "Artist Name": artist_name,
        "Location": hometown,
        "Song Title": "",
        "Sounds Like": "",
        "Social Link": social_link,
        "SoundCloud Link": soundcloud_url,
        "Played on triple J": "",
        "Played on Unearthed": "",
        "Release Date": "",
        "Primary Genre": genre_primary,
        "Date Added": timestamp,
        "External Links": external_links,
        "Email": email,
        "Email_All": ";".join(booking_emails),
        "Email_Source_URL": email_source_url,
        "Email_Source_Type": email_source_type,
        "Email_Extract_Method": email_extract_method,
        "Email_Provenance_JSON": email_provenance_json,
        "Lead_Source": USER_FACING_LABEL,
        "Source_Directory": SOURCE_KEY,
        "Source Directory": USER_FACING_LABEL,
        "Source URL": profile_url,
        "Source_URL": profile_url,
        "Undiscovered_Genre_Raw": genres,
        "Upcoming_Show_Count": str(upcoming_count) if upcoming_count else "",
        "Next_Upcoming_Show_Date": next_show,
        "Booking_Contact_Name": booking_name,
    }

    return row


def scrape_undiscovered_music(
    target_count: int = 0,
    params: Optional[Dict[str, Any]] = None,
    logger_fn: LoggerFn = None,
) -> List[Row]:
    """Main entry point for Undiscovered Music discovery.

    Args:
        target_count: Maximum accepted artist rows to return. 0 means default cap.
        params: Optional dict with keys like `max_results`, `url`, etc.
        logger_fn: Optional logging callable.

    Returns:
        List of row dicts ready for the raw-seed pipeline.
    """
    params = params or {}
    max_results = int(target_count or params.get("max_results") or params.get("target_count") or DEFAULT_MAX_RESULTS)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _log(logger_fn, f"[Undiscovered Music] Starting discovery target={max_results}")

    # Rejections and fetch failures must not consume the operator's accepted-row
    # target, so qualification needs access to the complete candidate pool.
    urls = discover_artist_urls(max_results=0, logger_fn=logger_fn)
    if not urls:
        _log(logger_fn, "[Undiscovered Music] No profile URLs discovered")
        return []

    rows: List[Row] = []
    accepted = 0
    rejected = 0
    failed = 0
    attempted = 0
    spotify_enricher: Optional[SpotifyLatestReleaseEnricher] = None
    spotify_unavailable = False
    valid_spotify_identities = 0
    malformed_spotify_identities = 0

    for idx, url in enumerate(urls, start=1):
        attempted += 1
        try:
            _log(logger_fn, f"[Undiscovered Music] ({idx}/{len(urls)}) Fetching {url}")
            result = fetch_html(url, directory=SOURCE_KEY, timeout_s=REQUEST_TIMEOUT_S)
            html = result.get("html") or ""
            if not html or result.get("status") in (403, 429, 503):
                _log(logger_fn, f"[Undiscovered Music] Skipped {url} (blocked/empty)")
                failed += 1
                continue

            profile = parse_artist_profile(html, url)
            if not qualify_artist_profile(profile):
                _log(logger_fn, f"[Undiscovered Music] Rejected {url} (insufficient identity signals)")
                rejected += 1
                continue

            row = _build_row(profile, timestamp)
            spotify_artist_id = str(profile.get("spotify_artist_id") or "")
            spotify_status = profile.get("spotify_identity_status")
            if spotify_status == "malformed":
                malformed_spotify_identities += 1
            if spotify_artist_id:
                valid_spotify_identities += 1
                if spotify_enricher is None and not spotify_unavailable:
                    try:
                        spotify_enricher = SpotifyLatestReleaseEnricher(
                            SpotifyClient(
                                client_id=params.get("spotify_client_id"),
                                client_secret=params.get("spotify_client_secret"),
                                logger=logger_fn,
                            ),
                            logger=logger_fn,
                        )
                    except Exception as exc:
                        spotify_unavailable = True
                        _log(logger_fn, f"[Spotify Latest Release] unavailable: {exc}")
                if spotify_enricher is not None:
                    latest = spotify_enricher.lookup(spotify_artist_id)
                    if not str(row.get("Song Title") or "").strip():
                        row["Song Title"] = latest.song_title
                    if not str(row.get("Release Date") or "").strip():
                        row["Release Date"] = latest.release_date
            rows.append(row)
            accepted += 1
            if max_results and accepted >= max_results:
                _log(logger_fn, f"[Undiscovered Music] Reached target_count={max_results}")
                break
        except Exception as exc:
            _log(logger_fn, f"[Undiscovered Music] Error processing {url}: {exc}")
            failed += 1
            continue

    _log(
        logger_fn,
        f"[Undiscovered Music] Complete: attempted={attempted}, accepted={accepted}, "
        f"rejected={rejected}, failed={failed}, total={len(rows)}",
    )
    _log(
        logger_fn,
        "[Spotify Latest Release] "
        f"valid_identities={valid_spotify_identities}, "
        f"successful_lookups={spotify_enricher.successful_lookups if spotify_enricher else 0}, "
        f"malformed_identities={malformed_spotify_identities}, "
        f"lookup_failures={spotify_enricher.lookup_failures if spotify_enricher else 0}",
    )
    return rows
