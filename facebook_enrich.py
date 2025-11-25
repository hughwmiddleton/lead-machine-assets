"""Shared helpers for Facebook candidate parsing and scoring."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Tokens that indicate a music-related Facebook page.
FB_MUSIC_KEYWORDS = [
    "musician/band",
    "musician",
    "band",
    "artist",
    "music",
    "record label",
    "singer",
    "songwriter",
    "rapper",
    "dj",
    "producer",
]

# Tokens that suggest a non-music business or corporate entity.
FB_NON_MUSIC_KEYWORDS = [
    "real estate",
    "estate agent",
    "realtor",
    "hotel",
    "resort",
    "spa",
    "salon",
    "shopping & retail",
    "restaurant",
    "bar",
    "cafe",
    "coffee shop",
    "coffee",
    "clothing store",
    "thrift & consignment store",
    "boutique",
    "op shop",
    "thrift",
    "construction company",
    "consulting agency",
    "school",
    "college",
    "university",
    "church",
    "ministry",
    "clinic",
    "hospital",
    "farm",
]

# Broader corporate markers to penalise or drop before scoring.
FB_CORPORATE_TOKENS = [
    "spa",
    "salon",
    "real estate",
    "estate agent",
    "realtor",
    "boutique",
    "store",
    "shop",
    "op shop",
    "thrift",
    "mart",
    "market",
    "hotel",
    "resort",
    "lodge",
    "hostel",
    "farm",
    "clinic",
    "hospital",
    "construction",
    "company",
    "llc",
    "ltd",
    "pvt",
    "limited",
    "restaurant",
    "cafe",
    "coffee shop",
    "coffee",
    "school",
    "college",
    "university",
    "church",
    "ministry",
]

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


def compute_fb_category_boost(category: Optional[str]) -> float:
    """Boost music categories; non-music pages get no boost."""
    if not category:
        return 0.0
    cat = normalize_fb_name(category)
    if not cat:
        return 0.0
    if any(token in cat for token in FB_MUSIC_KEYWORDS):
        return 2.5
    return 0.0


def has_corporate_token(value: Optional[str]) -> bool:
    if not value:
        return False
    norm = normalize_fb_name(value)
    return any(token in norm for token in FB_CORPORATE_TOKENS)


def _looks_music_related(name: str, category: str) -> bool:
    text = f"{name} {category}".lower()
    return any(tok in text for tok in FB_MUSIC_CATEGORY_TOKENS)


def _looks_corporate(name: str, category: str, url: str) -> bool:
    text = f"{name} {category} {url}".lower()
    return any(tok in text for tok in FB_CORPORATE_TOKENS)


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
        containers = [card_el, getattr(card_el, "parent", None), getattr(card_el, "parent", None) and getattr(card_el.parent, "parent", None)]
        for container in containers:
            if not container:
                continue
            for node in container.stripped_strings:
                val = _clean(node)
                if not val or val.lower() == (page_name or "").strip().lower():
                    continue
                if val in seen:
                    continue
                if len(val) > 80:
                    continue
                seen.add(val)
                candidates.append(val)
        for sib in getattr(card_el, "next_siblings", []) or []:
            try:
                text = _clean(getattr(sib, "get_text", lambda *_: "")(" ", strip=True))
            except Exception:
                text = ""
            if text and text not in seen and len(text) <= 80:
                seen.add(text)
                candidates.append(text)
    except Exception:
        candidates = []

    for candidate in candidates:
        lower = candidate.lower()
        if "/" in candidate or any(tok in lower for tok in ("band", "music", "artist", "dj", "musician")):
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
    Applies corporate-token penalties to both name and URL path.
    """
    artist_norm = normalize_fb_name(artist_name)
    name_norm = normalize_fb_name(cand_name)
    try:
        path_slug = urllib.parse.urlparse(cand_url or "").path.strip("/").split("/")[0]
    except Exception:
        path_slug = ""
    username_norm = normalize_fb_name(path_slug)
    category_norm = normalize_fb_name(category or "")

    is_music = _looks_music_related(cand_name, category or "")
    is_corporate = _looks_corporate(cand_name, category or "", cand_url or "")
    if is_corporate and not is_music:
        return -1.0, 0.0, 0.0

    base_score = _base_name_score(artist_norm, name_norm, username_norm)

    cat_boost = compute_fb_category_boost(category_norm or None) if is_music else 0.0
    penalty = -3.0 if (is_corporate and not is_music) else 0.0
    final_score = base_score + cat_boost + penalty
    return final_score, base_score, cat_boost


def select_best_fb_candidate(
    artist_name: str, candidates: List[FbCandidate]
) -> Tuple[Optional[FbCandidate], float, float, float]:
    """
    Pick the highest-scoring candidate; ignores anything below 0.0 final score.
    Returns (candidate, final_score, base_score, cat_boost).
    """
    best: Optional[FbCandidate] = None
    best_final = float("-inf")
    best_base = 0.0
    best_cat = 0.0
    for cand in candidates:
        final_score, base_score, cat_boost = score_fb_candidate(
            artist_name, cand.name, cand.url, cand.category
        )
        if final_score < 0:
            continue
        better_tie = final_score == best_final and cat_boost > best_cat
        if final_score > best_final or better_tie:
            best = cand
            best_final = final_score
            best_base = base_score
            best_cat = cat_boost
    if not best:
        return None, best_final, best_base, best_cat
    is_music = _looks_music_related(best.name, best.category)
    music_min = 1.0
    non_music_min = 1.5
    if is_music and best_final < music_min:
        return None, best_final, best_base, best_cat
    if not is_music and best_final < non_music_min:
        return None, best_final, best_base, best_cat
    return best, best_final, best_base, best_cat
