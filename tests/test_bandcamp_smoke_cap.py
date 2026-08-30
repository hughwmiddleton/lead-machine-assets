import importlib.util
from pathlib import Path

import bandcamp_profile_engine as bpe


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bandcamp_smoke_cap_stops_processing_early():
    lm = _load_legacy_module()
    rows_limit = 3  # simulate SMOKE_SEED_CAP applying over default target=20

    candidate_profiles = [
        {
            "profile_url": f"https://example{i}.bandcamp.com/",
            "seed_genre": "",
            "source_tag": "discover",
            "api_location": "",
        }
        for i in range(5)
    ]

    fetch_calls = []

    def fake_fetch(url: str):
        fetch_calls.append(url)
        return "<html></html>"

    def fake_parse(url: str, html: str, seed_genre: str):
        return {
            "profile_url": url,
            "artist_name": url.replace("https://", "").split(".")[0],
            "location": "",
            "latest_release_date": "",
            "socials": {},
        }

    aggregated, stats = lm._bandcamp_process_candidate_profiles(
        candidate_profiles,
        rows_limit,
        requested_label="",
        requested_hint="",
        normalized_mode="discover",
        normalized_search_location="",
        contacts_required=False,
        search_cutoff=None,
        effective_search_domain="",
        driver=None,
        smoke_cap_active=True,
        fetch_html_fn=fake_fetch,
        parse_html_fn=fake_parse,
        quick_visit_fn=lambda driver, url: "",
    )

    # Early stop after cap reached
    assert stats["kept_after_location"] == rows_limit
    assert len(fetch_calls) == rows_limit
    assert stats["selenium_used"] == 0
    assert stats["stop_processing"] is True
    assert len(aggregated) == rows_limit


def test_gui_default_profile_path_uses_shared_engine():
    lm = _load_legacy_module()
    calls = []

    def shared(url, **kwargs):
        calls.append(url)
        return bpe.BandcampProfileResult(
            bpe.PROFILE_ACCEPTED,
            url,
            profile={
                "profile_url": url,
                "artist_name": "Artist A",
                "location": "",
                "latest_release_date": "not present",
                "socials": {},
            },
        )

    aggregated, stats = lm._bandcamp_process_candidate_profiles(
        [{
            "profile_url": "https://artist-a.bandcamp.com/",
            "seed_genre": "",
            "source_tag": "direct",
            "api_location": "",
        }],
        1,
        requested_label="",
        requested_hint="",
        normalized_mode="direct",
        normalized_search_location="",
        contacts_required=False,
        search_cutoff=None,
        effective_search_domain="",
        driver=None,
        smoke_cap_active=True,
        profile_engine_fn=shared,
    )

    assert calls == ["https://artist-a.bandcamp.com/"]
    assert len(aggregated) == 1
    assert stats["http_success"] == 1
