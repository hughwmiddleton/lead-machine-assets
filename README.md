# lead-machine-assets

## Isolated Selenium profiles
1) Bootstrap once to log in: `scripts/bootstrap_profile.sh fb_logged_in` (base `${SELENIUM_PROFILE_BASE:-$HOME/selenium-profiles}`; name `${SELENIUM_PROFILE_NAME:-fb_logged_in}`).
2) Log in (e.g., Facebook), then quit Chrome fully before running Selenium.
3) Smoke-check: `DEBUG_PROFILE=1 python3 "Lead Machine (Final Update 5).py"` (prints `[PROFILE] user-data-dir=... inside_real_chrome=False`).
4) Overrides: set `SELENIUM_PROFILE_BASE` to change the base folder, `SELENIUM_PROFILE_NAME` to reuse a specific profile across scrapers.
