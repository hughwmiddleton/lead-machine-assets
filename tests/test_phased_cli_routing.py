import json
from pathlib import Path

import night_mode_runner


def _write_empty_config(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")


def test_cli_defaults_to_v1(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_empty_config(cfg_path)

    v1_calls = []
    v2_calls = []

    def fake_v1(**kwargs):
        v1_calls.append(kwargs)
        return {"result": "v1"}

    def fake_v2(**kwargs):
        v2_calls.append(kwargs)
        return {"result": "v2"}

    monkeypatch.setattr("night_mode_runner.run_night_mode", fake_v1)
    monkeypatch.setattr("night_mode_v2.phased_runner.run_phased_night_mode", fake_v2)

    night_mode_runner.main(["--config", cfg_path.as_posix()])

    assert len(v1_calls) == 1
    assert len(v2_calls) == 0


def test_cli_routes_to_phased_when_flag(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_empty_config(cfg_path)

    v1_calls = []
    v2_calls = []

    def fake_v1(**kwargs):
        v1_calls.append(kwargs)
        return {"result": "v1"}

    def fake_v2(**kwargs):
        v2_calls.append(kwargs)
        return {"result": "v2"}

    monkeypatch.setattr("night_mode_runner.run_night_mode", fake_v1)
    monkeypatch.setattr("night_mode_v2.phased_runner.run_phased_night_mode", fake_v2)

    night_mode_runner.main(["--config", cfg_path.as_posix(), "--phased"])

    assert len(v1_calls) == 0
    assert len(v2_calls) == 1
