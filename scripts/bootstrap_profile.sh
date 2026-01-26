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
base_env = os.environ.get("PROFILE_BASE")
name_env = os.environ.get("PROFILE_NAME")
base = Path(base_env) if base_env else Path("~").expanduser() / "selenium-profiles"
name = name_env if name_env else "fb_logged_in"
print((base.expanduser().resolve() / name).resolve())
PY
)"

export PROFILE_DIR
python3 - <<'PY'
import os, sys
from pathlib import Path
profile_dir = Path(os.environ["PROFILE_DIR"]).resolve()
real = Path("~/Library/Application Support/Google/Chrome").expanduser().resolve()
if real == profile_dir or real in profile_dir.parents:
    sys.stderr.write(f"Refusing to use real Chrome profile: {profile_dir}\n")
    sys.exit(1)
PY

mkdir -p "$PROFILE_DIR"

echo "Opening Chrome with isolated profile:"
echo "  $PROFILE_DIR"
echo "Log in (e.g., Facebook), then quit Chrome completely before running Selenium."

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="$PROFILE_DIR" \
  --profile-directory=Default \
  "$@"
