"""
Local, stand-alone harness to exercise the Facebook candidate selector/scorer.

This file is SAFE: it is not imported by production code, does not touch Selenium
or the live pipeline, and only uses mocked candidates.
"""

from __future__ import annotations

import sys
import os
import argparse
import csv
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

# Ensure this folder is importable when run as a script
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

# Attempt to import existing helpers; fallback to stubs if unavailable.
try:
    from cross_directory_enricher import normalize_name, persistent_fb_driver  # type: ignore
except Exception:
    normalize_name = None  # type: ignore
    persistent_fb_driver = None  # type: ignore

# Optional: production helpers if available
try:
    from facebook_enrich import (  # type: ignore
        FbCandidate,
        compute_fb_category_boost,
        extract_fb_category,
        normalize_fb_name,
        score_fb_candidate,
        select_best_fb_candidate,
    )
except Exception:
    FbCandidate = None  # type: ignore
    compute_fb_category_boost = None  # type: ignore
    extract_fb_category = None  # type: ignore
    normalize_fb_name = None  # type: ignore
    score_fb_candidate = None  # type: ignore
    select_best_fb_candidate = None  # type: ignore


# --- Data model for local testing ---
if FbCandidate is None:
    @dataclass
    class FbCandidate:
        name: str
        url: str
        category: str = ""


# --- Constants mirroring production logic (category boost + corporate filters) ---
FB_CATEGORY_BOOSTS = {
    "musician/band": 2.5,
    "artist": 2.2,
    "band": 2.2,
    "singer": 2.0,
    "music": 2.0,
    "record label": 1.8,
    "entertainer": 1.5,
    "public figure": 1.0,
}

MUSIC_STRONG = [
    "musician/band",
    "musician",
    "band",
    "artist",
    "music",
    "singer",
    "singer-songwriter",
    "rapper",
    "dj",
    "producer",
    "recording studio",
    "music production studio",
    "music producer",
    "songwriter",
    "performing artist",
    "public figure",
    "entertainer",
]

MUSIC_MEDIUM = [
    "record label",
    "entertainment website",
    "media",
    "radio station",
    "podcast",
    "music video",
    "music award",
    "festival",
]

NON_MUSIC_CORPORATE = [
    "spa",
    "care spa",
    "health spa",
    "resort",
    "hotel",
    "lodge",
    "hostel",
    "boutique",
    "clothing",
    "store",
    "shop",
    "mart",
    "market",
    "vintage",
    "retro",
    "restaurant",
    "bar",
    "cafe",
    "coffee shop",
    "coffee",
    "real estate",
    "estate agent",
    "construction",
    "company",
    "ltd",
    "llc",
    "inc",
    "university",
    "college",
    "school",
    "church",
    "temple",
    "mosque",
    "ministry",
    "government",
    "politician",
    "political",
]

BLOCKED_TOKENS = [
    "games",
    "game",
    "creations",
    "store",
    "shop",
    "mart",
    "market",
    "company",
    "co ",
    " co.",
    "corp",
    "inc",
    "ltd",
    "llc",
    "exterior",
    "exteriors",
    "boutique",
    "farm",
    "farms",
    "international",
    "spa",
    "resort",
    "hotel",
    "lodge",
    "hostel",
    "restaurant",
    "bar",
    "cafe",
    "coffee",
    "school",
    "college",
    "university",
    "church",
    "ministry",
]
DOWNRANK_TOKENS = ["studio", "studios"]


# --- Helper functions (mirroring production scoring pieces) ---
def _local_compute_fb_category_boost(category_norm: Optional[str]) -> float:
    if not category_norm:
        return 0.0
    cat = category_norm.strip().lower()
    if not cat:
        return 0.0
    if cat in FB_CATEGORY_BOOSTS:
        return FB_CATEGORY_BOOSTS[cat]
    if any(token in cat for token in MUSIC_STRONG):
        return 2.5
    if any(token in cat for token in MUSIC_MEDIUM):
        return 1.5
    if any(token in cat for token in NON_MUSIC_CORPORATE):
        return -3.0
    if "entertainer" in cat:
        return FB_CATEGORY_BOOSTS["entertainer"]
    if "public figure" in cat:
        return FB_CATEGORY_BOOSTS["public figure"]
    return 0.0


compute_fb_category_boost_fn: Callable[[Optional[str]], float]
if compute_fb_category_boost:
    compute_fb_category_boost_fn = compute_fb_category_boost  # type: ignore
else:
    compute_fb_category_boost_fn = _local_compute_fb_category_boost


def _safe_normalize(value: str) -> str:
    if normalize_fb_name:
        try:
            return normalize_fb_name(value)
        except Exception:
            pass
    if normalize_name:
        try:
            return normalize_name(value)
        except Exception:
            pass
    return " ".join((value or "").lower().split())


def _local_score_candidate(artist_name: str, cand: FbCandidate) -> Tuple[float, float, float]:
    artist_norm = _safe_normalize(artist_name)
    page_name_norm = _safe_normalize(cand.name)
    username_norm = _safe_normalize((cand.url or "").rstrip("/").split("/")[-1])
    category_norm = _safe_normalize(cand.category)

    base_score = 0.0
    if page_name_norm == artist_norm:
        base_score += 1.0
    elif page_name_norm.startswith(artist_norm):
        base_score += 0.7
    elif artist_norm in page_name_norm:
        base_score += 0.4
    if username_norm == artist_norm:
        base_score += 1.0
    elif username_norm.startswith(artist_norm):
        base_score += 0.7

    context = " ".join(
        [
            (cand.url or "").lower(),
            username_norm,
            (cand.name or "").lower(),
        ]
    )
    corporate_hit = any(tok in context and tok not in artist_norm for tok in BLOCKED_TOKENS)
    if corporate_hit:
        return 0.0, base_score, 0.0

    if any(tok in context and tok not in artist_norm for tok in DOWNRANK_TOKENS):
        base_score = max(base_score - 0.2, 0.0)

    cat_boost = compute_fb_category_boost_fn(category_norm)
    final_score = base_score + cat_boost
    return final_score, base_score, cat_boost


def score_candidate(artist_name: str, cand: FbCandidate) -> Tuple[float, float, float]:
    """
    Returns (final_score, base_score, cat_boost)
    """
    if score_fb_candidate:
        try:
            scored = score_fb_candidate(artist_name, cand.name, cand.url, cand.category)  # type: ignore
            if scored is None:
                return (-1.0, 0.0, 0.0)
            return scored
        except Exception:
            pass
    return _local_score_candidate(artist_name, cand)


def choose_best_candidate(artist_name: str, candidates: List[FbCandidate]) -> Tuple[Optional[FbCandidate], float, float, float]:
    if select_best_fb_candidate:
        try:
            return select_best_fb_candidate(artist_name, candidates)  # type: ignore
        except Exception:
            pass
    best: Optional[FbCandidate] = None
    best_score = float("-inf")
    best_base = 0.0
    best_cat = 0.0
    for cand in candidates:
        final_score, base_score, cat_boost = score_candidate(artist_name, cand)
        if final_score < 0:
            continue
        better_tie = final_score == best_score and cat_boost > best_cat
        if final_score > best_score or better_tie:
            best = cand
            best_score = final_score
            best_base = base_score
            best_cat = cat_boost
    return best, best_score, best_base, best_cat


# --- CSV diagnostic helpers ---
def _sniff_csv(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except Exception:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        return list(reader)


def _extract_artist_name(row: dict) -> str:
    for key in ("Artist Name", "artist_name", "artist", "name", "Artist"):
        if key in row and row[key]:
            return str(row[key]).strip()
    return ""


def _get_fb_driver():
    driver = None
    # Prefer the production factory if present
    if create_facebook_driver:
        try:
            return create_facebook_driver()
        except Exception as exc:
            print(f"[diag] Unable to create Facebook driver (create_facebook_driver): {exc}")
    if persistent_fb_driver:
        try:
            driver = persistent_fb_driver()
        except Exception as exc:
            print(f"[diag] Unable to create Facebook driver (persistent_fb_driver): {exc}")
            driver = None
    if driver is None:
        print("[diag] Cannot create Facebook driver (no factory available).")
    return driver


def _fb_search_candidates_live(driver, artist_name: str) -> List[FbCandidate]:
    """
    Minimal re-use of production-style search: navigate to FB search and
    collect anchors, then score locally. This is diagnostic only.
    """
    try:
        from urllib.parse import quote_plus, urlparse
        from bs4 import BeautifulSoup
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
    except Exception as exc:
        print(f"[diag] Missing deps for live FB search: {exc}")
        return []

    query = quote_plus(artist_name.strip())
    search_url = f"https://www.facebook.com/search/pages/?q={query}"
    print(f"[diag] FB search URL: {search_url}")
    try:
        driver.get(search_url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception as exc:
        print(f"[diag] FB search failed for '{artist_name}': {exc}")
        return []

    dom_candidates = facebook_enrich._fb_extract_candidates_from_search_dom(
        driver.page_source or "",
        logger=print,
        debug=os.getenv("FB_DEBUG_DOM_GATE") == "1",
        search_name=artist_name,
    )
    return dom_candidates


def run_csv_diagnostics(seed_path: str, limit: Optional[int] = None) -> None:
    if not os.path.exists(seed_path):
        print(f"[diag] Seed file not found: {seed_path}")
        return
    rows = _sniff_csv(seed_path)
    if limit:
        rows = rows[: limit]
    driver = _get_fb_driver()
    if driver is None:
        print("[diag] Cannot run FB diagnostics without a driver.")
        return

    # Choose search/select helpers (prefer production if available)
    search_fn = search_facebook_candidates if search_facebook_candidates else _fb_search_candidates_live
    select_fn = select_best_fb_candidate if select_best_fb_candidate else choose_best_candidate

    try:
        for idx, row in enumerate(rows, start=1):
            artist_name = _extract_artist_name(row)
            if not artist_name:
                continue
            print("=" * 80)
            print(f"Artist: {artist_name}")
            print(f"Source row: {idx}")
            try:
                candidates = search_fn(driver, artist_name)
            except Exception as exc:
                print(f"[diag] Error searching FB for '{artist_name}': {exc}")
                continue
            if not candidates:
                print("- Candidates:\n  (none)")
                print(f"=> Chosen candidate:\n   (no high-confidence Facebook match for '{artist_name}')\n")
                continue
            # Score and print
            try:
                best_result = select_fn(artist_name, candidates)
                if isinstance(best_result, tuple) and len(best_result) >= 4:
                    best, best_score, best_base, best_cat = best_result
                elif isinstance(best_result, tuple) and len(best_result) == 2:
                    best, best_score = best_result
                    final_score, base_score, cat_boost = (0.0, 0.0, 0.0)
                    if best:
                        final_score, base_score, cat_boost = score_candidate(artist_name, best)
                    best_base, best_cat, best_score = base_score, cat_boost, final_score
                else:
                    best, best_score, best_base, best_cat = choose_best_candidate(artist_name, candidates)
            except Exception as exc:
                print(f"[diag] Error selecting best candidate for '{artist_name}': {exc}")
                continue
            print("- Candidates:")
            for cand in candidates:
                final_score, base_score, cat_boost = score_candidate(artist_name, cand)
                print(
                    f"  - {cand.name!r} (cat='{cand.category}') -> "
                    f"base={base_score:.2f} cat_boost={cat_boost:.2f} final={final_score:.2f} url={cand.url}"
                )
            print("\n=> Chosen candidate:")
            if best:
                print(
                    f"   {best.name!r} url={best.url} "
                    f"(final={best_score:.2f}, base={best_base:.2f}, cat_boost={best_cat:.2f}, cat='{best.category}')"
                )
            else:
                print(f"   (no high-confidence Facebook match for '{artist_name}')")
            print()
            time.sleep(0.5)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# --- Test scenarios ---
def build_candidate(name: str, url: str, category: str) -> FbCandidate:
    return FbCandidate(name=name, url=url, category=category)


def run_case(label: str, artist_name: str, candidates: List[FbCandidate]) -> None:
    print("=" * 80)
    print(f"Artist: {artist_name}")
    print(f"Case: {label}")
    print("- Candidates:")
    for cand in candidates:
        final_score, base_score, cat_boost = score_candidate(artist_name, cand)
        print(
            f"  - {cand.name!r} (cat='{cand.category}') -> "
            f"base={base_score:.2f} cat_boost={cat_boost:.2f} final={final_score:.2f} url={cand.url}"
        )
    best, best_score, best_base, best_cat = choose_best_candidate(artist_name, candidates)
    print("\n=> Chosen candidate:")
    if best:
        print(
            f"   {best.name!r} url={best.url} "
            f"(final={best_score:.2f}, base={best_base:.2f}, cat_boost={best_cat:.2f}, cat='{best.category}')"
        )
    else:
        print("   None")
    print()


def test_aneya() -> None:
    artist = "Aneya"
    candidates = [
        build_candidate("Aneya Care Spa", "https://www.facebook.com/aneyacarespa", "Spa, beauty & personal care"),
        build_candidate("ANEYA BOUTIQUE", "https://www.facebook.com/ANEYABOUTIQUE75", "Clothing store"),
        build_candidate("Aneya Music", "https://www.facebook.com/aneyamusic", "Artist · Musician/band"),
    ]
    run_case("TEST 1 – Aneya (music vs spa/boutique)", artist, candidates)


def test_sewa() -> None:
    artist = "Ṣẹwà"
    candidates = [
        build_candidate("Sewa Sandhu - Real Estate", "https://www.facebook.com/SewaSandhuRealEstate", "Real estate agent"),
        build_candidate("Ṣẹwà", "https://www.facebook.com/sewamusic", "Artist · Musician/band"),
    ]
    run_case("TEST 2 – Ṣẹwà (musician vs real-estate)", artist, candidates)


def test_salle() -> None:
    artist = "Salle"
    candidates = [
        build_candidate("Salle Sells Retro Vintage", "https://www.facebook.com/sallesells", "Thrift & consignment store"),
        build_candidate("Salle Music", "https://www.facebook.com/sallemusic", "Artist · Musician/band"),
    ]
    run_case("TEST 3 – Salle (musician vs clothing store)", artist, candidates)


def test_the_wav() -> None:
    artist = "The.wav"
    candidates = [
        build_candidate("The Wave Resort - Gold Coast", "https://www.facebook.com/TheWaveResort", "Hotel resort"),
        build_candidate("The Wav Records", "https://www.facebook.com/thewavrecords", "Record label · Musician/band"),
    ]
    run_case("TEST 4 – The.wav (musician vs resort)", artist, candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local FB scoring tester/diagnostic.")
    parser.add_argument("--seed", help="Path to seed CSV for live FB diagnostic mode.")
    parser.add_argument("--limit", type=int, default=None, help="Max rows to process from seed CSV.")
    args = parser.parse_args()

    if not args.seed:
        # Demo mode (unchanged behaviour)
        test_aneya()
        test_sewa()
        test_salle()
        test_the_wav()
        return

    run_csv_diagnostics(args.seed, limit=args.limit)


if __name__ == "__main__":
    main()
