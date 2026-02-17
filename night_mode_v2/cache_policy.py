from typing import Dict


def should_skip_phase(
    manifest: Dict,
    phase: str,
    current_config_hash: str,
    required_outputs_exist: bool,
    schema_valid: bool,
) -> bool:
    """Determine if a phase can be skipped based on manifest state."""
    if not manifest:
        return False

    if manifest.get("config_hash") != current_config_hash:
        return False

    if not required_outputs_exist:
        return False

    if not schema_valid:
        return False

    phase_info = manifest.get("phases", {}).get(phase) or {}
    if phase_info.get("status") != "completed":
        return False

    return True
