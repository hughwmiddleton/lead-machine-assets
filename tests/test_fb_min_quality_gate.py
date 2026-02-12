import facebook_enrich
import night_mode_fb


class _DummySession:
    def __init__(self) -> None:
        self.last_health_ok = True
        self.last_health_reason = ""

    def navigate(self, _url):
        class _Driver:
            page_source = "<html></html>"
        return _Driver()


def test_min_quality_gate_rejects_bad_candidate(monkeypatch):
    monkeypatch.setenv("NIGHT_FB_MIN_QUALITY_GATE", "1")
    monkeypatch.setenv("NIGHT_FB_MIN_QUALITY_SCORE", "50")

    calls = {"count": 0}
    bad_cand = facebook_enrich.FbCandidate(
        name="Runway Collection",
        url="https://www.facebook.com/runwaycollection",
        category="Clothing brand",
    )

    def _fake_parse(html, logger=None, search_name=None):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            return [bad_cand]
        return []

    monkeypatch.setattr(night_mode_fb, "_parse_search_candidates", _fake_parse)

    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: _DummySession())
    monkeypatch.setattr(enricher, "_ensure_driver_alive", lambda session: session)
    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", lambda *args, **kwargs: None)

    page = enricher._search_for_page("Runway", location="", allow_anon=True)

    assert page is None
    assert calls["count"] >= 2  # initial + forced refine queries
