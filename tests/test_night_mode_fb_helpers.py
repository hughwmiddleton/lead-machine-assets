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


def test_extract_emails_from_live_anchor_values() -> None:
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body><div>No visible email</div></body></html>",
        anchor_values=[
            "mailto:bookings@artist.com",
            "https://example.com/contact?email=press%40artist.com",
        ],
    )

    assert emails == ["bookings@artist.com", "press@artist.com"]
    assert used_mailto is True


def test_extract_emails_fast_path_finds_rendered_email_without_raw_html(monkeypatch) -> None:
    html = "<html><body></body></html>"
    samples = []

    def fake_extract(sample: str):  # noqa: ANN001
        samples.append(sample)
        if sample == "Rendered bookings@artist.com":
            return ["bookings@artist.com"]
        if sample == html:
            raise AssertionError("raw_html should be skipped on the fast path")
        return []

    monkeypatch.setattr(night_mode_fb, "_extract_fb_emails_from_text_sample", fake_extract)

    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        html,
        rendered_text="Rendered bookings@artist.com",
        stop_after_first_filtered=True,
    )

    assert emails == ["bookings@artist.com"]
    assert used_mailto is False
    assert html not in samples


def test_extract_emails_fast_path_skips_raw_html_branch(monkeypatch) -> None:
    html = "<html><body></body></html>"
    samples = []

    def fake_extract(sample: str):  # noqa: ANN001
        samples.append(sample)
        if sample == html:
            raise AssertionError("raw_html should not be scanned when stop_after_first_filtered=True")
        return []

    monkeypatch.setattr(night_mode_fb, "_extract_fb_emails_from_text_sample", fake_extract)

    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        html,
        rendered_text="No email here",
        stop_after_first_filtered=True,
    )

    assert emails == []
    assert used_mailto is False
    assert samples == ["No email here"]


def test_extract_emails_non_fast_path_still_scans_raw_html(monkeypatch) -> None:
    html = "<html><body><script>bookings@artist.com</script></body></html>"
    samples = []

    def fake_extract(sample: str):  # noqa: ANN001
        samples.append(sample)
        if sample == html:
            return ["bookings@artist.com"]
        return []

    monkeypatch.setattr(night_mode_fb, "_extract_fb_emails_from_text_sample", fake_extract)

    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        html,
        stop_after_first_filtered=False,
    )

    assert emails == ["bookings@artist.com"]
    assert used_mailto is False
    assert html in samples


def test_extract_emails_fast_path_still_short_circuits_on_mailto(monkeypatch) -> None:
    html = '<html><body><a href="mailto:bookings@artist.com">Email</a></body></html>'

    def fail_extract(sample: str):  # noqa: ANN001
        raise AssertionError(f"unexpected extractor call for sample: {sample}")

    monkeypatch.setattr(night_mode_fb, "_extract_fb_emails_from_text_sample", fail_extract)

    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        html,
        rendered_text="Rendered press@artist.com",
        anchor_values=["https://example.com/contact?email=press%40artist.com"],
        stop_after_first_filtered=True,
    )

    assert emails == ["bookings@artist.com"]
    assert used_mailto is True


def test_filter_low_quality_fb_emails_rejects_file_like_and_artifact_candidates() -> None:
    emails = night_mode_fb._filter_low_quality_fb_emails(
        [
            "to@nomograph.mastering",
            "at@melroseplace.wav",
            "by@tyrant.jpg",
            "artist@band.music",
            "hello@studio.studio",
            "booking@artist.com",
        ]
    )

    assert emails == [
        "artist@band.music",
        "hello@studio.studio",
        "booking@artist.com",
    ]


def test_rank_fb_email_candidates_prefers_about_surface() -> None:
    ranked = night_mode_fb._rank_fb_email_candidates(
        ["contact@randomsite.com", "contact@artist.com"],
        artist_slug="",
        source_context={
            "surfaces": {
                "contact@randomsite.com": "main",
                "contact@artist.com": "about",
            }
        },
    )

    assert ranked[0] == "contact@artist.com"


def test_rank_fb_email_candidates_prefers_about_surface_over_booking_bonus() -> None:
    ranked = night_mode_fb._rank_fb_email_candidates(
        ["mgmt@randomagency.com", "team@artist.com"],
        artist_slug="",
        source_context={
            "surfaces": {
                "mgmt@randomagency.com": "main",
                "team@artist.com": "about",
            }
        },
    )

    assert ranked[0] == "team@artist.com"


def test_choose_primary_email_prefers_artist_slug_match() -> None:
    primary = night_mode_fb._choose_primary_email(
        ["info@label.com", "mitchsantiago@gmail.com"],
        "mitchsantiago",
    )

    assert primary == "mitchsantiago@gmail.com"


def test_rank_fb_email_candidates_prefers_booking_over_generic() -> None:
    ranked = night_mode_fb._rank_fb_email_candidates(
        ["info@artist.com", "bookings@artist.com"],
        artist_slug="artist",
    )

    assert ranked[0] == "bookings@artist.com"


def test_rank_fb_email_candidates_is_unchanged_without_context() -> None:
    emails = ["info@artist.com", "bookings@artist.com"]

    ranked_without_context = night_mode_fb._rank_fb_email_candidates(
        emails,
        artist_slug="artist",
    )
    ranked_with_none = night_mode_fb._rank_fb_email_candidates(
        emails,
        artist_slug="artist",
        source_context=None,
    )

    assert ranked_with_none == ranked_without_context


def test_rank_fb_email_candidates_mailto_gets_small_boost() -> None:
    ranked = night_mode_fb._rank_fb_email_candidates(
        ["press@artist.com", "team@artist.com"],
        artist_slug="artist",
        source_context={
            "surfaces": {
                "press@artist.com": "main",
                "team@artist.com": "mailto",
            }
        },
    )

    assert ranked[0] == "team@artist.com"


def test_rank_fb_email_candidates_preserves_order_on_tie() -> None:
    ranked = night_mode_fb._rank_fb_email_candidates(
        ["press@artist.com", "team@artist.com"],
        artist_slug="artist",
    )

    assert ranked == ["press@artist.com", "team@artist.com"]


class _RevealDriver:
    def __init__(self, clicked=None):
        self.clicked = list(clicked or [])
        self.calls = []

    def execute_script(self, script, *args):  # noqa: ANN001
        self.calls.append((str(script or ""), args))
        if "fb_reveal_controls" in str(script or ""):
            return list(self.clicked)
        return []


class _RevealPriorityDriver:
    def __init__(self):
        self.calls = []

    def execute_script(self, script, *args):  # noqa: ANN001
        script_text = str(script or "")
        self.calls.append((script_text, args))
        if "fb_reveal_controls" not in script_text:
            return []
        assert "div[role=\"main\"]" in script_text
        assert "contact info" in script_text
        assert "details" in script_text
        assert "ordered.sort" in script_text
        return ["main details see more", "profile see more"]


def test_reveal_fb_contact_controls_prefers_main_details_expander() -> None:
    driver = _RevealPriorityDriver()

    clicked = night_mode_fb._reveal_fb_contact_controls(driver, max_clicks=1)

    assert clicked == ["main details see more"]
    assert len(driver.calls) == 1
    assert driver.calls[0][1][1] == 1


def test_reveal_fb_contact_controls_is_bounded() -> None:
    driver = _RevealDriver(clicked=["see more", "contact info", "about"])

    clicked = night_mode_fb._reveal_fb_contact_controls(driver, max_clicks=2)

    assert clicked == ["see more", "contact info"]


class _CollectSurfaceDriver:
    def __init__(self, page_source: str = "<html></html>", scroll_delta: int = 480):
        self._page_source = page_source
        self.scroll_delta = scroll_delta
        self.events = []
        self.capture_count = 0
        self.navigation_attempts = 0
        self.scroll_scripts = []

    @property
    def page_source(self) -> str:
        self.events.append("page_source")
        self.capture_count += 1
        return self._page_source

    def execute_script(self, script, *args):  # noqa: ANN001
        script_text = str(script or "")
        if "fb_email_surface_post_reveal_scroll" in script_text:
            self.events.append("scroll")
            self.scroll_scripts.append(script_text)
            return self.scroll_delta
        raise AssertionError(f"unexpected execute_script call: {script_text[:80]}")

    def get(self, url):  # noqa: ANN001
        self.navigation_attempts += 1
        raise AssertionError(f"unexpected navigation to {url}")


class _ContainerFallbackDriver:
    def __init__(self, blocks):  # noqa: ANN001
        self.blocks = list(blocks or [])
        self.execute_calls = 0

    def execute_script(self, script, *args):  # noqa: ANN001
        script_text = str(script or "")
        if "role=\"main\"" in script_text or "role=\"complementary\"" in script_text or "aside" in script_text:
            self.execute_calls += 1
            return list(self.blocks)
        raise AssertionError(f"unexpected execute_script call: {script_text[:80]}")


def test_extract_fb_visible_text_with_container_fallback_includes_visible_sidebar_blocks(monkeypatch) -> None:
    driver = _ContainerFallbackDriver(
        ["Photos and videos", "Intro Contact elliot@rustmgmt.com"]
    )

    monkeypatch.setattr(
        night_mode_fb,
        "_extract_rendered_visible_text_from_driver",
        lambda driver: "Late 90s Upcoming shows",
    )

    rendered_text = night_mode_fb._extract_fb_visible_text_with_container_fallback(driver)
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body>No email in source</body></html>",
        rendered_text=rendered_text,
        stop_after_first_filtered=True,
    )

    assert rendered_text == "\n".join(
        [
            "Late 90s Upcoming shows",
            "Photos and videos",
            "Intro Contact elliot@rustmgmt.com",
        ]
    )
    assert driver.execute_calls == 1
    assert emails == ["elliot@rustmgmt.com"]
    assert used_mailto is False


def test_extract_fb_visible_text_with_container_fallback_preserves_main_email_fast_path(monkeypatch) -> None:
    driver = _ContainerFallbackDriver(["Intro Contact elliot@rustmgmt.com"])

    monkeypatch.setattr(
        night_mode_fb,
        "_extract_rendered_visible_text_from_driver",
        lambda driver: "Main stage contact bookings@artist.com",
    )

    rendered_text = night_mode_fb._extract_fb_visible_text_with_container_fallback(driver)

    assert rendered_text == "Main stage contact bookings@artist.com"
    assert driver.execute_calls == 0


def test_collect_fb_email_surface_state_sequences_reveal_scroll_then_capture(monkeypatch) -> None:
    driver = _CollectSurfaceDriver(page_source="<html>visible@artist.com</html>")
    events = driver.events
    capture_calls = {"rendered": 0, "anchors": 0}

    monkeypatch.setattr(
        night_mode_fb,
        "_reveal_fb_contact_controls",
        lambda driver, logger=None: events.append("reveal") or ["see more"],
    )
    monkeypatch.setattr(
        night_mode_fb,
        "_extract_fb_visible_text_with_container_fallback",
        lambda driver: events.append("rendered_text") or capture_calls.__setitem__("rendered", capture_calls["rendered"] + 1) or "Contact visible@artist.com",
    )
    monkeypatch.setattr(
        night_mode_fb,
        "_collect_fb_live_anchor_targets",
        lambda driver: events.append("anchors") or capture_calls.__setitem__("anchors", capture_calls["anchors"] + 1) or ["mailto:visible@artist.com"],
    )
    sleep_calls = []
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    page_source, rendered_text, anchor_values, reveal_actions = night_mode_fb._collect_fb_email_surface_state(driver)

    assert reveal_actions == ["see more"]
    assert page_source == "<html>visible@artist.com</html>"
    assert rendered_text == "Contact visible@artist.com"
    assert anchor_values == ["mailto:visible@artist.com"]
    assert events == ["reveal", "scroll", "page_source", "rendered_text", "anchors"]
    assert len(driver.scroll_scripts) == 1
    assert driver.capture_count == 1
    assert capture_calls == {"rendered": 1, "anchors": 1}
    assert sleep_calls == [0.15]


def test_collect_fb_email_surface_state_keeps_single_capture_without_navigation_or_retries(monkeypatch) -> None:
    driver = _CollectSurfaceDriver(
        page_source="<html><body>Already visible bookings@artist.com</body></html>",
        scroll_delta=0,
    )
    events = driver.events
    capture_calls = {"rendered": 0, "anchors": 0}

    monkeypatch.setattr(
        night_mode_fb,
        "_reveal_fb_contact_controls",
        lambda driver, logger=None: events.append("reveal") or [],
    )
    monkeypatch.setattr(
        night_mode_fb,
        "_extract_fb_visible_text_with_container_fallback",
        lambda driver: events.append("rendered_text") or capture_calls.__setitem__("rendered", capture_calls["rendered"] + 1) or "Already visible bookings@artist.com",
    )
    monkeypatch.setattr(
        night_mode_fb,
        "_collect_fb_live_anchor_targets",
        lambda driver: events.append("anchors") or capture_calls.__setitem__("anchors", capture_calls["anchors"] + 1) or [],
    )
    sleep_calls = []
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    page_source, rendered_text, anchor_values, reveal_actions = night_mode_fb._collect_fb_email_surface_state(driver)

    assert reveal_actions == []
    assert page_source == "<html><body>Already visible bookings@artist.com</body></html>"
    assert rendered_text == "Already visible bookings@artist.com"
    assert anchor_values == []
    assert events == ["reveal", "scroll", "page_source", "rendered_text", "anchors"]
    assert len(driver.scroll_scripts) == 1
    assert driver.capture_count == 1
    assert capture_calls == {"rendered": 1, "anchors": 1}
    assert driver.navigation_attempts == 0
    assert sleep_calls == []


def test_build_result_filters_telemetry_only_email() -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )

    result = enricher._build_result(
        ["abc@o363271.ingest.us.sentry.io"],
        "",
        "https://www.facebook.com/testartist",
        "Test Artist",
    )

    assert result is None


def test_build_result_ranks_primary_from_combined_main_and_about_surfaces() -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )

    result = enricher._build_result(
        ["bookings@artist.com", "about@artist.com"],
        "",
        "https://www.facebook.com/testartist",
        "Test Artist",
        source_context={
            "surfaces": {
                "bookings@artist.com": "main",
                "about@artist.com": "about",
            }
        },
    )

    assert result is not None
    assert result.email == "about@artist.com"
    assert result.email_all == "bookings@artist.com;about@artist.com"


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


def test_explicit_fb_entrypoint_urls_accept_share_entrypoint_but_block_share_php() -> None:
    share_urls = night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr"}
    )
    share_php_urls = night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Social Link": "https://www.facebook.com/share.php?u=test"}
    )

    assert share_urls == ["https://www.facebook.com/share/19bactwuev"]
    assert share_php_urls == []


def test_explicit_fb_entrypoint_urls_canonicalize_pages_category_shapes() -> None:
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish"}
    ) == ["https://www.facebook.com/tbish"]
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish/"}
    ) == ["https://www.facebook.com/tbish"]
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish/about"}
    ) == ["https://www.facebook.com/tbish"]
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish-2105329779689847"}
    ) == ["https://www.facebook.com/tbish"]
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish-2105329779689847/about"}
    ) == ["https://www.facebook.com/tbish"]


def test_explicit_fb_entrypoint_urls_reject_ambiguous_pages_category_shapes() -> None:
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/"}
    ) == []
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/"}
    ) == []
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/nan"}
    ) == []
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish/videos"}
    ) == []
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish-live"}
    ) == ["https://www.facebook.com/tbish-live"]
    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(
        {"Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/-2105329779689847"}
    ) == []


def test_classify_explicit_fb_intake_uses_canonicalized_pages_category_url() -> None:
    decision = night_mode_fb.classify_explicit_fb_intake(
        {"Artist Name": "tbish", "Facebook_URL": "https://www.facebook.com/pages/category/Musician-Band/tbish/about"}
    )

    assert decision.accepted_urls == ["https://www.facebook.com/tbish"]
    assert decision.guard_reason == ""


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


def test_pass_a_uses_canonicalized_pages_category_url(monkeypatch) -> None:
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

    row = {"Artist Name": "Test Artist", "Social Link": "https://www.facebook.com/pages/category/Musician-Band/tbish/about"}

    result = enricher.enrich_row_with_facebook_night(row)

    assert calls["urls"] == ["https://www.facebook.com/tbish"]
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"


def test_pass_a_uses_share_url_from_social_link(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        assert goto_about is False
        assert url == "https://www.facebook.com/share/19bactwuev"
        enricher._last_fb_visible_text = "Bookings shareartist@test.com"
        enricher._last_fb_live_anchor_values = []
        return (
            "<html><body><div>Bookings shareartist@test.com</div></body></html>",
            "https://www.facebook.com/artistsharepage",
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)

    row = {
        "Artist Name": "Share Artist",
        "Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("Email") == "shareartist@test.com"
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"
    assert result.get("Facebook_URL") == "https://www.facebook.com/artistsharepage"


def test_pass_a_uses_rendered_visible_text_when_page_source_has_no_email(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(night_mode_fb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(night_mode_fb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    pages = {
        "https://www.facebook.com/renderednight": (
            '<html><body><a href="/renderednight/about">About</a></body></html>',
            "https://www.facebook.com/renderednight",
            "No email on main page",
        ),
        "https://www.facebook.com/renderednight/about": (
            "<html><body><div>About Contact and basic info Contact info</div></body></html>",
            "https://www.facebook.com/renderednight/about",
            "About Contact and basic info Contact info divebaryouth.artist@gmail.com",
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        html, resolved, visible_text = pages[url]
        enricher._last_fb_visible_text = visible_text
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)

    row = {
        "Artist Name": "Rendered Night",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/renderednight",
        "Facebook_URL": "https://www.facebook.com/renderednight",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("Email") == "divebaryouth.artist@gmail.com"
    assert "divebaryouth.artist@gmail.com" in (result.get("Email_All") or "")
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"


def test_pass_a_rendered_visible_text_without_email_keeps_no_email_status(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(night_mode_fb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(night_mode_fb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    pages = {
        "https://www.facebook.com/renderednightnone": (
            '<html><body><a href="/renderednightnone/about">About</a></body></html>',
            "https://www.facebook.com/renderednightnone",
            "No email on main page",
        ),
        "https://www.facebook.com/renderednightnone/about": (
            "<html><body><div>About Contact and basic info Contact info</div></body></html>",
            "https://www.facebook.com/renderednightnone/about",
            "About Contact and basic info Contact info",
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        html, resolved, visible_text = pages[url]
        enricher._last_fb_visible_text = visible_text
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)

    row = {
        "Artist Name": "Rendered Night None",
        "Email": "",
        "Email_All": "",
        "facebook_url": "https://www.facebook.com/renderednightnone",
        "Facebook_URL": "https://www.facebook.com/renderednightnone",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert result.get("Email", "") == ""
    assert result.get("FB_Status") == "pass_a_no_email_on_page"


def test_explicit_pass_a_valid_render_state_passes_without_recovery(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(night_mode_fb, "_night_fb_has_music_signals", lambda *args, **kwargs: True)

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        assert goto_about is False
        enricher._last_fb_visible_text = "Bookings healthy@artist.com"
        enricher._last_fb_live_anchor_values = []
        return "<html><body><div>Contact</div></body></html>", url

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(
        enricher,
        "_collect_current_fb_email_surface_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("healthy explicit PASS A page should not restabilize")),
    )

    candidate = enricher._scrape_single_fb_candidate(
        "https://www.facebook.com/healthyartist",
        {"Email_All": ""},
        "Healthy Artist",
        candidate_context={"explicit_accepted_url": True},
    )

    night_result, emails, driver_kind, outcome = candidate

    assert night_result is not None
    assert night_result.email == "healthy@artist.com"
    assert emails == ["healthy@artist.com"]
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_explicit_pass_a_invalid_render_state_recollects_once_and_recovers(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(night_mode_fb, "_night_fb_has_music_signals", lambda *args, **kwargs: True)

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        assert goto_about is False
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return "<html><body></body></html>", url

    restabilized = {"count": 0}

    def fake_recollect(*args, **kwargs):  # noqa: ANN001
        restabilized["count"] += 1
        return (
            "<html><body><div>Bookings recovered@artist.com</div></body></html>",
            "Bookings recovered@artist.com",
            [],
            [],
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_recollect)

    candidate = enricher._scrape_single_fb_candidate(
        "https://www.facebook.com/recoverableartist",
        {"Email_All": ""},
        "Recoverable Artist",
        candidate_context={"explicit_accepted_url": True},
    )

    night_result, emails, driver_kind, outcome = candidate

    assert restabilized["count"] == 1
    assert night_result is not None
    assert night_result.email == "recovered@artist.com"
    assert emails == ["recovered@artist.com"]
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_explicit_pass_a_invalid_render_state_returns_content_unavailable_after_single_recovery(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        assert goto_about is False
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return "<html><body></body></html>", url

    restabilized = {"count": 0}

    def fake_recollect(*args, **kwargs):  # noqa: ANN001
        restabilized["count"] += 1
        return "<html><body></body></html>", "", [], []

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_recollect)

    candidate = enricher._scrape_single_fb_candidate(
        "https://www.facebook.com/brokenartist",
        {"Email_All": ""},
        "Broken Artist",
        candidate_context={"explicit_accepted_url": True},
    )

    night_result, emails, driver_kind, outcome = candidate

    assert restabilized["count"] == 1
    assert night_result is None
    assert emails == []
    assert driver_kind == "session"
    assert outcome == "content_unavailable"


def test_explicit_pass_a_invalid_render_state_preserves_content_unavailable_reason_for_fallback(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=logs.append,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_has_authenticated_session", lambda: True)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: object())
    monkeypatch.setattr(enricher, "_maybe_recover_or_skip_on_checkpoint", lambda: True)

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        assert goto_about is False
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return "<html><body></body></html>", url

    search_calls = []

    def fake_search(*args, **kwargs):  # noqa: ANN001
        search_calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(
        enricher,
        "_collect_current_fb_email_surface_state",
        lambda *args, **kwargs: ("<html><body></body></html>", "", [], []),
    )
    monkeypatch.setattr(enricher, "_search_for_page", fake_search)

    row = {
        "Artist Name": "Broken Artist",
        "Email": "",
        "Email_All": "",
        "Facebook_URL": "https://www.facebook.com/brokenartist",
    }

    result = enricher.enrich_row_with_facebook_night(row)

    assert len(search_calls) == 1
    assert result.get("FB_Status") == "pass_a_no_email_on_page"
    assert result.get("FB_Reason") == "content_unavailable"
    assert any("[Night FB][RenderGate]" in msg for msg in logs)


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


def test_fetch_html_with_url_logs_healthy_page_health(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=logs.append,
        use_shared_session=False,
    )

    class _Session:
        last_nav_timed_out = False

        def navigate(self, url):  # noqa: ANN001
            return type(
                "_Driver",
                (),
                {
                    "page_source": "<html><body>Artist page</body></html>",
                    "current_url": url,
                },
            )()

    monkeypatch.setattr(enricher, "_ensure_session", lambda: _Session())
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda session: session)

    html, current_url = enricher._fetch_html_with_url("https://www.facebook.com/example", goto_about=False)

    assert html
    assert current_url == "https://www.facebook.com/example"
    assert any(
        "[Night FB][Health]" in msg
        and "url=https://www.facebook.com/example" in msg
        and "captcha=0" in msg
        and "checkpoint=0" in msg
        and "login_wall=0" in msg
        for msg in logs
    )


def test_fetch_html_with_url_light_fetch_clears_stale_surfaces(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=logs.append,
        use_shared_session=False,
    )
    enricher._last_fb_visible_text = "stale@artist.com"
    enricher._last_fb_live_anchor_values = ["mailto:stale@artist.com"]
    enricher._last_fb_reveal_actions = ["expand"]

    class _Session:
        last_nav_timed_out = False

        def __init__(self) -> None:
            self.driver = None

        def navigate(self, url):  # noqa: ANN001
            self.driver = type(
                "_Driver",
                (),
                {
                    "page_source": "<html><body>No email on page.</body></html>",
                    "current_url": url,
                },
            )()
            return self.driver

    monkeypatch.setattr(enricher, "_ensure_session", lambda: _Session())
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda session: session)

    html, current_url = enricher._fetch_html_with_url(
        "https://www.facebook.com/example",
        goto_about=False,
        collect_surfaces=False,
    )
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        html or "",
        rendered_text=enricher._last_fb_visible_text,
        anchor_values=enricher._last_fb_live_anchor_values,
    )

    assert html == "<html><body>No email on page.</body></html>"
    assert current_url == "https://www.facebook.com/example"
    assert enricher._last_fb_visible_text == ""
    assert enricher._last_fb_live_anchor_values == []
    assert enricher._last_fb_reveal_actions == []
    assert emails == []
    assert used_mailto is False


def test_fetch_html_with_url_logs_login_wall_health(monkeypatch) -> None:
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=logs.append,
        use_shared_session=False,
    )

    class _Session:
        last_nav_timed_out = False

        def navigate(self, url):  # noqa: ANN001
            return type(
                "_Driver",
                (),
                {
                    "page_source": "<html><body>Log in to Facebook</body></html>",
                    "current_url": "https://www.facebook.com/login/?next=artist",
                },
            )()

    monkeypatch.setattr(enricher, "_ensure_session", lambda: _Session())
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda session: session)
    monkeypatch.setattr(enricher, "_refresh_driver", lambda session: None)

    html, current_url = enricher._fetch_html_with_url("https://www.facebook.com/example", goto_about=False)

    assert html is None
    assert current_url == "https://www.facebook.com/login/?next=artist"
    assert any(
        "[Night FB][Health]" in msg
        and "url=https://www.facebook.com/login/?next=artist" in msg
        and "login_wall=1" in msg
        for msg in logs
    )
