#!/bin/bash
set -e

PROJECT_DIR="/Users/hughmiddleton/Lead Machine/Lead Machine VS Code/lead-machine-assets"
VENV_DIR="/Users/hughmiddleton/Lead Machine/Lead Machine Code/venv"

cd "$PROJECT_DIR"

if [ -d "$VENV_DIR" ]; then
    # Activate the shared virtual environment if it exists.
    source "$VENV_DIR/bin/activate"
else
    echo "Warning: virtual environment not found at $VENV_DIR" >&2
fi

# Provide Last.fm API key for this session so the scraper can run from VS Code.
export LASTFM_API_KEY="7bc79636d72e2cb2fc4217aa7681199d"

# Spotify API credentials for playlist + About-page scraping.
export SPOTIFY_CLIENT_ID="d32944f1a2414cd7a1681b4759f6a402"
export SPOTIFY_CLIENT_SECRET="27188b55b8d94604a9a2172092e19416"
export SPOTIFY_REDIRECT_URI="http://127.0.0.1:8080/callback"
export SPOTIFY_REFRESH_TOKEN="AQB1vtP347IrhWrFAScJ_TwBSK0ZTiEdAbhxrmGf82vqmZIANMZdpLqnkpUDsEjGK9HZGGVfkfB9D915m28IK5CCAFFTMBwLd63n0UVmoYSSkjs_F8qXHJeDG-I0UgwrtAU"
# --- General ---
unset SC_DEBUG_LATEST

# FB: show candidate meta + rank preview
export FB_DEBUG_CAND_META=1
export FB_DEBUG_CAND_META_N=12
export FB_DEBUG_CANDIDATES=1

# Optional: show DOM gate summary (handy if it says container missing)
export FB_DEBUG_DOM_GATE=1
unset FB_DEBUG_DOM_GATE_HREFS

# Keep ranking preview on (already in your logs, but make sure it stays)
export FB_CANDIDATE_RANKING=1
export FB_CANDIDATE_RANKING_PREVIEW_N=10
export FB_DEBUG_EMAIL_OVERRIDE=1
unset FB_CANDIDATE_RANKING_DEBUG

# Leave these off unless you’re diagnosing final music gate internals
unset FB_DEBUG_CAND_GATE
unset FB_DEBUG_MUSIC_SIGNALS
unset FB_DEBUG_CAND_GATE_ASSERT

# FB automated login (only if you WANT Unearthed to share session)
# If you don’t want FB automation, keep it unset.
# export FB_ALLOW_AUTOMATED_LOGIN=1
unset FB_ALLOW_AUTOMATED_LOGIN

# SoundCloud debug (fine to keep if you're actively debugging SC challenge)
export NIGHT_SC_DEBUG=1

export PYTHONFAULTHANDLER=1
export BC_DEBUG_LOCATION=1
export BC_DEBUG_FILTER_SRC=1

# Night Mode SoundCloud engine switch
export NIGHTMODE_SC_ENGINE="t007"

# --- Bandcamp ---
export BC_DEBUG_LOCATION=1
export BC_DEBUG_FILTER_SRC=1


# (Optional) Install dependencies if needed
# pip install pandas tqdm selenium beautifulsoup4 webdriver_manager PyQt5

# Run the Lead Machine program from the updated source directory.
python3 "Lead Machine (Final Update 5).py"
