from typing import Dict, List, Optional
from urllib.parse import urlparse


def _normalize_name(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in name)
    return " ".join(cleaned.split())


def _extract_bandcamp_subdomain(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "bandcamp.com" not in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host.endswith("bandcamp.com"):
        return None
    subdomain = host.rsplit(".bandcamp.com", 1)[0]
    return subdomain or None


def _extract_soundcloud_handle(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "soundcloud.com" not in host:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    return path_parts[0] if path_parts else None


def _extract_unearthed_slug(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if "triplejunearthed" not in parsed.netloc.lower():
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    return path_parts[0] if path_parts else None


def _first_external_link(row: Dict[str, object]) -> Optional[str]:
    links = row.get("External Links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, str) and link.strip():
                return link.strip()
    elif isinstance(links, str) and links.strip():
        # assume semicolon delimited
        for segment in links.split(";"):
            candidate = segment.strip()
            if candidate:
                return candidate
    return None


def resolve_identity_keys(row: Dict[str, object]) -> List[str]:
    keys: List[str] = []

    spotify_id = (
        row.get("Spotify_Artist_ID")
        or row.get("Spotify Artist ID")
        or row.get("spotify_artist_id")
    )
    if isinstance(spotify_id, str) and spotify_id.strip():
        keys.append(f"spotify:{spotify_id.strip()}")

    source_url = row.get("Source URL") if isinstance(row.get("Source URL"), str) else None
    external_link = _first_external_link(row)

    bandcamp_subdomain = None
    for candidate in (source_url, external_link):
        if candidate:
            bandcamp_subdomain = _extract_bandcamp_subdomain(candidate)
        if bandcamp_subdomain:
            keys.append(f"bandcamp:{bandcamp_subdomain}")
            break

    soundcloud_link = row.get("SoundCloud Link")
    soundcloud_url = soundcloud_link if isinstance(soundcloud_link, str) else None
    handle = _extract_soundcloud_handle(soundcloud_url) if soundcloud_url else None
    if handle:
        keys.append(f"soundcloud:{handle}")

    unearthed_slug = _extract_unearthed_slug(source_url) if source_url else None
    if unearthed_slug:
        keys.append(f"unearthed:{unearthed_slug}")

    primary_url = None
    for candidate in (
        source_url,
        soundcloud_url,
        row.get("Spotify_URL") if isinstance(row.get("Spotify_URL"), str) else None,
        external_link,
    ):
        if candidate:
            primary_url = candidate
            break

    artist_name = row.get("Artist Name") if isinstance(row.get("Artist Name"), str) else None
    if artist_name:
        normalized_name = _normalize_name(artist_name)
        if normalized_name and primary_url:
            keys.append(f"nameurl:{normalized_name}|{primary_url}")

    return keys
