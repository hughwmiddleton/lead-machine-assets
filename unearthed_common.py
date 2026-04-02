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
    Locate the artist-level genre list on a current Unearthed profile page and return a
    readable string.

    Current live profiles expose the artist genres via a visible "Genres:" label near the
    top hero block. Anchor to that label and only inspect the local block before the Tracks
    section so we do not drift into per-track genre lists further down the page.
    """
    if not soup:
        return ""

    def _norm(text: str | None) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _label_text(tag) -> str:
        return _norm(tag.get_text(" ", strip=True)).rstrip(":").lower()

    def _is_heading(tag, label: str) -> bool:
        return getattr(tag, "name", "") in {"h1", "h2", "h3", "h4", "h5", "h6"} and _label_text(tag) == label

    stop_heading = soup.find(lambda t: _is_heading(t, "tracks"))
    genre_label = soup.find(
        lambda t: getattr(t, "name", "") in {"div", "p", "span", "strong", "h2", "h3", "h4"}
        and _label_text(t) == "genres"
    )

    genre_container = None
    if genre_label:
        cursor = genre_label
        while cursor:
            cursor = cursor.find_next()
            if cursor is None or cursor is stop_heading:
                break
            if getattr(cursor, "name", "") in {"h2", "h3", "h4", "h5", "h6"}:
                break
            if getattr(cursor, "name", "") in {"ul", "ol"} and cursor.find("li"):
                genre_container = cursor
                break

    if not genre_container:
        logger.debug("[Unearthed] Genre container not found on profile page.")
        return ""

    return genre_container.get_text(" / ", strip=True)


# Backwards compatibility for legacy imports.
_unearthed_extract_genre_text = extract_unearthed_genre_text
