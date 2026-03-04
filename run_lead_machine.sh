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
# =====================================================
#!/bin/bash
set -euo pipefail

# =====================================================
# GENERAL
# =====================================================
unset SC_DEBUG_LATEST
export PYTHONFAULTHANDLER=1

# Optional: load secrets/tokens (DO NOT COMMIT .env.local)
# [ -f ".env.local" ] && source ".env.local"

# Enrichment execution mode:
# - row_linear: per-row across sources
# - source_phased: sweep each source across all rows (better when cooldowns happen)
export ENRICHMENT_MODE="source_phased"

# Qt GUI visible (macOS cocoa)
unset QT_QPA_PLATFORM

# =====================================================
# EMAIL_ALL / QUARANTINE DEBUG
# =====================================================
export EMAIL_ALL_LOG=1
export EMAIL_ALL_GUARD=1

# =====================================================
# FB TUNING / DEBUG (High Signal, Low Noise)
# =====================================================
export FB_ANCHOR_WAIT_S=6

export FB_DEBUG_CAND_META=1
export FB_DEBUG_CAND_META_N=12
export FB_DEBUG_CANDIDATES=1

export FB_DEBUG_DOM_GATE=1
unset FB_DEBUG_DOM_GATE_HREFS
unset FB_DEBUG_DOM_GATE_HREFS_N

export FB_DEBUG_RANK_SORT=1
export FB_CANDIDATE_RANKING=1
export FB_CANDIDATE_RANKING_PREVIEW_N=10

# Refine pass ON (fix: "unset VAR=1" is invalid bash)
export FB_REFINE_QUERY=1

# Leave deep internals OFF
unset FB_DEBUG_CAND_GATE
unset FB_DEBUG_MUSIC_SIGNALS
unset FB_DEBUG_CAND_GATE_ASSERT
unset FB_CANDIDATE_RANKING_DEBUG

# No automated login typing
unset FB_ALLOW_AUTOMATED_LOGIN

# =====================================================
# FB FEATURES (CORE TEST)
# =====================================================
export FB_SEARCH_HARVEST_V2=1
export NIGHT_FB_DOM_FALLBACK=1

export NIGHT_FB_MIN_QUALITY_GATE=1
export NIGHT_FB_MIN_QUALITY_SCORE=25

export NIGHT_FB_CHECKPOINT_GUARD=1

export NIGHT_FB_PROFILE_DIR="/Users/hughmiddleton/Lead Machine/Lead Machine Code/night_fb_profile"

# Disable email override debug (use real behaviour)
unset FB_DEBUG_EMAIL_OVERRIDE

# =====================================================
# SOUNDCloud (Yield-critical)
# =====================================================
export NIGHT_SC_DEBUG=1
export NIGHTMODE_SC_ENGINE=1

# Make sure About/links scraping is allowed (do NOT set this to 1)
export SC_ADAPTIVE_ABOUT_DISABLE=0

# Working client_id from Chrome devtools
export SC_CLIENT_ID="1lzwHiVxAHeYKAMqN0IIGD3ZARgJy2kl"

# =====================================================
# BANDCAMP (minimal debug)
# =====================================================
unset BC_DEBUG_FILTER_SRC
unset BC_DEBUG_LOCATION

# =====================================================
# RUN
# =====================================================
python3 "Lead Machine (Final Update 5).py"