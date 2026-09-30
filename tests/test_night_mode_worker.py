"""Regression tests for NightModeWorker subprocess streaming and control.

Requires pytest-qt because NightModeWorker is a QThread.
"""

import os
import sys
import tempfile
import time

import pytest

# Import the GUI module to access NightModeWorker.
spec = __import__("importlib.util").util.spec_from_file_location(
    "lm_gui", "Lead Machine (Final Update 5).py"
)
lm_gui = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(lm_gui)


@pytest.fixture
def qtapp(qtbot):
    """pytest-qt provides qtbot and ensures a QApplication exists."""
    return qtbot


# ---------------------------------------------------------------------------
# 7. Worker streams multiple child stdout lines via Qt signal
# ---------------------------------------------------------------------------


def test_worker_streams_multiple_stdout_lines(qtbot):
    script = (
        "import sys, time\n"
        "print('line_one', flush=True)\n"
        "print('line_two', flush=True)\n"
        "print('line_three', flush=True)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "child.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        cmd = [sys.executable, "-u", script_path]
        worker = lm_gui.NightModeWorker(cmd, workdir=tmp, secrets=[])
        lines = []
        worker.log_signal.connect(lines.append)

        with qtbot.waitSignal(worker.finished_signal, timeout=10000):
            worker.start()

        # Must see the launcher line plus the three child lines.
        assert any("line_one" in ln for ln in lines)
        assert any("line_two" in ln for ln in lines)
        assert any("line_three" in ln for ln in lines)


# ---------------------------------------------------------------------------
# 8. stderr is merged into the visible log stream
# ---------------------------------------------------------------------------


def test_worker_stderr_merged_to_log(qtbot):
    script = (
        "import sys\n"
        "print('stdout_msg', flush=True)\n"
        "print('stderr_msg', file=sys.stderr, flush=True)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "child.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        cmd = [sys.executable, "-u", script_path]
        worker = lm_gui.NightModeWorker(cmd, workdir=tmp, secrets=[])
        lines = []
        worker.log_signal.connect(lines.append)

        with qtbot.waitSignal(worker.finished_signal, timeout=10000):
            worker.start()

        assert any("stdout_msg" in ln for ln in lines)
        assert any("stderr_msg" in ln for ln in lines)


# ---------------------------------------------------------------------------
# 9. Secret masking still works
# ---------------------------------------------------------------------------


def test_worker_masks_secrets(qtbot):
    secret = "SuperSecretPassword123"
    script = (
        f"import sys\n"
        f"print('auth with {secret}', flush=True)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "child.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        cmd = [sys.executable, "-u", script_path]
        worker = lm_gui.NightModeWorker(cmd, workdir=tmp, secrets=[secret])
        lines = []
        worker.log_signal.connect(lines.append)

        with qtbot.waitSignal(worker.finished_signal, timeout=10000):
            worker.start()

        assert any("***" in ln for ln in lines)
        assert not any(secret in ln for ln in lines)


# ---------------------------------------------------------------------------
# 10. stop/terminate behaviour retained
# ---------------------------------------------------------------------------


def test_worker_stop_terminates_process(qtbot):
    # Child that sleeps for a long time and occasionally prints.
    script = (
        "import time, sys\n"
        "for i in range(60):\n"
        "    print(f'tick {i}', flush=True)\n"
        "    time.sleep(1)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "child.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        cmd = [sys.executable, "-u", script_path]
        worker = lm_gui.NightModeWorker(cmd, workdir=tmp, secrets=[])
        lines = []
        worker.log_signal.connect(lines.append)
        worker.start()

        # Wait briefly for the process to start.
        qtbot.waitUntil(lambda: worker._process is not None, timeout=5000)
        # Give the child a moment to enter its loop.
        time.sleep(0.5)

        worker.stop()

        with qtbot.waitSignal(worker.finished_signal, timeout=10000):
            pass

        # Process should have been terminated (non-zero or at least finished quickly).
        assert worker._process is not None
        # The key behavioural guarantee: stop() causes the worker to finish
        # without hanging for the full 60-second sleep.


# ---------------------------------------------------------------------------
# PYTHONUNBUFFERED is injected into child environment
# ---------------------------------------------------------------------------


def test_worker_injects_pythonunbuffered(qtbot):
    script = (
        "import os, sys\n"
        "print(f'PYTHONUNBUFFERED={os.environ.get(\"PYTHONUNBUFFERED\", \"MISSING\")}', flush=True)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "child.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        cmd = [sys.executable, "-u", script_path]
        worker = lm_gui.NightModeWorker(cmd, workdir=tmp, secrets=[])
        lines = []
        worker.log_signal.connect(lines.append)

        with qtbot.waitSignal(worker.finished_signal, timeout=10000):
            worker.start()

        assert any("PYTHONUNBUFFERED=1" in ln for ln in lines)
