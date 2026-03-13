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


def test_pass_b_direct_surface_miss_triggers_one_homepage_fallback(monkeypatch) -> None:
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
    monkeypatch.setattr(night_mode_fb.facebook_enrich, "discover_google_first_fb_candidates", lambda *args, **kwargs: [])

    search_methods = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        if search_method == "direct_route":
            return "<html><body>direct-miss</body></html>", SimpleNamespace(page_source="<html><body>direct-miss</body></html>", current_url="https://www.facebook.com/search/pages/?q=test"), False, "https://www.facebook.com/search/pages/?q=test"
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
    assert search_methods == ["direct_route", "homepage_ui"]


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
    monkeypatch.setattr(night_mode_fb.facebook_enrich, "discover_google_first_fb_candidates", lambda *args, **kwargs: [])

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
    assert search_methods == ["direct_route"]


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
    monkeypatch.setattr(night_mode_fb.facebook_enrich, "discover_google_first_fb_candidates", lambda *args, **kwargs: [])

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
    assert "homepage_ui" not in search_methods


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
    monkeypatch.setattr(night_mode_fb.facebook_enrich, "discover_google_first_fb_candidates", lambda *args, **kwargs: [])

    search_methods = []
    direct_html = "<html><body>direct-miss</body></html>"
    homepage_html = (
        "<div role='main'>"
        "<a href='https://www.facebook.com/reg/'>Sign up</a>"
        "<a href='https://www.facebook.com/lite/'>Facebook Lite</a>"
        "<a href='https://www.facebook.com/about/'>About</a>"
        "</div>"
    )

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        if search_method == "direct_route":
            return (
                direct_html,
                SimpleNamespace(page_source=direct_html, current_url="https://www.facebook.com/search/pages/?q=test"),
                False,
                "https://www.facebook.com/search/pages/?q=test",
            )
        return (
            homepage_html,
            SimpleNamespace(page_source=homepage_html, current_url="https://www.facebook.com/"),
            False,
            "https://www.facebook.com/",
        )

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page is None
    assert search_methods == ["direct_route", "homepage_ui"]
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
    monkeypatch.setattr(night_mode_fb.facebook_enrich, "discover_google_first_fb_candidates", lambda *args, **kwargs: [])

    search_methods = []
    direct_html = "<html><body>direct-miss</body></html>"
    homepage_html = (
        "<div role='main'><div aria-label='Search results'>"
        "<a href='https://www.facebook.com/reg/'>Sign up</a>"
        "<a href='https://www.facebook.com/testartist'>Test Artist</a>"
        "</div></div>"
    )

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        if search_method == "direct_route":
            return (
                direct_html,
                SimpleNamespace(page_source=direct_html, current_url="https://www.facebook.com/search/pages/?q=test"),
                False,
                "https://www.facebook.com/search/pages/?q=test",
            )
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
    assert search_methods == ["direct_route", "homepage_ui"]
    assert any("search_method=homepage_ui junk_candidates_filtered=1" in message for message in logs)


def test_pass_b_uses_google_first_candidates_before_fb_search_and_keeps_context(monkeypatch) -> None:
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/testartist",
        category="Musician/Band",
        search_source="google_first",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(
        night_mode_fb.facebook_enrich,
        "discover_google_first_fb_candidates",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        enricher,
        "_fetch_search_surface",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("FB search should not run when Google-first returns candidates")),
    )
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate))
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartist"
    assert enricher._last_selected_candidate_context["url"] == "https://www.facebook.com/testartist"
    assert enricher._last_selected_candidate_context["selected_by"] == "ranked_sort"
    assert enricher._last_selected_candidate_context["search_source"] == "google_first"
    assert enricher._last_search_candidates[0]["url"] == "https://www.facebook.com/testartist"


def test_pass_b_google_first_pg_candidate_survives_select_candidate_url(monkeypatch) -> None:
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/pg/TestArtist/about",
        category="Musician/Band",
        search_source="google_first",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(
        night_mode_fb.facebook_enrich,
        "discover_google_first_fb_candidates",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        enricher,
        "_fetch_search_surface",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("FB search should not run when Google-first returns a viable /pg page-style candidate")),
    )
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate))
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartist"
    assert enricher._last_selected_candidate_context["url"] == "https://www.facebook.com/testartist"
    assert enricher._last_selected_candidate_context["search_source"] == "google_first"


def test_pass_b_uses_google_first_before_session_start_for_normal_rows(monkeypatch) -> None:
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/testartist",
        category="Musician/Band",
        search_source="google_first",
    )
    enricher = _make_enricher()
    monkeypatch.setattr(
        enricher,
        "_ensure_session",
        lambda: (_ for _ in ()).throw(AssertionError("Google-first success should not start FB session")),
    )
    monkeypatch.setattr(
        night_mode_fb.facebook_enrich,
        "discover_google_first_fb_candidates",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        enricher,
        "_fetch_search_surface",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("FB search should not run when Google-first returns candidates")),
    )
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate))
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))
    monkeypatch.setattr(enricher, "_select_candidate_url", lambda *args, **kwargs: candidate.url)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=False)

    assert page == "https://www.facebook.com/testartist"


def test_rows_without_explicit_fb_urls_can_use_google_first_public_candidate_before_session(monkeypatch) -> None:
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/testartist",
        category="Musician/Band",
        search_source="google_first",
    )
    enricher = _make_enricher()
    monkeypatch.setattr(
        enricher,
        "_ensure_session",
        lambda: (_ for _ in ()).throw(AssertionError("Google-first public candidate should not require FB session")),
    )
    monkeypatch.setattr(
        night_mode_fb.facebook_enrich,
        "discover_google_first_fb_candidates",
        lambda *args, **kwargs: [candidate],
    )
    monkeypatch.setattr(
        enricher,
        "_fetch_search_surface",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("FB search fallback should not run when public Google-first candidate succeeds")),
    )
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate))
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))
    monkeypatch.setattr(
        enricher,
        "_fetch_html_with_url_anon",
        lambda url, goto_about=False: (
            "<html><body><div>Musician/Band</div><a href='mailto:artist@test.com'>artist@test.com</a></body></html>",
            url,
        ),
    )
    monkeypatch.setattr(
        enricher,
        "_fetch_html_with_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Authenticated fetch should not run when public Google-first fetch succeeds")),
    )
    monkeypatch.setattr(night_mode_fb, "_night_fb_has_music_signals", lambda *args, **kwargs: True)

    row = {"Artist Name": "Test Artist", "Email": "", "Email_All": "", "Social Link": "", "External Links": ""}
    result = enricher.enrich_row_with_facebook_night(row)

    assert "artist@test.com" in (result.get("Email_All") or "")
    assert result.get("FB_Status") in {"ok", "pass_a_skipped_no_fb_url"}


def test_pass_b_falls_back_to_fb_search_when_google_candidate_fails_final_selection(monkeypatch) -> None:
    google_candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/googlecandidate",
        category="Musician/Band",
        search_source="google_first",
    )
    fallback_candidate = SimpleNamespace(
        name="Fallback Artist",
        url="https://www.facebook.com/fallbackartist",
        category="Musician/Band",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(
        night_mode_fb.facebook_enrich,
        "discover_google_first_fb_candidates",
        lambda *args, **kwargs: [google_candidate],
    )

    search_methods = []

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        html = "<div role='main'><div aria-label='Search results'><a href='https://www.facebook.com/fallbackartist'>Fallback Artist</a></div></div>"
        return html, SimpleNamespace(page_source=html, current_url="https://www.facebook.com/search/pages/?q=test"), False, "https://www.facebook.com/search/pages/?q=test"

    def _fake_rank(artist, candidates):  # noqa: ANN001
        return [
            {"candidate": candidate, "score": 10, "features": {"music_any": True}, "breakdown": ["music_any"]}
            for candidate in candidates
        ]

    def _fake_choose(*args, **kwargs):  # noqa: ANN001
        ranked = args[1] if len(args) > 1 else []
        if not ranked:
            return None, "no_safe_match"
        candidate = ranked[0]["candidate"]
        if candidate.url == google_candidate.url:
            return None, "no_safe_match"
        return candidate, "ranked_sort"

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: [fallback_candidate])
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", _fake_rank)
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", _fake_choose)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/fallbackartist"
    assert search_methods == ["direct_route"]
    assert enricher._last_selected_candidate_context["url"] == "https://www.facebook.com/fallbackartist"
    assert enricher._last_selected_candidate_context["selected_by"] == "ranked_sort"
    assert enricher._last_selected_candidate_context.get("search_source", "") != "google_first"
