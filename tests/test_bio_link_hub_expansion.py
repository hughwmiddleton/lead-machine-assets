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


def test_bio_link_hub_expands_linktree_into_website_candidate(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Example Artist",
            "Email": "",
            "Email_All": "",
            "External Links": "https://linktr.ee/exampleartist",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://linktr.ee/exampleartist": _result(
            "https://linktr.ee/exampleartist",
            html=(
                "<html><body>"
                "<a href='https://open.spotify.com/artist/123'>Spotify</a>"
                "<a href='https://instagram.com/exampleartist'>Instagram</a>"
                "<a href='https://exampleartist.com'>Website</a>"
                "</body></html>"
            ),
        ),
        "https://exampleartist.com": _result(
            "https://exampleartist.com",
            html="<html><body>team@exampleartist.com</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://linktr.ee/exampleartist", "https://exampleartist.com"]
    assert seed_df.at[0, "External Links"] == "https://exampleartist.com, https://linktr.ee/exampleartist"
    assert cde._collect_website_enrich_candidate_urls(seed_df.loc[0]) == ["https://exampleartist.com"]
    assert seed_df.at[0, "Email"] == "team@exampleartist.com"
    assert any("[Web] bio link hub fetched ok=True" in msg for msg in logs)


def test_bio_link_hub_social_and_streaming_links_do_not_become_websites(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Social Only Artist",
            "Email": "",
            "Email_All": "",
            "External Links": "https://beacons.ai/socialonly",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return _result(
            url,
            html=(
                "<html><body>"
                "<a href='https://open.spotify.com/artist/123'>Spotify</a>"
                "<a href='https://soundcloud.com/socialonly'>SoundCloud</a>"
                "<a href='https://instagram.com/socialonly'>Instagram</a>"
                "</body></html>"
            ),
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert calls == ["https://beacons.ai/socialonly"]
    assert seed_df.at[0, "External Links"] == "https://beacons.ai/socialonly"
    assert cde._collect_website_enrich_candidate_urls(seed_df.loc[0]) == []
    assert seed_df.at[0, "Email"] == ""


def test_bio_link_hub_from_website_field_reaches_existing_website_scraper(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Solo Artist",
            "Email": "",
            "Email_All": "",
            "Website": "https://solo.to/soloartist",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    pages = {
        "https://solo.to/soloartist": _result(
            "https://solo.to/soloartist",
            html="<html><body><a href='https://soloartist.com'>Official site</a></body></html>",
        ),
        "https://soloartist.com": _result(
            "https://soloartist.com",
            html="<html><body>hello@soloartist.com</body></html>",
        ),
    }

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is True
    assert calls == ["https://solo.to/soloartist", "https://soloartist.com"]
    assert seed_df.at[0, "External Links"] == "https://soloartist.com"
    assert seed_df.at[0, "Email"] == "hello@soloartist.com"


def test_bio_link_hub_does_not_recurse_into_nested_hubs(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Nested Hub Artist",
            "Email": "",
            "Email_All": "",
            "External Links": "https://linktr.ee/nestedartist",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    def fake_fetch(session, url, *, timeout_s, max_bytes):
        calls.append(url)
        return _result(
            url,
            html=(
                "<html><body>"
                "<a href='https://campsite.bio/nestedartist'>Nested hub</a>"
                "<a href='https://instagram.com/nestedartist'>Instagram</a>"
                "</body></html>"
            ),
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    matched = worker._enrich_row_website_email(seed_df, 0, ctx)

    assert matched is False
    assert calls == ["https://linktr.ee/nestedartist"]
    assert seed_df.at[0, "External Links"] == "https://linktr.ee/nestedartist"
    assert seed_df.at[0, "Email"] == ""
