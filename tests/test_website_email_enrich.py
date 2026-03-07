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


def test_website_email_bandcamp_url_only_extracts_visible_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Bandcamp Website Artist",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            "Spotify_Website_URL": "",
            "Bandcamp_URL": "https://artist.bandcamp.com",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://artist.bandcamp.com": _result(
            "https://artist.bandcamp.com",
            html="<html><body>Bookings: bookings@label.test</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://artist.bandcamp.com"]
    assert seed_df.at[0, "Email"] == "bookings@label.test"
    assert seed_df.at[0, "Email_All"] == "bookings@label.test"
    assert seed_df.at[0, "Email_Source_URL"] == "https://artist.bandcamp.com"
    assert seed_df.at[0, "Email_Source_Type"] == "website_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert worker._website_email_cache["artist.bandcamp.com"]["emails"] == ["bookings@label.test"]


def test_website_email_external_links_only_extracts_mailto(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "External Website Artist",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            "Spotify_Website_URL": "",
            "External Links": "https://instagram.com/extartist | https://artist.test",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html="<html><body><a href='mailto:hello@artist.test?subject=hi'>Email</a></body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://artist.test"]
    assert seed_df.at[0, "Email"] == "hello@artist.test"
    assert seed_df.at[0, "Email_All"] == "hello@artist.test"
    assert seed_df.at[0, "Email_Source_URL"] == "https://artist.test"
    assert seed_df.at[0, "Email_Source_Type"] == "website_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "mailto"


def test_website_email_contact_follow_up_caps_requests_at_two(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Capped Contact Artist",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            "Spotify_Website_URL": "",
            "External Links": "https://artist.test",
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
            html="<html><body>Bookings: bookings@artist.test</body></html>",
        ),
        "https://artist.test/about": _result(
            "https://artist.test/about",
            html="<html><body>No email</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_MAX_PAGES", 2)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://artist.test", "https://artist.test/contact"]
    assert len(calls) == 2
    assert seed_df.at[0, "Email"] == "bookings@artist.test"
    assert any("[Web] emails_found=1 pages_fetched=2" in msg for msg in logs)


def test_website_email_cached_positive_reuses_without_refetch(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Cache Source Artist",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            "Bandcamp_URL": "https://artist.test",
        }
    )
    seed_df = pd.concat(
        [
            seed_df,
            _seed_df(
                {
                    "Artist Name": "Cache Target Artist",
                    "Email": "",
                    "Email_All": "",
                    "Email_Source_URL": "",
                    "Email_Source_Type": "",
                    "Email_Extract_Method": "",
                    "Email_Type": "",
                    "External Links": "https://artist.test",
                }
            ),
        ],
        ignore_index=True,
    ).fillna("")
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html="<html><body>Bookings: bookings@label.test</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    ctx_one = worker._build_row_context(seed_df, 0, 1, 2)
    ctx_two = worker._build_row_context(seed_df, 1, 2, 2)

    matched_one = worker._enrich_row_website_email(seed_df, 0, ctx_one)
    matched_two = worker._enrich_row_website_email(seed_df, 1, ctx_two)

    assert matched_one is True
    assert matched_two is True
    assert calls == ["https://artist.test"]
    assert seed_df.at[1, "Email"] == "bookings@label.test"
    assert seed_df.at[1, "Email_Source_Type"] == "website_enrich"
    assert seed_df.at[1, "Email_Extract_Method"] == "regex"


def test_website_email_cached_miss_skips_refetch(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.concat(
        [
            _seed_df(
                {
                    "Artist Name": "Cache Miss One",
                    "Email": "",
                    "Email_All": "",
                    "Spotify_Website_URL": "https://artist.test",
                }
            ),
            _seed_df(
                {
                    "Artist Name": "Cache Miss Two",
                    "Email": "",
                    "Email_All": "",
                    "External Links": "https://artist.test",
                }
            ),
        ],
        ignore_index=True,
    ).fillna("")
    calls = []

    pages = {
        "https://artist.test": _result(
            "https://artist.test",
            html="<html><body><a href='/contact'>Contact</a></body></html>",
        ),
        "https://artist.test/contact": _result(
            "https://artist.test/contact",
            html="<html><body>No email here</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_MAX_PAGES", 2)

    ctx_one = worker._build_row_context(seed_df, 0, 1, 2)
    ctx_two = worker._build_row_context(seed_df, 1, 2, 2)

    matched_one = worker._enrich_row_website_email(seed_df, 0, ctx_one)
    matched_two = worker._enrich_row_website_email(seed_df, 1, ctx_two)

    assert matched_one is False
    assert matched_two is False
    assert calls == ["https://artist.test", "https://artist.test/contact"]
    assert worker._website_email_cache["artist.test"]["status"] == "miss"
    assert seed_df.at[1, "Email"] == ""
    assert any("cache hit domain=artist.test status=miss" in msg for msg in logs)


def test_website_email_shallow_sweep_finds_press_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Press Artist",
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
            html="<html><body>No email here</body></html>",
        ),
        "https://artist.test/press": _result(
            "https://artist.test/press",
            html="<html><body>Press: press@artist.test</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_MAX_PAGES", 3)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == [
        "https://artist.test",
        "https://artist.test/contact",
        "https://artist.test/press",
    ]
    assert seed_df.at[0, "Email"] == "press@artist.test"
    assert seed_df.at[0, "Email_All"] == "press@artist.test"
    assert seed_df.at[0, "Email_Source_URL"] == "https://artist.test/press"
    assert seed_df.at[0, "Email_Source_Type"] == "website_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "website_enrich"
    assert worker._domain_email_reuse_index["artist.test"]["email"] == "press@artist.test"
    assert any("[Web] shallow sweep paths_considered=" in msg for msg in logs)
    assert any("[Web] shallow sweep matched path=/press" in msg for msg in logs)


def test_website_email_shallow_sweep_respects_max_page_cap(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Shallow Cap Artist",
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
            html="<html><body>No email here</body></html>",
        ),
        "https://artist.test/contact": _result(
            "https://artist.test/contact",
            html="<html><body>No email</body></html>",
        ),
        "https://artist.test/press": _result(
            "https://artist.test/press",
            html="<html><body>No email</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_MAX_PAGES", 3)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert calls == [
        "https://artist.test",
        "https://artist.test/contact",
        "https://artist.test/press",
    ]
    assert len(calls) <= 3
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""


def test_website_email_shallow_non_html_counts_toward_cap(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Shallow PDF Artist",
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
            html="<html><body>No email here</body></html>",
        ),
        "https://artist.test/epk": _result(
            "https://artist.test/epk",
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
    monkeypatch.setattr(cde, "WEBSITE_EMAIL_SHALLOW_PATHS", ("/epk",))

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert calls == ["https://artist.test", "https://artist.test/epk"]
    assert len(calls) == 2
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert any("[Web] shallow sweep fetched=1 emails_found=0" in msg for msg in logs)
