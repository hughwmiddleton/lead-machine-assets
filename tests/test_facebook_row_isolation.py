import json

import cross_directory_enricher as cde
import night_mode_fb
from email_provenance import EMAIL_PROVENANCE_JSON_COL


def _enricher():
    return night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        use_shared_session=False,
    )


def _accepted_result(candidate: str, source: str, email: str) -> night_mode_fb.NightModeFacebookResult:
    return night_mode_fb.NightModeFacebookResult(
        email=email,
        email_all=email,
        facebook_url=source,
        email_source_url=source,
        email_extract_method="regex",
        candidate_url=candidate,
        accepted=True,
    )


def test_stimpies_then_meiia_cannot_inherit_facebook_contact() -> None:
    enricher = _enricher()
    stimpies_url = "https://www.facebook.com/stimpiess"
    stimpies = enricher._apply_night_fb_result(
        {"Artist Name": "Stimpies", "Facebook_URL": stimpies_url, "Email": "", "Email_All": ""},
        _accepted_result(stimpies_url, stimpies_url, "hello@stimpies.band"),
        ["hello@stimpies.band"],
        stimpies_url,
    )
    meiia_url = "https://www.facebook.com/meiialiveshere"
    meiia = enricher._apply_night_fb_result(
        {"Artist Name": "Meiia", "Facebook_URL": meiia_url, "Email": "", "Email_All": ""},
        _accepted_result(meiia_url, stimpies_url, "hello@stimpies.band"),
        ["hello@stimpies.band"],
        stimpies_url,
    )

    assert stimpies["Facebook_URL"] == stimpies_url
    assert stimpies["Email"] == "hello@stimpies.band"
    assert meiia["Facebook_URL"] == meiia_url
    assert meiia["Email"] == ""
    assert meiia["Email_All"] == ""
    assert "__fb_emails_applied" not in meiia
    assert meiia["FB_Status"] == "candidate_source_mismatch"


def test_previous_success_then_current_failure_clears_row_scoped_state(monkeypatch) -> None:
    enricher = _enricher()
    enricher._last_selected_candidate_context = {"url": "https://www.facebook.com/stimpiess"}
    enricher._last_search_candidates = [{"url": "https://www.facebook.com/stimpiess"}]
    enricher._last_search_reject_reason = "old_reason"
    enricher._last_explicit_guard_reason = "old_guard"
    enricher._last_fb_timeout_url = "https://www.facebook.com/stimpiess"

    result = enricher.enrich_row_with_facebook_night(
        {"Artist Name": "Meiia", "Source Directory": "Unearthed", "Email": "", "Email_All": ""}
    )

    assert result["FB_Status"] == "no_canonical_fb_url"
    assert enricher._last_selected_candidate_context is None
    assert enricher._last_search_candidates == []
    assert enricher._last_search_reject_reason == ""
    assert enricher._last_explicit_guard_reason == ""
    assert enricher._last_fb_timeout_url == ""


def test_failed_navigation_does_not_reuse_open_previous_browser_page(monkeypatch) -> None:
    class Driver:
        current_url = "https://www.facebook.com/stimpiess"
        page_source = "<html>hello@stimpies.band</html>"

    class Session:
        last_nav_timed_out = True
        last_nav_current_url = "https://www.facebook.com/meiialiveshere"
        last_nav_page_source = ""

        def navigate(self, *args, **kwargs):
            return Driver()

    enricher = _enricher()
    monkeypatch.setattr(enricher, "_ensure_session", lambda *args, **kwargs: Session())

    html, resolved = enricher._fetch_html_with_url(
        "https://www.facebook.com/meiialiveshere",
        skip_pre_nav_session_validation=True,
    )

    assert html is None
    assert resolved == "https://www.facebook.com/meiialiveshere"
    assert enricher._last_fb_surface_html is None


def test_normal_enrichment_rejects_stale_browser_surface() -> None:
    class Driver:
        current_url = "https://www.facebook.com/stimpiess"
        page_source = "<html><body>hello@stimpies.band</body></html>"

    class Session:
        driver = Driver()
        last_nav_current_url = ""
        last_nav_page_source = ""

        def navigate(self, *args, **kwargs):
            return self.driver

    emails, resolved, reason = cde._extract_fb_emails_bounded(
        Driver(),
        "https://www.facebook.com/meiialiveshere",
        fb_session=Session(),
    )

    assert emails == []
    assert resolved == "https://www.facebook.com/meiialiveshere"
    assert reason in {"candidate_source_mismatch", "fetch_error"}


def test_two_consecutive_valid_artists_keep_row_specific_values_and_provenance() -> None:
    enricher = _enricher()
    rows = []
    for artist, slug, email in (
        ("Stimpies", "stimpiess", "hello@stimpies.band"),
        ("Meiia", "meiialiveshere", "booking@meiia.com"),
    ):
        url = f"https://www.facebook.com/{slug}"
        rows.append(
            enricher._apply_night_fb_result(
                {"Artist Name": artist, "Facebook_URL": url, "Email": "", "Email_All": ""},
                _accepted_result(url, url, email),
                [email],
                url,
            )
        )

    assert rows[0]["__fb_emails_applied"] == "hello@stimpies.band"
    assert rows[1]["__fb_emails_applied"] == "booking@meiia.com"
    assert "booking@meiia.com" not in rows[0]["Email_All"]
    assert "hello@stimpies.band" not in rows[1]["Email_All"]
    first_provenance = json.loads(rows[0][EMAIL_PROVENANCE_JSON_COL])
    second_provenance = json.loads(rows[1][EMAIL_PROVENANCE_JSON_COL])
    assert first_provenance["hello@stimpies.band"]["source_url"].endswith("stimpiess")
    assert second_provenance["booking@meiia.com"]["source_url"].endswith("meiialiveshere")


def test_explicit_candidate_is_the_only_url_allowed_to_write_back() -> None:
    enricher = _enricher()
    candidate = "https://www.facebook.com/meiialiveshere"
    stale = "https://www.facebook.com/stimpiess"
    row = enricher._apply_night_fb_result(
        {"Artist Name": "Meiia", "Facebook_URL": candidate, "Email": "", "Email_All": ""},
        _accepted_result(candidate, stale, "hello@stimpies.band"),
        ["hello@stimpies.band"],
        stale,
    )

    assert row["Facebook_URL"] == candidate
    assert row["Email"] == ""


def test_email_source_must_match_selected_candidate() -> None:
    enricher = _enricher()
    candidate = "https://www.facebook.com/meiialiveshere"
    result = _accepted_result(candidate, candidate, "booking@meiia.com")
    result.email_source_url = "https://www.facebook.com/stimpiess"

    row = enricher._apply_night_fb_result(
        {"Artist Name": "Meiia", "Facebook_URL": candidate, "Email": "", "Email_All": ""},
        result,
        ["booking@meiia.com"],
        candidate,
    )

    assert row["Email"] == ""
    assert EMAIL_PROVENANCE_JSON_COL not in row
    assert row["FB_Reason"] == "candidate_source_mismatch"


def test_private_share_and_plugin_routes_have_no_candidate_ownership() -> None:
    candidate = "https://www.facebook.com/meiialiveshere"
    for route in (
        "https://www.facebook.com/share/abc",
        "https://www.facebook.com/share.php?u=x",
        "https://www.facebook.com/plugins/page.php?href=x",
        "https://www.facebook.com/messages/t/123",
    ):
        assert not night_mode_fb.facebook_source_belongs_to_candidate(candidate, route)
        assert not night_mode_fb.facebook_source_belongs_to_candidate(route, route)
