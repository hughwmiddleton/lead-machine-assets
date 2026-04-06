from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import cross_directory_enricher as cde
import pandas as pd


def _build_worker(tmp_path):
    worker = cde.CrossDirectoryEnricherWorker(
        seed_csv_path="",
        output_csv_path=(tmp_path / "bandcamp_enriched.csv").as_posix(),
        enable_live_search=True,
        max_live_searches=5,
    )
    worker.log_message = SimpleNamespace(emit=lambda msg: None)
    return worker


def _bandcamp_search_html(display_name: str, url: str, extra_text: str = "") -> str:
    extra = f'<div class="subhead">{extra_text}</div>' if extra_text else ""
    return (
        "<html><body><ul>"
        '<li class="searchresult">'
        f'<div class="heading">{display_name}</div>'
        f"{extra}"
        f'<a class="itemurl" href="{url}">{url}</a>'
        "</li></ul></body></html>"
    )


def _query_value(url: str) -> str:
    return parse_qs(urlparse(url).query).get("q", [""])[0]


def _prime_live_row(worker, **row_overrides):
    row = {
        "Artist Name": "Nightlight",
        "Source Directory": "bandcamp",
        "Bandcamp_URL": "",
        "SoundCloud Link": "",
        "facebook_url": "",
        "Facebook_URL": "",
        "Facebook URL": "",
        "Spotify_Website_URL": "",
        "External Links": "",
        "Social Link": "",
    }
    row.update(row_overrides)
    worker._live_seed_df = pd.DataFrame([row])
    worker._live_row_idx = 0
    return worker._live_seed_df


def test_build_bandcamp_queries_uses_metadata_first_for_ambiguous_artist():
    queries = cde.build_bandcamp_queries(
        "Binta",
        "Make Up",
        location_hint="Melbourne, Australia",
        primary_genre_hint="Dream Pop",
    )

    assert queries == ['"Binta" "Make Up" "melbourne"', '"Binta" "Make Up"', "Binta"]


def test_build_bandcamp_queries_without_metadata_preserves_existing_behavior():
    queries = cde.build_bandcamp_queries("Charlie Noordewier", "Joy and Despair")

    assert queries == [
        '"Charlie Noordewier" "Joy and Despair"',
        'Charlie Noordewier "Joy and Despair"',
        "Charlie Noordewier",
    ]


def test_live_search_bandcamp_uses_metadata_context_without_extra_queries(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._row_enrichment_state = {"bandcamp": "pending"}
    worker._live_context = {
        "artist": "Nightlight",
        "song_title": "Midnight Run",
        "track": "Midnight Run",
        "location": "Melbourne, Australia",
        "genre": "Dream Pop",
        "spotify_domain": "",
    }

    baseline = cde._bandcamp_confidence(
        "Nightlight",
        "Nightlight Band",
        "https://nightlight.bandcamp.com/",
        song_title="Midnight Run",
    )
    assert baseline < cde.MIN_BC_CONFIDENCE

    search_urls = []

    def fake_http_get(url, label="", count_breaker=False):
        search_urls.append(url)
        return (
            _bandcamp_search_html(
                "Nightlight Band",
                "https://nightlight.bandcamp.com/",
                extra_text="Midnight Run Melbourne Australia Dream Pop",
            ),
            200,
        )

    monkeypatch.setattr(cde, "BC_ENABLE_SEARCH_ENDPOINT", True)
    monkeypatch.setattr(worker, "_bc_should_skip_search", lambda: False)
    monkeypatch.setattr(worker, "_bc_directory_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_bc_gap", lambda: None)
    monkeypatch.setattr(worker, "_bc_http_get", fake_http_get)
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: cde.EnrichmentPayload(
            socials=set(),
            websites={"https://nightlight.example"},
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        ),
    )

    payload = worker._live_search_bandcamp("Nightlight")

    assert payload is not None
    assert payload.source_url == "https://nightlight.bandcamp.com/"
    assert payload.candidate_name == "Nightlight Band"
    assert len(search_urls) == 1
    assert _query_value(search_urls[0]) == '"Nightlight" "Midnight Run" "melbourne"'


def test_live_search_bandcamp_uses_guarded_discover_lookup_when_search_disabled(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._row_enrichment_state = {"bandcamp": "pending"}
    worker._spotify_identity_guard_ctx = {"active": True, "artist": "Nightlight"}
    worker._live_context = {
        "artist": "Nightlight",
        "song_title": "Midnight Run",
        "track": "Midnight Run",
        "location": "Melbourne, Australia",
        "genre": "Dream Pop",
        "spotify_domain": "",
    }
    payload = cde.EnrichmentPayload(
        socials=set(),
        websites={"https://nightlight.example"},
        emails=set(),
        link_hubs=set(),
        source_dir="bandcamp",
        source_url="https://nightlight.bandcamp.com/",
    )
    discover_calls = []

    monkeypatch.setattr(cde, "BC_ENABLE_SEARCH_ENDPOINT", False)
    monkeypatch.setattr(worker, "_bc_should_skip_search", lambda: False)
    monkeypatch.setattr(worker, "_bc_directory_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker,
        "_bc_discover_enrich",
        lambda artist, song_title, location, genre: discover_calls.append((artist, song_title, location, genre)) or (payload, False),
    )
    monkeypatch.setattr(
        worker,
        "_bc_slug_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("guarded discover success should not fall through to slug fallback")),
    )

    result = worker._live_search_bandcamp("Nightlight")

    assert result is payload
    assert discover_calls == [("Nightlight", "Midnight Run", "Melbourne, Australia", "Dream Pop")]
    assert worker._last_bc_row_stats["status"] == "fallback_ok"
    assert worker._last_bc_row_stats["mode"] == "directory_discover"


def test_live_search_bandcamp_keeps_no_match_when_guarded_discover_returns_nothing(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._row_enrichment_state = {"bandcamp": "pending"}
    worker._spotify_identity_guard_ctx = {"active": True, "artist": "Nightlight"}
    worker._live_context = {
        "artist": "Nightlight",
        "song_title": "Midnight Run",
        "track": "Midnight Run",
        "location": "Melbourne, Australia",
        "genre": "Dream Pop",
        "spotify_domain": "",
    }
    discover_calls = []
    slug_calls = []

    monkeypatch.setattr(cde, "BC_ENABLE_SEARCH_ENDPOINT", False)
    monkeypatch.setattr(worker, "_bc_should_skip_search", lambda: False)
    monkeypatch.setattr(worker, "_bc_directory_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker,
        "_bc_discover_enrich",
        lambda *args, **kwargs: discover_calls.append(args) or (None, False),
    )
    monkeypatch.setattr(
        worker,
        "_bc_slug_fallback",
        lambda *args, **kwargs: slug_calls.append(args) or None,
    )

    result = worker._live_search_bandcamp("Nightlight")

    assert result is None
    assert len(discover_calls) == 1
    assert len(slug_calls) == 1
    assert worker._last_bc_row_stats["status"] == "no_match"
    assert worker._last_bc_row_stats["mode"] == "directory_discover"


def test_live_search_bandcamp_does_not_use_discover_without_spotify_guard(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._row_enrichment_state = {"bandcamp": "pending"}
    worker._live_context = {
        "artist": "Nightlight",
        "song_title": "Midnight Run",
        "track": "Midnight Run",
        "location": "Melbourne, Australia",
        "genre": "Dream Pop",
        "spotify_domain": "",
    }

    monkeypatch.setattr(cde, "BC_ENABLE_SEARCH_ENDPOINT", False)
    monkeypatch.setattr(worker, "_bc_should_skip_search", lambda: False)
    monkeypatch.setattr(worker, "_bc_directory_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker,
        "_bc_discover_enrich",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("non-spotify live lookup should not use guarded discover recovery")),
    )
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    result = worker._live_search_bandcamp("Nightlight")

    assert result is None
    assert worker._last_bc_row_stats["status"] == "no_match"
    assert worker._last_bc_row_stats["mode"] == "directory_discover"


def test_live_search_bandcamp_does_not_use_sparse_slug_supplemental_fallback(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._row_enrichment_state = {"bandcamp": "pending"}
    fetch_calls = []

    monkeypatch.setattr(cde, "BC_ENABLE_SEARCH_ENDPOINT", False)
    monkeypatch.setattr(worker, "_bc_should_skip_search", lambda: False)
    monkeypatch.setattr(worker, "_bc_directory_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker,
        "_bc_http_get",
        lambda *args, **kwargs: (
            "<html><head><title>Nightlight</title><meta property='og:title' content='Nightlight Archive'></head></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or cde.EnrichmentPayload(
            socials=set(),
            websites=set(),
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        ),
    )

    result = worker._live_search_bandcamp("Nightlight")

    assert result is None
    assert fetch_calls == []


def test_live_search_bandcamp_guarded_slug_fallback_tries_spotify_suffix_candidates(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._row_enrichment_state = {"bandcamp": "pending"}
    worker._spotify_identity_guard_ctx = {"active": True, "artist": "Nightlight"}
    worker._live_context = {
        "artist": "Nightlight",
        "song_title": "Midnight Run",
        "track": "Midnight Run",
        "location": "Melbourne, Australia",
        "genre": "Dream Pop",
        "spotify_domain": "",
    }
    slug_urls = []
    html = (
        "<html><head><title>Nightlight</title></head>"
        "<body><div itemprop='byArtist'>Nightlight Music</div></body></html>"
    )

    def fake_http_get(url, label="", count_breaker=False):
        slug_urls.append(url)
        if url == "https://nightlight-music.bandcamp.com/":
            return (html, 200)
        return ("", 404)

    monkeypatch.setattr(cde, "BC_ENABLE_SEARCH_ENDPOINT", False)
    monkeypatch.setattr(worker, "_bc_should_skip_search", lambda: False)
    monkeypatch.setattr(worker, "_bc_directory_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_bc_discover_enrich", lambda *args, **kwargs: (None, False))
    monkeypatch.setattr(worker, "_bc_http_get", fake_http_get)
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: cde.EnrichmentPayload(
            socials=set(),
            websites={"https://nightlight.example"},
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        ),
    )

    result = worker._live_search_bandcamp("Nightlight")

    assert result is not None
    assert result.source_url == "https://nightlight-music.bandcamp.com/"
    assert slug_urls == [
        "https://nightlight.bandcamp.com/",
        "https://nightlightmusic.bandcamp.com/",
        "https://nightlight-music.bandcamp.com/",
    ]
    assert worker._last_bc_row_stats["status"] == "fallback_ok"
    assert worker._last_bc_row_stats["mode"] == "fallback_guess"


def test_bc_slug_fallback_non_unearthed_sparse_row_tries_sparse_suffix_candidates(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    _prime_live_row(worker, **{"Social Link": "https://www.instagram.com/nightlightmusic/"})
    slug_urls = []
    html = (
        "<html><head><title>Nightlight</title></head>"
        "<body><div itemprop='byArtist'>Nightlight Music</div></body></html>"
    )

    def fake_http_get(url, label="", count_breaker=False):
        slug_urls.append(url)
        if url == "https://nightlight-music.bandcamp.com/":
            return (html, 200)
        return ("", 404)

    monkeypatch.setattr(worker, "_bc_http_get", fake_http_get)
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: cde.EnrichmentPayload(
            socials=set(),
            websites={"https://nightlight.example"},
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        ),
    )

    result = worker._bc_slug_fallback("Nightlight", "")

    assert result is not None
    assert result.source_url == "https://nightlight-music.bandcamp.com/"
    assert slug_urls == [
        "https://nightlight.bandcamp.com/",
        "https://nightlightmusic.bandcamp.com/",
        "https://nightlight-music.bandcamp.com/",
    ]


def test_bc_slug_fallback_non_unearthed_sparse_row_enforces_slug_cap(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    _prime_live_row(worker, **{"Social Link": "https://www.instagram.com/nightlightmusic/"})
    slug_urls = []

    monkeypatch.setattr(
        worker,
        "_bc_http_get",
        lambda url, label="", count_breaker=False: slug_urls.append(url) or ("", 404),
    )

    result = worker._bc_slug_fallback("Nightlight", "")

    assert result is None
    assert slug_urls == [
        "https://nightlight.bandcamp.com/",
        "https://nightlightmusic.bandcamp.com/",
        "https://nightlight-music.bandcamp.com/",
        "https://nightlightband.bandcamp.com/",
    ]
    assert len(slug_urls) == cde.BC_FALLBACK_MAX_SLUGS


def test_bc_slug_fallback_unearthed_row_does_not_expand_sparse_suffix_candidates(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    _prime_live_row(
        worker,
        **{
            "Source Directory": "Triple J Unearthed",
            "Social Link": "https://www.instagram.com/nightlightmusic/",
        },
    )
    slug_urls = []

    monkeypatch.setattr(
        worker,
        "_bc_http_get",
        lambda url, label="", count_breaker=False: slug_urls.append(url) or ("", 404),
    )

    result = worker._bc_slug_fallback("Nightlight", "")

    assert result is None
    assert slug_urls == ["https://nightlight.bandcamp.com/"]


def test_bc_slug_fallback_strong_identity_row_keeps_base_slug_only(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    _prime_live_row(
        worker,
        **{
            "Social Link": "https://www.instagram.com/nightlightmusic/ | https://www.facebook.com/nightlightmusic/",
            "External Links": "https://nightlight.example",
        },
    )
    slug_urls = []

    monkeypatch.setattr(
        worker,
        "_bc_http_get",
        lambda url, label="", count_breaker=False: slug_urls.append(url) or ("", 404),
    )

    result = worker._bc_slug_fallback("Nightlight", "")

    assert result is None
    assert slug_urls == ["https://nightlight.bandcamp.com/"]


def test_bc_slug_fallback_non_unearthed_sparse_row_rejects_invalid_suffix_candidate(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    _prime_live_row(worker, **{"Social Link": "https://www.instagram.com/nightlightmusic/"})
    slug_urls = []
    fetch_calls = []
    html = "<html><head><title>Nightlight</title><meta property='og:title' content='Nightlight Archive'></head></html>"

    def fake_http_get(url, label="", count_breaker=False):
        slug_urls.append(url)
        if url == "https://nightlight-music.bandcamp.com/":
            return (html, 200)
        return ("", 404)

    monkeypatch.setattr(worker, "_bc_http_get", fake_http_get)
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or cde.EnrichmentPayload(
            socials=set(),
            websites={"https://nightlight.example"},
            emails=set(),
            link_hubs=set(),
            source_dir=source_dir,
            source_url=url,
        ),
    )

    result = worker._bc_slug_fallback("Nightlight", "")

    assert result is None
    assert fetch_calls == []
    assert "https://nightlight-music.bandcamp.com/" in slug_urls


def test_bc_slug_fallback_without_spotify_guard_keeps_base_slug_only(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    slug_urls = []

    monkeypatch.setattr(
        worker,
        "_bc_http_get",
        lambda url, label="", count_breaker=False: slug_urls.append(url) or ("", 404),
    )

    result = worker._bc_slug_fallback("Nightlight", "")

    assert result is None
    assert slug_urls == ["https://nightlight.bandcamp.com/"]
