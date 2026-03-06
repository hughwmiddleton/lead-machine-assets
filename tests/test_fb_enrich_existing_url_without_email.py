import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde
import pipeline_runner


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

    def fail_discover(*args, **kwargs):
        raise AssertionError("discovery should not run when a canonical/promotable facebook_url already exists")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fail_discover)
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


def test_fb_explicit_email_discovery_increments_summary(monkeypatch):
    pipeline_runner.reset_email_summary_counts()
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Has FB URL",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/socialfb",
            "Facebook_URL": "https://www.facebook.com/socialfb",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_extract(driver, url, log_fn=None):
        return (["fb@example.com"], "https://www.facebook.com/socialfb/about", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "fb@example.com"
    assert pipeline_runner.get_email_summary_counts()["emails_found"] == 1


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
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    driver = object()
    discover_calls = []
    extract_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((fb_driver, artist_name, location))
        return "https://www.facebook.com/discoveredband"

    def fake_extract(driver_obj, url, log_fn=None):
        extract_calls.append((driver_obj, url))
        return (["fb@example.com"], "https://www.facebook.com/discoveredband/about", "")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, driver, ctx)

    assert matched is True
    assert discover_calls == [(driver, "No FB URL", "")]
    assert extract_calls == [(driver, "https://www.facebook.com/discoveredband")]
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/discoveredband"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/discoveredband"
    assert seed_df.at[0, "Facebook URL"] == "https://www.facebook.com/discoveredband"
    assert any("attempting bounded discovery" in msg.lower() for msg in logs)
    assert any("candidate accepted" in msg.lower() for msg in logs)
    assert any("canonical facebook_url populated via discovery" in msg.lower() for msg in logs)


def test_fb_enrich_discovery_miss_preserves_no_fb_url_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Match",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return ""

    def fail_extract(*args, **kwargs):
        raise AssertionError("_extract_fb_emails_bounded should not run after a discovery miss")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fail_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert discover_calls == [("No Match", "")]
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert any("attempting bounded discovery" in msg.lower() for msg in logs)
    assert any("no safe candidate found" in msg.lower() for msg in logs)


def test_missing_facebook_url_discovery_allowed_once_per_row(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "One Shot",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("extract should not run after discovery miss")),
    )

    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert discover_calls == [("One Shot", "")]
    assert any("already attempted this run" in msg.lower() for msg in logs)


def test_discovery_fail_locks_future_retry_but_preserves_no_fb_url_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Locked Miss",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append(artist_name)
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    first = worker._enrich_row_facebook(seed_df, 0, object(), ctx)
    second = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert first is False
    assert second is False
    assert discover_calls == ["Locked Miss"]
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert any("locking fb discovery for this run" in msg.lower() for msg in logs)
    assert any("already attempted this run" in msg.lower() for msg in logs)


def test_discovery_success_stores_url_and_second_pass_uses_explicit_url_not_discovery(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Discovered Twice",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []
    extract_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return "https://www.facebook.com/discoveredtwice"

    def fake_extract(driver_obj, url, log_fn=None):
        extract_calls.append(url)
        return ([], url, "no_email_on_page")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert discover_calls == [("Discovered Twice", "")]
    assert extract_calls == [
        "https://www.facebook.com/discoveredtwice",
        "https://www.facebook.com/discoveredtwice",
    ]
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/discoveredtwice"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/discoveredtwice"


def test_explicit_facebook_url_bypasses_discovery_lock(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._fb_discovery_attempted_rows.add(0)
    seed_df = _seed_df(
        {
            "Artist Name": "Explicit Wins",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/explicitwins",
            "Facebook_URL": "https://www.facebook.com/explicitwins",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []
    extract_calls = []

    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: discover_calls.append(args) or "https://www.facebook.com/shouldnotrun",
    )

    def fake_extract(driver_obj, url, log_fn=None):
        extract_calls.append(url)
        return (["fb@example.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert not discover_calls
    assert extract_calls == ["https://www.facebook.com/explicitwins"]
    assert all("already attempted this run" not in msg.lower() for msg in logs)


def test_fb_discovery_lock_does_not_leak_across_worker_runs(monkeypatch):
    seed_one = _seed_df(
        {
            "Artist Name": "Fresh Worker",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    seed_two = seed_one.copy()
    logs_one = []
    logs_two = []
    worker_one = _make_worker(logs_one)
    worker_two = _make_worker(logs_two)
    ctx_one = worker_one._build_row_context(seed_one, 0, 1, 1)
    ctx_two = worker_two._build_row_context(seed_two, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, id(logger)))
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    assert worker_one._enrich_row_facebook(seed_one, 0, object(), ctx_one) is False
    assert worker_two._enrich_row_facebook(seed_two, 0, object(), ctx_two) is False
    assert len(discover_calls) == 2
    assert worker_one._fb_discovery_attempted_rows == {0}
    assert worker_two._fb_discovery_attempted_rows == {0}


def test_fb_enrich_rejects_invalid_discovered_candidate(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Invalid Candidate",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_find_best_page(artist_name, location, fb_client, logger):
        return "https://www.facebook.com/share.php?u=bad"

    def fail_extract(*args, **kwargs):
        raise AssertionError("_extract_fb_emails_bounded should not run for an invalid discovered candidate")

    monkeypatch.setattr(cde, "facebook_find_best_page", fake_find_best_page)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fail_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert seed_df.at[0, "facebook_url"] == ""
    assert seed_df.at[0, "Facebook_URL"] == ""
    assert any("no safe candidate found" in msg.lower() for msg in logs)


def test_cross_directory_promotion_backfills_canonical_from_lower_alias():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Lower Alias",
                "facebook_url": "https://www.facebook.com/existinglower",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = cde._apply_fb_promotion_df(df.copy())

    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/existinglower"
