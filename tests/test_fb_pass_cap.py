import os
from types import SimpleNamespace

import pandas as pd
import pipeline_runner


class DummyFBHelper:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_session_failure(self):
        return False, ""

    def get_pass_a_counts(self):
        return {}

    def enrich_row_with_facebook_night(self, row, row_index=0):
        self.calls += 1
        # minimal payload: mark status so run continues
        return {"FB_Status": "ok", "Email": "", "Email_All": "", "Facebook_URL": ""}


def _make_dummy_module():
    return SimpleNamespace(scrape_csv=lambda *args, **kwargs: None)


def test_nightmode_fb_pass_respects_cap(monkeypatch, tmp_path):
    # Prepare input CSV with 5 rows
    rows = [{"Artist Name": f"A{i}", "Email": "", "Email_All": "", "Facebook_URL": ""} for i in range(5)]
    input_csv = tmp_path / "in.csv"
    pd.DataFrame(rows).to_csv(input_csv, index=False)
    output_csv = tmp_path / "out.csv"
    state_path = tmp_path / "state.json"

    # Env: activate cap and fake credentials so path runs
    monkeypatch.setenv("FB_PASS_CAP", "2")
    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")

    dummy_helper = DummyFBHelper()
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: dummy_helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _make_dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)

    status = pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        max_rows_per_run=100,
        skip_rows_with_email=False,
    )

    # Only first 2 rows should have been processed, but all rows are preserved in output.
    assert dummy_helper.calls == 2
    df_out = pd.read_csv(output_csv)
    assert len(df_out.index) == 5
    assert status.processed_rows >= 2
