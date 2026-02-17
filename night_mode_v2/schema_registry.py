from typing import List, Set, Tuple

import pandas as pd

REQUIRED_COLUMNS = {
    "seed": {"Artist Name"},
    "pre_fb": {"Artist Name"},
    "post_fb": {"Artist Name"},
    "export": {"Artist Name", "Primary Email", "All Emails"},
}


def validate_schema(df: pd.DataFrame, phase: str) -> Tuple[bool, List[str], List[str]]:
    if phase not in REQUIRED_COLUMNS:
        raise ValueError(f"Unknown phase: {phase}")

    required: Set[str] = REQUIRED_COLUMNS[phase]
    df_cols = set(df.columns)

    missing = sorted(required - df_cols)
    extra = sorted(df_cols - required)
    is_valid = len(missing) == 0

    return is_valid, missing, extra
