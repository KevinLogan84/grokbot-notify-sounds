"""Queue, voices, config edits, and --notify."""

import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

import grokbot_notify_sounds as gns
from tests.helpers import BUNDLE, config_with, insert_app, insert_record, make_db, read_state

ROOT = Path(__file__).resolve().parents[1]
BOT_LINE = (
    'When you send me a message, also run on my Mac: '
    '~/.local/bin/grokbot-notify-sounds --notify --bot "<your bot name>" --title "<short title>"'
)
VOICES = """
Zoe (Premium)       en_GB    # Hi, I'm Zoe.
Evan (Enhanced)     en_US    # Hi, I'm Evan.
Allison (Premium)   en_US    # Hi, I'm Allison.
Serena (Enhanced)   en_GB    # Hi.
Samantha            en_US    # Hello.
"""


def glass_item(name, at, say=False):
    commands = gns.build_commands("Glass", 100, 100, say, "Samantha" if say else None, "T", "", "body")
    return gns.PlayItem(commands, name, at, say)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "state" / "playback.lock"
        self.state = Path(self.tmp.name) / "state" / "state.json"
        self.lock.parent.mkdir()
        gns.set_voice_listing("Samantha             en_US    # Hello\n")
        self.addCleanup(gns.clear_voice_listing)

    def _queue(self, config, player, book=None, now=None):
        return gns.PlaybackQueue(
            lambda: config,
            player,
            self.lock,
            book=book,
            clock=lambda: 10.0,
            now=now or (lambda: datetime(2026, 10, 31, 12, 0, 0)),
        )

    def test_serialization_and_burst_thresholds(self):
        order = []
        busy = {"on": False}

        def player(commands):
            self.assertFalse(busy["on"])
            busy["on"] = True
            try:
                for command in commands:
                    order.append(command[0])
                    time.sleep(0.01)
            finally:
                busy["on"] = False

        config = config_with(
            [{"name": "A", "contains": "a", "sound": "Glass", "say": True, "voice": "Samantha"}],
            burst_threshold=3,
            burst_window_seconds=5,
            queue_max=20,
        )
        queue = self._queue(config, player)
        queue.enqueue(glass_item("A", 10.0, say=True))
        queue.enqueue(glass_item("B", 10.1, say=True))
        queue.start()
        queue.drain(timeout=2)
        queue.close()
        self.assertEqual(order, ["afplay", "say", "afplay", "say"])

        paths = []

        def collect(commands):
            paths.append(Path(commands[0][3]).name)

        plain = config_with([], burst_threshold=3, burst_window_seconds=5, queue_max=20)
        under = self._queue(plain, collect)
        for index in range(3):
            under.enqueue(glass_item(f"n{index}", 10.0))
        under.start()
        under.drain(timeout=2)
        under.close()
        self.assertEqual(paths, ["Glass.aiff", "Glass.aiff", "Glass.aiff"])

        paths.clear()
        over = self._queue(plain, collect)
        for index in range(4):
            over.enqueue(glass_item(f"n{index}", 10.0))
        over.start()
        over.drain(timeout=2)
        over.close()
        self.assertEqual(paths, ["Glass.aiff", "Glass.aiff", "Glass.aiff"])

    def test_cooldown_and_queue_cap(self):
        played = []

        def player(commands):
            played.append(commands[0][3])

        config = config_with(
            [{"name": "Same", "contains": "a", "sound": "Glass"}],
            cooldown_seconds=30,
            burst_threshold=10,
            queue_max=20,
        )
        book = gns.CooldownBook(self.state)
        now = {"t": datetime(2026, 10, 31, 12, 0, 0)}
        queue = self._queue(config, player, book=book, now=lambda: now["t"])
        queue.enqueue(glass_item("Same", 1.0))
        queue.enqueue(glass_item("Same", 1.1))
        queue.enqueue(glass_item("Other", 1.2))
        queue.start()
        queue.drain(timeout=2)
        queue.close()
        self.assertEqual([Path(path).name for path in played], ["Glass.aiff", "Glass.aiff"])
        self.assertIn("Same", book.snapshot())

        played.clear()
        later = gns.CooldownBook(self.state)
        again = self._queue(config, player, book=later, now=lambda: now["t"])
        again.enqueue(glass_item("Same", 2.0))
        again.enqueue(glass_item("Other", 2.1))
        again.enqueue(glass_item("Fresh", 2.2))
        again.start()
        again.drain(timeout=2)
        again.close()
        self.assertEqual([Path(path).name for path in played], ["Glass.aiff"])
        self.assertIn("Same", later.snapshot())
        self.assertIn("Other", later.snapshot())

        played.clear()
        capped = config_with([], burst_threshold=3, burst_window_seconds=5, queue_max=2)
        queue = self._queue(capped, player)
        for index in range(5):
            queue.enqueue(glass_item(f"c{index}", 10.0))
        queue.start()
        queue.drain(timeout=2)
        queue.close()
        self.assertEqual([Path(path).name for path in played], ["Glass.aiff", "Glass.aiff", "Tink.aiff"])

    def test_lock_blocks_a_second_player(self):
        order = []
        release = threading.Event()
        started = threading.Event()

        def player(commands):
            order.append("start")
            if len(order) == 1:
                started.set()
                self.assertTrue(release.wait(2))
            order.append("end")

        first = threading.Thread(target=lambda: gns.play_with_lock(self.lock, [["a"]], player))
        second = threading.Thread(target=lambda: gns.play_with_lock(self.lock, [["b"]], player))
        first.start()
        self.assertTrue(started.wait(2))
        second.start()
        time.sleep(0.05)
        self.assertEqual(order, ["start"])
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(order, ["start", "end", "start", "end"])


class VoiceAndSoundTests(unittest.TestCase):
    def tearDown(self):
        gns.clear_voice_listing()
        gns.set_sounds_dir(None)

    def test_voice_ranking_and_cache(self):
        gns.set_voice_listing(VOICES)
        self.assertEqual(gns.pick_natural_voice(), "Allison (Premium)")
        gns._VOICE_LISTING_OVERRIDE = "Evan (Enhanced)     en_US    # Hi\n"
        self.assertEqual(gns.pick_natural_voice(), "Allison (Premium)")
        gns.clear_voice_cache()
        self.assertEqual(gns.pick_natural_voice(), "Evan (Enhanced)")
        gns.set_voice_listing("Zoe (Premium)       en_GB    # Hi\nSamantha            en_US    # Hello\n")
        self.assertEqual(gns.pick_natural_voice(), "Zoe (Premium)")
        gns.set_voice_listing("Samantha            en_US    # Hello\n")
        self.assertIsNone(gns.pick_natural_voice())
        report = gns.format_voice_report(gns.parse_voices("Samantha            en_US    # Hello\n"))
        self.assertIn("No Premium or Enhanced voice is installed.", report)
        self.assertIn("System Settings > Accessibility > Spoken Content > System voice > Manage Voices.", report)

    def test_omitted_say_does_not_query_voices(self):
        def boom():
            raise AssertionError("voices were queried")

        original = gns.read_voice_listing
        gns.read_voice_listing = boom
        gns.clear_voice_cache()
        try:
            config = config_with([{"name": "A", "contains": "hello", "sound": "Glass"}])
            self.assertFalse(config["default"]["say"])
            self.assertFalse(config["rules"][0]["say"])
            shipped = gns.parse_config_text((ROOT / "config.json").read_text(encoding="utf-8"))
            self.assertFalse(shipped["default"]["say"])
            outcome = gns.plan_notification("t", "", "hello", config, now=datetime(2026, 10, 31, 12, 0), cooldowns={})
            self.assertEqual(len(outcome.commands), 1)
            self.assertEqual(outcome.commands[0][0], "afplay")
            silent = gns.build_commands("Glass", 40, 100, False, None, "", "", "hi", say_text="hello", bot="A")
            self.assertEqual([command[0] for command in silent], ["afplay"])
        finally:
            gns.read_voice_listing = original
            gns.clear_voice_cache()

    def test_say_text_and_false_say(self):
        gns.set_voice_listing("Samantha            en_US    # Hello\n")
        spoken = gns.render_say_text("Hi {bot} {title} {body} {other}", "Al", "Title", "")
        self.assertEqual(spoken, "Hi Al Title  {other}")
        long = gns.render_say_text("x" * 400, "", "", "")
        self.assertEqual(len(long), 300)
        self.assertTrue(long.endswith("…"))
        config = config_with(
            [{"name": "A", "contains": "hello", "sound": "Glass", "say": False, "say_text": "Hi {bot}"}]
        )
        outcome = gns.plan_notification("", "", "hello", config, now=datetime(2026, 10, 31, 12, 0), cooldowns={})
        self.assertEqual(len(outcome.commands), 1)
        talking = config_with(
            [
                {
                    "name": "A",
                    "bot": "Alfred",
                    "sound": "Glass",
                    "say": True,
                    "say_text": "Hi {bot} {title} {body}",
                    "voice": "Samantha",
                }
            ]
        )
        outcome = gns.plan_notification(
            "Title",
            "",
            "Body",
            talking,
            now=datetime(2026, 10, 31, 12, 0),
            cooldowns={},
            sender="Alfred",
        )
        self.assertEqual(outcome.commands[1], ["say", "-v", "Samantha", "Hi Alfred Title Body"])

    def test_play_rejects_missing_sounds_and_returns_player_status(self):
        sounds = Path(self.tmp.name) / "sounds" if False else Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(sounds, ignore_errors=True))
        sounds.mkdir(exist_ok=True)
        (sounds / "Tink.aiff").write_bytes(b"")
        gns.set_sounds_dir(sounds)
        missing = Path(self.tmp.name) / "missing.wav" if False else sounds.parent / "missing.wav"
        code, _out, err = self._main(["--play", "Nope", "--state", str(sounds.parent / "state.json")])
        self.assertEqual(code, 2)
        self.assertIn("unknown built-in sound", err)
        self.assertNotIn("usage:", err.lower())
        code, _out, err = self._main(
            ["--play", str(missing), "--state", str(sounds.parent / "state.json")]
        )
        self.assertEqual(code, 2)
        self.assertIn("missing sound file", err)

        def fail(commands):
            raise gns.PlaybackError(3)

        original = gns.play_commands
        gns.play_commands = fail
        try:
            (sounds / "Glass.aiff").write_bytes(b"")
            code, _out, err = self._main(["--play", "Glass", "--state", str(sounds.parent / "state.json")])
        finally:
            gns.play_commands = original
        self.assertEqual(code, 3)
        self.assertIn("playback failed", err)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(
            json.dumps({"enabled": True, "master_volume": 100, "default": {"sound": "Tink", "volume": 40}, "rules": []}),
            encoding="utf-8",
        )
        self.log = Path(self.tmp.name) / "log"

    def _main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(argv + ["--config", str(self.config)], log_path=self.log)
        return code, out.getvalue(), err.getvalue()


class EditSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.state = self.root / "state.json"
        self.log = self.root / "log"
        self.sounds = self.root / "sounds"
        self.sounds.mkdir()
        (self.sounds / "Glass.aiff").write_bytes(b"")
        (self.sounds / "Tink.aiff").write_bytes(b"")
        self.config.write_text("{}\n", encoding="utf-8")
        gns.set_sounds_dir(self.sounds)
        gns.set_voice_listing(VOICES)
        self.addCleanup(gns.clear_voice_listing)
        self.addCleanup(lambda: gns.set_sounds_dir(None))

    def _main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(
                argv + ["--config", str(self.config), "--state", str(self.state)],
                log_path=self.log,
            )
        return code, out.getvalue(), err.getvalue()

    def test_set_keys_success_and_rejection(self):
        expected = [
            ("default.voice=Samantha", "Samantha", ("default", "voice")),
            ("default.sound=Glass", "Glass", ("default", "sound")),
            ("default.volume=40", 40, ("default", "volume")),
            ("default.say=true", True, ("default", "say")),
            ("quiet_hours.start=22:00", "22:00", ("quiet_hours", "start")),
            ("quiet_hours.mode=soft", "soft", ("quiet_hours", "mode")),
        ]
        for pair, value, path in expected:
            before_parents = pair.startswith("quiet_hours")
            code, out, err = self._main(["--set", pair])
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), f"set {pair}")
            data = json.loads(self.config.read_text(encoding="utf-8"))
            self.assertEqual(data[path[0]][path[1]], value)
            if before_parents:
                self.assertIn("quiet_hours", data)
        before = self.config.read_bytes()
        code, out, err = self._main(["--set", "default.volume=150"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("usage:", err.lower())
        self.assertEqual(err.count("\n"), 1)
        self.assertEqual(self.config.read_bytes(), before)
        code, out, err = self._main(["--set-master-volume", "150"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("usage:", err.lower())
        self.assertIn("master_volume", err)

    def test_rules_sounds_voices_and_lines(self):
        code, out, err = self._main(
            ["--add-rule", json.dumps({"name": "Alfred answers", "bot": "Alfred", "sound": "Nope"})]
        )
        self.assertEqual(code, 2, err)
        self.assertIn("unknown built-in", err)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")).get("rules", []), [])
        code, out, err = self._main(
            ["--add-rule", json.dumps({"name": "Alfred answers", "bot": "Alfred", "sound": "Glass", "voice": "Nobody"})]
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown voice", err)
        code, out, err = self._main(
            ["--add-rule", json.dumps({"name": "Alfred answers", "bot": "Alfred", "sound": "Glass", "say_text": "Hi {bot}"})]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), 'added rule "Alfred answers"')
        code, out, err = self._main(
            ["--update-rule", "Alfred answers", json.dumps({"voice": "Missing Voice"})]
        )
        self.assertEqual(code, 2)
        self.assertNotIn("voice", json.loads(self.config.read_text(encoding="utf-8"))["rules"][0])
        code, out, err = self._main(["--enable-rule", "Alfred answers"])
        self.assertEqual(out.strip(), 'enabled rule "Alfred answers"')
        code, out, err = self._main(["--disable-rule", "Alfred answers"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), 'disabled rule "Alfred answers"')
        self.assertFalse(json.loads(self.config.read_text(encoding="utf-8"))["rules"][0]["enabled"])
        code, _out, err = self._main(["--enable-rule", "Missing"])
        self.assertEqual(code, 2)
        self.assertIn("No rule named", err)
        text = self.config.read_text(encoding="utf-8")
        errors, _note = gns.validation_report(text, self.sounds, voice_listing=VOICES)
        self.assertEqual(errors, [])
        bad = json.loads(text)
        bad["rules"][0]["sound"] = "Nope"
        errors, _note = gns.validation_report(json.dumps(bad), self.sounds, voice_listing=VOICES)
        self.assertTrue(any("unknown built-in" in error for error in errors))
        bad["rules"][0]["sound"] = "Glass"
        bad["rules"][0]["voice"] = "Nobody"
        errors, _note = gns.validation_report(json.dumps(bad), self.sounds, voice_listing=VOICES)
        self.assertTrue(any("unknown voice" in error for error in errors))

        out = io.StringIO()
        with redirect_stdout(out):
            code = gns.main(["--list-sounds", "--config", str(self.config)], log_path=self.log)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[:14], list(gns.BUILTIN_SOUND_LINES))
        self.assertEqual(lines[5], "Glass: bright glassy chime")
        custom = self.root / "bell.wav"
        custom.write_bytes(b"")
        (self.sounds / "Hero.aiff").write_bytes(b"")
        self.config.write_text(
            json.dumps(
                {
                    "default": {"sound": str(custom), "volume": 40},
                    "rules": [
                        {"name": "A", "contains": "a", "sound": str(custom)},
                        {"name": "B", "contains": "b", "sound": "Hero"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = gns.main(["--list-sounds", "--config", str(self.config)], log_path=self.log)
        self.assertEqual(code, 0)
        custom_lines = [line for line in out.getvalue().splitlines() if line.endswith(": custom file")]
        self.assertEqual(custom_lines, [f"{custom}: custom file"])

    def test_respect_cooldown_and_reload_log(self):
        self.config.write_text(
            json.dumps(
                {
                    "cooldown_seconds": 60,
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [{"name": "Needs approval", "contains": "approve", "sound": "Glass"}],
                }
            ),
            encoding="utf-8",
        )
        played = []
        original = gns.play_commands
        gns.play_commands = played.append
        try:
            code, out, err = self._main(["--test", "please approve"])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(played), 1)
            code, out, err = self._main(["--test", "please approve", "--respect-cooldown"])
        finally:
            gns.play_commands = original
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "suppressed by cooldown")
        self.assertEqual(len(played), 1)
        self.assertIn("Needs approval", json.loads(self.state.read_text(encoding="utf-8"))["cooldowns"])

        messages = []

        class Grab(gns.logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Grab()
        gns.LOG.addHandler(handler)
        try:
            path = self.root / "reload.json"
            path.write_text(
                json.dumps({"default": {"sound": "Tink", "volume": 40}, "rules": [{"name": "A", "contains": "a", "sound": "Glass"}]}),
                encoding="utf-8",
            )
            source = gns.ConfigSource(path)
            self.assertEqual(source.current()["rules"][0]["name"], "A")
            path.write_text("{", encoding="utf-8")
            os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
            self.assertEqual(source.current()["rules"][0]["name"], "A")
        finally:
            gns.LOG.removeHandler(handler)
        self.assertTrue(any(message == "config reloaded (1 rules)" for message in messages))
        self.assertTrue(any(message.startswith("config invalid, keeping last good:") for message in messages))


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.state = self.root / "state" / "state.json"
        self.state.parent.mkdir()
        self.log = self.root / "log"
        self.played = []
        self.spawned = []
        self.threads = []
        self.release = threading.Event()
        self.original_spawn = gns.spawn_drainer
        self.original_play = gns.play_commands
        self.original_connect = gns.connect_readonly
        self.original_clock = gns.clock_now
        gns.play_commands = self._play
        gns.spawn_drainer = self._spawn
        self.config.write_text(
            json.dumps(
                {
                    "burst_threshold": 3,
                    "burst_window_seconds": 1.5,
                    "queue_max": 20,
                    "default": {"sound": "Tink", "volume": 40, "say": False},
                    "rules": [{"name": "Alfred answers", "bot": "Alfred", "sound": "Glass", "volume": 80}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.release.set()
        for thread in self.threads:
            thread.join(4)
        gns.spawn_drainer = self.original_spawn
        gns.play_commands = self.original_play
        gns.connect_readonly = self.original_connect
        gns.clock_now = self.original_clock
        for handler in list(gns.LOG.handlers):
            if isinstance(handler, gns.logging.handlers.RotatingFileHandler):
                gns.LOG.removeHandler(handler)
                handler.close()

    def _play(self, commands):
        self.played.append(list(commands))

    def _spawn(self, config_path, state_path):
        self.spawned.append(1)
        thread = threading.Thread(
            target=gns.drain_notify_queue,
            args=(config_path, state_path),
            kwargs={"player": gns.play_commands},
            daemon=True,
        )
        thread.start()
        self.threads.append(thread)
        return os.getpid()

    def _main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(
                argv + ["--config", str(self.config), "--state", str(self.state)],
                log_path=self.log,
            )
        return code, out.getvalue(), err.getvalue()

    def test_notify_skips_the_database_and_honors_rules(self):
        def boom(path):
            raise AssertionError(f"opened {path}")

        gns.connect_readonly = boom
        code, out, err = self._main(
            ["--notify", "--bot", "Alfred", "--title", "status", "--body", "hello", "--db", str(self.root / "no-db")]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.threads[0].join(4)
        self.assertEqual(len(self.played), 1)
        self.assertTrue(self.played[0][0][3].endswith("/Glass.aiff"))
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("notify bot=Alfred", text)
        self.assertIn("rule=Alfred answers", text)
        self.assertIn("sound=Glass", text)
        self.assertNotIn("hello", text)

        gns.clock_now = lambda: datetime(2026, 10, 31, 23, 0, 0)
        self.config.write_text(
            json.dumps(
                {
                    "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00", "mode": "mute"},
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [{"name": "Alfred answers", "bot": "Alfred", "sound": "Glass"}],
                }
            ),
            encoding="utf-8",
        )
        before = len(self.played)
        spawned = len(self.spawned)
        code, _out, err = self._main(["--notify", "--bot", "Alfred", "--title", "night"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.played), before)
        self.assertEqual(len(self.spawned), spawned)
        self.assertIn("skip-mute", self.log.read_text(encoding="utf-8"))

    def test_notify_returns_before_a_slow_player_and_collapses(self):
        self.config.write_text(
            json.dumps(
                {
                    "burst_threshold": 3,
                    "burst_window_seconds": 1.5,
                    "queue_max": 20,
                    "default": {"sound": "Tink", "volume": 40, "say": False},
                    "rules": [
                        {"name": name, "bot": name, "sound": "Glass", "volume": 80}
                        for name in ("One", "Two", "Three", "Four")
                    ],
                }
            ),
            encoding="utf-8",
        )
        started = threading.Event()

        def slow(commands):
            self.played.append(list(commands))
            if len(self.played) == 1:
                started.set()
                self.assertTrue(self.release.wait(3))

        gns.play_commands = slow
        began = time.monotonic()
        code, _out, err = self._main(["--notify", "--bot", "One", "--title", "one"])
        elapsed = time.monotonic() - began
        self.assertEqual(code, 0, err)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(started.wait(2))
        for name in ("Two", "Three", "Four"):
            code, _out, err = self._main(["--notify", "--bot", name, "--title", name])
            self.assertEqual(code, 0, err)
        time.sleep(0.25)
        self.release.set()
        for thread in self.threads:
            thread.join(4)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(len(self.played), 3)
        for commands in self.played:
            self.assertTrue(commands[0][3].endswith("/Glass.aiff"))
        self.assertIn("burst collapsed (2)", self.log.read_text(encoding="utf-8"))

    def test_shared_cooldown_and_spawn_flags(self):
        def inline(config_path, state_path):
            self.spawned.append(1)
            gns.drain_notify_queue(config_path, state_path, player=gns.play_commands)
            return os.getpid()

        gns.spawn_drainer = inline
        self.config.write_text(
            json.dumps(
                {
                    "cooldown_seconds": 120,
                    "burst_window_seconds": 0.3,
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [{"name": "Alfred answers", "bot": "Alfred", "sound": "Glass"}],
                }
            ),
            encoding="utf-8",
        )
        code, _out, err = self._main(["--notify", "--bot", "Alfred", "--body", "first"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.played), 1)
        code, _out, err = self._main(["--notify", "--bot", "Alfred", "--body", "second secret body"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.played), 1)
        self.assertIn("skip-cooldown", self.log.read_text(encoding="utf-8"))
        self.assertNotIn("secret body", self.log.read_text(encoding="utf-8"))

        seen = {}

        class Proc:
            pid = 4242

        def fake_popen(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return Proc()

        original = gns.subprocess.Popen
        gns.subprocess.Popen = fake_popen
        try:
            pid = self.original_spawn(self.config, self.state)
        finally:
            gns.subprocess.Popen = original
        self.assertEqual(pid, 4242)
        self.assertTrue(seen["kwargs"]["start_new_session"])
        self.assertNotIn("shell", seen["kwargs"])
        self.assertIn("--drain-notify", seen["command"])

    def test_notify_bots_skip_daemon_playback(self):
        db = self.root / "db"
        make_db(db)
        insert_app(db, 1, BUNDLE)
        insert_record(db, 1, 1, body="seed")
        state = self.root / "cursor.json"
        config = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "volume": 100}],
            notify_bots=["Alfred"],
        )
        gns.poll_once(db, config, state, {}, lambda: datetime(2026, 10, 31, 12, 0), play=self.played.append)
        insert_record(db, 2, 1, title="Note from Alfred", body="please approve SECRET")
        insert_record(db, 3, 1, title="Other", body="please approve")
        gns.poll_once(db, config, state, {}, lambda: datetime(2026, 10, 31, 12, 0), play=self.played.append)
        self.assertEqual(len(self.played), 1)
        self.assertTrue(self.played[0][0][3].endswith("/Sosumi.aiff"))
        self.assertEqual(read_state(state), 3)

    def test_invalid_notify_does_not_detach(self):
        self.config.write_text("{", encoding="utf-8")
        code, out, err = self._main(["--notify", "--bot", "Alfred"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("usage:", err.lower())
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.played, [])


class InstallTests(unittest.TestCase):
    def test_install_modes_and_uninstall_shim(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Bot line\n\n" + BOT_LINE + "\n", readme)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            home = Path(tmp) / "home"
            stub = Path(tmp) / "stub"
            home.mkdir()
            stub.mkdir()
            launch = stub / "launchctl"
            launch.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$HOME/launchctl.log\"\n"
                "case \"$1\" in\n"
                "  print) exit 1 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            launch.chmod(launch.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
            notify = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--mode", "notify"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(notify.returncode, 0, notify.stderr)
            self.assertIn(BOT_LINE, notify.stdout)
            self.assertNotIn("Full Disk Access", notify.stdout)
            shim = home / ".local" / "bin" / "grokbot-notify-sounds"
            config = home / ".config" / "grokbot-sounds" / "config.json"
            plist = home / "Library" / "LaunchAgents" / "com.grokbot.notify-sounds.plist"
            self.assertTrue(shim.is_file())
            self.assertIn("exec ", shim.read_text(encoding="utf-8"))
            self.assertIn("grokbot_notify_sounds.py", shim.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["mode"], "notify")
            self.assertFalse(plist.exists())
            self.assertFalse((home / "launchctl.log").exists())

            watch = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--mode", "watch"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(watch.returncode, 0, watch.stderr)
            self.assertTrue(plist.is_file())
            self.assertIn("bootstrap", (home / "launchctl.log").read_text(encoding="utf-8"))
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["mode"], "watch")
            self.assertIn("Full Disk Access", watch.stdout)

            removed = subprocess.run(
                [sys.executable, str(ROOT / "grokbot_notify_sounds.py"), "--uninstall"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(shim.exists())
            self.assertFalse(plist.exists())
            self.assertFalse((home / "Library" / "Application Support" / "GrokBotNotifySounds").exists())
            self.assertTrue(config.is_file())


class ModeTests(unittest.TestCase):
    def test_set_mode_without_launchctl(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text("{}\n", encoding="utf-8")
            log = root / "log"
            out = io.StringIO()
            err = io.StringIO()
            original = gns.sync_agent_mode

            def missing(mode, home=None, finder=None, run=None):
                return original(mode, home or root, finder=lambda _name: None, run=lambda command: None)

            gns.sync_agent_mode = missing
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    code = gns.main(
                        ["--set-mode", "notify", "--config", str(config)],
                        log_path=log,
                    )
            finally:
                gns.sync_agent_mode = original
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(out.getvalue().strip(), "mode set to notify; agent was not changed")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["mode"], "notify")
            err = io.StringIO()
            with redirect_stderr(err):
                code = gns.main(["--set-mode", "sideways", "--config", str(config)], log_path=log)
            self.assertEqual(code, 2)
            self.assertNotIn("usage:", err.getvalue().lower())
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["mode"], "notify")


FIXTURE = (ROOT / "tests" / "fixtures" / "macos-say-voices.txt").read_text(encoding="utf-8")


def _quiet_window(now):
    start = f"{now.hour:02d}:00"
    end = f"{(now.hour + 2) % 24:02d}:00"
    return start, end


class VoiceMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.state = self.root / "state.json"
        self.log = self.root / "log"
        self.sounds = self.root / "sounds"
        self.sounds.mkdir()
        (self.sounds / "Tink.aiff").write_bytes(b"")
        (self.sounds / "Glass.aiff").write_bytes(b"")
        self.config.write_text("{}\n", encoding="utf-8")
        gns.set_sounds_dir(self.sounds)
        gns.set_voice_listing(FIXTURE)
        self.addCleanup(gns.clear_voice_listing)
        self.addCleanup(lambda: gns.set_sounds_dir(None))
        self.addCleanup(lambda: gns.set_voice_state_path(None))

    def _main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(argv + ["--config", str(self.config), "--state", str(self.state)], log_path=self.log)
        return code, out.getvalue(), err.getvalue()

    def test_fixture_matches_names_without_rewriting_the_file(self):
        names = [voice.name for voice in gns.parse_voices(FIXTURE)]
        self.assertIn("Samantha", names)
        self.assertIn("Samantha (English (US))", names)
        self.assertIn("Flo (English (US))", names)
        self.assertEqual(gns.resolve_voice("Flo", gns.parse_voices(FIXTURE)).name, "Flo (English (US))")
        self.assertEqual(gns.resolve_voice("flo", gns.parse_voices(FIXTURE)).name, "Flo (English (US))")
        self.assertEqual(gns.resolve_voice("Samantha", gns.parse_voices(FIXTURE)).name, "Samantha")
        self.assertEqual(gns.resolve_voice("SAMANTHA", gns.parse_voices(FIXTURE)).name, "Samantha")
        repeated = (
            "Samantha            en_US    # Hello\n"
            "Samantha            en_US    # Hello again\n"
            "Samantha            en_GB    # Hi\n"
        )
        voices = gns.parse_voices(repeated)
        self.assertEqual([(voice.name, voice.lang) for voice in voices], [("Samantha", "en_US"), ("Samantha", "en_GB")])
        report = gns.format_voice_report(gns.parse_voices("Samantha            en_US    # Hello\nSamantha            en_US    # Again\n"))
        self.assertEqual(sum(1 for line in report.splitlines() if "Samantha" in line and "en_US" in line), 1)
        self.assertEqual(
            gns.resolve_voice("Samantha (English (US))", gns.parse_voices(FIXTURE)).name,
            "Samantha (English (US))",
        )
        self.assertEqual(gns.resolve_voice("reed", gns.parse_voices(FIXTURE)).name, "Reed (Premium)")
        self.assertEqual(gns.resolve_voice("Reed (Enhanced)", gns.parse_voices(FIXTURE)).name, "Reed (Enhanced)")

        code, out, err = self._main(["--set", "default.voice=flo"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "set default.voice=flo")
        stored = json.loads(self.config.read_text(encoding="utf-8"))["default"]["voice"]
        self.assertEqual(stored, "flo")
        code, out, err = self._main(["--validate"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "ok")
        code, out, err = self._main(["--set", "default.voice=Samantha (English (US))"])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(self.config.read_text(encoding="utf-8"))["default"]["voice"],
            "Samantha (English (US))",
        )
        commands = gns.build_commands("Glass", 40, 100, True, "flo", "", "", "hi")
        self.assertEqual(commands[1][:3], ["say", "-v", "Flo (English (US))"])

    def test_unset_spellings_and_autopick_fallback(self):
        code, out, err = self._main(["--set", "default.say=true", "--set", "default.voice=Samantha"])
        self.assertEqual(code, 0, err)
        code, out, err = self._main(["--set", "default.voice="])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "unset default.voice")
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("voice", data["default"])
        self.assertTrue(data["default"]["say"])
        code, out, err = self._main(["--set", "default.voice=Samantha"])
        self.assertEqual(code, 0, err)
        code, out, err = self._main(["--unset", "default.voice"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "unset default.voice")
        self.assertNotIn("voice", json.loads(self.config.read_text(encoding="utf-8"))["default"])
        config = gns.load_runtime_config(self.config)
        outcome = gns.plan_notification("t", "", "hello", config, now=datetime(2026, 10, 31, 12, 0), cooldowns={})
        self.assertEqual(outcome.commands[1][:3], ["say", "-v", "Zoe (Premium)"])

        only_glass = self.root / "only-glass"
        only_glass.mkdir()
        (only_glass / "Glass.aiff").write_bytes(b"")
        gns.set_sounds_dir(only_glass)
        self.config.write_text(json.dumps({"default": {"sound": "Glass", "volume": 40}}) + "\n", encoding="utf-8")
        before = self.config.read_bytes()
        code, out, err = self._main(["--unset", "default.sound"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("usage:", err.lower())
        self.assertEqual(self.config.read_bytes(), before)

    def test_voice_cache_does_not_call_say(self):
        gns.clear_voice_listing()
        gns.clear_voice_cache()
        gns.set_voice_state_path(self.state)
        cache = self.state.parent / "voices.json"
        cache.write_text(json.dumps([{"name": "Samantha", "lang": "en_US"}], ensure_ascii=False) + "\n", encoding="utf-8")

        def boom():
            raise AssertionError("say was called")

        original = gns.read_voice_listing
        gns.read_voice_listing = boom
        try:
            voices = gns.load_voices()
        finally:
            gns.read_voice_listing = original
            gns.clear_voice_cache()
        self.assertEqual(voices[0].name, "Samantha")


class NotifyBotSetTests(unittest.TestCase):
    def test_middle_dot_and_status(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            config = root / "config.json"
            state = root / "state.json"
            log = root / "log"
            config.write_text("{}\n", encoding="utf-8")
            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gns.main(
                    ["--set", 'notify_bots=["Shopper · Bot"]', "--config", str(config), "--state", str(state)],
                    log_path=log,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(out.getvalue().strip(), 'set notify_bots=["Shopper · Bot"]')
            stored = json.loads(config.read_text(encoding="utf-8"))["notify_bots"]
            self.assertEqual(stored, ["Shopper · Bot"])
            self.assertIn("·", config.read_text(encoding="utf-8"))
            out = io.StringIO()
            with redirect_stdout(out):
                code = gns.main(
                    ["--status", "--config", str(config), "--state", str(state), "--db", str(root / "missing")],
                    log_path=log,
                )
            self.assertEqual(code, 0)
            self.assertIn('notify_bots: ["Shopper · Bot"]', out.getvalue())
            before = config.read_bytes()
            err = io.StringIO()
            with redirect_stderr(err):
                code = gns.main(["--set", "notify_bots=[1]", "--config", str(config), "--state", str(state)], log_path=log)
            self.assertEqual(code, 2)
            self.assertEqual(config.read_bytes(), before)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gns.main(
                    ["--set", 'quiet_hours={"enabled": false, "start": "22:00", "end": "07:00", "mode": "mute"}', "--config", str(config), "--state", str(state)],
                    log_path=log,
                )
            self.assertEqual(code, 0, err.getvalue())
            self.assertFalse(json.loads(config.read_text(encoding="utf-8"))["quiet_hours"]["enabled"])


class QuietTestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.state = self.root / "state.json"
        self.log = self.root / "log"
        self.played = []
        self.original = gns.play_commands
        gns.play_commands = self.played.append
        now = datetime.now()
        start, end = _quiet_window(now)
        self.config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "paused": False,
                    "cooldown_seconds": 0,
                    "quiet_hours": {"enabled": True, "start": start, "end": end, "mode": "mute"},
                    "default": {"sound": "Tink", "volume": 40, "say": False},
                    "rules": [{"name": "Alfred answers", "bot": "Alfred", "sound": "Glass", "volume": 80}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        gns.play_commands = self.original

    def _main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(argv + ["--config", str(self.config), "--state", str(self.state)], log_path=self.log)
        return code, out.getvalue(), err.getvalue()

    def test_test_honors_quiet_hours_and_cooldown(self):
        code, out, err = self._main(["--test", "hello", "--bot", "Alfred"])
        self.assertEqual(code, 0, err)
        self.assertIn("result=muted", out)
        self.assertIn("result=muted", self.log.read_text(encoding="utf-8"))
        self.assertEqual(self.played, [])

        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["quiet_hours"]["mode"] = "soft"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = self._main(["--test", "hello", "--bot", "Alfred"])
        self.assertEqual(code, 0, err)
        self.assertIn("result=soft", out)
        self.assertIn("sound: Tink", out)
        self.assertIn("result=soft", self.log.read_text(encoding="utf-8"))
        self.assertEqual(len(self.played), 1)
        self.assertTrue(self.played[0][0][3].endswith("/Tink.aiff"))

        code, out, err = self._main(["--test", "hello", "--bot", "Alfred", "--ignore-quiet-hours"])
        self.assertEqual(code, 0, err)
        self.assertIn("sound: Glass", out)
        self.assertNotIn("result=soft", out)
        self.assertTrue(self.played[-1][0][3].endswith("/Glass.aiff"))

        data["quiet_hours"]["enabled"] = False
        data["cooldown_seconds"] = 60
        self.config.write_text(json.dumps(data), encoding="utf-8")
        if self.state.exists():
            self.state.unlink()
        self.played.clear()
        code, out, err = self._main(["--test", "hello", "--bot", "Alfred"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.played), 1)
        self.assertTrue(self.played[0][0][3].endswith("/Glass.aiff"))
        code, out, err = self._main(["--test", "hello", "--bot", "Alfred"])
        self.assertEqual(code, 0, err)
        self.assertIn("suppressed by cooldown", out)
        self.assertEqual(len(self.played), 1)
        code, out, err = self._main(["--test", "hello", "--bot", "Alfred", "--ignore-cooldown"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.played), 2)
        self.assertTrue(self.played[-1][0][3].endswith("/Glass.aiff"))

        data["quiet_hours"]["enabled"] = True
        data["quiet_hours"]["mode"] = "mute"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = self._main(
            ["--test", "hello", "--bot", "Alfred", "--ignore-quiet-hours", "--ignore-cooldown"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sound: Glass", out)
        self.assertEqual(len(self.played), 3)
        self.assertTrue(self.played[-1][0][3].endswith("/Glass.aiff"))


class NotifyParentTests(unittest.TestCase):
    def test_missing_bot_does_not_spawn(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = gns._fast_notify_main(["prog", "--notify"])
        self.assertEqual(code, 2)
        self.assertIn("--bot", err.getvalue())

    def _home(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        config = home / "config.json"
        state = home / "state" / "state.json"
        state.parent.mkdir()
        config.write_text(
            json.dumps({"default": {"sound": "Tink", "volume": 40, "say": False}, "rules": []}) + "\n",
            encoding="utf-8",
        )
        bindir = home / "bin"
        bindir.mkdir()
        play_log = home / "played"
        say_log = home / "said"
        afplay = bindir / "afplay"
        afplay.write_text('#!/bin/sh\nprintf \'%s\\n\' "$3" >> "$PLAY_LOG"\nexit 0\n', encoding="utf-8")
        afplay.chmod(0o755)
        say = bindir / "say"
        say.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SAY_LOG"\nsleep 2\nexit 0\n', encoding="utf-8")
        say.chmod(0o755)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["PLAY_LOG"] = str(play_log)
        env["SAY_LOG"] = str(say_log)
        return home, config, state, env, play_log, say_log

    def test_parent_returns_before_a_slow_worker(self):
        _home, config, state, env, _played, _said = self._home()
        env["GROKBOT_NOTIFY_WORKER_DELAY"] = "0.8"
        began = time.monotonic()
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "grokbot_notify_sounds.py"),
                "--notify",
                "--bot",
                "Alfred",
                "--config",
                str(config),
                "--state",
                str(state),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=1,
        )
        elapsed = time.monotonic() - began
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 0.2)

    def test_parent_does_not_call_say(self):
        _home, config, state, env, _played, said = self._home()
        began = time.monotonic()
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "grokbot_notify_sounds.py"),
                "--notify",
                "--bot",
                "Alfred",
                "--title",
                "hi",
                "--config",
                str(config),
                "--state",
                str(state),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=1,
        )
        elapsed = time.monotonic() - began
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 0.2)
        if said.exists():
            self.assertEqual(said.read_text(encoding="utf-8"), "")


class BurstProcessTests(unittest.TestCase):
    def test_six_processes_collapse_from_the_third_rule(self):
        # The detached drain worker can still be writing state when the test ends.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            home = Path(tmp)
            config = home / "config.json"
            state = home / "state" / "state.json"
            state.parent.mkdir()
            play_log = home / "played"
            log_dir = home / "Library" / "Logs"
            sounds = [
                ("One", "Hero"),
                ("Two", "Glass"),
                ("Three", "Ping"),
                ("Four", "Pop"),
                ("Five", "Frog"),
                ("Six", "Funk"),
            ]
            config.write_text(
                json.dumps(
                    {
                        "burst_threshold": 3,
                        "burst_window_seconds": 6,
                        "cooldown_seconds": 0,
                        "queue_max": 20,
                        "default": {"sound": "Tink", "volume": 40, "say": False},
                        "rules": [
                            {"name": bot, "bot": bot, "sound": sound, "volume": 100, "say": False}
                            for bot, sound in sounds
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bindir = home / "bin"
            bindir.mkdir()
            afplay = bindir / "afplay"
            afplay.write_text('#!/bin/sh\nprintf \'%s\\n\' "$3" >> "$PLAY_LOG"\nexit 0\n', encoding="utf-8")
            afplay.chmod(0o755)
            say = bindir / "say"
            say.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            say.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            env["PLAY_LOG"] = str(play_log)
            script = str(ROOT / "grokbot_notify_sounds.py")
            for bot, _sound in sounds:
                proc = subprocess.run(
                    [
                        sys.executable,
                        script,
                        "--notify-worker",
                        "--bot",
                        bot,
                        "--title",
                        bot,
                        "--config",
                        str(config),
                        "--state",
                        str(state),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
            log_path = log_dir / "grokbot-notify-sounds.log"
            deadline = time.time() + 12
            text = ""
            played = ""
            while time.time() < deadline:
                text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                played = play_log.read_text(encoding="utf-8") if play_log.exists() else ""
                if "burst collapsed (4)" in text and played.count(".aiff") >= 3:
                    break
                time.sleep(0.05)
            names = [Path(line.strip()).name for line in played.splitlines() if line.strip()]
            self.assertEqual(names, ["Hero.aiff", "Glass.aiff", "Ping.aiff"], text)
            self.assertNotIn("Pop.aiff", played)
            self.assertNotIn("Frog.aiff", played)
            self.assertNotIn("Funk.aiff", played)
            self.assertIn("burst collapsed (4)", text)
            self.assertEqual(text.count("result=play"), 2)
            self.assertEqual(text.count("result=collapsed"), 4)

    def test_in_process_summary_uses_the_third_sound_and_speech(self):
        gns.set_voice_listing("Samantha            en_US    # Hello.\n")
        self.addCleanup(gns.clear_voice_listing)
        played = []
        messages = []

        class Grab(gns.logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Grab()
        gns.LOG.addHandler(handler)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        lock = Path(tmp.name) / "playback.lock"
        config = config_with([], burst_threshold=3, burst_window_seconds=5, queue_max=20)
        queue = gns.PlaybackQueue(lambda: config, played.append, lock, clock=lambda: 10.0)
        sounds = ["Hero", "Glass", "Ping", "Pop", "Frog", "Funk"]
        try:
            for sound in sounds:
                say = sound == "Ping"
                commands = gns.build_commands(
                    sound,
                    100,
                    100,
                    say,
                    "Samantha" if say else None,
                    "Title",
                    "",
                    "body",
                    say_text="hello there" if say else None,
                )
                queue.enqueue(gns.PlayItem(commands, sound, 10.0, say, sound=sound, source="notify", bot=sound))
            queue.start()
            queue.drain(timeout=2)
            queue.close()
        finally:
            gns.LOG.removeHandler(handler)
        names = [Path(item[0][3]).name for item in played]
        self.assertEqual(names, ["Hero.aiff", "Glass.aiff", "Ping.aiff"])
        self.assertEqual(played[2][1], ["say", "-v", "Samantha", "and 3 more"])
        self.assertIn("burst collapsed (4)", messages)
        self.assertIn("burst summary: sound=Ping volume=1 say=and 3 more", messages)
        self.assertEqual(sum(1 for message in messages if message.endswith("result=play")), 2)
        self.assertEqual(sum(1 for message in messages if message.endswith("result=collapsed")), 4)

        played.clear()
        lone = gns.build_commands("Ping", 100, 100, True, "Samantha", "Title", "", "body", say_text="hello there")
        queue = gns.PlaybackQueue(lambda: config, played.append, lock, clock=lambda: 10.0)
        for sound in ("Hero", "Glass", "Ping"):
            commands = lone if sound == "Ping" else gns.build_commands(sound, 100, 100, False, None, "", "", "")
            queue.enqueue(gns.PlayItem(commands, sound, 10.0, sound == "Ping"))
        queue.start()
        queue.drain(timeout=2)
        queue.close()
        self.assertEqual(played[2][1][-1], "hello there")
        self.assertNotIn("and 0 more", " ".join(played[2][1]))


class DedupeProcessTests(unittest.TestCase):
    def test_omitted_window_follows_the_burst_window(self):
        omitted = gns.normalize_config(
            {"default": {"sound": "Tink", "volume": 40}, "burst_window_seconds": 2}
        )
        self.assertNotIn("dedupe_window_seconds", omitted)
        self.assertEqual(gns.dedupe_window_seconds(omitted), 2.0)
        disabled = gns.normalize_config(
            {
                "default": {"sound": "Tink", "volume": 40},
                "burst_window_seconds": 2,
                "dedupe_window_seconds": 0,
            }
        )
        self.assertEqual(gns.dedupe_window_seconds(disabled), 0.0)

    def _layout(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        config = home / "config.json"
        state = home / "state" / "state.json"
        state.parent.mkdir()
        config.write_text(
            json.dumps(
                {
                    "burst_threshold": 3,
                    "burst_window_seconds": 4,
                    "cooldown_seconds": 0,
                    "default": {"sound": "Tink", "volume": 40, "say": False},
                    "rules": [
                        {"name": "Ping", "contains": "hello", "sound": "Glass", "volume": 100, "say": False}
                    ],
                }
            ),
            encoding="utf-8",
        )
        bindir = home / "bin"
        bindir.mkdir()
        play_log = home / "played"
        afplay = bindir / "afplay"
        afplay.write_text('#!/bin/sh\nprintf \'%s\\n\' "$3" >> "$PLAY_LOG"\nexit 0\n', encoding="utf-8")
        afplay.chmod(0o755)
        say = bindir / "say"
        say.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        say.chmod(0o755)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["PLAY_LOG"] = str(play_log)
        log_path = home / "Library" / "Logs" / "grokbot-notify-sounds.log"
        return config, state, env, play_log, log_path

    def _notify(self, env, config, state, bot):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "grokbot_notify_sounds.py"),
                "--notify",
                "--bot",
                bot,
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

    def _wait(self, path, predicate, timeout=8):
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if predicate(text):
                return text
            time.sleep(0.05)
        return text

    def test_same_rule_and_bot_logs_dup(self):
        config, state, env, play_log, log_path = self._layout()
        self._notify(env, config, state, "Red")
        text = self._wait(log_path, lambda raw: "result=play" in raw)
        self.assertIn("result=play", text)
        self._notify(env, config, state, "Red")
        text = self._wait(log_path, lambda raw: "result=dup" in raw)
        self.assertIn("result=dup", text)
        self.assertEqual(text.count("result=play"), 1)
        self.assertNotIn("result=collapsed", text)
        played = play_log.read_text(encoding="utf-8") if play_log.exists() else ""
        self.assertEqual(played.count("Glass.aiff"), 1)

    def test_same_rule_different_bot_plays_both(self):
        config, state, env, play_log, log_path = self._layout()
        self._notify(env, config, state, "Red")
        text = self._wait(log_path, lambda raw: "result=play" in raw)
        self.assertIn("result=play", text)
        self._notify(env, config, state, "Blue")
        text = self._wait(log_path, lambda raw: raw.count("result=play") >= 2)
        self.assertGreaterEqual(text.count("result=play"), 2)
        self.assertNotIn("result=dup", text)
        played = self._wait(play_log, lambda raw: raw.count("Glass.aiff") >= 2)
        self.assertGreaterEqual(played.count("Glass.aiff"), 2)

    def test_zero_dedupe_window_plays_both(self):
        config, state, env, play_log, log_path = self._layout()
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "grokbot_notify_sounds.py"),
                "--set",
                "dedupe_window_seconds=0",
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
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["dedupe_window_seconds"], 0)
        self._notify(env, config, state, "Red")
        text = self._wait(log_path, lambda raw: "result=play" in raw)
        self.assertIn("result=play", text)
        self._notify(env, config, state, "Red")
        text = self._wait(log_path, lambda raw: raw.count("result=play") >= 2)
        self.assertGreaterEqual(text.count("result=play"), 2)
        self.assertNotIn("result=dup", text)
        played = self._wait(play_log, lambda raw: raw.count("Glass.aiff") >= 2)
        self.assertGreaterEqual(played.count("Glass.aiff"), 2)
