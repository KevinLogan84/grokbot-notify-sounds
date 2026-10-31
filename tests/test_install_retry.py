"""install.sh waits out bootout, then retries bootstrap."""

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$HOME/launchctl.log"
cmd="$1"
mode="${LC_MODE:-ok}"
prints=$(grep -c '^print ' "$HOME/launchctl.log" || true)
boots=$(grep -c '^bootstrap ' "$HOME/launchctl.log" || true)
case "$cmd" in
  bootout)
    exit 0
    ;;
  print)
    if [ "$mode" = "slow-clear" ] && [ "$prints" -le 2 ]; then
      exit 0
    fi
    exit 1
    ;;
  bootstrap)
    if [ "$mode" = "fail" ] || { [ "$mode" = "slow-clear" ] && [ "$boots" -le 1 ]; }; then
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    exit 0
    ;;
esac
exit 0
"""


class InstallRetryTests(unittest.TestCase):
    def _run(self, mode):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        stub_dir = root / "stub"
        home.mkdir()
        stub_dir.mkdir()
        launch = stub_dir / "launchctl"
        launch.write_text(STUB, encoding="utf-8")
        launch.chmod(launch.stat().st_mode | stat.S_IEXEC)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        env["LC_MODE"] = mode
        began = time.monotonic()
        proc = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--mode", "watch"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - began
        log = (home / "launchctl.log").read_text(encoding="utf-8") if (home / "launchctl.log").exists() else ""
        return proc, elapsed, log

    def test_bootstrap_retries_after_the_job_clears(self):
        proc, elapsed, log = self._run("slow-clear")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 4.0, log)
        self.assertEqual(log.count("\nprint "), 3 if log.endswith("\n") else log.count("print "), log)
        self.assertEqual(sum(1 for line in log.splitlines() if line.startswith("print ")), 3, log)
        self.assertEqual(sum(1 for line in log.splitlines() if line.startswith("bootstrap ")), 2, log)
        self.assertIn("LaunchAgent Python:", proc.stdout)
        self.assertIn("Full Disk Access", proc.stdout)
        self.assertNotIn("could not be loaded", proc.stderr)
        self.assertNotIn("could not be loaded", proc.stdout)

    def test_bootstrap_failure_exits_with_the_launchctl_error(self):
        proc, elapsed, log = self._run("fail")
        self.assertNotEqual(proc.returncode, 0)
        self.assertLess(elapsed, 4.5, log)
        self.assertEqual(sum(1 for line in log.splitlines() if line.startswith("print ")), 1, log)
        self.assertEqual(sum(1 for line in log.splitlines() if line.startswith("bootstrap ")), 3, log)
        self.assertIn("The agent could not be loaded:", proc.stderr)
        self.assertIn("Bootstrap failed: 5: Input/output error", proc.stderr)
        self.assertNotIn("LaunchAgent Python:", proc.stdout)
        self.assertNotIn("Full Disk Access", proc.stdout)


if __name__ == "__main__":
    unittest.main()
