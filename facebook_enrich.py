"""Shared helpers for Facebook candidate parsing and scoring."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
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
)

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

# Basic cleaner to strip noisy tails from FB category strings (phone, URLs, long blurbs).
def clean_fb_category_text(text: str) -> str:
    raw = re.sub(r"\s+", " ", text or "").strip(" -\u2022")
    if not raw:
        return raw
    segments = [seg.strip(" -") for seg in raw.split("·") if seg and seg.strip(" -")]
    music_segments = [seg for seg in segments if any(tok in seg.lower() for tok in MUSIC_TOKENS)]
    if music_segments:
        candidate = min(music_segments, key=len)
    elif segments:
        candidate = segments[0]
    else:
        candidate = raw
    candidate = re.split(r"(\+\d[\d\s().-]{5,}|facebook\.com/|https?://)", candidate)[0].strip(" -·")
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
]


@dataclass
class FbCandidate:
    name: str
    url: str
    category: str = ""


def is_noisy_fb_text_block(text: str) -> bool:
    text = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    normalized = re.sub(r"\s+", " ", text).lower()
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

        if any(tok in block for tok in MUSIC_FALLBACK_TOKENS):
            return True

    return False


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


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
    for blob in (name_lc, url_lc, category_lc):
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
    c = category.lower()
    score = 0.0
    for kw in MUSIC_CATEGORY_KEYWORDS:
        if kw in c:
            score += 1.0
    return min(score, 2.0)


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
                if is_noisy_fb_text_block(val):
                    continue
                seen.add(val)
                candidates.append(val)
            # Short text from nearby spans/divs (often the grey label).
            for sub in container.find_all(["span", "div"], limit=12):
                text = _clean(getattr(sub, "get_text", lambda *_: "")(" ", strip=True))
                if text and text not in seen and len(text) <= 80:
                    if is_noisy_fb_text_block(text):
                        continue
                    seen.add(text)
                    candidates.append(text)
            # Attributes sometimes hold the label.
            for attr in ("aria-label", "title"):
                text = _clean(getattr(container, "get", lambda *_: "")(attr, ""))
                if text and text not in seen and len(text) <= 80:
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
                if is_noisy_fb_text_block(text):
                    continue
                seen.add(text)
                candidates.append(text)
    except Exception:
        candidates = []

    for candidate in candidates:
        lower = candidate.lower()
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
    artist_name: str, candidates: List[FbCandidate]
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
            print(
                "[FB Enrich] Rejecting FB candidate '%s' for '%s' due to corporate token '%s' in %s"
                % (cand.name or cand.url, artist_name, corp_token or "<unknown>", corp_field or "name/url/category")
            )
            continue

        music_flag = is_music_page(name_lc, url_lc, category_lc)
        if not music_flag:
            print(
                "[FB Enrich] Skipping non-music FB candidate '%s' for '%s' (category='%s')"
                % (cand.name or cand.url, artist_name, cand.category or "<none>")
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
        print(
            "[FB Enrich] No high-confidence music FB match for '%s'." % artist_name
        )
        return None, best[0] if best else float("-inf"), best[1] if best else 0.0, best[2] if best else 0.0
    _, base_score, cat_boost, cand = best
    return cand, best[0], base_score, cat_boost
