import json
from types import SimpleNamespace

import pandas as pd
import pytest

import cross_directory_enricher as cde
from musicbrainz_relationship_bridge import build_relationship_bridge_plan


def _evidence(name="Artist A", aliases=(), bandcamp=(), soundcloud=()):
    artist = {"name": name}
    if aliases:
        artist["aliases"] = [{"name": alias} for alias in aliases]
    return json.dumps(
        {
            "spotify": {"artist_id": "spotify-id"},
            "musicbrainz": {
                "status": "matched",
                "match_method": "spotify_url_relationship",
                "artist": artist,
                "relationships": {
                    "bandcamp": [{"url": url, "type": "bandcamp"} for url in bandcamp],
                    "soundcloud": [{"url": url, "type": "soundcloud"} for url in soundcloud],
                    "facebook": [{"url": "https://facebook.com/must-not-promote"}],
                },
            },
        }
    )


def _row(artist="Artist A", **overrides):
    row = {
        "Artist Name": artist,
        "Lead_Source": "Spotify",
        "Source_Directory": "Spotify",
        "Source Directory": "Spotify",
        "Source URL": "https://open.spotify.com/artist/spotify-id",
        "Spotify_URL": "https://open.spotify.com/artist/spotify-id",
        "Spotify_Artist_ID": "spotify-id",
        "MusicBrainz_Status": "matched",
        "Identity_Match_Method": "spotify_url_relationship",
        "Identity_Evidence_JSON": _evidence(),
        "Bandcamp_URL": "",
        "SoundCloud Link": "",
        "Social Link": "",
        "External Links": "",
        "Email": "",
        "final_status": "valid",
        "contact_safety": "safe",
        "match_score_overall": "0.40",
        "directory_conflict_flag": "",
        "name_consistency_flag": "",
    }
    row.update(overrides)
    return row


def _plan(row):
    return build_relationship_bridge_plan(
        row,
        normalize_name=cde.normalise_artist_name,
        canonicalize_bandcamp=cde._canonicalise_musicbrainz_bandcamp_url,
        canonicalize_soundcloud=cde._canonicalise_musicbrainz_soundcloud_url,
        valid_bandcamp=cde._is_valid_unearthed_bandcamp_url,
        valid_soundcloud=lambda value: bool(cde._canonicalise_musicbrainz_soundcloud_url(value)),
    )


@pytest.mark.parametrize("artist", ["Artist A", "  ARTIST A  "])
def test_exact_or_normalized_row_name_allows_candidates(artist):
    row = _row(
        artist,
        Identity_Evidence_JSON=_evidence(
            bandcamp=("https://artist-a.bandcamp.com/album/release",),
            soundcloud=("https://www.soundcloud.com/Artist-A/tracks",),
        ),
    )
    plan = _plan(row)
    assert plan.eligible
    assert plan.bandcamp_urls == ("https://artist-a.bandcamp.com/",)
    assert plan.soundcloud_urls == ("https://soundcloud.com/artist-a",)


def test_exact_musicbrainz_alias_allows_candidates():
    row = _row(
        "Known Alias",
        Identity_Evidence_JSON=_evidence(
            name="Canonical Artist",
            aliases=("Known Alias",),
            bandcamp=("https://canonical.bandcamp.com",),
        ),
    )
    assert _plan(row).reason == "exact_alias"


def test_mayce_macy_kate_mismatch_blocks_promotion_without_mutating_shadow_or_safety():
    row = _row(
        "MAYCE",
        Email="safe@example.com",
        Identity_Evidence_JSON=_evidence(
            name="Macy Kate",
            bandcamp=("https://macykate.bandcamp.com",),
        ),
    )
    before = dict(row)
    plan = _plan(row)
    assert not plan.eligible
    assert plan.reason == "row_musicbrainz_identity_mismatch"
    assert row == before
    assert row["Email"] == "safe@example.com"
    assert row["final_status"] == "valid"


def test_candidates_reject_malformed_and_non_profile_urls_and_collapse_duplicates():
    row = _row(
        Identity_Evidence_JSON=_evidence(
            bandcamp=(
                "https://artist-a.bandcamp.com/album/one",
                "https://artist-a.bandcamp.com/music",
                "https://bandcamp.com/artist-a",
            ),
            soundcloud=(
                "https://soundcloud.com/artist-a/tracks",
                "https://www.soundcloud.com/artist-a",
                "https://soundcloud.com/search?q=artist-a",
                "https://on.soundcloud.com/short-code",
            ),
        ),
    )
    plan = _plan(row)
    assert plan.bandcamp_urls == ("https://artist-a.bandcamp.com/",)
    assert plan.soundcloud_urls == ("https://soundcloud.com/artist-a",)


def _worker(tmp_path):
    worker = cde.CrossDirectoryEnricherWorker(
        seed_csv_path="",
        output_csv_path=(tmp_path / "out.csv").as_posix(),
        enable_live_search=True,
        max_live_searches=20,
    )
    worker.log_message = SimpleNamespace(emit=lambda _message: None)
    return worker


def _ctx(worker, dataframe):
    worker._init_row_enrichment_state()
    return worker._build_row_context(dataframe, 0, 1, 1)


def test_feature_flag_disabled_is_shadow_only(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", raising=False)
    dataframe = pd.DataFrame([_row(Identity_Evidence_JSON=_evidence(bandcamp=("https://artist-a.bandcamp.com",)))])
    worker = _worker(tmp_path)
    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_profile", lambda *args, **kwargs: pytest.fail("fetch"))
    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, "Bandcamp_URL"] == ""


def test_enabled_known_bandcamp_and_soundcloud_use_guarded_payloads_and_preserve_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame(
        [_row(Identity_Evidence_JSON=_evidence(
            bandcamp=("https://artist-a.bandcamp.com",),
            soundcloud=("https://soundcloud.com/artist-a",),
        ))]
    )
    worker = _worker(tmp_path)
    fetched = []

    def fake_fetch(platform, url, artist, ctx):
        fetched.append((platform, url))
        return cde.EnrichmentPayload(
            source_dir=platform,
            source_url=url,
            match_score=1.0,
            candidate_name=artist,
        )

    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_profile", fake_fetch)
    assert worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert fetched == [
        ("bandcamp", "https://artist-a.bandcamp.com/"),
        ("soundcloud", "https://soundcloud.com/artist-a"),
    ]
    assert dataframe.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
    assert dataframe.at[0, "SoundCloud Link"] == "https://soundcloud.com/artist-a"
    assert dataframe.at[0, "Social Link"] == ""
    for field in ("Lead_Source", "Source_Directory", "Source Directory", "Source URL"):
        assert dataframe.at[0, field] == _row()[field]


@pytest.mark.parametrize("platform,column", [("bandcamp", "Bandcamp_URL"), ("soundcloud", "SoundCloud Link")])
def test_rejected_known_identity_falls_back_without_block_or_safety_change(tmp_path, monkeypatch, platform, column):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    evidence = _evidence(**{platform: (f"https://artist-a.{platform}.com" if platform == "bandcamp" else "https://soundcloud.com/artist-a",)})
    dataframe = pd.DataFrame([_row(Email="safe@example.com", Identity_Evidence_JSON=evidence)])
    worker = _worker(tmp_path)
    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_profile", lambda *args, **kwargs: None)
    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, column] == ""
    assert dataframe.at[0, "Email"] == "safe@example.com"
    assert dataframe.at[0, "final_status"] == "valid"
    assert dataframe.at[0, "contact_safety"] == "safe"


@pytest.mark.parametrize("platform", ["bandcamp", "soundcloud"])
def test_multiple_independently_accepted_candidates_remain_unresolved(tmp_path, monkeypatch, platform):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    urls = (
        ("https://one.bandcamp.com", "https://two.bandcamp.com")
        if platform == "bandcamp"
        else ("https://soundcloud.com/one", "https://soundcloud.com/two")
    )
    dataframe = pd.DataFrame([_row(Identity_Evidence_JSON=_evidence(**{platform: urls}))])
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_musicbrainz_known_profile",
        lambda platform, url, artist, ctx: cde.EnrichmentPayload(
            source_dir=platform, source_url=url, match_score=1.0, candidate_name=artist
        ),
    )
    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, "Bandcamp_URL"] == ""
    assert dataframe.at[0, "SoundCloud Link"] == ""


def test_direct_known_bandcamp_fetch_uses_existing_parser_and_rejects_contradiction(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_url",
        lambda *args, **kwargs: '<html><head><meta property="og:title" content="Other Artist"></head></html>',
    )
    assert worker._fetch_profile_and_build(
        "https://artist-a.bandcamp.com/", "bandcamp", identity_artist_name="Artist A"
    ) is None


def test_direct_known_bandcamp_fetch_accepts_exact_profile_and_keeps_url_only_payload(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_url",
        lambda *args, **kwargs: '<html><head><meta property="og:title" content="Artist A"></head></html>',
    )
    payload = worker._fetch_profile_and_build(
        "https://artist-a.bandcamp.com/", "bandcamp", identity_artist_name="Artist A"
    )
    assert payload is not None
    assert payload.source_url == "https://artist-a.bandcamp.com"
    assert payload.candidate_name == "Artist A"
    assert payload.match_score >= cde.MIN_BC_CONFIDENCE


def test_direct_known_soundcloud_fetch_uses_existing_parser_and_accepts_exact_identity(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_url",
        lambda *args, **kwargs: (
            '<html><head><meta property="og:title" content="Artist A | Free Listening">'
            '<a href="https://artist.example">site</a></head></html>'
        ),
    )
    payload = worker._fetch_profile_and_build(
        "https://soundcloud.com/artist-a", "soundcloud", identity_artist_name="Artist A"
    )
    assert payload is not None
    assert payload.source_dir == "soundcloud"
    assert payload.candidate_name == "Artist A"
    assert payload.match_score >= cde.MIN_SC_CONFIDENCE


def test_unsafe_email_status_is_not_upgraded_by_valid_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Email="unsafe@example.com",
        final_status="unsafe",
        contact_safety="unsafe",
        Identity_Evidence_JSON=_evidence(bandcamp=("https://artist-a.bandcamp.com",)),
    )])
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_musicbrainz_known_profile",
        lambda platform, url, artist, ctx: cde.EnrichmentPayload(
            source_dir=platform, source_url=url, match_score=1.0, candidate_name=artist
        ),
    )
    assert worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, "Email"] == "unsafe@example.com"
    assert dataframe.at[0, "final_status"] == "unsafe"
    assert dataframe.at[0, "contact_safety"] == "unsafe"


def test_row_linear_and_source_phased_modes_schedule_bridge_before_live_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row()])

    row_worker = _worker(tmp_path)
    row_events = []
    monkeypatch.setattr(row_worker, "_enrich_row_directories", lambda *args: False)
    monkeypatch.setattr(row_worker, "_enrich_row_musicbrainz_relationships", lambda *args: row_events.append("bridge") or False)
    monkeypatch.setattr(row_worker, "_enrich_row_sc_live", lambda *args: row_events.append("soundcloud") or (False, False))
    monkeypatch.setattr(row_worker, "_enrich_row_live_lookup", lambda *args, **kwargs: (False, False))
    monkeypatch.setattr(row_worker, "_run_spotify_discovery_pass", lambda *args, **kwargs: False)
    monkeypatch.setattr(row_worker, "_run_instagram_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(row_worker, "_enrich_row_website_email", lambda *args: False)
    monkeypatch.setattr(row_worker, "_checkpoint_row_complete", lambda *args: None)
    monkeypatch.setattr(row_worker, "_update_progress", lambda *args: None)
    row_worker._run_row_linear(dataframe.copy(), {}, [], None, 1)
    assert row_events[:2] == ["bridge", "soundcloud"]

    phased_worker = _worker(tmp_path)
    phased_events = []
    monkeypatch.setenv("SOURCE_DIVERSITY_SCHEDULER", "0")
    monkeypatch.setattr(phased_worker, "_phase_directory_matching", lambda *args, **kwargs: phased_events.append("directories"))
    monkeypatch.setattr(phased_worker, "_phase_musicbrainz_relationships", lambda *args, **kwargs: phased_events.append("bridge"))
    monkeypatch.setattr(phased_worker, "_phase_soundcloud", lambda *args, **kwargs: phased_events.append("soundcloud") or {})
    monkeypatch.setattr(phased_worker, "_phase_live_lookup", lambda *args, **kwargs: None)
    monkeypatch.setattr(phased_worker, "_phase_spotify_discovery", lambda *args, **kwargs: None)
    monkeypatch.setattr(phased_worker, "_phase_instagram_email", lambda *args, **kwargs: None)
    monkeypatch.setattr(phased_worker, "_phase_website_email", lambda *args, **kwargs: None)
    monkeypatch.setattr(phased_worker, "_checkpoint_row_complete", lambda *args: None)
    phased_worker._run_source_phased(dataframe.copy(), {}, [], None, 1)
    assert phased_events[:3] == ["directories", "bridge", "soundcloud"]
