import pytest

import night_mode_fb as nmfb
from selenium.common.exceptions import TimeoutException


class _DummyLegacy:
    def setup_facebook_driver(self):
        return object()


class _LegacyVisitedPage:
    def __init__(self, current_url: str, page_source: str):
        self.current_url = current_url
        self.page_source = page_source


@pytest.fixture
def enricher(monkeypatch):
    helper = nmfb.NightModeFacebookEnricher(
        _DummyLegacy(), username="user", password="pass", logger=None, use_shared_session=False
    )
    # Avoid real network/driver work.
    monkeypatch.setattr(helper, "_ensure_driver_alive", lambda session: session)
    return helper


def _run_unearthed_blind_case(monkeypatch, enricher, *, page_html: str, scrape_result):
    logs = []
    counts = {"search": 0, "scrape": 0}
    candidate_url = "https://www.facebook.com/unearthed.blind"
    driver = _LegacyVisitedPage(candidate_url, page_html)

    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(
        nmfb.NightModeFacebookEnricher,
        "_scrape_single_fb_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("explicit PASS A should not run")),
    )

    def fake_search(query, location="", allow_anon=True):
        counts["search"] += 1
        return candidate_url

    def fake_scrape(driver_obj, fb_url, logger=None):
        counts["scrape"] += 1
        assert driver_obj is driver
        assert fb_url == candidate_url
        return scrape_result

    monkeypatch.setattr(enricher, "_search_for_page", fake_search)
    monkeypatch.setattr(enricher, "_get_unearthed_driver", lambda: driver)
    monkeypatch.setattr(nmfb, "_scrape_fb_page_unearthed_legacy", fake_scrape)

    row = {
        "Artist Name": "Unearthed Blind",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "",
    }

    return enricher.enrich_row_with_facebook_night(row), counts, logs


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
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("Email") == "unearthed@example.com"


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

    assert observed["fb_url"] == "https://www.facebook.com/unearthed.promoted.existing"
    assert observed["explicit_accepted_url"] is True
    assert observed["email_all_before"] == "seed@example.com"
    assert result.get("FB_Status") == "pass_a_found_email"
    assert "fbwin@example.com" in (result.get("Email_All") or "")


def test_unearthed_without_explicit_url_keeps_legacy_path(monkeypatch, enricher):
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(
        enricher,
        "_has_authenticated_session",
        lambda: (_ for _ in ()).throw(AssertionError("explicit PASS A should not run")),
    )

    observed = {}

    def fake_legacy(result, artist_name, fb_urls):
        observed["artist_name"] = artist_name
        observed["fb_urls"] = list(fb_urls)
        updated = dict(result)
        updated["FB_Status"] = "unearthed_no_emails"
        return updated

    monkeypatch.setattr(enricher, "_enrich_row_unearthed_legacy", fake_legacy)

    row = {
        "Artist Name": "Unearthed Legacy",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["artist_name"] == "Unearthed Legacy"
    assert observed["fb_urls"] == []
    assert result.get("FB_Status") == "unearthed_no_emails"


def test_unearthed_no_url_blind_discovery_success(monkeypatch, enricher):
    result, counts, logs = _run_unearthed_blind_case(
        monkeypatch,
        enricher,
        page_html="<html><body><div>Bookings: blind@example.com</div></body></html>",
        scrape_result=(["blind@example.com"], "ok", "https://www.facebook.com/unearthed.blind"),
    )

    assert result.get("FB_Status") == "ok_unearthed_blind"
    assert result.get("Email") == "blind@example.com"
    assert result.get("Facebook_URL") == "https://www.facebook.com/unearthed.blind"
    assert counts == {"search": 1, "scrape": 1}
    assert any("[Unearthed Path] no usable FB URL; allowing bounded FB discovery" in msg for msg in logs)
    assert any("[Unearthed Path] entering Unearthed no-URL FB discovery" in msg for msg in logs)
    assert any("[Unearthed Path] discovery yielded candidate" in msg for msg in logs)


def test_unearthed_no_url_blind_discovery_no_email_keeps_legacy_result(monkeypatch, enricher):
    result, counts, _logs = _run_unearthed_blind_case(
        monkeypatch,
        enricher,
        page_html="<html><body><div>No contact details here.</div></body></html>",
        scrape_result=([], "no_emails", "https://www.facebook.com/unearthed.blind"),
    )

    assert result.get("FB_Status") == "no_emails"
    assert result.get("FB_Reason", "") == ""
    assert result.get("Email", "") == ""
    assert counts == {"search": 1, "scrape": 1}


def test_unearthed_no_url_blind_discovery_checkpoint_uses_night_classification(monkeypatch, enricher):
    result, _counts, logs = _run_unearthed_blind_case(
        monkeypatch,
        enricher,
        page_html="<html><body>Security Check. Confirm it's you.</body></html>",
        scrape_result=([], "no_emails", "https://www.facebook.com/unearthed.blind"),
    )

    assert result.get("FB_Status") == "checkpoint"
    assert result.get("FB_Reason") == "checkpoint"
    assert result.get(nmfb.FB_ATTEMPT_STATE_COL) == "attempted_fb_login_wall_or_checkpoint"
    assert any("context=unearthed_legacy_final_page" in msg and "checkpoint=1" in msg for msg in logs)


def test_unearthed_no_url_blind_discovery_login_wall_uses_night_classification(monkeypatch, enricher):
    result, _counts, logs = _run_unearthed_blind_case(
        monkeypatch,
        enricher,
        page_html="<html><body>Log in to Facebook</body></html>",
        scrape_result=([], "no_emails", "https://www.facebook.com/unearthed.blind"),
    )

    assert result.get("FB_Status") == "login_wall"
    assert result.get("FB_Reason") == "redirect_login"
    assert result.get(nmfb.FB_ATTEMPT_STATE_COL) == "attempted_fb_login_wall_or_checkpoint"
    assert any("context=unearthed_legacy_final_page" in msg and "login_wall=1" in msg for msg in logs)


def test_unearthed_no_url_blind_discovery_captcha_uses_night_classification(monkeypatch, enricher):
    result, _counts, logs = _run_unearthed_blind_case(
        monkeypatch,
        enricher,
        page_html="<html><body>Help us confirm captcha before you continue.</body></html>",
        scrape_result=([], "no_emails", "https://www.facebook.com/unearthed.blind"),
    )

    assert result.get("FB_Status") == "login_wall"
    assert result.get("FB_Reason") == "captcha"
    assert result.get(nmfb.FB_ATTEMPT_STATE_COL) == "attempted_fb_login_wall_or_checkpoint"
    assert any("context=unearthed_legacy_final_page" in msg and "captcha=1" in msg for msg in logs)


def test_unearthed_no_url_blind_discovery_warning_uses_night_classification(monkeypatch, enricher):
    result, _counts, logs = _run_unearthed_blind_case(
        monkeypatch,
        enricher,
        page_html="<html><body>Try again later.</body></html>",
        scrape_result=([], "no_emails", "https://www.facebook.com/unearthed.blind"),
    )

    assert result.get("FB_Status") == "warning_interstitial"
    assert result.get("FB_Reason") == "warning_interstitial"
    assert result.get(nmfb.FB_ATTEMPT_STATE_COL) == "attempted_fb_login_wall_or_checkpoint"
    assert any("context=unearthed_legacy_final_page" in msg and "warning=warning_interstitial" in msg for msg in logs)


def test_unearthed_no_url_blind_discovery_miss(monkeypatch, enricher):
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)
    monkeypatch.setattr(
        nmfb.NightModeFacebookEnricher,
        "_scrape_single_fb_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("explicit PASS A should not run")),
    )
    monkeypatch.setattr(enricher, "_search_for_page", lambda query, location="", allow_anon=True: "")

    row = {
        "Artist Name": "Unearthed Blind Miss",
        "Source Directory": "unearthed",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
        "Social Link": "",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") == "unearthed_no_candidates"
    assert any("[Unearthed Path] no usable FB URL; allowing bounded FB discovery" in msg for msg in logs)
    assert any("[Unearthed Path] entering Unearthed no-URL FB discovery" in msg for msg in logs)
    assert any("[Unearthed Path] discovery yielded no candidate" in msg for msg in logs)


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


def test_pass_a_resolves_share_entrypoint_before_scrape(monkeypatch, enricher):
    share_url = "https://www.facebook.com/share/19bactwuev"
    canonical_url = "https://www.facebook.com/artistsharepage"
    observed = {"fetch_calls": [], "scrape_calls": []}

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())

    def fake_fetch(url, goto_about=False, collect_surfaces=True):
        observed["fetch_calls"].append((url, goto_about, collect_surfaces))
        return "<html><body>share landing</body></html>", canonical_url

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["scrape_calls"].append(fb_url)
        assert fb_url == canonical_url
        return (
            nmfb.NightModeFacebookResult(
                email="share@example.com",
                email_all="share@example.com",
                facebook_url=fb_url,
                email_source_url=fb_url,
                email_extract_method="regex",
            ),
            ["share@example.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)

    row = {
        "Artist Name": "Share Artist",
        "Email": "",
        "Email_All": "",
        "Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["fetch_calls"] == [(share_url, False, False)]
    assert observed["scrape_calls"] == [canonical_url]
    assert result.get("Email") == "share@example.com"
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("Facebook_URL") == canonical_url


def test_unresolvable_share_entrypoint_fails_closed_without_explicit_scrape(monkeypatch, enricher):
    share_url = "https://www.facebook.com/share/19bactwuev"
    observed = {"scrape_calls": [], "search_calls": []}

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(
        enricher,
        "_fetch_html_with_url",
        lambda url, goto_about=False, collect_surfaces=True: ("<html><body>share</body></html>", share_url),
    )

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["scrape_calls"].append(fb_url)
        raise AssertionError("unresolved share URL should not reach explicit scrape")

    def fake_search(artist_name, location="", allow_anon=True, song_title="", row=None):
        observed["search_calls"].append((artist_name, location, allow_anon, song_title))
        return ""

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    monkeypatch.setattr(enricher, "_search_for_page", fake_search)

    row = {
        "Artist Name": "Share Artist",
        "Email": "",
        "Email_All": "",
        "Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["scrape_calls"] == []
    assert observed["search_calls"] == [("Share Artist", "", False, "")]
    assert result.get("FB_Status") == "pass_a_skipped_no_fb_url"


def test_share_entrypoint_resolved_junk_surface_is_rejected_before_scrape(monkeypatch, enricher):
    observed = {"scrape_calls": [], "search_calls": []}

    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(
        enricher,
        "_fetch_html_with_url",
        lambda url, goto_about=False, collect_surfaces=True: (
            "<html><body>junk surface</body></html>",
            "https://www.facebook.com/groups/artistshare",
        ),
    )

    def fake_scrape(self, fb_url, row, artist_name, allow_anon=False, candidate_context=None):
        observed["scrape_calls"].append(fb_url)
        raise AssertionError("junk resolved share URL should not reach explicit scrape")

    def fake_search(artist_name, location="", allow_anon=True, song_title="", row=None):
        observed["search_calls"].append((artist_name, location, allow_anon, song_title))
        return ""

    monkeypatch.setattr(nmfb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", fake_scrape)
    monkeypatch.setattr(enricher, "_search_for_page", fake_search)

    row = {
        "Artist Name": "Share Artist",
        "Email": "",
        "Email_All": "",
        "Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert observed["scrape_calls"] == []
    assert observed["search_calls"] == [("Share Artist", "", False, "")]
    assert result.get("FB_Status") == "pass_a_skipped_no_fb_url"


def test_prepare_explicit_fb_urls_leaves_canonical_urls_unchanged(monkeypatch, enricher):
    observed = {"resolved": 0}

    def fail_share_resolve(*args, **kwargs):
        observed["resolved"] += 1
        raise AssertionError("canonical explicit URLs should not trigger share resolution")

    monkeypatch.setattr(enricher, "_resolve_explicit_fb_share_url", fail_share_resolve)

    urls = enricher._prepare_explicit_fb_urls_for_pass_a(
        [
            "https://www.facebook.com/artistpage",
            "https://www.facebook.com/profile.php?id=123",
        ],
        allow_anon=False,
    )

    assert urls == [
        "https://www.facebook.com/artistpage",
        "https://www.facebook.com/profile.php?id=123",
    ]
    assert observed["resolved"] == 0


def test_share_resolution_restores_pass_a_page_budget(monkeypatch, enricher):
    enricher._page_budget_remaining = 2

    def fake_fetch(url, goto_about=False, collect_surfaces=True):
        enricher._page_budget_remaining -= 1
        return "<html><body>share landing</body></html>", "https://www.facebook.com/artistsharepage"

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)

    resolved = enricher._resolve_explicit_fb_share_url(
        "https://www.facebook.com/share/19bactwuev",
        allow_anon=False,
    )

    assert resolved == "https://www.facebook.com/artistsharepage"
    assert enricher._page_budget_remaining == 2


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
    assert discovered_fallback in (result.get("Facebook_URL") or result.get("facebook_url") or "")


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
    assert result.get("FB_Status") == "pass_a_no_email_on_page"


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
