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
        self.tmp = tempfile.TemporaryDirectory()
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
        self.assertEqual(paths, ["Glass.aiff", "Tink.aiff"])

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
        self.assertEqual([Path(path).name for path in played], ["Glass.aiff", "Tink.aiff"])

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
        self.tmp = tempfile.TemporaryDirectory()
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
        self.tmp = tempfile.TemporaryDirectory()
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
        self.tmp = tempfile.TemporaryDirectory()
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
                    "burst_window_seconds": 5,
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
            thread.join(3)
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
        self.threads[0].join(2)
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
        started = threading.Event()

        def slow(commands):
            self.played.append(list(commands))
            if len(self.played) == 1:
                started.set()
                self.assertTrue(self.release.wait(3))

        gns.play_commands = slow
        began = time.monotonic()
        code, _out, err = self._main(["--notify", "--bot", "Alfred", "--title", "one"])
        elapsed = time.monotonic() - began
        self.assertEqual(code, 0, err)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(started.wait(2))
        for index in range(3):
            code, _out, err = self._main(["--notify", "--bot", "Alfred", "--title", f"more-{index}"])
            self.assertEqual(code, 0, err)
        time.sleep(0.25)
        self.release.set()
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(len(self.played), 2)
        self.assertTrue(self.played[0][0][3].endswith("/Glass.aiff"))
        self.assertTrue(self.played[1][0][3].endswith("/Tink.aiff"))

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
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            stub = Path(tmp) / "stub"
            home.mkdir()
            stub.mkdir()
            launch = stub / "launchctl"
            launch.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$HOME/launchctl.log"\nexit 0\n', encoding="utf-8")
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
        with tempfile.TemporaryDirectory() as tmp:
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
