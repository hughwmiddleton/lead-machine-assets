import csv
import datetime as dt
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import urlsplit, urlunsplit

from email_normalizer import normalize_email_value

from .alias_map import map_headers_to_canonical
from .importer import build_canonical_row, ensure_master_csv_exists, read_csv_rows
from .schema import get_canonical_master_schema, get_default_master_csv_path

PathLike = Union[str, Path]

_LIST_LIKE_FIELDS = {"All_Emails", "Social Link", "External_Links", "Review_Urls"}
_DUPLICATE_STRATEGIES = {"update", "skip", "keep_both"}
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
    return new_row


def _merge_existing_row(
    existing_row: Dict[str, str],
    incoming_row: Dict[str, str],
    source_basename: str,
    timestamp: str,
) -> Tuple[Dict[str, str], bool]:
    merged = {field: _clean_cell(existing_row.get(field, "")) for field in get_canonical_master_schema()}
    changed = False

    for field_name in get_canonical_master_schema():
        existing_value = merged[field_name]
        incoming_value = _clean_cell(incoming_row.get(field_name, ""))

        if field_name in {"Import_Source_File", "Import_Batch", "Date_Added", "Last_Updated"}:
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
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
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
