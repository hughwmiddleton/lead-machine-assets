import csv
from pathlib import Path
from typing import Callable, Dict, Optional, Union

import pandas as pd

from .origin import (
    repair_origin_fields,
    repair_origin_integrity_df,
    validate_origin_integrity_df,
    validate_origin_integrity_rows,
)

PathLike = Union[str, Path]


def has_primary_email(row: Dict[str, str]) -> bool:
    return bool(str(row.get("Primary_Email", "")).strip())


WOODPECKER_EXPORT_PRESET = {
    "name": "woodpecker",
    "headers": [
        "Artist Name",
        "Primary Email",
        "All Emails",
        "Location",
        "Primary Genre",
        "Website",
        "Spotify_URL",
        "SoundCloud_URL",
        "External Links",
        "Discovery Source",
        "Source Directory",
        "Source URL",
        "Played on triple J",
        "Played on Unearthed",
        "Release Date",
        "Date Added",
        "Final_Status",
        "Needs_Review",
        "Review_Urls",
    ],
    "field_map": {
        "Artist Name": "Artist",
        "Primary Email": "Primary_Email",
        "All Emails": "All_Emails",
        "Location": "Location",
        "Primary Genre": "Primary_Genre",
        "Website": "Website",
        "Spotify_URL": "Spotify_URL",
        "SoundCloud_URL": "SoundCloud_URL",
        "External Links": "External_Links",
        "Discovery Source": "Discovery_Source",
        "Source Directory": "Source_Directory",
        "Source URL": "Source_URL",
        "Played on triple J": "Played_On_Triple_J",
        "Played on Unearthed": "Played_On_Unearthed",
        "Release Date": "Release_Date",
        "Date Added": "Date_Added",
        "Final_Status": "Final_Status",
        "Needs_Review": "Needs_Review",
        "Review_Urls": "Review_Urls",
    },
    "row_filter": "has_primary_email",
    "filename_pattern": "woodpecker_export.csv",
}

FINAL_EXPORT_PRESET = {
    "name": "final_export",
    "headers": [
        "Artist Name",
        "Location",
        "Country_Derived",
        "Song Title",
        "Primary Genre",
        "Unearthed_Genre_Raw",
        "Social Link",
        "SoundCloud Link",
        "Spotify_URL",
        "External Links",
        "Primary Email",
        "All Emails",
        "Email Source",
        "Email_Source_URL",
        "Email_Source_Type",
        "Email_Extract_Method",
        "Contact_Mode",
        "Discovery Source",
        "Source Directory",
        "Source URL",
        "Review_Urls",
        "Played on triple J",
        "Played on Unearthed",
        "Release Date",
        "Date Added",
        "final_status",
        "Needs_Review",
        "FB_Review_Reason",
    ],
    "field_map": {},
    "row_filter": None,
    "filename_pattern": "final_export.csv",
    "exporter": "legacy_final_export_bridge",
}

_ROW_FILTERS: Dict[str, Callable[[Dict[str, str]], bool]] = {
    "has_primary_email": has_primary_email,
}


def export_with_preset(
    preset: dict,
    master_csv_path: PathLike,
    output_path: PathLike,
) -> dict:
    custom_exporter = _resolve_exporter(preset.get("exporter"))
    if custom_exporter is not None:
        return custom_exporter(preset, master_csv_path, output_path)

    headers = list(preset["headers"])
    field_map = dict(preset["field_map"])
    row_filter = _resolve_row_filter(preset.get("row_filter"))
    master_path = Path(master_csv_path)
    export_path = Path(output_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)

    rows_read = 0
    rows_exported = 0

    with open(master_path, "r", encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle, restval="")
        staged_rows = []
        for raw_row in reader:
            row = {str(key): "" if value is None else str(value) for key, value in raw_row.items() if key is not None}
            rows_read += 1
            if row_filter is not None and not row_filter(row):
                continue
            repair_origin_fields(row)
            staged_rows.append(row)
        validate_origin_integrity_rows(staged_rows)
        with open(export_path, "w", encoding="utf-8-sig", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=headers)
            writer.writeheader()
            for row in staged_rows:
                export_row = {}
                for header in headers:
                    source_field = field_map.get(header)
                    export_row[header] = row.get(source_field, "") if source_field else ""
                writer.writerow(export_row)
                rows_exported += 1

    return {
        "preset": str(preset["name"]),
        "rows_read": rows_read,
        "rows_exported": rows_exported,
        "rows_skipped": rows_read - rows_exported,
        "output_file": str(export_path),
    }


def _export_legacy_final_export_bridge(
    preset: dict,
    master_csv_path: PathLike,
    output_path: PathLike,
) -> dict:
    from pipeline_runner import _build_final_export_frame, recompute_final_status_post_enrichment

    master_path = Path(master_csv_path)
    export_path = Path(output_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)

    master_df = pd.read_csv(master_path, dtype=str, keep_default_na=False).fillna("")
    rows_read = len(master_df.index)
    master_df = repair_origin_integrity_df(master_df)
    validate_origin_integrity_df(master_df)

    bridge_df = _build_legacy_final_export_bridge_frame(master_df)
    bridge_df = recompute_final_status_post_enrichment(bridge_df, logger=None)
    export_df = _build_final_export_frame(bridge_df)
    export_df = export_df.reindex(columns=list(preset["headers"]), fill_value="")
    export_df.to_csv(export_path, index=False, encoding="utf-8-sig")

    rows_exported = len(export_df.index)
    return {
        "preset": str(preset["name"]),
        "rows_read": rows_read,
        "rows_exported": rows_exported,
        "rows_skipped": rows_read - rows_exported,
        "output_file": str(export_path),
    }


def _build_legacy_final_export_bridge_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()

    _backfill_column(work, "Artist Name", ["Artist"])
    _backfill_column(work, "Email", ["Primary_Email"])
    _backfill_column(work, "Email_All", ["All_Emails"])
    _backfill_column(work, "Country_Derived", ["Country"])
    _backfill_column(work, "Song Title", ["Song_Title"])
    _backfill_column(work, "Primary Genre", ["Primary_Genre"])
    _backfill_column(work, "SoundCloud Link", ["SoundCloud_URL"])
    _backfill_column(work, "External Links", ["External_Links"])
    _backfill_column(work, "Source Directory", ["Source_Directory"])
    _backfill_column(work, "Lead_Source", ["Lead_Source", "Source_Directory", "Source Directory"])
    _backfill_column(work, "Source_Directory", ["Lead_Source", "Source Directory"])
    work = repair_origin_integrity_df(work)
    _backfill_column(work, "Source URL", ["Source_URL"])
    _backfill_column(work, "Played on triple J", ["Played_On_Triple_J"])
    _backfill_column(work, "Played on Unearthed", ["Played_On_Unearthed"])
    _backfill_column(work, "Release Date", ["Release_Date"])
    _backfill_column(work, "Date Added", ["Date_Added"])
    _backfill_column(work, "final_status", ["Final_Status"])
    _backfill_column(work, "FB_Review_Reason", ["Review_Reason"])
    _backfill_column(work, "Spotify Playlist", ["Discovery_Source"])
    _backfill_column(work, "Social Link", ["Facebook_URL", "Instagram_URL", "Twitter_URL", "TikTok_URL"])

    if "FB_Status" not in work.columns:
        work["FB_Status"] = ""
    if "Email_Source_Type" in work.columns:
        fb_status_blank = work["FB_Status"].fillna("").astype(str).str.strip() == ""
        facebook_enriched = work["Email_Source_Type"].fillna("").astype(str).str.lower().eq("facebook_enrich")
        work.loc[fb_status_blank & facebook_enriched, "FB_Status"] = "ok"

    return work


def _backfill_column(df: pd.DataFrame, target: str, sources: list[str]) -> None:
    if target not in df.columns:
        df[target] = ""
    target_series = df[target].fillna("").astype(str)
    source_columns = [column for column in sources if column in df.columns]
    if not source_columns:
        return
    source_frame = df.loc[:, source_columns].fillna("").astype(str).replace("", pd.NA)
    merged_source = source_frame.bfill(axis=1).iloc[:, 0].fillna("")
    df[target] = target_series.where(target_series.str.strip() != "", merged_source)


def _resolve_row_filter(
    row_filter: Optional[Union[str, Callable[[Dict[str, str]], bool]]],
) -> Optional[Callable[[Dict[str, str]], bool]]:
    if row_filter is None:
        return None
    if callable(row_filter):
        return row_filter
    if isinstance(row_filter, str):
        if row_filter not in _ROW_FILTERS:
            raise ValueError(f"Unknown row filter: {row_filter}")
        return _ROW_FILTERS[row_filter]
    return _ROW_FILTERS[str(row_filter)]


def _resolve_exporter(
    exporter: Optional[Union[str, Callable[[dict, PathLike, PathLike], dict]]],
) -> Optional[Callable[[dict, PathLike, PathLike], dict]]:
    if exporter is None:
        return None
    if callable(exporter):
        return exporter
    if str(exporter) == "legacy_final_export_bridge":
        return _export_legacy_final_export_bridge
    raise ValueError(f"Unknown exporter: {exporter}")


EXPORT_PRESETS = {
    "woodpecker": WOODPECKER_EXPORT_PRESET,
    "final_export": FINAL_EXPORT_PRESET,
}
