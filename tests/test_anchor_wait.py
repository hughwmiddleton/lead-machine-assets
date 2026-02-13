import time

import night_mode_fb


def test_anchor_wait_populates(monkeypatch):
    class DummyDriver:
        pass

    driver = DummyDriver()
    state = {"count": 0}

    def fake_find_first(drv, selector):
        return "container"

    def fake_extract(container):
        # First call: 0 anchors, second call: 2 anchors
        state["count"] += 1
        anchors = list(range(0 if state["count"] == 1 else 2))
        return anchors, anchors

    monkeypatch.setattr(night_mode_fb, "_find_first", fake_find_first)
    monkeypatch.setattr(night_mode_fb, "_extract_anchor_hrefs", fake_extract)

    success, before, after, waited_ms = night_mode_fb._wait_for_anchor_population(
        driver,
        "div[role='main']",
        min_anchors=1,
        timeout=0.5,
        poll_seconds=0.01,
        logger=None,
        context={"row_id": 1, "artist": "Test Artist"},
    )

    assert success is True
    assert before == 0
    assert after >= 1
    assert waited_ms >= 0


def test_anchor_wait_handles_container_appearing(monkeypatch):
    class DummyDriver:
        pass

    calls = {"step": 0}

    def fake_find_first(drv, selector):
        calls["step"] += 1
        # None on first poll, real container afterwards
        return None if calls["step"] == 1 else "container"

    def fake_extract(container):
        if container is None:
            return [], []
        return ["a1"], ["el1"]

    monkeypatch.setattr(night_mode_fb, "_find_first", fake_find_first)
    monkeypatch.setattr(night_mode_fb, "_extract_anchor_hrefs", fake_extract)

    success, before, after, waited_ms = night_mode_fb._wait_for_anchor_population(
        DummyDriver(),
        "div[role='main']",
        min_anchors=1,
        timeout=0.3,
        poll_seconds=0.01,
        logger=None,
        context={"row_id": 2},
    )

    assert success is True
    assert before == 0  # first poll had no container/anchors
    assert after == 1
    assert waited_ms >= 0
