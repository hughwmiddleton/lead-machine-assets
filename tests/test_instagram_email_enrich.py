import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker._set_platform_state = lambda *args, **kwargs: None
    return worker


def _seed_df(row):
    return pd.DataFrame([row], dtype=str).fillna("")


def test_instagram_email_meta_description_writes_email_and_provenance(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    fetch_calls = []

    def fake_fetch(session, url):
        fetch_calls.append(url)
        return (
            "<html><head><meta property='og:description' content='Bookings: bookings@artist.com'></head>"
            "<body><div>Official profile</div></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert fetch_calls == ["https://www.instagram.com/igartist/"]
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/igartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_body_scan_still_writes_email_and_provenance(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    fetch_calls = []

    def fake_fetch(session, url):
        fetch_calls.append(url)
        return ("<html><body>Bookings: bookings@artist.com</body></html>", 200)

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert fetch_calls == ["https://www.instagram.com/igartist/"]
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/igartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


@pytest.mark.parametrize(
    "row",
    [
        {
            "Artist Name": "Has Email",
            "Email": "existing@example.com",
            "Email_All": "existing@example.com",
            "Instagram_URL": "https://www.instagram.com/igartist/",
        },
        {
            "Artist Name": "Bad URL",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/p/abc123/",
        },
    ],
)
def test_instagram_email_negative_skips_without_fetch(monkeypatch, row):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    for key, value in row.items():
        seed_df.at[0, key] = value
    before = seed_df.copy(deep=True)
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.equals(before)
    assert logs == []


def test_instagram_email_no_visible_or_meta_email_keeps_single_fetch(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Email Here",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/noemailhere/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    before = seed_df.copy(deep=True)

    fetch_calls = []

    def fake_fetch(session, url):
        fetch_calls.append(url)
        return (
            "<html><head><meta property='og:description' content='Official profile'>"
            "<meta name='description' content='Music artist'></head><body><a href='https://example.com/contact'>Contact</a>"
            "<a href='https://linktr.ee/noemailhere'>Linktree</a></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert fetch_calls == ["https://www.instagram.com/noemailhere/"]
    assert seed_df.equals(before)
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/noemailhere/",
        "[IG Email] No email found",
    ]
