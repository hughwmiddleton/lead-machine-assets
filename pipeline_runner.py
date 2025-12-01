"""Thin wrapper layer to invoke existing Lead Machine scrapers and enrichment steps.

This module isolates Night Mode from the core scraper logic so that future
changes to scrapers do not require updating the orchestration layer.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

LoggerFn = Optional[Callable[[str], None]]

_LEGACY_MODULE = None
_LOGGER = logging.getLogger(__name__)


def _load_legacy_module():
    """
    Load the main Lead Machine module without triggering its __main__ entrypoint.
    The file name contains spaces, so importlib is used instead of a normal import.
    """
    global _LEGACY_MODULE
    if _LEGACY_MODULE is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        legacy_path = os.path.join(base_dir, "Lead Machine (Final Update 5).py")
        spec = importlib.util.spec_from_file_location("lead_machine_main", legacy_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load legacy module from {legacy_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[arg-type]
        _LEGACY_MODULE = module
    return _LEGACY_MODULE


def _safe_log(logger: LoggerFn, message: str) -> None:
    if not message:
        return
    if logger:
        try:
            logger(message)
            return
        except Exception:
            pass
    _LOGGER.info(message)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _read_seed_list(seed_path: str | None) -> List[str]:
    if not seed_path:
        return []
    path = os.path.abspath(seed_path)
    if not os.path.exists(path):
        return []
    try:
        df = pd.read_csv(path)
        if df.empty:
            return []
        first_col = df.columns[0]
        seeds = [str(v).strip() for v in df[first_col].tolist() if str(v).strip()]
        return seeds
    except Exception:
        return []


def _write_rows_to_csv(rows: Iterable[Any], path: str, source_directory: str = "") -> str:
    _ensure_parent(path)
    materialized: List[Any] = list(rows or [])
    if not materialized:
        pd.DataFrame().to_csv(path, index=False)
        return path
    if isinstance(materialized[0], dict):
        columns = []
        for row in materialized:
            columns.extend(list((row or {}).keys()))
        # Preserve deterministic order for stability.
        seen = set()
        ordered_columns = []
        for col in columns:
            if col not in seen:
                ordered_columns.append(col)
                seen.add(col)
        df = pd.DataFrame(materialized, columns=ordered_columns)
    else:
        df = pd.DataFrame(materialized)
    if source_directory and "Source Directory" not in df.columns:
        df["Source Directory"] = source_directory
    df.to_csv(path, index=False)
    return path


def run_directory_job(job_config: Dict[str, Any], raw_output_path: str, logger: LoggerFn = None) -> str:
    """
    Run a single directory scraper based on job_config.

    This wrapper intentionally keeps the surface area small and delegates
    behaviour to the existing scrapers without changing their defaults.
    """
    module = _load_legacy_module()
    directory = (job_config.get("directory") or "").strip().lower()
    target_count = int(job_config.get("target_valid_leads") or job_config.get("target_count") or 0)
    mode = (job_config.get("mode") or "").strip().lower()

    if directory == "spotify":
        params = {
            "playlist_ids": job_config.get("playlist_ids"),
            "search_term": job_config.get("search_term") or job_config.get("input_seed_csv") or "",
            "spotify_client_id": job_config.get("spotify_client_id") or os.environ.get("SPOTIFY_CLIENT_ID"),
            "spotify_client_secret": job_config.get("spotify_client_secret") or os.environ.get("SPOTIFY_CLIENT_SECRET"),
        }
        rows = module.scrape_spotify(target_count, params, logger=logger)
        return _write_rows_to_csv(rows, raw_output_path, source_directory="spotify")

    if directory == "bandcamp":
        seed = (
            job_config.get("bandcamp_seed")
            or job_config.get("input_seed_csv")
            or job_config.get("seed")
            or job_config.get("url")
            or ""
        )
        progress_path = os.path.join(os.path.dirname(os.path.abspath(raw_output_path)), "bandcamp_progress.json")
        module.scrape_bandcamp(
            seed,
            pages_per_tag=job_config.get("pages_per_tag", getattr(module, "BANDCAMP_PAGES_PER_TAG", 5)),
            existing_csv=raw_output_path,
            max_artists=target_count or getattr(module, "BANDCAMP_TARGET_ROWS", 200),
            progress_path=progress_path,
            mode=mode or "discover",
            max_pages=job_config.get("max_pages"),
            max_items=job_config.get("max_items"),
            search_domain=job_config.get("search_domain", "artists"),
            search_location_filter=job_config.get("search_location", ""),
        )
        return raw_output_path

    if directory == "soundcloud":
        url = job_config.get("soundcloud_url") or job_config.get("input_seed_csv") or job_config.get("seed") or ""
        module.scrape_soundcloud(
            url,
            seed_tags=job_config.get("seed_tags"),
            pages_per_tag=job_config.get("pages_per_tag", getattr(module, "SOUNDCLOUD_PAGES_PER_TAG", 5)),
            existing_csv=raw_output_path,
            max_artists=target_count or 200,
            max_handles=job_config.get("max_handles"),
            min_yield=job_config.get("min_yield", 3),
            dry_run=bool(job_config.get("dry_run", False)),
        )
        return raw_output_path

    if directory == "lastfm":
        seeds = _read_seed_list(job_config.get("input_seed_csv"))
        module.scrape_lastfm_similar(
            seeds,
            existing_csv=raw_output_path,
            max_artists=target_count or getattr(module, "LASTFM_MAX_SIMILAR_PER_SEED", 200),
            log_fn=logger,
        )
        return raw_output_path

    raise ValueError(f"Unsupported directory: {directory}")


def run_enrichment(raw_csv_path: str, enriched_output_path: str, logger: LoggerFn = None) -> str:
    """
    Invoke the existing enrichment/validation pipeline on a CSV.

    Currently this runs:
      - origin_validator.run_auto_validate (reusable validation stage)
      - final_checker.run_final_checker (adds duplicate/consistency flags)

    The final CSV is always written to enriched_output_path.
    """
    import origin_validator
    import final_checker

    _safe_log(logger, f"[Enrich] Starting enrichment for {raw_csv_path}")
    _ensure_parent(enriched_output_path)
    result_path = enriched_output_path
    try:
        result_path = origin_validator.run_auto_validate(
            raw_csv_path,
            output_path=enriched_output_path,
            validate_scope="uncertain_only",
            logger=logger,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        _safe_log(logger, f"[Enrich] Auto-validate failed safely: {exc}")
        shutil.copyfile(raw_csv_path, enriched_output_path)
        result_path = enriched_output_path

    final_path = result_path
    try:
        checked_path = final_checker.run_final_checker(result_path)
        if checked_path and os.path.exists(checked_path):
            shutil.copyfile(checked_path, enriched_output_path)
            final_path = enriched_output_path
        else:
            final_path = result_path
    except Exception as exc:  # pragma: no cover - defensive fallback
        _safe_log(logger, f"[Enrich] Final checker failed safely: {exc}")
        final_path = result_path

    _safe_log(logger, f"[Enrich] Completed enrichment -> {final_path}")
    return final_path
