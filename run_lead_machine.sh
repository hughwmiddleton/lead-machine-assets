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

# (Optional) Install dependencies if needed
# pip install pandas tqdm selenium beautifulsoup4 webdriver_manager PyQt5

# Run the Lead Machine program from the updated source directory.
python "Lead Machine (Final Update 5).py"
