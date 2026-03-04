#!/usr/bin/env python3
"""Manual Spotify login to seed the persistent Playwright profile used by the
spotify_about_scraper. Opens a visible Chromium window; log in once and future
scrapes reuse the session/cookies stored in the same profile directory.

Usage:
    SPOTIFY_PW_PROFILE_DIR="/path/to/.cache/spotify_pw_profile" \
    python3 scripts/spotify_login_bootstrap.py
"""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def _resolve_profile_dir() -> Path:
    """Prefer the scraper's profile path to avoid drift; fall back to the same logic."""

    try:
        from spotify_about_scraper import _SPOTIFY_PW_PROFILE_DIR  # type: ignore

        return Path(_SPOTIFY_PW_PROFILE_DIR)
    except Exception:
        repo_root = Path(__file__).resolve().parent.parent
        fallback = os.environ.get("SPOTIFY_PW_PROFILE_DIR") or os.path.abspath(
            os.path.join(repo_root, ".cache", "spotify_pw_profile")
        )
        return Path(fallback)


def main() -> int:
    profile_dir = _resolve_profile_dir().expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"[Spotify login] Using profile dir: {profile_dir}")
    print("Log in, confirm you see your account avatar / logged-in state, then close the window.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            slow_mo=50,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://open.spotify.com/", wait_until="load")
            context.wait_for_event("close")
        except KeyboardInterrupt:
            print("Received interrupt; closing browser.")
        finally:
            try:
                context.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
