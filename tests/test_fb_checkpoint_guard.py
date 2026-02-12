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

    def _fake_scrape(*args, **kwargs):  # noqa: ANN001
        called["count"] += 1
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
    assert result.get("FB_Status") in ("pass_a_found_email", "pass_a_no_email_on_page", "ok", "explicit_url", "ok_explicit") or result.get("Email")
