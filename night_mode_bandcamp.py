"""Shared Bandcamp aggregation helpers for Night Mode runners."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import pandas as pd

from bandcamp_profile_engine import canonicalize_bandcamp_profile_url


def build_bandcamp_aggregate_csv(
    run_dir: str,
    job_states: List[Dict[str, Any]],
    logger: logging.Logger,
) -> str:
    """Aggregate completed Bandcamp job outputs into one run-scoped CSV."""
    bandcamp_states = []
    for state in job_states:
        if str(state.get("status") or "").strip().lower() != "completed":
            continue
        source_directory = str(state.get("source_directory") or "").strip().lower()
        if source_directory != "bandcamp":
            continue
        bandcamp_states.append(state)

    if not bandcamp_states:
        return ""

    enriched_paths = []
    for state in bandcamp_states:
        job_dir = os.path.dirname(str(state.get("raw_csv") or ""))
        if not job_dir:
            continue
        bandcamp_specific = os.path.join(job_dir, "bandcamp_enriched.csv")
        if os.path.exists(bandcamp_specific):
            enriched_paths.append(bandcamp_specific)
        else:
            generic_enriched = str(state.get("enriched_csv") or "")
            if generic_enriched and os.path.exists(generic_enriched):
                enriched_paths.append(generic_enriched)

    if not enriched_paths:
        logger.info("[Master] Bandcamp directory CSVs -> 0 files")
        return ""

    frames = []
    total_rows = 0
    for path in enriched_paths:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            if df.empty:
                continue
            frames.append(df)
            total_rows += len(df.index)
        except pd.errors.EmptyDataError:
            continue
        except Exception as exc:
            logger.warning("[Master] Bandcamp aggregate skipping %s: %s", path, exc)
            continue

    if not frames:
        logger.info("[Master] Bandcamp directory CSVs -> %d files, 0 rows", len(enriched_paths))
        return ""

    logger.info("[Master] Bandcamp directory CSVs -> %d files, %d rows", len(enriched_paths), total_rows)

    combined = pd.concat(frames, ignore_index=True, sort=False)

    def _bandcamp_identity(row: pd.Series) -> str:
        for col in ("Profile URL", "Bandcamp_URL", "Bandcamp URL", "Bandcamp Link", "Source URL"):
            val = str(row.get(col) or "").strip()
            if not val:
                continue
            canonical = canonicalize_bandcamp_profile_url(val)
            if canonical:
                return f"bc_url::{canonical.lower()}"
        artist = str(row.get("Artist Name") or "").strip().lower()
        source_url = str(row.get("Source URL") or "").strip().lower()
        if artist and source_url:
            return f"artist_url::{artist}|{source_url}"
        if artist:
            return f"artist::{artist}"
        return f"row::{row.name}"

    combined["__bc_dedupe_key"] = combined.apply(_bandcamp_identity, axis=1)
    combined = combined.drop_duplicates(subset="__bc_dedupe_key", keep="first")
    combined = combined.drop(columns=["__bc_dedupe_key"])

    aggregate_path = os.path.join(run_dir, "bandcamp_enriched_aggregate.csv")
    combined.to_csv(aggregate_path, index=False)
    logger.info("[Master] Bandcamp aggregate -> %s (rows=%d)", aggregate_path, len(combined.index))
    return aggregate_path
