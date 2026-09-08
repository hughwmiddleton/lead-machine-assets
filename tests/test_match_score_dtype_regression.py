import pandas as pd

from cross_directory_enricher import CrossDirectoryEnricherWorker


def test_match_score_update_keeps_normalized_float64_column_numeric():
    frame = pd.DataFrame({"Match_Score": pd.Series([0.0], dtype="float64")})

    CrossDirectoryEnricherWorker._update_row_match_score(None, frame, 0, 1.0)

    assert frame["Match_Score"].dtype == "float64"
    assert frame.at[0, "Match_Score"] == 1.0
    assert isinstance(frame.at[0, "Match_Score"], float)


def test_match_score_update_preserves_pandas_string_dtype_compatibility():
    frame = pd.DataFrame({"Match_Score": pd.Series(["0.40"], dtype="str")})

    CrossDirectoryEnricherWorker._update_row_match_score(None, frame, 0, 0.98)

    assert isinstance(frame["Match_Score"].dtype, pd.StringDtype)
    assert frame.at[0, "Match_Score"] == "0.98"
