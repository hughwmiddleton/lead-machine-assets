#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

import night_mode_fb as nmfb
import pipeline_runner
from night_mode_fb import NightFBRunState, NightFBSessionSource

DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "miya_zawa_unearthed_share_pre_fb.csv"
DEFAULT_MULTI_ROW_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fb_share_multi_row_pre_fb.csv"
DEFAULT_OUTPUT_ROOT = Path("/tmp/lead-machine-miya-zawa-share-smoke")
DEFAULT_RESOLVED_URL = "https://www.facebook.com/itsmiyazawa"
FIXTURE_RESOLVED_URL_COLUMN = "Smoke_Resolved_Facebook_URL"


class _SmokeLogger:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        line = str(message)
        self.lines.append(line)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")


class _SmokeNightFBHelper:
    def __init__(self, logger: Callable[[str], None]) -> None:
        self._logger = logger
        self.calls = 0
        self.visited_urls: list[str] = []
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> "_SmokeNightFBHelper":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def get_session_failure(self) -> tuple[bool, str]:
        return False, ""

    def get_pass_a_counts(self) -> dict[str, int]:
        return {}

    def get_email_stats(self) -> dict[str, int]:
        return {}

    def enrich_row_with_facebook_night(self, row: dict[str, Any], row_index: int = 0) -> dict[str, str]:
        self.calls += 1
        payload_row = dict(row or {})
        self.rows.append({"row": payload_row, "row_index": row_index})
        canonical_url = str(payload_row.get("Facebook_URL", "") or "").strip()
        if canonical_url:
            self.visited_urls.append(canonical_url)
            self._logger(f"[FB Email] Visiting {canonical_url}")
        return {
            "FB_Status": "pass_a_content_unavailable",
            "FB_Attempt_State": "attempted_fb_content_unavailable",
            "FB_Write_State": "fb_no_email_written",
            "FB_Reason": "explicit_url",
            "Facebook_URL": canonical_url,
            "Email": "",
            "Email_All": "",
        }


@dataclass
class SmokeRunArtifacts:
    fixture_path: Path
    output_root: Path
    input_csv: Path
    output_csv: Path
    state_path: Path
    log_path: Path
    summary_path: Path
    rows_summary_path: Path
    log_lines: list[str]
    row: dict[str, str]
    rows: list[dict[str, Any]]
    rows_by_artist: dict[str, dict[str, Any]]
    summary: dict[str, Any]


def _load_fixture_df(fixture_path: Path) -> pd.DataFrame:
    return pd.read_csv(fixture_path, dtype=str, keep_default_na=False).fillna("")


def _dummy_legacy_module() -> SimpleNamespace:
    return SimpleNamespace(scrape_csv=lambda *args, **kwargs: None)


def _last_match(lines: list[str], needle: str) -> str:
    for line in reversed(lines):
        if needle in line:
            return line
    return ""


def _extract_gate_bool(line: str, key: str) -> bool:
    match = re.search(rf"{re.escape(key)}=(True|False)", line)
    return bool(match and match.group(1) == "True")


def _extract_intake_outcome(line: str) -> str:
    match = re.search(r'outcome="([^"]+)"', line)
    return match.group(1) if match else ""


def _extract_pass_a_attempted(line: str) -> int:
    match = re.search(r"attempted=(\d+)", line)
    return int(match.group(1)) if match else 0


def _extract_log_int(line: str, key: str) -> int | None:
    match = re.search(rf"{re.escape(key)}=(\d+)", line)
    return int(match.group(1)) if match else None


def _extract_log_str(line: str, key: str) -> str:
    for quote in ("'", '"'):
        match = re.search(rf"{re.escape(key)}={quote}([^{quote}]*){quote}", line)
        if match:
            return match.group(1)
    return ""


def _extract_share_outcome(line: str) -> str:
    match = re.search(r"outcome='([^']+)'", line)
    return match.group(1) if match else ""


def _extract_share_canonical_url(line: str) -> str:
    match = re.search(r"canonical_url='([^']+)'", line)
    if match:
        return match.group(1)
    match = re.search(r"resolved_url='([^']+)'", line)
    return match.group(1) if match else ""


def _write_df(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _build_fixture_resolved_url_map(fixture_df: pd.DataFrame) -> dict[str, str]:
    resolved_map: dict[str, str] = {}
    if FIXTURE_RESOLVED_URL_COLUMN not in fixture_df.columns:
        return resolved_map

    for _, row in fixture_df.iterrows():
        row_dict = row.to_dict()
        resolved_url = str(row_dict.get(FIXTURE_RESOLVED_URL_COLUMN, "") or "").strip()
        if not resolved_url:
            continue
        raw_share_url, _ = pipeline_runner._find_explicit_fb_share_candidate(row_dict)
        if not raw_share_url:
            artist_name = str(row_dict.get("Artist Name", "") or row_dict.get("Artist", "") or "<unknown>")
            raise ValueError(
                f"Fixture row for '{artist_name}' provided {FIXTURE_RESOLVED_URL_COLUMN} but has no Facebook /share URL"
            )
        existing = resolved_map.get(raw_share_url)
        if existing and existing != resolved_url:
            raise ValueError(f"Conflicting resolved URLs for share fixture {raw_share_url}")
        resolved_map[raw_share_url] = resolved_url
    return resolved_map


def _build_row_summaries(
    out_df: pd.DataFrame,
    *,
    log_lines: list[str],
    helper: _SmokeNightFBHelper,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    gate_lines_by_row: dict[int, str] = {}
    intake_lines_by_artist: dict[str, str] = {}
    share_lines_by_artist: dict[str, str] = {}
    attempted_row_indices = {
        int(item["row_index"])
        for item in helper.rows
        if str(item.get("row_index", "")).strip().isdigit()
    }

    for line in log_lines:
        if "[Night FB][Row Gate]" in line:
            row_index = _extract_log_int(line, "row")
            if row_index is not None:
                gate_lines_by_row[row_index] = line
        elif "[Night FB][Explicit Intake]" in line:
            artist_name = _extract_log_str(line, "artist")
            if artist_name:
                intake_lines_by_artist[artist_name] = line
        elif "[FB Share Canonicalize]" in line and "outcome='" in line:
            artist_name = _extract_log_str(line, "artist")
            if artist_name:
                share_lines_by_artist[artist_name] = line

    rows: list[dict[str, Any]] = []
    rows_by_artist: dict[str, dict[str, Any]] = {}
    for idx, (_, row) in enumerate(out_df.iterrows()):
        row_dict = row.to_dict()
        artist_name = str(row_dict.get("Artist Name", "") or row_dict.get("Artist", "") or f"row-{idx}")
        gate_line = gate_lines_by_row.get(idx, "")
        intake_line = intake_lines_by_artist.get(artist_name, "")
        share_line = share_lines_by_artist.get(artist_name, "")
        row_summary = {
            "row_index": int(idx),
            "artist_name": artist_name,
            "social_link": row_dict.get("Social Link", ""),
            "canonical_facebook_url": row_dict.get("Facebook_URL", ""),
            "facebook_url_alias": row_dict.get("facebook_url", ""),
            "facebook_url_title_alias": row_dict.get("Facebook URL", ""),
            "fb_status": row_dict.get("FB_Status", ""),
            "fb_opportunity_state": row_dict.get("FB_Opportunity_State", ""),
            "fb_gate_state": row_dict.get("FB_Gate_State", ""),
            "fb_attempt_state": row_dict.get("FB_Attempt_State", ""),
            "fb_write_state": row_dict.get("FB_Write_State", ""),
            "fb_url_present": _extract_gate_bool(gate_line, "fb_url_present"),
            "fb_entrypoint_present": _extract_gate_bool(gate_line, "fb_entrypoint_present"),
            "explicit_intake_outcome": _extract_intake_outcome(intake_line),
            "fb_scrape_started": idx in attempted_row_indices,
            "pass_a_attempted": int(idx in attempted_row_indices),
            "share_canonicalization_outcome": _extract_share_outcome(share_line),
            "share_canonicalization_url": _extract_share_canonical_url(share_line),
        }
        rows.append(row_summary)

        row_key = artist_name if artist_name not in rows_by_artist else f"{artist_name}#{idx}"
        rows_by_artist[row_key] = row_summary

    return rows, rows_by_artist


def run_smoke(
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    mode: str = "resolved",
    resolved_url: str = DEFAULT_RESOLVED_URL,
) -> SmokeRunArtifacts:
    fixture_path = Path(fixture_path).resolve()
    output_root = Path(output_root).resolve() / mode
    input_csv = output_root / "master_pre_fb.csv"
    output_csv = output_root / "master_post_fb.csv"
    state_path = output_root / "facebook_state.json"
    log_path = output_root / "smoke.log"
    summary_path = output_root / "summary.json"
    rows_summary_path = output_root / "row_summaries.json"

    fixture_df = _load_fixture_df(fixture_path)
    fixture_row_count = int(len(fixture_df.index))
    if fixture_row_count < 1:
        raise ValueError(f"Expected at least one fixture row in {fixture_path}")
    fixture_resolved_url_map = _build_fixture_resolved_url_map(fixture_df)

    output_root.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    _write_df(fixture_df, input_csv)
    _write_df(fixture_df, output_csv)
    state_path.write_text("{}", encoding="utf-8")

    logger = _SmokeLogger(log_path)
    helper = _SmokeNightFBHelper(logger)

    resolver: Optional[Callable[[str], Optional[str]]]
    if mode == "resolved":
        resolver = lambda raw: fixture_resolved_url_map.get(str(raw or "").strip(), resolved_url)
    elif mode == "unresolved":
        resolver = lambda raw: ""
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    original_build_resolver = pipeline_runner._build_night_fb_share_promotion_resolver
    original_load_legacy_module = pipeline_runner._load_legacy_module
    original_ensure_session = nmfb.NightModeFacebookEnricher._ensure_session
    original_has_authenticated_session = nmfb.NightModeFacebookEnricher._has_authenticated_session
    original_checkpoint_guard = nmfb.NightModeFacebookEnricher._maybe_recover_or_skip_on_checkpoint
    original_scrape_single_candidate = nmfb.NightModeFacebookEnricher._scrape_single_fb_candidate

    pipeline_runner._build_night_fb_share_promotion_resolver = lambda **kwargs: resolver
    pipeline_runner._load_legacy_module = _dummy_legacy_module

    def _fake_scrape_single_fb_candidate(
        self,
        fb_url: str,
        row: dict[str, Any],
        artist_name: str,
        allow_anon: bool = False,
        candidate_context: Optional[dict[str, Any]] = None,
    ) -> tuple[nmfb.NightModeFacebookResult, list[str], str, str]:
        helper.calls += 1
        helper.visited_urls.append(fb_url)
        helper.rows.append(
            {
                "row": dict(row or {}),
                "row_index": int(str((row or {}).get("__row_id", len(helper.rows))) or len(helper.rows)),
                "artist_name": artist_name,
                "allow_anon": allow_anon,
                "candidate_context": dict(candidate_context or {}),
            }
        )
        logger(f"[FB Email] Visiting {fb_url}")
        return (
            nmfb.NightModeFacebookResult(
                email="",
                email_all="",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            [],
            "session",
            "no_email_on_page",
        )

    nmfb.NightModeFacebookEnricher._ensure_session = lambda self: None
    nmfb.NightModeFacebookEnricher._has_authenticated_session = lambda self: True
    nmfb.NightModeFacebookEnricher._maybe_recover_or_skip_on_checkpoint = lambda self: True
    nmfb.NightModeFacebookEnricher._scrape_single_fb_candidate = _fake_scrape_single_fb_candidate
    try:
        run_state = NightFBRunState(
            session_source=NightFBSessionSource(
                mode="credentials",
                reason="miya_zawa_share_smoke",
                can_probe=True,
                has_credentials=True,
            )
        )
        pipeline_runner.run_facebook_global_pass_nightmode(
            input_csv=input_csv.as_posix(),
            output_csv=output_csv.as_posix(),
            state_path=state_path.as_posix(),
            max_rows_per_run=max(1, fixture_row_count),
            per_row_delay_range=(0.0, 0.0),
            short_break_every=0,
            long_break_every=0,
            logger=logger,
            skip_rows_with_email=True,
            night_fb_run_state=run_state,
        )
    finally:
        pipeline_runner._build_night_fb_share_promotion_resolver = original_build_resolver
        pipeline_runner._load_legacy_module = original_load_legacy_module
        nmfb.NightModeFacebookEnricher._ensure_session = original_ensure_session
        nmfb.NightModeFacebookEnricher._has_authenticated_session = original_has_authenticated_session
        nmfb.NightModeFacebookEnricher._maybe_recover_or_skip_on_checkpoint = original_checkpoint_guard
        nmfb.NightModeFacebookEnricher._scrape_single_fb_candidate = original_scrape_single_candidate

    out_df = _load_fixture_df(output_csv)
    row = out_df.iloc[0].to_dict()
    rows, rows_by_artist = _build_row_summaries(out_df, log_lines=list(logger.lines), helper=helper)
    gate_line = _last_match(logger.lines, "[Night FB][Row Gate]")
    intake_line = _last_match(logger.lines, "[Night FB][Explicit Intake]")
    pass_a_summary_line = _last_match(logger.lines, "[FB Night][PASS A Summary]")
    rows_summary_path.write_text(json.dumps({"rows": rows, "rows_by_artist": rows_by_artist}, indent=2), encoding="utf-8")

    summary = {
        "fixture_path": fixture_path.as_posix(),
        "output_root": output_root.as_posix(),
        "mode": mode,
        "input_rows": int(len(out_df.index)),
        "resolved_fixture_rows": int(len(fixture_resolved_url_map)),
        "helper_calls": int(helper.calls),
        "helper_urls": list(helper.visited_urls),
        "canonical_facebook_url": row.get("Facebook_URL", ""),
        "facebook_url_alias": row.get("facebook_url", ""),
        "facebook_url_title_alias": row.get("Facebook URL", ""),
        "social_link": row.get("Social Link", ""),
        "fb_status": row.get("FB_Status", ""),
        "fb_opportunity_state": row.get("FB_Opportunity_State", ""),
        "fb_gate_state": row.get("FB_Gate_State", ""),
        "fb_attempt_state": row.get("FB_Attempt_State", ""),
        "fb_write_state": row.get("FB_Write_State", ""),
        "fb_url_present": _extract_gate_bool(gate_line, "fb_url_present"),
        "fb_entrypoint_present": _extract_gate_bool(gate_line, "fb_entrypoint_present"),
        "explicit_intake_outcome": _extract_intake_outcome(intake_line),
        "fb_scrape_started": bool(helper.calls),
        "pass_a_attempted": _extract_pass_a_attempted(pass_a_summary_line),
        "rows_with_canonical_facebook_url": sum(bool(item["canonical_facebook_url"]) for item in rows),
        "rows_with_explicit_intake_attempt": sum(item["explicit_intake_outcome"] == "attempt" for item in rows),
        "rows_with_pass_a_attempt": sum(item["pass_a_attempted"] for item in rows),
        "night_fb_discovery_skipped": any(
            "[Unearthed Path] no usable FB URL; skipping Night FB discovery" in line for line in logger.lines
        ),
        "discovery_logs_present": any(
            "allowing bounded FB discovery" in line or "entering Unearthed no-URL FB discovery" in line
            for line in logger.lines
        ),
        "logs_path": log_path.as_posix(),
        "output_csv": output_csv.as_posix(),
        "rows_summary_path": rows_summary_path.as_posix(),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return SmokeRunArtifacts(
        fixture_path=fixture_path,
        output_root=output_root,
        input_csv=input_csv,
        output_csv=output_csv,
        state_path=state_path,
        log_path=log_path,
        summary_path=summary_path,
        rows_summary_path=rows_summary_path,
        log_lines=list(logger.lines),
        row=row,
        rows=rows,
        rows_by_artist=rows_by_artist,
        summary=summary,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the default miya-zawa canary or a custom Facebook /share smoke fixture.",
    )
    parser.add_argument(
        "--fixture",
        "--fixture-path",
        dest="fixture_path",
        default=str(DEFAULT_FIXTURE),
        help="Path to the pre-FB smoke fixture CSV. Defaults to the single-row miya-zawa canary.",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Deterministic output root. The harness writes into a mode-specific subdirectory.",
    )
    parser.add_argument(
        "--mode",
        choices=("resolved", "unresolved"),
        default="resolved",
        help="resolved: prove canonical promotion drives explicit intake. unresolved: prove no explicit readiness is fabricated.",
    )
    parser.add_argument(
        "--resolved-url",
        default=DEFAULT_RESOLVED_URL,
        help="Canonical Facebook URL injected by the smoke resolver in resolved mode.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    artifacts = run_smoke(
        fixture_path=Path(args.fixture_path),
        output_root=Path(args.output_root),
        mode=args.mode,
        resolved_url=args.resolved_url,
    )
    print(json.dumps(artifacts.summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
