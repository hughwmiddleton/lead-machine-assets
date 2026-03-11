import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde
import pipeline_runner


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker.progress = SimpleNamespace(emit=lambda *args, **kwargs: None)
    worker._set_platform_state = lambda *args, **kwargs: None
    worker._platform_attempt_allowed = lambda *args, **kwargs: True
    return worker


def _seed_df(row):
    df = pd.DataFrame([row], dtype=str).fillna("")
    return df


def test_discover_facebook_url_bounded_requires_strong_candidate(monkeypatch):
    calls = []

    class _DummyClient:
        pass

    monkeypatch.setattr(cde, "FacebookSearchClient", lambda driver, logger: _DummyClient())

    def fake_find_best_page(artist_name, location, fb_client, logger, require_strong_candidate=False):
        calls.append(
            {
                "artist_name": artist_name,
                "location": location,
                "require_strong_candidate": require_strong_candidate,
            }
        )
        return "https://www.facebook.com/strongband"

    monkeypatch.setattr(cde, "facebook_find_best_page", fake_find_best_page)

    result = cde._discover_facebook_url_bounded(object(), "The Midnight Echo", "", logger=None)

    assert result == "https://www.facebook.com/strongband"
    assert calls == [
        {
            "artist_name": "The Midnight Echo",
            "location": "",
            "require_strong_candidate": True,
        }
    ]


def test_facebook_candidate_is_strong_accepts_music_category():
    candidate = cde.FbCandidate(
        name="The Midnight Echo",
        url="https://www.facebook.com/themidnightecho",
        category="Musician/Band",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "The Midnight Echo",
        candidate,
        "<html><body><div>Musician/Band</div></body></html>",
        "Musician/Band",
        ["Musician/Band"],
        [],
    )

    assert accepted is True
    assert reason == "music_category"


def test_facebook_candidate_is_strong_rejects_personal_profile_phrase():
    candidate = cde.FbCandidate(
        name="John Smith",
        url="https://www.facebook.com/johnsmith",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "John Smith",
        candidate,
        "<html><body><div>John Smith is on Facebook</div></body></html>",
        "",
        ["John Smith is on Facebook"],
        [],
    )

    assert accepted is False
    assert reason == "personal_profile_phrase"


def test_facebook_candidate_is_strong_rejects_slug_only_weak_match():
    candidate = cde.FbCandidate(
        name="Skyline",
        url="https://www.facebook.com/skyline",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "Skyline",
        candidate,
        "<html><body><div>Welcome to Skyline</div></body></html>",
        "",
        ["Welcome to Skyline"],
        [],
    )

    assert accepted is False
    assert reason == "slug_or_name_only_match"


def test_facebook_candidate_is_strong_rejects_short_name_without_positive_signals():
    candidate = cde.FbCandidate(
        name="Sun",
        url="https://www.facebook.com/sun",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "Sun",
        candidate,
        "<html><body><div>Sun</div></body></html>",
        "",
        ["Sun"],
        [],
    )

    assert accepted is False
    assert reason == "short_name_without_strong_signal"


def test_facebook_candidate_is_strong_accepts_short_name_with_music_platform_link():
    candidate = cde.FbCandidate(
        name="Sun",
        url="https://www.facebook.com/sun",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "Sun",
        candidate,
        '<html><body><a href="https://soundcloud.com/sun">SoundCloud</a></body></html>',
        "",
        ["Listen now"],
        ["https://soundcloud.com/sun"],
    )

    assert accepted is True
    assert reason == "music_platform_link"


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


def test_fb_gate_skips_weak_row_without_terminal_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "DJ",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("facebook discovery should be gated")),
    )
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("facebook extraction should be gated")),
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert "FB_Status" not in seed_df.columns
    assert any("skipping fb heavy enrichment" in msg.lower() for msg in logs)


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


def test_phase_facebook_reuses_indexed_domain_email_without_second_scrape(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Bright One",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
                "facebook_url": "https://www.facebook.com/brightone",
                "Facebook_URL": "https://www.facebook.com/brightone",
                "Facebook URL": "https://www.facebook.com/brightone",
                "Social Link": "",
                "External Links": "",
            },
            {
                "Artist Name": "Bright Two",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
                "facebook_url": "https://www.facebook.com/brighttwo",
                "Facebook_URL": "https://www.facebook.com/brighttwo",
                "Facebook URL": "https://www.facebook.com/brighttwo",
                "Social Link": "",
                "External Links": "",
            },
        ],
        dtype=str,
    ).fillna("")

    extract_calls = []

    def fake_extract(driver_obj, url, log_fn=None):
        extract_calls.append(url)
        if url == "https://www.facebook.com/brightone":
            return (["mgmt@brightmusic.com"], url, "")
        raise AssertionError("second row should reuse indexed domain email before FB scrape")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    worker._phase_facebook(seed_df, object(), total=2)

    assert extract_calls == ["https://www.facebook.com/brightone"]
    assert seed_df.at[1, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[1, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[1, "Email_Type"] == "fb_enrich"
    assert seed_df.at[1, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[1, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[1, "Email_Extract_Method"] == "regex"
    assert len(worker._domain_email_reuse_index) == 1
    assert worker._domain_email_reuse_index["brightmusic.com"]["role"] == "management"
    assert worker._domain_email_reuse_count == 1


def test_domain_email_reuse_only_fills_rows_without_email():
    logs = []
    worker = _make_worker(logs)
    worker._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Already Filled",
            "Email": "existing@brightmusic.com",
            "Email_All": "existing@brightmusic.com",
            "Email_Type": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )

    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    reused = worker._maybe_apply_domain_email_reuse(seed_df, 0, ctx)

    assert reused is False
    assert seed_df.at[0, "Email"] == "existing@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_skips_when_only_email_all_is_populated():
    logs = []
    worker = _make_worker(logs)
    worker._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Email All Only",
            "Email": "",
            "Email_All": "existing@brightmusic.com",
            "Email_Type": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )

    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    reused = worker._maybe_apply_domain_email_reuse(seed_df, 0, ctx)

    assert reused is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagates_to_earlier_blank_same_domain_row():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Blank",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[1, "Email"] == "mgmt@brightmusic.com"
    assert worker._domain_email_reuse_count == 1
    assert worker._yield_tracker.counts["domain_reuse"] == 1


def test_domain_email_reuse_propagation_does_not_overwrite_existing_email():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Existing Email",
                "Email": "existing@brightmusic.com",
                "Email_All": "existing@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == "existing@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagation_skips_email_all_only_rows():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Email All Only",
                "Email": "",
                "Email_All": "existing@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagation_skips_different_domains():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Other Domain",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://othermusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagation_is_idempotent_for_repeated_same_email():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Blank",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "First Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
            {
                "Artist Name": "Repeated Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brighttwo",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/team",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True
    assert worker._domain_email_reuse_count == 1

    assert worker._index_domain_email_reuse_from_row(seed_df, 2, "brightmusic.com") is False

    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert worker._domain_email_reuse_count == 1
    assert worker._yield_tracker.counts["domain_reuse"] == 1


def test_domain_email_reuse_index_is_worker_local():
    logs_one = []
    logs_two = []
    worker_one = _make_worker(logs_one)
    worker_two = _make_worker(logs_two)

    assert worker_one._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com;mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )

    seed_one = _seed_df(
        {
            "Artist Name": "Worker One",
            "Email": "",
            "Email_All": "",
            "Email_Type": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )
    seed_two = seed_one.copy()

    ctx_one = worker_one._build_row_context(seed_one, 0, 1, 1)
    ctx_two = worker_two._build_row_context(seed_two, 0, 1, 1)

    assert worker_one._maybe_apply_domain_email_reuse(seed_one, 0, ctx_one) is True
    assert worker_two._maybe_apply_domain_email_reuse(seed_two, 0, ctx_two) is False
    assert seed_one.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_one.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_two.at[0, "Email"] == ""
    assert seed_two.at[0, "Email_All"] == ""
    assert worker_one._domain_email_reuse_index["brightmusic.com"]["role"] == "management"


def test_seed_directory_email_rows_are_not_indexed_for_domain_reuse():
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Seed Directory Artist",
            "Email": "seed@brightmusic.com",
            "Email_All": "seed@brightmusic.com",
            "Email_Type": "",
            "Email_Source_URL": "https://brightmusic.com/contact",
            "Email_Source_Type": "",
            "Email_Extract_Method": "regex",
            "Email Source": "Seed directory (site/email scrape)",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )

    indexed = worker._index_domain_email_reuse_from_row(
        seed_df,
        0,
        "brightmusic.com",
        source_label="Seed directory (site/email scrape)",
    )

    assert indexed is False
    assert worker._domain_email_reuse_index == {}
    assert worker._domain_profile_index == {}


def test_late_domain_backfill_fills_earlier_empty_rows_without_fetches(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Empty",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Keep Existing Email",
                "Email": "existing@brightmusic.com",
                "Email_All": "existing@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
            {
                "Artist Name": "Keep Existing Email All",
                "Email": "",
                "Email_All": "keep@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/press",
            },
        ],
        dtype=str,
    ).fillna("")
    discovery_df = pd.DataFrame(
        [
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/home",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(discovery_df, 0, "brightmusic.com") is True

    def fail(*args, **kwargs):
        raise AssertionError("late-run backfill should not touch enrichment/fetch paths")

    monkeypatch.setattr(worker, "_enrich_row_sc_live", fail)
    monkeypatch.setattr(worker, "_enrich_row_live_lookup", fail)
    monkeypatch.setattr(worker, "_enrich_row_instagram_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_website_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_facebook", fail)

    stats = worker._run_late_domain_email_backfill(seed_df, total=4)

    assert stats == {
        "rows_scanned": 3,
        "rows_eligible": 1,
        "rows_backfilled": 1,
        "rows_skipped": 2,
    }
    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[1, "Email"] == "existing@brightmusic.com"
    assert seed_df.at[1, "Email_All"] == "existing@brightmusic.com"
    assert seed_df.at[2, "Email"] == ""
    assert seed_df.at[2, "Email_All"] == "keep@brightmusic.com"
    assert worker._domain_email_reuse_count == 1
    assert worker._yield_tracker.counts["domain_reuse"] == 1
    assert any("Late domain reuse backfill" in msg for msg in logs)


def test_late_domain_backfill_applies_best_ranked_contact_and_preserves_all_same_domain_contacts(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Empty",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
        ],
        dtype=str,
    ).fillna("")
    discovery_df = pd.DataFrame(
        [
            {
                "Artist Name": "Generic Discovery",
                "Email": "info@brightmusic.com",
                "Email_All": "info@brightmusic.com",
                "Email_Type": "website_enrich",
                "Email_Source_URL": "https://brightmusic.com/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
            {
                "Artist Name": "Better Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com;info@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/home",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(discovery_df, 0, "brightmusic.com") is True
    assert worker._index_domain_email_reuse_from_row(discovery_df, 1, "brightmusic.com") is True

    def fail(*args, **kwargs):
        raise AssertionError("late-run backfill should not touch enrichment/fetch paths")

    monkeypatch.setattr(worker, "_enrich_row_sc_live", fail)
    monkeypatch.setattr(worker, "_enrich_row_live_lookup", fail)
    monkeypatch.setattr(worker, "_enrich_row_instagram_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_website_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_facebook", fail)

    stats = worker._run_late_domain_email_backfill(seed_df, total=3)

    assert stats == {
        "rows_scanned": 1,
        "rows_eligible": 1,
        "rows_backfilled": 1,
        "rows_skipped": 0,
    }
    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert pipeline_runner.normalize_emails(seed_df.at[0, "Email_All"]) == [
        "info@brightmusic.com",
        "mgmt@brightmusic.com",
    ]
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"


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

    def fake_find_best_page(artist_name, location, fb_client, logger, require_strong_candidate=False):
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
