import json

import night_mode_fb


def _build_enricher(logs=None) -> night_mode_fb.NightModeFacebookEnricher:
    return night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=logs.append if logs is not None else None,
        use_shared_session=False,
    )


def _set_surface_state(
    enricher: night_mode_fb.NightModeFacebookEnricher,
    *,
    url: str,
    html: str = "",
    visible_text: str = "",
    anchor_values=None,
    reveal_actions=None,
    driver_kind: str = "session",
    render_invalid_reason: str = "",
) -> None:
    enricher._last_fb_surface_html = html
    enricher._last_fb_surface_html_available = True
    enricher._last_fb_surface_url = url
    enricher._last_fb_surface_driver_kind = driver_kind
    enricher._last_fb_visible_text = visible_text
    enricher._last_fb_visible_text_available = True
    enricher._last_fb_live_anchor_values = list(anchor_values or [])
    enricher._last_fb_anchor_values_available = True
    enricher._last_fb_reveal_actions = list(reveal_actions or [])
    enricher._last_fb_reveal_actions_available = True
    enricher._last_fb_render_invalid_reason = render_invalid_reason


def test_night_fb_evidence_debug_off_writes_no_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_DIR", tmp_path.as_posix())
    monkeypatch.delenv("NIGHT_FB_EVIDENCE_DEBUG", raising=False)

    enricher = _build_enricher()
    telemetry_email = "trace@o363271.ingest.us.sentry.io"

    def fake_scrape(*args, **kwargs):  # noqa: ANN001
        _set_surface_state(
            enricher,
            url="https://www.facebook.com/debugartist",
            html=f"<html><body>{telemetry_email}</body></html>",
            visible_text=telemetry_email,
            anchor_values=[f"mailto:{telemetry_email}"],
            reveal_actions=["contact_info_button"],
        )
        return (
            night_mode_fb.NightModeFacebookResult(
                email="",
                email_all="",
                facebook_url="https://www.facebook.com/debugartist",
            ),
            [],
            "session",
            "no_email_on_page",
        )

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", fake_scrape)

    result = enricher.enrich_row_with_facebook_night(
        {"Artist Name": "Debug Artist", "Facebook_URL": "https://www.facebook.com/debugartist"},
        row_index=7,
    )

    assert result["FB_Status"] == "pass_a_no_email_on_page"
    assert not (tmp_path / "fb_evidence_debug").exists()


def test_night_fb_evidence_debug_on_fail_row_writes_compact_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_DIR", tmp_path.as_posix())
    monkeypatch.setenv("NIGHT_FB_EVIDENCE_DEBUG", "1")

    enricher = _build_enricher()
    telemetry_email = "trace@o363271.ingest.us.sentry.io"

    def fake_scrape(*args, **kwargs):  # noqa: ANN001
        _set_surface_state(
            enricher,
            url="https://www.facebook.com/debugartist",
            html=f"<html><body>Email {telemetry_email}</body></html>",
            visible_text=f"Email {telemetry_email}",
            anchor_values=[f"mailto:{telemetry_email}"],
            reveal_actions=["contact_info_button"],
        )
        return (
            night_mode_fb.NightModeFacebookResult(
                email="",
                email_all="",
                facebook_url="https://www.facebook.com/debugartist",
            ),
            [],
            "session",
            "no_email_on_page",
        )

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", fake_scrape)

    result = enricher.enrich_row_with_facebook_night(
        {"Artist Name": "Debug Artist", "Facebook_URL": "https://www.facebook.com/debugartist"},
        row_index=7,
    )

    assert result["FB_Status"] == "pass_a_no_email_on_page"

    evidence_files = list((tmp_path / "fb_evidence_debug").glob("*.json"))
    assert len(evidence_files) == 1

    payload = json.loads(evidence_files[0].read_text(encoding="utf-8"))

    assert payload["identity"]["FB_Status"] == "pass_a_no_email_on_page"
    assert payload["identity"]["FB_Reason"] == "session_fetch_ok_no_email"
    assert payload["identity"]["FB_Attempt_State"] == "attempted_fb_no_email_on_page"
    assert payload["collector"]["html_contains_at"] is True
    assert payload["collector"]["visible_text_contains_at"] is True
    assert payload["collector"]["anchor_count"] == 1
    assert payload["collector"]["anchor_has_mailto"] is True
    assert payload["collector"]["reveal_actions"] == ["contact_info_button"]
    assert payload["extraction"]["emails_from_html"] == [telemetry_email]
    assert payload["extraction"]["emails_from_text"] == [telemetry_email]
    assert payload["extraction"]["emails_from_anchors"] == []
    assert payload["extraction"]["raw_merged_candidates"] == [telemetry_email]
    assert payload["writeback"]["final_email"] is None
    assert payload["writeback"]["final_email_all"] is None
    assert payload["writeback"]["had_upstream_email_candidate"] is True


def test_night_fb_evidence_debug_skips_success_rows(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_DIR", tmp_path.as_posix())
    monkeypatch.setenv("NIGHT_FB_EVIDENCE_DEBUG", "1")

    enricher = _build_enricher()

    def fake_scrape(*args, **kwargs):  # noqa: ANN001
        return (
            night_mode_fb.NightModeFacebookResult(
                email="bookings@debugartist.com",
                email_all="bookings@debugartist.com",
                facebook_url="https://www.facebook.com/debugartist",
            ),
            ["bookings@debugartist.com"],
            "session",
            "found_email",
        )

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", fake_scrape)

    result = enricher.enrich_row_with_facebook_night(
        {"Artist Name": "Debug Artist", "Facebook_URL": "https://www.facebook.com/debugartist"},
        row_index=8,
    )

    assert result["FB_Status"] == "pass_a_found_email"
    assert not (tmp_path / "fb_evidence_debug").exists()


def test_explicit_pass_a_exit_seam_rescues_visible_contact_email(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    visible_email = "bookings@debugartist.com"
    about_url = "https://www.facebook.com/debugartist/about_contact_and_basic_info"

    def fake_scrape(*args, **kwargs):  # noqa: ANN001
        _set_surface_state(
            enricher,
            url=about_url,
            html="<html><body><div>Contact</div></body></html>",
            visible_text=f"Contact {visible_email}",
            anchor_values=[],
        )
        enricher._last_pass_a_visible_contact_surfaces = [
            (
                night_mode_fb.FacebookAcceptedPageFetchResult(
                    requested_url=about_url,
                    resolved_url=about_url,
                    html="<html><body><div>Contact</div></body></html>",
                    rendered_text=f"Contact {visible_email}",
                    anchor_values=[],
                ),
                "about",
            )
        ]
        return (
            night_mode_fb.NightModeFacebookResult(
                email="",
                email_all="",
                facebook_url="https://www.facebook.com/debugartist",
            ),
            [],
            "session",
            "no_email_on_page",
        )

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", fake_scrape)
    monkeypatch.setattr(
        enricher,
        "_search_for_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("PASS A exit-seam rescue should return before PASS B")),
    )

    result = enricher.enrich_row_with_facebook_night(
        {"Artist Name": "Debug Artist", "Facebook_URL": "https://www.facebook.com/debugartist"},
        row_index=10,
    )

    assert result["FB_Status"] == "pass_a_found_email"
    assert result["FB_Reason"] == "explicit_url"
    assert result["Email"] == visible_email
    assert visible_email in (result["Email_All"] or "")
    assert result["FB_Email_Source"] == "about"
    assert any("[FB About Extract] scanning visible contact surface" in msg for msg in logs)
    assert any(f"[FB About Extract] email_found={visible_email}" in msg for msg in logs)


def test_night_fb_content_unavailable_evidence_includes_render_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_DIR", tmp_path.as_posix())
    monkeypatch.setenv("NIGHT_FB_EVIDENCE_DEBUG", "1")

    enricher = _build_enricher()

    def fake_scrape(*args, **kwargs):  # noqa: ANN001
        _set_surface_state(
            enricher,
            url="https://www.facebook.com/debugartist",
            html="<html><body>This content isn't available right now</body></html>",
            visible_text="This content isn't available right now",
            anchor_values=[],
            reveal_actions=[],
            render_invalid_reason="content_unavailable",
        )
        return None, [], "session", "content_unavailable"

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", fake_scrape)
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)

    result = enricher.enrich_row_with_facebook_night(
        {"Artist Name": "Debug Artist", "Facebook_URL": "https://www.facebook.com/debugartist"},
        row_index=9,
    )

    assert result["FB_Status"] == "pass_a_content_unavailable"

    evidence_files = list((tmp_path / "fb_evidence_debug").glob("*.json"))
    assert len(evidence_files) == 1

    payload = json.loads(evidence_files[0].read_text(encoding="utf-8"))

    assert payload["identity"]["FB_Status"] == "pass_a_content_unavailable"
    assert payload["render_health"]["render_invalid_reason"] == "content_unavailable"
    assert payload["render_health"]["login_wall"] is False
    assert payload["render_health"]["driver_kind"] == "session"
