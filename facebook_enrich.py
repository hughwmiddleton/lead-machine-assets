"""Shared helpers for Facebook candidate parsing and scoring."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import List, Optional, Tuple

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


def extract_fb_category(card_el, page_name: str = "") -> Optional[str]:
    """
    Extract the short grey category string from a FB search result container.
    Accepts a BeautifulSoup element (e.g., the anchor's parent).
    """
    if card_el is None:
        return None
    seen = set()

    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    candidates: List[str] = []
    try:
        containers = [
            card_el,
            getattr(card_el, "parent", None),
            getattr(card_el, "parent", None) and getattr(card_el.parent, "parent", None),
        ]
        for container in containers:
            if not container:
                continue
            # Inline text near the anchor.
            for node in container.stripped_strings:
                val = _clean(node)
                if not val or val.lower() == (page_name or "").strip().lower():
                    continue
                if val in seen or len(val) > 80:
                    continue
                if is_fb_creator_category(val):
                    continue
                if is_noisy_fb_text_block(val):
                    continue
                seen.add(val)
                candidates.append(val)
            # Short text from nearby spans/divs (often the grey label).
            for sub in container.find_all(["span", "div"], limit=12):
                text = _clean(getattr(sub, "get_text", lambda *_: "")(" ", strip=True))
                if text and text not in seen and len(text) <= 80:
                    if is_fb_creator_category(text):
                        continue
                    if is_noisy_fb_text_block(text):
                        continue
                    seen.add(text)
                    candidates.append(text)
            # Attributes sometimes hold the label.
            for attr in ("aria-label", "title"):
                text = _clean(getattr(container, "get", lambda *_: "")(attr, ""))
                if text and text not in seen and len(text) <= 80:
                    if is_fb_creator_category(text):
                        continue
                    if is_noisy_fb_text_block(text):
                        continue
                    seen.add(text)
                    candidates.append(text)
        for sib in getattr(card_el, "next_siblings", []) or []:
            try:
                text = _clean(getattr(sib, "get_text", lambda *_: "")(" ", strip=True))
            except Exception:
                text = ""
            if text and text not in seen and len(text) <= 80:
                if is_fb_creator_category(text):
                    continue
                if is_noisy_fb_text_block(text):
                    continue
                seen.add(text)
                candidates.append(text)
    except Exception:
        candidates = []

    for candidate in candidates:
        lower = candidate.lower()
        if is_fb_creator_category(lower):
            continue
        if "/" in candidate or any(tok in lower for tok in ("band", "music", "artist", "dj", "musician", "singer", "songwriter", "performer", "vocalist")):
            return candidate
        if len(candidate.split()) <= 6:
            return candidate
    return candidates[0] if candidates else None


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

    filtered = [c for c in candidates if not is_junk_facebook_candidate(c)]
    junk_count = len(candidates) - len(filtered)
    if junk_count:
        _shared_fb_log(logger, f"[FB Shared] Filtered {junk_count} junk FB candidate(s) for '{search_name}'", suppress_console=suppress_console)
    if not filtered:
        _shared_fb_log(logger, f"[FB Shared] All FB candidates for '{search_name}' were junk; no usable FB page.", suppress_console=suppress_console)
        return None

    best = None
    for cand in filtered:
        scored = score_fb_candidate(search_name, cand.name, cand.url, cand.category)
        if scored is None:
            continue
        final_score, base_score, cat_boost = scored
        music_bonus = 0.0
        if is_music_like_category(getattr(cand, "category", ""), logger=logger, debug_logging_enabled=True):
            music_bonus += MUSIC_CATEGORY_BOOST
        if getattr(cand, "is_music_page", False):
            music_bonus += MUSIC_FLAG_BOOST
        adjusted = final_score + music_bonus
        if best is None or adjusted > best[0]:
            best = (adjusted, base_score, cat_boost, cand)

    if best is None:
        _shared_fb_log(logger, f"[FB Shared] No valid FB candidates for '{search_name}' after scoring.", suppress_console=suppress_console)
        return None

    candidate = best[3]
    if is_junk_facebook_candidate(candidate):
        _shared_fb_log(logger, f"[FB Shared] Best candidate for '{search_name}' turned out to be junk after scoring; dropping.", suppress_console=suppress_console)
        return None

    return candidate
