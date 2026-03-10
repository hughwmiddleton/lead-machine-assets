import csv
from pathlib import Path
from typing import Callable, Dict, Optional, Union

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
        "Artist",
        "Primary_Email",
        "All_Emails",
        "Location",
        "Country",
        "Primary_Genre",
        "Secondary_Genre",
        "Website",
        "Domain",
        "Spotify_URL",
        "SoundCloud_URL",
        "Instagram_URL",
        "Bandcamp_URL",
        "External_Links",
        "Played_On_Triple_J",
        "Played_On_Unearthed",
        "Industry_Signals",
        "Discovery_Source",
        "Source_Directory",
        "Source_URL",
        "Import_Batch",
        "Date_Added",
        "First_Discovered_Date",
        "Last_Updated",
        "Lead_Status",
        "Outreach_Status",
        "Final_Status",
        "Needs_Review",
        "Review_Reason",
        "Review_Urls",
        "Notes",
    ],
    "field_map": {
        "Artist": "Artist",
        "Primary_Email": "Primary_Email",
        "All_Emails": "All_Emails",
        "Location": "Location",
        "Country": "Country",
        "Primary_Genre": "Primary_Genre",
        "Secondary_Genre": "Secondary_Genre",
        "Website": "Website",
        "Domain": "Domain",
        "Spotify_URL": "Spotify_URL",
        "SoundCloud_URL": "SoundCloud_URL",
        "Instagram_URL": "Instagram_URL",
        "Bandcamp_URL": "Bandcamp_URL",
        "External_Links": "External_Links",
        "Played_On_Triple_J": "Played_On_Triple_J",
        "Played_On_Unearthed": "Played_On_Unearthed",
        "Industry_Signals": "Industry_Signals",
        "Discovery_Source": "Discovery_Source",
        "Source_Directory": "Source_Directory",
        "Source_URL": "Source_URL",
        "Import_Batch": "Import_Batch",
        "Date_Added": "Date_Added",
        "First_Discovered_Date": "First_Discovered_Date",
        "Last_Updated": "Last_Updated",
        "Lead_Status": "Lead_Status",
        "Outreach_Status": "Outreach_Status",
        "Final_Status": "Final_Status",
        "Needs_Review": "Needs_Review",
        "Review_Reason": "Review_Reason",
        "Review_Urls": "Review_Urls",
        "Notes": "Notes",
    },
    "row_filter": None,
    "filename_pattern": "final_export.csv",
}

_ROW_FILTERS: Dict[str, Callable[[Dict[str, str]], bool]] = {
    "has_primary_email": has_primary_email,
}


def export_with_preset(
    preset: dict,
    master_csv_path: PathLike,
    output_path: PathLike,
) -> dict:
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
        with open(export_path, "w", encoding="utf-8-sig", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=headers)
            writer.writeheader()
            for raw_row in reader:
                row = {str(key): "" if value is None else str(value) for key, value in raw_row.items() if key is not None}
                rows_read += 1
                if row_filter is not None and not row_filter(row):
                    continue
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


EXPORT_PRESETS = {
    "woodpecker": WOODPECKER_EXPORT_PRESET,
    "final_export": FINAL_EXPORT_PRESET,
}
