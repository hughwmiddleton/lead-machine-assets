import json
from pathlib import Path

import night_mode_runner
from night_mode_v2 import phased_runner


def _write_empty_config(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")


def test_cli_defaults_to_v2(monkeypatch, tmp_path):
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

    assert len(v1_calls) == 0
    assert len(v2_calls) == 1


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


def test_legacy_false_config_is_normalized_to_phased():
    config = night_mode_runner.normalize_night_mode_config(
        {"phased": False, "use_phased_runner": False, "jobs": []}
    )

    assert config["use_phased_runner"] is True
    assert config["phased"] is True


def test_phased_runner_load_overrides_legacy_false_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"use_phased_runner": False, "phased": False}), encoding="utf-8")

    config = phased_runner._load_config(cfg_path.as_posix())

    assert config["use_phased_runner"] is True
    assert config["phased"] is True


def test_cli_ignores_legacy_false_and_never_invokes_v1(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"use_phased_runner": False, "phased": False}), encoding="utf-8")

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

    assert v1_calls == []
    assert len(v2_calls) == 1
