from collections import Counter
from pathlib import Path
from typing import Dict, Union

from .importer import read_csv_rows

PathLike = Union[str, Path]


def summarize_master_dataset(master_csv_path: PathLike) -> Dict[str, object]:
    read_result = read_csv_rows(master_csv_path)
    rows = list(read_result.get("rows", []))

    sources: Counter[str] = Counter()
    rows_with_email = 0
    needs_review = 0

    for row in rows:
        primary_email = str(row.get("Primary_Email", "") or "")
        if primary_email.strip():
            rows_with_email += 1

        if row.get("Needs_Review", "") == "Yes":
            needs_review += 1

        source_directory = str(row.get("Source_Directory", "") or "").strip()
        if source_directory:
            sources[source_directory] += 1

    return {
        "total_rows": len(rows),
        "rows_with_email": rows_with_email,
        "needs_review": needs_review,
        "sources": dict(sorted(sources.items())),
    }
