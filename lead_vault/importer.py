import csv
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from .alias_map import map_headers_to_canonical
from .schema import get_canonical_master_schema, get_default_master_csv_path

PathLike = Union[str, Path]
_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def ensure_master_csv_exists(path: Optional[PathLike] = None) -> Path:
    master_path = Path(path) if path is not None else get_default_master_csv_path()
    master_path.parent.mkdir(parents=True, exist_ok=True)

    if not master_path.exists():
        with open(master_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(get_canonical_master_schema())

    return master_path


def read_csv_rows(path: PathLike) -> Dict[str, object]:
    csv_path = Path(path)
    last_exc: Optional[Exception] = None

    for encoding in _CSV_ENCODINGS:
        try:
            with open(csv_path, "r", newline="", encoding=encoding) as handle:
                reader = csv.DictReader(handle, restval="")
                headers = list(reader.fieldnames or [])
                rows: List[Dict[str, str]] = []
                for raw_row in reader:
                    row: Dict[str, str] = {}
                    for key, value in raw_row.items():
                        if key is None:
                            continue
                        row[str(key)] = "" if value is None else str(value)
                    rows.append(row)
                return {
                    "detected_headers": headers,
                    "rows": rows,
                    "encoding": encoding,
                }
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
        except Exception as exc:
            last_exc = exc
            break

    if last_exc is not None:
        raise last_exc

    return {"detected_headers": [], "rows": [], "encoding": None}


def build_canonical_row(
    raw_row: Dict[str, object],
    header_map: Dict[str, str],
    header_order: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    canonical_row = {field: "" for field in get_canonical_master_schema()}
    ordered_headers = list(header_order) if header_order is not None else list(raw_row.keys())

    for raw_header in ordered_headers:
        canonical_field = header_map.get(raw_header)
        if not canonical_field:
            continue

        raw_value = raw_row.get(raw_header, "")
        value = "" if raw_value is None else str(raw_value)

        # Duplicate aliases are resolved in detected input-header order.
        # The first non-empty mapped value wins and later values never overwrite it.
        if not value:
            continue
        if canonical_row[canonical_field]:
            continue
        canonical_row[canonical_field] = value

    return canonical_row


def import_csv_to_canonical_rows(path: PathLike) -> Dict[str, object]:
    read_result = read_csv_rows(path)
    detected_headers = list(read_result["detected_headers"])
    rows = list(read_result["rows"])
    encoding = read_result["encoding"]
    header_map = map_headers_to_canonical(detected_headers)
    canonical_rows = [build_canonical_row(row, header_map, header_order=detected_headers) for row in rows]
    unmapped_headers = [header for header in detected_headers if header not in header_map]

    return {
        "encoding": encoding,
        "detected_headers": detected_headers,
        "mapped_headers": header_map,
        "unmapped_headers": unmapped_headers,
        "canonical_rows": canonical_rows,
        "row_count": len(canonical_rows),
    }
