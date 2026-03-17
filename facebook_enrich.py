"""Shared helpers for Facebook candidate parsing and scoring."""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.parse
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

# Blocks of common FB UI/notification text that should be ignored entirely.
NOISY_FB_TOKENS = [
    "Unread",
    "Mark as read",
    "Notifications",
    "Out with the In",
    "Home ·",
    "menu",
    "search",
    "mentioned you in a post",
    "news feed",
    "create post",
    "stories",
]

# Non-music creator labels that should be treated as noise or rejected.
FB_CREATOR_CATEGORY_TOKENS = (
    "reel creator",
    "page · reel creator",
    "page·reel creator",
)

# NOTE (2025-12-08): Music role/category detection was originally English-only
# (e.g., "Musician/band"). We now normalise and whitelist common translations
# so non-English aria-label/category strings are accepted. See
# MUSICIAN_ROLE_KEYWORDS and normalize_role_text()/normalise_role_text().
# Tokens that indicate a music-related Facebook page.
MUSIC_CATEGORY_KEYWORDS = (
    "musician",
    "musician/band",
    "artist",
    "band",
    "music",
    "singer",
    "rapper",
    "dj",
    "producer",
    "recording artist",
    "record label",
    "composer",
    "songwriter",
)

# Normalised, curated set of musician-role/category labels (accent-free).
MUSICIAN_ROLE_KEYWORDS: set[str] = {
    # English
    "musician",
    "musician/band",
    "artist",
    "band",
    "music artist",
    "music band",
    "music group",
    "singer",
    "singer-songwriter",
    "singer / songwriter",
    "rapper",
    "vocalist",
    "songwriter",
    "music producer",
    "producer",
    "dj",
    "disc jockey",
    "recording artist",
    "recording musician",
    "composer",
    "performer",

    # Spanish (accent-stripped) / Portuguese
    "musico",
    "musico/banda",
    "banda",
    "artista",
    "artista musical",
    "cantante",
    "cantautor",
    "cantautora",
    "rapero",
    "rapera",
    "productor musical",
    "grupo musical",
    "cantor",
    "cantora",
    "cantor-compositor",

    # French
    "musicien",
    "musicienne",
    "musicien/groupe",
    "groupe",
    "groupe musical",
    "artiste",
    "artiste musical",
    "chanteur",
    "chanteuse",
    "auteur-compositeur",
    "rappeur",
    "producteur musical",

    # German
    "musiker",
    "musikerin",
    "kunstler",
    "kunstlerin",
    "sanger",
    "sangerin",
    "musikproduzent",
    "musikgruppe",

    # Italian
    "musicista",
    "musicista/band",
    "gruppo musicale",
    "gruppo",
    "cantante",
    "cantautore",
    "cantautrice",
    "produttore musicale",

    # Dutch
    "muzikant",
    "muzikante",
    "artiest",
    "zanger",
    "zangeres",
    "muziekproducent",
    "muziekgroep",

    # Polish
    "muzyk",
    "artysta",
    "artystka",
    "wokalista",
    "wokalistka",
    "piosenkarz",
    "piosenkarka",
    "zespol",
    "zespol muzyczny",
    "producent muzyczny",

    # Scandinavian (Swedish/Norwegian/Danish)
    "musiker",
    "sanger",
    "sangerinde",
    "sangerinne",
    "musikproducent",
    "musikgruppe",
    "musikband",

    # Turkish (accent-stripped)
    "muzisyen",
    "sarkici",
    "sanatci",
    "muzik grubu",
    "rapci",

    # Russian / Slavic (kept in native form)
    "музыкант",
    "музыканты",
    "артист",
    "певец",
    "певица",
    "группа",
    "музыкальная группа",
    "рэпер",
    "вокалист",
    "музыкальный продюсер",

    # Chinese (romanised)
    "yinyueren",
    "geshou",
    "yuedui",
    "zuhe",
    "shuochang geshou",
    "zhizuoren",

    # Japanese (romanised)
    "myuujishan",
    "aatisuto",
    "bando",
    "kashu",
    "vokalisto",

    # Korean (romanised)
    "myujisyeon",
    "atisuteu",
    "baendeu",
    "gasu",
    "bokeolliseuteu",

    # Hindi / Hinglish
    "sangeetkar",
    "gayak",
    "gayika",
    "kalakaar",
    "raepper",

    # Indonesian / Malay
    "musisi",
    "pemuzik",
    "penyanyi",
    "penyanyi-penulis lagu",
    "kumpulan muzik",
    "produser musik",

    # Tagalog
    "musikero",
    "musikera",
    "mang-aawit",
    "bokalis",
}
# 2025-12-08: Extended musician page detection to cover common multilingual
# aria labels/categories via this normalised keyword set.

MUSIC_CATEGORY_BOOST = 0.8
MUSIC_FLAG_BOOST = 0.5


def is_fb_login_redirect(url: str) -> bool:
    """Return True if the URL points to a Facebook login/registration redirect (/r.php)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower().lstrip("/")
    if "facebook.com" not in host:
        return False
    return path.startswith("r.php")

# If ANY of these strings appear in the candidate name, category or URL slug, hard-reject.
BUSINESS_KILL_TOKENS = (
    "real estate",
    "realestate",
    "estate agent",
    "property",
    "agency",
    "travel",
    "spa",
    "salon",
    "clinic",
    "resort",
    "hotel",
    "guest house",
    "hostel",
    "motel",
    "villa",
    "apartments",
    "apartment",
    "restaurant",
    "cafe",
    "coffee shop",
    "bar",
    "grill",
    "shop",
    "store",
    "boutique",
    "market",
    "mall",
    "university",
    "college",
    "school",
    "church",
    "ministry",
    "foundation",
    "ngo",
    "ministries",
    "farm",
    "farms",
    "company",
    "co.",
    "corp",
    "corporation",
    "llc",
    "ltd",
    "pty",
    "inc",
    "international",
)

MUSIC_NAME_SUFFIXES = ("music", "band", "official")

# Pages should look explicitly music-related to be considered.
MUSIC_TOKENS = [
    "musician",
    "musicians",
    "artist",
    "artists",
    "musical artist",
    "musical group",
    "musical ensemble",
    "musician/band",
    "band",
    "bands",
    "music",
    "music group",
    "music collective",
    "music production",
    "music producer",
    "recording artist",
    "recording artists",
    "hip hop artist",
    "hip-hop artist",
    "hiphop artist",
    "performing artist",
    "performer",
    "performers",
    "vocalist",
    "vocalists",
    "singer",
    "singers",
    "singer-songwriter",
    "songwriter",
    "songwriters",
    "rapper",
    "dj",
    "producer",
    "rock band",
    "indie band",
    "pop band",
    "record label",
    "composer",
    "composers",
    "lyricist",
    "lyricists",
]

# Narrow music cues for the strict fallback.
MUSIC_FALLBACK_TOKENS = [
    "musician",
    "music",
    "band",
    "artist",
    "singer",
    "songwriter",
    "rapper",
    "producer",
    "dj",
]
NON_MUSIC_ARTIST_TOKENS = [
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

# Basic cleaner to strip noisy tails from FB category strings (phone, URLs, long blurbs).
def clean_fb_category_text(text: str) -> str:
    raw = re.sub(r"\s+", " ", text or "").strip(" -\u2022")
    if not raw:
        return raw
    # Split on common FB separators and keep the first music-relevant piece.
    segments = [seg.strip(" -") for seg in re.split(r"[·|]", raw) if seg and seg.strip(" -")]
    for seg in segments:
        lower = seg.lower()
        if any(tok in lower for tok in MUSIC_TOKENS):
            candidate = seg
            break
    else:
        candidate = segments[0] if segments else raw
    # Trim at phones/URLs/emails if present.
    candidate = re.split(r"(\+\d[\d\s().-]{5,}|\b\d{3,}[-\s]\d{3,}|\bmailto:|facebook\.com/|https?://|\w+@)", candidate)[0].strip(" -·")
    return candidate or raw

# Broader corporate markers to penalise or drop before scoring.
FB_CORPORATE_TOKENS = [
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
    "guest house",
    "guesthouse",
    "real estate",
    "realestate",
    "estate agent",
    "estateagency",
    "spa",
    "gallery",
    "galleria",
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
    "reel creator",
]

HARD_CORPORATE_TOKENS = {
    "hotel",
    "resort",
    "spa",
    "store",
    "shop",
    "bakery",
    "restaurant",
    "cafe",
    "bar",
    "grill",
    "church",
    "ministries",
    "real estate",
    "estate agent",
    "estate agents",
    "agency",
    "realtor",
    "inc",
    "ltd",
    "pty",
    "company",
    "boutique",
    "salon",
    "clinic",
    "pharmacy",
    "insurance",
    "bank",
    "university",
    "college",
    "school",
    "academy",
    "kindergarten",
    "foundation",
    "ngo",
    "nonprofit",
    "charity",
    "farm",
    "reel creator",
}

SOFT_CORPORATE_TOKENS = {
    "media",
    "brand",
    "podcast",
    "radio",
    "tv",
    "magazine",
    "blog",
    "community",
    "collective",
    "crew",
}

ARTIST_LIKE_TOKENS = {
    "musician",
    "artist",
    "band",
    "singer",
    "rapper",
    "vocalist",
    "producer",
    "dj",
    "music",
    "recording",
    "records",
    "record label",
}

# Music category tokens for quick checks (kept lowercase).
FB_MUSIC_CATEGORY_TOKENS = [
    "musician/band",
    "artist",
    "band",
    "music",
    "record label",
    "musician",
    "songwriter",
    # Common non-English musician labels (normalized)
    "musico",
    "musico/banda",
    "banda",
    "musicien",
    "musicien/groupe",
    "groupe",
    "groupe musical",
    "musicista",
    "musicista/band",
    "gruppo musicale",
    "gruppo",
]


@dataclass
class FbCandidate:
    name: str
    url: str
    category: str = ""


def is_noisy_fb_text_block(text: str) -> bool:
    text = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    normalized = re.sub(r"\s+", " ", text).lower()
    if is_fb_creator_category(normalized):
        return True
    for token in NOISY_FB_TOKENS:
        if token.lower() in normalized:
            return True
    return False


def looks_like_music_fallback(text_blocks: List[str], artist_name: str) -> bool:
    """
    Strict fallback: for pages with no reliable FB category.
    Returns True only if we find a text block that:
      - is not noisy UI text
      - contains the artist name (case-insensitive, or a close variant)
      - AND contains at least one music-related token.
    """
    if not artist_name:
        return False

    artist_l = artist_name.strip().lower()
    if not artist_l:
        return False

    for raw in text_blocks or []:
        if not raw:
            continue
        if is_noisy_fb_text_block(raw):
            continue

        block = raw.lower()

        if artist_l not in block:
            continue

        if "artist" in block:
            if any(bad in block for bad in NON_MUSIC_ARTIST_TOKENS):
                continue
            return True

        if any(tok in block for tok in MUSIC_FALLBACK_TOKENS):
            return True

    return False


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_role_text(raw: Optional[str]) -> str:
    """
    Normalise aria/category text for role detection:
    - lowercase, accent-insensitive
    - trim and collapse whitespace
    - preserve separators like "/" so musician/band combos stay intact
    """
    if not raw:
        return ""
    text = str(raw).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = " ".join(text.split())
    return text


# British spelling alias for callers that expect normalise_* naming.
def normalise_role_text(raw: Optional[str]) -> str:
    return normalize_role_text(raw)


def _role_keyword_hit(normalized_text: str) -> bool:
    if not normalized_text:
        return False
    if normalized_text in MUSICIAN_ROLE_KEYWORDS:
        return True
    # Match against segments split by common separators while keeping "/" combos intact.
    segments = [seg.strip(" -/") for seg in re.split(r"[|·]", normalized_text) if seg and seg.strip(" -/")]
    for segment in segments:
        if segment in MUSICIAN_ROLE_KEYWORDS:
            return True
        # Allow safe whole-word matches to catch things like "artist musician/band".
        for kw in MUSICIAN_ROLE_KEYWORDS:
            if re.search(rf"(?<!\\w){re.escape(kw)}(?!\\w)", segment):
                return True
    for kw in MUSICIAN_ROLE_KEYWORDS:
        if re.search(rf"(?<!\\w){re.escape(kw)}(?!\\w)", normalized_text):
            return True
    return False


def is_musician_page_from_roles(*raw_role_texts: Optional[str]) -> bool:
    """
    Return True if any supplied role/category strings suggest a musician/artist/band.
    Accepts multiple hints (aria-label, category text, etc.).
    """
    normalised: list[str] = [normalize_role_text(text) for text in raw_role_texts if text]
    if not normalised:
        return False

    # Exact match first.
    for text in normalised:
        if text in MUSICIAN_ROLE_KEYWORDS:
            return True

    # Controlled substring match.
    for text in normalised:
        if _role_keyword_hit(text):
            return True
    return False


def _debug_log_unknown_role(logger, aria_label: Optional[str], category_text: Optional[str]) -> None:
    if not logger:
        return
    log_fn = getattr(logger, "debug", None) if hasattr(logger, "debug") else None
    if not callable(log_fn):
        return
    norm_aria = normalize_role_text(aria_label or "")
    norm_cat = normalize_role_text(category_text or "")
    log_fn(
        "Potential musician role missed: aria=%r norm_aria=%r category=%r norm_cat=%r",
        aria_label,
        norm_aria,
        category_text,
        norm_cat,
    )


def is_musician_page(
    aria_text: Optional[str],
    category_text: Optional[str],
    logger=None,
    debug_logging_enabled: bool = False,
) -> bool:
    """
    Return True if aria/category text suggests a musician/artist/band page.
    Uses a curated, normalised keyword set to avoid over-broad matches.
    """
    if is_musician_page_from_roles(aria_text, category_text):
        return True

    candidates = [normalize_role_text(raw or "") for raw in (aria_text, category_text) if raw]

    if debug_logging_enabled and (aria_text or category_text):
        norm_aria = normalize_role_text(aria_text or "")
        norm_cat = normalize_role_text(category_text or "")
        if "music" in norm_aria or "music" in norm_cat:
            _debug_log_unknown_role(logger, aria_text, category_text)

    return False


def normalize_fb_name(name: str) -> str:
    """
    Normalise a name for fuzzy matching:
    - lowercase, accent-insensitive
    - remove punctuation
    - drop common noise tokens like "music" or "official"
    """
    if not isinstance(name, str):
        return ""
    cleaned = _strip_accents(name).lower()
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    # Remove common suffix/prefix noise
    noise_tokens = {"music", "musician", "band", "official", "records", "record"}
    parts = [part for part in cleaned.split() if part and part not in noise_tokens]
    return " ".join(parts)


def has_corporate_token(value: Optional[str]) -> bool:
    if not value:
        return False
    norm = normalize_fb_name(value)
    return any(token in norm for token in FB_CORPORATE_TOKENS)


def detect_corporate_token(url: str, name: str, category: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Return (True, token) if any corporate token appears in url/name/category (substring match).
    """
    url_l = (url or "").lower()
    name_l = (name or "").lower()
    cat_l = (category or "").lower()
    # Merge all corporate token sources.
    token_set = list(dict.fromkeys(list(BUSINESS_KILL_TOKENS) + list(FB_CORPORATE_TOKENS) + ["shoppe", "onlineshoppe"]))
    for tok in token_set:
        if tok and (tok in url_l or tok in name_l or tok in cat_l):
            return True, tok
    return False, None


def _looks_music_related(name: str, category: str, url: str = "") -> bool:
    text = f"{name} {category} {url}".lower()
    if any(tok in text for tok in MUSIC_CATEGORY_KEYWORDS):
        return True
    if _role_keyword_hit(normalize_role_text(category)):
        return True
    if any(suffix in text for suffix in MUSIC_NAME_SUFFIXES):
        return True
    return False


def _looks_corporate(name: str, category: str, url: str) -> bool:
    text = f"{name} {category} {url}".lower()
    # Include variations like "shoppe".
    corporate_tokens = set(FB_CORPORATE_TOKENS) | {"shoppe", "onlineshoppe"}
    return any(tok in text for tok in corporate_tokens)


def _find_corporate_token(name: str, category: str, url: str) -> Optional[str]:
    """
    Return the first matching corporate token found in name/category/url, if any.
    """
    text = f"{name} {category} {url}".lower()
    corporate_tokens = list(BUSINESS_KILL_TOKENS) + list(FB_CORPORATE_TOKENS) + ["shoppe", "onlineshoppe"]
    for tok in corporate_tokens:
        if tok in text:
            return tok
    return None


def _normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def _contains_any(text: str, tokens: set[str]) -> bool:
    text_l = _normalize_text(text)
    if not text_l:
        return False
    return any(tok in text_l for tok in tokens)


@dataclass
class CorporateSignals:
    has_hard: bool = False
    has_soft: bool = False
    has_artist: bool = False


def classify_corporate_signals(*pieces: str) -> CorporateSignals:
    """
    Hybrid classifier:
      - has_hard: any hard corporate token in any piece (URL, name, category text)
      - has_soft: any soft corporate token in any piece
      - has_artist: any artist-like token in any piece
    """
    joined = " ".join(_normalize_text(p) for p in pieces if p)
    if not joined:
        return CorporateSignals()
    return CorporateSignals(
        has_hard=_contains_any(joined, HARD_CORPORATE_TOKENS),
        has_soft=_contains_any(joined, SOFT_CORPORATE_TOKENS),
        has_artist=_contains_any(joined, ARTIST_LIKE_TOKENS),
    )


def is_music_page(name_lc: str, url_lc: str, category_lc: str) -> bool:
    """
    Return True if any MUSIC_TOKENS appear in name/url/category.
    """
    if is_musician_page(None, category_lc):
        return True

    normalized_blobs = (
        normalize_role_text(name_lc),
        normalize_role_text(url_lc),
        normalize_role_text(category_lc),
    )
    for blob in normalized_blobs:
        for token in MUSIC_TOKENS:
            if token in blob:
                return True
    return False


def _corporate_hit(name_lc: str, url_lc: str, category_lc: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Return (hit, token, field) for corporate tokens matched in name/url/category.
    """
    for token in FB_CORPORATE_TOKENS:
        if token in name_lc:
            return True, token, "name"
        if token in url_lc:
            return True, token, "url"
        if token in category_lc:
            return True, token, "category"
    return False, None, None


def compute_category_boost(category: Optional[str]) -> float:
    """
    Additive boost for music-positive categories, capped to a small range.
    """
    if not category:
        return 0.0
    c = normalize_role_text(category)
    score = 0.0
    if _role_keyword_hit(c):
        score += 1.0
    for kw in MUSIC_CATEGORY_KEYWORDS:
        if kw in c:
            score += 1.0
    return min(score, 2.0)


def is_fb_creator_category(text: Optional[str]) -> bool:
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False
    return any(tok in normalized for tok in FB_CREATOR_CATEGORY_TOKENS)


def _extract_fb_category_candidates(card_el, page_name: str = "") -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Extract structured category/descriptor tokens from a FB search result container.
    Returns (primary_category, descriptor, tokens) where tokens preserve discovery order.
    Accepts a BeautifulSoup element (e.g., the anchor's parent).
    """
    if card_el is None:
        return None, None, []

    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _is_engagement_noise(text: str) -> bool:
        """
        Drop follower/like/review counters while keeping descriptive tokens.
        """
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        number = r"\d+(?:[.,]\d+)?"
        if re.match(rf"^{number}\s*[km]?\s*(followers?|follower|likes?|members?)$", lowered):
            return True
        if re.match(rf"^{number}\s*(?:reviews?|ratings?|comments?|views?)$", lowered):
            return True
        if re.match(rf"^{number}\s*(?:/5)?\s*\(\s*{number}\s*reviews?\s*\)$", lowered):
            return True
        if re.match(rf"^{number}\s*(?:★|stars?)\s*(?:\(\s*{number}\s*reviews?\s*\))?$", lowered):
            return True
        if "always open" in lowered or lowered in ("open now", "closed now", "temporarily closed"):
            return True
        return False

    def _tokenize(text: str) -> List[str]:
        if not text:
            return []
        parts = re.split(r"[·|]", text)
        tokens: List[str] = []
        for part in parts:
            token = _clean(part)
            if not token or _is_engagement_noise(token):
                continue
            if is_fb_creator_category(token):
                continue
            if is_noisy_fb_text_block(token):
                continue
            if len(token) > 80:
                continue
            # Drop obvious CTA/JSON noise tokens that occasionally leak into subtitles.
            lowered = token.lower()
            if ("{" in token and "}" in token and len(token) > 40) or lowered in ("follow", "message", "search", "book now"):
                continue
            tokens.append(token)
        return tokens

    seen_tokens: set[str] = set()
    seen_blobs: set[str] = set()
    tokens: List[str] = []

    def _add_tokens_from_text(raw_text: str) -> None:
        blob = _clean(raw_text)
        if not blob:
            return
        blob_key = blob.lower()
        if blob_key in seen_blobs:
            return
        seen_blobs.add(blob_key)
        for token in _tokenize(blob):
            token_key = token.lower()
            if not token_key or token_key == (page_name or "").strip().lower():
                continue
            if token_key in seen_tokens:
                continue
            seen_tokens.add(token_key)
            tokens.append(token)

    # Identify the anchor and a likely card container so we can pull the subtitle line(s) that
    # appear directly under the name in the search result card.
    anchor = card_el if isinstance(card_el, Tag) and card_el.name == "a" else None
    if anchor is None and hasattr(card_el, "find"):
        try:
            anchor = card_el.find("a", href=True)
        except Exception:
            anchor = None

    target_href = ""
    try:
        target_href = (anchor.get("href") or "").split("#", 1)[0] if anchor else ""
    except Exception:
        target_href = ""

    ancestors = []
    cur = card_el
    for _ in range(6):
        if not cur:
            break
        ancestors.append(cur)
        cur = getattr(cur, "parent", None)

    card_container = None
    for anc in ancestors:
        try:
            role_val = (anc.get("role") or "").lower()
            aria_val = (anc.get("aria-label") or "").lower()
        except Exception:
            role_val = ""
            aria_val = ""
        if role_val in ("article", "listitem", "feed", "main", "region"):
            card_container = anc
            break
        if "search result" in aria_val:
            card_container = anc
            break
        try:
            if anc.name in ("article", "section", "li") or anc.get("data-pagelet"):
                card_container = anc
                break
        except Exception:
            continue
    if card_container is None and ancestors:
        card_container = ancestors[0]

    def _collect_following_text(max_lines: int = 4) -> None:
        """
        Walk forward from the anchor and grab the first few textual lines until we hit
        a different anchor (likely the next card).
        """
        if anchor is None:
            return
        lines_added = 0
        for el in anchor.next_elements:
            if lines_added >= max_lines:
                break
            if isinstance(el, Tag) and el.name == "a":
                try:
                    other_href = (el.get("href") or "").split("#", 1)[0]
                except Exception:
                    other_href = ""
                if other_href and other_href != target_href:
                    break  # crossed into another card
                continue
            if isinstance(el, NavigableString):
                text = _clean(str(el))
                if not text:
                    continue
                before = len(tokens)
                _add_tokens_from_text(text)
                if len(tokens) > before:
                    lines_added += 1

    try:
        # 1) Immediate subtitle lines after the anchor (best signal for "Musician/band", etc.)
        _collect_following_text(max_lines=4)

        # 2) Scrape short text blocks inside the likely card container and its close ancestors.
        container_chain_candidates = [card_el, getattr(card_el, "parent", None), card_container, anchor]
        container_chain: List[Tag] = []
        seen_ids = set()
        for node in container_chain_candidates:
            if node is None:
                continue
            node_id = id(node)
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            container_chain.append(node)

        for container in container_chain:
            if not container:
                continue
            # Attributes sometimes hold the label.
            for attr in ("aria-label", "title"):
                text = _clean(getattr(container, "get", lambda *_: "")(attr, ""))
                _add_tokens_from_text(text)

            # Inline text near the anchor/card.
            for node in container.stripped_strings:
                _add_tokens_from_text(node)

            # Short text from nearby spans/divs (often the grey label). Limit to avoid sweeping entire feeds.
            for sub in container.find_all(["span", "div"], limit=20):
                try:
                    # Skip blocks that clearly belong to another anchor/card.
                    if anchor and sub is not anchor and sub.find("a", href=True):
                        hrefs = {(_clean(a.get("href") or "").split("#", 1)[0]) for a in sub.find_all("a", href=True)}
                        if hrefs and (not target_href or any(h and h != target_href for h in hrefs)):
                            continue
                except Exception:
                    pass
                text = _clean(getattr(sub, "get_text", lambda *_: "")(" ", strip=True))
                _add_tokens_from_text(text)

    except Exception:
        tokens = []

    def _looks_like_name(val: str) -> bool:
        return normalize_fb_name(val) == normalize_fb_name(page_name)

    def _contains_music(val: str) -> bool:
        val_l = (val or "").lower()
        music_tokens = (
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
        return any(tok in val_l for tok in music_tokens)

    primary: Optional[str] = None
    descriptor: Optional[str] = None

    for token in tokens:
        if _contains_music(token):
            primary = token
            break
        if not _looks_like_name(token):
            primary = token
            break

    if primary is None and tokens:
        primary = tokens[0]

    if tokens:
        for token in tokens:
            if token == primary:
                continue
            if _looks_like_name(token):
                continue
            if descriptor is None:
                descriptor = token
            if descriptor is not None and _contains_music(token) and not _contains_music(primary or ""):
                descriptor = token
                break

    if descriptor is None:
        # Prefer a music token as descriptor when only a single label was found.
        for token in tokens:
            if _contains_music(token):
                descriptor = token
                break
        if descriptor is None and tokens:
            descriptor = tokens[0] if not _looks_like_name(tokens[0]) else (tokens[1] if len(tokens) > 1 else None)

    return primary, descriptor, tokens


def extract_fb_category(card_el, page_name: str = "") -> Optional[str]:
    """
    Backwards-compatible wrapper returning only the primary category text.
    """
    primary, _, _ = _extract_fb_category_candidates(card_el, page_name=page_name)
    return primary


def _base_name_score(artist_norm: str, name_norm: str, username_norm: str) -> float:
    score = 0.0
    if artist_norm and name_norm:
        if name_norm == artist_norm:
            score += 1.0
        elif name_norm.startswith(artist_norm):
            score += 0.7
        elif artist_norm in name_norm or name_norm in artist_norm:
            score += 0.4
    if artist_norm and username_norm:
        if username_norm == artist_norm:
            score += 1.0
        elif username_norm.startswith(artist_norm):
            score += 0.7
    return score


def score_fb_candidate(
    artist_name: str, cand_name: str, cand_url: str, category: Optional[str]
) -> Tuple[float, float, float]:
    """
    Return (final_score, base_score, cat_boost) for a FB candidate.
    If the candidate should be hard-rejected (business kill tokens), return None.
    """
    artist_norm = normalize_fb_name(artist_name)
    name_norm = normalize_fb_name(cand_name)
    try:
        path_slug = urllib.parse.urlparse(cand_url or "").path.strip("/").split("/")[0]
    except Exception:
        path_slug = ""
    username_norm = normalize_fb_name(path_slug)
    category_norm = normalize_fb_name(category or "")

    if is_fb_creator_category(category):
        return None

    sig = classify_corporate_signals(cand_url, cand_name, category or "")
    if sig.has_hard and not sig.has_artist:
        return None  # caller should skip hard corporate without artist signals

    # Name similarity via token Jaccard.
    artist_tokens = set(artist_norm.split())
    name_tokens = set(name_norm.split())
    overlap = len(artist_tokens & name_tokens)
    union = len(artist_tokens | name_tokens) or 1
    name_score = overlap / union

    # Small bonus for close name matches.
    artist_raw = artist_name.lower().strip()
    name_raw = cand_name.lower().strip()
    if name_raw == artist_raw:
        name_score += 0.5
    else:
        for suffix in MUSIC_NAME_SUFFIXES:
            if name_raw == f"{artist_raw} {suffix}" or name_raw.startswith(f"{artist_raw} - "):
                name_score += 0.3
                break

    cat_boost = compute_category_boost(category)
    if sig.has_soft and not sig.has_artist:
        cat_boost -= 0.5

    final_score = name_score + cat_boost
    return final_score, name_score, cat_boost


def select_best_fb_candidate(
    artist_name: str,
    candidates: List[FbCandidate],
    suppress_console: bool = False,
    logger=None,
) -> Tuple[Optional[FbCandidate], float, float, float]:
    """
    Pick the highest-scoring candidate; ignores anything below the minimum threshold.
    Returns (candidate, final_score, base_score, cat_boost).
    """
    MIN_FINAL_SCORE = 1.0
    best: Optional[Tuple[float, float, float, FbCandidate]] = None
    best_is_music = False
    for cand in candidates:
        name_lc = (cand.name or "").lower()
        url_lc = (cand.url or "").lower()
        category_lc = (cand.category or "").lower()

        corp_hit, corp_token, corp_field = _corporate_hit(name_lc, url_lc, category_lc)
        if corp_hit:
            _shared_fb_log(
                logger,
                "[FB Enrich] Rejecting FB candidate '%s' for '%s' due to corporate token '%s' in %s"
                % (cand.name or cand.url, artist_name, corp_token or "<unknown>", corp_field or "name/url/category"),
                suppress_console=suppress_console,
            )
            continue

        music_flag = is_music_page(name_lc, url_lc, category_lc)
        if not music_flag:
            _shared_fb_log(
                logger,
                "[FB Enrich] Skipping non-music FB candidate '%s' for '%s' (category='%s')"
                % (cand.name or cand.url, artist_name, cand.category or "<none>"),
                suppress_console=suppress_console,
            )
            continue
        scored = score_fb_candidate(artist_name, cand.name, cand.url, cand.category)
        if scored is None:
            continue
        final_score, base_score, cat_boost = scored
        if best is None or final_score > best[0]:
            best = (final_score, base_score, cat_boost, cand)
            best_is_music = music_flag
    if best is None or best[0] < MIN_FINAL_SCORE or not best_is_music:
        _shared_fb_log(
            logger,
            "[FB Enrich] No high-confidence music FB match for '%s'." % artist_name,
            suppress_console=suppress_console,
        )
        return None, best[0] if best else float("-inf"), best[1] if best else 0.0, best[2] if best else 0.0
    _, base_score, cat_boost, cand = best
    return cand, best[0], base_score, cat_boost


def _shared_fb_log(logger, message: str, suppress_console: bool = False) -> None:
    if not message:
        return
    if logger and hasattr(logger, "info"):
        try:
            logger.info(message)
            return
        except Exception:
            pass
    if callable(logger):
        try:
            logger(message)
            return
        except Exception:
            pass
    if suppress_console:
        return
    try:
        print(message)
    except Exception:
        pass


def is_junk_facebook_candidate(candidate: FbCandidate) -> bool:
    name = (getattr(candidate, "name", "") or "").strip().lower()
    cat = (getattr(candidate, "category", "") or "").strip().lower()
    url = (getattr(candidate, "url", "") or "").strip().lower()
    # Shared junk filter for business/notification/composer FB URLs
    if is_junk_fb_candidate_url(url):
        return True
    parsed = urlparse(url)
    netloc = parsed.netloc or ""
    path = parsed.path or ""

    if name == "facebook":
        return True
    if url in ("https://www.facebook.com/", "http://www.facebook.com/"):
        return True
    if "go to facebook home" in cat:
        return True

    if name in {"forgotten account?", "find your account"}:
        return True
    if "email or phone" in cat:
        return True
    if "/recover/" in url or "recover/initiate" in url:
        return True
    if "/login/" in url or "/checkpoint/" in url:
        return True

    if name in {"video", "videos"}:
        return True
    if "browse in video" in cat:
        return True
    if "facebook.com/watch" in url:
        return True

    return False


def is_music_like_category(category: str, logger=None, debug_logging_enabled: bool = False) -> bool:
    if not category:
        return False
    cl = normalize_role_text(category)
    if _role_keyword_hit(cl):
        return True
    match = any(k in cl for k in MUSIC_CATEGORY_KEYWORDS)
    if debug_logging_enabled and not match and ("music" in cl):
        _debug_log_unknown_role(logger, None, category)
    return match


def is_junk_fb_candidate_url(url: str) -> bool:
    """
    Shared junk filter for obvious business/notification/composer Facebook URLs.
    """
    if not url:
        return False
    url_l = (url or "").lower()
    junk_substrings = (
        "business.facebook.com",
        "/latest/composer",
        "notif_id=",
        "notif_t=",
    )
    if any(token in url_l for token in junk_substrings):
        return True
    try:
        parsed = urlparse(url_l)
        netloc = parsed.netloc or ""
        path = parsed.path or ""
        query = parsed.query or ""
        if "business.facebook.com" in netloc:
            return True
        if "/latest/composer" in path:
            return True
        if "notif_id=" in query or "notif_t=" in query:
            return True
    except Exception:
        return False
    return False


def fb_is_allowed_profile_candidate_url(url: str) -> bool:
    """
    Deterministic hard gate.
    True only if url is an allowed FB profile URL shape:
      - https://www.facebook.com/<username>[/]
      - https://www.facebook.com/profile.php?id=<digits> (and ONLY id param)
    Explicitly rejects known junk surfaces.
    """
    if not url:
        return False
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return False

    scheme = (parsed.scheme or "").lower()
    if scheme and scheme not in ("http", "https"):
        return False

    host = (parsed.netloc or "").lower()
    if not host:
        return False
    host_norm = "facebook.com" if host == "m.facebook.com" else host
    allowed_hosts = {"facebook.com", "www.facebook.com", "m.facebook.com"}
    if host_norm not in allowed_hosts and host not in allowed_hosts:
        return False
    if host.startswith("business.") or "business.facebook.com" in host:
        return False

    path = parsed.path or ""
    query = parsed.query or ""
    lowered_blob = f"{path}?{query}".lower() if query else path.lower()
    reject_tokens = (
        "/events/",
        "/watch",
        "/reel",
        "/afad",
        "/notifications",
        "ref=notif",
        "notif_id",
        "notif_t",
        "business.facebook.com",
        "/latest/composer",
        "/business/",
    )
    if any(tok in lowered_blob for tok in reject_tokens):
        return False

    path_clean = path.rstrip("/")
    segments = [seg for seg in path.split("/") if seg]

    if path_clean.lower() == "/profile.php":
        qs = urllib.parse.parse_qs(query, keep_blank_values=False)
        if set(qs.keys()) != {"id"}:
            return False
        ids = qs.get("id", [])
        if len(ids) != 1:
            return False
        id_value = ids[0]
        return bool(id_value and id_value.isdigit())

    if len(segments) == 1:
        username = segments[0]
        if not username or username.lower() == "profile.php":
            return False
        if query:
            return False
        return True

    return False


def fb_reason_code_split(url: str, existing_reason: str) -> str:
    """
    Split legacy 'business_notif' reason into notif_ui vs business_ui buckets.
    """
    if existing_reason != "business_notif":
        return existing_reason
    url_lower = (url or "").lower()
    if not url_lower:
        return existing_reason

    business_tokens = ("business.facebook.com", "/latest/composer", "/business/")
    notif_tokens = ("ref=notif", "notif_id", "notif_t", "/notifications", "/events/", "/watch", "/reel", "/afad")

    if any(tok in url_lower for tok in business_tokens):
        return "business_ui"
    if any(tok in url_lower for tok in notif_tokens):
        return "notif_ui"
    return existing_reason


def _fb_reason_code_split_self_check() -> None:
    assert fb_reason_code_split("https://facebook.com/watch/abc", "business_notif") == "notif_ui"
    assert fb_reason_code_split("https://facebook.com/notifications/?ref=notif", "business_notif") == "notif_ui"
    assert fb_reason_code_split("https://business.facebook.com/latest/composer/123", "business_notif") == "business_ui"
    assert fb_reason_code_split("https://facebook.com/business/somepage", "business_notif") == "business_ui"
    assert fb_reason_code_split("https://facebook.com/someartist", "business_notif") == "business_notif"
    assert fb_reason_code_split("https://facebook.com/watch/abc", "other_reason") == "other_reason"


if os.getenv("FB_DEBUG_REASON_SPLIT") == "1":
    _fb_reason_code_split_self_check()


def _fb_is_candidate_url_allowed(url: str) -> bool:
    """
    Strict allowlist for FB search candidates:
      - https://www.facebook.com/<username>[?<tracking params>]
      - https://www.facebook.com/profile.php?id=<digits>[&<tracking params>]
    Rejects non-page surfaces such as /groups, /watch, /reel, /events,
    /notifications, /afad and notif params.
    """
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower().strip()
    while True:
        if host.startswith("www."):
            host = host[4:]
            continue
        if host.startswith("m."):
            host = host[2:]
            continue
        if host.startswith("web."):
            host = host[4:]
            continue
        if host.startswith("touch."):
            host = host[6:]
            continue
        break
    if host != "facebook.com":
        return False

    path = parsed.path or ""
    segments = [seg for seg in path.strip("/").split("/") if seg]
    query = parsed.query or ""
    lowered_query = query.lower()

    if not segments:
        return False

    reserved = {
        "groups",
        "watch",
        "reel",
        "events",
        "notifications",
        "afad",
        "photo.php",
        "story.php",
        "permalink.php",
        "sharer.php",
    }
    if segments[0].lower() in reserved:
        return False

    if any(tok in lowered_query for tok in ("ref=notif", "notif_id", "notif_t")):
        return False

    if segments[0].lower() == "profile.php":
        if len(segments) != 1:
            return False
        qs = urllib.parse.parse_qs(query or "", keep_blank_values=False)
        ids = qs.get("id", [])
        return len(ids) == 1 and ids[0].isdigit()

    # Strict username path: exactly one segment. Query wrappers are tolerated
    # here because downstream Night FB selection canonicalizes them back to the
    # base page URL before navigation.
    if len(segments) == 1:
        return True

    return False


def _fb_extract_candidates_from_search_dom(html_or_driver, logger=None, debug: bool = False, search_name: str = "") -> List[FbCandidate]:
    """
    DOM-scoped extractor for Facebook search candidates.
    - Restricts anchor collection to known search-result containers.
    - Normalizes + dedupes hrefs.
    - Applies existing hard URL gate (fb_is_allowed_profile_candidate_url).
    - Emits structured diagnostics.
    """
    def _emit(msg: str) -> None:
        if not msg:
            return
        if logger and hasattr(logger, "info"):
            try:
                logger.info(msg)
                return
            except Exception:
                pass
        if callable(logger):
            try:
                logger(msg)
                return
            except Exception:
                pass
        try:
            print(msg)
        except Exception:
            pass

    html = ""
    driver = None
    if hasattr(html_or_driver, "page_source"):
        driver = html_or_driver
        try:
            html = html_or_driver.page_source or ""
        except Exception:
            html = ""
    elif isinstance(html_or_driver, str):
        html = html_or_driver
    else:
        html = ""

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    all_anchor_count = len(soup.select("a")) if soup else 0

    container_selectors: List[str] = [
        "div[role=\"main\"] div[role=\"feed\"]",
        "div[role=\"main\"] div[aria-label=\"Search results\"]",
        "div[aria-label=\"Search results\"]",
        "div[role=\"main\"]",
        # Legacy/extra fallbacks kept for robustness
        "div[role=\"main\"] section[aria-label*=\"Search results\"]",
        "div[role=\"main\"] [data-pagelet^=\"SearchResults\"]",
        "div[role=\"main\"] div[aria-label*=\"Search results\"]",
        "div[role=\"main\"] div[role=\"article\"]",
    ]

    chosen_selector = "NONE"
    containers = []
    fallback_reason = ""
    for selector in container_selectors:
        try:
            containers = soup.select(selector)
        except Exception:
            containers = []
        if containers:
            chosen_selector = selector
            break

    gate_debug_env = os.getenv("FB_DEBUG_DOM_GATE", "0")

    def _normalize_href(href: str) -> str:
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

    def _gather_candidate_elements(container_list: List[Tag]) -> List[Tag]:
        scoped: List[Tag] = []
        seen_ids = set()
        for container in container_list:
            for selector in ("a", 'a[role="link"]'):
                try:
                    found = container.select(selector)
                except Exception:
                    found = []
                for el in found:
                    el_id = id(el)
                    if el_id in seen_ids:
                        continue
                    seen_ids.add(el_id)
                    scoped.append(el)
        return scoped

    def _extract_candidates_from_elements(elements: List[Tag]) -> Tuple[List[FbCandidate], int]:
        raw: List[FbCandidate] = []
        href_seen = set()
        unique_elements = set()

        for element in elements:
            unique_elements.add(id(element))

            anchor = element
            try:
                href_raw = anchor.get("href") or ""
            except Exception:
                href_raw = ""

            if not href_raw:
                try:
                    nested = anchor.select_one("a[href]")
                except Exception:
                    nested = None
                if nested:
                    anchor = nested
                    try:
                        href_raw = nested.get("href") or ""
                    except Exception:
                        href_raw = ""

            href = _normalize_href(href_raw)
            if not href or "facebook.com" not in href:
                continue
            if href in href_seen:
                continue
            href_seen.add(href)

            try:
                parsed = urllib.parse.urlparse(href)
                path = (parsed.path or "").lower()
                if "/search/" in path or "/help" in path or "/login" in path:
                    continue
            except Exception:
                pass

            name = anchor.get_text(" ", strip=True) or href
            aria_label = anchor.get("aria-label") or ""
            category = ""
            category_candidates: List[str] = []
            if hasattr(anchor, "find_all"):
                category_el = getattr(anchor, "parent", None)
                if category_el is None and hasattr(anchor, "find_parent"):
                    try:
                        category_el = anchor.find_parent("div")
                    except Exception:
                        category_el = None
                if category_el is None:
                    category_el = anchor
                category, descriptor, category_candidates = _extract_fb_category_candidates(category_el, page_name=name)
            else:
                category, descriptor, category_candidates = None, None, []
            if not category and aria_label and not is_fb_creator_category(aria_label):
                category = aria_label

            secondary_text = ""
            for cat_text in category_candidates:
                if not cat_text:
                    continue
                if category and cat_text == category:
                    continue
                if name and cat_text.strip().lower() == name.strip().lower():
                    continue
                secondary_text = cat_text
                break

            fb_cand = FbCandidate(name=name, url=href, category=category or "")
            try:
                setattr(fb_cand, "aria_label", aria_label)
                setattr(fb_cand, "category_candidates", category_candidates)
                setattr(fb_cand, "secondary_text", secondary_text)
                descriptor_val = descriptor or secondary_text
                setattr(fb_cand, "descriptor", descriptor_val)
                setattr(fb_cand, "category_tokens", list(category_candidates))
            except Exception:
                pass
            raw.append(fb_cand)

        return raw, len(unique_elements)

    if not containers:
        try:
            # Conservative fallbacks: a standalone Search results region or article cards.
            fallback_region = soup.find(attrs={"aria-label": re.compile(r"^Search results$", re.I)})
        except Exception:
            fallback_region = None

        if fallback_region:
            containers = [fallback_region]
            chosen_selector = "aria-label=Search results (fallback)"
            fallback_reason = "fallback_aria_label"
        else:
            try:
                containers = soup.select('[role="article"]')
            except Exception:
                containers = []
            if containers:
                chosen_selector = "[role=article] (fallback)"
                fallback_reason = "fallback_article"

        if not containers:
            _emit(
                f"[FB Shared][DOM Gate] chosen_container_selector=NONE containers_found=0 anchors_in_scope=0 "
                f"candidates_pre_url_gate=0 candidates_post_url_gate=0 dropped_by_dom_gate={all_anchor_count} "
                f"reason=dom_container_missing search_name='{search_name or ''}'"
            )
            return []

    # Gather anchors/role links inside chosen containers (dedup by element id).
    candidate_elements: List[Tag] = _gather_candidate_elements(containers)
    anchors_in_scope_container = len(candidate_elements)

    fallback_enabled = os.getenv("NIGHT_FB_DOM_FALLBACK") == "1"
    fallback_used = False

    def _is_nav_like(anchor: Tag) -> bool:
        try:
            parent_with_aria = anchor.find_parent(attrs={"aria-label": True})
        except Exception:
            parent_with_aria = None
        aria_val = ""
        if parent_with_aria is not None:
            try:
                aria_val = parent_with_aria.get("aria-label") or ""
            except Exception:
                aria_val = ""
        aria_lower = aria_val.lower()
        return any(tok in aria_lower for tok in ("navigation", "header", "footer"))

    # First pass: only inside the scoped containers.
    raw_candidates, _ = _extract_candidates_from_elements(candidate_elements)
    candidates_pre_url_gate = len(raw_candidates)

    if candidates_pre_url_gate == 0:
        _emit(
            f"[FB Shared][DOM Gate] reason=zero_usable_hrefs_in_scope containers_found={len(containers)} "
            f"anchors_in_scope={anchors_in_scope_container} candidates_pre_url_gate=0 "
            f"fallback={1 if fallback_enabled else 0} search_name='{search_name or ''}'"
        )

    # Optional DOM fallback: only when container extraction produced zero usable hrefs.
    if fallback_enabled and candidates_pre_url_gate == 0:
        fallback_container_selectors = [
            'article[role="article"]',
            'div[role="feed"]',
            'div[role="main"]',
        ]

        fallback_containers: List[Tag] = []
        for fb_selector in fallback_container_selectors:
            try:
                fallback_containers.extend(soup.select(fb_selector))
            except Exception:
                continue

        fallback_elements = _gather_candidate_elements(fallback_containers)
        fallback_elements = [el for el in fallback_elements if not _is_nav_like(el)]

        seen_ids = {id(el) for el in candidate_elements}
        for el in fallback_elements:
            el_id = id(el)
            if el_id in seen_ids:
                continue
            seen_ids.add(el_id)
            candidate_elements.append(el)

        anchors_in_scope_container = len(candidate_elements)
        raw_candidates, _ = _extract_candidates_from_elements(candidate_elements)
        candidates_pre_url_gate = len(raw_candidates)
        fallback_used = bool(candidate_elements)
        if fallback_used:
            fallback_reason = fallback_reason or "zero_anchor_fallback"

    anchors_in_scope = len(candidate_elements)

    gate_reject = 0
    filtered: List[FbCandidate] = []
    for cand in raw_candidates:
        url_val = (cand.url or "").strip()
        if not url_val:
            gate_reject += 1
            continue
        try:
            if is_junk_fb_candidate_url(url_val):
                gate_reject += 1
                continue
        except Exception:
            pass
        if not _fb_is_candidate_url_allowed(url_val):
            gate_reject += 1
            continue
        filtered.append(cand)

    candidates_post_url_gate = len(filtered)
    dropped_by_dom_gate = max(0, all_anchor_count - anchors_in_scope)

    _emit(
        f"[FB Shared][DOM Gate] chosen_container_selector={chosen_selector} containers_found={len(containers)} "
        f"anchors_in_scope={anchors_in_scope} candidates_pre_url_gate={candidates_pre_url_gate} "
        f"candidates_post_url_gate={candidates_post_url_gate} url_gate_rejected={gate_reject} dropped_by_dom_gate={dropped_by_dom_gate} "
        f"reason={fallback_reason or 'container_match'} search_name='{search_name or ''}'"
    )

    if gate_debug_env in ("1", "2"):
        warnings = []
        bad_tokens = ("/notifications", "/watch", "/reel", "/events/", "notif_id", "notif_t")
        for cand in filtered:
            url_l = (cand.url or "").lower()
            if any(tok in url_l for tok in bad_tokens):
                warnings.append(url_l)
        artifact = {
            "selector": chosen_selector,
            "containers_found": len(containers),
            "anchors_in_scope": anchors_in_scope,
            "candidates_pre_url_gate": candidates_pre_url_gate,
            "candidates_post_url_gate": candidates_post_url_gate,
            "pre_urls": [c.url for c in raw_candidates[:20]],
            "post_urls": [c.url for c in filtered[:20]],
            "warnings": warnings,
            "timestamp": int(time.time()),
            "search_name": search_name or "",
        }
        try:
            fname = f"fb_dom_gate_debug_{int(time.time())}.json"
            with open(fname, "w", encoding="utf-8") as fh:
                json.dump(artifact, fh, indent=2)
        except Exception:
            pass
        if warnings and gate_debug_env == "2":
            raise AssertionError(f"FB DOM gate warnings: {warnings}")

    return filtered


def _fb_candidate_gate_self_check() -> None:
    assert fb_is_allowed_profile_candidate_url("https://www.facebook.com/someband")
    assert fb_is_allowed_profile_candidate_url("https://www.facebook.com/someband/")
    assert fb_is_allowed_profile_candidate_url("https://www.facebook.com/profile.php?id=1234567890")
    assert not fb_is_allowed_profile_candidate_url("https://www.facebook.com/watch?v=123")
    assert not fb_is_allowed_profile_candidate_url("https://www.facebook.com/events/birthdays")
    assert not fb_is_allowed_profile_candidate_url("https://www.facebook.com/reel/123")
    assert not fb_is_allowed_profile_candidate_url("https://www.facebook.com/notifications")
    assert not fb_is_allowed_profile_candidate_url("https://www.facebook.com/profile.php?id=123&foo=bar")
    assert not fb_is_allowed_profile_candidate_url("https://business.facebook.com/somepage")


if os.getenv("FB_DEBUG_CAND_GATE_ASSERT") == "1":
    _fb_candidate_gate_self_check()
if os.getenv("FB_DEBUG_CAND_URL_GATE") == "1":
    assert _fb_is_candidate_url_allowed("https://www.facebook.com/someband")
    assert _fb_is_candidate_url_allowed("https://www.facebook.com/profile.php?id=123456")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/groups/")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/groups/foo")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/photo.php?fbid=123")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/story.php?story_fbid=123&id=456")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/permalink.php?story_fbid=123&id=456")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/sharer.php?u=https%3A%2F%2Fexample.com")
    assert not _fb_is_candidate_url_allowed("https://www.facebook.com/someband/about")
    assert _fb_is_candidate_url_allowed("https://www.facebook.com/profile.php?id=12&foo=1")
    assert _fb_is_candidate_url_allowed("https://www.facebook.com/someband?__tn__=%2Cd")


def select_best_facebook_candidate(
    candidates: List[FbCandidate],
    search_name: str,
    logger=None,
    suppress_console: bool = False,
) -> Optional[FbCandidate]:
    """
    Shared selector for FB candidates used by daytime and Night Mode enrichment.

    - Filters junk/home/recover/login candidates.
    - Applies music-category and music-flag boosts.
    - Relies on existing FB scoring for consistency.
    """
    if not candidates:
        return None

    filtered = [
        c
        for c in candidates
        if (not is_junk_facebook_candidate(c)) and _fb_is_candidate_url_allowed(getattr(c, "url", ""))
    ]
    junk_count = len(candidates) - len(filtered)
    if junk_count:
        _shared_fb_log(logger, f"[FB Shared] Filtered {junk_count} junk FB candidate(s) for '{search_name}'", suppress_console=suppress_console)
    if not filtered:
        _shared_fb_log(logger, f"[FB Shared] All FB candidates for '{search_name}' were junk; no usable FB page.", suppress_console=suppress_console)
        return None

    ranked: List[Tuple[float, float, float, float, FbCandidate, Dict[str, Any]]] = []
    for cand in filtered:
        scored = score_fb_candidate(search_name, cand.name, cand.url, cand.category)
        if scored is None:
            continue
        final_score, base_score, cat_boost = scored
        music_bonus = 0.0
        category_val = getattr(cand, "category", "")
        if is_music_like_category(category_val, logger=logger, debug_logging_enabled=True):
            music_bonus += MUSIC_CATEGORY_BOOST
        if getattr(cand, "is_music_page", False):
            music_bonus += MUSIC_FLAG_BOOST
        adjusted = final_score + music_bonus
        ranked.append(
            (
                adjusted,
                base_score,
                cat_boost,
                music_bonus,
                cand,
                {
                    "name": getattr(cand, "name", "") or "",
                    "url": getattr(cand, "url", "") or "",
                    "category_raw": getattr(cand, "category", "") or "",
                    "music_hint": bool(getattr(cand, "music_hint", False)),
                },
            )
        )

    if not ranked:
        _shared_fb_log(logger, f"[FB Shared] No valid FB candidates for '{search_name}' after scoring.", suppress_console=suppress_console)
        return None

    def _is_profile(url: str) -> bool:
        url_l = (url or "").lower()
        return "profile.php" in url_l or "/people/" in url_l

    def _is_page(url: str) -> bool:
        try:
            parsed = urlparse(url or "")
        except Exception:
            return False
        path = (parsed.path or "").strip("/").split("/")
        if not path:
            return False
        if path[0] in ("pages", "p"):
            return True
        return not _is_profile(url)

    def _rank_key(item: Tuple[float, float, float, float, FbCandidate, Dict[str, Any]]):
        total, base, _, _, cand, _ = item
        url_val = getattr(cand, "url", "") or ""
        music_hint = bool(getattr(cand, "music_hint", False))
        return (
            -total,
            -base,
            -int(music_hint),
            -int(_is_page(url_val)),
            int(_is_profile(url_val)),
            getattr(cand, "name", "") or "",
        )

    ranked.sort(key=_rank_key)

    debug_rank = os.getenv("FB_DEBUG_RANK_SORT") == "1" or os.getenv("FB_CANDIDATE_RANKING") == "1"
    if debug_rank:
        _shared_fb_log(
            logger,
            f"[FB Shared][Rank Preview] query='{search_name}' candidates={len(ranked)} selected_by='ranked_sort'",
            suppress_console=suppress_console,
        )
        for idx, item in enumerate(ranked[:10], start=1):
            total, base, cat_boost, music_bonus, cand, meta = item
            url_val = getattr(cand, "url", "") or ""
            name_val = getattr(cand, "name", "") or url_val
            raw_cat = meta.get("category_raw", "")
            is_profile = _is_profile(url_val)
            is_page = _is_page(url_val)
            breakdown = f"base={base:.2f} cat={cat_boost:.2f} music_bonus={music_bonus:.2f}"
            _shared_fb_log(
                logger,
                f"[FB Shared][Rank Preview] {idx}) name='{name_val}' url='{url_val}' cat_raw='{raw_cat}' "
                f"is_profile={is_profile} is_page={is_page} music_hint={meta.get('music_hint')} total_score={total:.2f} {breakdown}",
                suppress_console=suppress_console,
            )

    candidate = ranked[0][4]
    if is_junk_facebook_candidate(candidate):
        _shared_fb_log(logger, f"[FB Shared] Best candidate for '{search_name}' turned out to be junk after scoring; dropping.", suppress_console=suppress_console)
        return None

    if debug_rank:
        top = ranked[0]
        _shared_fb_log(
            logger,
            f"[FB Shared] Selected FB candidate selected_by='ranked_sort' name='{getattr(candidate, 'name', '') or getattr(candidate, 'url', '')}' "
            f"url='{getattr(candidate, 'url', '')}' total_score={top[0]:.2f} base={top[1]:.2f} cat_boost={top[2]:.2f} music_bonus={top[3]:.2f}",
            suppress_console=suppress_console,
        )

    return candidate
