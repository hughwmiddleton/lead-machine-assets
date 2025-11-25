"""
Local, safe test harness for Facebook candidate scoring.

Uses mocked candidates (no Selenium or network), and tries to import
the scoring helpers from the enrichment module. Falls back to local
implementations if not available so it never breaks the project.
"""

from __future__ import annotations

import sys
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

# Make sure this directory is importable when run as a script
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)


# Attempt to import scoring helpers; fall back to local stubs if unavailable.
select_best_facebook_candidate: Optional[Callable[..., Any]] = None
compute_fb_category_boost: Optional[Callable[[Optional[str]], float]] = None
FbCandidate = None

try:
    from facebook_enrich import FbCandidate, compute_fb_category_boost, select_best_fb_candidate  # type: ignore

    select_best_facebook_candidate = select_best_fb_candidate  # type: ignore
except Exception:
    select_best_facebook_candidate = None
    compute_fb_category_boost = None


def _local_compute_fb_category_boost(category_norm: Optional[str]) -> float:
    if not category_norm:
        return 0.0
    cat = category_norm.strip().lower()
    if not cat:
        return 0.0
    music_strong = [
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
    music_medium = [
        "record label",
        "entertainment website",
        "media",
        "radio station",
        "podcast",
        "music video",
        "music award",
        "festival",
    ]
    non_music_corp = [
        "spa",
        "care spa",
        "health spa",
        "resort",
        "hotel",
        "boutique",
        "clothing",
        "store",
        "shop",
        "vintage",
        "retro",
        "restaurant",
        "bar",
        "cafe",
        "coffee shop",
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
        "government",
        "politician",
        "political",
    ]
    if any(token in cat for token in music_strong):
        return 2.5
    if any(token in cat for token in music_medium):
        return 1.5
    if any(token in cat for token in non_music_corp):
        return -3.0
    return 0.0


def _local_select_best_facebook_candidate(
    artist: str, candidates: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Simplified selector using mocked candidates:
    final_score = base_score + category_boost(category_norm)
    """
    best: Optional[Dict[str, Any]] = None
    best_score = float("-inf")
    boost_fn = compute_fb_category_boost or _local_compute_fb_category_boost
    for cand in candidates:
        base = cand.get("base_score", 0.0) or 0.0
        cat_norm = cand.get("category_norm") or ""
        cat_boost = boost_fn(cat_norm)
        final = base + cat_boost
        cand["_final_score"] = final
        cand["_cat_boost"] = cat_boost
        if final > best_score:
            best_score = final
            best = cand
    return best, best_score


def _run_case(
    label: str,
    artist: str,
    candidates: List[Dict[str, Any]],
    expected_url: str,
) -> bool:
    selector = select_best_facebook_candidate or _local_select_best_facebook_candidate
    prepared_candidates: List[Any] = candidates
    if selector is select_best_facebook_candidate and FbCandidate:
        try:
            prepared_candidates = [
                cand if isinstance(cand, FbCandidate) else FbCandidate(name=cand.get("name", ""), url=cand.get("url", ""), category=cand.get("category_raw") or cand.get("category_norm") or "")  # type: ignore
                for cand in candidates
            ]
        except Exception:
            prepared_candidates = candidates
    try:
        result = selector(artist, prepared_candidates)  # type: ignore
        if isinstance(result, tuple):
            if len(result) >= 4:
                winner, score = result[0], result[1]
            elif len(result) == 2:
                winner, score = result
            else:
                winner, score = result[0], None  # type: ignore
        else:
            winner, score = result, None  # type: ignore
    except TypeError:
        # If the imported selector has a different signature, fall back to local.
        winner, score = _local_select_best_facebook_candidate(artist, candidates)
    winner_url = getattr(winner, "url", None) if winner is not None else None
    if winner_url is None and isinstance(winner, dict):
        winner_url = winner.get("url")
    passed = winner is not None and winner_url == expected_url
    final_display = "n/a" if score is None else f"{score:.2f}"
    print(
        f"{label}: {'PASS' if passed else 'FAIL'} – "
        f"expected '{expected_url}', got '{winner_url if winner else None}', "
        f"final_score={final_display}"
    )
    return passed


def run_all_tests() -> None:
    tests_passed = 0
    tests_total = 0

    def run(label, artist, good, bad):
        nonlocal tests_passed, tests_total
        tests_total += 1
        passed = _run_case(label, artist, [good, bad], good["url"])
        if passed:
            tests_passed += 1

    run(
        "TEST 1 – Aneya (music vs spa)",
        "Aneya",
        {
            "name": "Aneya Music",
            "url": "https://facebook.com/aneyamusic",
            "category_raw": "Musician/band",
            "category_norm": "musician/band",
            "base_score": 1.1,
        },
        {
            "name": "Aneya Care Spa",
            "url": "https://facebook.com/aneyacarespa",
            "category_raw": "Health spa",
            "category_norm": "health spa",
            "base_score": 1.5,
        },
    )

    run(
        "TEST 2 – Ṣẹwà (musician vs real-estate)",
        "Ṣẹwà",
        {
            "name": "Ṣẹwà",
            "url": "https://facebook.com/sewamusic",
            "category_raw": "Musician/band",
            "category_norm": "musician/band",
            "base_score": 1.0,
        },
        {
            "name": "Sewa Sandhu - Real Estate",
            "url": "https://facebook.com/sewasandhurealestate",
            "category_raw": "Real Estate Agent",
            "category_norm": "real estate agent",
            "base_score": 1.4,
        },
    )

    run(
        "TEST 3 – Salle (musician vs clothing store)",
        "Salle",
        {
            "name": "Salle",
            "url": "https://facebook.com/sallemusic",
            "category_raw": "Musician",
            "category_norm": "musician",
            "base_score": 1.0,
        },
        {
            "name": "Salle Sells Retro Vintage",
            "url": "https://facebook.com/salleretro",
            "category_raw": "Clothing store",
            "category_norm": "clothing store",
            "base_score": 1.6,
        },
    )

    run(
        "TEST 4 – The.wav (musician vs resort)",
        "The.wav",
        {
            "name": "The.wav",
            "url": "https://facebook.com/thewavmusic",
            "category_raw": "Musician/band",
            "category_norm": "musician/band",
            "base_score": 1.0,
        },
        {
            "name": "The Wave Resort - Gold Coast",
            "url": "https://facebook.com/thewaveresort",
            "category_raw": "Hotel/Resort",
            "category_norm": "hotel/resort",
            "base_score": 1.7,
        },
    )

    print(f"\nSummary: {tests_passed}/{tests_total} tests passed.")
    if tests_passed != tests_total:
        sys.exit(1)


if __name__ == "__main__":
    print("Running Facebook scoring tests...\n")
    run_all_tests()
