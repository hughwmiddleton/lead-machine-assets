import pandas as pd

from night_mode_v2.schema_registry import REQUIRED_COLUMNS, validate_schema


def test_missing_columns_detected():
    df = pd.DataFrame(columns=["Artist Name"])
    is_valid, missing, extra = validate_schema(df, "export")

    assert is_valid is False
    assert set(missing) == {"Primary Email", "All Emails"}
    assert extra == []


def test_valid_schema_passes():
    columns = list(REQUIRED_COLUMNS["export"])
    df = pd.DataFrame(columns=columns)
    is_valid, missing, extra = validate_schema(df, "export")

    assert is_valid is True
    assert missing == []
    assert extra == []
