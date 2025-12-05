import logging
import re
from typing import Optional, Tuple

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_unearthed_genre(raw: str | None) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (primary_genre, raw_cleaned).
    primary_genre = first genre token, normalised (title case).
    raw_cleaned = original string, stripped of extra whitespace.
    """
    if raw is None:
        return None, None
    raw_cleaned = raw.strip()
    if not raw_cleaned:
        return None, None
    parts = re.split(r"[\\/•,-]", raw_cleaned)
    first = next((p.strip() for p in parts if p and p.strip()), None)
    if not first:
        return None, raw_cleaned
    primary = first.title()
    return primary, raw_cleaned


def extract_unearthed_genre_text(soup: BeautifulSoup) -> str:
    """
    Locate the genre line in the Unearthed artist hero header and return a readable string.
    Prefers the container between the ARTIST pill and artist name; falls back to the first
    genre list on the page. Logs at DEBUG level if no candidate is found.
    """
    if not soup:
        return ""

    def _has_class(tag, candidates):
        classes = tag.get("class") or []
        return any(c in classes for c in candidates)

    genre_container = None
    hero_block = soup.select_one("div.q0wzh")
    if hero_block:
        genre_container = hero_block.find(
            lambda t: t.name in ("div", "ul") and _has_class(t, ("ZF6HQ", "PARBR"))
        )

    if not genre_container:
        sr_label = soup.find(
            lambda t: t.name in ("span", "div")
            and _has_class(t, ("gRMNM",))
            and "genre" in t.get_text(" ", strip=True).lower()
        )
        if sr_label:
            genre_container = sr_label.find_next(
                lambda t: t.name in ("div", "ul") and _has_class(t, ("ZF6HQ", "PARBR"))
            )

    if not genre_container:
        genre_container = soup.find(
            "ul",
            class_=lambda c: (
                isinstance(c, str)
                and "PARBR" in c.split()
            )
            or (isinstance(c, (list, tuple)) and "PARBR" in c),
        )

    if not genre_container:
        logger.debug("[Unearthed] Genre container not found on profile page.")
        return ""

    return genre_container.get_text(" / ", strip=True)


# Backwards compatibility for legacy imports.
_unearthed_extract_genre_text = extract_unearthed_genre_text
