from __future__ import annotations

import json
from pathlib import Path

from scripts.run_ue_nm_smoke import DEFAULT_CONFIG, build_smoke_payload, write_smoke_config


def test_build_smoke_payload_clamps_known_runner_limits_only() -> None:
    source = {
        "export_mode": "both",
        "facebook": {
            "max_rows_per_run": 100,
            "cooldown_seconds": 900,
        },
        "facebook_max_rows_per_run": 30,
        "jobs": [
            {
                "directory": "unearthed",
                "target_valid_leads": 25,
                "target_count": 18,
                "max_results": 17,
                "max_artists": 16,
                "mode": "",
            }
        ],
        "master_enrichment": {"enabled": True},
    }

    smoke, changes = build_smoke_payload(source, cap=15)

    assert source["facebook"]["max_rows_per_run"] == 100
    assert source["jobs"][0]["target_valid_leads"] == 25
    assert smoke["facebook"]["max_rows_per_run"] == 15
    assert smoke["facebook_max_rows_per_run"] == 15
    assert smoke["jobs"][0]["target_valid_leads"] == 15
    assert smoke["jobs"][0]["target_count"] == 15
    assert smoke["jobs"][0]["max_results"] == 15
    assert smoke["jobs"][0]["max_artists"] == 15
    assert smoke["master_enrichment"] == {"enabled": True}
    assert len(changes) == 6


def test_write_smoke_config_preserves_repo_source_config_and_caps_current_ue_config(tmp_path: Path) -> None:
    original_text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    smoke_config_path = tmp_path / "UE_NM_Run_Test500.smoke.json"

    changes = write_smoke_config(DEFAULT_CONFIG, smoke_config_path, cap=15)

    assert DEFAULT_CONFIG.read_text(encoding="utf-8") == original_text
    smoke_payload = json.loads(smoke_config_path.read_text(encoding="utf-8"))
    assert smoke_payload["jobs"][0]["target_valid_leads"] == 15
    assert smoke_payload["facebook"]["max_rows_per_run"] == 15
    assert not any("target_valid_leads" in change for change in changes)
    assert any(change.endswith("100 -> 15") for change in changes)
