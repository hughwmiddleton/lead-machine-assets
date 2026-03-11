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


def test_spotify_discovery_pass_populates_facebook_identity(tmp_path, monkeypatch):
    worker = _build_worker(tmp_path)
    df = pd.DataFrame([_base_row()])
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
    df = pd.DataFrame([_base_row(**{"External Links": "https://linktr.ee/artist-a"})])
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
