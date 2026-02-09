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
unset SC_DEBUG_LATEST
export FB_DEBUG_CANDIDATES=1
export NIGHT_SC_DEBUG=1

# Night Mode SoundCloud engine switch (default is "current")
export NIGHTMODE_SC_ENGINE="t007"
export FB_DEBUG_CAND_GATE=1
export FB_DEBUG_MUSIC_SIGNALS=1
export FB_DEBUG_CAND_GATE_ASSERT=1
export FB_DEBUG_DOM_GATE=1 
export FB_DEBUG_DOM_GATE_HREFS=1
export PYTHONFAULTHANDLER=1
# FB candidate ranking (NEW)
export FB_CANDIDATE_RANKING=1
export FB_CANDIDATE_RANKING_PREVIEW_N=8
unset FB_CANDIDATE_RANKING_DEBUG=1
unset FB_ALLOW_AUTOMATED_LOGIN

# (Optional) Install dependencies if needed
# pip install pandas tqdm selenium beautifulsoup4 webdriver_manager PyQt5

# Run the Lead Machine program from the updated source directory.
python3 "Lead Machine (Final Update 5).py"
