from types import SimpleNamespace

import pandas as pd

import cross_directory_enricher as cde


def test_enrichment_mode_defaults_to_source_phased_when_environment_is_unset(monkeypatch):
    monkeypatch.delenv("ENRICHMENT_MODE", raising=False)

    assert cde.resolve_enrichment_mode() == ("source_phased", "default")


def test_enrichment_mode_respects_explicit_source_phased_override(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_MODE", "source_phased")

    assert cde.resolve_enrichment_mode() == ("source_phased", "environment")


def test_enrichment_mode_respects_explicit_row_linear_override(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_MODE", "row_linear")

    assert cde.resolve_enrichment_mode() == ("row_linear", "environment")


def test_direct_python_dispatch_uses_default_source_phased_routing(monkeypatch):
    monkeypatch.delenv("ENRICHMENT_MODE", raising=False)
    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *args, **kwargs: None)
    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.night_mode = False
    worker._resume_row_index = 0
    worker.log_message = SimpleNamespace(emit=lambda _message: None)
    calls = []
    worker._run_source_phased = lambda *args, **kwargs: calls.append("source_phased")
    worker._run_row_linear = lambda *args, **kwargs: calls.append("row_linear")

    selected_mode, _source = cde.resolve_enrichment_mode()
    worker._run_with_night_runtime_chunks(
        pd.DataFrame([{"Artist Name": "Direct Launch"}]),
        directory_indexes={},
        priority=[],
        fb_driver=None,
        total=1,
        enrichment_mode=selected_mode,
    )

    assert calls == ["source_phased"]


def test_explicit_row_linear_override_uses_existing_row_linear_routing(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_MODE", "row_linear")
    monkeypatch.setattr(cde.CrossDirectoryEnricherWorker, "__init__", lambda self, *args, **kwargs: None)
    worker = cde.CrossDirectoryEnricherWorker(None, None)
    worker.night_mode = False
    worker._resume_row_index = 0
    worker.log_message = SimpleNamespace(emit=lambda _message: None)
    calls = []
    worker._run_source_phased = lambda *args, **kwargs: calls.append("source_phased")
    worker._run_row_linear = lambda *args, **kwargs: calls.append("row_linear")

    selected_mode, _source = cde.resolve_enrichment_mode()
    worker._run_with_night_runtime_chunks(
        pd.DataFrame([{"Artist Name": "Explicit Override"}]),
        directory_indexes={},
        priority=[],
        fb_driver=None,
        total=1,
        enrichment_mode=selected_mode,
    )

    assert calls == ["row_linear"]
