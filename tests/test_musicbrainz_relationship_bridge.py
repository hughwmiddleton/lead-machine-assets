import json
from types import SimpleNamespace

import pandas as pd
import pytest

import cross_directory_enricher as cde
import musicbrainz_relationship_bridge as mbrb
import bandcamp_profile_engine as bpe
from musicbrainz_relationship_bridge import build_relationship_bridge_plan


def _evidence(
    name="Artist A",
    aliases=(),
    bandcamp=(),
    soundcloud=(),
    instagram=(),
    official_homepage=(),
):
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
                    "instagram": [{"url": url, "type": "instagram"} for url in instagram],
                    "official_homepage": [
                        {"url": url, "type": "official homepage"} for url in official_homepage
                    ],
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
        "Email_Provenance_JSON": "",
        "final_status": "valid",
        "contact_safety": "safe",
        "Match_Score": "0.40",
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
        canonicalize_instagram=cde._canonicalize_instagram_profile_url,
        canonicalize_website=lambda value: cde._normalise_url(value) or "",
        valid_instagram=lambda value: bool(cde._canonicalize_instagram_profile_url(value)),
        valid_website=cde._is_website_enrich_candidate_url,
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


def test_instagram_and_official_website_candidates_are_canonical_and_platform_scoped():
    row = _row(
        Identity_Evidence_JSON=_evidence(
            instagram=(
                "https://www.instagram.com/artist_a/?igsh=tracking",
                "https://instagram.com/artist_a/",
                "https://instagram.com/p/not-a-profile/",
            ),
            official_homepage=(
                "http://artist-a.test/",
                "https://facebook.com/must-not-be-a-website",
            ),
        )
    )
    plan = _plan(row)
    assert plan.instagram_urls == ("https://www.instagram.com/artist_a/",)
    assert plan.official_website_urls == ("http://artist-a.test/",)


def _website_result(url, html, *, final_url="", status=200, is_html=True):
    return cde.WebsiteFetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        content_type="text/html" if is_html else "application/octet-stream",
        html=html,
        is_html=is_html,
    )


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


def _accepted(payload):
    return mbrb.KnownProfileFetchResult(mbrb.KNOWN_PROFILE_ACCEPTED, payload=payload)


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<html><head><title>Client Challenge</title></head></html>", "client_challenge_title"),
        ("<html><body>Verify you are human</body></html>", "recognized_soft_block"),
    ],
)
def test_bandcamp_http_200_challenge_surfaces_are_recognized(html, expected):
    assert cde._bandcamp_challenge_reason(html) == expected


def test_normal_bandcamp_artist_page_is_not_challenge():
    html = '<html><head><title>Artist A</title><meta property="og:title" content="Artist A"></head></html>'
    assert cde._bandcamp_challenge_reason(html) == ""


def test_known_bandcamp_result_distinguishes_challenge_identity_rejection_and_error(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    ctx = {"song_title": ""}

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", lambda *args, **kwargs: bpe.BandcampProfileResult(
        bpe.PROFILE_CHALLENGE_UNAVAILABLE,
        "https://artist-a.bandcamp.com/",
        reason="client_challenge_title",
    ))
    challenged = worker._fetch_musicbrainz_known_profile(
        "bandcamp", "https://artist-a.bandcamp.com/", "Artist A", ctx
    )
    assert challenged.status == mbrb.KNOWN_PROFILE_CHALLENGE_UNAVAILABLE
    assert challenged.reason == "client_challenge_title"
    assert challenged.payload is None

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", lambda *args, **kwargs: bpe.BandcampProfileResult(
        bpe.PROFILE_ACCEPTED,
        "https://artist-a.bandcamp.com/",
        profile={"artist_name": "Other Artist", "socials": {}, "emails": []},
        identity_evidence={"page_artist": "Other Artist"},
    ))
    rejected = worker._fetch_musicbrainz_known_profile(
        "bandcamp", "https://artist-a.bandcamp.com/", "Artist A", ctx
    )
    assert rejected.status == mbrb.KNOWN_PROFILE_IDENTITY_REJECTED
    assert rejected.reason == "artist_identity_contradiction"

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", lambda *args, **kwargs: bpe.BandcampProfileResult(
        bpe.PROFILE_ERROR,
        "https://artist-a.bandcamp.com/",
        reason="profile_fetch_failed",
    ))
    failed = worker._fetch_musicbrainz_known_profile(
        "bandcamp", "https://artist-a.bandcamp.com/", "Artist A", ctx
    )
    assert failed.status == mbrb.KNOWN_PROFILE_ERROR
    assert failed.reason == "profile_fetch_failed"
    assert worker.live_search_attempts == 3


def test_known_bandcamp_uses_shared_engine_not_generic_fetch_and_keeps_bandcamp_provenance(
    tmp_path, monkeypatch
):
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_url",
        lambda *args, **kwargs: pytest.fail("generic _fetch_url must not handle known Bandcamp URLs"),
    )
    calls = []

    def shared(url, **kwargs):
        calls.append((url, kwargs.get("session")))
        return bpe.BandcampProfileResult(
            bpe.PROFILE_ACCEPTED,
            "https://artist-a.bandcamp.com/",
            profile={
                "artist_name": "Artist A",
                "website": "https://artist-a.example",
                "email": "artist@example.com",
                "emails": ["artist@example.com"],
                "all_social_links": ["https://instagram.com/artist_a"],
                "socials": {"instagram": "https://instagram.com/artist_a"},
            },
            identity_evidence={"page_artist": "Artist A"},
        )

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", shared)
    result = worker._fetch_musicbrainz_known_profile(
        "bandcamp", "https://artist-a.bandcamp.com/", "Artist A", {"song_title": ""}
    )

    assert result.status == mbrb.KNOWN_PROFILE_ACCEPTED
    assert calls == [("https://artist-a.bandcamp.com/", worker._bc_session)]
    assert result.payload.source_dir == "bandcamp"
    assert result.payload.source_url == "https://artist-a.bandcamp.com/"
    assert result.payload.emails == {"artist@example.com"}
    assert result.payload.websites == {"https://artist-a.example"}
    assert result.payload.socials == {"https://instagram.com/artist_a"}


def test_link_hub_follow_excludes_application_shell_and_is_bounded():
    external_socials = "".join(
        f'<a href="https://instagram.com/artist_{index}">Artist social</a>'
        for index in range(cde.MAX_LINK_HUB_SOCIALS_PER_ROW + 5)
    )
    html = (
        '<a href="https://linktr.ee/blog">Blog</a>'
        '<a href="https://linktr.ee/unrelated_creator">Other creator</a>'
        '<a href="https://beacons.ai/unrelated_creator">Other hub</a>'
        + external_socials
    )

    class Response:
        text = html

        @staticmethod
        def raise_for_status():
            return None

    session = SimpleNamespace(get=lambda *args, **kwargs: Response())
    result = cde._scrape_link_hub_socials(session, "https://linktr.ee/artist_a")

    assert len(result) == cde.MAX_LINK_HUB_SOCIALS_PER_ROW
    assert all("linktr.ee" not in value for value in result)
    assert all("beacons.ai" not in value for value in result)


def test_accepted_shared_bandcamp_result_enters_guarded_application(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Identity_Evidence_JSON=_evidence(bandcamp=("https://artist-a.bandcamp.com",)),
    )])
    worker = _worker(tmp_path)
    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", lambda *args, **kwargs: bpe.BandcampProfileResult(
        bpe.PROFILE_ACCEPTED,
        "https://artist-a.bandcamp.com/",
        profile={
            "artist_name": "Artist A",
            "email": "artist@example.com",
            "emails": ["artist@example.com"],
            "socials": {},
        },
        identity_evidence={"page_artist": "Artist A"},
    ))
    guarded_calls = []
    original_guard = worker._apply_payload_guarded

    def guarded(*args, **kwargs):
        guarded_calls.append(args[2])
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(worker, "_apply_payload_guarded", guarded)
    assert worker._enrich_row_musicbrainz_relationships(
        dataframe, 0, _ctx(worker, dataframe)
    )
    assert len(guarded_calls) == 1
    assert guarded_calls[0].source_dir == "bandcamp"
    assert dataframe.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
    assert dataframe.at[0, "Email"] == "artist@example.com"
    assert dataframe.at[0, "Lead_Source"] == "Spotify"


def test_musicbrainz_bandcamp_challenge_uses_bounded_browser_and_guarded_apply(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Identity_Evidence_JSON=_evidence(bandcamp=("https://artist-a.bandcamp.com",)),
    )])
    worker = _worker(tmp_path)
    browser_calls = []
    guarded_calls = []
    monkeypatch.setattr(
        worker,
        "_bandcamp_browser_fetch",
        lambda url: browser_calls.append(url) or "<html>usable artist page</html>",
    )
    monkeypatch.setattr(
        worker,
        "_fetch_url",
        lambda *args, **kwargs: pytest.fail("generic fetch must remain unused"),
    )

    def shared(url, **kwargs):
        assert kwargs["browser_on_empty"] is False
        assert kwargs["browser_fetcher"](url) == "<html>usable artist page</html>"
        return bpe.BandcampProfileResult(
            bpe.PROFILE_ACCEPTED,
            url,
            profile={
                "artist_name": "Artist A",
                "email": "artist@example.com",
                "emails": ["artist@example.com"],
                "socials": {},
            },
            browser_used=True,
            identity_evidence={"page_artist": "Artist A"},
        )

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", shared)
    original_guard = worker._apply_payload_guarded

    def guarded(*args, **kwargs):
        guarded_calls.append(args[2])
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(worker, "_apply_payload_guarded", guarded)
    assert worker._enrich_row_musicbrainz_relationships(
        dataframe, 0, _ctx(worker, dataframe)
    )
    assert browser_calls == ["https://artist-a.bandcamp.com/"]
    assert len(guarded_calls) == 1
    assert guarded_calls[0].source_dir == "bandcamp"
    assert dataframe.at[0, "Email"] == "artist@example.com"
    assert dataframe.at[0, "Lead_Source"] == "Spotify"
    assert worker.live_search_attempts == 1


def test_usable_identity_contradiction_does_not_trigger_browser_retry(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    browser_calls = []
    monkeypatch.setattr(
        worker,
        "_bandcamp_browser_fetch",
        lambda url: browser_calls.append(url) or pytest.fail("identity rejection must not browse"),
    )
    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", lambda *args, **kwargs: bpe.BandcampProfileResult(
        bpe.PROFILE_ACCEPTED,
        "https://artist-a.bandcamp.com/",
        profile={"artist_name": "Other Artist", "socials": {}, "emails": []},
        browser_used=False,
        identity_evidence={"page_artist": "Other Artist"},
    ))
    result = worker._fetch_musicbrainz_known_profile(
        "bandcamp",
        "https://artist-a.bandcamp.com/",
        "Artist A",
        {"song_title": ""},
        allow_bandcamp_browser_fallback=True,
    )
    assert result.status == mbrb.KNOWN_PROFILE_IDENTITY_REJECTED
    assert browser_calls == []
    assert worker.live_search_attempts == 1


def test_browser_challenge_remains_pending_and_consumes_one_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Identity_Evidence_JSON=_evidence(bandcamp=("https://artist-a.bandcamp.com",)),
    )])
    worker = _worker(tmp_path)
    worker.max_live_searches = 2
    browser_calls = []
    monkeypatch.setattr(
        worker,
        "_bandcamp_browser_fetch",
        lambda url: browser_calls.append(url) or "<title>Client Challenge</title>",
    )

    def shared(url, **kwargs):
        kwargs["browser_fetcher"](url)
        return bpe.BandcampProfileResult(
            bpe.PROFILE_CHALLENGE_UNAVAILABLE,
            url,
            reason="client_challenge_title",
            browser_used=True,
        )

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", shared)
    assert not worker._enrich_row_musicbrainz_relationships(
        dataframe, 0, _ctx(worker, dataframe)
    )
    assert browser_calls == ["https://artist-a.bandcamp.com/"]
    assert worker.live_search_attempts == 1
    assert worker._row_enrichment_state["bandcamp"] == "pending"
    assert worker._increment_live_counter()


def test_bandcamp_browser_owner_is_lazy_reused_and_cleaned_up(tmp_path, monkeypatch):
    worker = _worker(tmp_path)

    class Driver:
        def __init__(self):
            self.quit_calls = 0

        def quit(self):
            self.quit_calls += 1

    driver = Driver()
    setup_calls = []
    visit_calls = []
    monkeypatch.setattr(cde, "setup_bandcamp_driver", lambda: setup_calls.append(True) or driver)
    monkeypatch.setattr(
        cde,
        "bandcamp_quick_visit",
        lambda active_driver, url: visit_calls.append((active_driver, url)) or "<html>artist</html>",
    )
    assert worker._bandcamp_browser_fetch("https://one.bandcamp.com/") == "<html>artist</html>"
    assert worker._bandcamp_browser_fetch("https://two.bandcamp.com/") == "<html>artist</html>"
    worker._cleanup_bandcamp_browser_driver()
    worker._cleanup_bandcamp_browser_driver()
    assert setup_calls == [True]
    assert [url for _, url in visit_calls] == [
        "https://one.bandcamp.com/",
        "https://two.bandcamp.com/",
    ]
    assert driver.quit_calls == 1


def test_multiple_bandcamp_candidates_respect_global_budget_with_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Identity_Evidence_JSON=_evidence(bandcamp=(
            "https://one.bandcamp.com",
            "https://two.bandcamp.com",
        )),
    )])
    worker = _worker(tmp_path)
    worker.max_live_searches = 1
    shared_calls = []
    browser_calls = []
    monkeypatch.setattr(
        worker,
        "_bandcamp_browser_fetch",
        lambda url: browser_calls.append(url) or "<title>Client Challenge</title>",
    )

    def shared(url, **kwargs):
        shared_calls.append(url)
        kwargs["browser_fetcher"](url)
        return bpe.BandcampProfileResult(
            bpe.PROFILE_CHALLENGE_UNAVAILABLE,
            url,
            reason="client_challenge_title",
            browser_used=True,
        )

    monkeypatch.setattr(cde, "_shared_fetch_bandcamp_profile", shared)
    assert not worker._enrich_row_musicbrainz_relationships(
        dataframe, 0, _ctx(worker, dataframe)
    )
    assert len(shared_calls) == 1
    assert len(browser_calls) == 1
    assert worker.live_search_attempts == 1


def test_mayce_macy_kate_gate_prevents_shared_bandcamp_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        "MAYCE",
        Identity_Evidence_JSON=_evidence(
            name="Macy Kate",
            bandcamp=("https://macykate.bandcamp.com",),
        ),
    )])
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        cde,
        "_shared_fetch_bandcamp_profile",
        lambda *args, **kwargs: pytest.fail("identity gate must run before Bandcamp fetch"),
    )
    assert not worker._enrich_row_musicbrainz_relationships(
        dataframe, 0, _ctx(worker, dataframe)
    )
    assert worker.live_search_attempts == 0


@pytest.mark.parametrize(
    ("email", "final_status", "contact_safety"),
    [
        ("safe@example.com", "valid", "safe"),
        ("unsafe@example.com", "unsafe", "unsafe"),
    ],
)
def test_challenged_bandcamp_candidate_is_neutral_and_preserves_fallback_budget(
    tmp_path,
    monkeypatch,
    email,
    final_status,
    contact_safety,
):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Email=email,
        final_status=final_status,
        contact_safety=contact_safety,
        Identity_Evidence_JSON=_evidence(bandcamp=("https://artist-a.bandcamp.com",)),
    )])
    worker = _worker(tmp_path)
    worker.max_live_searches = 2
    monkeypatch.setattr(
        cde,
        "_shared_fetch_bandcamp_profile",
        lambda *args, **kwargs: bpe.BandcampProfileResult(
            bpe.PROFILE_CHALLENGE_UNAVAILABLE,
            "https://artist-a.bandcamp.com/",
            reason="client_challenge_title",
        ),
    )
    ctx = _ctx(worker, dataframe)
    protected = {
        field: dataframe.at[0, field]
        for field in (
            "Email",
            "Email_Provenance_JSON",
            "final_status",
            "contact_safety",
            "Match_Score",
            "match_score_overall",
            "directory_conflict_flag",
            "name_consistency_flag",
            "Lead_Source",
            "Source_Directory",
            "Source Directory",
            "Source URL",
        )
    }

    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, ctx)
    assert dataframe.at[0, "Bandcamp_URL"] == ""
    assert worker._row_enrichment_state["bandcamp"] == "pending"
    assert worker._platform_attempt_allowed("bandcamp", "Artist A", "Bandcamp Enrich")
    assert worker.live_search_attempts == 1
    assert worker._increment_live_counter()  # Existing fallback receives the remaining bounded unit.
    assert not worker._increment_live_counter()  # The existing cap remains enforced.
    for field, value in protected.items():
        assert dataframe.at[0, field] == value


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

    def fake_fetch(platform, url, artist, ctx, **kwargs):
        fetched.append((platform, url))
        return _accepted(cde.EnrichmentPayload(
            source_dir=platform,
            source_url=url,
            match_score=1.0,
            candidate_name=artist,
        ))

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
    monkeypatch.setattr(
        worker,
        "_fetch_musicbrainz_known_profile",
        lambda *args, **kwargs: mbrb.KnownProfileFetchResult(
            mbrb.KNOWN_PROFILE_IDENTITY_REJECTED,
            reason="artist_identity_contradiction",
        ),
    )
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
        lambda platform, url, artist, ctx, **kwargs: _accepted(cde.EnrichmentPayload(
            source_dir=platform, source_url=url, match_score=1.0, candidate_name=artist
        )),
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
    assert worker._last_known_profile_status == mbrb.KNOWN_PROFILE_IDENTITY_REJECTED


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
    assert worker._last_known_profile_status == mbrb.KNOWN_PROFILE_ACCEPTED


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
    assert worker._last_known_profile_status == mbrb.KNOWN_PROFILE_ACCEPTED


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
        lambda platform, url, artist, ctx, **kwargs: _accepted(cde.EnrichmentPayload(
            source_dir=platform, source_url=url, match_score=1.0, candidate_name=artist
        )),
    )
    assert worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, "Email"] == "unsafe@example.com"
    assert dataframe.at[0, "final_status"] == "unsafe"
    assert dataframe.at[0, "contact_safety"] == "unsafe"


def test_known_instagram_uses_existing_admission_and_classifies_unavailable(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    calls = []

    def accepted_admission(session, row, url, artist):
        calls.append((session, row["Artist Name"], url, artist))
        return (True, "strong:profile_name_match")

    monkeypatch.setattr(cde, "_spotify_seed_instagram_admission_profile_validation", accepted_admission)
    result = worker._fetch_musicbrainz_known_instagram(
        "https://www.instagram.com/artist_a/", "Artist A", _row()
    )
    assert result.status == mbrb.KNOWN_PROFILE_ACCEPTED
    assert result.payload.socials == {"https://www.instagram.com/artist_a/"}
    assert not result.payload.emails
    assert calls[0][1:] == (
        "Artist A",
        "https://www.instagram.com/artist_a/",
        "Artist A",
    )

    monkeypatch.setattr(
        cde,
        "_spotify_seed_instagram_admission_profile_validation",
        lambda *args: (False, "blocked:profile_unavailable"),
    )
    unavailable = worker._fetch_musicbrainz_known_instagram(
        "https://www.instagram.com/artist_a/", "Artist A", _row()
    )
    assert unavailable.status == mbrb.KNOWN_PROFILE_CHALLENGE_UNAVAILABLE
    assert unavailable.payload is None

    monkeypatch.setattr(
        cde,
        "_spotify_seed_instagram_admission_profile_validation",
        lambda *args: (False, "blocked:insufficient_identity"),
    )
    rejected = worker._fetch_musicbrainz_known_instagram(
        "https://www.instagram.com/artist_a/", "Artist A", _row()
    )
    assert rejected.status == mbrb.KNOWN_PROFILE_IDENTITY_REJECTED
    assert rejected.payload is None


def test_known_website_accepts_strong_identity_and_same_domain_redirect(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: _website_result(
            "http://artist-a.test/",
            "<html><head><title>Artist A | Official Website</title></head></html>",
            final_url="https://www.artist-a.test/",
        ),
    )
    result = worker._fetch_musicbrainz_known_website("http://artist-a.test/", "Artist A")
    assert result.status == mbrb.KNOWN_PROFILE_ACCEPTED
    assert result.payload.websites == {"https://www.artist-a.test/"}
    assert not result.payload.emails


@pytest.mark.parametrize(
    "fetch_result",
    [
        _website_result(
            "https://artist-a.test/",
            "<html><head><title>Other Artist</title></head></html>",
        ),
        _website_result(
            "https://artist-a.test/",
            "<html><head><title>Artist A</title></head></html>",
            final_url="https://unrelated.test/",
        ),
        _website_result("https://artist-a.test/", "", status=503, is_html=False),
    ],
)
def test_known_website_rejects_contradiction_cross_domain_redirect_or_unavailable(
    tmp_path, monkeypatch, fetch_result
):
    worker = _worker(tmp_path)
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", lambda *args, **kwargs: fetch_result)
    result = worker._fetch_musicbrainz_known_website("https://artist-a.test/", "Artist A")
    assert result.status != mbrb.KNOWN_PROFILE_ACCEPTED
    assert result.payload is None


def test_unique_instagram_and_website_are_bridged_without_email_or_origin_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Email="safe@example.com",
        Bandcamp_URL="https://artist-a.bandcamp.com/",
        Email_Provenance_JSON='{"safe@example.com":{"source_type":"seed"}}',
        Identity_Evidence_JSON=_evidence(
            instagram=("https://instagram.com/artist_a/",),
            official_homepage=("https://artist-a.test/",),
        ),
    )])
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_fetch_musicbrainz_known_instagram",
        lambda url, artist, row: _accepted(cde.EnrichmentPayload(
            socials={url}, source_dir="musicbrainz_instagram_bridge", source_url=url,
            match_score=1.0, candidate_name=artist,
        )),
    )
    monkeypatch.setattr(
        worker,
        "_fetch_musicbrainz_known_website",
        lambda url, artist: _accepted(cde.EnrichmentPayload(
            websites={url}, source_dir="musicbrainz_website_bridge", source_url=url,
            match_score=1.0, candidate_name=artist,
        )),
    )
    before = dataframe.loc[0, [
        "Email", "Email_Provenance_JSON", "Lead_Source", "Source_Directory",
        "Source Directory", "Source URL", "final_status", "contact_safety",
    ]].to_dict()
    assert worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert cde._get_canonical_instagram_url(dataframe.loc[0]) == "https://www.instagram.com/artist_a/"
    assert dataframe.at[0, "External Links"] == "https://artist-a.test/"
    assert dataframe.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com/"
    for field, value in before.items():
        assert dataframe.at[0, field] == value


def test_existing_instagram_and_website_values_skip_musicbrainz_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        **{
            "Social Link": "https://www.instagram.com/existing_artist/",
            "External Links": "https://existing-artist.test/",
            "Identity_Evidence_JSON": _evidence(
                instagram=("https://instagram.com/artist_a/",),
                official_homepage=("https://artist-a.test/",),
            ),
        },
    )])
    worker = _worker(tmp_path)
    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_instagram", lambda *args: pytest.fail("instagram fetch"))
    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_website", lambda *args: pytest.fail("website fetch"))
    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, "Social Link"] == "https://www.instagram.com/existing_artist/"
    assert dataframe.at[0, "External Links"] == "https://existing-artist.test/"


@pytest.mark.parametrize("platform", ["instagram", "official_homepage"])
def test_multiple_accepted_instagram_or_website_candidates_remain_unresolved(
    tmp_path, monkeypatch, platform
):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    urls = (
        ("https://instagram.com/one/", "https://instagram.com/two/")
        if platform == "instagram"
        else ("https://one.test/", "https://two.test/")
    )
    dataframe = pd.DataFrame([_row(Identity_Evidence_JSON=_evidence(**{platform: urls}))])
    worker = _worker(tmp_path)
    if platform == "instagram":
        monkeypatch.setattr(
            worker,
            "_fetch_musicbrainz_known_instagram",
            lambda url, artist, row: _accepted(cde.EnrichmentPayload(
                socials={url}, source_dir="musicbrainz_instagram_bridge",
                source_url=url, match_score=1.0, candidate_name=artist,
            )),
        )
    else:
        monkeypatch.setattr(
            worker,
            "_fetch_musicbrainz_known_website",
            lambda url, artist: _accepted(cde.EnrichmentPayload(
                websites={url}, source_dir="musicbrainz_website_bridge",
                source_url=url, match_score=1.0, candidate_name=artist,
            )),
        )
    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))
    assert dataframe.at[0, "Social Link"] == ""
    assert dataframe.at[0, "External Links"] == ""


def test_mayce_mismatch_blocks_instagram_and_website_without_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        "MAYCE",
        Identity_Evidence_JSON=_evidence(
            name="Macy Kate",
            instagram=("https://instagram.com/macykate/",),
            official_homepage=("https://macykate.test/",),
        ),
    )])
    worker = _worker(tmp_path)
    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_instagram", lambda *args: pytest.fail("instagram fetch"))
    monkeypatch.setattr(worker, "_fetch_musicbrainz_known_website", lambda *args: pytest.fail("website fetch"))
    assert not worker._enrich_row_musicbrainz_relationships(dataframe, 0, _ctx(worker, dataframe))


def test_bridged_website_email_keeps_website_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICBRAINZ_RELATIONSHIP_BRIDGE_ENABLED", "1")
    dataframe = pd.DataFrame([_row(
        Identity_Evidence_JSON=_evidence(official_homepage=("https://artist-a.test/",)),
    )])
    worker = _worker(tmp_path)
    homepage = _website_result(
        "https://artist-a.test/",
        '<html><head><title>Artist A</title></head><body><a href="mailto:hello@artist-a.test">Email</a></body></html>',
    )
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", lambda *args, **kwargs: homepage)
    ctx = _ctx(worker, dataframe)
    assert worker._enrich_row_musicbrainz_relationships(dataframe, 0, ctx)
    assert worker._enrich_row_website_email(dataframe, 0, ctx)
    assert dataframe.at[0, "Email"] == "hello@artist-a.test"
    assert dataframe.at[0, "Email_Source_Type"] == "website_enrich"
    assert dataframe.at[0, "Email_Source_URL"] == "https://artist-a.test/"
    assert "musicbrainz" not in dataframe.at[0, "Email_Provenance_JSON"].lower()


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
