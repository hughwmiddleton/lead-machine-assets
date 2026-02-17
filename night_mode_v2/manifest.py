import json
import os
import tempfile
from typing import Dict, Any


def config_hash(config: Dict[str, Any]) -> str:
    """Return deterministic SHA256 hash for a config dict."""
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return __import__("hashlib").sha256(serialized.encode("utf-8")).hexdigest()


def write_manifest(path: str, data: Dict[str, Any]) -> None:
    """Atomically write manifest data to disk."""
    dirpath = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(dirpath, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(dir=dirpath, prefix=".manifest_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, sort_keys=True)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def load_manifest(path: str) -> Dict[str, Any]:
    """Load manifest data if present; tolerate missing or malformed files."""
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON") from exc
