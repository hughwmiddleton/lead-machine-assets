from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd

import cross_directory_enricher as cde
import pipeline_runner
import source_scheduler
from fb_attribution import FB_OPPORTUNITY_STATE_COL, apply_fb_opportunity_state_df


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
    worker._fb_session_auth_checked = True
    worker._fb_session_authenticated = True
    worker._fb_session_auth_reason = "authenticated"
    worker._fb_session_invalid = False
    worker._fb_discovery_disabled = False
    worker._fb_discovery_disabled_reason = ""
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


def _bandcamp_slug_html(title="Nightlight", by_artist="", og_title="", links=None):
    parts = ["<html><head>"]
    if title:
        parts.append(f"<title>{title}</title>")
    if og_title:
        parts.append(f'<meta property="og:title" content="{og_title}">')
    parts.append("</head><body>")
    if by_artist:
        parts.append(
            '<div itemprop="byArtist"><span itemprop="name">%s</span></div>' % by_artist
        )
    for link in links or ():
        parts.append(f'<a href="{link}">link</a>')
    parts.append("</body></html>")
    return "".join(parts)


def _build_bandcamp_slug_payload(url: str) -> cde.EnrichmentPayload:
    return cde.EnrichmentPayload(
        socials={"https://www.instagram.com/nightlightmusic/"},
        source_dir="bandcamp",
        source_url=url,
        source_detail="Bandcamp",
        candidate_name="Nightlight",
    )


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
    assert worker._spotify_identity_pass_attempted == 0
    assert worker._spotify_identity_pass_enriched == 0


def test_spotify_discovery_pass_prefers_song_title_signal_for_facebook_opportunity(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame(
        [_base_row(Location="Melbourne", **{"Song Title": "TAKE//OVER"})]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"calls": []}

    assert ctx["spotify_identity_tier"] == 3

    def fake_discover(_fb_driver, _artist_name, extra_signal, _logger):
        discovered["calls"].append(extra_signal)
        if extra_signal == "TAKE OVER":
            return "https://www.facebook.com/artist-a"
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())
    df = apply_fb_opportunity_state_df(df)

    assert enriched is True
    assert discovered["calls"] == ["TAKE OVER"]
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/artist-a"
    assert df.at[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert worker._spotify_low_tier_fb_skips == 0


def test_spotify_discovery_pass_allows_location_qualified_low_tier_spotify_row(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row(Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"count": 0}

    def fake_discover(*args, **kwargs):
        discovered["count"] += 1
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is False
    assert ctx["spotify_identity_tier"] == 3
    assert ctx["spotify_identity"]["reasons"] == ("location",)
    assert discovered["count"] == 1
    assert worker._spotify_low_tier_fb_skips == 0


def test_spotify_discovery_pass_normalizes_location_signal_for_facebook_discovery(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row(Location="Melbourne, VIC")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"signals": []}

    def fake_discover(_fb_driver, _artist_name, extra_signal, _logger):
        discovered["signals"].append(extra_signal)
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is False
    assert discovered["signals"] == ["Melbourne VIC"]
    assert df.at[0, "Facebook_URL"] == ""


def test_spotify_discovery_pass_normalized_location_can_create_facebook_opportunity(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row(Location="Melbourne, VIC")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"signals": []}

    def fake_discover(_fb_driver, _artist_name, extra_signal, _logger):
        discovered["signals"].append(extra_signal)
        if extra_signal == "Melbourne VIC":
            return "https://www.facebook.com/artist-a"
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())
    df = apply_fb_opportunity_state_df(df)

    assert enriched is True
    assert discovered["signals"] == ["Melbourne VIC"]
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/artist-a"
    assert df.at[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"


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


def test_spotify_discovery_pass_runs_identity_pass_before_low_tier_suppression(tmp_path, monkeypatch):
    recovery_calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0}
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(Spotify_Genres="indie pop")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    discovered = {"count": 0}

    monkeypatch.setattr(
        worker,
        "_live_search_bandcamp",
        lambda _artist: recovery_calls.__setitem__("bandcamp", recovery_calls["bandcamp"] + 1),
    )
    monkeypatch.setattr(
        worker,
        "_night_sc_attempt_row",
        lambda *args, **kwargs: recovery_calls.__setitem__("soundcloud", recovery_calls["soundcloud"] + 1) or False,
    )
    monkeypatch.setattr(
        worker,
        "_live_search_lastfm",
        lambda _artist: recovery_calls.__setitem__("lastfm", recovery_calls["lastfm"] + 1),
    )
    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: discovered.__setitem__("count", discovered["count"] + 1),
    )
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is False
    assert ctx["spotify_identity_tier"] == 3
    assert recovery_calls == {"bandcamp": 1, "soundcloud": 1, "lastfm": 1}
    assert discovered["count"] == 0
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 0
    assert worker._spotify_identity_pass_no_signal == 1
    assert worker._spotify_low_tier_fb_skips == 1
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
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

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
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "SoundCloud Link"] == "https://soundcloud.com/artist-a"
    assert calls["bandcamp"] == 1
    assert calls["lastfm"] == 0
    assert worker._spotify_identity_pass_attempted == 0


def test_spotify_discovery_pass_low_tier_identity_pass_promotes_soundcloud(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
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
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "SoundCloud Link"] == "https://soundcloud.com/artist-a"
    assert ctx["spotify_identity_tier"] == 2
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 1
    assert worker._spotify_identity_pass_no_signal == 0
    assert worker._spotify_identity_pass_promotions["soundcloud"] == 1
    assert calls["bandcamp"] == 1
    assert calls["lastfm"] == 0


def test_spotify_discovery_pass_refreshes_ctx_after_identity_pass_mutation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(**{"Artist Name": "XY"})])
    ctx = worker._build_row_context(df, 0, 1, 1)

    before = worker._row_allows_heavy_enricher(df.loc[0], ctx, "soundcloud")

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda _artist: None)
    monkeypatch.setattr(
        worker,
        "_night_sc_attempt_row",
        lambda seed_df, row_idx, artist_name, spotify_id="": seed_df.at.__setitem__(
            (row_idx, "SoundCloud Link"),
            "https://soundcloud.com/artist-a",
        ) or True,
    )
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)
    after = worker._row_allows_heavy_enricher(df.loc[0], ctx, "soundcloud")

    assert before.allowed is False
    assert after.allowed is True
    assert "explicit_soundcloud_link" in after.reasons
    assert ctx["signal_snapshot"]["soundcloud_link"] == "https://soundcloud.com/artist-a"
    assert worker._live_context["signal_snapshot"]["soundcloud_link"] == "https://soundcloud.com/artist-a"


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
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert apply_calls == [lastfm_payload]
    assert "https://artist.test" in df.at[0, "External Links"]
    assert "https://www.instagram.com/artista" in df.at[0, "Social Link"]


def test_spotify_identity_pass_attempts_instagram_recovery_before_no_signal(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(**{"External Links": "https://linktr.ee/artista"})])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "website_fetch": 0}

    def fake_bandcamp(_artist):
        calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        calls["lastfm"] += 1
        return None

    def fake_fetch(_session, url, *, timeout_s, max_bytes):
        calls["website_fetch"] += 1
        assert url == "https://linktr.ee/artista"
        return cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html='<html><body><a href="https://www.instagram.com/artista/">Instagram</a></body></html>',
            is_html=True,
        )

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/artista/"
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 1
    assert worker._spotify_identity_pass_no_signal == 0
    assert worker._spotify_identity_pass_promotions["instagram"] == 1
    assert calls == {"bandcamp": 1, "soundcloud": 1, "lastfm": 0, "website_fetch": 1}


def test_spotify_identity_pass_ig_miss_preserves_no_signal_and_lastfm_fallback(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(**{"External Links": "https://linktr.ee/artista"})])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "website_fetch": 0}

    def fake_bandcamp(_artist):
        calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        calls["lastfm"] += 1
        return None

    def fake_fetch(_session, url, *, timeout_s, max_bytes):
        calls["website_fetch"] += 1
        assert url == "https://linktr.ee/artista"
        return cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html='<html><body><a href="https://twitter.com/artista">Twitter</a></body></html>',
            is_html=True,
        )

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is False
    assert df.at[0, "Social Link"] == ""
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 0
    assert worker._spotify_identity_pass_no_signal == 1
    assert worker._spotify_identity_pass_promotions["instagram"] == 0
    assert calls == {"bandcamp": 1, "soundcloud": 1, "lastfm": 1, "website_fetch": 1}


def test_spotify_identity_pass_ig_no_website_noops_without_fetch(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(**{"Artist Name": "DJ", "Spotify_Artist_ID": "artist-opaque-id"})])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "website_fetch": 0}

    def fake_bandcamp(_artist):
        calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        calls["lastfm"] += 1
        return None

    def fake_fetch(*args, **kwargs):
        calls["website_fetch"] += 1
        raise AssertionError("website fetch should not run without a deterministic candidate")

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is False
    assert df.at[0, "Social Link"] == ""
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 0
    assert worker._spotify_identity_pass_no_signal == 1
    assert worker._spotify_identity_pass_promotions["instagram"] == 0
    assert calls == {"bandcamp": 1, "soundcloud": 1, "lastfm": 1, "website_fetch": 0}


def test_spotify_seed_instagram_identity_recovery_rejects_unvalidated_compact_guess(tmp_path):
    logs = []
    worker = _build_worker(tmp_path)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    spotify_id = "2PIlRxTYueV3iVxYMjSu9U"
    df = pd.DataFrame([_base_row(**{"Artist Name": "Artist A", "Spotify_Artist_ID": spotify_id})])

    candidates = cde._spotify_seed_instagram_candidate_urls(df.loc[0], "Artist A", spotify_id=spotify_id)
    applied = worker._run_spotify_seed_instagram_identity_recovery(
        df,
        0,
        "Artist A",
        spotify_id=spotify_id,
    )

    assert candidates == ["https://www.instagram.com/artista/"]
    assert applied is False
    assert df.at[0, "Social Link"] == ""
    assert logs == [
        "[Spotify IG Seed] candidate_rejected reason=identity_unverified url=https://www.instagram.com/artista/"
    ]


def test_spotify_seed_instagram_identity_recovery_accepts_trusted_external_source_url(tmp_path):
    logs = []
    worker = _build_worker(tmp_path)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    df = pd.DataFrame(
        [
            _base_row(
                **{
                    "Artist Name": "Artist A",
                    "Spotify_Artist_ID": "spotify-opaque-id",
                    "Spotify_Website_URL": "https://www.instagram.com/officialartist/",
                }
            )
        ]
    )

    candidates = cde._spotify_seed_instagram_candidate_urls(df.loc[0], "Artist A", spotify_id="spotify-opaque-id")
    applied = worker._run_spotify_seed_instagram_identity_recovery(
        df,
        0,
        "Artist A",
        spotify_id="spotify-opaque-id",
    )

    assert candidates == ["https://www.instagram.com/officialartist/"]
    assert applied is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/officialartist/"
    assert logs == [
        "[Spotify IG Seed] candidate_upgraded reason=external_source url=https://www.instagram.com/officialartist/"
    ]


def test_spotify_seed_instagram_identity_recovery_prefers_external_source_over_constructed_guess(tmp_path):
    logs = []
    worker = _build_worker(tmp_path)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    df = pd.DataFrame(
        [
            _base_row(
                **{
                    "Artist Name": "artista",
                    "Spotify_Artist_ID": "spotify-opaque-id",
                    "Spotify_Website_URL": "https://www.instagram.com/officialartist/",
                }
            )
        ]
    )

    candidates = cde._spotify_seed_instagram_candidate_urls(df.loc[0], "artista", spotify_id="spotify-opaque-id")
    applied = worker._run_spotify_seed_instagram_identity_recovery(
        df,
        0,
        "artista",
        spotify_id="spotify-opaque-id",
    )

    assert candidates == ["https://www.instagram.com/officialartist/"]
    assert applied is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/officialartist/"
    assert logs == [
        "[Spotify IG Seed] candidate_upgraded reason=external_source url=https://www.instagram.com/officialartist/"
    ]


def test_spotify_seed_instagram_identity_recovery_accepts_exact_external_corroboration(tmp_path):
    logs = []
    worker = _build_worker(tmp_path)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    df = pd.DataFrame(
        [
            _base_row(
                **{
                    "Artist Name": "Artist A",
                    "Spotify_Artist_ID": "spotify-opaque-id",
                    "Bandcamp_URL": "https://artista.bandcamp.com/",
                }
            )
        ]
    )

    candidates = cde._spotify_seed_instagram_candidate_urls(df.loc[0], "Artist A", spotify_id="spotify-opaque-id")
    applied = worker._run_spotify_seed_instagram_identity_recovery(
        df,
        0,
        "Artist A",
        spotify_id="spotify-opaque-id",
    )

    assert candidates == ["https://www.instagram.com/artista/"]
    assert applied is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/artista/"
    assert logs == [
        "[Spotify IG Seed] candidate_accepted reason=external_corroboration url=https://www.instagram.com/artista/"
    ]


def test_spotify_seed_instagram_identity_recovery_does_not_trust_ambiguous_row_instagram_origin(tmp_path):
    logs = []
    worker = _build_worker(tmp_path)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    spotify_id = "2PIlRxTYueV3iVxYMjSu9U"
    df = pd.DataFrame(
        [
            _base_row(
                **{
                    "Artist Name": "Artist A",
                    "Spotify_Artist_ID": spotify_id,
                    "Source URL": "https://www.instagram.com/officialartist/",
                    "Source Directory": "spotify",
                }
            )
        ]
    )

    candidates = cde._spotify_seed_instagram_candidate_urls(df.loc[0], "Artist A", spotify_id=spotify_id)
    applied = worker._run_spotify_seed_instagram_identity_recovery(
        df,
        0,
        "Artist A",
        spotify_id=spotify_id,
    )

    assert candidates == ["https://www.instagram.com/artista/"]
    assert applied is False
    assert df.at[0, "Social Link"] == ""
    assert logs == [
        "[Spotify IG Seed] candidate_rejected reason=identity_unverified url=https://www.instagram.com/artista/"
    ]


def test_spotify_identity_pass_generates_seed_instagram_before_no_signal_without_website(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(**{"Artist Name": "artista", "Spotify_Artist_ID": "spotify-opaque-id"})])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "website_fetch": 0}

    def fake_bandcamp(_artist):
        calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        calls["lastfm"] += 1
        return None

    def fake_fetch(*args, **kwargs):
        calls["website_fetch"] += 1
        raise AssertionError("website fetch should not run when seed Instagram recovery succeeds")

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/artista/"
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 1
    assert worker._spotify_identity_pass_no_signal == 0
    assert worker._spotify_identity_pass_promotions["instagram"] == 1
    assert calls == {"bandcamp": 1, "soundcloud": 1, "lastfm": 0, "website_fetch": 0}


def test_spotify_seed_instagram_validated_candidate_still_runs_shared_extractor(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    logs = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    df = pd.DataFrame([_base_row(**{"Artist Name": "artista", "Spotify_Artist_ID": "spotify-opaque-id"})])
    ctx = worker._build_row_context(df, 0, 1, 1)

    applied = worker._run_spotify_seed_instagram_identity_recovery(
        df,
        0,
        "artista",
        spotify_id="spotify-opaque-id",
    )

    assert applied is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/artista/"

    @contextmanager
    def fake_profile_fetch_scope(session, url, retain_live_page=False):  # noqa: ANN001
        assert url == "https://www.instagram.com/artista/"
        assert retain_live_page is False
        yield cde.InstagramProfileFetchResult(
            html='<html><head><meta property="og:description" content="bookings@artist.com"></head></html>',
            status=200,
        )

    monkeypatch.setattr(cde, "_instagram_profile_fetch_scope", fake_profile_fetch_scope)
    monkeypatch.setattr(cde, "_open_instagram_live_page_bridge", lambda *args, **kwargs: None)

    matched = worker._enrich_row_instagram_email(df, 0, ctx)

    assert matched is True
    assert df.at[0, "Email"] == "bookings@artist.com"
    assert df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert logs[0] == "[Spotify IG Seed] candidate_accepted reason=identity_validated url=https://www.instagram.com/artista/"
    assert "[IG Email] Visiting https://www.instagram.com/artista/" in logs
    assert "[IG Email] Found email: bookings@artist.com" in logs


def test_spotify_identity_pass_website_instagram_recovery_still_runs_after_seed_candidate_miss(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    logs = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame(
        [_base_row(**{"Artist Name": "DJ", "Spotify_Artist_ID": "artist-opaque-id", "External Links": "https://linktr.ee/artista"})]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "website_fetch": 0}

    def fake_bandcamp(_artist):
        calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        calls["lastfm"] += 1
        return None

    def fake_fetch(_session, url, *, timeout_s, max_bytes):
        calls["website_fetch"] += 1
        assert url == "https://linktr.ee/artista"
        return cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html='<html><body><a href="https://www.instagram.com/artista/">Instagram</a></body></html>',
            is_html=True,
        )

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", fake_soundcloud)
    monkeypatch.setattr(worker, "_live_search_lastfm", fake_lastfm)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "Social Link"] == "https://www.instagram.com/artista/"
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 1
    assert worker._spotify_identity_pass_no_signal == 0
    assert worker._spotify_identity_pass_promotions["instagram"] == 1
    assert calls == {"bandcamp": 1, "soundcloud": 1, "lastfm": 0, "website_fetch": 1}
    assert "[Spotify IG Seed] candidate_upgraded reason=external_source url=https://www.instagram.com/artista/" in logs


def test_spotify_identity_pass_allows_conservative_low_score_bandcamp_promotion(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=cde.MATCH_THRESHOLD - 0.05,
        candidate_name="Artist A",
    )

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda _artist: payload)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 1
    assert worker._spotify_identity_pass_no_signal == 0
    assert worker._spotify_identity_pass_promotions["bandcamp"] == 1


def test_spotify_identity_pass_allows_borderline_bandcamp_slug_supported_promotion(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=cde.MATCH_THRESHOLD - 0.05,
        candidate_name="Artist A on Bandcamp",
    )

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda _artist: payload)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 1
    assert worker._spotify_identity_pass_no_signal == 0
    assert worker._spotify_identity_pass_promotions["bandcamp"] == 1


def test_spotify_identity_pass_still_blocks_too_low_bandcamp_slug_supported_payload(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=cde.MATCH_THRESHOLD - 0.15,
        candidate_name="Artist A on Bandcamp",
    )

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda _artist: payload)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is False
    assert df.at[0, "Bandcamp_URL"] == ""
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 0
    assert worker._spotify_identity_pass_no_signal == 1


def test_spotify_identity_pass_still_blocks_ambiguous_low_score_payload(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://wrong-artist.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=cde.MATCH_THRESHOLD - 0.05,
        candidate_name="Wrong Artist",
    )

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda _artist: payload)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("facebook discovery should stay skipped")),
    )

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())

    assert enriched is False
    assert df.at[0, "Bandcamp_URL"] == ""
    assert ctx["spotify_identity_tier"] == 3
    assert worker._spotify_identity_pass_attempted == 1
    assert worker._spotify_identity_pass_enriched == 0
    assert worker._spotify_identity_pass_no_signal == 1


def test_spotify_identity_pass_propagates_promoted_identity_to_same_run_facebook_logic(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    payload = cde.EnrichmentPayload(
        websites={"https://artist.test"},
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=cde.MATCH_THRESHOLD - 0.05,
        candidate_name="Artist A",
    )
    discover_calls = {"count": 0}

    def fake_discover(*args, **kwargs):
        discover_calls["count"] += 1
        return "https://www.facebook.com/artist-a"

    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda _artist: payload)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=object())
    df = apply_fb_opportunity_state_df(df)

    assert enriched is True
    assert discover_calls["count"] == 1
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/artist-a"
    assert df.at[0, FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert ctx["spotify_identity_tier"] != 3
    assert worker._live_context["spotify_identity_tier"] != 3
    assert "https://artist.test" in df.at[0, "External Links"]


def test_low_score_guard_still_blocks_non_spotify_and_unearthed_rows(tmp_path):
    worker = _build_worker(tmp_path)
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=cde.MATCH_THRESHOLD - 0.05,
        candidate_name="Artist A",
    )

    non_spotify_df = pd.DataFrame(
        [_base_row(Spotify_URL="", Spotify_Artist_ID="", **{"Source Directory": "bandcamp"})]
    )
    unearthed_df = pd.DataFrame(
        [
            _base_row(
                Spotify_URL="",
                Spotify_Artist_ID="",
                **{"Source Directory": "unearthed", "Played on Unearthed": "Yes"},
            )
        ]
    )

    assert worker._apply_payload_guarded(non_spotify_df, 0, payload, "Artist A") is False
    assert worker._apply_payload_guarded(unearthed_df, 0, payload, "Artist A") is False
    assert non_spotify_df.at[0, "Bandcamp_URL"] == ""
    assert unearthed_df.at[0, "Bandcamp_URL"] == ""


def test_spotify_discovery_pass_skips_live_recovery_when_gated(tmp_path, monkeypatch):
    recovery_calls = {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "spotify_bc_slug": 0}

    def fake_bandcamp(_artist):
        recovery_calls["bandcamp"] += 1
        return None

    def fake_soundcloud(*args, **kwargs):
        recovery_calls["soundcloud"] += 1
        return False

    def fake_lastfm(_artist):
        recovery_calls["lastfm"] += 1
        return None

    def fake_spotify_bc_slug(*args, **kwargs):
        recovery_calls["spotify_bc_slug"] += 1
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
    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_spotify_bc_slug)

    ctx_non_spotify = worker._build_row_context(df, 0, 1, len(df))
    ctx_not_sparse = worker._build_row_context(df, 1, 2, len(df))
    ctx_live_disabled = worker._build_row_context(df, 2, 3, len(df))

    worker._run_spotify_discovery_pass(df, 0, ctx_non_spotify, fb_driver=None)
    worker._run_spotify_discovery_pass(df, 1, ctx_not_sparse, fb_driver=None)
    worker.enable_live_search = False
    worker._run_spotify_discovery_pass(df, 2, ctx_live_disabled, fb_driver=None)

    assert recovery_calls == {"bandcamp": 0, "soundcloud": 0, "lastfm": 0, "spotify_bc_slug": 0}


def test_spotify_discovery_pass_recovery_initializes_missing_row_state(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    if hasattr(worker, "_row_enrichment_state"):
        delattr(worker, "_row_enrichment_state")
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    state_seen = {}

    def fake_bandcamp_slug(artist, song_title, slug_candidates=None):
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

    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bandcamp_slug)
    monkeypatch.setattr(worker, "_live_search_bandcamp", lambda artist: None)
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


def test_spotify_discovery_pass_reopens_stale_bandcamp_skip_for_spotify_recovery(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    worker._row_enrichment_state = {
        "bandcamp": "skipped",
        "soundcloud": "matched",
        "lastfm": "skipped",
    }
    df = pd.DataFrame([_base_row()])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"live_bandcamp": 0, "spotify_bc_slug": 0}
    state_seen = {}

    def fake_live_search_bandcamp(_artist):
        calls["live_bandcamp"] += 1
        state_seen["bandcamp"] = worker._row_enrichment_state.get("bandcamp")
        state_seen["soundcloud"] = worker._row_enrichment_state.get("soundcloud")
        state_seen["lastfm"] = worker._row_enrichment_state.get("lastfm")
        worker._set_platform_state("bandcamp", "skipped")
        return None

    def fake_bandcamp_slug(*args, **kwargs):
        calls["spotify_bc_slug"] += 1
        return cde.EnrichmentPayload(
            source_dir="bandcamp_directory",
            source_url="https://artist-a.bandcamp.com/",
            source_detail="Bandcamp Directory",
            match_score=0.95,
            candidate_name="Artist A",
        )

    monkeypatch.setattr(worker, "_live_search_bandcamp", fake_live_search_bandcamp)
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bandcamp_slug)

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is True
    assert calls == {"live_bandcamp": 1, "spotify_bc_slug": 1}
    assert state_seen == {
        "bandcamp": "pending",
        "soundcloud": "matched",
        "lastfm": "skipped",
    }
    assert worker._row_enrichment_state["soundcloud"] == "matched"
    assert worker._row_enrichment_state["lastfm"] == "skipped"
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"


def test_spotify_discovery_pass_keeps_matched_bandcamp_terminal(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    worker._row_enrichment_state = {
        "bandcamp": "matched",
        "soundcloud": "pending",
        "lastfm": "pending",
    }
    df = pd.DataFrame([_base_row(Bandcamp_URL="https://existing.bandcamp.com/")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"live_bandcamp": 0}
    original_live_search_bandcamp = worker._live_search_bandcamp

    def wrapped_live_search_bandcamp(artist):
        calls["live_bandcamp"] += 1
        return original_live_search_bandcamp(artist)

    monkeypatch.setattr(worker, "_live_search_bandcamp", wrapped_live_search_bandcamp)
    monkeypatch.setattr(
        worker,
        "_increment_live_counter",
        lambda: (_ for _ in ()).throw(AssertionError("matched bandcamp should stay terminal")),
    )
    monkeypatch.setattr(worker, "_night_sc_attempt_row", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker, "_live_search_lastfm", lambda _artist: None)
    monkeypatch.setattr(
        worker,
        "_bc_slug_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("existing bandcamp should not rerun slug recovery")),
    )

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is False
    assert calls == {"live_bandcamp": 1}
    assert df.at[0, "Bandcamp_URL"] == "https://existing.bandcamp.com/"
    assert worker._row_enrichment_state["bandcamp"] == "matched"


def test_spotify_discovery_pass_does_not_reopen_bandcamp_for_non_spotify_rows(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    worker._row_enrichment_state = {"bandcamp": "skipped"}
    df = pd.DataFrame(
        [
            _base_row(
                Spotify_URL="",
                Spotify_Artist_ID="",
                **{"Source Directory": "bandcamp"},
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)

    monkeypatch.setattr(
        worker,
        "_live_search_bandcamp",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("non-spotify rows should not retry bandcamp")),
    )
    monkeypatch.setattr(
        worker,
        "_bc_slug_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("non-spotify rows should not enter spotify slug recovery")),
    )

    enriched = worker._run_spotify_discovery_pass(df, 0, ctx, fb_driver=None)

    assert enriched is False
    assert worker._row_enrichment_state["bandcamp"] == "skipped"


def test_spotify_sparse_bandcamp_recovery_allows_identity_links_when_bandcamp_missing(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=0.95,
        candidate_name="Artist A",
    )
    df = pd.DataFrame(
        [
            _base_row(
                Spotify_Website_URL="https://artist.test",
                Location="Melbourne",
                **{"External Links": "https://artist.test", "Social Link": "https://www.instagram.com/artista/"},
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"count": 0}

    def fake_bc_slug(*args, **kwargs):
        calls["count"] += 1
        return payload

    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bc_slug)

    recovered = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)

    assert recovered is True
    assert calls["count"] == 1
    assert df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"


def test_spotify_sparse_bandcamp_recovery_fills_empty_bandcamp_url_only(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    payload = cde.EnrichmentPayload(
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=0.95,
        candidate_name="Artist A",
    )

    empty_df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    empty_ctx = worker._build_row_context(empty_df, 0, 1, 1)
    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: payload)

    recovered = worker._run_spotify_sparse_bandcamp_recovery(empty_df, 0, empty_ctx)

    assert recovered is True
    assert empty_df.at[0, "Bandcamp_URL"] == "https://artist-a.bandcamp.com"

    existing_worker = _build_worker(tmp_path)
    existing_worker.enable_live_search = True
    existing_worker.max_live_searches = 5
    existing_df = pd.DataFrame(
        [
            _base_row(
                Spotify_Website_URL="https://artist.test",
                Location="Melbourne",
                Bandcamp_URL="https://existing.bandcamp.com/",
            )
        ]
    )
    existing_ctx = existing_worker._build_row_context(existing_df, 0, 1, 1)
    calls = {"count": 0}

    def fake_existing_bc_slug(*args, **kwargs):
        calls["count"] += 1
        return payload

    monkeypatch.setattr(existing_worker, "_bc_slug_fallback", fake_existing_bc_slug)
    skipped = existing_worker._run_spotify_sparse_bandcamp_recovery(existing_df, 0, existing_ctx)

    assert skipped is False
    assert calls["count"] == 0
    assert existing_df.at[0, "Bandcamp_URL"] == "https://existing.bandcamp.com/"


def test_spotify_sparse_bandcamp_recovery_runs_once_per_row(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"count": 0}

    def fake_bc_slug(*args, **kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bc_slug)

    first = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)
    second = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)

    assert first is False
    assert second is False
    assert calls["count"] == 1


def test_spotify_sparse_bandcamp_recovery_ignores_ambiguous_candidates(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame([_base_row(Spotify_Website_URL="https://artist.test", Location="Melbourne")])
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"count": 0}

    def fake_bc_slug(*args, **kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bc_slug)

    recovered = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)

    assert recovered is False
    assert calls["count"] == 1
    assert df.at[0, "Bandcamp_URL"] == ""


def test_spotify_sparse_bandcamp_recovery_skips_non_spotify_rows(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame(
        [
            _base_row(
                **{
                    "Source Directory": "bandcamp",
                    "Spotify_URL": "",
                    "Spotify_Artist_ID": "",
                    "Spotify_Website_URL": "https://artist.test",
                    "Location": "Melbourne",
                    "External Links": "https://artist.test",
                    "Social Link": "https://www.instagram.com/artista/",
                }
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    calls = {"count": 0}

    def fake_bc_slug(*args, **kwargs):
        calls["count"] += 1
        return cde.EnrichmentPayload(
            source_dir="bandcamp_directory",
            source_url="https://artist-a.bandcamp.com/",
            source_detail="Bandcamp Directory",
            match_score=0.95,
            candidate_name="Artist A",
        )

    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bc_slug)

    recovered = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)

    assert recovered is False
    assert calls["count"] == 0
    assert df.at[0, "Bandcamp_URL"] == ""


def test_spotify_sparse_bandcamp_slug_candidates_preserve_compact_and_add_hyphenated_suffixes():
    candidates = cde._spotify_sparse_bandcamp_slug_candidates("Nightlight")

    assert "nightlightmusic" in candidates
    assert "nightlightband" in candidates
    assert "nightlight-music" in candidates
    assert "nightlight-band" in candidates


def test_spotify_sparse_bandcamp_slug_candidates_remain_bounded_and_deduped():
    candidates = cde._spotify_sparse_bandcamp_slug_candidates("Nightlight")

    assert len(candidates) <= cde.SPOTIFY_BC_RECOVERY_MAX_SLUGS
    assert len(candidates) == len(set(candidates))


def test_spotify_sparse_bandcamp_recovery_hands_hyphenated_suffix_candidates_to_lookup(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame(
        [
            _base_row(
                **{
                    "Artist Name": "Nightlight",
                    "Spotify_Website_URL": "https://artist.test",
                    "Location": "Melbourne",
                }
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    seen = {}

    def fake_bc_slug(artist, song_title, slug_candidates=None):
        seen["artist"] = artist
        seen["song_title"] = song_title
        seen["slug_candidates"] = list(slug_candidates or [])
        return cde.EnrichmentPayload(
            source_dir="bandcamp_directory",
            source_url="https://nightlight-music.bandcamp.com/",
            source_detail="Bandcamp Directory",
            match_score=0.95,
            candidate_name=artist,
        )

    monkeypatch.setattr(worker, "_bc_slug_fallback", fake_bc_slug)

    recovered = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)

    assert recovered is True
    assert seen["artist"] == "Nightlight"
    assert seen["song_title"] == ""
    assert "nightlight-music" in seen["slug_candidates"]
    assert len(seen["slug_candidates"]) <= cde.SPOTIFY_BC_RECOVERY_MAX_SLUGS


def test_bc_slug_fallback_accepts_artist_style_name_variant(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", by_artist="Nightlight Music")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))

    def fake_fetch_profile(url, source_dir, confidence=None):
        fetch_calls.append((url, source_dir, confidence))
        return _build_bandcamp_slug_payload(url)

    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_fetch_profile)

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is not None
    assert fetch_calls and fetch_calls[0][0] == "https://nightlight.bandcamp.com/"


def test_bc_slug_fallback_accepts_official_prefix_artist_name(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", by_artist="Official Nightlight")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))

    def fake_fetch_profile(url, source_dir, confidence=None):
        fetch_calls.append(url)
        return _build_bandcamp_slug_payload(url)

    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_fetch_profile)

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is not None
    assert fetch_calls == ["https://nightlight.bandcamp.com/"]


def test_bc_slug_fallback_accepts_outbound_confirmation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(
        title="Nightlight",
        og_title="Nightlight on Bandcamp",
        links=["https://www.instagram.com/nightlightmusic/"],
    )
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))

    def fake_fetch_profile(url, source_dir, confidence=None):
        fetch_calls.append(url)
        return _build_bandcamp_slug_payload(url)

    monkeypatch.setattr(worker, "_fetch_profile_and_build", fake_fetch_profile)

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is not None
    assert fetch_calls == ["https://nightlight.bandcamp.com/"]


def test_bc_slug_fallback_accepts_sparse_suffix_slug_when_display_name_confirms_artist(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", og_title="Nightlight Archive")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight-music"])

    assert payload is not None
    assert fetch_calls == ["https://nightlight-music.bandcamp.com/"]


def test_bc_slug_fallback_rejects_records_variant_without_outbound_confirmation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", by_artist="Nightlight Records")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is None
    assert fetch_calls == []


def test_bc_slug_fallback_rejects_sparse_suffix_records_variant_without_outbound_confirmation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", og_title="Nightlight Records")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight-music"])

    assert payload is None
    assert fetch_calls == []


def test_bc_slug_fallback_rejects_dj_variant_without_outbound_confirmation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", og_title="DJ Nightlight")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is None
    assert fetch_calls == []


def test_bc_slug_fallback_rejects_sparse_suffix_collective_variant_without_outbound_confirmation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", og_title="Nightlight Collective")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight-music"])

    assert payload is None
    assert fetch_calls == []


def test_bc_slug_fallback_rejects_weak_sparse_slug_without_supplemental_confirmation(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(title="Nightlight", og_title="Nightlight Archive")
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is None
    assert fetch_calls == []


def test_bc_slug_fallback_rejects_outbound_mismatch(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    html = _bandcamp_slug_html(
        title="Nightlight",
        og_title="Nightlight on Bandcamp",
        links=["https://www.instagram.com/nightlightrecords/"],
    )
    fetch_calls = []

    monkeypatch.setattr(worker, "_bc_http_get", lambda *args, **kwargs: (html, 200))
    monkeypatch.setattr(
        worker,
        "_fetch_profile_and_build",
        lambda url, source_dir, confidence=None: fetch_calls.append(url) or _build_bandcamp_slug_payload(url),
    )

    payload = worker._bc_slug_fallback("Nightlight", "", slug_candidates=["nightlight"])

    assert payload is None
    assert fetch_calls == []


def test_spotify_sparse_bandcamp_recovery_routes_links_via_apply_payload_guarded(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    worker.enable_live_search = True
    worker.max_live_searches = 5
    df = pd.DataFrame(
        [
            _base_row(
                Spotify_Website_URL="https://artist.test",
                Location="Melbourne",
                **{"Social Link": "https://www.instagram.com/existing/"},
            )
        ]
    )
    ctx = worker._build_row_context(df, 0, 1, 1)
    payload = cde.EnrichmentPayload(
        socials={"https://www.facebook.com/artist-a/"},
        websites={"https://artist.test/contact"},
        source_dir="bandcamp_directory",
        source_url="https://artist-a.bandcamp.com/",
        source_detail="Bandcamp Directory",
        match_score=0.95,
        candidate_name="Artist A",
    )
    apply_calls = []
    original_apply_payload_guarded = worker._apply_payload_guarded

    def wrapped_apply_payload_guarded(seed_df, row_idx, payload_obj, artist_name, spotify_id=""):
        apply_calls.append(payload_obj)
        return original_apply_payload_guarded(seed_df, row_idx, payload_obj, artist_name, spotify_id=spotify_id)

    monkeypatch.setattr(worker, "_bc_slug_fallback", lambda *args, **kwargs: payload)
    monkeypatch.setattr(worker, "_apply_payload_guarded", wrapped_apply_payload_guarded)

    recovered = worker._run_spotify_sparse_bandcamp_recovery(df, 0, ctx)

    assert recovered is True
    assert apply_calls == [payload]
    assert "https://artist.test/contact" in df.at[0, "External Links"]
    assert "https://www.facebook.com/artist-a" in df.at[0, "Social Link"]
    assert "https://www.instagram.com/existing" in df.at[0, "Social Link"]
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/artist-a"
