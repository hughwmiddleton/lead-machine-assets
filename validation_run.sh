#!/bin/bash
set -e

PROJECT_DIR="/Users/hughmiddleton/Lead Machine/Lead Machine VS Code/lead-machine-assets"
VENV_DIR="/Users/hughmiddleton/Lead Machine/Lead Machine Code/venv"

cd "$PROJECT_DIR"

if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
fi

# Load local secrets if present
if [ -f ".env.local" ]; then
    source ".env.local"
else
    echo "Warning: .env.local not found. Expected at $(pwd)/.env.local" >&2
    echo "Some features (Last.fm, Spotify, SoundCloud) may be unavailable." >&2
fi

# Export required credential variables
export LASTFM_API_KEY="${LASTFM_API_KEY:-}"
export SPOTIFY_CLIENT_ID="${SPOTIFY_CLIENT_ID:-}"
export SPOTIFY_CLIENT_SECRET="${SPOTIFY_CLIENT_SECRET:-}"
export SPOTIFY_REDIRECT_URI="${SPOTIFY_REDIRECT_URI:-http://127.0.0.1:8080/callback}"
export SPOTIFY_REFRESH_TOKEN="${SPOTIFY_REFRESH_TOKEN:-}"
export SC_CLIENT_ID="${SC_CLIENT_ID:-}"

export PYTHONFAULTHANDLER=1
export ENRICHMENT_MODE="source_phased"
export EMAIL_ALL_LOG=1
export EMAIL_ALL_GUARD=1
export FB_ANCHOR_WAIT_S=6
export FB_DEBUG_CAND_META=1
export FB_DEBUG_CAND_META_N=12
export FB_DEBUG_CANDIDATES=1
export FB_DEBUG_DOM_GATE=1
export FB_DEBUG_RANK_SORT=1
export FB_CANDIDATE_RANKING=1
export FB_CANDIDATE_RANKING_PREVIEW_N=10
export FB_REFINE_QUERY=1
export FB_SEARCH_HARVEST_V2=1
export NIGHT_FB_DOM_FALLBACK=1
export NIGHT_FB_MIN_QUALITY_GATE=1
export NIGHT_FB_MIN_QUALITY_SCORE=25
export NIGHT_FB_CHECKPOINT_GUARD=1
export NIGHT_FB_PROFILE_DIR="/Users/hughmiddleton/Lead Machine/Lead Machine Code/night_fb_profile"
export NIGHT_SC_DEBUG=1
export NIGHTMODE_SC_ENGINE=1
export SC_ADAPTIVE_ABOUT_DISABLE=0
export SC_DEBUG_FALLBACK_GATE=1
export SC_ALLOW_FALLBACK_ON_TRACKS_401_403=1

python3 night_mode_runner.py --config validation_run_config.json --run-root overnight_runs --stop-on-failure 2>&1
