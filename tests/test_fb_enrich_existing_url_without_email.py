import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker._set_platform_state = lambda *args, **kwargs: None
    worker._platform_attempt_allowed = lambda *args, **kwargs: True
    return worker


def _seed_df(row):
    df = pd.DataFrame([row], dtype=str).fillna("")
    return df


@pytest.mark.parametrize(
    "row_overrides,expected_call",
    [
        (
            {"facebook_url": "https://www.facebook.com/socialfb", "Social Link": ""},
            "https://www.facebook.com/socialfb",
        ),
        (
            {"facebook_url": "", "Social Link": "facebook.com/socialfb"},
            "https://www.facebook.com/socialfb",
        ),
    ],
)
def test_fb_enrich_runs_when_fb_url_present_or_promotable(monkeypatch, row_overrides, expected_call):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Has FB URL",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    for key, val in row_overrides.items():
        seed_df.at[0, key] = val
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    calls = []

    def fake_extract(driver, url, log_fn=None):
        calls.append(url)
        return (["fb@example.com"], "https://www.facebook.com/socialfb/about", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert calls == [expected_call]
    assert seed_df.at[0, "Email"] == "fb@example.com"
    assert "fb@example.com" in seed_df.at[0, "Email_All"]
    assert seed_df.at[0, "FB_Status"].lower() == "found_email"
    assert not any("already has facebook link" in msg.lower() for msg in logs)


def test_fb_enrich_handles_missing_email_columns(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Missing Email Columns",
                "Social Link": "facebook.com/missingcolumns",
                "External Links": "facebook.com/missingcolumns",
            }
        ],
        dtype=str,
    ).fillna("")
    seed_df = cde._ensure_email_columns(seed_df)
    seed_df = cde._apply_fb_promotion_df(seed_df)
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    calls = []

    def fake_extract(driver, url, log_fn=None):
        calls.append(url)
        return ([], url, "no_email_on_page")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert calls, "FB extraction should be attempted when a promotable link exists"
    assert "Email_All" in seed_df.columns
    assert seed_df.at[0, "Email_All"] == ""


def test_fb_enrich_skips_when_email_present(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Has Email",
            "Email": "existing@example.com",
            "Email_All": "existing@example.com",
            "facebook_url": "https://www.facebook.com/socialfb",
            "Facebook_URL": "https://www.facebook.com/socialfb",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    called = False

    def fake_extract(*args, **kwargs):
        nonlocal called
        called = True
        return (["should-not-run"], "https://www.facebook.com/socialfb", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert called is False
    assert seed_df.at[0, "Email"] == "existing@example.com"
    assert "skip" in " ".join(logs).lower()


def test_fb_enrich_skips_when_fb_url_missing(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No FB URL",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    called = False

    def fake_extract(*args, **kwargs):
        nonlocal called
        called = True
        return ([], "", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert called is False
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert any("no explicit facebook url" in msg.lower() for msg in logs)
