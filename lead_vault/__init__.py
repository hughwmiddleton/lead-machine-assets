"""
Lead Vault backend helpers.

Pure utility modules for canonical schema definition and CSV ingestion.
"""

from .alias_map import HEADER_ALIASES, map_headers_to_canonical, normalize_header
from .exporter import WOODPECKER_EXPORT_PRESET, export_with_preset, has_primary_email
from .importer import (
    build_canonical_row,
    ensure_master_csv_exists,
    import_csv_to_canonical_rows,
    read_csv_rows,
)
from .merge import merge_csv_into_master, preview_csv_import
from .schema import CANONICAL_MASTER_SCHEMA, get_canonical_master_schema, get_default_master_csv_path

__all__ = [
    "CANONICAL_MASTER_SCHEMA",
    "HEADER_ALIASES",
    "WOODPECKER_EXPORT_PRESET",
    "build_canonical_row",
    "ensure_master_csv_exists",
    "export_with_preset",
    "get_canonical_master_schema",
    "get_default_master_csv_path",
    "has_primary_email",
    "import_csv_to_canonical_rows",
    "map_headers_to_canonical",
    "merge_csv_into_master",
    "normalize_header",
    "preview_csv_import",
    "read_csv_rows",
]
