#!/usr/bin/env bash
set -euo pipefail

# Create and open an isolated Chrome profile for Selenium.
# Usage: scripts/bootstrap_profile.sh [profile_name] [extra chrome args...]
# Defaults to profile "fb_logged_in" under ${SELENIUM_PROFILE_BASE:-$HOME/selenium-profiles}.

PROFILE_NAME="${SELENIUM_PROFILE_NAME:-fb_logged_in}"
if [[ $# -ge 1 ]]; then
  PROFILE_NAME="$1"
  shift
fi

PROFILE_BASE="${SELENIUM_PROFILE_BASE:-$HOME/selenium-profiles}"
export PROFILE_BASE PROFILE_NAME
PROFILE_DIR="$(python3 - <<'PY'
import os
from pathlib import Path
base = Path(os.environ.get("PROFILE_BASE", "") or Path("~").expanduser() / "selenium-profiles")
name = os.environ.get("PROFILE_NAME", "fb_logged_in")
print((base.expanduser() / name).resolve())
PY
)"

if [[ "$PROFILE_DIR" == *"Library/Application Support/Google/Chrome"* ]]; then
  echo "Refusing to use real Chrome profile: $PROFILE_DIR" >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"

echo "Opening Chrome with isolated profile:"
echo "  $PROFILE_DIR"
echo "Log in (e.g., Facebook), then quit Chrome completely before running Selenium."

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="$PROFILE_DIR" \
  --profile-directory=Default \
  "$@"
