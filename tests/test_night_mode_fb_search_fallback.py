from __future__ import annotations

from types import SimpleNamespace

import night_mode_fb


def _make_enricher(legacy_module=None):
    return night_mode_fb.NightModeFacebookEnricher(
        legacy_module=legacy_module,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )


def _ranked(candidate):
    return [{"candidate": candidate, "score": 10, "features": {"music_any": True}, "breakdown": ["music_any"]}]


class _DummySession:
    last_health_ok = True
    last_health_reason = ""

    def ensure_logged_in(self):
        return SimpleNamespace(page_source="", current_url="https://www.facebook.com/")


def test_pass_a_explicit_url_still_bypasses_search(monkeypatch) -> None:
    enricher = _make_enricher()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)

    def _fake_scrape(url, *args, **kwargs):  # noqa: ANN001
        result = night_mode_fb.NightModeFacebookResult(
            email="artist@test.com",
            email_all="artist@test.com",
            facebook_url=night_mode_fb._normalise_fb_url(url),
            email_extract_method="regex",
        )
        return result, ["artist@test.com"], "session", "found_email"

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", _fake_scrape)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PASS A should not invoke PASS B search")),
    )

    row = {"Artist Name": "Example Artist", "Facebook_URL": "https://www.facebook.com/exampleartist"}
    result = enricher.enrich_row_with_facebook_night(row)

    assert result["FB_Status"] == "pass_a_found_email"
    assert result["FB_Reason"] == "explicit_url"


def test_pass_b_homepage_ui_is_single_primary_search(monkeypatch) -> None:
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/testartist",
        category="Musician/Band",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        return "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/testartist'>Test Artist</a></div></div>", SimpleNamespace(page_source="<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/testartist'>Test Artist</a></div></div>", current_url="https://www.facebook.com/search/top/?q=test"), False, "https://www.facebook.com/search/top/?q=test"

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(
        night_mode_fb,
        "_harvest_candidates",
        lambda html, *args, **kwargs: [] if "direct-miss" in (html or "") else [candidate],
    )
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate) if candidates else [])
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))
    monkeypatch.setattr(enricher, "_select_candidate_url", lambda *args, **kwargs: candidate.url)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartist"
    assert search_methods == ["homepage_ui"]


def test_build_fb_discovery_query_prefers_song_title_over_location() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="Melbourne, VIC",
        song_title="TAKE//OVER",
    )

    assert secondary == "TAKE OVER"
    assert query == "Signal Artist TAKE OVER"


def test_build_fb_discovery_query_prefers_song_title_over_handle_like_upstream_hint() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Julia Wallace",
        location="Melbourne, VIC",
        song_title="Pick Up The Pace",
        row={"SoundCloud Link": "https://soundcloud.com/jwallacce/tracks"},
    )

    assert secondary == "Pick Up The Pace"
    assert query == "Julia Wallace Pick Up The Pace"


def test_build_fb_discovery_query_prefers_song_title_over_synthetic_handle_like_upstream_hint() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="Melbourne, VIC",
        song_title="Midnight Echo",
        row={"SoundCloud Link": "https://soundcloud.com/signalhandle123/tracks"},
    )

    assert secondary == "Midnight Echo"
    assert query == "Signal Artist Midnight Echo"
    assert "signalhandle123" not in query


def test_build_fb_discovery_query_prefers_upstream_identity_hint_when_title_absent() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="Melbourne, VIC",
        song_title="",
        row={"SoundCloud Link": "https://soundcloud.com/signalhandle/tracks"},
    )

    assert secondary == "signalhandle"
    assert query == "Signal Artist signalhandle"


def test_build_fb_discovery_query_falls_back_to_normalized_location() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="Melbourne, VIC",
        song_title="(Live)",
    )

    assert secondary == "Melbourne VIC"
    assert query == "Signal Artist Melbourne VIC"


def test_build_fb_discovery_query_rejects_low_information_song_title_and_uses_location() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="Melbourne, VIC",
        song_title="Demo Mix",
    )

    assert secondary == "Melbourne VIC"
    assert query == "Signal Artist Melbourne VIC"


def test_build_fb_discovery_query_rejects_low_information_song_title_without_location() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="",
        song_title="Track 01",
    )

    assert secondary == ""
    assert query == "Signal Artist"


def test_build_fb_discovery_query_keeps_existing_fallback_when_no_hint_available() -> None:
    query, secondary = night_mode_fb._build_fb_discovery_query(
        "Signal Artist",
        location="Melbourne, VIC",
        song_title="(Live)",
        row={"SoundCloud Link": "https://soundcloud.com/charts/top", "Bandcamp_URL": "https://blog.bandcamp.com/article"},
    )

    assert secondary == "Melbourne VIC"
    assert query == "Signal Artist Melbourne VIC"


def test_pass_b_secondary_signal_skips_refine_queries(monkeypatch) -> None:
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)

    queries = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        queries.append((query_str, search_method))
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    row = {}
    page = enricher._search_for_page(
        "Signal Artist",
        location="Melbourne, VIC",
        allow_anon=True,
        song_title="Night Drive",
        row=row,
    )

    assert page is None
    assert queries == [("Signal Artist Night Drive", "homepage_ui")]
    assert row["FB_Refine_Decision"] == "skipped_not_allowed"
    assert row["FB_Refine_Executed"] is False


def test_pass_b_artist_only_still_uses_existing_refine_queries(monkeypatch) -> None:
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)

    queries = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        queries.append((query_str, search_method))
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    page = enricher._search_for_page("Signal Artist", location="", allow_anon=True, song_title="")

    assert page is None
    assert queries == [
        ("Signal Artist", "homepage_ui"),
        ("Signal Artist musician", "homepage_ui"),
        ("Signal Artist band", "homepage_ui"),
    ]


def test_pass_b_upstream_identity_hint_keeps_existing_query_count(monkeypatch) -> None:
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)

    queries = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        queries.append((query_str, search_method))
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    page = enricher._search_for_page(
        "Signal Artist",
        location="",
        allow_anon=True,
        song_title="",
        row={"SoundCloud Link": "https://soundcloud.com/signalhandle/tracks"},
    )

    assert page is None
    assert queries == [
        ("Signal Artist signalhandle", "homepage_ui"),
        ("Signal Artist musician", "homepage_ui"),
        ("Signal Artist band", "homepage_ui"),
    ]


def test_pass_b_no_homepage_fallback_on_no_safe_match(monkeypatch) -> None:
    candidate = SimpleNamespace(
        name="Mismatch Artist",
        url="https://www.facebook.com/mismatchartist",
        category="Musician/Band",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        html = "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/mismatchartist'>Mismatch Artist</a></div></div>"
        return html, SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test"), False, "https://www.facebook.com/search/pages/?q=test"

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate))
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))

    page = enricher._search_for_page("Mismatch Artist", location="", allow_anon=True)

    assert page is None
    assert search_methods == ["homepage_ui"]


def test_pass_b_no_homepage_fallback_on_min_quality_reject(monkeypatch) -> None:
    monkeypatch.setenv("NIGHT_FB_MIN_QUALITY_GATE", "1")
    monkeypatch.setenv("NIGHT_FB_MIN_QUALITY_SCORE", "50")
    candidate = SimpleNamespace(
        name="Weak Artist",
        url="https://www.facebook.com/weakartist",
        category="Musician/Band",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        html = "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/weakartist'>Weak Artist</a></div></div>"
        return html, SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test"), False, "https://www.facebook.com/search/pages/?q=test"

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate))
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))
    monkeypatch.setattr(night_mode_fb, "_score_candidate_min_quality", lambda *args, **kwargs: (0, ["weak"], {}))

    page = enricher._search_for_page("Weak Artist", location="", allow_anon=True)

    assert page is None
    assert search_methods == ["homepage_ui"]


def test_pass_b_skips_legacy_search_layer_before_modern_search(monkeypatch) -> None:
    class _Legacy:
        def fb_find_page_and_emails_by_name(self, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("legacy PASS B search should not run")

    enricher = _make_enricher(legacy_module=_Legacy())
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)

    calls = []
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: calls.append("search") or "")

    row = {"Artist Name": "Search Only Artist", "Email": "", "Email_All": ""}
    enricher.enrich_row_with_facebook_night(row)

    assert calls == ["search"]


def test_pass_b_homepage_fallback_rejects_generic_auth_surface(monkeypatch) -> None:
    monkeypatch.setenv("FB_SEARCH_HARVEST_V2", "0")
    logs = []
    enricher = _make_enricher()
    enricher.logger = logs.append
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []
    homepage_html = (
        "<div role='main'>"
        "<a href='https://www.facebook.com/reg/'>Sign up</a>"
        "<a href='https://www.facebook.com/lite/'>Facebook Lite</a>"
        "<a href='https://www.facebook.com/about/'>About</a>"
        "</div>"
    )

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        return (
            homepage_html,
            SimpleNamespace(page_source=homepage_html, current_url="https://www.facebook.com/"),
            False,
            "https://www.facebook.com/",
        )

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page is None
    assert search_methods == ["homepage_ui"]
    assert any("search_method=homepage_ui junk_candidates_filtered=3" in message for message in logs)
    assert any("search_method=homepage_ui failure_mode=generic_auth_surface" in message for message in logs)


def test_pass_b_homepage_fallback_filters_junk_but_keeps_real_candidate(monkeypatch) -> None:
    monkeypatch.setenv("FB_SEARCH_HARVEST_V2", "0")
    logs = []
    enricher = _make_enricher()
    enricher.logger = logs.append
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []
    homepage_html = (
        "<div role='main'><div aria-label='Search results'>"
        "<a href='https://www.facebook.com/reg/'>Sign up</a>"
        "<a href='https://www.facebook.com/testartist'>Test Artist</a>"
        "</div></div>"
    )

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        return (
            homepage_html,
            SimpleNamespace(page_source=homepage_html, current_url="https://www.facebook.com/search/top/?q=test"),
            False,
            "https://www.facebook.com/search/top/?q=test",
        )

    def _fake_rank(artist, candidates):  # noqa: ANN001
        return [
            {"candidate": candidate, "score": 10, "features": {"music_any": True}, "breakdown": ["music_any"]}
            for candidate in candidates
        ]

    def _fake_choose(*args, **kwargs):  # noqa: ANN001
        ranked = args[1] if len(args) > 1 else []
        return (ranked[0]["candidate"], "ranked_sort") if ranked else (None, "no_safe_match")

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", _fake_rank)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", _fake_choose)
    monkeypatch.setattr(enricher, "_select_candidate_url", lambda *args, **kwargs: args[1].url)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartist"
    assert search_methods == ["homepage_ui"]
    assert any("search_method=homepage_ui junk_candidates_filtered=1" in message for message in logs)


def test_initial_refine_fires_when_worthy_candidate_present(monkeypatch) -> None:
    """Refine runs when first-pass has no music, top_score<=0, but a weak+ name match exists."""
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)

    worthy_ranked = [
        {"candidate": SimpleNamespace(name="Test Artist", url="https://www.facebook.com/testartist", category=""),
         "score": 0, "features": {"music_any": False, "match_level": "weak", "is_page_style_url": False}, "breakdown": []},
    ]
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [worthy_ranked[0]["candidate"]])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: worthy_ranked)

    queries = []

    def _fake_fetch(query_str, *, search_method, session=None):
        queries.append(query_str)
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    row = {}
    enricher._search_for_page("Test Artist", location="", allow_anon=True, row=row)

    # Refine queries should have fired (primary + "musician" + "band")
    assert any("musician" in q for q in queries), f"Expected refine query with 'musician', got {queries}"
    assert row["FB_Refine_Decision"] == "allowed"
    assert row["FB_Refine_Executed"] is True


def test_initial_refine_skipped_for_junk_candidate_set(monkeypatch) -> None:
    """Refine does NOT run when first-pass candidates are all junk (mismatch, no page signal)."""
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)

    junk_ranked = [
        {"candidate": SimpleNamespace(name="Random Page", url="https://www.facebook.com/randompage", category=""),
         "score": -5, "features": {"music_any": False, "match_level": "mismatch", "is_page_style_url": False}, "breakdown": []},
        {"candidate": SimpleNamespace(name="Service Only", url="https://www.facebook.com/serviceonly", category=""),
         "score": 0, "features": {"music_any": False, "match_level": "none", "is_page_style_url": False}, "breakdown": []},
    ]
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [r["candidate"] for r in junk_ranked])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: junk_ranked)

    queries = []

    def _fake_fetch(query_str, *, search_method, session=None):
        queries.append(query_str)
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    row = {}
    enricher._search_for_page("Test Artist", location="", allow_anon=True, row=row)

    # Only the primary query should fire — no refine queries
    assert not any("musician" in q for q in queries), f"Refine should NOT fire for junk set, got {queries}"
    assert not any("band" in q for q in queries), f"Refine should NOT fire for junk set, got {queries}"
    assert row["FB_Refine_Decision"] == "skipped_junk_gate"
    assert row["FB_Refine_Executed"] is False


def test_initial_refine_skipped_when_suppress_refine_queries_is_set(monkeypatch) -> None:
    """Homepage surface miss suppression records a skipped refine decision."""
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: "homepage_surface_miss")
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: [])

    queries = []

    def _fake_fetch(query_str, *, search_method, session=None):
        queries.append(query_str)
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    row = {}
    page = enricher._search_for_page("Test Artist", location="", allow_anon=True, row=row)

    assert page is None
    assert row["FB_Refine_Decision"] == "skipped_suppressed"
    assert row["FB_Refine_Executed"] is False
    assert not any("musician" in q for q in queries), f"Refine should be suppressed, got {queries}"


def test_initial_refine_skipped_for_overlay_soft_block(monkeypatch) -> None:
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: [])

    def _fake_harvest(_html, *_args, diagnostics=None, **_kwargs):
        if diagnostics is not None:
            diagnostics["overlay_soft_block"] = True
        return []

    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", _fake_harvest)

    def _fake_fetch(query_str, *, search_method, session=None):
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    row = {}
    page = enricher._search_for_page("Test Artist", location="", allow_anon=True, row=row)

    assert page is None
    assert row["FB_Refine_Decision"] == "skipped_overlay"
    assert row["FB_Refine_Executed"] is False


def test_pass_b_terminal_result_preserves_refine_telemetry(monkeypatch) -> None:
    monkeypatch.setenv("FB_REFINE_QUERY", "1")
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (None, "no_safe_match"))
    monkeypatch.setattr(night_mode_fb, "_fb_search_surface_miss_reason", lambda *args, **kwargs: None)

    worthy_ranked = [
        {"candidate": SimpleNamespace(name="Test Artist", url="https://www.facebook.com/testartist", category=""),
         "score": 0, "features": {"music_any": False, "match_level": "weak", "is_page_style_url": False}, "breakdown": []},
    ]
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [worthy_ranked[0]["candidate"]])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: worthy_ranked)

    def _fake_fetch(query_str, *, search_method, session=None):
        html = "<div role='main'><div aria-label='Search results'></div></div>"
        driver = SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test")
        return html, driver, False, driver.current_url

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    result = enricher.enrich_row_with_facebook_night({"Artist Name": "Test Artist", "Email": "", "Email_All": ""})

    assert result["FB_Status"] == "pass_a_skipped_no_fb_url"
    assert result["FB_Refine_Decision"] == "allowed"
    assert result["FB_Refine_Executed"] is True


def test_pass_b_homepage_fallback_harvests_role_link_card_candidate(monkeypatch) -> None:
    monkeypatch.setenv("FB_SEARCH_HARVEST_V2", "0")
    logs = []
    enricher = _make_enricher()
    enricher.logger = logs.append
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []
    homepage_html = """
    <div role="main">
      <div aria-label="Search results">
        <div role="article" class="card">
          <div role="link" data-href="https://www.facebook.com/testartistmusic">
            <a aria-label="Test Artist">Test Artist</a>
            <div class="subtitle">Musician/band</div>
          </div>
        </div>
        <div role="article" class="card">
          <div role="link" data-href="https://www.facebook.com/testartiststore">
            <a aria-label="Test Artist Store">Test Artist Store</a>
            <div class="subtitle">Gift shop</div>
          </div>
        </div>
      </div>
    </div>
    """

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        return (
            homepage_html,
            SimpleNamespace(page_source=homepage_html, current_url="https://www.facebook.com/search/top/?q=test"),
            False,
            "https://www.facebook.com/search/top/?q=test",
        )

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartistmusic"
    assert search_methods == ["homepage_ui"]
