import logging
from typing import Dict, Iterable, Mapping, MutableMapping, Optional

ORIGIN_LOCKED_FIELDS = {"Source_Directory", "Lead_Source"}
LEGACY_SOURCE_DIRECTORY_FIELD = "Source Directory"


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
        if key in ORIGIN_LOCKED_FIELDS:
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
    existing_lead_source = _clean(existing_row.get("Lead_Source"))
    incoming_lead_source = _clean(incoming_row.get("Lead_Source"))

    if not existing_lead_source and incoming_lead_source:
        existing_row["Lead_Source"] = incoming_lead_source
        existing_lead_source = incoming_lead_source
    elif (
        existing_lead_source
        and incoming_lead_source
        and existing_lead_source != incoming_lead_source
        and logger is not None
    ):
        logger.error(
            "[Origin] Lead_Source conflict kept existing: existing=%s incoming=%s",
            existing_lead_source,
            incoming_lead_source,
        )

    repair_origin_fields(existing_row, logger=logger)


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
