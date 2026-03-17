from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import cross_directory_enricher as cde


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
