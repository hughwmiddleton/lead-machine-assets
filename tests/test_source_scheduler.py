import pytest
import source_scheduler

from source_scheduler import (
    ADAPTIVE_PRIORITY_MAX_BONUS,
    SourceDiversityScheduler,
    SourceResult,
    SourceSpec,
    TimedRetry,
    preferred_upstream_identity_hint,
    promote_facebook_url,
)


def test_scheduler_interleaves_sources():
    calls = []
    rows = [0, 1, 2]

    def make_spec(name):
        return SourceSpec(
            name=name,
            rows=rows,
            run_row=lambda idx: (calls.append(f"{name}{idx}") or SourceResult(attempted=True)),
            is_available=lambda: (True, None),
        )

    scheduler = SourceDiversityScheduler(
        [make_spec("SC"), make_spec("LF"), make_spec("FB")],
        row_label=str,
    )
    scheduler.run()

    # Expect non-bursty order: first three calls touch all sources.
    assert calls[:3] == ["SC0", "LF0", "FB0"]
    # Ensure sequence alternates by source, not SC-only burst.
    assert calls[:6] == ["SC0", "LF0", "FB0", "SC1", "LF1", "FB1"]


def test_scheduler_skips_on_cooldown_but_continues_other_sources():
    rows = [0, 1, 2, 3]
    sc_calls = []
    lf_calls = []
    sc_checks = {"count": 0}

    def sc_is_available():
        sc_checks["count"] += 1
        # After first two rows, SC enters cooldown.
        return (sc_checks["count"] <= 2, "cooldown" if sc_checks["count"] > 2 else None)

    sc_spec = SourceSpec(
        name="SC",
        rows=rows,
        run_row=lambda idx: (sc_calls.append(idx) or SourceResult(attempted=True)),
        is_available=sc_is_available,
    )
    lf_spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: (lf_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
    )

    summary = SourceDiversityScheduler([sc_spec, lf_spec], row_label=str).run()

    assert sc_calls == [0, 1]
    assert summary["SC"]["skipped_cooldown"] == len(rows) - len(sc_calls)
    # LF should still process all rows.
    assert lf_calls == rows


def test_scheduler_attempt_counts_match_rows():
    rows = [5, 6, 7, 8]
    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: SourceResult(attempted=True, enriched=(idx % 2 == 0)),
        is_available=lambda: (True, None),
    )
    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert summary["LF"]["attempted"] == len(rows)
    assert summary["LF"]["enriched"] == 2  # rows 6 and 8 (even idx) considered enriched
    assert summary["LF"]["skipped_cooldown"] == 0


def test_row_source_lock_prevents_repeat_attempts():
    rows = [0, 0, 1]
    calls = []

    def run_row(idx):
        calls.append(idx)
        if idx == 0:
            return SourceResult(attempted=True, enriched=False, retry_later=False)
        return SourceResult(attempted=True, enriched=True)

    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=run_row,
        is_available=lambda: (True, None),
    )

    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert calls == [0, 1]
    assert summary["LF"]["attempted"] == 2
    assert summary["LF"]["enriched"] == 1


def test_row_source_lock_does_not_apply_to_retry_later():
    rows = [0, 0, 1]
    calls = []
    row_zero_attempts = {"count": 0}

    def run_row(idx):
        calls.append(idx)
        if idx == 0:
            row_zero_attempts["count"] += 1
            return SourceResult(
                attempted=True,
                enriched=(row_zero_attempts["count"] > 1),
                retry_later=(row_zero_attempts["count"] == 1),
            )
        return SourceResult(attempted=True, enriched=True)

    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=run_row,
        is_available=lambda: (True, None),
    )

    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert calls == [0, 0, 1]
    assert summary["LF"]["attempted"] == 3
    assert summary["LF"]["enriched"] == 2


def test_preferred_upstream_identity_hint_prefers_soundcloud_handle():
    row = {
        "SoundCloud Link": "https://soundcloud.com/SignalHandle/tracks?utm_source=feed",
        "Bandcamp_URL": "https://signalartist.bandcamp.com/album/demo",
        "Source URL": "https://fallback.bandcamp.com/track/night-drive",
    }

    assert preferred_upstream_identity_hint(row) == "signalhandle"


def test_preferred_upstream_identity_hint_prefers_bandcamp_when_soundcloud_missing():
    row = {
        "SoundCloud Link": "",
        "Bandcamp_URL": "https://Night-Light.bandcamp.com/album/demo?from=discover",
        "Source URL": "",
    }

    assert preferred_upstream_identity_hint(row) == "night-light"


def test_preferred_upstream_identity_hint_falls_back_to_provider_source_url():
    row = {
        "SoundCloud Link": "",
        "Bandcamp_URL": "",
        "Source URL": "https://soundcloud.com/fallbackhandle/night-drive",
    }

    assert preferred_upstream_identity_hint(row) == "fallbackhandle"


def test_preferred_upstream_identity_hint_uses_shared_soundcloud_parser(monkeypatch):
    monkeypatch.setattr(source_scheduler, "soundcloud_handle_from_profile_url", lambda value: "sharedhandle")

    row = {"SoundCloud Link": "https://soundcloud.com/ignored", "Bandcamp_URL": "", "Source URL": ""}

    assert preferred_upstream_identity_hint(row) == "sharedhandle"


def test_preferred_upstream_identity_hint_uses_shared_bandcamp_parser(monkeypatch):
    monkeypatch.setattr(source_scheduler, "soundcloud_handle_from_profile_url", lambda value: None)
    monkeypatch.setattr(
        source_scheduler,
        "canonicalize_bandcamp_url",
        lambda value: "https://shared-band.bandcamp.com/album/demo",
    )

    row = {"SoundCloud Link": "", "Bandcamp_URL": "https://ignored.bandcamp.com", "Source URL": ""}

    assert preferred_upstream_identity_hint(row) == "shared-band"


def test_preferred_upstream_identity_hint_ignores_unusable_provider_urls():
    row = {
        "SoundCloud Link": "https://soundcloud.com/charts/top",
        "Bandcamp_URL": "https://blog.bandcamp.com/article",
        "Source URL": "not a url",
    }

    assert preferred_upstream_identity_hint(row) == ""


def test_preferred_upstream_identity_hint_rejects_help_and_search_style_provider_urls():
    row = {
        "SoundCloud Link": "https://soundcloud.com/search?q=signal",
        "Bandcamp_URL": "https://help.bandcamp.com/hc/en-us",
        "Source URL": "https://get.bandcamp.help/hc/en-us/articles/123",
    }

    assert preferred_upstream_identity_hint(row) == ""


def test_preferred_upstream_identity_hint_returns_empty_without_provider_signal():
    row = {
        "SoundCloud Link": "",
        "Bandcamp_URL": "",
        "Source URL": "https://example.com/artist",
    }

    assert preferred_upstream_identity_hint(row) == ""


def test_unearthed_soundcloud_source_link_promotes_into_soundcloud_field():
    import pandas as pd
    import cross_directory_enricher as cde

    df = pd.DataFrame(
        [
            {
                "Artist Name": "jski",
                "Source Directory": "Triple J Unearthed",
                "Social Link": "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P | https://www.instagram.com/jski",
                "External Links": "",
                "SoundCloud Link": "",
                "Bandcamp_URL": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = cde._apply_unearthed_platform_promotion_df(df.copy())

    assert promoted.at[0, "SoundCloud Link"] == "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P"


def test_unearthed_soundcloud_directory_discovery_skips_when_source_url_present(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.log_message = Log()
    worker._find_directory_matches = lambda *args, **kwargs: pytest.fail("same-platform discovery should be skipped")
    worker._payload_from_directory_matches = lambda *args, **kwargs: pytest.fail("payload build should be skipped")
    worker._apply_payload_guarded = lambda *args, **kwargs: pytest.fail("payload apply should be skipped")
    worker._apply_structured_fields = lambda *args, **kwargs: False

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "jski",
                "Source Directory": "Triple J Unearthed",
                "Social Link": "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P",
                "External Links": "",
                "SoundCloud Link": "",
                "Bandcamp_URL": "",
            }
        ],
        dtype=str,
    ).fillna("")
    seed_df = cde._apply_unearthed_platform_promotion_df(seed_df)

    result = worker._enrich_row_directories(
        seed_df,
        0,
        directory_indexes={"soundcloud": object()},
        priority=["soundcloud"],
        ctx={
            "artist": "jski",
            "key": "jski",
            "track_key": "",
            "spotify_id": "",
            "seed_links_by_source": {},
            "position": 1,
            "total": 1,
        },
    )

    assert result is False
    assert any("[SoundCloud] skipping discovery (Unearthed URL present)" in line for line in logs)


def test_unearthed_soundcloud_fallback_runs_when_seed_url_invalid(monkeypatch):
    import pandas as pd
    import types
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker.night_mode = False
    worker._sc_live_enrich_disabled = False
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: types.SimpleNamespace(allowed=True)
    live_calls = []
    worker._live_search_soundcloud = lambda artist: live_calls.append(artist) or None
    worker._mark_sc_blocked_row = lambda *args, **kwargs: False
    worker._apply_payload_guarded = lambda *args, **kwargs: False
    worker._set_platform_state = lambda *args, **kwargs: None
    worker._log_low_confidence_skip = lambda *args, **kwargs: None

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "jski",
                "Source Directory": "Triple J Unearthed",
                "Social Link": "https://soundcloud.com/search?q=jski",
                "External Links": "",
                "SoundCloud Link": "",
            }
        ],
        dtype=str,
    ).fillna("")
    seed_df = cde._apply_unearthed_platform_promotion_df(seed_df)

    enriched, skip_rest = worker._enrich_row_sc_live(
        seed_df,
        0,
        {"artist": "jski", "spotify_id": ""},
    )

    assert seed_df.at[0, "SoundCloud Link"] == ""
    assert enriched is False
    assert skip_rest is False
    assert live_calls == ["jski"]


def test_non_unearthed_soundcloud_social_link_does_not_change_sc_behavior(monkeypatch):
    import pandas as pd
    import types
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker.night_mode = False
    worker._sc_live_enrich_disabled = False
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: types.SimpleNamespace(allowed=True)
    live_calls = []
    worker._live_search_soundcloud = lambda artist: live_calls.append(artist) or None
    worker._mark_sc_blocked_row = lambda *args, **kwargs: False
    worker._apply_payload_guarded = lambda *args, **kwargs: False
    worker._set_platform_state = lambda *args, **kwargs: None
    worker._log_low_confidence_skip = lambda *args, **kwargs: None

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Control Artist",
                "Source Directory": "Spotify",
                "Social Link": "https://on.soundcloud.com/fTC9tqogxYPeIMMA3P",
                "External Links": "",
                "SoundCloud Link": "",
            }
        ],
        dtype=str,
    ).fillna("")
    seed_df = cde._apply_unearthed_platform_promotion_df(seed_df)

    enriched, skip_rest = worker._enrich_row_sc_live(
        seed_df,
        0,
        {"artist": "Control Artist", "spotify_id": ""},
    )

    assert seed_df.at[0, "SoundCloud Link"] == ""
    assert enriched is False
    assert skip_rest is False
    assert live_calls == ["Control Artist"]


def test_scheduler_mode_skips_legacy_phases(monkeypatch):
    # Build a worker instance without running QThread.__init__
    from cross_directory_enricher import CrossDirectoryEnricherWorker
    monkeypatch.setattr(CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    worker = CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()

    calls = {"dir": 0, "ig": 0, "sc": 0, "lf": 0, "fb": 0, "sched": 0}

    monkeypatch.setattr(worker, "_phase_directory_matching", lambda *a, **k: calls.__setitem__("dir", calls["dir"] + 1))
    monkeypatch.setattr(worker, "_phase_spotify_discovery", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_phase_instagram_email", lambda *a, **k: calls.__setitem__("ig", calls["ig"] + 1))
    monkeypatch.setattr(worker, "_phase_soundcloud", lambda *a, **k: calls.__setitem__("sc", calls["sc"] + 1))
    monkeypatch.setattr(worker, "_phase_live_lookup", lambda *a, **k: calls.__setitem__("lf", calls["lf"] + 1))
    monkeypatch.setattr(worker, "_phase_facebook", lambda *a, **k: calls.__setitem__("fb", calls["fb"] + 1))
    monkeypatch.setattr(worker, "_run_interleaved_sources", lambda *a, **k: calls.__setitem__("sched", calls["sched"] + 1))

    monkeypatch.setenv("SOURCE_DIVERSITY_SCHEDULER", "1")

    # Minimal inputs: empty dataframe and stubs for unused params.
    import pandas as pd
    seed_df = pd.DataFrame()
    worker._run_source_phased(seed_df, directory_indexes={}, priority=[], fb_driver=None, total=0)

    assert calls["dir"] == 1
    assert calls["ig"] == 1
    assert calls["sched"] == 1
    assert calls["sc"] == 0
    assert calls["lf"] == 0
    assert calls["fb"] == 0


def test_interleaved_fb_run_maps_login_wall_to_retry_later(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    captured = {}

    class FakeScheduler:
        def __init__(self, sources, row_label=None, log_fn=None, short_circuit_fn=None):
            captured["sources"] = list(sources)

        def run(self):
            return {}

    monkeypatch.setattr(cde, "SourceDiversityScheduler", FakeScheduler)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = False
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker._fb_discovery_attempted_rows = set()
    worker._domain_email_reuse_rows = set()
    worker._build_row_context = lambda *args, **kwargs: {"artist": "Artist X", "position": 1, "total": 1}
    worker._maybe_apply_domain_email_reuse = lambda *args, **kwargs: False
    worker._init_row_enrichment_state = lambda: None

    def fake_enrich_row_facebook(seed_df, row_idx, fb_driver, ctx):
        seed_df.at[row_idx, "FB_Status"] = "login_wall"
        return False

    worker._enrich_row_facebook = fake_enrich_row_facebook

    seed_df = pd.DataFrame([{"Artist Name": "Artist X", "FB_Status": ""}], index=[0])
    worker._run_interleaved_sources(seed_df, fb_driver=object(), total=1)

    fb_source = next(spec for spec in captured["sources"] if spec.name == "FB")
    result = fb_source.run_row(0)

    assert result.attempted is True
    assert result.enriched is False
    assert result.retry_later is True


def test_scheduler_mode_excludes_unearthed_fb_first_rows(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)
    monkeypatch.setattr(cde, "_apply_fb_promotion_df", lambda df, log_fn=None: df)

    captured = {}

    class FakeScheduler:
        def __init__(self, sources, row_label=None, log_fn=None, short_circuit_fn=None):
            captured["rows_by_source"] = {spec.name: list(spec.rows) for spec in sources}

        def run(self):
            return {}

    monkeypatch.setattr(cde, "SourceDiversityScheduler", FakeScheduler)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker._unearthed_fb_first_row_ids = {0}

    seed_df = pd.DataFrame(
        [
            {"Artist Name": "Unearthed FB First", "Source Directory": "Unearthed"},
            {"Artist Name": "Control Artist", "Source Directory": "Spotify"},
        ],
        dtype=str,
    ).fillna("")

    worker._run_interleaved_sources(seed_df, fb_driver=object(), total=len(seed_df))

    assert captured["rows_by_source"]["SC"] == [1]
    assert captured["rows_by_source"]["LF"] == [1]
    assert captured["rows_by_source"]["FB"] == [1]


def test_row_linear_unearthed_fb_first_bypasses_shared_enrichers(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = Log()
    worker._update_progress = lambda *args, **kwargs: None
    worker._should_short_circuit_after_domain_reuse = lambda *args, **kwargs: False
    worker._init_row_enrichment_state = lambda: None
    worker._log_spotify_discovery_summary = lambda *args, **kwargs: None

    calls = {"dir": [], "sc": [], "lf": [], "spotify": [], "ig": [], "website": [], "fb": []}

    def build_ctx(df, row_idx, position, total):
        return {
            "artist": df.at[row_idx, "Artist Name"],
            "position": position,
            "total": total,
            "spotify_id": "",
        }

    worker._build_row_context = build_ctx
    worker._enrich_row_directories = lambda df, row_idx, directory_indexes, priority, ctx: calls["dir"].append(row_idx) or False
    worker._enrich_row_sc_live = lambda df, row_idx, ctx: (calls["sc"].append(row_idx) or False, False)
    worker._enrich_row_live_lookup = lambda df, row_idx, ctx: (calls["lf"].append(row_idx) or False, False)
    worker._run_spotify_discovery_pass = lambda df, row_idx, ctx, fb_driver=None: calls["spotify"].append(row_idx) or False
    worker._enrich_row_instagram_email = lambda df, row_idx, ctx: calls["ig"].append(row_idx) or False
    worker._enrich_row_website_email = lambda df, row_idx, ctx: calls["website"].append(row_idx) or False
    worker._enrich_row_facebook = lambda df, row_idx, fb_driver, ctx: calls["fb"].append(row_idx) or False

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed FB First",
                "Source Directory": "Unearthed",
                "Social Link": "https://www.facebook.com/unearthed.fb.first",
                "Facebook_URL": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "",
            },
            {
                "Artist Name": "Unearthed Fallback",
                "Source Directory": "Unearthed",
                "Social Link": "",
                "Facebook_URL": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "",
            },
            {
                "Artist Name": "Spotify Control",
                "Source Directory": "Spotify",
                "Social Link": "",
                "Facebook_URL": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "",
            },
        ],
        dtype=str,
    ).fillna("")

    worker._run_row_linear(seed_df, directory_indexes={}, priority=[], fb_driver=object(), total=len(seed_df))

    for phase_name in calls:
        assert 0 not in calls[phase_name]
        assert 1 in calls[phase_name]
        assert 2 in calls[phase_name]
    assert any("[Unearthed Path] activated artist='Unearthed FB First' row=0" in line for line in logs)
    assert any("[Unearthed Path] skipping non-essential enrichers artist='Unearthed FB First' row=0" in line for line in logs)
    assert any("[Unearthed Path] no usable FB URL, resuming standard path artist='Unearthed Fallback' row=1" in line for line in logs)


def test_row_linear_unearthed_without_usable_explicit_fb_does_not_bypass(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = Log()
    worker._update_progress = lambda *args, **kwargs: None
    worker._should_short_circuit_after_domain_reuse = lambda *args, **kwargs: False
    worker._init_row_enrichment_state = lambda: None
    worker._log_spotify_discovery_summary = lambda *args, **kwargs: None

    calls = {"dir": [], "sc": [], "lf": [], "spotify": [], "ig": [], "website": [], "fb": []}

    worker._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }
    worker._enrich_row_directories = lambda df, row_idx, directory_indexes, priority, ctx: calls["dir"].append(row_idx) or False
    worker._enrich_row_sc_live = lambda df, row_idx, ctx: (calls["sc"].append(row_idx) or False, False)
    worker._enrich_row_live_lookup = lambda df, row_idx, ctx: (calls["lf"].append(row_idx) or False, False)
    worker._run_spotify_discovery_pass = lambda df, row_idx, ctx, fb_driver=None: calls["spotify"].append(row_idx) or False
    worker._enrich_row_instagram_email = lambda df, row_idx, ctx: calls["ig"].append(row_idx) or False
    worker._enrich_row_website_email = lambda df, row_idx, ctx: calls["website"].append(row_idx) or False
    worker._enrich_row_facebook = lambda df, row_idx, fb_driver, ctx: calls["fb"].append(row_idx) or False

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Unearthed Guarded",
                "Source Directory": "Unearthed",
                "Social Link": "",
                "Facebook_URL": "https://www.facebook.com/sharer/sharer.php?u=https://example.com",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "",
            },
        ],
        dtype=str,
    ).fillna("")

    worker._run_row_linear(seed_df, directory_indexes={}, priority=[], fb_driver=object(), total=len(seed_df))

    for phase_name in calls:
        assert calls[phase_name] == [0]
    assert any("[Unearthed Path] no usable FB URL, resuming standard path artist='Unearthed Guarded' row=0" in line for line in logs)


def test_row_linear_non_unearthed_explicit_fb_row_remains_unchanged(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    logs = []

    class Log:
        @staticmethod
        def emit(msg, *args, **kwargs):
            logs.append(str(msg))

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.log_message = Log()
    worker._update_progress = lambda *args, **kwargs: None
    worker._should_short_circuit_after_domain_reuse = lambda *args, **kwargs: False
    worker._init_row_enrichment_state = lambda: None
    worker._log_spotify_discovery_summary = lambda *args, **kwargs: None

    calls = {"dir": [], "sc": [], "lf": [], "spotify": [], "ig": [], "website": [], "fb": []}

    worker._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }
    worker._enrich_row_directories = lambda df, row_idx, directory_indexes, priority, ctx: calls["dir"].append(row_idx) or False
    worker._enrich_row_sc_live = lambda df, row_idx, ctx: (calls["sc"].append(row_idx) or False, False)
    worker._enrich_row_live_lookup = lambda df, row_idx, ctx: (calls["lf"].append(row_idx) or False, False)
    worker._run_spotify_discovery_pass = lambda df, row_idx, ctx, fb_driver=None: calls["spotify"].append(row_idx) or False
    worker._enrich_row_instagram_email = lambda df, row_idx, ctx: calls["ig"].append(row_idx) or False
    worker._enrich_row_website_email = lambda df, row_idx, ctx: calls["website"].append(row_idx) or False
    worker._enrich_row_facebook = lambda df, row_idx, fb_driver, ctx: calls["fb"].append(row_idx) or False

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Spotify Explicit",
                "Source Directory": "Spotify",
                "Social Link": "https://www.facebook.com/spotify.explicit",
                "Facebook_URL": "",
                "External Links": "",
                "Email": "",
                "Email_All": "",
                "SoundCloud Link": "",
            },
        ],
        dtype=str,
    ).fillna("")

    worker._run_row_linear(seed_df, directory_indexes={}, priority=[], fb_driver=object(), total=len(seed_df))

    for phase_name in calls:
        assert calls[phase_name] == [0]
    assert not any("[Unearthed Path]" in line for line in logs)


def test_interleaved_lf_run_maps_search_cooldown_to_timed_retry(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)

    captured = {}

    class FakeScheduler:
        def __init__(self, sources, row_label=None, log_fn=None, short_circuit_fn=None):
            captured["sources"] = list(sources)

        def run(self):
            return {}

    monkeypatch.setattr(cde, "SourceDiversityScheduler", FakeScheduler)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.max_live_searches = 0
    worker.live_search_attempts = 0
    worker._notified_limit = False
    worker._lf_search_cooldown_until = 105.0
    worker._lf_profile_cooldown_until = 0.0
    worker.__dict__["_lf_search_skipped_cooldown"] = 0
    worker.__dict__["_lf_profile_skipped_cooldown"] = 0
    worker._lf_now = lambda: 100.0
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker._fb_discovery_attempted_rows = set()
    worker._domain_email_reuse_rows = set()
    worker._build_row_context = lambda *args, **kwargs: {"artist": "Artist X", "position": 1, "total": 1, "spotify_id": ""}
    worker._maybe_apply_domain_email_reuse = lambda *args, **kwargs: False
    worker._init_row_enrichment_state = lambda: None
    worker._enrich_row_sc_live = lambda *args, **kwargs: (False, False)

    def fake_enrich_row_live_lookup(seed_df, row_idx, ctx):
        worker.__dict__["_lf_search_skipped_cooldown"] += 1
        return (False, False)

    worker._enrich_row_live_lookup = fake_enrich_row_live_lookup

    seed_df = pd.DataFrame([{"Artist Name": "Artist X", "SoundCloud Link": "", "lastfm_url": ""}], index=[0])
    worker._run_interleaved_sources(seed_df, fb_driver=None, total=1)

    lf_source = next(spec for spec in captured["sources"] if spec.name == "LF")
    result = lf_source.run_row(0)

    assert result.attempted is True
    assert result.enriched is False
    assert result.retry_later is True
    assert result.timed_retry is not None
    assert result.timed_retry.ready_at == 105.0
    assert result.timed_retry.max_attempts == 2


def test_interleaved_scheduler_orders_festival_rows_first(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(cde, "ENABLE_FACEBOOK_ENRICHMENT", True)

    captured = {}

    class FakeScheduler:
        def __init__(self, sources, row_label=None, log_fn=None, short_circuit_fn=None):
            captured["sources"] = list(sources)

        def run(self):
            return {}

    monkeypatch.setattr(cde, "SourceDiversityScheduler", FakeScheduler)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = False
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()

    seed_df = pd.DataFrame(
        [
            {"Artist Name": "Normal Artist", "Seed Priority": ""},
            {"Artist Name": "Festival Artist", "Seed Priority": "festival"},
            {"Artist Name": "Festival Headliner", "Seed Priority": "festival_high"},
            {"Artist Name": "Festival Support", "Seed Priority": "festival"},
        ],
        index=[10, 20, 30, 40],
    )

    worker._run_interleaved_sources(seed_df, fb_driver=object(), total=4)

    fb_source = next(spec for spec in captured["sources"] if spec.name == "FB")
    assert list(fb_source.rows) == [30, 20, 40, 10]


def test_email_summary_resets_per_cross_directory_run(tmp_path, monkeypatch):
    import cross_directory_enricher as cde
    import pipeline_runner

    seed_csv = tmp_path / "missing.csv"
    output_one = tmp_path / "out_one.csv"
    output_two = tmp_path / "out_two.csv"

    def _make_worker(output_path):
        worker = cde.CrossDirectoryEnricherWorker(seed_csv.as_posix(), output_path.as_posix(), enable_live_search=False)
        worker.log_message = type("obj", (), {"emit": lambda *args, **kwargs: None})
        worker.progress = type("obj", (), {"emit": lambda *args, **kwargs: None})
        worker.finished = type("obj", (), {"emit": lambda *args, **kwargs: None})
        return worker

    pipeline_runner.increment_pattern_emails(7)
    pipeline_runner.record_email_summary_row_change(
        {"Email": "", "Email_All": ""},
        {"Email": "stale@example.com", "Email_All": "stale@example.com"},
    )

    worker_one = _make_worker(output_one)
    worker_one._run_impl()
    assert pipeline_runner.get_email_summary_counts() == {"emails_found": 0, "pattern_emails": 0}

    pipeline_runner.increment_pattern_emails(4)
    pipeline_runner.record_email_summary_row_change(
        {"Email": "", "Email_All": ""},
        {"Email": "carry@example.com", "Email_All": "carry@example.com"},
    )
    worker_two = _make_worker(output_two)
    worker_two._run_impl()
    assert pipeline_runner.get_email_summary_counts() == {"emails_found": 0, "pattern_emails": 0}


def test_adaptive_priority_prefers_successful_sources(monkeypatch):
    # Deterministic jitter for reproducibility.
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)

    calls = []
    rows = [0, 1, 2, 3]

    def aa_run(idx):
        calls.append(f"AA{idx}")
        return SourceResult(attempted=True, enriched=True)

    def zz_run(idx):
        calls.append(f"ZZ{idx}")
        return SourceResult(attempted=True, enriched=False)

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="ZZ", rows=rows, run_row=zz_run, is_available=lambda: (True, None)),
            SourceSpec(name="AA", rows=rows, run_row=aa_run, is_available=lambda: (True, None)),
        ],
        row_label=str,
    )
    scheduler.run()

    # Before the warm-up threshold, tie-break order remains unchanged.
    assert calls[:4] == ["ZZ0", "AA0", "ZZ1", "AA1"]
    # Once enough observations exist, the successful source gets a modest preference.
    assert calls[4:8] == ["AA2", "ZZ2", "AA3", "ZZ3"]


def test_adaptive_bias_does_not_apply_before_warmup_threshold(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)

    calls = []
    rows = [0, 1]

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(
                name="ZZ",
                rows=rows,
                run_row=lambda idx: (calls.append(f"ZZ{idx}") or SourceResult(attempted=True, enriched=False)),
                is_available=lambda: (True, None),
            ),
            SourceSpec(
                name="AA",
                rows=rows,
                run_row=lambda idx: (calls.append(f"AA{idx}") or SourceResult(attempted=True, enriched=True)),
                is_available=lambda: (True, None),
            ),
        ],
        row_label=str,
    )

    scheduler.run()

    assert calls == ["ZZ0", "AA0", "ZZ1", "AA1"]


def test_adaptive_bias_is_bounded():
    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(
                name="AA",
                rows=[],
                run_row=lambda idx: SourceResult(attempted=True),
                is_available=lambda: (True, None),
            )
        ],
        row_label=str,
    )

    metrics = scheduler._metrics["AA"]
    metrics["attempts"] = 10
    metrics["enriched"] = 10

    assert scheduler._adaptive_priority_bonus(metrics) == pytest.approx(ADAPTIVE_PRIORITY_MAX_BONUS)


def test_cooldown_sources_are_deprioritised(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)

    rows = [0, 1, 2, 3]
    calls = []
    lf_checks = {"count": 0}

    def sc_run(idx):
        calls.append(f"SC{idx}")
        return SourceResult(attempted=True, enriched=True)

    def lf_is_available():
        lf_checks["count"] += 1
        # LF in cooldown for first two checks, then becomes available.
        return (lf_checks["count"] > 2, "cooldown" if lf_checks["count"] <= 2 else None)

    def lf_run(idx):
        calls.append(f"LF{idx}")
        return SourceResult(attempted=True, enriched=False)

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None)),
            SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lf_is_available),
        ],
        row_label=str,
    )
    scheduler.run()

    # LF rows 0 and 1 are skipped for cooldown; first LF run should occur after SC has progressed.
    first_lf_idx = calls.index("LF2")
    assert first_lf_idx > calls.index("SC2")
    # Healthy SC keeps leading even after LF becomes available.
    assert calls.index("SC3") < calls.index("LF3")


def test_opportunity_skips_soundcloud_when_url_present():
    rows = [0]
    row_data = {0: {"Artist Name": "Test Artist", "soundcloud_url": "https://soundcloud.com/test"}}
    sc_calls = []

    sc_spec = SourceSpec(
        name="SC",
        rows=rows,
        run_row=lambda idx: (sc_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([sc_spec], row_label=str).run()

    assert not sc_calls
    assert summary["SC"]["attempted"] == 0
    assert summary["SC"]["skipped_opportunity"] == 1


def test_opportunity_allows_lastfm_when_missing_url():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "lastfm_url": ""}}
    calls = []

    lf_spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: (calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([lf_spec], row_label=str).run()

    assert calls == [0]
    assert summary["LF"]["attempted"] == 1
    assert summary["LF"]["skipped_opportunity"] == 0


def test_opportunity_runs_facebook_when_url_missing_but_artist_present():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": ""}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_true_when_social_link_contains_facebook():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "https://www.facebook.com/dizzydaysband", "Email": ""}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_after_promotion_from_social_link():
    rows = [0]
    row = {"Artist Name": "Artist X", "facebook_url": "", "Social Link": "https://www.facebook.com/dizzydaysband"}
    promote_facebook_url(row)
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row,
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert row.get("facebook_url") == "https://www.facebook.com/dizzydaysband"
    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_true_with_artist_name_even_without_explicit_facebook_url():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "Social Link": "https://www.instagram.com/xxx"}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_false_when_email_present():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "https://facebook.com/artist", "Email": "x@test.com"}}
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert not fb_calls
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1


def test_fb_opportunity_false_without_artist_or_explicit_facebook_url():
    rows = [0]
    row_data = {0: {"Artist Name": "", "Social Link": "https://www.instagram.com/xxx", "Email": ""}}
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert not fb_calls
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1


def test_fb_opportunity_true_when_external_links_contains_facebook():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "http://m.facebook.com/dizzydays", "Email_All": ""}}
    fb_calls = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_fb_opportunity_false_when_discovery_already_attempted_and_no_url():
    rows = [0]
    row_data = {0: {"Artist Name": "Artist X", "facebook_url": "", "__fb_discovery_attempted_this_run": "1"}}
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert not fb_calls
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1


def test_fb_opportunity_true_with_explicit_url_even_if_discovery_attempted():
    rows = [0]
    row_data = {
        0: {
            "Artist Name": "Artist X",
            "facebook_url": "https://www.facebook.com/artistx",
            "__fb_discovery_attempted_this_run": "1",
        }
    }
    fb_calls: list[int] = []

    fb_spec = SourceSpec(
        name="FB",
        rows=rows,
        run_row=lambda idx: (fb_calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=lambda idx: row_data[idx],
    )
    summary = SourceDiversityScheduler([fb_spec], row_label=str).run()

    assert fb_calls == [0]
    assert summary["FB"]["attempted"] == 1
    assert summary["FB"]["skipped_opportunity"] == 0


def test_failed_fb_discovery_does_not_starve_other_sources_or_change_interleaving(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    rows = [0]
    row_data = {
        0: {
            "Artist Name": "Artist A",
            "facebook_url": "",
            "__fb_discovery_attempted_this_run": "1",
            "soundcloud_url": "",
            "lastfm_url": "",
            "Email": "",
            "Email_All": "",
        }
    }
    calls: list[str] = []

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(
                name="SC",
                rows=rows,
                run_row=lambda idx: (calls.append(f"SC{idx}") or SourceResult(attempted=True)),
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
            SourceSpec(
                name="LF",
                rows=rows,
                run_row=lambda idx: (calls.append(f"LF{idx}") or SourceResult(attempted=True)),
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
            SourceSpec(
                name="FB",
                rows=rows,
                run_row=lambda idx: (calls.append(f"FB{idx}") or SourceResult(attempted=True)),
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
        ],
        row_label=str,
    )
    summary = scheduler.run()

    assert "FB0" not in calls
    assert set(calls) == {"SC0", "LF0"}
    assert summary["FB"]["attempted"] == 0
    assert summary["FB"]["skipped_opportunity"] == 1
    assert summary["SC"]["attempted"] == 1
    assert summary["LF"]["attempted"] == 1


def test_opportunity_weight_prioritises_sources(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    rows = [0]
    row_data = {
        0: {
            "Artist Name": "Artist A",
            "soundcloud_url": "https://soundcloud.com/test",  # no SC opportunity
            "facebook_url": "https://facebook.com/test",  # FB opportunity
        }
    }
    order: list[str] = []

    def sc_run(idx):
        order.append("SC")
        return SourceResult(attempted=True, enriched=False)

    def fb_run(idx):
        order.append("FB")
        return SourceResult(attempted=True, enriched=False)

    row_getter = lambda idx: row_data[idx]
    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None), row_getter=row_getter),
            SourceSpec(name="FB", rows=rows, run_row=fb_run, is_available=lambda: (True, None), row_getter=row_getter),
        ],
        row_label=str,
    )
    summary = scheduler.run()

    assert order[0] == "FB"
    assert summary["SC"]["skipped_opportunity"] == 1


def test_short_circuit_stops_remaining_sources(monkeypatch):
    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    rows = [0]
    row_data = {0: {"Artist Name": "Artist A", "Email": "", "Email_All": ""}}
    calls = {"SC": 0, "LF": 0}

    def sc_run(idx):
        calls["SC"] += 1
        row_data[idx]["Email"] = "artist@example.com"
        return SourceResult(attempted=True, enriched=True)

    def lf_run(idx):
        calls["LF"] += 1
        return SourceResult(attempted=True, enriched=True)

    row_getter = lambda idx: row_data[idx]

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(name="SC", rows=rows, run_row=sc_run, is_available=lambda: (True, None), row_getter=row_getter),
            SourceSpec(name="LF", rows=rows, run_row=lf_run, is_available=lambda: (True, None), row_getter=row_getter),
        ],
        row_label=str,
        short_circuit_fn=lambda row: __import__("pipeline_runner").has_contact_email_for_short_circuit(row),
    )
    scheduler.run()

    assert calls["SC"] == 1
    assert calls["LF"] == 0


def test_scheduler_defers_availability_cooldown_and_retries_when_ready():
    now = {"value": 0}
    sc_calls = []

    def sc_is_available():
        return (now["value"] >= 5, "cooldown" if now["value"] < 5 else None)

    def sc_run(idx):
        sc_calls.append(idx)
        return SourceResult(attempted=True, enriched=True)

    tick_calls = []

    def tick_run(idx):
        tick_calls.append(idx)
        now["value"] = 10
        return SourceResult(attempted=True, enriched=False)

    summary = SourceDiversityScheduler(
        [
            SourceSpec(
                name="SC",
                rows=[0],
                run_row=sc_run,
                is_available=sc_is_available,
                retry_now=lambda: now["value"],
                unavailable_retry=lambda row_idx, reason, retry_count: TimedRetry(ready_at=5, max_attempts=2),
            ),
            SourceSpec(
                name="AUX",
                rows=[99],
                run_row=tick_run,
                is_available=lambda: (True, None),
            ),
        ],
        row_label=str,
    ).run()

    assert tick_calls == [99]
    assert sc_calls == [0]
    assert summary["SC"]["skipped_cooldown"] == 1
    assert summary["SC"]["attempted"] == 1


def test_interleaved_lf_cooldown_rows_are_deferred_and_retried(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.max_live_searches = 0
    worker.live_search_attempts = 0
    worker._notified_limit = False
    worker._fb_discovery_attempted_rows = set()
    worker._domain_email_reuse_rows = set()
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker._init_row_enrichment_state = lambda: None
    worker._maybe_apply_domain_email_reuse = lambda *args, **kwargs: False
    worker._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }
    worker._sc_in_live_cooldown = lambda now=None: False
    worker._enrich_row_sc_live = lambda *args, **kwargs: (False, False)

    now = {"value": 100.0}
    worker._lf_now = lambda: now["value"]
    worker._lf_search_cooldown_until = 105.0
    worker._lf_profile_cooldown_until = 105.0

    lf_attempts = []

    def fake_lf_enrich(df, row_idx, ctx):
        lf_attempts.append((row_idx, now["value"]))
        df.at[row_idx, "lastfm_url"] = "https://www.last.fm/music/retried"
        return (True, False)

    def fake_fb_enrich(seed_df, row_idx, fb_driver, ctx):
        now["value"] = 110.0
        return False

    worker._enrich_row_live_lookup = fake_lf_enrich
    worker._enrich_row_facebook = fake_fb_enrich

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist X",
                "SoundCloud Link": "",
                "lastfm_url": "",
                "facebook_url": "https://www.facebook.com/artistx",
                "Email": "",
                "Email_All": "",
                "FB_Status": "",
            }
        ],
        index=[0],
    )

    worker._run_interleaved_sources(seed_df, fb_driver=object(), total=1)

    assert lf_attempts == [(0, 110.0)]
    assert seed_df.at[0, "lastfm_url"] == "https://www.last.fm/music/retried"


def test_scheduler_exits_cleanly_when_deferred_work_is_not_ready():
    checks = {"available": 0}

    def sc_is_available():
        checks["available"] += 1
        return (False, "cooldown")

    summary = SourceDiversityScheduler(
        [
            SourceSpec(
                name="SC",
                rows=[0],
                run_row=lambda idx: pytest.fail("deferred row should not run before ready"),
                is_available=sc_is_available,
                retry_now=lambda: 0,
                unavailable_retry=lambda row_idx, reason, retry_count: TimedRetry(ready_at=10, max_attempts=2),
            )
        ],
        row_label=str,
    ).run()

    assert checks["available"] == 1
    assert summary["SC"]["attempted"] == 0
    assert summary["SC"]["skipped_cooldown"] == 1


def test_attempted_false_result_is_not_queued_for_retry():
    calls = []
    now = {"value": 0}

    def run_row(idx):
        calls.append((idx, now["value"]))
        now["value"] += 1
        return SourceResult()

    summary = SourceDiversityScheduler(
        [
            SourceSpec(
                name="LF",
                rows=[0],
                run_row=run_row,
                is_available=lambda: (True, None),
                retry_now=lambda: now["value"],
            )
        ],
        row_label=str,
    ).run()

    assert calls == [(0, 0)]
    assert summary["LF"]["attempted"] == 0


def test_scheduler_bounds_timed_retries():
    now = {"value": 0}
    sc_retry_counts = []

    def sc_run(row_idx, retry_count):
        sc_retry_counts.append(retry_count)
        return SourceResult(
            attempted=True,
            enriched=False,
            retry_later=True,
            timed_retry=TimedRetry(ready_at=now["value"] + 1, max_attempts=2),
        )

    def tick_run(idx):
        now["value"] += 1
        return SourceResult(attempted=True, enriched=False)

    summary = SourceDiversityScheduler(
        [
            SourceSpec(
                name="SC",
                rows=[0],
                run_row=lambda idx: sc_run(idx, 0),
                run_row_retry=sc_run,
                is_available=lambda: (True, None),
                retry_now=lambda: now["value"],
            ),
            SourceSpec(
                name="AUX",
                rows=[10, 11, 12],
                run_row=tick_run,
                is_available=lambda: (True, None),
            ),
        ],
        row_label=str,
    ).run()

    assert sc_retry_counts == [0, 1]
    assert summary["SC"]["attempted"] == 2


def test_scheduler_skips_deferred_retry_when_row_completed_elsewhere():
    now = {"value": 0}
    row_data = {0: {"Artist Name": "Artist A", "Email": "", "Email_All": ""}}
    sc_calls = []
    lf_calls = []

    def sc_run(row_idx, retry_count):
        sc_calls.append((row_idx, retry_count))
        return SourceResult(
            attempted=True,
            enriched=False,
            retry_later=True,
            timed_retry=TimedRetry(ready_at=5, max_attempts=2),
        )

    def lf_run(row_idx):
        lf_calls.append(row_idx)
        now["value"] = 10
        row_data[row_idx]["Email"] = "artist@example.com"
        return SourceResult(attempted=True, enriched=True)

    scheduler = SourceDiversityScheduler(
        [
            SourceSpec(
                name="SC",
                rows=[0],
                run_row=lambda idx: sc_run(idx, 0),
                run_row_retry=sc_run,
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
                retry_now=lambda: now["value"],
            ),
            SourceSpec(
                name="LF",
                rows=[0],
                run_row=lf_run,
                is_available=lambda: (True, None),
                row_getter=lambda idx: row_data[idx],
            ),
        ],
        row_label=str,
        short_circuit_fn=lambda row: bool(row.get("Email") or row.get("Email_All")),
    )
    scheduler.run()

    assert sc_calls == [(0, 0)]
    assert lf_calls == [0]


def test_interleaved_sc_cooldown_rows_are_deferred_and_retried(monkeypatch):
    import pandas as pd
    import cross_directory_enricher as cde

    monkeypatch.setattr("source_scheduler.random.uniform", lambda a, b: 0)
    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *a, **k: None)

    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.enable_live_search = True
    worker.max_live_searches = 0
    worker.live_search_attempts = 0
    worker._notified_limit = False
    worker._sc_live_disabled_until = 0.0
    worker._sc_in_live_cooldown = lambda now=None: bool(worker._sc_live_disabled_until and worker._sc_live_disabled_until > cde.time.time())
    worker._fb_discovery_attempted_rows = set()
    worker._domain_email_reuse_rows = set()
    worker._lf_endpoint_in_cooldown = lambda endpoint: False
    worker.log_message = type("Logger", (), {"emit": lambda *args, **kwargs: None})()
    worker._init_row_enrichment_state = lambda: None
    worker._maybe_apply_domain_email_reuse = lambda *args, **kwargs: False
    worker._build_row_context = lambda df, row_idx, position, total: {
        "artist": df.at[row_idx, "Artist Name"],
        "position": position,
        "total": total,
        "spotify_id": "",
    }

    seed_df = pd.DataFrame(
        {
            "Artist Name": ["Artist 0", "Artist 1"],
            "SoundCloud Link": ["", ""],
            "Email": ["", ""],
            "Email_All": ["", ""],
            "SC_Status": ["", ""],
            "SC_Reason": ["", ""],
        },
        index=[0, 1],
    )

    now = {"value": 1000.0}
    monkeypatch.setattr(cde.time, "time", lambda: now["value"])

    sc_attempts = {}
    lf_attempts = []

    def fake_sc_enrich(df, row_idx, ctx):
        sc_attempts[row_idx] = sc_attempts.get(row_idx, 0) + 1
        if row_idx == 0 and sc_attempts[row_idx] == 1:
            worker._sc_live_disabled_until = now["value"] + 5.0
            return (False, False)
        df.at[row_idx, "SC_Status"] = f"retried_{sc_attempts[row_idx]}"
        return (False, False)

    def fake_lf_enrich(df, row_idx, ctx):
        lf_attempts.append(row_idx)
        if len(lf_attempts) >= 2:
            now["value"] = 1010.0
        return (False, False)

    worker._enrich_row_sc_live = fake_sc_enrich
    worker._enrich_row_live_lookup = fake_lf_enrich

    worker._run_interleaved_sources(seed_df, fb_driver=None, total=len(seed_df))

    assert sc_attempts == {0: 2, 1: 1}
    assert lf_attempts == [0, 1]
    assert seed_df.at[0, "SC_Status"] == "retried_2"
    assert seed_df.at[1, "SC_Status"] == "retried_1"


def test_scheduler_opportunity_fallback_on_row_error():
    rows = [0]
    calls = []

    def row_getter(_):
        raise RuntimeError("row failure")

    spec = SourceSpec(
        name="LF",
        rows=rows,
        run_row=lambda idx: (calls.append(idx) or SourceResult(attempted=True)),
        is_available=lambda: (True, None),
        row_getter=row_getter,
    )

    summary = SourceDiversityScheduler([spec], row_label=str).run()

    assert calls == [0]
    assert summary["LF"]["attempted"] == 1
