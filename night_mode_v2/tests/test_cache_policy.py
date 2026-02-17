from night_mode_v2.cache_policy import should_skip_phase


def test_manifest_empty():
    assert should_skip_phase({}, "seed", "h1", True, True) is False


def test_config_mismatch():
    manifest = {"config_hash": "old", "phases": {"seed": {"status": "completed"}}}
    assert should_skip_phase(manifest, "seed", "new", True, True) is False


def test_missing_outputs():
    manifest = {"config_hash": "h", "phases": {"seed": {"status": "completed"}}}
    assert should_skip_phase(manifest, "seed", "h", False, True) is False


def test_schema_invalid():
    manifest = {"config_hash": "h", "phases": {"seed": {"status": "completed"}}}
    assert should_skip_phase(manifest, "seed", "h", True, False) is False


def test_phase_not_completed():
    manifest = {"config_hash": "h", "phases": {"seed": {"status": "pending"}}}
    assert should_skip_phase(manifest, "seed", "h", True, True) is False


def test_should_skip_true():
    manifest = {"config_hash": "h", "phases": {"seed": {"status": "completed"}}}
    assert should_skip_phase(manifest, "seed", "h", True, True) is True
