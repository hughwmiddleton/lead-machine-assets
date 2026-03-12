from __future__ import annotations

from types import SimpleNamespace

import pytest

import facebook_enrich
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


class _RefreshableSession:
    def __init__(self) -> None:
        self.last_health_ok = True
        self.last_health_reason = ""
        self.refresh_calls = 0
        self.driver = SimpleNamespace(page_source="", current_url="https://www.facebook.com/")

    def ensure_logged_in(self):
        return self.driver

    def refresh_session(self):
        self.refresh_calls += 1
        self.driver = SimpleNamespace(page_source="", current_url="https://www.facebook.com/")
        return self.driver


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/about",
        "https://www.facebook.com/help",
        "https://www.facebook.com/lite",
        "https://www.facebook.com/login",
        "https://www.facebook.com/reg",
        "https://www.facebook.com/privacy",
        "https://www.facebook.com/terms",
    ],
)
def test_fb_candidate_gate_rejects_generic_internal_routes(url: str) -> None:
    assert facebook_enrich._fb_is_candidate_url_allowed(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/someartist",
        "https://www.facebook.com/profile.php?id=123456789",
    ],
)
def test_fb_candidate_gate_keeps_valid_profile_routes(url: str) -> None:
    assert facebook_enrich._fb_is_candidate_url_allowed(url) is True


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
    monkeypatch.setattr(enricher, "_ensure_homepage_search_session_ready", lambda current_session: (object(), ""))

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


def test_pass_b_direct_surface_generic_junk_links_trigger_homepage_fallback(monkeypatch) -> None:
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/testartist",
        category="Musician/Band",
    )
    enricher = _make_enricher()
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_ensure_homepage_search_session_ready", lambda current_session: (object(), ""))

    search_methods = []
    direct_html = (
        "<div role='main'><div aria-label='Search results'>"
        "<a href='https://www.facebook.com/about'>About</a>"
        "<a href='https://www.facebook.com/help'>Help</a>"
        "<a href='https://www.facebook.com/login'>Log in</a>"
        "</div></div>"
    )
    homepage_html = (
        "<div role='main'><div aria-label='Search results'>"
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

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(
        night_mode_fb,
        "_harvest_candidates",
        lambda html, *args, **kwargs: [] if "facebook.com/about" in (html or "") else [candidate],
    )
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate) if candidates else [])
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))
    monkeypatch.setattr(enricher, "_select_candidate_url", lambda *args, **kwargs: candidate.url)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartist"
    assert night_mode_fb._fb_search_surface_miss_reason(
        direct_html,
        current_url="https://www.facebook.com/search/pages/?q=test",
    ) == "zero_usable_hrefs"
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
    monkeypatch.setattr(enricher, "_ensure_homepage_search_session_ready", lambda current_session: (object(), ""))

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
    assert any("search_method=homepage_ui failure_mode=generic_auth_surface" in message for message in logs)


def test_pass_b_homepage_fallback_filters_junk_but_keeps_real_candidate(monkeypatch) -> None:
    monkeypatch.setenv("FB_SEARCH_HARVEST_V2", "0")
    logs = []
    enricher = _make_enricher()
    enricher.logger = logs.append
    session = _DummySession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)
    monkeypatch.setattr(enricher, "_ensure_homepage_search_session_ready", lambda current_session: (object(), ""))

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


def test_pass_b_homepage_fallback_refreshes_stale_session_once(monkeypatch) -> None:
    monkeypatch.setenv("FB_SEARCH_HARVEST_V2", "0")
    candidate = SimpleNamespace(
        name="Test Artist",
        url="https://www.facebook.com/testartist",
        category="Musician/Band",
    )
    enricher = _make_enricher()
    session = _RefreshableSession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []
    direct_html = "<html><body>direct-miss</body></html>"
    homepage_html = (
        "<div role='main'><div aria-label='Search results'>"
        "<a href='https://www.facebook.com/testartist'>Test Artist</a>"
        "</div></div>"
    )
    probe_calls = {"count": 0}

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

    def _fake_session_health(driver):  # noqa: ANN001
        probe_calls["count"] += 1
        if probe_calls["count"] == 1:
            return False, "redirect_login"
        return True, ""

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(
        night_mode_fb,
        "_harvest_candidates",
        lambda html, *args, **kwargs: [] if "direct-miss" in (html or "") else [candidate],
    )
    monkeypatch.setattr(night_mode_fb, "_session_looks_healthy", _fake_session_health)
    monkeypatch.setattr(night_mode_fb, "_find_fb_home_search_input", lambda *args, **kwargs: object())
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda artist, candidates: _ranked(candidate) if candidates else [])
    monkeypatch.setattr(enricher, "_choose_ranked_candidate", lambda *args, **kwargs: (candidate, "ranked_sort"))
    monkeypatch.setattr(enricher, "_select_candidate_url", lambda *args, **kwargs: candidate.url)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page == "https://www.facebook.com/testartist"
    assert search_methods == ["direct_route", "homepage_ui"]
    assert session.refresh_calls == 1
    assert probe_calls["count"] == 2


def test_pass_b_homepage_fallback_aborts_after_one_failed_refresh(monkeypatch) -> None:
    monkeypatch.setenv("FB_SEARCH_HARVEST_V2", "0")
    logs = []
    enricher = _make_enricher()
    enricher.logger = logs.append
    session = _RefreshableSession()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: session)
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda current_session: current_session)

    search_methods = []
    direct_html = "<html><body>direct-miss</body></html>"

    def _fake_fetch(query_str, *, search_method, session=None):  # noqa: ANN001
        search_methods.append(search_method)
        assert search_method == "direct_route"
        return (
            direct_html,
            SimpleNamespace(page_source=direct_html, current_url="https://www.facebook.com/search/pages/?q=test"),
            False,
            "https://www.facebook.com/search/pages/?q=test",
        )

    monkeypatch.setattr(enricher, "_fetch_search_surface", _fake_fetch)
    monkeypatch.setattr(night_mode_fb, "_session_looks_healthy", lambda driver: (False, "redirect_login"))
    monkeypatch.setattr(night_mode_fb, "_find_fb_home_search_input", lambda *args, **kwargs: None)

    page = enricher._search_for_page("Test Artist", location="", allow_anon=True)

    assert page is None
    assert search_methods == ["direct_route"]
    assert session.refresh_calls == 1
    assert session.last_health_ok is False
    assert session.last_health_reason == "redirect_login"
    assert any("Homepage fallback session precheck failed; attempting one refresh." in message for message in logs)
    assert any("search_method=homepage_ui failure_mode=redirect_login" in message for message in logs)
