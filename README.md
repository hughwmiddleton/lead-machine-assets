# lead-machine-assets

## Isolated Selenium profiles
1) Bootstrap a login profile once: `scripts/bootstrap_profile.sh fb_logged_in` (uses `${SELENIUM_PROFILE_BASE:-$HOME/selenium-profiles}`).
2) Log in (e.g., Facebook), then quit Chrome completely.
3) Run scrapers normally. Set `DEBUG_PROFILE=1` to print the resolved `--user-data-dir` and profile directory.

Env overrides:
- `SELENIUM_PROFILE_BASE`: base folder for all Selenium-only profiles.
- `SELENIUM_PROFILE_NAME`: optional override for the profile name used by scrapers.
