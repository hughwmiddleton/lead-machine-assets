#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "tests" / "UE_NM_Run_Test500.json"
NIGHT_MODE_RUNNER = REPO_ROOT / "night_mode_runner.py"
DEFAULT_CAP = 15
JOB_LIMIT_KEYS = ("target_valid_leads", "target_count", "max_results", "max_artists")
TOP_LEVEL_LIMIT_KEYS = ("facebook_max_rows_per_run",)


def _maybe_clamp_mapping_int(mapping: dict[str, Any], key: str, cap: int, changes: list[str], path: str) -> None:
    if key not in mapping:
        return
    raw_value = mapping[key]
    try:
        numeric_value = int(raw_value)
    except Exception:
        return
    clamped_value = min(numeric_value, cap)
    if clamped_value == numeric_value:
        return
    mapping[key] = clamped_value
    changes.append(f"{path}.{key}: {numeric_value} -> {clamped_value}")


def build_smoke_payload(config: dict[str, Any], cap: int = DEFAULT_CAP) -> tuple[dict[str, Any], list[str]]:
    smoke_config = copy.deepcopy(config)
    changes: list[str] = []

    for key in TOP_LEVEL_LIMIT_KEYS:
        _maybe_clamp_mapping_int(smoke_config, key, cap, changes, "config")

    facebook_cfg = smoke_config.get("facebook")
    if isinstance(facebook_cfg, dict):
        _maybe_clamp_mapping_int(facebook_cfg, "max_rows_per_run", cap, changes, "config.facebook")

    jobs = smoke_config.get("jobs")
    if isinstance(jobs, list):
        for idx, job in enumerate(jobs):
            if not isinstance(job, dict):
                continue
            for key in JOB_LIMIT_KEYS:
                _maybe_clamp_mapping_int(job, key, cap, changes, f"config.jobs[{idx}]")

    return smoke_config, changes


def write_smoke_config(source_config_path: Path, output_config_path: Path, cap: int = DEFAULT_CAP) -> list[str]:
    with source_config_path.open("r", encoding="utf-8") as handle:
        source_payload = json.load(handle)
    smoke_payload, changes = build_smoke_payload(source_payload, cap=cap)
    output_config_path.parent.mkdir(parents=True, exist_ok=True)
    with output_config_path.open("w", encoding="utf-8") as handle:
        json.dump(smoke_payload, handle, indent=2)
        handle.write("\n")
    return changes


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a capped smoke version of the Unearthed Night Mode config via night_mode_runner.py.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Source Night Mode config to derive a smoke config from.",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=DEFAULT_CAP,
        help="Maximum allowed value for known row/artist limit fields.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write and report the derived smoke config without executing night_mode_runner.py.",
    )
    parser.add_argument(
        "runner_args",
        nargs=argparse.REMAINDER,
        help="Extra args passed through to night_mode_runner.py. Prefix with -- to separate wrapper args.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    source_config_path = Path(args.config).expanduser().resolve()
    if not source_config_path.is_file():
        print(f"Source config not found: {source_config_path}", file=sys.stderr)
        return 2
    if args.cap <= 0:
        print(f"--cap must be a positive integer; got {args.cap}", file=sys.stderr)
        return 2
    if not NIGHT_MODE_RUNNER.is_file():
        print(f"Night Mode runner not found: {NIGHT_MODE_RUNNER}", file=sys.stderr)
        return 2

    runner_args = list(args.runner_args)
    if runner_args and runner_args[0] == "--":
        runner_args = runner_args[1:]
    if "--config" in runner_args:
        print("Pass the source config via the wrapper --config flag, not via runner_args.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="ue_nm_smoke_") as temp_dir:
        smoke_config_path = Path(temp_dir) / f"{source_config_path.stem}.smoke.json"
        changes = write_smoke_config(source_config_path, smoke_config_path, cap=args.cap)

        print(f"Source config : {source_config_path}")
        print(f"Smoke config  : {smoke_config_path}")
        if changes:
            print("Clamped fields:")
            for change in changes:
                print(f" - {change}")
        else:
            print(f"Clamped fields: none (all known limits already <= {args.cap})")

        if args.dry_run:
            return 0

        cmd = [sys.executable, str(NIGHT_MODE_RUNNER), "--config", str(smoke_config_path), *runner_args]
        print("Executing    :", " ".join(cmd))
        completed = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
