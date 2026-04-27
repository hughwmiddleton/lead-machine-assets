import json

import progress_state


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_init_progress_overwrites_and_uses_env_override(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    progress_path.write_text('{"phase": "old", "processed_rows": 99}', encoding="utf-8")
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))
    monkeypatch.setattr(progress_state, "_now", lambda: 1000.0)

    progress_state.init_progress(25, "run-1", {"phase": "processing", "current_source": "unearthed"})

    payload = _load(progress_path)
    assert payload["run_id"] == "run-1"
    assert payload["phase"] == "processing"
    assert payload["processed_rows"] == 0
    assert payload["total_rows"] == 25
    assert payload["percentage"] == 0.0
    assert payload["current_source"] == "unearthed"


def test_update_progress_percentage_eta_and_cap(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))
    times = iter([1000.0, 1020.0, 1040.0])
    monkeypatch.setattr(progress_state, "_now", lambda: next(times))

    progress_state.init_progress(20, "run-2")
    progress_state.update_progress(10, {"emails_found": 3})
    payload = _load(progress_path)
    assert payload["percentage"] == 50.0
    assert payload["elapsed_seconds"] == 20.0
    assert payload["eta_seconds"] == 20
    assert payload["emails_found"] == 3

    progress_state.update_progress(25)
    payload = _load(progress_path)
    assert payload["percentage"] == 100.0
    assert payload["eta_seconds"] == 0


def test_eta_null_until_enough_rows_or_unknown_total(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))
    times = iter([1000.0, 1000.5, 1002.0, 1003.0])
    monkeypatch.setattr(progress_state, "_now", lambda: next(times))

    progress_state.init_progress(100, "run-3")
    progress_state.update_progress(9)
    assert _load(progress_path)["eta_seconds"] is None

    progress_state.init_progress(None, "run-4")
    progress_state.update_progress(10)
    payload = _load(progress_path)
    assert payload["percentage"] is None
    assert payload["eta_seconds"] is None


def test_discovery_update_and_transition(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))
    times = iter([1000.0, 1000.3])
    monkeypatch.setattr(progress_state, "_now", lambda: next(times))

    progress_state.update_progress(
        0,
        {
            "phase": "discovery",
            "discovered_urls": 12,
            "current_source": "unearthed",
        },
    )
    discovery = _load(progress_path)
    assert discovery["phase"] == "discovery"
    assert discovery["discovered_urls"] == 12
    assert discovery["total_rows"] is None

    progress_state.init_progress(
        12,
        "run-5",
        {"phase": "processing", "current_source": "unearthed"},
    )
    transition = _load(progress_path)
    assert transition["phase"] == "processing"
    assert transition["total_rows"] == 12
    assert transition["processed_rows"] == 0


def test_discovery_updates_are_throttled_to_250ms(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))
    progress_state._last_discovery_write_at = 0.0
    times = iter([1000.0, 1000.1, 1000.3])
    monkeypatch.setattr(progress_state, "_now", lambda: next(times))

    progress_state.update_progress(0, {"phase": "discovery", "discovered_urls": 1})
    progress_state.update_progress(0, {"phase": "discovery", "discovered_urls": 2})
    assert _load(progress_path)["discovered_urls"] == 1

    progress_state.update_progress(0, {"phase": "discovery", "discovered_urls": 3})
    assert _load(progress_path)["discovered_urls"] == 3


def test_read_progress_missing_and_malformed_are_safe(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))

    assert progress_state.read_progress() == {"phase": "idle", "processed_rows": 0, "total_rows": None}
    progress_path.write_text("{not-json", encoding="utf-8")
    assert progress_state.read_progress() == {"phase": "idle", "processed_rows": 0, "total_rows": None}


def test_finalize_preserves_counts_and_sets_complete(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("LEAD_MACHINE_PROGRESS_FILE", str(progress_path))
    times = iter([1000.0, 1010.0, 1020.0])
    monkeypatch.setattr(progress_state, "_now", lambda: next(times))

    progress_state.init_progress(1000, "run-6")
    progress_state.update_progress(998)
    progress_state.finalize_progress({"emails_found": 184})

    payload = _load(progress_path)
    assert payload["phase"] == "complete"
    assert payload["processed_rows"] == 998
    assert payload["total_rows"] == 1000
    assert payload["percentage"] == 100.0
    assert payload["eta_seconds"] == 0
    assert payload["emails_found"] == 184
