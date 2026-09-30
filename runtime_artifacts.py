"""Small, fail-closed helpers for disposable runtime diagnostics."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parent
FACEBOOK_DEBUG_DIR = REPO_ROOT / "runtime" / "debug" / "facebook"
_SAFE_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*_$")


def prune_matching_files(directory: Path, pattern: str, *, keep: int) -> list[Path]:
    """Delete older regular files matching *pattern*, retaining the newest *keep*.

    Symlinks fail closed because diagnostics must never be able to redirect deletion
    outside their dedicated directory.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")

    matches: list[Path] = []
    for path in directory.glob(pattern):
        if path.is_symlink():
            raise RuntimeError(f"refusing to prune symlink: {path}")
        if path.is_file():
            matches.append(path)

    matches.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    removed: list[Path] = []
    for path in matches[keep:]:
        path.unlink()
        removed.append(path)
    return removed


def write_bounded_json_debug(
    payload: Mapping[str, Any],
    *,
    prefix: str,
    keep: int = 30,
    directory: Path | None = None,
) -> Path:
    """Write one JSON diagnostic and cap matching files in its directory."""
    if not _SAFE_PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"unsafe debug prefix: {prefix!r}")
    if keep < 1:
        raise ValueError("keep must be at least 1")

    target_dir = directory or FACEBOOK_DEBUG_DIR
    if target_dir.is_symlink():
        raise RuntimeError(f"refusing symlinked debug directory: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    path = target_dir / f"{prefix}{time.time_ns()}.json"
    path.write_text(json.dumps(dict(payload), indent=2), encoding="utf-8")
    prune_matching_files(target_dir, f"{prefix}*.json", keep=keep)
    return path
