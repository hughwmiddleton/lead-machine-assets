import pytest

import night_mode_fb as nmfb
from selenium.common.exceptions import TimeoutException


class _DummyLegacy:
    def setup_facebook_driver(self):
        return object()


@pytest.fixture
def enricher(monkeypatch):
    helper = nmfb.NightModeFacebookEnricher(
        _DummyLegacy(), username="user", password="pass", logger=None, use_shared_session=False
    )
    # Avoid real network/driver work.
    monkeypatch.setattr(helper, "_ensure_driver_alive", lambda session: session)
    return helper


def test_explicit_fb_url_prefers_authenticated_session(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["allow_anon"] = allow_anon
        observed["fb_url"] = fb_url
        return (
            nmfb.NightModeFacebookResult(
                email="fb@example.com",
                email_all="fb@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["fb@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    def _fail_anon(self):
        raise AssertionError("anon not expected")

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_get_anon_driver", _fail_anon)

    row = {
        "Artist Name": "Explicit FB",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/explicit.session.test",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed.get("allow_anon") is False, "Should not fall back to anonymous when session is available"
    assert any('[Night FB][Explicit Intake]' in msg and 'outcome="attempt"' in msg for msg in logs)
    assert "Using explicit FB URLs with authenticated session" in " ".join(logs)
    assert any('mode="session"' in msg for msg in logs), "PASS A log should report session mode"
    assert not any("anon_after_session_fail" in msg for msg in logs)
    assert result.get("FB_Status", "").startswith("pass_a")
    assert "fb@example.com" in result.get("Email_All", "")


def test_explicit_fb_url_falls_back_to_legacy_anon(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["allow_anon"] = allow_anon
        return None  # force fallback handling

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    # Make anon driver available so legacy probe path stays intact if reached elsewhere.
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())

    row = {
        "Artist Name": "Explicit FB",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/explicit.anon.test",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed.get("allow_anon") is True, "Legacy anon probe should remain enabled when session missing"
    assert any('[Night FB][Explicit Intake]' in msg and 'outcome="attempt"' in msg for msg in logs)
    assert "Falling back to legacy anon probe for explicit FB URLs" in " ".join(logs)
    assert any('mode="legacy_anon_probe"' in msg for msg in logs), "PASS A log should report legacy anon mode"
    assert result.get("FB_Status", "")  # status still set/returned without crashing


def test_explicit_fb_url_preserves_extracted_email_when_override_rejects(monkeypatch, enricher):
    main_url = "https://www.facebook.com/explicit.override.test"
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PASS A should not invoke PASS B search")),
    )

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        assert url == main_url
        return (
            """
            <html>
              <head>
                <title>Off-Brand Title</title>
              </head>
              <body>
                <div>Bookings: keepme@artist.com</div>
              </body>
            </html>
            """,
            main_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(
        nmfb,
        "should_accept_email_override",
        lambda *args, **kwargs: (False, "email_override_reject:name_mismatch"),
    )

    row = {
        "Artist Name": "Explicit FB",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": main_url,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("Email") == "keepme@artist.com"
    assert "keepme@artist.com" in (result.get("Email_All") or "")
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"


def test_unearthed_explicit_canonical_url_uses_normal_pass_a(monkeypatch, enricher):
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_enrich_row_unearthed_legacy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy path should not be used")),
    )

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["fb_url"] = fb_url
        observed["allow_anon"] = allow_anon
        observed["explicit_accepted_url"] = bool(candidate_context and candidate_context.get("explicit_accepted_url"))
        observed["accepted_page_fast_loader_safe"] = bool(
            candidate_context and candidate_context.get("accepted_page_fast_loader_safe")
        )
        return (
            nmfb.NightModeFacebookResult(
                email="unearthed@example.com",
                email_all="unearthed@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["unearthed@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Unearthed Explicit",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://www.facebook.com/unearthed.explicit",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["fb_url"] == "https://www.facebook.com/unearthed.explicit"
    assert observed["allow_anon"] is False
    assert observed["explicit_accepted_url"] is True
    assert observed["accepted_page_fast_loader_safe"] is True
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("Email") == "unearthed@example.com"


def test_pass_b_selected_candidate_is_promoted_without_changing_selection(enricher):
    primary_candidate = {
        "name": "Discovery Artist",
        "url": "https://www.facebook.com/discovery.artist",
        "category": "Musician/band",
    }
    fallback_candidate = {
        "name": "Discovery Artist Backup",
        "url": "https://www.facebook.com/discovery.artist.backup",
        "category": "Musician/band",
    }
    ranked_for_preview = [
        {
            "candidate": primary_candidate,
            "score": 40,
            "features": {"name": "Discovery Artist", "match_level": "near"},
        },
        {
            "candidate": fallback_candidate,
            "score": 39,
            "features": {"name": "Discovery Artist Backup", "match_level": "near"},
        },
    ]

    selected_url = enricher._select_candidate_url(
        "Discovery Artist",
        primary_candidate,
        [primary_candidate, fallback_candidate],
        [primary_candidate, fallback_candidate],
        ranked_for_preview,
        "ranked_sort",
        25,
    )

    assert selected_url == "https://www.facebook.com/discovery.artist"
    assert [ctx["url"] for ctx in enricher._last_search_candidates] == [
        "https://www.facebook.com/discovery.artist",
        "https://www.facebook.com/discovery.artist.backup",
    ]
    assert enricher._last_selected_candidate_context == enricher._last_search_candidates[0]
    assert enricher._last_selected_candidate_context["search_discovery_accepted"] is True
    assert enricher._last_selected_candidate_context["explicit_accepted_url"] is True
    assert enricher._last_selected_candidate_context["accepted_page_fast_loader_safe"] is True
    assert "explicit_accepted_url" not in enricher._last_search_candidates[1]
    assert "accepted_page_fast_loader_safe" not in enricher._last_search_candidates[1]


def test_unearthed_promotable_social_link_uses_normal_pass_a(monkeypatch, enricher):
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_enrich_row_unearthed_legacy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy path should not be used")),
    )

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["fb_url"] = fb_url
        observed["explicit_accepted_url"] = bool(candidate_context and candidate_context.get("explicit_accepted_url"))
        return (
            nmfb.NightModeFacebookResult(
                email="promoted@example.com",
                email_all="promoted@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["promoted@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Unearthed Promoted",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "https://www.facebook.com/unearthed.promoted",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["fb_url"] == "https://www.facebook.com/unearthed.promoted"
    assert observed["explicit_accepted_url"] is True
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("Email") == "promoted@example.com"


def test_unearthed_promotable_social_link_with_existing_email_uses_normal_pass_a(monkeypatch, enricher):
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_enrich_row_unearthed_legacy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy path should not be used")),
    )

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["fb_url"] = fb_url
        observed["explicit_accepted_url"] = bool(candidate_context and candidate_context.get("explicit_accepted_url"))
        observed["email_all_before"] = row.get("Email_All")
        return (
            nmfb.NightModeFacebookResult(
                email="fbwin@example.com",
                email_all="seed@example.com;fbwin@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["fbwin@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Unearthed Promoted Existing Email",
        "Source Directory": "unearthed",
        "Email": "seed@example.com",
        "Email_All": "seed@example.com",
        "Facebook_URL": "",
        "Social Link": "https://www.facebook.com/unearthed.promoted.existing",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed == {}
    assert result.get("FB_Status", "") in {"", "ok"}
    assert result.get("Email_All") == "seed@example.com"


def test_unearthed_without_seeded_fb_skips_night_discovery(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_has_authenticated_session",
        lambda: (_ for _ in ()).throw(AssertionError("explicit PASS A should not run")),
    )
    monkeypatch.setattr(
        enricher,
        "_enrich_row_unearthed_legacy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy Unearthed helper should not run")),
    )
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Unearthed no-seed rows must not search")),
    )
    monkeypatch.setattr(
        nmfb.NightModeFacebookEnricher,
        "_scrape_single_fb_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("candidate evaluation should not run")),
    )

    row = {
        "Artist Name": "Unearthed Legacy",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") == ""
    assert result.get(nmfb.FB_ATTEMPT_STATE_COL, "") == ""
    assert any("[Unearthed Path] no usable FB URL; skipping Night FB discovery" in msg for msg in logs)
    assert not any("[Unearthed Path] no usable FB URL; allowing bounded FB discovery" in msg for msg in logs)
    assert not any("[Unearthed Path] entering Unearthed no-URL FB discovery" in msg for msg in logs)


def test_unearthed_unresolved_share_url_uses_single_runtime_pass_a_fallback(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("share runtime fallback should stay on PASS A")),
    )

    share_url = "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    canonical_url = "https://www.facebook.com/unearthed.runtime"
    resolve_calls = []
    scrape_calls = []

    monkeypatch.setattr(
        enricher,
        "_resolve_pass_a_explicit_scrape_url",
        lambda direct_url, authed_session_available=False: resolve_calls.append(direct_url) or canonical_url,
    )

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        scrape_calls.append((fb_url, allow_anon))
        return (
            nmfb.NightModeFacebookResult(
                email="runtime@example.com",
                email_all="runtime@example.com",
                facebook_url=canonical_url,
                email_source_url=canonical_url,
                email_extract_method="regex",
            ),
            ["runtime@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Unearthed Runtime Share",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": share_url,
        nmfb.FB_SHARE_RUNTIME_FALLBACK_URL_COL: share_url,
        nmfb.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL: "Social Link",
    }

    decision = nmfb.classify_explicit_fb_intake(row)

    result = enricher.enrich_row_with_facebook_night(row)

    assert decision.outcome == "attempt_share_runtime_fallback"
    assert decision.accepted_urls == []
    assert decision.runtime_share_fallback_urls == [share_url]
    assert decision.source_fallback_blocked is True
    assert nmfb.explicit_fb_entrypoint_urls_for_row(row) == []
    assert resolve_calls == [share_url]
    assert scrape_calls == [(canonical_url, False)]
    assert result.get("FB_Status", "") == "pass_a_found_email"
    assert result.get("Facebook_URL", "") == canonical_url
    assert any("source_fallback_blocked=1" in msg for msg in logs)
    assert any('[Night FB][Explicit Intake]' in msg and 'outcome="attempt_share_runtime_fallback"' in msg for msg in logs)
    assert any("[FB Share Runtime Resolve] canonical_url='https://www.facebook.com/unearthed.runtime' commit=1" in msg for msg in logs)
    assert not any("[Unearthed Path] no usable FB URL; allowing bounded FB discovery" in msg for msg in logs)


def test_unearthed_runtime_share_fallback_never_commits_raw_share_url(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("share runtime fallback should not fall into search")),
    )

    share_url = "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    resolve_calls = []

    monkeypatch.setattr(
        enricher,
        "_resolve_pass_a_explicit_scrape_url",
        lambda direct_url, authed_session_available=False: resolve_calls.append(direct_url) or share_url,
    )

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        return (
            nmfb.NightModeFacebookResult(
                email="",
                email_all="",
                facebook_url=share_url,
                email_source_url=share_url,
                email_extract_method="regex",
            ),
            [],
            "session",
            "no_email_on_page",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Unearthed Runtime Share Fail",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": share_url,
        nmfb.FB_SHARE_RUNTIME_FALLBACK_URL_COL: share_url,
        nmfb.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL: "Social Link",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert resolve_calls == [share_url]
    assert result.get("FB_Status", "") == "pass_a_no_email_on_page"
    assert result.get("Facebook_URL", "") == ""
    assert any("[FB Share Runtime Resolve] canonical_url='' commit=0" in msg for msg in logs)


def test_non_unearthed_without_seeded_fb_still_runs_search(monkeypatch, enricher):
    search_calls = []
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda artist_name, location="", allow_anon=True, song_title="", row=None: search_calls.append(
            (artist_name, location, allow_anon, song_title)
        ) or "",
    )
    monkeypatch.setattr(
        nmfb.NightModeFacebookEnricher,
        "_scrape_single_fb_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no candidate should prevent candidate scraping")),
    )

    row = {
        "Artist Name": "Spotify Discovery",
        "Source Directory": "spotify",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert search_calls == [("Spotify Discovery", "", True, "")]
    assert result.get("FB_Status") == "pass_a_skipped_no_fb_url"


def test_non_unearthed_unresolved_share_url_still_uses_standard_search(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    search_calls = []
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda artist_name, location="", allow_anon=True, song_title="", row=None: search_calls.append(
            (artist_name, location, allow_anon, song_title)
        ) or "",
    )
    monkeypatch.setattr(
        nmfb.NightModeFacebookEnricher,
        "_scrape_single_fb_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rejected share URL must not be scraped")),
    )

    row = {
        "Artist Name": "Spotify Rejected Share",
        "Source Directory": "spotify",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert search_calls == [("Spotify Rejected Share", "", True, "")]
    assert result.get("FB_Status") == "pass_a_skipped_no_fb_url"
    assert any("source_fallback_blocked=1" in msg for msg in logs)
    assert any('[Night FB][Explicit Intake]' in msg and 'outcome="no_explicit_url"' in msg for msg in logs)


def test_non_unearthed_explicit_url_still_skips_legacy_helper(monkeypatch, enricher):
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_enrich_row_unearthed_legacy",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy Unearthed helper should not be used")),
    )

    observed = {}

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["fb_url"] = fb_url
        return (
            nmfb.NightModeFacebookResult(
                email="normal@example.com",
                email_all="normal@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["normal@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Normal Explicit",
        "Source Directory": "spotify",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://www.facebook.com/normal.explicit",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["fb_url"] == "https://www.facebook.com/normal.explicit"
    assert result.get("FB_Status") == "pass_a_found_email"


def test_rows_without_explicit_fb_urls_unchanged(monkeypatch, enricher):
    # Ensure explicit-URL branch is untouched when no FB URLs exist.
    def _fail_session():
        raise AssertionError("should not be called")

    monkeypatch.setattr(enricher, "_has_authenticated_session", _fail_session)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)

    def _fail_scrape(self, *args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _fail_scrape)

    row = {"Artist Name": "No FB", "Email": "", "Email_All": "", "Social Link": ""}
    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status", "").startswith("pass_a_skipped") or result.get("FB_Status", "") == "ok" or not result.get("FB_Status", "")


class _TimeoutDriver:
    def __init__(self, html: str = ""):
        self._html = html
        self.set_timeout_called = None
        self.stop_called = False
        self.get_calls = 0

    def set_page_load_timeout(self, seconds):
        self.set_timeout_called = seconds

    def get(self, url):  # noqa: ANN001
        self.get_calls += 1
        raise TimeoutException("timed out")

    def execute_script(self, script):
        if script == "window.stop();":
            self.stop_called = True

    @property
    def page_source(self):
        return self._html

    @property
    def current_url(self):
        return "https://www.facebook.com/timeout"


def test_explicit_fb_timeout_without_html(monkeypatch, enricher):
    driver = _TimeoutDriver(html="")
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: driver)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)

    row = {
        "Artist Name": "Timeout Artist",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/timeout.artist",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") == "pass_a_timeout"
    assert result.get("FB_Reason") == "timeout"
    assert driver.set_timeout_called == 20
    assert driver.stop_called is True


def test_explicit_fb_timeout_with_salvage_html(monkeypatch, enricher):
    html = '<html><body><a href="mailto:artist@test.com">email</a></body></html>'
    driver = _TimeoutDriver(html=html)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: driver)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    # Ensure email override allows keeping emails even if music signals are weak in this synthetic page.
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    row = {
        "Artist Name": "Timeout Artist",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/timeout.artist",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") != "pass_a_timeout"
    assert driver.stop_called is True
    # Salvaged HTML should still be parsed and produce the email.
    assert "artist@test.com" in (result.get("Email_All") or "") or result.get("Email") == "artist@test.com"


def test_explicit_content_unavailable_allows_one_pass_b_discovery_fallback(monkeypatch, enricher):
    explicit_primary = "https://www.facebook.com/stale.primary"
    explicit_secondary = "https://www.facebook.com/stale.secondary"
    discovered_primary = "https://www.facebook.com/discovered.primary"
    discovered_fallback = "https://www.facebook.com/discovered.fallback"
    events = []
    search_calls = []

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())

    def fake_search(artist_name, location="", allow_anon=True, song_title="", row=None):
        search_calls.append((artist_name, location, allow_anon, song_title))
        events.append(("search", artist_name))
        enricher._last_search_candidates = [
            {"url": discovered_primary},
            {"url": discovered_fallback},
        ]
        enricher._last_selected_candidate_context = enricher._last_search_candidates[0]
        return discovered_primary

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        events.append(("scrape", fb_url))
        if fb_url in {explicit_primary, explicit_secondary, discovered_primary}:
            return None, [], "session", "content_unavailable"
        if fb_url == discovered_fallback:
            return (
                nmfb.NightModeFacebookResult(
                    email="fallback@example.com",
                    email_all="fallback@example.com",
                    facebook_url=fb_url,
                    email_source_url=fb_url,
                    email_extract_method="regex",
                ),
                ["fallback@example.com"],
                "session",
                "found_email",
            )
        raise AssertionError(f"unexpected FB scrape target: {fb_url}")

    monkeypatch.setattr(enricher, "_search_for_page", fake_search)
    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Fallback Artist",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": explicit_primary,
        "Social Link": explicit_secondary,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert search_calls == [("Fallback Artist", "", False, "")]
    assert [event for event in events if event[0] == "search"] == [("search", "Fallback Artist")]
    assert result.get("Email") == "fallback@example.com"
    assert "fallback@example.com" in (result.get("Email_All") or "")
    assert result.get("Facebook_URL") == explicit_primary
    assert result.get("FB_Status") != "pass_a_content_unavailable"


def test_explicit_content_unavailable_triggers_search_once_per_row(monkeypatch, enricher):
    explicit_primary = "https://www.facebook.com/stale.primary.onlyonce"
    explicit_secondary = "https://www.facebook.com/stale.secondary.onlyonce"
    scrape_calls = []
    search_calls = []

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        scrape_calls.append(fb_url)
        return None, [], "session", "content_unavailable"

    def fake_search(artist_name, location="", allow_anon=True, song_title="", row=None):
        search_calls.append((artist_name, location, allow_anon, song_title))
        return ""

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    monkeypatch.setattr(enricher, "_search_for_page", fake_search)

    row = {
        "Artist Name": "Single Search Artist",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": explicit_primary,
        "Social Link": explicit_secondary,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert scrape_calls == [explicit_primary, explicit_secondary]
    assert search_calls == [("Single Search Artist", "", False, "")]
    assert result.get("FB_Status") == "pass_a_content_unavailable"


def test_mixed_explicit_outcomes_do_not_broaden_into_discovery(monkeypatch, enricher):
    explicit_unavailable = "https://www.facebook.com/stale.mixed"
    explicit_usable = "https://www.facebook.com/usable.mixed"
    search_calls = []

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        if fb_url == explicit_unavailable:
            return None, [], "session", "content_unavailable"
        if fb_url == explicit_usable:
            return (
                nmfb.NightModeFacebookResult(
                    email="",
                    email_all="",
                    facebook_url=fb_url,
                    email_source_url=fb_url,
                    email_extract_method="regex",
                ),
                [],
                "session",
                "no_email_on_page",
            )
        raise AssertionError(f"unexpected FB scrape target: {fb_url}")

    def fail_search(*args, **kwargs):
        search_calls.append((args, kwargs))
        raise AssertionError("mixed explicit outcomes should not invoke PASS B search")

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    monkeypatch.setattr(enricher, "_search_for_page", fail_search)

    row = {
        "Artist Name": "Mixed Explicit Artist",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": explicit_unavailable,
        "Social Link": explicit_usable,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert search_calls == []
    assert result.get("FB_Status") == "pass_a_no_email_on_page"
    assert result.get("FB_Reason") == "session_fetch_ok_no_email"


def test_explicit_visible_contact_surface_rescue_runs_on_live_pass_a_seam(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    main_url = "https://www.facebook.com/iamgodlands"
    about_url = "https://www.facebook.com/iamgodlands/about"
    visible_email = "mikeadams@littleempiremusic.com"
    telemetry_email = "trace@o363271.ingest.us.sentry.io"
    fetch_calls = []
    search_calls = []

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: search_calls.append((args, kwargs)) or "",
    )
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        fetch_calls.append((url, collect_surfaces))
        return (
            """
            <html>
              <head>
                <title>GODLANDS</title>
              </head>
              <body>
                <div>Loaded Facebook page.</div>
              </body>
            </html>
            """,
            main_url,
        )

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=main_url,
                html="<html><body><div>Loaded Facebook page.</div></body></html>",
                rendered_text="Loaded Facebook page.",
                anchor_values=[],
            ),
            secondary_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=about_url,
                resolved_url=about_url,
                html="<html><body><div>About loaded.</div></body></html>",
                rendered_text=f"About Contact and basic info {visible_email}",
                anchor_values=[],
            ),
            main_emails=[],
            secondary_emails=[telemetry_email],
            secondary_attempted=True,
            combined_emails=[telemetry_email],
            final_resolved_url=about_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)

    row = {
        "Artist Name": "GODLANDS",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": main_url,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert fetch_calls == [(main_url, True)]
    assert search_calls == []
    assert result.get("Email") == visible_email
    assert visible_email in (result.get("Email_All") or "")
    assert telemetry_email not in (result.get("Email_All") or "")
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"
    assert result.get("FB_Email_Source") == "about"
    assert any("[FB About Extract] scanning visible contact surface" in msg for msg in logs)
    assert any(f"[FB About Extract] email_found={visible_email}" in msg for msg in logs)


def test_explicit_visible_contact_surfaces_thread_to_pass_a_exit_seam(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    main_url = "https://www.facebook.com/iamgodlands"
    about_url = "https://www.facebook.com/iamgodlands/about"
    visible_email = "mikeadams@littleempiremusic.com"
    received_surfaces = []

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PASS A should not invoke PASS B search")),
    )
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        assert url == main_url
        return (
            """
            <html>
              <head>
                <title>GODLANDS</title>
              </head>
              <body>
                <div>Loaded Facebook page.</div>
              </body>
            </html>
            """,
            main_url,
        )

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=main_url,
                html="<html><body><div>Loaded Facebook page.</div></body></html>",
                rendered_text="Loaded Facebook page.",
                anchor_values=[],
            ),
            secondary_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=about_url,
                resolved_url=about_url,
                html=f"<html><body><div>About Contact and basic info {visible_email}</div></body></html>",
                rendered_text="",
                anchor_values=[],
            ),
            main_emails=[],
            secondary_emails=[],
            secondary_attempted=True,
            combined_emails=[],
            final_resolved_url=about_url,
        )

    original_rescue = nmfb.NightModeFacebookEnricher._rescue_explicit_pass_a_visible_contact_email

    def wrapped_rescue(self, row, artist_name, *, fallback_page_url="", visible_contact_surfaces=None):  # noqa: ANN001
        received_surfaces[:] = list(visible_contact_surfaces or [])
        self._last_pass_a_visible_contact_surfaces = []
        return original_rescue(
            self,
            row,
            artist_name,
            fallback_page_url=fallback_page_url,
            visible_contact_surfaces=visible_contact_surfaces,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)
    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_rescue_explicit_pass_a_visible_contact_email", wrapped_rescue)

    row = {
        "Artist Name": "GODLANDS",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": main_url,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert received_surfaces
    assert [label for _surface, label in received_surfaces] == ["about", "about"]
    assert received_surfaces[0][0].resolved_url == about_url
    assert received_surfaces[1][0].resolved_url == main_url
    assert result.get("Email") == visible_email
    assert visible_email in (result.get("Email_All") or "")
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"
    assert result.get("FB_Email_Source") == "about"
    assert any("[FB About Extract] scanning visible contact surface" in msg for msg in logs)
    assert any(f"[FB About Extract] email_found={visible_email}" in msg for msg in logs)
