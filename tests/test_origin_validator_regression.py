import pandas as pd

import origin_validator


def test_run_auto_validate_handles_unsupported_directory_without_name_error(tmp_path):
    input_csv = tmp_path / "input.csv"
    output_csv = tmp_path / "output.csv"
    pd.DataFrame(
        [
            {
                "Artist Name": "Test Artist",
                "Song Title": "Test Song",
                "Source Directory": "mystery_directory",
                "Social Link": "https://example.com/test-artist",
                "final_status": "REVIEW",
                "match_score_overall": 0.7,
            }
        ]
    ).to_csv(input_csv, index=False)

    result_path = origin_validator.run_auto_validate(
        input_csv.as_posix(),
        output_path=output_csv.as_posix(),
    )

    df_out = pd.read_csv(result_path)
    assert df_out.loc[0, "origin_match_flag"] == 0
    assert df_out.loc[0, "origin_match_reason"] == "unsupported_directory"
