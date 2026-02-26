import logging
from pathlib import Path

import pandas as pd

import night_mode_runner
import pipeline_runner


def test_merge_raw_master_counts_missing_provenance(tmp_path: Path) -> None:
    run_dir = tmp_path
    job_csv = run_dir / "job_a_raw.csv"
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Test Artist",
                "Email": "a@example.com",
                "Email_Source_URL": "",
                "Source Directory": "spotify",
                "Source URL": "https://open.spotify.com/artist/abc",
            }
        ]
    )
    df.to_csv(job_csv, index=False)
    stats = night_mode_runner.SmokeStats()
    logger = logging.getLogger("test_provenance")
    result = night_mode_runner._merge_raw_master(
        run_dir.as_posix(), [{"job_id": "job_a", "raw_csv": job_csv.as_posix()}], logger, stats=stats
    )

    assert result is not None
    assert stats.emails_total == 1
    assert stats.emails_missing_source_url == 1


def test_export_columns_include_provenance() -> None:
    for cols in (
        pipeline_runner.DEFAULT_EXPORT_COLUMNS,
        pipeline_runner.FINAL_EXPORT_COLUMNS,
        pipeline_runner.WOODPECKER_EXPORT_COLUMNS,
    ):
        assert "Email_Source_URL" in cols
        assert "Email_Source_Type" in cols
        assert "Email_Extract_Method" in cols
