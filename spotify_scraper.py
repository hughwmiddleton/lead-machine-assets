"""
Skeleton Spotify scraper module for Lead Machine.

This stub keeps the wiring lightweight and returns dummy rows so that the GUI
can call into a Spotify-specific entry point without touching any other
scrapers yet.
"""
import time
from typing import Callable, Dict, List, Optional

Row = Dict[str, str]
LoggerFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


def scrape_spotify(
    scrape_amount: int,
    params: Dict,
    logger: Optional[LoggerFn] = None,
    progress_callback: Optional[ProgressFn] = None,
) -> List[Row]:
    """
    Skeleton Spotify scraper for Lead Machine.

    - This is a safe, non-breaking stub.
    - For now, it returns dummy rows shaped like the other directory outputs.
    - Later we will replace the dummy data with real Spotify API + About-page scraping.

    MUST NOT modify behaviour of other scrapers when Spotify is not selected.
    """

    params = params or {}
    search_term = (params.get("search_term") or "").strip() or "indie rock"
    if logger:
        logger(f"[Spotify] Starting skeleton scrape for {scrape_amount} artists (term: '{search_term}')")
    rows: List[Row] = []

    for idx in range(1, max(scrape_amount, 0) + 1):
        artist_name = f"Spotify Test Artist {idx}"
        row: Row = {
            "Artist Name": artist_name,
            "Location": "",
            "Song Title": "",
            "Sounds Like": "",
            "Social Link": "",
            "SoundCloud Link": "",
            "Played on triple J": "",
            "Played on Unearthed": "",
            "Release Date": "",
            "Primary Genre": "spotify_stub",
            "Date Added": time.strftime("%d/%m/%Y"),
            "External Links": "",
            "Email": "",
            "Spotify_URL": f"https://open.spotify.com/artist/{idx:06d}",
            "Spotify_Artist_ID": f"stub-{idx:06d}",
            "Spotify_Instagram_URL": "",
            "Spotify_Facebook_URL": "",
            "Spotify_Twitter_URL": "",
            "Spotify_Website_URL": "",
        }
        rows.append(row)

        if progress_callback:
            progress_callback(idx, scrape_amount)

    if logger:
        logger(f"[Spotify] Skeleton scrape complete — generated {len(rows)} rows (dummy data)")

    return rows
