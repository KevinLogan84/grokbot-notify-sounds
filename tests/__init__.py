"""Keep the suite from ever exec'ing afplay, say, or launchctl."""

import atexit
import os
import signal
import subprocess
import tempfile
import time
import unittest

os.environ["GROKBOT_SOUNDS_DRY_RUN"] = "1"

from tests import sandbox  # noqa: E402,F401  (fake bin dir and child-pid file; dry-run still does not exec)

_SUITE_TEMPS: list[str] = []
_BLOCKED_COMMANDS = {"afplay", "say", "launchctl"}


def _track_temp(path: str) -> str:
    if path and path not in _SUITE_TEMPS:
        _SUITE_TEMPS.append(path)
    return path


_orig_mkdtemp = tempfile.mkdtemp


def _mkdtemp(*args, **kwargs):
    return _track_temp(_orig_mkdtemp(*args, **kwargs))


tempfile.mkdtemp = _mkdtemp


def _pid_alive(pid: int) -> bool:
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stop_pid(pid: int) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 0.4
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _kill_pidfile(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        pid = int(text)
    except (OSError, ValueError):
        return
    _stop_pid(pid)


def _process_rows() -> list[tuple[int, str]]:
    proc = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            rows.append((int(pid_text), command))
        except ValueError:
            continue
    return rows


def stop_suite_workers() -> None:
    """Kill drain workers and notify workers started from this suite's temp dirs."""
    roots = [path for path in _SUITE_TEMPS if path]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            if "notify-drainer.pid" in filenames:
                _kill_pidfile(os.path.join(dirpath, "notify-drainer.pid"))
    if not roots:
        return
    for pid, command in _process_rows():
        if "grokbot_notify_sounds.py" not in command:
            continue
        if any(root in command for root in roots):
            _stop_pid(pid)
    sandbox.reap_children(grace=0.2)


_orig_temp_cleanup = tempfile.TemporaryDirectory.cleanup


def _temp_cleanup(self):
    stop_suite_workers()
    return _orig_temp_cleanup(self)


tempfile.TemporaryDirectory.cleanup = _temp_cleanup


def _guard_argv(argv) -> None:
    if not isinstance(argv, (list, tuple)) or not argv:
        return
    name = os.path.basename(str(argv[0]))
    if name in _BLOCKED_COMMANDS:
        raise AssertionError(f"refusing to exec {name}")


_orig_subprocess_run = subprocess.run
_orig_popen = subprocess.Popen


def _guarded_run(*args, **kwargs):
    argv = args[0] if args else kwargs.get("args")
    _guard_argv(argv)
    return _orig_subprocess_run(*args, **kwargs)


class _GuardedPopen(_orig_popen):
    def __init__(self, args, *more, **kwargs):
        _guard_argv(args)
        super().__init__(args, *more, **kwargs)


subprocess.run = _guarded_run
subprocess.Popen = _GuardedPopen


class DryRunTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["GROKBOT_SOUNDS_DRY_RUN"] = "1"
        super().setUp()

    def tearDown(self):
        stop_suite_workers()
        super().tearDown()


_orig_case_setup = unittest.TestCase.setUp
_orig_case_run = unittest.TestCase.run
_orig_case_cleanups = unittest.TestCase.doCleanups


def _case_setup(self):
    os.environ["GROKBOT_SOUNDS_DRY_RUN"] = "1"
    return _orig_case_setup(self)


def _case_run(self, result=None):
    os.environ["GROKBOT_SOUNDS_DRY_RUN"] = "1"
    return _orig_case_run(self, result)


def _case_cleanups(self):
    stop_suite_workers()
    return _orig_case_cleanups(self)


unittest.TestCase.setUp = _case_setup
unittest.TestCase.run = _case_run
unittest.TestCase.doCleanups = _case_cleanups
atexit.register(stop_suite_workers)
