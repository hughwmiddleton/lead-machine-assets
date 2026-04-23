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
