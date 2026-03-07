import pandas as pd
import pytest

import night_mode_fb
from origin_validator import dedupe_pre_auto_validate


def test_extract_emails_and_primary_selection() -> None:
    html = """
    <html>
      <body>
        <a href="mailto:bookings@zuso.com">Email</a>
        <div>Contact: ericaavenue@gmail.com</div>
        <div>alt: info@label.com</div>
      </body>
    </html>
    """
    emails, used_mailto = night_mode_fb._extract_emails_from_html(html)
    assert "ericaavenue@gmail.com" in emails
    assert "info@label.com" in emails
    assert used_mailto is True
    primary = night_mode_fb._choose_primary_email(emails, "ericaavenue")
    assert primary == "ericaavenue@gmail.com"
    merged = night_mode_fb._merge_email_all("bookings@zuso.com", emails)
    assert "bookings@zuso.com" in merged.split(";")
    assert len(set(merged.split(";"))) == len(merged.split(";"))


def test_extract_emails_from_script_json_and_escaped_variants() -> None:
    html = r"""
    <html>
      <body>
        <div>Visible: info@label.com</div>
        <script type="application/json">
          {"email":"mgmt@label.com","business_email":"info@label.com"}
        </script>
        <script>
          window.__data = {\"email\":\"mgmt@label.com\"};
        </script>
      </body>
    </html>
    """

    emails, used_mailto = night_mode_fb._extract_emails_from_html(html)

    assert emails == ["info@label.com", "mgmt@label.com"]
    assert used_mailto is False


def test_dedupe_prefers_fb_in_night_mode() -> None:
    df = pd.DataFrame(
        [
            {"Artist Name": "Surely Shirley", "Song Title": "Track", "Source Directory": "unearthed", "Email": ""},
            {
                "Artist Name": "Surely Shirley",
                "Song Title": "Track",
                "Source Directory": "unearthed",
                "Email": "",
                "Facebook_URL": "https://facebook.com/surely-shirley",
            },
        ]
    )
    deduped = dedupe_pre_auto_validate(df, "Source Directory", night_mode=True)
    assert len(deduped.index) == 1
    assert deduped.iloc[0]["Facebook_URL"] == "https://facebook.com/surely-shirley"


def test_extract_fb_urls_from_generic_links() -> None:
    row = {
        "Social Link": "https://instagram.com/foo | https://www.facebook.com/panicboomband",
        "External Links": "https://fb.com/panicboom; https://example.com",
    }

    urls = night_mode_fb._extract_fb_urls_for_night_mode(row)

    assert urls == ["https://www.facebook.com/panicboomband", "https://fb.com/panicboom"]


def test_classify_explicit_fb_intake_attempt_with_promotion_gap() -> None:
    row = {
        "Artist Name": "George Riley",
        "Facebook_URL": "",
        "Social Link": "https://www.facebook.com/georgerileymusic | https://instagram.com/georgeriley",
    }

    decision = night_mode_fb.classify_explicit_fb_intake(row)

    assert decision.outcome == "attempt"
    assert decision.accepted_urls == ["https://www.facebook.com/georgerileymusic"]
    assert decision.promotion_expected_missing_canonical is True
    assert decision.promotion_source == "Social Link"
    assert "Social Link" in decision.source_fields


def test_classify_explicit_fb_intake_rejects_invalid_placeholder() -> None:
    decision = night_mode_fb.classify_explicit_fb_intake(
        {"Artist Name": "Bad FB", "Facebook_URL": "https://facebook.com/nan/about"}
    )

    assert decision.outcome == "reject_invalid"
    assert decision.accepted_urls == []
    assert decision.rejected_invalid == ["https://facebook.com/nan/about"]
    assert decision.invalid_reason == "invalid_placeholder_or_malformed"


def test_classify_explicit_fb_intake_rejects_guarded_shape() -> None:
    decision = night_mode_fb.classify_explicit_fb_intake(
        {"Artist Name": "Guarded FB", "Facebook_URL": "https://www.facebook.com/share.php?u=test"}
    )

    assert decision.outcome == "reject_guard"
    assert decision.accepted_urls == []
    assert decision.rejected_guard == ["https://www.facebook.com/share.php?u=test"]
    assert decision.guard_reason == "share_surface"


def test_classify_explicit_fb_intake_reports_no_explicit_url() -> None:
    decision = night_mode_fb.classify_explicit_fb_intake(
        {"Artist Name": "No FB", "Social Link": "https://www.instagram.com/no-fb"}
    )

    assert decision.outcome == "no_explicit_url"
    assert decision.accepted_urls == []
    assert decision.rejected_invalid == []
    assert decision.rejected_guard == []


def test_pass_a_uses_fb_url_from_social_link(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)

    calls = {"urls": []}

    def _fake_scrape(url, *args, **kwargs):  # noqa: ANN001
        calls["urls"].append(url)
        night_result = night_mode_fb.NightModeFacebookResult(
            email="artist@test.com",
            email_all="artist@test.com",
            facebook_url=night_mode_fb._normalise_fb_url(url),
            email_extract_method="regex",
        )
        return night_result, ["artist@test.com"], "session", "found_email"

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", _fake_scrape)

    row = {"Artist Name": "Test Artist", "Social Link": "https://www.facebook.com/panicboomband"}

    result = enricher.enrich_row_with_facebook_night(row)

    assert calls["urls"] == ["https://www.facebook.com/panicboomband"]
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"


def test_explicit_fb_urls_canonicalized_and_deduped(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())

    calls = []

    def _fake_scrape(self, fb_url, *args, **kwargs):  # noqa: ANN001
        calls.append(fb_url)
        return None

    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _fake_scrape)

    row = {
        "Artist Name": "Dedup Artist",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://facebook.com/artist?ref=share",
        "Social Link": "https://facebook.com/artist/ | https://facebook.com/artist",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert len(calls) == 1, "Duplicate explicit FB URLs should be visited once"
    assert night_mode_fb._normalise_fb_url(calls[0]) == "https://www.facebook.com/artist"
    assert result.get("FB_Status"), "PASS A should still produce a status even without emails"


def test_explicit_fb_url_placeholders_rejected(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    logs = []
    enricher.logger = lambda msg: logs.append(msg)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")

    def _fail_scrape(self, *args, **kwargs):  # noqa: ANN001
        raise AssertionError("should not scrape invalid explicit URLs")

    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _fail_scrape)

    rows = [
        {"Artist Name": "Bad NaN", "Email": "", "Email_All": "", "Facebook_URL": "nan"},
        {"Artist Name": "Bad None", "Email": "", "Email_All": "", "Facebook_URL": None},
        {"Artist Name": "Bad Empty", "Email": "", "Email_All": "", "Facebook_URL": "   "},
    ]

    for row in rows:
        result = enricher.enrich_row_with_facebook_night(row)
        assert result.get("FB_Status") == "pass_a_skipped_no_fb_url"

    assert any('[Night FB][Explicit Intake]' in msg and 'outcome="reject_invalid"' in msg for msg in logs)

    # Valid URL still passes through.
    row_valid = {
        "Artist Name": "Good",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://facebook.com/artistpage",
    }

    calls = []

    def _capture_scrape(self, fb_url, *args, **kwargs):  # noqa: ANN001
        calls.append(fb_url)
        return None

    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _capture_scrape)

    enricher.enrich_row_with_facebook_night(row_valid)

    assert [night_mode_fb._normalise_fb_url(calls[0])] == ["https://www.facebook.com/artistpage"]


def test_valid_facebook_url_starts_scrape_even_when_email_column_is_stale(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        enricher,
        "_fetch_html_with_url",
        lambda url, goto_about=False: ('<html><body><a href="mailto:artist@test.com">Email</a></body></html>', url),
    )
    monkeypatch.setattr(night_mode_fb, "_night_fb_has_music_signals", lambda *args, **kwargs: True)

    row = {
        "Artist Name": "Example Band",
        "Email": "stale@example.com",
        "Email_All": "",
        "facebook_url": "https://facebook.com/exampleband",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("Email") == "artist@test.com"
    assert "artist@test.com" in (result.get("Email_All") or "")
    assert any('[Night FB] Starting FB scrape for artist="Example Band"' in msg for msg in logs)
    assert any('[Night FB][PASS A]' in msg and 'outcome="found_email"' in msg for msg in logs)


@pytest.mark.parametrize(
    ("value", "expect_skip_log"),
    [
        (None, True),
        (float("nan"), True),
        ("", False),
        ("   ", True),
        ("nan", True),
    ],
)
def test_invalid_direct_facebook_url_values_skip_without_scraping(monkeypatch, value, expect_skip_log) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")

    called = False

    def _fail_scrape(self, *args, **kwargs):  # noqa: ANN001
        nonlocal called
        called = True
        raise AssertionError("invalid direct facebook_url should not reach scraper")

    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _fail_scrape)

    row = {
        "Artist Name": "Bad FB",
        "Email": "",
        "Email_All": "",
        "facebook_url": value,
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert called is False
    assert result.get("FB_Status") == "pass_a_skipped_no_fb_url"
    if expect_skip_log:
        assert any("invalid facebook_url value" in msg for msg in logs)


def test_is_valid_fb_url_value_rejects_placeholder_paths() -> None:
    assert night_mode_fb._is_valid_fb_url_value("https://facebook.com/nan/about") is False
    assert night_mode_fb._is_valid_fb_url_value("/nan") is False


def test_is_valid_fb_url_value_rejects_root_only_fb_urls() -> None:
    assert night_mode_fb._is_valid_fb_url_value("https://facebook.com") is False
    assert night_mode_fb._is_valid_fb_url_value("facebook.com") is False


def test_is_invalid_fb_value_rejects_missing_placeholders() -> None:
    assert night_mode_fb._is_invalid_fb_value(float("nan")) is True
    assert night_mode_fb._is_invalid_fb_value("nan") is True
    assert night_mode_fb._is_invalid_fb_value("") is True
    assert night_mode_fb._is_invalid_fb_value("https://facebook.com/artist") is False


def test_normalise_fb_url_rejects_nan_paths() -> None:
    assert night_mode_fb._normalise_fb_url("facebook.com/nan") == ""
    assert night_mode_fb._normalise_fb_url("https://facebook.com/nan/about") == ""
    assert night_mode_fb._normalise_fb_url("/nan") == ""
    assert night_mode_fb._normalise_fb_url("https://facebook.com/realartist") == "https://www.facebook.com/realartist"


def test_canonicalize_explicit_urls_drop_invalid_nan_entries() -> None:
    urls = ["facebook.com/nan", "https://facebook.com/artist"]
    result = night_mode_fb._canonicalize_and_dedupe_explicit_fb_urls(urls)
    assert result == ["https://www.facebook.com/artist"]


def test_profile_php_explicit_urls_deduped(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())

    calls = []

    def _fake_scrape(self, fb_url, *args, **kwargs):  # noqa: ANN001
        calls.append(fb_url)
        return None

    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _fake_scrape)

    row = {
        "Artist Name": "Profile Artist",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://facebook.com/profile.php?id=123&ref=share",
        "Social Link": "https://facebook.com/profile.php?id=123 | https://facebook.com/profile.php?id=123/",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert len(calls) == 1, "Profile profile.php variants should be visited once"
    assert night_mode_fb._normalise_fb_url(calls[0]) == "https://www.facebook.com/profile.php?id=123"
    assert result.get("FB_Status"), "PASS A should still produce a status even without emails"


def test_guard_rejected_explicit_url_logs_reason(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: False)
    monkeypatch.setattr(enricher, "_should_allow_anonymous", lambda row: True)
    monkeypatch.setattr(enricher, "_get_anon_driver", lambda: object())
    monkeypatch.setattr(enricher, "_search_for_page", lambda *args, **kwargs: "")

    row = {
        "Artist Name": "Guarded",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://www.facebook.com/guarded/about",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("FB_Status")
    assert any('[Night FB][Explicit Intake]' in msg and 'outcome="attempt"' in msg and 'guard_reason="shape_disallowed"' in msg for msg in logs)
    assert any('[Night FB][Explicit Guard]' in msg and 'reason="shape_disallowed"' in msg for msg in logs)
