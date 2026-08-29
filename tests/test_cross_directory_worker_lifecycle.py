from types import SimpleNamespace

import pandas as pd

import cross_directory_enricher as cde


class CountingSession:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class CountingDriver:
    def __init__(self):
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


def _worker_with_counting_sessions(tmp_path, monkeypatch):
    session = CountingSession()
    bandcamp_session = CountingSession()
    monkeypatch.setattr(cde, "_build_session", lambda: session)
    monkeypatch.setattr(cde, "_build_bandcamp_session", lambda: bandcamp_session)
    worker = cde.CrossDirectoryEnricherWorker(
        (tmp_path / "seed.csv").as_posix(),
        (tmp_path / "output.csv").as_posix(),
        enable_live_search=False,
    )
    return worker, session, bandcamp_session


def _capture_signals(worker):
    logs = []
    finished = []
    worker.log_message = SimpleNamespace(emit=logs.append)
    worker.finished = SimpleNamespace(emit=finished.append)
    return logs, finished


def test_normal_run_closes_both_sessions_and_browser_once(tmp_path, monkeypatch):
    worker, session, bandcamp_session = _worker_with_counting_sessions(tmp_path, monkeypatch)
    driver = CountingDriver()
    worker._bandcamp_browser_driver = driver
    _, finished = _capture_signals(worker)
    rows_processed = []

    def complete_normally():
        for row_index in range(3):
            rows_processed.append(row_index)
        worker.finished.emit(worker.output_csv_path)

    monkeypatch.setattr(worker, "_run_impl", complete_normally)

    worker.run()

    assert rows_processed == [0, 1, 2]
    assert finished == [worker.output_csv_path]
    assert session.close_calls == 1
    assert bandcamp_session.close_calls == 1
    assert driver.quit_calls == 1


def test_exception_run_closes_both_sessions_and_browser_once(tmp_path, monkeypatch):
    worker, session, bandcamp_session = _worker_with_counting_sessions(tmp_path, monkeypatch)
    driver = CountingDriver()
    worker._bandcamp_browser_driver = driver
    logs, finished = _capture_signals(worker)

    def fail_after_session_creation():
        raise RuntimeError("deterministic enrichment failure")

    monkeypatch.setattr(worker, "_run_impl", fail_after_session_creation)

    worker.run()

    assert finished == [""]
    assert logs == ["[Enricher] Error: deterministic enrichment failure"]
    assert session.close_calls == 1
    assert bandcamp_session.close_calls == 1
    assert driver.quit_calls == 1


def test_worker_cleanup_is_idempotent(tmp_path, monkeypatch):
    worker, session, bandcamp_session = _worker_with_counting_sessions(tmp_path, monkeypatch)
    driver = CountingDriver()
    worker._bandcamp_browser_driver = driver

    worker._cleanup_owned_resources()
    worker._cleanup_owned_resources()

    assert session.close_calls == 1
    assert bandcamp_session.close_calls == 1
    assert driver.quit_calls == 1


def test_headless_production_wrapper_closes_worker_resources(tmp_path, monkeypatch):
    worker, session, bandcamp_session = _worker_with_counting_sessions(tmp_path, monkeypatch)
    driver = CountingDriver()
    worker._bandcamp_browser_driver = driver
    monkeypatch.setattr(cde, "CrossDirectoryEnricherWorker", lambda **kwargs: worker)
    monkeypatch.setattr(worker, "_run_impl", lambda: None)

    result = cde.run_cross_directory_enrichment("seed.csv", "output.csv")

    assert result == "output.csv"
    assert session.close_calls == 1
    assert bandcamp_session.close_calls == 1
    assert driver.quit_calls == 1


def test_headless_production_wrapper_cleans_up_on_exception(tmp_path, monkeypatch):
    worker, session, bandcamp_session = _worker_with_counting_sessions(tmp_path, monkeypatch)
    driver = CountingDriver()
    worker._bandcamp_browser_driver = driver
    monkeypatch.setattr(cde, "CrossDirectoryEnricherWorker", lambda **kwargs: worker)

    def fail():
        raise RuntimeError("headless failure")

    monkeypatch.setattr(worker, "_run_impl", fail)

    try:
        cde.run_cross_directory_enrichment("seed.csv", "output.csv")
    except RuntimeError as exc:
        assert str(exc) == "headless failure"
    else:
        raise AssertionError("headless exception should retain its existing propagation semantics")

    assert session.close_calls == 1
    assert bandcamp_session.close_calls == 1
    assert driver.quit_calls == 1


def test_empty_seed_keeps_output_and_finished_behavior(tmp_path, monkeypatch):
    worker, session, bandcamp_session = _worker_with_counting_sessions(tmp_path, monkeypatch)
    seed_path = tmp_path / "seed.csv"
    pd.DataFrame(columns=["Artist Name"]).to_csv(seed_path, index=False)
    logs, finished = _capture_signals(worker)
    monkeypatch.setattr(cde, "_read_csv_flexible", lambda path: pd.DataFrame(columns=["Artist Name"]))
    monkeypatch.setattr(worker, "_create_fb_runtime_for_current_chunk", lambda: None)
    monkeypatch.setattr(cde, "_cleanup_enricher_facebook_driver", lambda: None)

    worker.run()

    output_path = tmp_path / "output.csv"
    assert finished == [output_path.as_posix()], logs
    assert output_path.exists()
    assert list(pd.read_csv(output_path).columns)
    assert session.close_calls == 1
    assert bandcamp_session.close_calls == 1
