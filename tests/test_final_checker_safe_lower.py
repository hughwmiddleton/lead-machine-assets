import os

import pandas as pd

import final_checker


def test_final_checker_handles_nan_values(tmp_path) -> None:
    df = pd.DataFrame(
        [
            {"Artist Name": float("nan"), "Song Title": float("nan"), "Email": "test@example.com"},
        ]
    )
    csv_path = tmp_path / "input.csv"
    df.to_csv(csv_path, index=False)

    checked_path = final_checker.run_final_checker(str(csv_path))

    assert os.path.exists(checked_path)
    output = pd.read_csv(checked_path)
    # Should still compute final_status without raising.
    assert "final_status" in output.columns
