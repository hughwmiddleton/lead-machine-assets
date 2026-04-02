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

    def _direct_child_tags(tag):
        if not tag or not getattr(tag, "children", None):
            return []
        return [child for child in tag.children if getattr(child, "name", None)]

    def _next_tag_sibling(tag):
        if not tag:
            return None
        sibling = tag.next_sibling
        while sibling is not None and not getattr(sibling, "name", None):
            sibling = sibling.next_sibling
        return sibling

    def _first_direct_list(container):
        if not container or not getattr(container, "name", None):
            return None
        if getattr(container, "name", "") in {"ul", "ol"} and container.find("li", recursive=False):
            return container
        for child in _direct_child_tags(container):
            if _is_heading(child, "tracks"):
                break
            if child.name in {"ul", "ol"} and child.find("li", recursive=False):
                return child
            for grandchild in _direct_child_tags(child):
                if grandchild.name in {"ul", "ol"} and grandchild.find("li", recursive=False):
                    return grandchild
        return None

    genre_label = soup.find(
        lambda t: getattr(t, "name", "") in {"div", "p", "span", "strong", "h2", "h3", "h4"}
        and _label_text(t) == "genres"
    )

    genre_container = None
    if genre_label:
        label_parent = genre_label.parent if getattr(genre_label, "parent", None) else None
        candidates = [
            _next_tag_sibling(genre_label),
            _first_direct_list(label_parent),
            _next_tag_sibling(label_parent),
        ]
        for candidate in candidates:
            genre_container = _first_direct_list(candidate)
            if genre_container:
                break

    if not genre_container:
        artist_heading = soup.find("h1")
        hero_container = artist_heading.parent if artist_heading and getattr(artist_heading, "parent", None) else None
        if hero_container:
            for child in _direct_child_tags(hero_container):
                if child is artist_heading:
                    break
                genre_container = _first_direct_list(child)
                if genre_container:
                    break

    if not genre_container:
        logger.debug("[Unearthed] Genre container not found on profile page.")
        return ""

    return genre_container.get_text(" / ", strip=True)


# Backwards compatibility for legacy imports.
_unearthed_extract_genre_text = extract_unearthed_genre_text
