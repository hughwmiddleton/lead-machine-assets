from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_main.sh"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_smoke_layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "lead-machine-assets"
    (repo_root / "scripts").mkdir(parents=True)
    (repo_root / "tests").mkdir()
    (repo_root / "scripts" / "smoke_main.sh").symlink_to(SMOKE_SCRIPT)
    output_root = tmp_path / "output_tests"
    output_root.mkdir()
    return repo_root, output_root


def _install_python3_wrapper(bin_dir: Path) -> None:
    wrapper = bin_dir / "python3"
    wrapper.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "from __future__ import annotations",
                "",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                f'REAL_PYTHON = {sys.executable!r}',
                "",
                "args = sys.argv[1:]",
                "if args and args[0] == '-':",
                "    os.execv(REAL_PYTHON, [REAL_PYTHON, *args])",
                "",
                "if args and args[0] == 'night_mode_runner.py':",
                "    run_root = ''",
                "    idx = 1",
                "    while idx < len(args):",
                "        if args[idx] == '--run-root' and idx + 1 < len(args):",
                "            run_root = args[idx + 1]",
                "            idx += 2",
                "            continue",
                "        idx += 1",
                "    if not run_root:",
                "        print('missing --run-root', file=sys.stderr)",
                "        raise SystemExit(1)",
                "    run_root_path = Path(run_root)",
                "    (run_root_path / 'job_1').mkdir(parents=True, exist_ok=True)",
                "    (run_root_path / 'master_raw.csv').write_text('Artist Name\\nExample Artist\\n', encoding='utf-8')",
                "    (run_root_path / 'job_1' / 'raw.csv').write_text('Artist Name\\nExample Artist\\n', encoding='utf-8')",
                "    raise SystemExit(0)",
                "",
                "os.execv(REAL_PYTHON, [REAL_PYTHON, *args])",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)


def _run_smoke(
    repo_root: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = repo_root.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    _install_python3_wrapper(bin_dir)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.setdefault("SMOKE_TRIM_CONFIG", "0")
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(repo_root / "scripts" / "smoke_main.sh")],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_smoke_main_honors_explicit_smoke_config(tmp_path: Path) -> None:
    repo_root, output_root = _build_smoke_layout(tmp_path)
    requested = repo_root / "tests" / "UK_jobs_test.json"
    discovered = output_root / "US_mexico-overnight-test2" / "US_Mexico_Overnight-test.json"
    _write_json(requested, {"jobs": []})
    _write_json(discovered, {"jobs": []})

    result = _run_smoke(
        repo_root,
        extra_env={
            "SMOKE_CONFIG": str(requested),
            "SMOKE_TRIM_CONFIG": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"[INFO] Using config: {requested}" in result.stdout
    assert f"[INFO] Using config: {discovered}" not in result.stdout


def test_smoke_main_auto_discovers_config_when_env_unset(tmp_path: Path) -> None:
    repo_root, output_root = _build_smoke_layout(tmp_path)
    discovered = output_root / "US_mexico-overnight-test2" / "US_Mexico_Overnight-test.json"
    _write_json(discovered, {"jobs": []})

    result = _run_smoke(repo_root, extra_env={"SMOKE_TRIM_CONFIG": "0"})

    assert result.returncode == 0, result.stderr
    assert f"[INFO] Using config: {discovered}" in result.stdout


def test_smoke_main_fails_clearly_for_missing_explicit_smoke_config(tmp_path: Path) -> None:
    repo_root, _ = _build_smoke_layout(tmp_path)
    missing = repo_root / "tests" / "missing.json"

    result = _run_smoke(
        repo_root,
        extra_env={
            "SMOKE_CONFIG": str(missing),
            "SMOKE_TRIM_CONFIG": "0",
        },
    )

    assert result.returncode == 2
    assert f"[FAIL] SMOKE_CONFIG does not exist: {missing}" in result.stderr


def test_smoke_main_preserves_trim_flow_for_explicit_config(tmp_path: Path) -> None:
    repo_root, _ = _build_smoke_layout(tmp_path)
    config_path = repo_root / "tests" / "UK_jobs_test.json"
    _write_json(config_path, {"artists": [{"name": "A"}, {"name": "B"}]})

    result = _run_smoke(
        repo_root,
        extra_env={
            "SMOKE_CONFIG": str(config_path),
            "SMOKE_TRIM_CONFIG": "1",
            "SMOKE_SEED_CAP": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"[INFO] Using config: {config_path}" in result.stdout
    match = re.search(r"\[INFO\] Trimming succeeded; using trimmed config: (.+)", result.stdout)
    assert match, result.stdout
    trimmed_config = Path(match.group(1).strip())
    assert trimmed_config.exists()
    assert trimmed_config.name == "smoke_config_trimmed.json"
    trimmed_payload = json.loads(trimmed_config.read_text(encoding="utf-8"))
    assert trimmed_payload["artists"] == [{"name": "A"}]
