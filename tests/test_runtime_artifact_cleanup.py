from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from runtime_artifacts import prune_matching_files, write_bounded_json_debug


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_runtime_artifacts.py"
SPEC = importlib.util.spec_from_file_location("cleanup_runtime_artifacts", SCRIPT_PATH)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_writer_retains_only_latest_debug_files(tmp_path: Path) -> None:
    debug_dir = tmp_path / "debug"
    for index in range(5):
        path = write_bounded_json_debug(
            {"index": index},
            prefix="fb_dom_gate_debug_",
            keep=3,
            directory=debug_dir,
        )
        timestamp = 1_000_000_000 + index
        os.utime(path, ns=(timestamp, timestamp))
        prune_matching_files(debug_dir, "fb_dom_gate_debug_*.json", keep=3)

    assert len(list(debug_dir.glob("fb_dom_gate_debug_*.json"))) == 3


def test_dry_run_plan_does_not_delete_and_excludes_operational_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    disposable = repo / "fb_dom_gate_debug_123.json"
    disposable.write_text("{}", encoding="utf-8")
    progress = repo / "data" / "runtime_progress" / "current_run_progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text("{}", encoding="utf-8")
    cursor = repo / "overnight_runs" / "unearthed_cursor.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text("{}", encoding="utf-8")

    plan = cleanup.build_cleanup_plan(repo)

    assert [artifact.path for artifact in plan] == [disposable]
    assert disposable.exists()
    assert progress.exists()
    assert cursor.exists()


def test_cli_defaults_to_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    disposable = repo / "nm_smoke.log"
    disposable.write_text("diagnostic", encoding="utf-8")
    monkeypatch.setattr(cleanup, "REPO_ROOT", repo)

    assert cleanup.main([]) == 0

    assert disposable.exists()
    assert "WOULD REMOVE [debug-log] nm_smoke.log" in capsys.readouterr().out


def test_apply_deletes_only_allowlisted_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    debug_dump = repo / "fb_dom_gate_debug_123.json"
    debug_dump.write_text("{}", encoding="utf-8")
    cache_file = repo / "package" / "__pycache__" / "module.cpython-314.pyc"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"cache")
    source = repo / "package" / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    macos_cache = repo / "data" / ".DS_Store"
    macos_cache.parent.mkdir(parents=True)
    macos_cache.write_bytes(b"metadata")

    plan = cleanup.build_cleanup_plan(repo)
    cleanup.apply_cleanup_plan(repo, plan)

    assert not debug_dump.exists()
    assert not cache_file.exists()
    assert not cache_file.parent.exists()
    assert not macos_cache.exists()
    assert source.exists()


def test_debug_retention_preserves_latest_and_active_run_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    debug_dir = repo / "runtime" / "debug" / "facebook"
    debug_dir.mkdir(parents=True)
    dumps = []
    for index in range(4):
        path = debug_dir / f"fb_dom_gate_debug_{index}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, ns=(index + 1, index + 1))
        dumps.append(path)
    active_state = repo / "overnight_runs" / "2099-01-01_000000" / "state.json"
    active_state.parent.mkdir(parents=True)
    active_state.write_text('{"status": "running"}', encoding="utf-8")

    plan = cleanup.build_cleanup_plan(repo, keep_debug=2)
    cleanup.apply_cleanup_plan(repo, plan)

    assert not dumps[0].exists()
    assert not dumps[1].exists()
    assert dumps[2].exists()
    assert dumps[3].exists()
    assert active_state.exists()


def test_browser_cache_requires_exact_fingerprint(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    suspicious = repo / "..."
    suspicious.mkdir()
    (suspicious / "valuable.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(cleanup.UnsafeArtifactError, match="unrecognised"):
        cleanup.build_cleanup_plan(repo)
    assert suspicious.exists()


def test_unexpected_path_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    unexpected = repo / "data" / "master_lead_machine_contacts.csv"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_text("Email\n", encoding="utf-8")

    with pytest.raises(cleanup.UnsafeArtifactError, match="protected"):
        cleanup.validate_artifact(repo, cleanup.Artifact(unexpected, "debug-log"))


def test_gitignore_covers_generated_classes_without_hiding_operational_state() -> None:
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    rules = {line.strip() for line in ignore.splitlines() if line.strip() and not line.startswith("#")}

    assert "__pycache__/" in ignore
    assert "*.py[cod]" in ignore
    assert "/runtime/debug/" in ignore
    assert "/.../" in ignore
    assert "fb_dom_gate_debug_*.json" in ignore
    assert "/fb_attach_*.png" in ignore
    assert "unearthed_artist_url_index" not in ignore
    assert "overnight_runs/" not in rules
    assert "data/runtime_progress/" not in rules
    assert "data/master_backup_*.csv" not in rules
    assert "data/master_lead_machine_contacts.csv" not in rules
    assert "!/overnight_runs/" in rules
    assert "!/overnight_runs/**" in rules
