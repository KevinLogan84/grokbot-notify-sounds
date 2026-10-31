"""Dry-run playback never execs afplay, say, or launchctl."""

import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import grokbot_notify_sounds as gns

ROOT = Path(__file__).resolve().parents[1]


def _command_pids(token: str) -> list[int]:
    proc = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    found = []
    for line in proc.stdout.splitlines():
        if token not in line:
            continue
        pid_text = line.strip().split(" ", 1)[0]
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != os.getpid():
            found.append(pid)
    return found


class DryRunTests(unittest.TestCase):
    def tearDown(self):
        from tests import stop_suite_workers

        stop_suite_workers()
        os.environ["GROKBOT_SOUNDS_DRY_RUN"] = "1"
        os.environ.pop("GROKBOT_SOUNDS_PLAYER", None)

    def test_dry_run_records_and_does_not_exec(self):
        commands = [
            ["afplay", "-v", "0.4", "/System/Library/Sounds/Glass.aiff"],
            ["say", "-v", "Samantha", "hello"],
        ]

        def boom(*_args, **_kwargs):
            raise AssertionError("playback exec")

        original = gns.subprocess.run
        gns.subprocess.run = boom
        before = len(gns.DRY_RUN_PLAYS)
        try:
            gns.play_commands(commands)
        finally:
            gns.subprocess.run = original
        recorded = gns.DRY_RUN_PLAYS[before:]
        self.assertEqual(recorded, [{"sound": "Glass", "volume": "0.4", "say": "hello"}])

    def test_player_fake_also_blocks_exec(self):
        os.environ.pop("GROKBOT_SOUNDS_DRY_RUN", None)
        os.environ["GROKBOT_SOUNDS_PLAYER"] = "fake"

        def boom(*_args, **_kwargs):
            raise AssertionError("playback exec")

        original = gns.subprocess.run
        gns.subprocess.run = boom
        before = len(gns.DRY_RUN_PLAYS)
        try:
            gns.play_commands([["afplay", "-v", "1", "/System/Library/Sounds/Tink.aiff"]])
        finally:
            gns.subprocess.run = original
        self.assertEqual(gns.DRY_RUN_PLAYS[before:], [{"sound": "Tink", "volume": "1", "say": "none"}])

    def test_dry_run_does_not_call_launchctl(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("launchctl")

        original = gns.subprocess.run
        gns.subprocess.run = boom
        try:
            self.assertEqual(gns.probe_agent(which=lambda _name: "/usr/bin/launchctl"), "unknown")
        finally:
            gns.subprocess.run = original

    def test_forward_keeps_the_dry_run_flag(self):
        env = gns._forward_sound_env({"PATH": "/usr/bin", "GROKBOT_SOUNDS_DRY_RUN": ""})
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["GROKBOT_SOUNDS_DRY_RUN"], "1")

    def test_temp_config_without_dry_run_refuses_to_play(self):
        os.environ.pop("GROKBOT_SOUNDS_DRY_RUN", None)
        os.environ.pop("GROKBOT_SOUNDS_PLAYER", None)
        messages = []

        class Grab(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Grab()
        gns.LOG.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "config.json"
                state = root / "state.json"
                config.write_text('{"default": {"sound": "Tink", "volume": 40}}\n', encoding="utf-8")
                gns.append_notify_item(
                    state,
                    {
                        "commands": [["afplay", "-v", "0.4", "/System/Library/Sounds/Tink.aiff"]],
                        "rule_name": "default",
                        "sound": "Tink",
                        "say": False,
                        "bot": "Red",
                        "source": "notify",
                        "enqueued_at": time.time(),
                    },
                )

                def boom(_commands):
                    raise AssertionError("played")

                gns.drain_notify_queue(config, state, player=boom)
                self.assertTrue(gns._temp_playback_refused(config))
                home_config = Path.home() / ".config" / "grokbot-sounds" / "config.json"
                self.assertFalse(gns._temp_playback_refused(home_config))
        finally:
            gns.LOG.removeHandler(handler)
        self.assertTrue(any("refusing to play" in message for message in messages))

    def test_notify_drain_is_gone_afterward(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.json"
            state = home / "state" / "state.json"
            state.parent.mkdir()
            config.write_text(
                '{"burst_window_seconds": 0, "cooldown_seconds": 0, '
                '"default": {"sound": "Tink", "volume": 40, "say": false}, '
                '"rules": [{"name": "Ping", "contains": "hello", "sound": "Glass", "volume": 100, "say": false}]}\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["GROKBOT_SOUNDS_DRY_RUN"] = "1"
            log_path = home / "Library" / "Logs" / "grokbot-notify-sounds.log"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "grokbot_notify_sounds.py"),
                    "--notify",
                    "--bot",
                    "Red",
                    "--body",
                    "hello",
                    "--config",
                    str(config),
                    "--state",
                    str(state),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            token = str(state)
            deadline = time.time() + 5
            text = ""
            while time.time() < deadline:
                text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                if "dry-run sound=Glass" in text and not _command_pids(token):
                    break
                time.sleep(0.05)
            self.assertIn("dry-run sound=Glass", text)
            self.assertEqual(_command_pids(token), [])
            self.assertEqual(_command_pids("--drain-notify"), [])
            self.assertFalse((state.parent / "notify-drainer.pid").exists())
