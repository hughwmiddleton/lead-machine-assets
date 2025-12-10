#!/usr/bin/env python3
"""SoundCloud metadata enricher for filling Primary Genre and Release Date."""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests


HUMAN_DATE_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)


def is_blank(value: Optional[str]) -> bool:
    return value is None or str(value).strip() == ""


def classify_soundcloud_url(url: str) -> Literal["track", "profile", "unknown"]:
    parsed = urlparse(url)
    path_segments = [seg for seg in parsed.path.split("/") if seg]

    if not path_segments:
        return "unknown"

    lower_segments = [seg.lower() for seg in path_segments]
    if lower_segments[-1] in {"likes", "favorites"}:
        return "profile"
    if len(lower_segments) >= 2 and lower_segments[-2:] in (["likes", "tracks"], ["favorites", "tracks"]):
        return "profile"

    if len(path_segments) == 1:
        return "profile"

    if len(path_segments) >= 2:
        second = lower_segments[1]
        if len(path_segments) == 2 and second in {"likes", "favorites", "sets"}:
            return "profile"
        return "track"

    return "unknown"


def _find_first_value(obj: Any, keys: Iterable[str]) -> Optional[Any]:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key]:
                return obj[key]
        for value in obj.values():
            found = _find_first_value(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first_value(item, keys)
            if found is not None:
                return found
    return None


@dataclass
class SoundCloudMetadata:
    genre: Optional[str]
    release_date: Optional[str]
    bio_text: Optional[str] = None
    inferred_genre: Optional[str] = None
    genre_source: Optional[str] = None


GENRE_KEYWORDS: dict[str, list[str]] = {
    "Indie": ["indie", "shoegaze", "bedroom pop"],
    "Rock": ["rock", "metal", "punk", "grunge"],
    "Pop": ["pop", "synthpop", "electropop"],
    "Electronic": [
        "electronic",
        "edm",
        "idm",
        "synthwave",
        "electro",
        "techno",
        "house",
        "deep house",
        "trance",
        "drum and bass",
        "dnb",
        "dubstep",
        "bassline",
        "garage",
    ],
    "Hip-hop & Rap": ["hip hop", "hip-hop", "rap", "trap", "boom bap"],
    "R&B & Soul": ["r&b", "soul", "neo soul", "neo-soul"],
    "Folk & Singer-Songwriter": ["folk", "singer-songwriter", "acoustic", "americana"],
    "Jazz": ["jazz", "swing", "bebop", "fusion"],
    "Classical": ["classical", "orchestral", "symphony"],
    "World": ["world", "afrobeat", "cumbia", "reggaeton"],
    "Soundtrack": ["soundtrack", "score", "ost", "cinematic"],
    "Ambient": ["ambient", "drone", "soundscape"],
    "Experimental": ["experimental", "avant-garde", "noise"],
    "Trance": ["trance", "uplifting trance", "psytrance"],
    "Dubstep": ["dubstep", "riddim"],
}


def infer_genre_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    cleaned = text.lower().strip()
    if len(cleaned) < 3:
        return None, None

    best_genre = None
    best_score = 0
    for genre_name, keywords in GENRE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword in cleaned:
                score += 1
        if score > best_score:
            best_score = score
            best_genre = genre_name

    if best_score <= 0:
        return None, None
    return best_genre, "keyword_heuristic"


def infer_genre_from_metadata(
    title: Optional[str],
    username: Optional[str],
    bio_text: Optional[str],
    extra_text: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    parts = [p for p in (title, username, bio_text, extra_text) if p]
    if not parts:
        return None, None
    combined = " ".join(parts)
    return infer_genre_from_text(combined)


class SoundCloudMetadataClient:
    def __init__(self, sleep_seconds: float = 1.0) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/117.0.0.0 Safari/537.36"
                )
            }
        )
        self.sleep_seconds = max(0.0, float(sleep_seconds))

    def _respect_rate_limit(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        url = url.strip()
        if not url:
            return ""
        if not url.startswith("http"):
            return ""
        parsed = urlparse(url)
        path_segments = [seg for seg in parsed.path.split("/") if seg]
        cleaned_path = "/" + "/".join(path_segments) if path_segments else ""
        cleaned_path = cleaned_path.rstrip("/")
        normalized = parsed._replace(path=cleaned_path, query="", fragment="")
        return urlunparse(normalized)

    def _normalize_profile_url(self, url: str) -> str:
        normalized = self._normalize_url(url)
        if not normalized:
            return ""
        parsed = urlparse(normalized)
        path_segments = [seg for seg in parsed.path.split("/") if seg]
        if not path_segments:
            return ""

        # Remove trailing likes/favorites segments, including /likes/tracks.
        lower_segments = [seg.lower() for seg in path_segments]
        if len(lower_segments) >= 2 and lower_segments[-2:] in (["likes", "tracks"], ["favorites", "tracks"]):
            path_segments = path_segments[:-2]
        elif lower_segments and lower_segments[-1] in {"likes", "favorites"}:
            path_segments = path_segments[:-1]

        # Reduce to root profile path.
        if path_segments:
            path_segments = [path_segments[0]]
        cleaned_path = "/" + "/".join(path_segments)
        cleaned_path = cleaned_path.rstrip("/")
        return urlunparse(parsed._replace(path=cleaned_path, query="", fragment=""))

    def fetch_metadata_for_url(self, url: str) -> SoundCloudMetadata:
        normalized = self._normalize_url(url)
        if not normalized:
            return SoundCloudMetadata(None, None)

        url_type = classify_soundcloud_url(normalized)
        if url_type == "track":
            return self._fetch_track_metadata(normalized)
        if url_type == "profile":
            normalized_profile = self._normalize_profile_url(normalized)
            if not normalized_profile:
                return SoundCloudMetadata(None, None)
            track_urls, profile_genre, bio_text = self._find_track_candidates_from_profile(normalized_profile)
            parsed_profile = urlparse(normalized_profile)
            profile_username = next((seg for seg in parsed_profile.path.split("/") if seg), None)

            for candidate in track_urls:
                meta = self._fetch_track_metadata(
                    candidate,
                    bio_text=bio_text,
                    profile_username=profile_username,
                )
                if bio_text and not meta.bio_text:
                    meta.bio_text = bio_text
                if meta.genre_source is None:
                    meta.genre_source = "track+profile_bio" if bio_text else "track"
                if meta.genre or meta.release_date or meta.inferred_genre:
                    return meta

            genre_source = "profile" if profile_genre else None
            inferred_genre, inferred_source = infer_genre_from_metadata(
                title=None,
                username=profile_username,
                bio_text=bio_text,
                extra_text=None,
            )
            final_genre = profile_genre or inferred_genre
            final_source = genre_source or inferred_source
            return SoundCloudMetadata(
                final_genre,
                None,
                bio_text=bio_text,
                inferred_genre=inferred_genre,
                genre_source=final_source,
            )
        return SoundCloudMetadata(None, None)

    def _find_track_candidates_from_profile(
        self, profile_url: str
    ) -> tuple[list[str], Optional[str], Optional[str]]:
        try:
            resp = self.session.get(profile_url, timeout=15)
            if resp.status_code != 200:
                print(f"[WARN] Profile fetch failed ({resp.status_code}) for {profile_url}")
                return [], None, None
            self._respect_rate_limit()
        except Exception as exc:
            print(f"[WARN] Profile fetch error for {profile_url}: {exc}")
            return [], None, None

        parsed = urlparse(profile_url)
        user_segment = [seg for seg in parsed.path.split("/") if seg]
        user_part = re.escape(user_segment[0]) if user_segment else r"[^/]+"
        relative_pattern = re.compile(r'href=["\'](/{}[^"\'?#\s]+/[^"\'?#\s]+)["\']'.format(user_part), re.IGNORECASE)
        absolute_pattern = re.compile(
            r'https?://soundcloud\.com/{}[^"\'?#\s]+/[^"\'?#\s]+'.format(user_part),
            re.IGNORECASE,
        )

        track_urls: list[str] = []
        seen: set[str] = set()

        for match in relative_pattern.finditer(resp.text):
            track_path = match.group(1)
            if not track_path:
                continue
            full_url = f"https://soundcloud.com{track_path.rstrip('/')}"
            if full_url not in seen:
                seen.add(full_url)
                track_urls.append(full_url)
            if len(track_urls) >= 3:
                break

        if len(track_urls) < 3:
            for match in absolute_pattern.finditer(resp.text):
                track_path = match.group(0)
                if not track_path:
                    continue
                full_url = track_path.rstrip("/")
                if full_url not in seen:
                    seen.add(full_url)
                    track_urls.append(full_url)
                if len(track_urls) >= 3:
                    break

        profile_genre = self._extract_profile_genre(resp.text)
        bio_text = self._extract_profile_bio(resp.text)
        if not track_urls and not profile_genre:
            print(f"[WARN] No track link found on profile {profile_url}")
        return track_urls, profile_genre, bio_text

    def _fetch_track_metadata(
        self,
        url: str,
        *,
        bio_text: Optional[str] = None,
        profile_username: Optional[str] = None,
    ) -> SoundCloudMetadata:
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                print(f"[WARN] Track fetch failed ({resp.status_code}) for {url}")
                return SoundCloudMetadata(None, None)
            html = resp.text
            self._respect_rate_limit()
        except Exception as exc:
            print(f"[WARN] Track fetch error for {url}: {exc}")
            return SoundCloudMetadata(None, None)

        genre = None
        release_date = None
        track_title: Optional[str] = None
        artist_name: Optional[str] = profile_username
        description_text: Optional[str] = None
        tag_text: Optional[str] = None
        genre_source: Optional[str] = None

        json_meta = self._extract_json_metadata(html)
        if json_meta:
            genre_value = _find_first_value(json_meta, ("genre",))
            if isinstance(genre_value, str) and genre_value.strip():
                genre = genre_value.strip()
                genre_source = "track"

            created_at = _find_first_value(json_meta, ("created_at", "createdAt"))
            formatted = self._format_date(created_at) if isinstance(created_at, str) else None
            if formatted:
                release_date = formatted

            title_value = _find_first_value(json_meta, ("title",))
            if isinstance(title_value, str) and title_value.strip():
                track_title = title_value.strip()

            username_value = _find_first_value(json_meta, ("username", "user_name", "artist"))
            if isinstance(username_value, str) and username_value.strip():
                artist_name = username_value.strip()

            description_value = _find_first_value(json_meta, ("description", "bio"))
            if isinstance(description_value, str) and description_value.strip():
                description_text = description_value.strip()

            tag_value = _find_first_value(json_meta, ("tag_list", "tags"))
            if isinstance(tag_value, str) and tag_value.strip():
                tag_text = tag_value.strip()

        if not genre:
            genre = self._extract_genre_from_meta(html)
            if genre:
                genre_source = genre_source or "track"

        if not release_date:
            release_date = self._extract_human_date(html)

        if not track_title:
            track_title = self._extract_title_from_meta(html)
        if not description_text:
            description_text = self._extract_meta_description(html)
        if not artist_name:
            artist_name = self._extract_username_from_url(url)

        extra_text_parts = [part for part in (description_text, tag_text) if part]
        extra_text = " ".join(extra_text_parts) if extra_text_parts else None

        inferred_genre, inferred_source = infer_genre_from_metadata(
            title=track_title,
            username=artist_name,
            bio_text=bio_text,
            extra_text=extra_text,
        )

        final_genre = genre or inferred_genre
        final_source = genre_source or inferred_source

        return SoundCloudMetadata(
            final_genre,
            release_date,
            bio_text=bio_text,
            inferred_genre=inferred_genre,
            genre_source=final_source,
        )

    def _extract_json_metadata(self, html: str) -> Optional[Any]:
        script_pattern = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
        for match in script_pattern.finditer(html):
            script_content = match.group(1)
            if '"genre"' in script_content and ('"created_at"' in script_content or '"title"' in script_content):
                cleaned = script_content.strip()
                candidates = [cleaned]
                brace_start = cleaned.find("{")
                brace_end = cleaned.rfind("}")
                if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
                    candidates.append(cleaned[brace_start : brace_end + 1])
                bracket_start = cleaned.find("[")
                bracket_end = cleaned.rfind("]")
                if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
                    candidates.append(cleaned[bracket_start : bracket_end + 1])
                for candidate in candidates:
                    try:
                        return json.loads(candidate)
                    except Exception:
                        continue
        return None

    def _extract_genre_from_meta(self, html: str) -> Optional[str]:
        meta_pattern = re.compile(
            r'<meta\s+(?:property|name)="(?:music:genre|og:audio:genre)"\s+content="([^"]+)"',
            re.IGNORECASE,
        )
        match = meta_pattern.search(html)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate
        return None

    def _extract_profile_genre(self, html: str) -> Optional[str]:
        # Try structured descriptions first (og:description / twitter:description / meta description).
        desc_pattern = re.compile(
            r'<meta\s+(?:property|name)="(?:og:description|twitter:description|description)"\s+content="([^"]+)"',
            re.IGNORECASE,
        )
        match = desc_pattern.search(html)
        if match:
            desc = html_module.unescape(match.group(1)).strip()
            if desc:
                if "discover followers on soundcloud" in desc.lower():
                    return None
                if desc.lower().startswith("play ") and "soundcloud" in desc.lower():
                    return None
                return desc

        # Fallback: look for hashtags like # Alternative or # Rock in the HTML text.
        hashtag_pattern = re.compile(r"#\s*([A-Za-z][A-Za-z0-9 &'\\-/]+)")
        match = hashtag_pattern.search(html)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate

        return None

    def _extract_profile_bio(self, html: str) -> Optional[str]:
        bio_text = None
        script_pattern = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
        for match in script_pattern.finditer(html):
            script_content = match.group(1)
            if not script_content or ("description" not in script_content and "bio" not in script_content):
                continue
            cleaned = script_content.strip()
            candidates = []
            brace_start = cleaned.find("{")
            brace_end = cleaned.rfind("}")
            if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
                candidates.append(cleaned[brace_start : brace_end + 1])
            bracket_start = cleaned.find("[")
            bracket_end = cleaned.rfind("]")
            if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
                candidates.append(cleaned[bracket_start : bracket_end + 1])
            for candidate in candidates:
                try:
                    parsed = json.loads(candidate)
                except Exception:
                    continue
                found = _find_first_value(parsed, ("description", "bio", "bio_text"))
                if isinstance(found, str) and found.strip():
                    bio_text = found.strip()
                    break
            if bio_text:
                break

        if not bio_text:
            bio_text = self._extract_meta_description(html)

        if bio_text:
            bio_text = re.sub(r"\s+", " ", bio_text).strip()
            lower = bio_text.lower()
            if len(bio_text) < 10:
                return None
            if "discover followers on soundcloud" in lower:
                return None
            if lower.startswith("play ") and "soundcloud" in lower:
                return None
            return bio_text
        return None

    def _extract_meta_description(self, html: str) -> Optional[str]:
        meta_desc_pattern = re.compile(
            r'<meta\s+(?:property|name)="description"\s+content="([^"]+)"',
            re.IGNORECASE,
        )
        match = meta_desc_pattern.search(html)
        if match:
            candidate = html_module.unescape(match.group(1)).strip()
            if candidate:
                return candidate
        return None

    def _extract_title_from_meta(self, html: str) -> Optional[str]:
        title_pattern = re.compile(
            r'<meta\s+(?:property|name)="og:title"\s+content="([^"]+)"',
            re.IGNORECASE,
        )
        match = title_pattern.search(html)
        if match:
            candidate = html_module.unescape(match.group(1)).strip()
            if candidate:
                return candidate
        title_tag = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_tag:
            candidate = html_module.unescape(title_tag.group(1)).strip()
            if candidate:
                return candidate
        return None

    def _extract_username_from_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            return segments[0]
        return None

    def _extract_human_date(self, html: str) -> Optional[str]:
        match = HUMAN_DATE_PATTERN.search(html)
        if not match:
            return None
        try:
            dt = datetime.strptime(match.group(0), "%B %d, %Y")
            return f"{dt.day}/{dt.month}/{dt.year}"
        except Exception:
            return None

    def _format_date(self, date_str: str) -> Optional[str]:
        if not date_str:
            return None
        cleaned = date_str.strip()
        if not cleaned:
            return None
        try:
            normalized = cleaned.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            return f"{dt.day}/{dt.month}/{dt.year}"
        except Exception:
            pass
        iso_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
        if iso_match:
            try:
                dt = datetime(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
                return f"{dt.day}/{dt.month}/{dt.year}"
            except Exception:
                return None
        return None


def enrich_soundcloud_metadata(
    input_csv: str,
    output_csv: Optional[str] = None,
    *,
    max_rows: Optional[int] = None,
    skip_existing: bool = True,
    sleep_seconds: float = 1.0,
) -> str:
    client = SoundCloudMetadataClient(sleep_seconds=sleep_seconds)

    with open(input_csv, newline="", encoding="utf-8") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = list(reader.fieldnames or [])
        if "Release Date" not in fieldnames:
            fieldnames.append("Release Date")
        if "Primary Genre" not in fieldnames:
            fieldnames.append("Primary Genre")
        if "Genre Source" not in fieldnames:
            fieldnames.append("Genre Source")
        rows = list(reader)

    rows_processed = 0
    rows_updated = 0
    errors = 0

    for idx, row in enumerate(rows):
        if max_rows is not None and rows_processed >= max_rows:
            break

        rows_processed += 1
        sc_url = (row.get("SoundCloud Link") or "").strip()
        if not sc_url:
            continue

        existing_date = (row.get("Release Date") or "").strip()
        existing_genre = (row.get("Primary Genre") or "").strip()

        needs_date = is_blank(existing_date)
        needs_genre = is_blank(existing_genre)

        if skip_existing and not (needs_date or needs_genre):
            continue

        try:
            metadata = client.fetch_metadata_for_url(sc_url)
        except Exception as exc:
            print(f"[WARN] Row {idx + 1}: error fetching {sc_url}: {exc}")
            errors += 1
            continue

        updated = False

        if metadata.release_date and (needs_date or not skip_existing):
            row["Release Date"] = metadata.release_date
            updated = True

        sc_genre = (metadata.genre or "").strip()
        inferred_genre = (metadata.inferred_genre or "").strip()
        genre_source = (metadata.genre_source or "").strip()

        if sc_genre:
            if needs_genre or not skip_existing:
                row["Primary Genre"] = sc_genre
                row["Genre Source"] = genre_source or "track"
                updated = True
        elif inferred_genre:
            if needs_genre or not skip_existing:
                row["Primary Genre"] = inferred_genre
                row["Genre Source"] = genre_source or "keyword_heuristic"
                updated = True

        if updated:
            rows_updated += 1

    if output_csv is None:
        base, ext = os.path.splitext(input_csv)
        output_csv = f"{base}_sc_enriched{ext or '.csv'}"

    with open(output_csv, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"[SoundCloud Enricher] Processed {rows_processed} rows, "
        f"updated {rows_updated} rows, errors {errors}, wrote: {output_csv}"
    )
    return output_csv


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Enrich SoundCloud CSV with genre and release date metadata."
    )
    parser.add_argument("-i", "--input", required=True, help="Path to input SoundCloud raw CSV")
    parser.add_argument("-o", "--output", help="Path for enriched output CSV (optional)")
    parser.add_argument("-m", "--max-rows", type=int, help="Optional max number of rows to process")
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Overwrite existing Release Date / Primary Genre values",
    )
    parser.add_argument(
        "-s",
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to sleep between HTTP requests (default: 1.0)",
    )

    args = parser.parse_args()
    skip_existing = not args.no_skip_existing

    enrich_soundcloud_metadata(
        input_csv=args.input,
        output_csv=args.output,
        max_rows=args.max_rows,
        skip_existing=skip_existing,
        sleep_seconds=args.sleep,
    )
