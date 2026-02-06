#!/usr/bin/env python3
"""
Offline diagnostic bundle for Facebook candidate gating.
- Prints Python version
- Prints sha256 of key FB files
- Evaluates the URL allowlist on common cases
- Shows FB_DEBUG_* env vars (only names/values, no secrets)
"""

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from facebook_enrich import _fb_is_candidate_url_allowed  # type: ignore
except Exception as exc:
    print(f"[fb_debug_bundle] Failed to import facebook_enrich: {exc}")
    sys.exit(1)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                h.update(chunk)
    except FileNotFoundError:
        return "missing"
    return h.hexdigest()


def main() -> None:
    print(f"python_version={sys.version.split()[0]}")

    files = [
        ROOT / "facebook_enrich.py",
        ROOT / "night_mode_fb.py",
    ]
    for f in files:
        print(f"{f.name}: {sha256_file(f)}")

    urls = [
        "https://www.facebook.com/someband",
        "https://www.facebook.com/someband?__tn__=%2Cd",
        "https://www.facebook.com/someband/about",
        "https://www.facebook.com/profile.php?id=123",
        "https://www.facebook.com/profile.php?id=123&foo=1",
        "https://www.facebook.com/groups/",
        "https://web.facebook.com/someband",
    ]
    print("allowlist_results:")
    for u in urls:
        try:
            allowed = _fb_is_candidate_url_allowed(u)
        except Exception as exc:
            allowed = f"error: {exc}"
        print(f"  {u} -> {allowed}")

    print("env_FB_DEBUG:")
    for k, v in sorted(os.environ.items()):
        if k.startswith("FB_DEBUG"):
            print(f"  {k}={v}")


if __name__ == "__main__":
    main()
