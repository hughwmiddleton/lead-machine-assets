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
from typing import Any, Iterable, Literal, Optional
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

    if len(path_segments) == 1:
        return "profile"

    if len(path_segments) >= 2:
        second = path_segments[1].lower()
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
        if len(path_segments) >= 2 and path_segments[1].lower() in {"likes", "favorites", "sets"}:
            cleaned_path = f"/{path_segments[0]}"
        else:
            cleaned_path = "/" + "/".join(path_segments) if path_segments else ""
        cleaned_path = cleaned_path.rstrip("/")
        normalized = parsed._replace(path=cleaned_path, query="", fragment="")
        return urlunparse(normalized)

    def fetch_metadata_for_url(self, url: str) -> SoundCloudMetadata:
        normalized = self._normalize_url(url)
        if not normalized:
            return SoundCloudMetadata(None, None)

        url_type = classify_soundcloud_url(normalized)
        if url_type == "track":
            return self._fetch_track_metadata(normalized)
        if url_type == "profile":
            track_urls, profile_genre = self._find_track_candidates_from_profile(normalized)
            for candidate in track_urls:
                meta = self._fetch_track_metadata(candidate)
                if meta.genre or meta.release_date:
                    return meta
            if profile_genre:
                return SoundCloudMetadata(profile_genre, None)
            return SoundCloudMetadata(None, None)
        return SoundCloudMetadata(None, None)

    def _find_track_candidates_from_profile(self, profile_url: str) -> tuple[list[str], Optional[str]]:
        try:
            resp = self.session.get(profile_url, timeout=15)
            if resp.status_code != 200:
                print(f"[WARN] Profile fetch failed ({resp.status_code}) for {profile_url}")
                return [], None
            self._respect_rate_limit()
        except Exception as exc:
            print(f"[WARN] Profile fetch error for {profile_url}: {exc}")
            return [], None

        parsed = urlparse(profile_url)
        user_segment = [seg for seg in parsed.path.split("/") if seg]
        pattern = re.compile(
            r'href="(/{}[^"/?#\s]+/[^"/?#\s]+)"'.format(
                re.escape(user_segment[0]) if user_segment else r"[^/]+"
            ),
            re.IGNORECASE,
        )

        track_urls: list[str] = []
        for match in pattern.finditer(resp.text):
            track_path = match.group(1)
            if not track_path:
                continue
            full_url = f"https://soundcloud.com{track_path.rstrip('/')}"
            if full_url not in track_urls:
                track_urls.append(full_url)
            if len(track_urls) >= 3:
                break

        profile_genre = self._extract_profile_genre(resp.text)
        if not track_urls and not profile_genre:
            print(f"[WARN] No track link found on profile {profile_url}")
        return track_urls, profile_genre

    def _fetch_track_metadata(self, url: str) -> SoundCloudMetadata:
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

        json_meta = self._extract_json_metadata(html)
        if json_meta:
            genre_value = _find_first_value(json_meta, ("genre",))
            if isinstance(genre_value, str) and genre_value.strip():
                genre = genre_value.strip()

            created_at = _find_first_value(json_meta, ("created_at", "createdAt"))
            formatted = self._format_date(created_at) if isinstance(created_at, str) else None
            if formatted:
                release_date = formatted

        if not genre:
            genre = self._extract_genre_from_meta(html)

        if not release_date:
            release_date = self._extract_human_date(html)

        return SoundCloudMetadata(genre, release_date)

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

        if metadata.genre and (needs_genre or not skip_existing):
            row["Primary Genre"] = metadata.genre
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
