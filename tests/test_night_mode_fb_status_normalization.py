import night_mode_fb


def _enricher():
    # Minimal enricher instance; logger None keeps tests quiet.
    return night_mode_fb.NightModeFacebookEnricher(legacy_module=None, username="", password="", logger=None)


def test_missing_url_status_becomes_ok_when_fb_url_found():
    enricher = _enricher()
    row = {"Artist Name": "Test Artist", "FB_Status": "no_fb_url"}
    result = night_mode_fb.NightModeFacebookResult(facebook_url="https://facebook.com/test-artist")

    updated = enricher._apply_night_fb_result(row, result, emails=[], page_url=result.facebook_url)

    assert updated["FB_Status"] == "ok"
    assert updated["Facebook_URL"] == "https://www.facebook.com/test-artist"


def test_pass_a_skipped_status_becomes_ok_when_fb_url_found():
    enricher = _enricher()
    row = {"Artist Name": "Test Artist", "FB_Status": "pass_a_skipped_no_fb_url"}
    result = night_mode_fb.NightModeFacebookResult(facebook_url="https://facebook.com/test-artist")

    updated = enricher._apply_night_fb_result(row, result, emails=[], page_url=result.facebook_url)

    assert updated["FB_Status"] == "ok"
    assert updated["Facebook_URL"] == "https://www.facebook.com/test-artist"


def test_unresolved_share_url_does_not_become_canonical_facebook_url():
    enricher = _enricher()
    row = {"Artist Name": "Share Artist", "FB_Status": "pass_a_skipped_no_fb_url"}
    result = night_mode_fb.NightModeFacebookResult(
        facebook_url="https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr"
    )

    updated = enricher._apply_night_fb_result(row, result, emails=[], page_url=result.facebook_url)

    assert updated["FB_Status"] == "pass_a_skipped_no_fb_url"
    assert updated.get("Facebook_URL", "") == ""


def test_terminal_status_remains_blocked():
    enricher = _enricher()
    row = {"Artist Name": "Blocked Artist", "FB_Status": "blocked"}
    result = night_mode_fb.NightModeFacebookResult(facebook_url="https://facebook.com/blocked-artist")

    updated = enricher._apply_night_fb_result(row, result, emails=[], page_url=result.facebook_url)

    assert updated["FB_Status"] == "blocked"
    # Guard against overwriting URL when status is terminal/rejected.
    assert "Facebook_URL" not in updated or updated["Facebook_URL"] == row.get("Facebook_URL", "")


def test_existing_canonical_facebook_url_is_not_replaced_by_different_target():
    enricher = _enricher()
    row = {
        "Artist Name": "Stable Artist",
        "FB_Status": "ok",
        "Facebook_URL": "https://www.facebook.com/stable-artist",
    }
    result = night_mode_fb.NightModeFacebookResult(facebook_url="https://facebook.com/other-artist")

    updated = enricher._apply_night_fb_result(row, result, emails=[], page_url=result.facebook_url)

    assert updated["Facebook_URL"] == "https://www.facebook.com/stable-artist"


def test_no_canonical_facebook_url_is_not_driver_error(monkeypatch):
    enricher = _enricher()
    logs = []
    enricher.logger = logs.append
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")

    result = enricher.enrich_row_with_facebook_night(
        {
            "Artist Name": "No Canonical Artist",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Source Directory": "unearthed",
        }
    )

    assert result["FB_Status"] == "no_canonical_fb_url"
    assert result["FB_Status"] != "driver_error"
    assert result["FB_Attempt_State"] == "fb_not_attempted"
    assert any("[Unearthed Path] no usable FB URL; skipping Night FB discovery" in msg for msg in logs)


def test_no_discovery_candidates_is_not_driver_error(monkeypatch):
    enricher = _enricher()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")

    result = enricher.enrich_row_with_facebook_night(
        {
            "Artist Name": "Discovery Empty Artist",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "SoundCloud Link": "https://soundcloud.com/discovery-empty-artist",
        }
    )

    assert result["FB_Status"] == "no_candidates"
    assert result["FB_Status"] != "driver_error"


def test_candidate_rejected_is_not_driver_error(monkeypatch):
    enricher = _enricher()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)

    def _rejecting_search(*args, **kwargs):
        enricher._last_search_reject_reason = "authority_mismatch"
        enricher._last_search_reject_score = 0.12
        return ""

    monkeypatch.setattr(enricher, "_search_for_page", _rejecting_search)

    result = enricher.enrich_row_with_facebook_night(
        {
            "Artist Name": "Rejected Candidate",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "SoundCloud Link": "https://soundcloud.com/rejected-candidate",
        }
    )

    assert result["FB_Status"] == "candidate_rejected"
    assert result["FB_Status"] != "driver_error"
    assert result["FB_Reason"] == "authority_mismatch"


def test_true_pass_a_driver_failure_is_driver_error(monkeypatch):
    enricher = _enricher()
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: False)

    def _raise_driver_error(*args, **kwargs):
        raise night_mode_fb.FacebookDriverError("driver_session_died")

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", _raise_driver_error)

    result = enricher.enrich_row_with_facebook_night(
        {
            "Artist Name": "Driver Failure Artist",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://www.facebook.com/driverfailureartist",
        }
    )

    assert result["FB_Status"] == "driver_error"
    assert result["FB_Attempt_State"] == "attempted_fb_timeout_or_fetch_error"


def test_pre_execution_session_failure_without_canonical_url_is_not_driver_error(monkeypatch):
    enricher = _enricher()
    enricher._session_failed = True
    enricher._session_failed_reason = "session_start_failed"
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")

    result = enricher.enrich_row_with_facebook_night(
        {
            "Artist Name": "Pre Execution Failure",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
        }
    )

    assert result["FB_Status"] == "no_canonical_fb_url"
    assert result["FB_Status"] != "driver_error"
    assert result["FB_Attempt_State"] == "fb_not_attempted"
