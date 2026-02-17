import json

from night_mode_v2.manifest import config_hash, load_manifest, write_manifest


def test_write_and_load_roundtrip(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    data = {"config_hash": "abc", "phases": {"seed": {"status": "completed"}}}

    write_manifest(manifest_path.as_posix(), data)
    loaded = load_manifest(manifest_path.as_posix())

    assert loaded == data


def test_config_hash_deterministic():
    cfg1 = {"a": 1, "b": {"c": 2}}
    cfg2 = {"b": {"c": 2}, "a": 1}

    assert config_hash(cfg1) == config_hash(cfg2)
