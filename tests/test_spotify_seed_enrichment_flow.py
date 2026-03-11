from types import SimpleNamespace

import pandas as pd

import cross_directory_enricher as cde
import pipeline_runner
import source_scheduler


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _build_worker(tmp_path):
    worker = cde.CrossDirectoryEnricherWorker(
        seed_csv_path="",
        output_csv_path=(tmp_path / "spotify_enriched.csv").as_posix(),
        enable_live_search=False,
        max_live_searches=0,
    )
    worker.log_message = SimpleNamespace(emit=lambda msg: None)
    return worker


def _base_row(**overrides):
    row = {
        "Artist Name": "Artist A",
        "Source Directory": "spotify",
        "Spotify_URL": "https://open.spotify.com/artist/artist-a",
        "Spotify_Artist_ID": "artist-a",
        "Spotify_Website_URL": "",
        "Spotify_Genres": "",
        "SoundCloud Link": "",
        "Bandcamp_URL": "",
        "External Links": "",
        "Website": "",
        "Email": "",
        "Email_All": "",
        "Email_Type": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
        "facebook_url": "",
        "Facebook_URL": "",
        "Facebook URL": "",
        "FB_Status": "",
        "Location": "",
        "Primary Genre": "",
        "Song Title": "",
        "Source URL": "",
        "Social Link": "",
    }
    row.update(overrides)
    return row


def test_run_master_enrichment_forwards_detected_directory_csvs(tmp_path, monkeypatch):
    seed_csv = tmp_path / "master_raw.csv"
    output_csv = tmp_path / "master_enriched.csv"
    _write_csv(seed_csv, [{"Artist Name": "Artist A"}])
    bandcamp_csv = tmp_path / "job_bandcamp_1" / "bandcamp_enriched.csv"
    soundcloud_csv = tmp_path / "job_soundcloud_1" / "raw.csv"
    lastfm_csv = tmp_path / "job_lastfm_1" / "raw.csv"
    unearthed_csv = tmp_path / "job_unearthed_1" / "raw.csv"
    for path in (bandcamp_csv, soundcloud_csv, lastfm_csv, unearthed_csv):
        _write_csv(path, [{"Artist Name": "Artist A"}])

    captured = []

    def fake_run_cross_directory_enrichment(seed_path, output_path, **kwargs):
        captured.append(kwargs)
        _write_csv(output_path, [{"Artist Name": "Artist A"}])

    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)

    pipeline_runner.run_master_enrichment(
        seed_csv.as_posix(),
        output_csv.as_posix(),
        logger=None,
        enable_live_search=False,
        bandcamp_csv_path="",
    )

    assert len(captured) == 1
    assert captured[0]["bandcamp_csv_path"] == bandcamp_csv.as_posix()
    assert captured[0]["soundcloud_csv_path"] == soundcloud_csv.as_posix()
    assert captured[0]["lastfm_csv_path"] == lastfm_csv.as_posix()
    assert captured[0]["unearthed_csv_path"] == unearthed_csv.as_posix()


def test_run_master_enrichment_keeps_missing_directory_paths_empty(tmp_path, monkeypatch):
    seed_csv = tmp_path / "master_raw.csv"
    output_csv = tmp_path / "master_enriched.csv"
    _write_csv(seed_csv, [{"Artist Name": "Artist A"}])
    bandcamp_csv = tmp_path / "job_bandcamp_1" / "bandcamp_enriched.csv"
    _write_csv(bandcamp_csv, [{"Artist Name": "Artist A"}])

    captured = []

    def fake_run_cross_directory_enrichment(seed_path, output_path, **kwargs):
        captured.append(kwargs)
        _write_csv(output_path, [{"Artist Name": "Artist A"}])

    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)

    pipeline_runner.run_master_enrichment(
        seed_csv.as_posix(),
        output_csv.as_posix(),
        logger=None,
        enable_live_search=False,
        bandcamp_csv_path="",
    )

    assert len(captured) == 1
    assert captured[0]["bandcamp_csv_path"] == bandcamp_csv.as_posix()
    assert captured[0]["soundcloud_csv_path"] == ""
    assert captured[0]["lastfm_csv_path"] == ""
    assert captured[0]["unearthed_csv_path"] == ""


def test_spotify_origin_detection_survives_source_directory_changes(tmp_path):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row(**{"Source Directory": "Bandcamp"})])

    row = df.loc[0]
    ctx = worker._build_row_context(df, 0, 1, 1)

    assert source_scheduler.is_spotify_origin_row(row) is True
    assert ctx["spotify_origin"] is True
    assert worker._row_is_spotify_origin(row, ctx) is True


def test_domain_reuse_does_not_short_circuit_spotify_origin_rows(tmp_path):
    worker = _build_worker(tmp_path)
    worker._domain_email_reuse_rows = set()
    worker._domain_email_reuse_index = {
        "artist.test": {
            "email": "hello@artist.test",
            "email_all": "hello@artist.test",
            "source_url": "https://artist.test/contact",
            "source_type": "website_enrich",
            "extract_method": "regex",
            "email_type": "website_enrich",
        }
    }
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test")])
    ctx = worker._build_row_context(df, 0, 1, 1)

    should_continue = worker._should_short_circuit_after_domain_reuse(df, 0, ctx)

    assert should_continue is False
    assert df.at[0, "Email"] == "hello@artist.test"


def test_facebook_discovery_runs_for_spotify_origin_row_even_with_email(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._fb_discovery_attempted_rows = set()
    worker._init_row_enrichment_state()

    discovered = {"count": 0}

    def fake_discover(*args, **kwargs):
        discovered["count"] += 1
        return "https://www.facebook.com/artist-a"

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", lambda *args, **kwargs: ([], "", "no_email_on_page"))

    df = pd.DataFrame([_base_row(Email="existing@example.com")])
    ctx = worker._build_row_context(df, 0, 1, 1)

    worker._enrich_row_facebook(df, 0, object(), ctx)

    assert discovered["count"] == 1


def test_facebook_discovery_still_skips_non_spotify_row_with_email(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker._fb_discovery_attempted_rows = set()
    worker._init_row_enrichment_state()

    discovered = {"count": 0}

    def fake_discover(*args, **kwargs):
        discovered["count"] += 1
        return "https://www.facebook.com/artist-a"

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    df = pd.DataFrame(
        [
            _base_row(
                Spotify_URL="",
                Spotify_Artist_ID="",
                Email="existing@example.com",
                **{"Source Directory": "bandcamp"},
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)

    worker._enrich_row_facebook(df, 0, object(), ctx)

    assert discovered["count"] == 0


def test_website_crawl_runs_for_spotify_origin_row_even_with_email(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    fetch_calls = []

    def fake_fetch(*args, **kwargs):
        fetch_calls.append(kwargs.get("timeout_s"))
        return cde.WebsiteFetchResult(
            url="https://artist.test",
            final_url="https://artist.test",
            status=200,
            content_type="text/html",
            html="<html><body>contact@artist.test</body></html>",
            is_html=True,
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    df = pd.DataFrame([_base_row(Email="existing@example.com", **{"External Links": "https://artist.test"})])
    ctx = worker._build_row_context(df, 0, 1, 1)

    enriched = worker._enrich_row_website_email(df, 0, ctx)

    assert enriched is True
    assert fetch_calls


def test_website_crawl_still_skips_non_spotify_row_with_email(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    fetch_calls = []

    def fake_fetch(*args, **kwargs):
        fetch_calls.append(True)
        return cde.WebsiteFetchResult(
            url="https://artist.test",
            final_url="https://artist.test",
            status=200,
            content_type="text/html",
            html="<html><body>contact@artist.test</body></html>",
            is_html=True,
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    df = pd.DataFrame(
        [
            _base_row(
                Spotify_URL="",
                Spotify_Artist_ID="",
                Email="existing@example.com",
                **{"External Links": "https://artist.test"},
                **{"Source Directory": "bandcamp"},
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)

    enriched = worker._enrich_row_website_email(df, 0, ctx)

    assert enriched is False
    assert fetch_calls == []


def test_scheduler_does_not_short_circuit_spotify_origin_rows_after_email():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist A", "Email": "", "Email_All": "", "Spotify_URL": "https://open.spotify.com/artist/a"}}
    calls = {"SC": 0, "LF": 0}

    def sc_run(idx):
        calls["SC"] += 1
        row_data[idx]["Email"] = "artist@example.com"
        return source_scheduler.SourceResult(attempted=True, enriched=True)

    def lf_run(idx):
        calls["LF"] += 1
        return source_scheduler.SourceResult(attempted=True, enriched=True)

    scheduler = source_scheduler.SourceDiversityScheduler(
        [
            source_scheduler.SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None), row_getter=lambda idx: row_data[idx]),
            source_scheduler.SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lambda: (True, None), row_getter=lambda idx: row_data[idx]),
        ],
        row_label=str,
        short_circuit_fn=lambda row: pipeline_runner.has_contact_email_for_short_circuit(row),
    )

    scheduler.run()

    assert calls["SC"] == 1
    assert calls["LF"] == 1


def test_scheduler_still_short_circuits_non_spotify_rows_after_email():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist A", "Email": "", "Email_All": "", "Spotify_URL": ""}}
    calls = {"SC": 0, "LF": 0}

    def sc_run(idx):
        calls["SC"] += 1
        row_data[idx]["Email"] = "artist@example.com"
        return source_scheduler.SourceResult(attempted=True, enriched=True)

    def lf_run(idx):
        calls["LF"] += 1
        return source_scheduler.SourceResult(attempted=True, enriched=True)

    scheduler = source_scheduler.SourceDiversityScheduler(
        [
            source_scheduler.SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None), row_getter=lambda idx: row_data[idx]),
            source_scheduler.SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lambda: (True, None), row_getter=lambda idx: row_data[idx]),
        ],
        row_label=str,
        short_circuit_fn=lambda row: pipeline_runner.has_contact_email_for_short_circuit(row),
    )

    scheduler.run()

    assert calls["SC"] == 1
    assert calls["LF"] == 0


def test_scheduler_fb_opportunity_remains_open_for_spotify_origin_rows_with_email():
    spotify_row = {"Artist Name": "Artist A", "Email": "artist@example.com", "Spotify_Artist_ID": "abc"}
    normal_row = {"Artist Name": "Artist A", "Email": "artist@example.com", "Spotify_Artist_ID": ""}

    assert source_scheduler._row_source_opportunity(spotify_row, "FB") is True
    assert source_scheduler._row_source_opportunity(normal_row, "FB") is False


def test_phase_spotify_discovery_only_targets_spotify_origin_rows(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame(
        [
            _base_row(),
            _base_row(
                Spotify_URL="",
                Spotify_Artist_ID="",
                **{"Source Directory": "bandcamp"},
            ),
        ]
    )
    seen = []

    def fake_pass(seed_df, row_idx, ctx, fb_driver=None):
        seen.append((row_idx, ctx["spotify_origin"]))
        return False

    monkeypatch.setattr(worker, "_run_spotify_discovery_pass", fake_pass)

    worker._phase_spotify_discovery(df, total=len(df), fb_driver=None)

    assert seen == [(0, True)]


def test_spotify_runtime_identity_uses_existing_seed_context(tmp_path):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame(
        [
            _base_row(),
            _base_row(
                Spotify_Website_URL="https://artist.test",
                Location="Melbourne",
                Spotify_Genres="indie pop",
            ),
        ]
    )

    low_ctx = worker._build_row_context(df, 0, 1, len(df))
    mid_ctx = worker._build_row_context(df, 1, 2, len(df))

    assert low_ctx["spotify_identity_score"] == 0
    assert low_ctx["spotify_identity_tier"] == 3
    assert low_ctx["spotify_identity"]["reasons"] == ()
    assert mid_ctx["spotify_identity_score"] == 4
    assert mid_ctx["spotify_identity_tier"] == 2
    assert set(mid_ctx["spotify_identity"]["reasons"]) == {
        "website_candidate",
        "location",
        "genre",
    }


def test_spotify_discovery_pass_populates_facebook_identity_for_higher_tier_spotify_row(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"count": 0}

    def fake_discover(*args, **kwargs):
        discovered["count"] += 1
        return "https://www.facebook.com/artist-a"

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is True
    assert discovered["count"] == 1
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/artist-a"


def test_spotify_discovery_pass_skips_facebook_identity_for_low_tier_spotify_row(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"count": 0}

    def fake_discover(*args, **kwargs):
        discovered["count"] += 1
        return "https://www.facebook.com/artist-a"

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is False
    assert ctx["spotify_identity_tier"] == 3
    assert discovered["count"] == 0
    assert df.at[0, "Facebook_URL"] == ""
    assert worker._spotify_low_tier_fb_skips == 1


def test_spotify_discovery_pass_does_not_overwrite_existing_facebook_url(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    existing = "https://www.facebook.com/existing-artist"
    df = pd.DataFrame([_base_row(Facebook_URL=existing, facebook_url=existing)])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"count": 0}

    def fake_discover(*args, **kwargs):
        discovered["count"] += 1
        return "https://www.facebook.com/weaker-match"

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is False
    assert discovered["count"] == 0
    assert df.at[0, "Facebook_URL"] == existing
    assert df.at[0, "facebook_url"] == existing


def test_spotify_discovery_pass_expands_link_hub_urls(tmp_path):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row(**{"External Links": "https://linktr.ee/artist-a"})])
    ctx = worker._build_row_context(df, 0, 1, 1)
    fetches = {"count": 0}

    class _FakeResponse:
        text = """
        <html><body>
        <a href="https://www.instagram.com/artista/">Instagram</a>
        <a href="https://artist.test">Website</a>
        </body></html>
        """

        def raise_for_status(self):
            return None

    def fake_get(url, timeout=None):
        fetches["count"] += 1
        assert url == "https://linktr.ee/artist-a"
        return _FakeResponse()

    worker.session.get = fake_get

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert fetches["count"] == 1
    assert "https://www.instagram.com/artista" in df.at[0, "Social Link"]
    assert "https://artist.test" in df.at[0, "External Links"]


def test_spotify_discovery_pass_runs_once_per_row(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame(
        [
            _base_row(
                Spotify_Website_URL="https://artist.test",
                Location="Melbourne",
                **{"External Links": "https://linktr.ee/artist-a"},
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    hub_fetches = {"count": 0}
    fb_discovers = {"count": 0}

    class _FakeResponse:
        text = '<html><body><a href="https://artist.test">Website</a></body></html>'

        def raise_for_status(self):
            return None

    def fake_get(url, timeout=None):
        hub_fetches["count"] += 1
        return _FakeResponse()

    def fake_discover(*args, **kwargs):
        fb_discovers["count"] += 1
        return "https://www.facebook.com/artist-a"

    worker.session.get = fake_get
    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    first = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())
    second = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert first is True
    assert second is False
    assert hub_fetches["count"] == 1
    assert fb_discovers["count"] == 1


def test_spotify_discovery_pass_skips_live_recovery_for_low_tier_spotify_row(tmp_path, monkeypatch):
    recovery_calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0}
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)

    monkeypatch.setattr(
        worker,
        "_live_search_bandcamp",
        lambda _artist: recovery_calls.__setitem__("bandcamp", recovery_calls["bandcamp"] + 1),
    )
    monkeypatch.setattr(
        worker,
        "_night_sc_attempt_row",
        lambda *args, **kwargs: recovery_calls.__setitem__("soundcloud", recovery_calls["soundcloud"] + 1),
    )
    monkeypatch.setattr(
        worker,
        "_live_search_lastfm",
        lambda _artist: recovery_calls.__setitem__("lastfm", recovery_calls["lastfm"] + 1),
    )

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is False
    assert ctx["spotify_identity_tier"] == 3
    assert recovery_calls == {"bandcamp": 0, "soundcloud": 0, "lastfm": 0}
    assert worker._spotify_low_tier_recovery_skips == 1


def test_spotify_discovery_pass_recovers_bandcamp_before_other_live_sources(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"soundcloud": 0, "lastfm": 0}

    monkeypatch.setattr(
        worker,
        "_live_search_bandcamp",
        lambda artist: cde.EnrichmentPayload(
            source_dir="bandcamp_directory",
            source_url="https://artist-a.bandcamp.com/",
            source_detail="Bandcamp Directory",
            match_score=0.95,
            candidate_name=artist,
        ),
    )
    monkeypatch.setattr(
        worker,
        "_night_sc_attempt_row",
        lambda *args, **kwargs: calls.__setitem__("soundcloud", calls["soundcloud"] + 1),
    )
    monkeypatch.setattr(
        worker,
        "_live_search_lastfm",
        lambda artist: calls.__setitem__("lastfm", calls["lastfm"] + 1),
    )

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
    assert calls["soundcloud"] == 0
    assert calls["lastfm"] == 0


def test_spotify_discovery_pass_recovers_soundcloud_before_lastfm(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"bandcamp": 0, "lastfm": 0}

    def fake_bandcamp(_artist):
        calls["bandcamp"] += 1
        return None

    def fake_soundcloud(seed_df, row_idx, artist_name, spotify_id=""):
        seed_df.at[row_idx, "SoundCloud Link"] = "https://soundcloud.com/artist-a"
        return True

    def fake_lastfm(_artist):
        calls["lastfm"] += 1
        return None

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "SoundCloud Link"] == "https://soundcloud.com/artist-a"
    assert calls["bandcamp"] == 1
    assert calls["lastfm"] == 0


def test_spotify_discovery_pass_recovers_lastfm_via_apply_payload_guarded(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    apply_calls = []
    lastfm_payload = cde.EnrichmentPayload(
        socials={"https://www.instagram.com/artista/"},
        websites={"https://artist.test"},
        source_dir="lastfm_directory",
        source_url="https://www.last.fm/music/Artist+A",
        source_detail="Last.fm Directory",
        match_score=0.95,
        candidate_name="Artist A",
    )
    original_apply_payload_guarded = worker._apply_payload_guarded

    def wrapped_apply_payload_guarded(seed_df, row_idx, payload, artist_name, spotify_id=""):
        apply_calls.append(payload)
        return original_apply_payload_guarded(seed_df, row_idx, payload, artist_name, spotify_id=spotify_id)

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda artist: None)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda artist: lastfm_payload)
    monkeypatch.setattr(worker, "_apply_payload_guarded", wrapped_apply_payload_guarded)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert apply_calls == [lastfm_payload]
    assert "https://artist.test" in df.at[0, "External Links"]
    assert "https://www.instagram.com/artista" in df.at[0, "Social Link"]


def test_spotify_discovery_pass_skips_live_recovery_when_gated(tmp_path, monkeypatch):
    recovery_calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0}

    def fake_bandcamp(_artist):
        recovery_calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        recovery_calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        recovery_calls["lastfm"] += 1
        return None

    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame(
        [
            _base_row(
                Spotify_URL="",
                Spotify_Artist_ID="",
                **{"Source Directory": "bandcamp"},
            ),
            _base_row(
                Bandcamp_URL="https://artist-a.bandcamp.com/",
                **{"SoundCloud Link": "https://soundcloud.com/artist-a"},
                Facebook_URL="https://www.facebook.com/artist-a",
                facebook_url="https://www.facebook.com/artist-a",
            ),
            _base_row(),
        ]
    )
    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)

    ctx_non_spotify = worker._build_row_context(df, 0, 1, len(df))
    ctx_not_sparse = worker._build_row_context(df, 1, 2, len(df))
    ctx_live_disabled = worker._build_row_context(df, 2, 3, len(df))

    worker._run_spotify_discovery_pass(df, 0, ctx_non_spotify, fb_driver=None)
    worker._run_spotify_discovery_pass(df, 1, ctx_not_sparse, fb_driver=None)
    worker.enable_live_search = False
    worker._run_spotify_discovery_pass(df, 2, ctx_live_disabled, fb_driver=None)

    assert recovery_calls == {"bandcamp": 0, "soundcloud": 0, "lastfm": 0}


def test_spotify_discovery_pass_recovery_initializes_missing_row_state(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    if hasattr(worker, "_row_enrichment_state"):
        delattr(worker, "_row_enrichment_state")
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    state_seen = {}

    def fake_bandcamp(artist):
        state_seen["bandcamp"] = worker._row_enrichment_state.get("bandcamp")
        state_seen["soundcloud"] = worker._row_enrichment_state.get("soundcloud")
        state_seen["lastfm"] = worker._row_enrichment_state.get("lastfm")
        return cde.EnrichmentPayload(
            source_dir="bandcamp_directory",
            source_url="https://artist-a.bandcamp.com/",
            source_detail="Bandcamp Directory",
            match_score=0.95,
            candidate_name=artist,
        )

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda artist: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert state_seen == {
        "bandcamp": "pending",
        "soundcloud": "pending",
        "lastfm": "pending",
    }
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
