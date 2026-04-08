import night_mode_fb


class _CheckpointSession:
    def __init__(self) -> None:
        self.last_health_ok = False
        self.last_health_reason = "checkpoint"


def test_checkpoint_guard_blocks_search(monkeypatch):
    monkeypatch.setenv("NIGHT_FB_CHECKPOINT_GUARD", "1")
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: _CheckpointSession())
    row = {"Artist Name": "Test Artist", "Facebook_URL": ""}

    result = enricher.enrich_row_with_facebook_night(row)

    assert result["FB_Status"] == "checkpoint_search_disabled"
    assert result["FB_Reason"] == "checkpoint"


def test_checkpoint_guard_allows_explicit_url(monkeypatch):
    monkeypatch.setenv("NIGHT_FB_CHECKPOINT_GUARD", "1")
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: _CheckpointSession())

    called = {"count": 0}
    observed = {}

    def _fake_scrape(*args, **kwargs):  # noqa: ANN001
        called["count"] += 1
        observed["allow_anon"] = kwargs.get("allow_anon")
        observed["candidate_context"] = kwargs.get("candidate_context") or {}
        night_result = night_mode_fb.NightModeFacebookResult(
            email="artist@test.com",
            email_all="artist@test.com",
            facebook_url="https://www.facebook.com/explicit",
        )
        return night_result, ["artist@test.com"], "session", "found_email"

    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda session: session)
    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", _fake_scrape)

    row = {"Artist Name": "Test Artist", "Facebook_URL": "https://www.facebook.com/explicit"}
    result = enricher.enrich_row_with_facebook_night(row)

    assert called["count"] == 1
    assert observed["allow_anon"] is False
    assert observed["candidate_context"].get("explicit_accepted_url") is True
    assert result.get("FB_Status") in ("pass_a_found_email", "pass_a_no_email_on_page", "ok", "explicit_url", "ok_explicit") or result.get("Email")


def test_trust_budget_disables_search_before_full_run_disable(monkeypatch):
    run_state = night_mode_fb.create_night_fb_run_state("user", "pass")
    logs = []
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="user",
        password="pass",
        logger=logs.append,
        use_shared_session=False,
        run_state=run_state,
    )

    healthy = {
        "captcha": False,
        "checkpoint": False,
        "login_wall": False,
        "auth_surface": "",
        "warning_reason": "",
        "search_miss_reason": "",
    }
    degraded_search = dict(healthy, search_miss_reason="overlay_zero_anchors")
    warning = dict(healthy, warning_reason="warning_interstitial")

    enricher._apply_trust_budget_health(healthy, context="page")
    assert run_state.trust_score == 0
    assert run_state.search_disabled_for_run is False
    assert run_state.disabled_for_run is False

    enricher._apply_trust_budget_health(degraded_search, context="search")
    enricher._apply_trust_budget_health(degraded_search, context="search")
    enricher._apply_trust_budget_health(degraded_search, context="search")

    assert run_state.trust_score == -3
    assert run_state.search_disabled_for_run is True
    assert run_state.search_disable_reason == "overlay_zero_anchors"
    assert run_state.disabled_for_run is False
    assert enricher._search_disabled_due_to_checkpoint is True

    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)
    monkeypatch.setattr(
        enricher,
        "_fetch_search_surface",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search should already be disabled")),
    )
    assert enricher._search_for_page("Test Artist", location="", allow_anon=True) is None

    enricher._apply_trust_budget_health(warning, context="page")

    assert run_state.trust_score == -5
    assert run_state.disabled_for_run is True
    assert run_state.disable_reason == "warning_interstitial"
    assert enricher.protective_shutdown is True
    assert enricher._skip_fb_due_to_checkpoint is True
    assert any("[Night FB][Trust Budget]" in msg and "action=disable_search" in msg for msg in logs)
    assert any("[Night FB][Trust Budget]" in msg and "action=disable_fb" in msg for msg in logs)
