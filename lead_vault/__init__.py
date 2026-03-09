"""
Lead Vault backend helpers.

Pure utility modules for canonical schema definition and CSV ingestion.
"""

from .alias_map import HEADER_ALIASES, map_headers_to_canonical, normalize_header
from .importer import (
    build_canonical_row,
    ensure_master_csv_exists,
    import_csv_to_canonical_rows,
    read_csv_rows,
)
from .schema import CANONICAL_MASTER_SCHEMA, get_canonical_master_schema, get_default_master_csv_path

__all__ = [
    "CANONICAL_MASTER_SCHEMA",
    "HEADER_ALIASES",
    "build_canonical_row",
    "ensure_master_csv_exists",
    "get_canonical_master_schema",
    "get_default_master_csv_path",
    "import_csv_to_canonical_rows",
    "map_headers_to_canonical",
    "normalize_header",
    "read_csv_rows",
]
