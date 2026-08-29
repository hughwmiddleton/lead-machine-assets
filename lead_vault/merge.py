import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import re
import uuid
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import urlsplit, urlunsplit

from email_normalizer import (
    is_obvious_placeholder_email,
    is_platform_support_email,
    is_system_telemetry_email,
    normalize_email_value,
)

from .alias_map import map_headers_to_canonical
from .importer import build_canonical_row, ensure_master_csv_exists, read_csv_rows
from .origin import merge_origin_fields, preserve_origin_fields, repair_origin_fields, validate_origin_integrity_rows
from .schema import get_canonical_master_schema, get_default_master_csv_path

PathLike = Union[str, Path]

_LIST_LIKE_FIELDS = {"All_Emails", "Social Link", "External_Links", "Review_Urls"}
_DUPLICATE_STRATEGIES = {"update", "skip", "keep_both", "merge_consolidate"}
_SOCIAL_LINK_FIELDS = {
    "Website",
    "Contact_Page_URL",
    "Facebook_URL",
    "Instagram_URL",
    "Twitter_URL",
    "SoundCloud_URL",
    "Bandcamp_URL",
    "Spotify_URL",
    "LastFM_URL",
    "YouTube_URL",
    "TikTok_URL",
}
_CONSOLIDATION_RELATIONSHIP_FIELDS = {
    "Contact_Page_URL",
    "Facebook_URL",
    "Instagram_URL",
    "Twitter_URL",
    "SoundCloud_URL",
    "Bandcamp_URL",
    "Spotify_URL",
    "LastFM_URL",
    "YouTube_URL",
    "TikTok_URL",
}
_MUSICBRAINZ_IDENTITY_FIELDS = (
    "MusicBrainz_MBID",
    "MusicBrainz_Status",
    "Identity_Match_Method",
    "Identity_Confidence",
    "Identity_Evidence_JSON",
)
_IGNORE_HEADER_SENTINEL = "__IGNORE__"


def preview_csv_import(
    source_path: PathLike,
    header_overrides: Optional[Dict[str, str]] = None,
    ignored_headers: Optional[Iterable[str]] = None,
    master_path: Optional[PathLike] = None,
) -> Dict[str, object]:
    read_result = read_csv_rows(source_path)
    detected_headers = list(read_result["detected_headers"])
    rows = list(read_result["rows"])
    encoding = read_result["encoding"]
    mapped_headers, ignored, unmapped_headers = _resolve_headers(
        detected_headers,
        header_overrides=header_overrides,
        ignored_headers=ignored_headers,
    )
    result = _make_result(
        source_path=source_path,
        master_path=master_path,
        encoding=encoding,
        detected_headers=detected_headers,
        mapped_headers=mapped_headers,
        ignored_headers=ignored,
        row_count=len(rows),
    )
    result["unmapped_headers"] = unmapped_headers
    if unmapped_headers:
        result["warnings"].append(
            f"Unmapped headers must be resolved before import: {', '.join(unmapped_headers)}"
        )
    return result


def merge_csv_into_master(
    source_path: PathLike,
    header_overrides: Optional[Dict[str, str]] = None,
    ignored_headers: Optional[Iterable[str]] = None,
    master_path: Optional[PathLike] = None,
    now: Optional[dt.datetime] = None,
    duplicate_strategy: str = "update",
) -> Dict[str, object]:
    return _run_csv_merge(
        source_path,
        header_overrides=header_overrides,
        ignored_headers=ignored_headers,
        master_path=master_path,
        now=now,
        duplicate_strategy=duplicate_strategy,
        write_changes=True,
    )


def preview_csv_merge_counts(
    source_path: PathLike,
    header_overrides: Optional[Dict[str, str]] = None,
    ignored_headers: Optional[Iterable[str]] = None,
    master_path: Optional[PathLike] = None,
    now: Optional[dt.datetime] = None,
    duplicate_strategy: str = "update",
) -> Dict[str, object]:
    return _run_csv_merge(
        source_path,
        header_overrides=header_overrides,
        ignored_headers=ignored_headers,
        master_path=master_path,
        now=now,
        duplicate_strategy=duplicate_strategy,
        write_changes=False,
    )


def confirm_csv_merge_preview(
    preview_result: Dict[str, object],
    *,
    preview_session_id: Optional[str] = None,
) -> Dict[str, object]:
    if not isinstance(preview_result, dict):
        raise ValueError("Merge preview result is required.")
    if preview_result.get("duplicate_strategy") != "merge_consolidate":
        raise ValueError("Only Merge + Consolidate previews can be confirmed from a cached snapshot.")

    expected_session_id = str(preview_result.get("preview_session_id") or "")
    if not expected_session_id:
        raise ValueError("Merge preview is missing a preview_session_id. Re-run preview.")
    if preview_session_id is not None and str(preview_session_id) != expected_session_id:
        raise ValueError("Merge preview session mismatch. Re-run preview.")

    source_path = Path(str(preview_result.get("source_path") or ""))
    master_path = Path(str(preview_result.get("master_path") or ""))
    if not source_path.exists():
        raise ValueError("Incoming CSV changed or is missing. Re-run preview.")
    if not master_path.exists():
        raise ValueError("Master CSV changed or is missing. Re-run preview.")

    if _file_sha256(source_path) != preview_result.get("incoming_csv_hash"):
        raise ValueError("Incoming CSV changed after preview. Re-run preview.")
    if _file_sha256(master_path) != preview_result.get("master_csv_hash"):
        raise ValueError("Master CSV changed after preview. Re-run preview.")

    snapshot = preview_result.get("merge_result_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("Merge preview snapshot missing. Re-run preview.")
    raw_final_rows = snapshot.get("final_rows")
    if not isinstance(raw_final_rows, list):
        raise ValueError("Merge preview final rows missing. Re-run preview.")

    final_rows = [_copy_master_shaped_row(row if isinstance(row, dict) else {}) for row in raw_final_rows]
    backup_path = _backup_master_csv(master_path)
    _write_master_rows_exact(final_rows, master_path)

    confirmed = dict(preview_result)
    confirmed["backup_path"] = str(backup_path)
    confirmed["dry_run"] = False
    confirmed["confirmed_preview_session_id"] = expected_session_id
    return confirmed


def _run_csv_merge(
    source_path: PathLike,
    header_overrides: Optional[Dict[str, str]] = None,
    ignored_headers: Optional[Iterable[str]] = None,
    master_path: Optional[PathLike] = None,
    now: Optional[dt.datetime] = None,
    duplicate_strategy: str = "update",
    write_changes: bool = True,
) -> Dict[str, object]:
    if duplicate_strategy not in _DUPLICATE_STRATEGIES:
        raise ValueError(
            f"Unsupported duplicate strategy '{duplicate_strategy}'. "
            f"Expected one of: {', '.join(sorted(_DUPLICATE_STRATEGIES))}"
        )
    if duplicate_strategy == "merge_consolidate":
        return _run_consolidating_csv_merge(
            source_path,
            header_overrides=header_overrides,
            ignored_headers=ignored_headers,
            master_path=master_path,
            write_changes=write_changes,
        )
    preview = preview_csv_import(
        source_path,
        header_overrides=header_overrides,
        ignored_headers=ignored_headers,
        master_path=master_path,
    )
    rows = list(read_csv_rows(source_path)["rows"])
    preview["row_count"] = len(rows)
    if preview["unmapped_headers"]:
        preview["rows_unresolved_mapping"] = len(rows)
        return preview

    resolved_master_path = Path(master_path) if master_path is not None else get_default_master_csv_path()
    if write_changes:
        resolved_master_path = ensure_master_csv_exists(resolved_master_path)
    master_rows = _load_master_rows(resolved_master_path) if resolved_master_path.exists() else []
    indexes, row_key_cache = _build_indexes(master_rows)
    preview["duplicate_strategy"] = duplicate_strategy

    run_dt = _coerce_run_datetime(now)
    batch_id = _make_import_batch(run_dt)
    timestamp = _format_timestamp(run_dt)
    source_basename = Path(source_path).name
    changed = False

    for row_offset, raw_row in enumerate(rows, start=2):
        try:
            canonical_row = build_canonical_row(
                raw_row,
                preview["mapped_headers"],
                header_order=preview["detected_headers"],
            )
            incoming_row = _prepare_incoming_row(canonical_row)
            match_state, matched_index, reason = _select_match(incoming_row, indexes)

            if match_state == "ambiguous":
                preview["rows_ambiguous"] += 1
                preview["error_rows"].append(
                    {
                        "row_number": row_offset,
                        "reason": reason or "ambiguous_match",
                        "row": incoming_row,
                    }
                )
                continue

            if match_state == "matched" and matched_index is not None:
                preview["rows_duplicates_detected"] += 1
                if duplicate_strategy == "skip":
                    preview["rows_skipped_duplicates"] += 1
                    continue
                if duplicate_strategy == "keep_both":
                    new_row = _prepare_new_row(
                        incoming_row,
                        source_basename=source_basename,
                        batch_id=batch_id,
                        timestamp=timestamp,
                    )
                    master_rows.append(new_row)
                    _append_index_entry(indexes, row_key_cache, len(master_rows) - 1, new_row)
                    preview["rows_added"] += 1
                    preview["rows_kept_duplicates"] += 1
                    changed = True
                    continue

                merged_row, row_changed = _merge_existing_row(
                    master_rows[matched_index],
                    incoming_row,
                    source_basename=source_basename,
                    timestamp=timestamp,
                )
                if row_changed:
                    _replace_index_entry(indexes, row_key_cache, matched_index, merged_row)
                    master_rows[matched_index] = merged_row
                    preview["rows_updated"] += 1
                    changed = True
                else:
                    preview["rows_skipped_duplicates"] += 1
                continue

            new_row = _prepare_new_row(
                incoming_row,
                source_basename=source_basename,
                batch_id=batch_id,
                timestamp=timestamp,
            )
            master_rows.append(new_row)
            _append_index_entry(indexes, row_key_cache, len(master_rows) - 1, new_row)
            preview["rows_added"] += 1
            changed = True
        except Exception as exc:
            preview["rows_errors"] += 1
            preview["error_rows"].append(
                {
                    "row_number": row_offset,
                    "reason": str(exc),
                    "row": raw_row,
                }
            )

    if changed and write_changes:
        _write_master_rows(master_rows, resolved_master_path)

    return preview


def _run_consolidating_csv_merge(
    source_path: PathLike,
    header_overrides: Optional[Dict[str, str]] = None,
    ignored_headers: Optional[Iterable[str]] = None,
    master_path: Optional[PathLike] = None,
    write_changes: bool = True,
) -> Dict[str, object]:
    resolved_master_path = Path(master_path) if master_path is not None else get_default_master_csv_path()
    incoming_csv_hash = _file_sha256(source_path)
    master_csv_hash = _file_sha256(resolved_master_path) if resolved_master_path.exists() else ""
    preview = preview_csv_import(
        source_path,
        header_overrides=header_overrides,
        ignored_headers=ignored_headers,
        master_path=resolved_master_path,
    )
    rows = list(read_csv_rows(source_path)["rows"])
    preview["row_count"] = len(rows)
    preview["duplicate_strategy"] = "merge_consolidate"
    preview["dry_run"] = not write_changes
    preview["preview_session_id"] = uuid.uuid4().hex if not write_changes else ""
    preview["incoming_csv_hash"] = incoming_csv_hash
    preview["master_csv_hash"] = master_csv_hash
    preview.update(
        {
            "rows_existing": 0,
            "rows_incoming": len(rows),
            "rows_merged": 0,
            "rows_replaced": 0,
            "rows_kept_existing": 0,
            "rows_added_new": 0,
            "rows_final": 0,
            "backup_path": "",
            "merge_preview_counts": {
                "NEW": 0,
                "UPGRADE": 0,
                "KEEP_EXISTING": 0,
                "UNCHANGED": 0,
            },
            "merge_preview_upgrade_rows": [],
            "merge_result_snapshot": {},
        }
    )
    if preview["unmapped_headers"]:
        preview["rows_unresolved_mapping"] = len(rows)
        return preview

    if write_changes:
        resolved_master_path = ensure_master_csv_exists(resolved_master_path)
    master_rows = _load_master_rows_exact(resolved_master_path) if resolved_master_path.exists() else []
    preview["rows_existing"] = len(master_rows)

    consolidated: Dict[Tuple[str, str], Dict[str, object]] = {}
    final_rows: List[Dict[str, str]] = []

    def _add_unkeyed(row: Dict[str, str], *, source: str) -> None:
        if source == "incoming":
            preview["rows_added_new"] += 1
        final_rows.append(_copy_master_shaped_row(row))

    def _consider(row: Dict[str, str], *, source: str, score_row: Optional[Dict[str, str]] = None) -> None:
        key = _lead_vault_consolidation_key(row)
        if key is None:
            preview["rows_errors"] += 1
            preview["error_rows"].append(
                {
                    "row_number": "",
                    "reason": "missing artist_url and Artist Name",
                    "row": row,
                }
            )
            return
        current = consolidated.get(key)
        candidate = _copy_master_shaped_row(row)
        if source == "incoming":
            _remove_unsafe_incoming_contact(candidate)
        candidate_score = _score_lead_vault_row(candidate, score_row=score_row)
        if current is None:
            consolidated[key] = {
                "row": candidate,
                "score": candidate_score,
                "source": source,
            }
            if source == "incoming":
                preview["rows_added_new"] += 1
            return

        preview["rows_duplicates_detected"] += 1
        preview["rows_merged"] += 1
        existing_score = int(current["score"])
        if _candidate_beats_current(
            candidate,
            candidate_score,
            current["row"],
            existing_score,
            current_source=str(current["source"]),
            candidate_source=source,
        ):
            previous_row = _copy_master_shaped_row(current["row"])
            preserve_origin_fields(candidate, previous_row)
            candidate = _coalesce_consolidation_enrichment(candidate, previous_row)
            if current["source"] == "existing" and source == "incoming":
                preview["rows_replaced"] += 1
                preview["rows_updated"] += 1
            consolidated[key] = {
                "row": candidate,
                "score": candidate_score,
                "source": source,
            }
            return
        current["row"] = _coalesce_consolidation_enrichment(
            _copy_master_shaped_row(current["row"]),
            candidate,
        )
        if current["source"] == "existing" and source == "incoming":
            preview["rows_kept_existing"] += 1
            preview["rows_skipped_duplicates"] += 1

    for row in master_rows:
        key = _lead_vault_consolidation_key(row)
        if key is None:
            _add_unkeyed(row, source="existing")
            continue
        _consider(row, source="existing")

    for row_offset, raw_row in enumerate(rows, start=2):
        try:
            canonical_row = build_canonical_row(
                raw_row,
                preview["mapped_headers"],
                header_order=preview["detected_headers"],
            )
            key = _lead_vault_consolidation_key(canonical_row)
            if key is None:
                preview["rows_errors"] += 1
                preview["error_rows"].append(
                    {
                        "row_number": row_offset,
                        "reason": "missing artist_url and Artist Name",
                        "row": canonical_row,
                    }
                )
                continue

            candidate = _copy_master_shaped_row(canonical_row)
            _remove_unsafe_incoming_contact(candidate)
            candidate_score = _score_lead_vault_row(candidate, score_row=raw_row)
            current = consolidated.get(key)
            outcome = "NEW"
            if current is not None:
                current_row = _copy_master_shaped_row(current["row"])
                current_score = int(current["score"])
                if str(current["source"]) != "existing":
                    outcome = "NEW"
                elif _candidate_beats_current(
                    candidate,
                    candidate_score,
                    current_row,
                    current_score,
                    current_source=str(current["source"]),
                    candidate_source="incoming",
                ):
                    outcome = "UPGRADE"
                    if current["source"] == "existing":
                        preview["merge_preview_upgrade_rows"].append(
                            _make_upgrade_preview_row(
                                existing_row=current_row,
                                incoming_row=candidate,
                                existing_score=current_score,
                                incoming_score=candidate_score,
                                key=key,
                            )
                        )
                elif _meaningfully_unchanged_preview(current_row, candidate, current_score, candidate_score):
                    outcome = "UNCHANGED"
                else:
                    outcome = "KEEP_EXISTING"
            preview["merge_preview_counts"][outcome] += 1
            _consider(canonical_row, source="incoming", score_row=raw_row)
        except Exception as exc:
            preview["rows_errors"] += 1
            preview["error_rows"].append(
                {
                    "row_number": row_offset,
                    "reason": str(exc),
                    "row": raw_row,
                }
            )

    final_rows.extend(_copy_master_shaped_row(item["row"]) for item in consolidated.values())
    for row in final_rows:
        repair_origin_fields(row, ingest_source=_derive_ingest_source(row, Path(source_path).name))
    preview["rows_added"] = preview["rows_added_new"]
    preview["rows_final"] = len(final_rows)
    classified_total = sum(int(value) for value in preview["merge_preview_counts"].values())
    if classified_total != len(rows) - int(preview.get("rows_errors", 0) or 0):
        raise AssertionError("Merge preview classification count mismatch.")
    preview["merge_result_snapshot"] = {
        "final_rows": [_copy_master_shaped_row(row) for row in final_rows],
        "outcome_counts": dict(preview["merge_preview_counts"]),
        "upgrade_rows": list(preview["merge_preview_upgrade_rows"]),
    }

    if write_changes:
        backup_path = _backup_master_csv(resolved_master_path)
        preview["backup_path"] = str(backup_path)
        _write_master_rows_exact(final_rows, resolved_master_path)

    return preview


def _make_result(
    source_path: PathLike,
    master_path: Optional[PathLike],
    encoding: Optional[str],
    detected_headers: Sequence[str],
    mapped_headers: Dict[str, str],
    ignored_headers: Sequence[str],
    row_count: int,
) -> Dict[str, object]:
    resolved_master_path = (
        str(Path(master_path))
        if master_path is not None
        else str(get_default_master_csv_path())
    )
    return {
        "master_path": resolved_master_path,
        "source_path": str(Path(source_path)),
        "encoding": encoding,
        "detected_headers": list(detected_headers),
        "mapped_headers": dict(mapped_headers),
        "ignored_headers": list(ignored_headers),
        "unmapped_headers": [],
        "row_count": row_count,
        "rows_added": 0,
        "rows_updated": 0,
        "rows_duplicates_detected": 0,
        "rows_skipped_duplicates": 0,
        "rows_kept_duplicates": 0,
        "rows_unresolved_mapping": 0,
        "rows_ambiguous": 0,
        "rows_errors": 0,
        "warnings": [],
        "error_rows": [],
    }


def _resolve_headers(
    detected_headers: Sequence[str],
    header_overrides: Optional[Dict[str, str]] = None,
    ignored_headers: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, str], List[str], List[str]]:
    canonical_schema = set(get_canonical_master_schema())
    mapped_headers = map_headers_to_canonical(detected_headers)
    ignored = {str(header) for header in (ignored_headers or [])}
    overrides = dict(header_overrides or {})

    for raw_header, target in overrides.items():
        header_name = str(raw_header)
        if target == _IGNORE_HEADER_SENTINEL:
            ignored.add(header_name)
            mapped_headers.pop(header_name, None)
            continue
        if target in canonical_schema:
            mapped_headers[header_name] = target

    ignored_headers_out: List[str] = []
    unmapped_headers: List[str] = []
    for header in detected_headers:
        if header in ignored:
            mapped_headers.pop(header, None)
            ignored_headers_out.append(header)
            continue
        if header not in mapped_headers:
            unmapped_headers.append(header)

    return mapped_headers, ignored_headers_out, unmapped_headers


def _load_master_rows(path: PathLike) -> List[Dict[str, str]]:
    read_result = read_csv_rows(path)
    rows = list(read_result["rows"])
    schema = get_canonical_master_schema()
    master_rows: List[Dict[str, str]] = []
    for raw_row in rows:
        row = {field: _clean_cell(raw_row.get(field, "")) for field in schema}
        master_rows.append(row)
    return master_rows


def _load_master_rows_exact(path: PathLike) -> List[Dict[str, str]]:
    read_result = read_csv_rows(path)
    rows = list(read_result["rows"])
    schema = get_canonical_master_schema()
    return [{field: "" if raw_row.get(field, "") is None else str(raw_row.get(field, "")) for field in schema} for raw_row in rows]


def _build_indexes(
    rows: Sequence[Dict[str, str]],
) -> Tuple[Dict[str, Dict[str, Set[int]]], List[Dict[str, object]]]:
    indexes: Dict[str, Dict[str, Set[int]]] = {
        "profile_url": defaultdict(set),
        "artist_location": defaultdict(set),
    }
    row_key_cache: List[Dict[str, object]] = []
    for index, row in enumerate(rows):
        keys = _row_keys(row)
        row_key_cache.append(keys)
        _add_keys_to_indexes(indexes, index, keys)
    return indexes, row_key_cache


def _replace_index_entry(
    indexes: Dict[str, Dict[str, Set[int]]],
    row_key_cache: List[Dict[str, object]],
    row_index: int,
    row: Dict[str, str],
) -> None:
    _remove_keys_from_indexes(indexes, row_index, row_key_cache[row_index])
    keys = _row_keys(row)
    row_key_cache[row_index] = keys
    _add_keys_to_indexes(indexes, row_index, keys)


def _append_index_entry(
    indexes: Dict[str, Dict[str, Set[int]]],
    row_key_cache: List[Dict[str, object]],
    row_index: int,
    row: Dict[str, str],
) -> None:
    keys = _row_keys(row)
    row_key_cache.append(keys)
    _add_keys_to_indexes(indexes, row_index, keys)


def _add_keys_to_indexes(
    indexes: Dict[str, Dict[str, Set[int]]],
    row_index: int,
    keys: Dict[str, object],
) -> None:
    for field_name in ("profile_url", "artist_location"):
        value = keys.get(field_name)
        if isinstance(value, str) and value:
            indexes[field_name][value].add(row_index)


def _remove_keys_from_indexes(
    indexes: Dict[str, Dict[str, Set[int]]],
    row_index: int,
    keys: Dict[str, object],
) -> None:
    for field_name in ("profile_url", "artist_location"):
        value = keys.get(field_name)
        if isinstance(value, str) and value:
            _discard_index_value(indexes[field_name], value, row_index)


def _discard_index_value(index: Dict[str, Set[int]], value: str, row_index: int) -> None:
    hits = index.get(value)
    if not hits:
        return
    hits.discard(row_index)
    if not hits:
        index.pop(value, None)


def _row_keys(row: Dict[str, str]) -> Dict[str, object]:
    profile_url = _normalize_profile_url(row.get("Source_URL", ""))
    artist_location = _normalize_artist_location_key(
        row.get("Artist", ""),
        row.get("Location", ""),
        profile_url=profile_url,
    )
    return {
        "profile_url": profile_url,
        "artist_location": artist_location,
    }


def _select_match(
    incoming_row: Dict[str, str],
    indexes: Dict[str, Dict[str, Set[int]]],
) -> Tuple[str, Optional[int], str]:
    matched_index: Optional[int] = None
    profile_url = _normalize_profile_url(incoming_row.get("Source_URL", ""))
    if profile_url:
        state, matched_index, reason = _fold_hits(
            indexes["profile_url"].get(profile_url, set()),
            matched_index,
            "profile_url",
            profile_url,
        )
        if state != "continue":
            return state, matched_index, reason
        if matched_index is not None:
            return "matched", matched_index, "matched_existing"
        return "new", None, "profile_url_no_match"
    artist_location = _normalize_artist_location_key(
        incoming_row.get("Artist", ""),
        incoming_row.get("Location", ""),
        profile_url=profile_url,
    )
    if not artist_location:
        return "new", None, "no_match_keys"
    state, matched_index, reason = _fold_hits(
        indexes["artist_location"].get(artist_location, set()),
        matched_index,
        "artist_location",
        artist_location,
    )
    if state == "continue" and matched_index is not None:
        return "matched", matched_index, "artist_location_fallback"
    if state == "continue":
        return "new", None, "artist_location_no_match"
    return state, matched_index, reason


def _fold_hits(
    hits: Set[int],
    matched_index: Optional[int],
    field_name: str,
    value: str,
) -> Tuple[str, Optional[int], str]:
    if len(hits) > 1:
        return "ambiguous", None, f"multiple matches for {field_name}={value}"
    if len(hits) == 1:
        hit = next(iter(hits))
        if matched_index is None:
            return "continue", hit, "matched"
        if matched_index != hit:
            return "ambiguous", None, f"conflicting matches for {field_name}={value}"
    return "continue", matched_index, "continue"


def _prepare_incoming_row(row: Dict[str, str]) -> Dict[str, str]:
    prepared = {field: _clean_cell(row.get(field, "")) for field in get_canonical_master_schema()}
    prepared["Primary_Email"] = _normalize_email(prepared.get("Primary_Email", ""))
    prepared["All_Emails"] = _merge_email_lists("", prepared.get("All_Emails", ""))
    prepared["Source_URL"] = _clean_cell(prepared.get("Source_URL", ""))
    repair_origin_fields(prepared)
    return prepared


def _prepare_new_row(
    row: Dict[str, str],
    source_basename: str,
    batch_id: str,
    timestamp: str,
) -> Dict[str, str]:
    new_row = {field: _clean_cell(row.get(field, "")) for field in get_canonical_master_schema()}
    if "Import_Source_File" in new_row:
        new_row["Import_Source_File"] = source_basename
    if "Import_Batch" in new_row:
        new_row["Import_Batch"] = batch_id
    if "Date_Added" in new_row:
        new_row["Date_Added"] = timestamp
    if "Last_Updated" in new_row:
        new_row["Last_Updated"] = timestamp
    repair_origin_fields(new_row, ingest_source=_derive_ingest_source(new_row, source_basename))
    return new_row


def _merge_existing_row(
    existing_row: Dict[str, str],
    incoming_row: Dict[str, str],
    source_basename: str,
    timestamp: str,
) -> Tuple[Dict[str, str], bool]:
    merged = {field: _clean_cell(existing_row.get(field, "")) for field in get_canonical_master_schema()}
    original_origin = (merged.get("Lead_Source", ""), merged.get("Source_Directory", ""))
    merge_origin_fields(merged, incoming_row)
    changed = original_origin != (merged.get("Lead_Source", ""), merged.get("Source_Directory", ""))

    for field_name in get_canonical_master_schema():
        existing_value = merged[field_name]
        incoming_value = _clean_cell(incoming_row.get(field_name, ""))

        if field_name in {"Import_Source_File", "Import_Batch", "Date_Added", "Last_Updated", "Lead_Source", "Source_Directory"}:
            continue

        if field_name in _LIST_LIKE_FIELDS:
            merged_value = (
                _merge_all_emails_field(merged, incoming_row)
                if field_name == "All_Emails"
                else _merge_tokenized_values(existing_value, incoming_value, normalizer=_normalize_link_token)
            )
            if merged_value != existing_value:
                merged[field_name] = merged_value
                changed = True
            continue

        if field_name == "Primary_Email":
            merged_primary, merged_all = _merge_primary_email_field(merged, incoming_row)
            if merged_primary != existing_value:
                merged[field_name] = merged_primary
                changed = True
            if merged_all != merged["All_Emails"]:
                merged["All_Emails"] = merged_all
                changed = True
            continue

        if field_name == "Facebook_URL":
            merged_value, external_links = _merge_facebook_field(merged, incoming_row)
            if merged_value != existing_value:
                merged[field_name] = merged_value
                changed = True
            if external_links != merged["External_Links"]:
                merged["External_Links"] = external_links
                changed = True
            continue

        if field_name in _SOCIAL_LINK_FIELDS:
            merged_value, external_links = _merge_social_field(field_name, merged, incoming_row)
            if merged_value != existing_value:
                merged[field_name] = merged_value
                changed = True
            if external_links != merged["External_Links"]:
                merged["External_Links"] = external_links
                changed = True
            continue

        if not incoming_value:
            continue
        if not existing_value:
            merged[field_name] = incoming_value
            changed = True
            continue
        if _values_equivalent(field_name, existing_value, incoming_value):
            continue

    if not changed:
        return merged, False

    if "Import_Source_File" in merged and not _clean_cell(merged.get("Import_Source_File", "")):
        merged["Import_Source_File"] = source_basename
    if "Last_Updated" in merged:
        merged["Last_Updated"] = timestamp
    return merged, True


def _merge_all_emails_field(existing_row: Dict[str, str], incoming_row: Dict[str, str]) -> str:
    existing_primary = _normalize_email(existing_row.get("Primary_Email", ""))
    incoming_primary = _normalize_email(incoming_row.get("Primary_Email", ""))
    existing_all = _clean_cell(existing_row.get("All_Emails", ""))
    incoming_all = _clean_cell(incoming_row.get("All_Emails", ""))

    extras: List[str] = []
    if existing_primary and incoming_primary and incoming_primary != existing_primary:
        extras.append(incoming_primary)

    return _merge_email_lists(existing_all, incoming_all, extras)


def _merge_primary_email_field(existing_row: Dict[str, str], incoming_row: Dict[str, str]) -> Tuple[str, str]:
    existing_primary = _normalize_email(existing_row.get("Primary_Email", ""))
    incoming_primary = _normalize_email(incoming_row.get("Primary_Email", ""))
    merged_primary = existing_primary or incoming_primary
    merged_all = _merge_all_emails_field(
        {
            **existing_row,
            "Primary_Email": existing_primary,
        },
        {
            **incoming_row,
            "Primary_Email": incoming_primary,
        },
    )
    return merged_primary, merged_all


def _merge_facebook_field(existing_row: Dict[str, str], incoming_row: Dict[str, str]) -> Tuple[str, str]:
    existing_value = _clean_cell(existing_row.get("Facebook_URL", ""))
    incoming_value = _clean_cell(incoming_row.get("Facebook_URL", ""))
    external_links = _merge_external_links(existing_row, incoming_row)

    if not incoming_value:
        return existing_value, external_links
    if existing_value and not _values_equivalent("Facebook_URL", existing_value, incoming_value):
        external_links = _merge_tokenized_values(
            external_links,
            existing_value,
            extras=[incoming_value],
            normalizer=_normalize_link_token,
        )
    return incoming_value, external_links


def _merge_social_field(
    field_name: str,
    existing_row: Dict[str, str],
    incoming_row: Dict[str, str],
) -> Tuple[str, str]:
    existing_value = _clean_cell(existing_row.get(field_name, ""))
    incoming_value = _clean_cell(incoming_row.get(field_name, ""))
    external_links = _merge_external_links(existing_row, incoming_row)
    if not incoming_value:
        return existing_value, external_links
    if not existing_value or _values_equivalent(field_name, existing_value, incoming_value):
        return existing_value or incoming_value, external_links
    external_links = _merge_tokenized_values(
        external_links,
        incoming_value,
        normalizer=_normalize_link_token,
    )
    return existing_value, external_links


def _copy_master_shaped_row(row: Dict[str, str]) -> Dict[str, str]:
    return {field: "" if row.get(field, "") is None else str(row.get(field, "")) for field in get_canonical_master_schema()}


def _derive_ingest_source(row: Dict[str, str], source_basename: str = "") -> str:
    for field_name in ("Lead_Source", "Source_Directory", "Discovery_Source", "Source_Job"):
        value = _clean_cell(row.get(field_name, ""))
        if value:
            return value
    stem = Path(source_basename).stem if source_basename else ""
    return _clean_cell(stem)


def _is_nullish_value(value: object) -> bool:
    text = "" if value is None else str(value).strip()
    return text == "" or text.casefold() in {"nan", "none"}


def _lead_vault_consolidation_key(row: Dict[str, str]) -> Optional[Tuple[str, str]]:
    artist_url = row.get("Source_URL", "")
    if not _is_nullish_value(artist_url):
        return ("artist_url", _normalize_profile_url(artist_url) or str(artist_url).strip().casefold())

    artist = row.get("Artist", "")
    if not _is_nullish_value(artist):
        return ("artist", re.sub(r"\s+", " ", str(artist).strip()).casefold())

    return None


def _has_lead_vault_email(row: Dict[str, str]) -> bool:
    return not _is_nullish_value(row.get("Primary_Email", ""))


def _split_email_all_for_scoring(value: object) -> Set[str]:
    if _is_nullish_value(value):
        return set()
    emails: Set[str] = set()
    for token in re.split(r"[;,|]+", str(value)):
        email = token.strip().lower()
        if email and not _is_nullish_value(email):
            emails.add(email)
    return emails


def _row_has_flag(row: Dict[str, str], flag_value: str, score_row: Optional[Dict[str, str]] = None) -> bool:
    target = flag_value.casefold()
    values: List[object] = list(row.values())
    if score_row:
        values.extend(score_row.values())
    for value in values:
        if str(value or "").strip().casefold() == target:
            return True
    return False


def _score_lead_vault_row(row: Dict[str, str], score_row: Optional[Dict[str, str]] = None) -> int:
    score = 0
    has_email = _has_lead_vault_email(row)
    if has_email:
        score += 100
    score += 10 * len(_split_email_all_for_scoring(row.get("All_Emails", "")))
    if not _is_nullish_value(row.get("Facebook_URL", "")):
        score += 5
    if not _is_nullish_value(row.get("Instagram_URL", "")):
        score += 5
    if not _is_nullish_value(row.get("External_Links", "")):
        score += 3
    if not has_email and _row_has_flag(row, "fb_no_email_written", score_row=score_row):
        score -= 5
    if not has_email and _row_has_flag(row, "ig_no_email_written", score_row=score_row):
        score -= 5
    return score


def _remove_unsafe_incoming_contact(row: Dict[str, str]) -> None:
    """Keep rejected contact values from affecting consolidation winner selection."""
    primary = _normalize_email(row.get("Primary_Email", ""))
    if primary and _is_unsafe_lead_vault_email(primary):
        row["Primary_Email"] = ""

    raw_all = row.get("All_Emails", "")
    all_emails = _normalize_email_list(raw_all)
    safe_all = [email for email in all_emails if not _is_unsafe_lead_vault_email(email)]
    if len(safe_all) != len(all_emails):
        row["All_Emails"] = ";".join(safe_all)

    if not _has_lead_vault_email(row) and not _split_email_all_for_scoring(row.get("All_Emails", "")):
        for field_name in (
            "Email_Source",
            "Email_Source_URL",
            "Email_Type",
            "Email_Source_Type",
            "Email_Extract_Method",
            "Contact_Mode",
        ):
            row[field_name] = ""


def _is_unsafe_lead_vault_email(email: str) -> bool:
    return (
        is_obvious_placeholder_email(email)
        or is_platform_support_email(email)
        or is_system_telemetry_email(email)
    )


def _coalesce_consolidation_enrichment(
    winner: Dict[str, str],
    loser: Dict[str, str],
) -> Dict[str, str]:
    """Preserve approved enrichment without changing the canonical row winner.

    This deliberately does not call the generic update merge: contact, origin,
    status, diagnostics, and canonical identity fields have different semantics.
    """
    merged = _copy_master_shaped_row(winner)
    rejected_relationships = _rejected_musicbrainz_relationship_urls(loser)

    loser_website = _allowed_losing_relationship_value(
        "Website", loser.get("Website", ""), rejected_relationships
    )
    website_added = False
    if not _clean_cell(merged.get("Website", "")) and loser_website:
        merged["Website"] = loser_website
        website_added = True
    if website_added or (
        loser_website
        and _values_equivalent("Website", merged.get("Website", ""), loser_website)
    ):
        for field_name in ("Domain", "Domain_Root"):
            if not _clean_cell(merged.get(field_name, "")):
                merged[field_name] = _clean_cell(loser.get(field_name, ""))

    for field_name in _CONSOLIDATION_RELATIONSHIP_FIELDS:
        incoming_value = _allowed_losing_relationship_value(
            field_name, loser.get(field_name, ""), rejected_relationships
        )
        if not incoming_value:
            continue
        existing_value = _clean_cell(merged.get(field_name, ""))
        if not existing_value:
            merged[field_name] = incoming_value
            continue
        if _values_equivalent(field_name, existing_value, incoming_value):
            continue
        # Keep the winner's canonical profile. A second validated social profile
        # remains discoverable as an external relationship where the schema allows.
        if field_name not in {"Contact_Page_URL", "Spotify_URL"}:
            merged["External_Links"] = _merge_tokenized_values(
                merged.get("External_Links", ""),
                incoming_value,
                normalizer=_normalize_link_token,
            )

    loser_social = _filter_rejected_relationship_tokens(
        loser.get("Social Link", ""), rejected_relationships
    )
    merged["Social Link"] = _merge_tokenized_values(
        merged.get("Social Link", ""), loser_social, normalizer=_normalize_link_token
    )
    loser_external = _filter_rejected_relationship_tokens(
        loser.get("External_Links", ""), rejected_relationships
    )
    merged["External_Links"] = _merge_tokenized_values(
        merged.get("External_Links", ""), loser_external, normalizer=_normalize_link_token
    )

    if not _clean_cell(merged.get("Instagram_Handle", "")) and _clean_cell(
        merged.get("Instagram_URL", "")
    ):
        merged["Instagram_Handle"] = _clean_cell(loser.get("Instagram_Handle", ""))

    if all(not _clean_cell(merged.get(field_name, "")) for field_name in _MUSICBRAINZ_IDENTITY_FIELDS):
        if _musicbrainz_identity_is_eligible(loser):
            for field_name in _MUSICBRAINZ_IDENTITY_FIELDS:
                merged[field_name] = _clean_cell(loser.get(field_name, ""))

    return merged


def _allowed_losing_relationship_value(
    field_name: str,
    value: object,
    rejected_relationships: Set[str],
) -> str:
    cleaned = _clean_cell(value)
    if not cleaned:
        return ""
    normalized = _normalize_link_token(cleaned)
    if normalized and normalized in rejected_relationships:
        return ""
    return cleaned


def _filter_rejected_relationship_tokens(value: object, rejected_relationships: Set[str]) -> str:
    allowed = []
    for token in _split_merged_tokens(value):
        if _normalize_link_token(token) not in rejected_relationships:
            allowed.append(token)
    return ";".join(allowed)


def _musicbrainz_evidence(row: Dict[str, str]) -> Optional[Dict[str, object]]:
    try:
        payload = json.loads(_clean_cell(row.get("Identity_Evidence_JSON", "")))
    except (TypeError, ValueError):
        return None
    musicbrainz = payload.get("musicbrainz") if isinstance(payload, dict) else None
    return musicbrainz if isinstance(musicbrainz, dict) else None


def _musicbrainz_identity_is_eligible(row: Dict[str, str]) -> bool:
    if _clean_cell(row.get("MusicBrainz_Status", "")) != "matched":
        return False
    if _clean_cell(row.get("Identity_Match_Method", "")) != "spotify_url_relationship":
        return False
    evidence = _musicbrainz_evidence(row)
    if not evidence or evidence.get("status") != "matched":
        return False
    if evidence.get("match_method") != "spotify_url_relationship":
        return False
    artist = evidence.get("artist")
    if not isinstance(artist, dict):
        return False
    row_artist = _normalize_artist(row.get("Artist", ""))
    accepted_names = {_normalize_artist(artist.get("name", ""))}
    aliases = artist.get("aliases", [])
    if isinstance(aliases, list):
        for alias in aliases:
            accepted_names.add(
                _normalize_artist(alias.get("name", "") if isinstance(alias, dict) else alias)
            )
    accepted_names.discard("")
    return bool(row_artist and row_artist in accepted_names)


def _rejected_musicbrainz_relationship_urls(row: Dict[str, str]) -> Set[str]:
    if _musicbrainz_identity_is_eligible(row):
        return set()
    evidence = _musicbrainz_evidence(row)
    relationships = evidence.get("relationships") if evidence else None
    if not isinstance(relationships, dict):
        return set()
    rejected: Set[str] = set()
    for entries in relationships.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            value = entry.get("url", "") if isinstance(entry, dict) else entry
            normalized = _normalize_link_token(value)
            if normalized:
                rejected.add(normalized)
    return rejected


def _candidate_beats_current(
    candidate_row: Dict[str, str],
    candidate_score: int,
    current_row: Dict[str, str],
    current_score: int,
    *,
    current_source: str,
    candidate_source: str,
) -> bool:
    current_has_email = _has_lead_vault_email(current_row)
    candidate_has_email = _has_lead_vault_email(candidate_row)
    if current_source == "existing" and candidate_source == "incoming":
        if current_has_email and not candidate_has_email:
            return False
        if candidate_has_email and not current_has_email:
            return True
    if candidate_score > current_score:
        return True
    if candidate_score < current_score:
        return False
    if current_source == "existing" and candidate_source == "incoming":
        return False
    return False


def _preview_primary_email(row: Dict[str, str]) -> str:
    return _normalize_email(row.get("Primary_Email", ""))


def _preview_email_all_values(row: Dict[str, str]) -> List[str]:
    return _normalize_email_list(row.get("All_Emails", ""))


def _meaningfully_unchanged_preview(
    existing_row: Dict[str, str],
    incoming_row: Dict[str, str],
    existing_score: int,
    incoming_score: int,
) -> bool:
    return (
        _preview_primary_email(existing_row) == _preview_primary_email(incoming_row)
        and set(_preview_email_all_values(existing_row)) == set(_preview_email_all_values(incoming_row))
        and int(existing_score) == int(incoming_score)
    )


def _make_upgrade_preview_row(
    *,
    existing_row: Dict[str, str],
    incoming_row: Dict[str, str],
    existing_score: int,
    incoming_score: int,
    key: Tuple[str, str],
) -> Dict[str, object]:
    return {
        "artist_name": _clean_cell(incoming_row.get("Artist", "")) or _clean_cell(existing_row.get("Artist", "")),
        "key": key[1],
        "key_type": key[0],
        "existing_email": _preview_primary_email(existing_row),
        "incoming_email": _preview_primary_email(incoming_row),
        "existing_email_all_count": len(set(_preview_email_all_values(existing_row))),
        "incoming_email_all_count": len(set(_preview_email_all_values(incoming_row))),
        "existing_score": int(existing_score),
        "incoming_score": int(incoming_score),
    }


def _merge_external_links(existing_row: Dict[str, str], incoming_row: Dict[str, str]) -> str:
    return _merge_tokenized_values(
        existing_row.get("External_Links", ""),
        incoming_row.get("External_Links", ""),
        normalizer=_normalize_link_token,
    )


def _merge_tokenized_values(
    existing_value: object,
    incoming_value: object,
    extras: Optional[Sequence[object]] = None,
    *,
    normalizer=None,
) -> str:
    normalize = normalizer or (lambda value: _clean_cell(value).lower())
    merged: List[str] = []
    seen: Set[str] = set()

    def _ingest(raw: object) -> None:
        for token in _split_merged_tokens(raw):
            cleaned = _clean_cell(token)
            if not cleaned:
                continue
            dedupe_key = normalize(cleaned)
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(cleaned)

    _ingest(existing_value)
    _ingest(incoming_value)
    for extra in extras or []:
        _ingest(extra)
    return ";".join(merged)


def _split_merged_tokens(raw: object) -> List[str]:
    text = _clean_cell(raw)
    if not text:
        return []
    return [token for token in re.split(r"[;\n]+", text) if _clean_cell(token)]


def _merge_email_lists(existing_value: str, incoming_value: str, extras: Optional[Sequence[str]] = None) -> str:
    merged: List[str] = []
    seen: Set[str] = set()

    def _ingest(raw: object) -> None:
        for email in _normalize_email_list(raw):
            if email not in seen:
                seen.add(email)
                merged.append(email)

    _ingest(existing_value)
    _ingest(incoming_value)
    for extra in extras or []:
        _ingest(extra)
    return ";".join(merged)


def _collect_row_emails(row: Dict[str, str]) -> List[str]:
    emails: List[str] = []
    seen: Set[str] = set()
    for raw in (row.get("Primary_Email", ""), row.get("All_Emails", "")):
        for email in _normalize_email_list(raw):
            if email not in seen:
                seen.add(email)
                emails.append(email)
    return emails


def _normalize_email_list(raw: object) -> List[str]:
    try:
        from pipeline_runner import normalize_emails

        return list(normalize_emails(raw))
    except Exception:
        text = "" if raw is None else str(raw)
        tokens = re.split(r"[\s,;]+", text)
        emails: List[str] = []
        seen: Set[str] = set()
        for token in tokens:
            normalized = _normalize_email(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                emails.append(normalized)
        return emails


def _normalize_email(value: object) -> str:
    normalized = normalize_email_value("" if value is None else str(value))
    return normalized.lower() if normalized else ""


def _normalize_profile_url(value: object) -> str:
    normalized = _normalize_url(value)
    return normalized.lower() if normalized else ""


def _normalize_domain_root(value: object) -> str:
    text = _clean_cell(value).lower()
    if not text or "." not in text or " " in text:
        return ""
    return text


def _normalize_url(value: object) -> str:
    raw = _clean_cell(value)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if "://" not in raw and not raw.startswith("mailto:"):
        raw = f"https://{raw.lstrip('/')}"
    try:
        from soundcloud_engine import normalize_external_url

        cleaned = normalize_external_url(raw) or raw
    except Exception:
        cleaned = raw
    try:
        parsed = urlsplit(cleaned)
    except Exception:
        return cleaned.rstrip("/")

    host = (parsed.netloc or "").lower()
    if not host:
        return cleaned.rstrip("/")
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    return urlunsplit(((parsed.scheme or "https").lower(), host, path, "", ""))


def _normalize_soundcloud_url(value: object) -> str:
    return _normalize_url(value)


def _normalize_bandcamp_url(value: object) -> str:
    raw = _clean_cell(value)
    if not raw:
        return ""
    try:
        from cross_directory_enricher import _canonicalise_bandcamp_url

        raw = _canonicalise_bandcamp_url(raw) or raw
    except Exception:
        pass
    return _normalize_url(raw)


def _normalize_artist(value: object) -> str:
    name = _clean_cell(value)
    if not name:
        return ""
    cleaned = unicodedata.normalize("NFKD", name)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.lower()
    for suffix in (" official", " - topic"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    cleaned = re.sub(r"^(the|a)\s+", "", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _normalize_location(value: object) -> str:
    location = _clean_cell(value)
    if not location:
        return ""
    location = unicodedata.normalize("NFKD", location)
    location = "".join(ch for ch in location if not unicodedata.combining(ch))
    location = location.casefold()
    location = re.sub(r"\s+", " ", location).strip()
    return location


def _normalize_artist_location_key(artist: object, location: object, *, profile_url: str = "") -> str:
    if profile_url:
        return ""
    normalized_artist = _normalize_artist(artist)
    normalized_location = _normalize_location(location)
    if not normalized_artist or not normalized_location:
        return ""
    return f"{normalized_artist}|||{normalized_location}"


def _normalize_link_token(value: object) -> str:
    text = _clean_cell(value)
    if not text:
        return ""
    normalized_url = _normalize_profile_url(text)
    return normalized_url or text.casefold()


def _values_equivalent(field_name: str, existing_value: str, incoming_value: str) -> bool:
    if field_name == "Primary_Email":
        return _normalize_email(existing_value) == _normalize_email(incoming_value)
    if field_name == "Domain_Root":
        return _normalize_domain_root(existing_value) == _normalize_domain_root(incoming_value)
    if field_name in {"Website", "SoundCloud_URL", "Contact_Page_URL", "Facebook_URL", "Instagram_URL", "Twitter_URL", "Spotify_URL", "LastFM_URL", "YouTube_URL", "TikTok_URL", "Source_URL"}:
        return _normalize_url(existing_value) == _normalize_url(incoming_value)
    if field_name == "Bandcamp_URL":
        return _normalize_bandcamp_url(existing_value) == _normalize_bandcamp_url(incoming_value)
    if field_name == "Artist":
        return _normalize_artist(existing_value) == _normalize_artist(incoming_value)
    if field_name == "Location":
        return _normalize_location(existing_value) == _normalize_location(incoming_value)
    return _clean_cell(existing_value) == _clean_cell(incoming_value)


def _write_master_rows(rows: Sequence[Dict[str, str]], path: PathLike) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.stem}.tmp{target.suffix}")
    fieldnames = get_canonical_master_schema()

    if temp_path.exists():
        temp_path.unlink()

    try:
        prepared_rows = []
        for row in rows:
            prepared = {field: _clean_cell(row.get(field, "")) for field in fieldnames}
            repair_origin_fields(prepared, ingest_source=_derive_ingest_source(prepared, Path(path).name))
            prepared_rows.append(prepared)
        validate_origin_integrity_rows(prepared_rows)
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in prepared_rows:
                writer.writerow({field: _clean_cell(row.get(field, "")) for field in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _write_master_rows_exact(rows: Sequence[Dict[str, str]], path: PathLike) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.stem}.tmp{target.suffix}")
    fieldnames = get_canonical_master_schema()

    if temp_path.exists():
        temp_path.unlink()

    try:
        prepared_rows = []
        for row in rows:
            prepared = {field: "" if row.get(field, "") is None else str(row.get(field, "")) for field in fieldnames}
            repair_origin_fields(prepared, ingest_source=_derive_ingest_source(prepared, Path(path).name))
            prepared_rows.append(prepared)
        validate_origin_integrity_rows(prepared_rows)
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in prepared_rows:
                writer.writerow({field: "" if row.get(field, "") is None else str(row.get(field, "")) for field in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _backup_master_csv(path: PathLike) -> Path:
    source = Path(path)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = source.with_name(f"master_backup_{timestamp}.csv")
    shutil.copy2(source, backup_path)
    return backup_path


def _file_sha256(path: PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_run_datetime(value: Optional[dt.datetime]) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _format_timestamp(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_import_batch(value: dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _clean_cell(value: object) -> str:
    return "" if value is None else str(value).strip()
