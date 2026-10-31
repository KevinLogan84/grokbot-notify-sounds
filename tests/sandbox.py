"""Suite-wide sandbox: tests never run the real afplay or say, and never leak workers.

Importing this module (tests/__init__.py does) sets two environment variables that every
subprocess, and every detached --notify-worker or --drain-notify they spawn, inherits:

* GROKBOT_NOTIFY_BIN_DIR points afplay and say at silent fakes.
* GROKBOT_NOTIFY_CHILD_PIDS collects the pid of each detached worker so reap_children()
  can wait for, then stop, them before a test's temp directory is removed.
"""

import atexit
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="gns-sandbox-"))
FAKE_BIN = _ROOT / "bin"
CHILD_PIDS = _ROOT / "children"

_FAKE = """#!/bin/sh
# Silent stand-in for {name}. Logs args to ${var} when a test sets it.
if [ -n "${var}" ]; then printf '%s\\n' "{fmt}" >> "${var}"; fi
exit 0
"""


def _install_fakes() -> None:
    FAKE_BIN.mkdir(parents=True, exist_ok=True)
    for name, var, fmt in (("afplay", "PLAY_LOG", "$3"), ("say", "SAY_LOG", "$*")):
        path = FAKE_BIN / name
        path.write_text(_FAKE.format(name=name, var=var, fmt=fmt), encoding="utf-8")
        path.chmod(0o755)
    CHILD_PIDS.touch()


def _alive(pid: int) -> bool:
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_children(grace: float = 3.0) -> None:
    """Wait for every recorded detached worker to exit; stop stragglers. Idempotent."""
    try:
        pids = {int(line) for line in CHILD_PIDS.read_text(encoding="utf-8").split() if line.strip()}
    except (OSError, ValueError):
        pids = set()
    deadline = time.monotonic() + grace
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            for pid in pids:
                if _alive(pid):
                    try:
                        os.killpg(pid, sig)  # workers start their own session, so pid == pgid
                    except OSError:
                        try:
                            os.kill(pid, sig)
                        except OSError:
                            pass
            deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.02)
        pids = {pid for pid in pids if _alive(pid)}
        if not pids:
            break
    CHILD_PIDS.write_text("", encoding="utf-8")


def _cleanup() -> None:
    reap_children(grace=0.5)
    shutil.rmtree(_ROOT, ignore_errors=True)


_install_fakes()
os.environ["GROKBOT_NOTIFY_BIN_DIR"] = str(FAKE_BIN)
os.environ["GROKBOT_NOTIFY_CHILD_PIDS"] = str(CHILD_PIDS)
atexit.register(_cleanup)
