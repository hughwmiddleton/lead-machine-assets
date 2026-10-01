import json

import pandas as pd

import night_mode_runner


def test_run_summary_exposes_facebook_degradation(tmp_path, monkeypatch):
    final_export = tmp_path / "master_export_leads.csv"
    pd.DataFrame([{"Artist Name": "Completed Artist", "Email": "artist@example.com"}]).to_csv(
        final_export,
        index=False,
    )
    (tmp_path / night_mode_runner.FACEBOOK_STATE_FILENAME).write_text(
        json.dumps(
            {
                "fb_availability_state": "unavailable",
                "fb_degradation_reason": "two_factor",
                "fb_degraded_after_row": 20,
                "fb_recovery_attempts": 1,
                "fb_skipped_opportunities": 4,
                "fb_run_completed": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(night_mode_runner, "_preview_lead_vault_counts", lambda _: (0, 0))

    summary = night_mode_runner._build_run_summary(
        tmp_path.as_posix(),
        final_export=final_export.as_posix(),
    )

    assert summary["artists_processed"] == 1
    assert summary["facebook_degradation"] == {
        "state": "unavailable",
        "reason": "two_factor",
        "after_row": 20,
        "recovery_attempts": 1,
        "later_opportunities_skipped": 4,
        "run_completed": True,
    }
