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


def _result(url, *, html="", final_url="", content_type="text/html; charset=utf-8", is_html=True, status=200):
    return cde.WebsiteFetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        content_type=content_type,
        html=html,
        is_html=is_html,
    )


def test_website_email_homepage_contact_mailto(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Web Artist",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            "Spotify_Website_URL": "https://artist.test",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html="<html><body><a href='/contact'>Contact</a></body></html>",
        ),
        "https://artist.test/contact": _result(
            "https://artist.test/contact",
            html="<html><body><a href='mailto:bookings@artist.test?subject=hi'>Email</a></body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://artist.test", "https://artist.test/contact"]
    assert seed_df.at[0, "Email"] == "bookings@artist.test"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.test"
    assert seed_df.at[0, "Email_Source_URL"] == "https://artist.test/contact"
    assert seed_df.at[0, "Email_Source_Type"] == "website_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "mailto"
    assert seed_df.at[0, "Email_Type"] == "website_enrich"
    assert "artist.test" in worker._domain_email_reuse_index
    assert any("[Web] homepage fetched ok=True" in msg for msg in logs)
    assert any("[Web] emails_found=1 pages_fetched=2" in msg for msg in logs)


def test_website_email_same_domain_only(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Same Domain Artist",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://artist.test",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html=(
                "<html><body>"
                "<a href='/contact'>Contact</a>"
                "<a href='https://external.test/contact'>Contact external</a>"
                "</body></html>"
            ),
        ),
        "https://artist.test/contact": _result(
            "https://artist.test/contact",
            html="<html><body>team@artist.test</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://artist.test", "https://artist.test/contact"]
    assert "https://external.test/contact" not in calls
    assert seed_df.at[0, "Email"] == "team@artist.test"


def test_website_email_respects_max_page_cap(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Cap Artist",
            "Email": "",
            "Email_All": "",
            "Spotify_Website_URL": "https://artist.test",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html=(
                "<html><body>"
                "<a href='/contact'>Contact</a>"
                "<a href='/about'>About</a>"
                "</body></html>"
            ),
        ),
        "https://artist.test/contact": _result(
            "https://artist.test/contact",
            html="<html><body>No email</body></html>",
        ),
        "https://artist.test/about": _result(
            "https://artist.test/about",
            html="<html><body>No email here either</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_MAX_PAGES", 2)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert len(calls) <= 2
    assert calls[0] == "https://artist.test"
    assert calls[1] in {"https://artist.test/contact", "https://artist.test/about"}
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""


def test_website_email_non_html_counts_toward_cap(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "PDF Artist",
            "Email": "",
            "Email_All": "",
            "Spotify_Website_URL": "https://artist.test",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html="<html><body><a href='/contact'>Contact</a></body></html>",
        ),
        "https://artist.test/contact": _result(
            "https://artist.test/contact",
            html="",
            content_type="application/pdf",
            is_html=False,
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_MAX_PAGES", 2)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert calls == ["https://artist.test", "https://artist.test/contact"]
    assert len(calls) == 2
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert any("[Web] emails_found=0 pages_fetched=2" in msg for msg in logs)


def test_website_email_skips_when_canonical_email_exists(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Existing Email Artist",
            "Email": "",
            "Email_All": "existing@artist.test",
            "Spotify_Website_URL": "https://artist.test",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fail_fetch(*args, **kwargs):
        raise AssertionError("website fetch should not run when canonical email already exists")

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fail_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email_All"] == "existing@artist.test"
