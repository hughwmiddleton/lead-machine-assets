#!/usr/bin/env python3
"""Safely report or remove allowlisted disposable repository artefacts."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_KEEP_DEFAULT = 30
_LEGACY_ROOT_PATTERNS = (
    (re.compile(r"fb_dom_gate_debug_[0-9]+\.json"), "debug-json"),
    (re.compile(r"fb_(?:attach_)?(?:home|search)\.png"), "debug-screenshot"),
    (re.compile(r"fb_known_page\.png"), "debug-screenshot"),
    (re.compile(r"nm_[A-Za-z0-9_.-]+\.log"), "debug-log"),
    (re.compile(r"validation_run\.log"), "debug-log"),
)
_DEBUG_PATTERNS = (
    re.compile(r"fb_dom_gate_debug_[0-9]+\.json"),
    re.compile(r"fb_(?:attach_)?(?:home|search)\.png"),
    re.compile(r"fb_known_page\.png"),
)
_PROTECTED_PREFIXES = (
    Path("data"),
    Path("overnight_runs"),
    Path("night-mode-v1-runs"),
    Path("presaved long unearthed runs"),
    Path("tests/fixtures"),
)


class UnsafeArtifactError(RuntimeError):
    """Raised when a requested cleanup target is outside the allowlist."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    kind: str


def _relative(repo_root: Path, path: Path) -> Path:
    try:
        return path.relative_to(repo_root)
    except ValueError as exc:
        raise UnsafeArtifactError(f"target escapes repository: {path}") from exc


def _is_protected(relative: Path) -> bool:
    return any(relative == prefix or prefix in relative.parents for prefix in _PROTECTED_PREFIXES)


def _legacy_kind(relative: Path) -> str | None:
    if len(relative.parts) != 1:
        return None
    for pattern, kind in _LEGACY_ROOT_PATTERNS:
        if pattern.fullmatch(relative.name):
            return kind
    return None


def validate_artifact(repo_root: Path, artifact: Artifact) -> Path:
    """Return a validated relative path or fail closed."""
    root = repo_root.resolve()
    path = artifact.path
    relative = _relative(root, path)
    if not relative.parts or relative == Path(".") or relative == Path(".git"):
        raise UnsafeArtifactError(f"refusing broad target: {path}")
    if relative.name == ".DS_Store" and artifact.kind == "macos-cache" and path.is_file():
        return relative
    if _is_protected(relative):
        raise UnsafeArtifactError(f"refusing protected operational path: {relative}")
    if path.is_symlink():
        raise UnsafeArtifactError(f"refusing symlink: {relative}")

    if relative == Path("...") and artifact.kind == "browser-cache":
        default_dir = path / "Default"
        if not default_dir.is_dir() or not (path / "Local State").is_file():
            raise UnsafeArtifactError("refusing unrecognised contents at exact browser-cache path '...'")
        return relative

    if "__pycache__" in relative.parts and artifact.kind == "python-cache":
        if path.is_file() and path.suffix in {".pyc", ".pyo"}:
            return relative
        raise UnsafeArtifactError(f"unexpected file in Python cache: {relative}")

    legacy_kind = _legacy_kind(relative)
    if legacy_kind == artifact.kind and path.is_file():
        return relative

    debug_prefix = Path("runtime/debug/facebook")
    if debug_prefix in relative.parents and artifact.kind.startswith("debug-") and path.is_file():
        if any(pattern.fullmatch(relative.name) for pattern in _DEBUG_PATTERNS):
            return relative

    raise UnsafeArtifactError(f"path is not allowlisted: {relative}")


def _iter_python_cache(repo_root: Path) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for cache_dir in repo_root.rglob("__pycache__"):
        if ".git" in cache_dir.parts:
            continue
        if cache_dir.is_symlink():
            raise UnsafeArtifactError(f"refusing symlinked Python cache: {cache_dir}")
        for path in cache_dir.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix not in {".pyc", ".pyo"}:
                raise UnsafeArtifactError(f"unexpected file in Python cache: {path}")
            artifacts.append(Artifact(path, "python-cache"))
    return artifacts


def _iter_debug_overflow(repo_root: Path, keep_debug: int) -> list[Artifact]:
    debug_dir = repo_root / "runtime" / "debug" / "facebook"
    if not debug_dir.exists():
        return []
    if debug_dir.is_symlink() or not debug_dir.is_dir():
        raise UnsafeArtifactError(f"unsafe debug directory: {debug_dir}")

    artifacts: list[Artifact] = []
    for pattern in _DEBUG_PATTERNS:
        matches = [path for path in debug_dir.iterdir() if pattern.fullmatch(path.name)]
        for path in matches:
            if path.is_symlink() or not path.is_file():
                raise UnsafeArtifactError(f"unsafe debug artefact: {path}")
        matches.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        kind = "debug-json" if pattern.pattern.endswith(r"\.json") else "debug-screenshot"
        artifacts.extend(Artifact(path, kind) for path in matches[keep_debug:])
    return artifacts


def build_cleanup_plan(repo_root: Path | None = None, *, keep_debug: int = DEBUG_KEEP_DEFAULT) -> list[Artifact]:
    if keep_debug < 1:
        raise UnsafeArtifactError("keep-debug must be at least 1")
    root = (repo_root or REPO_ROOT).resolve()
    if root == Path(root.anchor) or not (root / ".git").exists():
        raise UnsafeArtifactError(f"refusing non-repository root: {root}")

    artifacts = _iter_python_cache(root)
    for path in root.rglob(".DS_Store"):
        if ".git" not in path.parts:
            artifacts.append(Artifact(path, "macos-cache"))
    for path in root.iterdir():
        kind = _legacy_kind(Path(path.name))
        if kind and path.is_file():
            artifacts.append(Artifact(path, kind))

    browser_cache = root / "..."
    if browser_cache.exists():
        artifacts.append(Artifact(browser_cache, "browser-cache"))
    artifacts.extend(_iter_debug_overflow(root, keep_debug))

    unique = {(artifact.path, artifact.kind): artifact for artifact in artifacts}
    result = sorted(unique.values(), key=lambda artifact: artifact.path.as_posix())
    for artifact in result:
        validate_artifact(root, artifact)
    return result


def apply_cleanup_plan(repo_root: Path, artifacts: list[Artifact]) -> None:
    root = repo_root.resolve()
    for artifact in artifacts:
        validate_artifact(root, artifact)
    for artifact in artifacts:
        if artifact.path.is_dir():
            shutil.rmtree(artifact.path)
        else:
            artifact.path.unlink()

    for cache_dir in sorted(root.rglob("__pycache__"), reverse=True):
        if cache_dir.is_dir() and not cache_dir.is_symlink() and not any(cache_dir.iterdir()):
            cache_dir.rmdir()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    mode.add_argument("--apply", action="store_true", help="delete the reported allowlisted artefacts")
    parser.add_argument(
        "--keep-debug",
        type=int,
        default=DEBUG_KEEP_DEFAULT,
        help=f"retain this many recent files per dedicated debug class (default: {DEBUG_KEEP_DEFAULT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_cleanup_plan(REPO_ROOT, keep_debug=args.keep_debug)
        action = "REMOVE" if args.apply else "WOULD REMOVE"
        for artifact in plan:
            relative = validate_artifact(REPO_ROOT, artifact)
            suffix = "/" if artifact.path.is_dir() else ""
            print(f"{action} [{artifact.kind}] {relative.as_posix()}{suffix}")
        if args.apply:
            apply_cleanup_plan(REPO_ROOT, plan)
        print(f"{len(plan)} allowlisted artefact(s); mode={'apply' if args.apply else 'dry-run'}")
        return 0
    except (OSError, UnsafeArtifactError) as exc:
        print(f"cleanup refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
