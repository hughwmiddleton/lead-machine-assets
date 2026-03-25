from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cross_directory_enricher import (  # type: ignore
    CrossDirectoryEnricherWorker,
    _canonicalize_instagram_profile_url,
    _fetch_instagram_profile_html,
)
from night_mode_fb import _extract_emails_from_html  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


def build_worker() -> CrossDirectoryEnricherWorker:
    return CrossDirectoryEnricherWorker(
        seed_csv_path="",
        bandcamp_csv_path="",
        soundcloud_csv_path="",
        lastfm_csv_path="",
        unearthed_csv_path="",
        output_csv_path="/tmp/ig_probe.csv",
    )


def collect_anchor_hrefs(html: str) -> list[str]:
    if not html or BeautifulSoup is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    hrefs: list[str] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href:
            hrefs.append(href)

    return hrefs


def normalize_extractor_result(result: object) -> list[str]:
    if isinstance(result, tuple):
        raw_emails = result[0]
    else:
        raw_emails = result

    if raw_emails is None:
        return []

    if isinstance(raw_emails, str):
        return [raw_emails]

    if isinstance(raw_emails, (set, tuple, list)):
        emails: list[str] = []
        for item in raw_emails:
            if isinstance(item, str) and item.strip():
                emails.append(item.strip())
        return sorted(set(emails))

    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instagram_url", help="Public Instagram profile URL")
    parser.add_argument(
        "--show-sample",
        action="store_true",
        help="Print a short sanitized HTML sample for debugging",
    )
    args = parser.parse_args()

    canonical = _canonicalize_instagram_profile_url(args.instagram_url)
    if not canonical:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "invalid_or_non_profile_instagram_url",
                    "input": args.instagram_url,
                },
                indent=2,
            )
        )
        raise SystemExit(1)

    worker = build_worker()

    html, status = _fetch_instagram_profile_html(worker, canonical)

    html_text = html or ""
    html_len = len(html_text)
    hrefs = collect_anchor_hrefs(html_text)

    extractor_result = _extract_emails_from_html(
        html_text,
        anchor_values=hrefs,
    )
    emails = normalize_extractor_result(extractor_result)

    meta = {
        "ok": True,
        "canonical_url": canonical,
        "http_status": status,
        "html_length": html_len,
        "anchor_count": len(hrefs),
        "emails_found": emails,
        "raw_extractor_result_type": str(type(extractor_result)),
        "raw_extractor_result_repr": repr(extractor_result),
        "fetch_state": (
            "fetch_failed"
            if status is None
            else "blocked_or_empty"
            if status != 200 or html_len < 500
            else "usable_page"
        ),
    }

    print(json.dumps(meta, indent=2))

    if args.show_sample and html_text:
        sample = re.sub(r"\s+", " ", html_text[:1200]).strip()
        print("\n--- HTML SAMPLE ---")
        print(sample)


if __name__ == "__main__":
    main()