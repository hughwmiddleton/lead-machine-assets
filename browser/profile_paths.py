"""Shared helpers for isolating Selenium Chrome profiles."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Dict

REAL_CHROME_DATA_DIR = Path(
    "~/Library/Application Support/Google/Chrome"
).expanduser().resolve()
DEFAULT_BASE_DIR = Path(
    os.environ.get("SELENIUM_PROFILE_BASE", "~/selenium-profiles")
).expanduser().resolve()

_DEBUG_ONCE: set[Path] = set()


def get_base_dir() -> Path:
    """Return the base directory for Selenium profiles."""
    return Path(
        os.environ.get("SELENIUM_PROFILE_BASE", DEFAULT_BASE_DIR)
    ).expanduser().resolve()


def get_profile_dir(
    profile_name: str = "default", *, allow_env_override: bool = True
) -> Path:
    """
    Resolve an absolute profile directory.
    If SELENIUM_PROFILE_NAME is set and allow_env_override is True, it overrides the name.
    """
    if allow_env_override and os.environ.get("SELENIUM_PROFILE_NAME"):
        name = os.environ["SELENIUM_PROFILE_NAME"]
    else:
        name = profile_name
    return (get_base_dir() / name).resolve()


def is_inside_real_chrome(profile_dir: Path) -> bool:
    """True if the path sits under the real Chrome data directory."""
    profile_dir = profile_dir.resolve()
    return REAL_CHROME_DATA_DIR in profile_dir.parents or profile_dir == REAL_CHROME_DATA_DIR


def ensure_profile_dir(profile_dir: Path) -> Path:
    """Create the profile directory if needed and return it."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def singleton_lock_active(profile_dir: Path, *, stale_after_seconds: int = 180) -> bool:
    """
    Detect whether Chrome currently holds the profile by inspecting SingletonLock.
    Treat a lock file touched within stale_after_seconds as active.
    """
    lock_path = profile_dir / "SingletonLock"
    try:
        if lock_path.exists():
            mtime = lock_path.stat().st_mtime
            return (time.time() - mtime) < stale_after_seconds
    except Exception:
        return False
    return False


def assert_profile_available(profile_dir: Path, *, stale_after_seconds: int = 180) -> None:
    """Raise if the directory is unsafe or appears in use."""
    if is_inside_real_chrome(profile_dir):
        raise RuntimeError(
            f"Refusing to use real Chrome profile at {profile_dir}. "
            "Set SELENIUM_PROFILE_BASE to an isolated directory (e.g. ~/selenium-profiles)."
        )
    if singleton_lock_active(profile_dir, stale_after_seconds=stale_after_seconds):
        raise RuntimeError(
            f"Selenium profile at {profile_dir} appears to be in use (SingletonLock present). "
            "Quit Chrome for that profile or choose a different profile name."
        )


def _collect_artifacts(profile_dir: Path) -> Dict[str, bool]:
    """Check for key Chrome profile artifacts."""
    return {
        "Local State": (profile_dir / "Local State").exists(),
        "Default": (profile_dir / "Default").exists(),
        "Default/Preferences": (profile_dir / "Default" / "Preferences").exists(),
        "Default/Cookies": (profile_dir / "Default" / "Cookies").exists(),
    }


def log_profile_debug(
    profile_dir: Path,
    *,
    profile_directory: str | None = None,
    logger: Callable[[str], None] | None = None,
) -> None:
    """
    Emit one-time debug info when DEBUG_PROFILE=1 is set.
    Prints the resolved user-data-dir, profile-directory, existence, artifacts, and safety.
    """
    if not os.environ.get("DEBUG_PROFILE"):
        return
    profile_dir = profile_dir.resolve()
    if profile_dir in _DEBUG_ONCE:
        return
    _DEBUG_ONCE.add(profile_dir)
    sink = logger or (lambda msg: print(msg, flush=True))
    try:
        artifacts = _collect_artifacts(profile_dir)
        sink(
            "[PROFILE] user-data-dir=%s profile-directory=%s exists=%s inside_real_chrome=%s artifacts=%s"
            % (
                profile_dir,
                profile_directory or "<default>",
                profile_dir.exists(),
                is_inside_real_chrome(profile_dir),
                artifacts,
            )
        )
    except Exception:
        # Best-effort only; never fail driver creation because of logging.
        pass
