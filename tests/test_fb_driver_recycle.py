from types import SimpleNamespace

import pandas as pd

import pipeline_runner
from fb_attribution import FB_ATTEMPT_STATE_COL, FB_GATE_STATE_COL, FB_OPPORTUNITY_STATE_COL, FB_WRITE_STATE_COL


class _RecycleDriver:
    def __init__(self):
        self.loaded_urls = []

    def get(self, url):  # noqa: ANN001
        self.loaded_urls.append(url)


class _RecycleHelper:
    def __init__(self, harness):
        self.harness = harness
        self.driver = _RecycleDriver()
        self.rows = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        self.closed = True

    def get_session_failure(self):
        return False, ""

    def get_pass_a_counts(self):
        return {}

    def get_email_stats(self):
        return {}

    def enrich_row_with_facebook_night(self, row, row_index=0):
        self.rows.append({"row": dict(row or {}), "row_index": row_index})
        status = self.harness.next_status()
        extra = {}
        if isinstance(status, dict):
            extra = dict(status)
            status = extra.pop("FB_Status", extra.pop("status", "ok"))
        result = {
            "FB_Status": status,
            "Facebook_URL": row.get("Facebook_URL") or f"https://facebook.com/row{row_index}",
        }
        result.update(extra)
        if status == "ok":
            result.update(
                {
                    "Email": f"row{row_index}@example.com",
                    "Email_All": f"row{row_index}@example.com",
                    "Email_Type": "fb_night",
                }
            )
        return result


class _RecycleHarness:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.helpers = []

    def __call__(self, *args, **kwargs):
        helper = _RecycleHelper(self)
        self.helpers.append(helper)
        return helper

    def next_status(self):
        status = self.statuses[self.calls] if self.calls < len(self.statuses) else "ok"
        self.calls += 1
        return status

    @property
    def attempted_rows(self):
        return [entry["row_index"] for helper in self.helpers for entry in helper.rows]


def _dummy_module():
    return SimpleNamespace(scrape_csv=lambda *args, **kwargs: None)


def _eligible_rows(count):
    return [
        {
            "Artist Name": f"Artist {idx}",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": f"https://facebook.com/artist{idx}",
        }
        for idx in range(count)
    ]


def _run_fb_pass(monkeypatch, tmp_path, rows, statuses, *, reset_interval="50"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "fb_state.json"
    pd.DataFrame(rows).to_csv(input_csv, index=False)

    harness = _RecycleHarness(statuses)
    logs = []

    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")
    monkeypatch.setenv("FB_DRIVER_RESET_INTERVAL", str(reset_interval))
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", harness)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", _dummy_module)
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pipeline_runner,
        "_build_night_fb_share_promotion_resolver",
        lambda **kwargs: (lambda url: ""),
    )

    pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        max_rows_per_run=len(rows),
        per_row_delay_range=(0.0, 0.0),
        short_break_every=0,
        long_break_every=0,
        skip_rows_with_email=True,
        logger=logs.append,
    )

    df_out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
    return harness, logs, df_out


def test_fb_driver_recycles_once_at_row_interval_and_counter_restarts(monkeypatch, tmp_path):
    harness, logs, df_out = _run_fb_pass(
        monkeypatch,
        tmp_path,
        _eligible_rows(51),
        ["ok"] * 51,
        reset_interval="50",
    )

    assert len(harness.helpers) == 2
    assert harness.calls == 51
    assert harness.attempted_rows == list(range(51))
    assert list(df_out["Artist Name"]) == [f"Artist {idx}" for idx in range(51)]
    assert sum("[FB Driver] reset_trigger=row_interval count=50" in msg for msg in logs) == 1
    assert any("[FB Driver] reset_completed next_row=50" in msg for msg in logs)
    assert logs.count("[FB Driver] attempt_counter=1") >= 2


def test_fb_driver_recycles_on_failure_spike_after_full_window(monkeypatch, tmp_path):
    harness, logs, _df_out = _run_fb_pass(
        monkeypatch,
        tmp_path,
        _eligible_rows(11),
        ["driver_error"] * 5 + ["ok"] * 6,
        reset_interval="50",
    )

    assert len(harness.helpers) == 2
    assert harness.attempted_rows == list(range(11))
    assert any("[FB Driver] reset_trigger=failure_spike window=10 failures=5" in msg for msg in logs)
    assert not any("[FB Driver] reset_trigger=row_interval" in msg for msg in logs)
    assert any("[FB Driver] reset_completed next_row=10" in msg for msg in logs)


def test_fb_driver_does_not_recycle_below_threshold_or_before_full_window(monkeypatch, tmp_path):
    harness, logs, _df_out = _run_fb_pass(
        monkeypatch,
        tmp_path,
        _eligible_rows(9),
        ["driver_error"] * 4 + ["ok"] * 5,
        reset_interval="50",
    )

    assert len(harness.helpers) == 1
    assert harness.attempted_rows == list(range(9))
    assert not any("[FB Driver] reset_trigger=" in msg for msg in logs)


def test_fb_driver_recycle_counts_only_real_fb_executions(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": "Attempt One",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://facebook.com/attemptone",
        },
        {
            "Artist Name": "Skipped No Identity",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Social Link": "",
        },
        {
            "Artist Name": "Attempt Two",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://facebook.com/attempttwo",
        },
    ]

    harness, logs, df_out = _run_fb_pass(monkeypatch, tmp_path, rows, ["ok", "ok"], reset_interval="2")

    assert harness.attempted_rows == [0, 2]
    assert len(harness.helpers) == 2
    assert list(df_out["Artist Name"]) == ["Attempt One", "Skipped No Identity", "Attempt Two"]
    assert any("[FB Driver] reset_trigger=row_interval count=2" in msg for msg in logs)
    assert any("[FB Driver] reset_completed next_row=3" in msg for msg in logs)


def test_fb_driver_recycle_does_not_count_helper_not_attempted_rows(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": "Unearthed No URL",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Source Directory": "Unearthed",
        }
        for _idx in range(3)
    ]

    harness, logs, _df_out = _run_fb_pass(
        monkeypatch,
        tmp_path,
        rows,
        [
            {
                "FB_Status": "no_canonical_fb_url",
                FB_ATTEMPT_STATE_COL: "fb_not_attempted",
            }
        ]
        * 3,
        reset_interval="1",
    )

    assert harness.attempted_rows == [0, 1, 2]
    assert not any("[FB Driver] attempt_counter=" in msg for msg in logs)
    assert not any("[FB Driver] reset_trigger=" in msg for msg in logs)


def test_fb_driver_recycle_mixed_rows_counts_executed_only(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": "Executed A",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://facebook.com/executeda",
        },
        {
            "Artist Name": "Not Executed",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Source Directory": "Unearthed",
        },
        {
            "Artist Name": "Executed B",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://facebook.com/executedb",
        },
    ]

    harness, logs, _df_out = _run_fb_pass(
        monkeypatch,
        tmp_path,
        rows,
        [
            {"FB_Status": "ok", "__fb_driver_execution_entered": "1"},
            {"FB_Status": "no_canonical_fb_url", FB_ATTEMPT_STATE_COL: "fb_not_attempted"},
            {"FB_Status": "ok", "__fb_driver_execution_entered": "1"},
        ],
        reset_interval="2",
    )

    assert harness.attempted_rows == [0, 1, 2]
    assert sum("[FB Driver] attempt_counter=" in msg for msg in logs) == 2
    assert any("[FB Driver] reset_trigger=row_interval count=2" in msg for msg in logs)


def test_fb_driver_recycle_does_not_count_share_resolution_failure(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": "Share Failed",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://www.facebook.com/share/abc123",
            FB_GATE_STATE_COL: "fb_share_resolution_failed",
        }
    ]

    harness, logs, _df_out = _run_fb_pass(monkeypatch, tmp_path, rows, [], reset_interval="1")

    assert harness.attempted_rows == []
    assert not any("[FB Driver] attempt_counter=" in msg for msg in logs)
    assert not any("[FB Driver] reset_trigger=" in msg for msg in logs)


def test_fb_driver_recycle_does_not_count_existing_email_skip(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": "Already Has Email",
            "Email": "existing@example.com",
            "Email_All": "existing@example.com",
            "Facebook_URL": "https://facebook.com/alreadyhasemail",
            "Email_Provenance_JSON": (
                '{"existing@example.com":{"extract_method":"regex",'
                '"source_type":"facebook_enrich",'
                '"source_url":"https://facebook.com/alreadyhasemail",'
                '"surface":"about"}}'
            ),
        }
    ]

    harness, logs, _df_out = _run_fb_pass(monkeypatch, tmp_path, rows, [], reset_interval="1")

    assert harness.attempted_rows == []
    assert not any("[FB Driver] attempt_counter=" in msg for msg in logs)
    assert not any("[FB Driver] reset_trigger=" in msg for msg in logs)


def test_fb_driver_recycle_no_execution_rows_do_not_trigger_reset(monkeypatch, tmp_path):
    rows = [
        {
            "Artist Name": f"No URL {idx}",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Source Directory": "Unearthed",
        }
        for idx in range(50)
    ]

    harness, logs, _df_out = _run_fb_pass(
        monkeypatch,
        tmp_path,
        rows,
        [
            {
                "FB_Status": "no_canonical_fb_url",
                FB_ATTEMPT_STATE_COL: "fb_not_attempted",
            }
        ]
        * 50,
        reset_interval="1",
    )

    assert harness.attempted_rows == list(range(50))
    assert len(harness.helpers) == 1
    assert not any("[FB Driver] attempt_counter=" in msg for msg in logs)
    assert not any("[FB Driver] reset_trigger=" in msg for msg in logs)


def test_fb_driver_recycle_does_not_change_fb_attribution_states(monkeypatch, tmp_path):
    rows = _eligible_rows(3)

    _base_harness, _base_logs, base_df = _run_fb_pass(
        monkeypatch,
        tmp_path / "base",
        rows,
        ["ok", "ok", "ok"],
        reset_interval="0",
    )
    recycle_harness, _recycle_logs, recycle_df = _run_fb_pass(
        monkeypatch,
        tmp_path / "recycle",
        rows,
        ["ok", "ok", "ok"],
        reset_interval="1",
    )

    assert recycle_harness.attempted_rows == [0, 1, 2]
    assert list(recycle_df["Artist Name"]) == list(base_df["Artist Name"])
    assert list(recycle_df[FB_OPPORTUNITY_STATE_COL]) == list(base_df[FB_OPPORTUNITY_STATE_COL])
    assert list(recycle_df[FB_ATTEMPT_STATE_COL]) == list(base_df[FB_ATTEMPT_STATE_COL])
    assert list(recycle_df[FB_WRITE_STATE_COL]) == list(base_df[FB_WRITE_STATE_COL])
