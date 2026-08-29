import logging
from typing import Dict, Iterable, Mapping, MutableMapping, Optional

LEGACY_SOURCE_DIRECTORY_FIELD = "Source Directory"
ORIGIN_DIRECTORY_FIELDS = ("Lead_Source", "Source_Directory", LEGACY_SOURCE_DIRECTORY_FIELD)
ORIGIN_URL_FIELDS = ("Source URL", "Source_URL")
ORIGIN_LOCKED_FIELDS = frozenset((*ORIGIN_DIRECTORY_FIELDS, *ORIGIN_URL_FIELDS))


class OriginIntegrityError(ValueError):
    pass


class OriginLockedRow(dict):
    def __setitem__(self, key, value):
        if key in ORIGIN_LOCKED_FIELDS and key in self and _clean(self.get(key)):
            return
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        updates = dict(*args, **kwargs)
        safe_row_update(self, updates)


def safe_row_update(row: MutableMapping[str, object], updates: Mapping[str, object]) -> MutableMapping[str, object]:
    for key, value in dict(updates).items():
        if key in ORIGIN_LOCKED_FIELDS and _clean(row.get(key)):
            continue
        row[key] = value
    return row


def repair_origin_fields(
    row: MutableMapping[str, object],
    ingest_source: Optional[object] = None,
    *,
    logger: Optional[logging.Logger] = None,
) -> MutableMapping[str, object]:
    source = _clean(ingest_source)
    lead_source = _clean(row.get("Lead_Source"))
    source_directory = _clean(row.get("Source_Directory"))
    legacy_source_directory = _clean(row.get(LEGACY_SOURCE_DIRECTORY_FIELD))

    if not lead_source:
        lead_source = source or source_directory or legacy_source_directory
        if lead_source:
            row["Lead_Source"] = lead_source

    if not source_directory:
        source_directory = source or legacy_source_directory or lead_source
        if source_directory:
            row["Source_Directory"] = source_directory

    if lead_source and source_directory and not _origin_values_compatible(lead_source, source_directory) and logger is not None:
        logger.error(
            "[Origin] Source_Directory mismatch kept: Lead_Source=%s Source_Directory=%s",
            lead_source,
            source_directory,
        )

    if LEGACY_SOURCE_DIRECTORY_FIELD in row and not legacy_source_directory:
        row[LEGACY_SOURCE_DIRECTORY_FIELD] = lead_source or source_directory
    return row


def merge_origin_fields(
    existing_row: MutableMapping[str, object],
    incoming_row: Mapping[str, object],
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    for field_name in ORIGIN_LOCKED_FIELDS:
        existing_value = _clean(existing_row.get(field_name))
        incoming_value = _clean(incoming_row.get(field_name))
        if not existing_value and incoming_value:
            existing_row[field_name] = incoming_value
        elif existing_value and incoming_value and existing_value != incoming_value and logger is not None:
            logger.error(
                "[Origin] %s conflict kept existing: existing=%s incoming=%s",
                field_name,
                existing_value,
                incoming_value,
            )

    repair_origin_fields(existing_row, logger=logger)


def preserve_origin_fields(
    target_row: MutableMapping[str, object],
    canonical_row: Mapping[str, object],
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Restore established discovery origin onto a replacement/enriched row.

    Blank canonical values do not erase legitimate initialization already present on
    the target. Nonblank canonical values always win over secondary-source values.
    """
    canonical_values = {field_name: canonical_row.get(field_name) for field_name in ORIGIN_LOCKED_FIELDS}
    directory_values = [_clean(canonical_values.get(field_name)) for field_name in ORIGIN_DIRECTORY_FIELDS]
    spotify_url = _clean(canonical_row.get("Spotify_URL") or canonical_row.get("Spotify URL"))
    spotify_origin = any(value.casefold() == "spotify" for value in directory_values)
    if spotify_url and (spotify_origin or not any(directory_values)):
        canonical_values["Lead_Source"] = canonical_values.get("Lead_Source") or "Spotify"
        canonical_values["Source_Directory"] = canonical_values.get("Source_Directory") or "Spotify"
        if LEGACY_SOURCE_DIRECTORY_FIELD in canonical_row:
            canonical_values[LEGACY_SOURCE_DIRECTORY_FIELD] = (
                canonical_values.get(LEGACY_SOURCE_DIRECTORY_FIELD) or "Spotify"
            )
        for field_name in ORIGIN_URL_FIELDS:
            if field_name in canonical_row:
                canonical_values[field_name] = canonical_values.get(field_name) or spotify_url

    for field_name in ORIGIN_LOCKED_FIELDS:
        canonical_value = _clean(canonical_values.get(field_name))
        target_value = _clean(target_row.get(field_name))
        if not canonical_value:
            continue
        if target_value and target_value != canonical_value and logger is not None:
            logger.error(
                "[Origin] %s conflict restored canonical: canonical=%s secondary=%s",
                field_name,
                canonical_value,
                target_value,
            )
        target_row[field_name] = canonical_values.get(field_name)
    repair_origin_fields(target_row, logger=logger)


def validate_origin_integrity_rows(rows: Iterable[Mapping[str, object]]) -> None:
    violations = []
    for idx, row in enumerate(rows, start=1):
        lead_source = _clean(row.get("Lead_Source"))
        source_directory = _clean(row.get("Source_Directory"))
        legacy_source_directory = _clean(row.get(LEGACY_SOURCE_DIRECTORY_FIELD))
        if not lead_source:
            violations.append(f"row {idx}: Lead_Source blank")
        if not source_directory:
            violations.append(f"row {idx}: Source_Directory blank")
        if lead_source and source_directory and not _origin_values_compatible(lead_source, source_directory):
            violations.append(
                f"row {idx}: Source_Directory={source_directory!r} does not match Lead_Source={lead_source!r}"
            )
        if legacy_source_directory and lead_source and not _origin_values_compatible(lead_source, legacy_source_directory):
            violations.append(
                f"row {idx}: Source Directory={legacy_source_directory!r} does not match Lead_Source={lead_source!r}"
            )
    if violations:
        raise OriginIntegrityError("; ".join(violations[:10]))


def repair_origin_integrity_df(df, ingest_source: Optional[object] = None):
    if "Lead_Source" not in df.columns:
        df["Lead_Source"] = ""
    if "Source_Directory" not in df.columns:
        df["Source_Directory"] = ""

    for idx in df.index:
        row = {
            "Lead_Source": df.at[idx, "Lead_Source"],
            "Source_Directory": df.at[idx, "Source_Directory"],
        }
        if LEGACY_SOURCE_DIRECTORY_FIELD in df.columns:
            row[LEGACY_SOURCE_DIRECTORY_FIELD] = df.at[idx, LEGACY_SOURCE_DIRECTORY_FIELD]
        repair_origin_fields(row, ingest_source=ingest_source)
        df.at[idx, "Lead_Source"] = row.get("Lead_Source", "")
        df.at[idx, "Source_Directory"] = row.get("Source_Directory", "")
        if LEGACY_SOURCE_DIRECTORY_FIELD in df.columns:
            df.at[idx, LEGACY_SOURCE_DIRECTORY_FIELD] = row.get(LEGACY_SOURCE_DIRECTORY_FIELD, row.get("Lead_Source", ""))
    return df


def validate_origin_integrity_df(df) -> None:
    validate_origin_integrity_rows(df.to_dict(orient="records"))


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _origin_values_compatible(lead_source: str, source_directory: str) -> bool:
    lead = _clean(lead_source).lower()
    directory = _clean(source_directory).lower()
    if not lead or not directory:
        return True
    if lead == directory:
        return True
    return lead == "triple j unearthed" and directory == "unearthed"
