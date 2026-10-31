import io
import json
import logging
import plistlib
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
import tempfile

import grokbot_notify_sounds as gns
from tests.helpers import (
    BUNDLE,
    config_with,
    insert_app,
    insert_raw,
    insert_record,
    make_db,
    read_state,
)

NOW = datetime(2026, 10, 31, 12, 0, 0)


class PlistTests(unittest.TestCase):
    def test_binary_plist_fields_and_bytes(self):
        blob = plistlib.dumps(
            {"req": {"titl": "Title", "subt": "Sub", "body": "Body"}, "other": 1},
            fmt=plistlib.FMT_BINARY,
        )
        self.assertEqual(gns.decode_notification(blob), ("Title", "Sub", "Body"))

        missing = plistlib.dumps({"req": {"titl": "Only"}}, fmt=plistlib.FMT_BINARY)
        self.assertEqual(gns.decode_notification(missing), ("Only", "", ""))

        raw = plistlib.dumps(
            {"req": {"titl": b"Hi", "subt": b"caf\xe9", "body": b"ok"}},
            fmt=plistlib.FMT_BINARY,
        )
        title, subtitle, body = gns.decode_notification(raw)
        self.assertEqual(title, "Hi")
        self.assertEqual(body, "ok")
        self.assertIn("caf", subtitle)
        self.assertNotEqual(subtitle, "")

        empty = plistlib.dumps({"nope": True}, fmt=plistlib.FMT_BINARY)
        self.assertEqual(gns.decode_notification(empty), ("", "", ""))


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "db"
        self.state = root / "state.json"
        self.log_path = root / "agent.log"
        make_db(self.db)
        insert_app(self.db, 1, BUNDLE)
        insert_app(self.db, 2, "com.example.other")
        self.played = []
        self.config = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "volume": 100}]
        )

    def poll(self, config=None, cooldowns=None):
        gns.poll_once(
            self.db,
            self.config if config is None else config,
            self.state,
            {} if cooldowns is None else cooldowns,
            lambda: NOW,
            play=self.played.append,
        )

    def test_last_seen_plays_each_new_row_once_across_restarts(self):
        insert_record(self.db, 1, 1, body="old approve")
        insert_record(self.db, 4, 2, body="other app approve")
        self.poll()
        self.assertEqual(self.played, [])
        self.assertEqual(read_state(self.state), 1)

        insert_record(self.db, 5, 1, title="Helper", body="please approve")
        self.poll()
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.played[0][0][0], "afplay")
        self.assertEqual(self.played[0][0][2], "1")
        self.assertEqual(self.played[0][0][3], "/System/Library/Sounds/Sosumi.aiff")
        self.assertEqual(read_state(self.state), 5)

        self.played.clear()
        self.poll()
        self.assertEqual(self.played, [])

        insert_record(self.db, 6, 1, body="approve the follow-up")
        insert_record(self.db, 7, 2, body="approve elsewhere")
        self.poll()
        self.assertEqual(len(self.played), 1)
        self.assertEqual(read_state(self.state), 6)

    def test_each_row_advances_before_the_next_play(self):
        insert_record(self.db, 1, 1, body="seed")
        self.poll()
        seen = []

        def play(commands):
            seen.append(read_state(self.state))
            self.played.append(commands)

        insert_record(self.db, 5, 1, body="approve one")
        insert_record(self.db, 6, 1, body="approve two")
        gns.poll_once(self.db, self.config, self.state, {}, lambda: NOW, play=play)
        self.assertEqual(seen, [1, 5])
        self.assertEqual(read_state(self.state), 6)
        self.assertEqual(len(self.played), 2)

    def test_skip_and_bad_payload_still_advance(self):
        insert_record(self.db, 2, 1, body="already here")
        self.poll()
        disabled = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}],
            enabled=False,
        )
        insert_record(self.db, 3, 1, body="approve while off")
        self.poll(disabled)
        self.assertEqual(self.played, [])
        self.assertEqual(read_state(self.state), 3)

        insert_raw(self.db, 4, 1, b"this is not a plist")
        insert_record(self.db, 5, 1, body="please approve")
        self.poll()
        self.assertEqual(len(self.played), 1)
        self.assertEqual(read_state(self.state), 5)

    def test_empty_bundle_matches_nothing(self):
        insert_record(self.db, 8, 1, body="approve")
        empty = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}],
            bundle_id="",
        )
        self.poll(empty)
        self.assertEqual(self.played, [])
        self.assertEqual(read_state(self.state), 0)
        insert_record(self.db, 9, 1, body="approve again")
        self.poll(empty)
        self.assertEqual(self.played, [])

    def test_cooldown_inside_one_poll(self):
        insert_record(self.db, 1, 1, body="seed")
        self.poll()
        config = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}],
            cooldown_seconds=30,
        )
        insert_record(self.db, 2, 1, body="approve")
        insert_record(self.db, 3, 1, body="approve")
        box = {}
        gns.poll_once(self.db, config, self.state, box, lambda: NOW, play=self.played.append)
        self.assertEqual(len(self.played), 1)
        self.assertEqual(read_state(self.state), 3)

    def test_database_is_opened_read_only(self):
        conn = gns.connect_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.Error):
                conn.execute("INSERT INTO app (app_id, identifier) VALUES (9, 'nope')")
        finally:
            conn.close()

    def test_missing_database_mentions_full_disk_access(self):
        messages = []

        class Grab(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Grab()
        gns.LOG.addHandler(handler)
        try:
            missing = Path(self.tmp.name) / "missing-db"
            gns.poll_once(missing, self.config, self.state, {}, lambda: NOW, play=self.played.append)
        finally:
            gns.LOG.removeHandler(handler)
        self.assertTrue(any("Full Disk Access" in message for message in messages))
        self.assertFalse(missing.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.played, [])

    def test_log_does_not_store_the_body(self):
        gns.setup_logging(self.log_path)
        try:
            insert_record(self.db, 1, 1, body="seed")
            self.poll()
            token = "SUPERSECRET_BODY_TOKEN"
            insert_record(self.db, 2, 1, title="Hello", body=f"please approve {token}")
            self.poll()
            for handler in gns.LOG.handlers:
                handler.flush()
            text = self.log_path.read_text(encoding="utf-8")
        finally:
            for handler in list(gns.LOG.handlers):
                if isinstance(handler, logging.handlers.RotatingFileHandler):
                    gns.LOG.removeHandler(handler)
                    handler.close()
        self.assertIn("rec_id=2", text)
        self.assertIn("Needs approval", text)
        self.assertIn("Sosumi", text)
        self.assertNotIn(token, text)

    def test_pause_advances_last_seen_without_playing(self):
        insert_record(self.db, 1, 1, body="seed")
        self.poll()
        paused = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "say": True}],
            paused=True,
        )
        insert_record(self.db, 2, 1, title="Hello", body="please approve")
        self.poll(paused)
        self.assertEqual(self.played, [])
        self.assertEqual(read_state(self.state), 2)
        self.poll()
        self.assertEqual(self.played, [])
        self.assertEqual(read_state(self.state), 2)

    def test_list_bundle_ids_are_sorted(self):
        conn = gns.connect_readonly(self.db)
        try:
            self.assertEqual(gns.list_bundle_ids(conn), ["com.example.grokbot", "com.example.other"])
        finally:
            conn.close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log_path = self.root / "cli.log"
        self.original_play = gns.play_commands
        self.played = []

        def fake(commands):
            self.played.append(commands)

        gns.play_commands = fake
        self.addCleanup(self._restore_play)

    def _restore_play(self):
        gns.play_commands = self.original_play

    def test_list_rules_validate_and_test(self):
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "bundle_id": "",
                    "master_volume": 50,
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [
                        {
                            "name": "Needs approval",
                            "contains": ["approve", "needs your OK"],
                            "sound": "Sosumi",
                            "enabled": False,
                        },
                        {
                            "name": "Helper answers",
                            "bot": "Helper",
                            "sound": "Glass",
                            "volume": 80,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = gns.main(["--list-rules", "--config", str(path), "--db", str(self.root / "nope")], log_path=self.log_path)
        self.assertEqual(code, 0)
        listed = out.getvalue().splitlines()
        self.assertEqual(len(listed), 2)
        self.assertIn("name=Needs approval", listed[0])
        self.assertIn("sound=Sosumi", listed[0])
        self.assertIn("enabled=false", listed[0])
        self.assertIn("name=Helper answers", listed[1])
        self.assertIn("sound=Glass", listed[1])
        self.assertIn("volume=80", listed[1])

        err = io.StringIO()
        with redirect_stderr(err):
            code = gns.main(["--validate", "--config", str(self.root / "missing.json")], log_path=self.log_path)
        self.assertEqual(code, 2)
        self.assertIn("Cannot read config", err.getvalue())

        bad = self.root / "bad.json"
        bad.write_text("{", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code = gns.main(["--validate", "--config", str(bad)], log_path=self.log_path)
        self.assertEqual(code, 2)
        self.assertIn("Invalid JSON", err.getvalue())

        out = io.StringIO()
        with redirect_stdout(out):
            code = gns.main(
                ["--test", "status update", "--title", "Helper", "--config", str(path), "--state", str(self.root / "state.json")],
                log_path=self.log_path,
            )
        self.assertEqual(code, 0)
        self.assertIn("rule: Helper answers", out.getvalue())
        self.assertIn("result: play", out.getvalue())
        self.assertEqual(self.played[0][0][:3], ["afplay", "-v", "0.4"])
        self.assertTrue(self.played[0][0][3].endswith("/Glass.aiff"))

    def test_test_flag_reports_playback_miss_and_still_exits_zero(self):
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}],
                }
            ),
            encoding="utf-8",
        )

        def missing(commands):
            raise FileNotFoundError(2, "No such file or directory", "afplay")

        gns.play_commands = missing
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(
                ["--test", "please approve", "--config", str(path), "--state", str(self.root / "state.json")],
                log_path=self.log_path,
            )
        self.assertEqual(code, 0)
        self.assertIn("rule: Needs approval", out.getvalue())
        self.assertIn("afplay", err.getvalue())

    def test_once_and_list_apps(self):
        db = self.root / "db"
        state = self.root / "state.json"
        make_db(db)
        insert_app(db, 1, "com.example.zeta")
        insert_app(db, 2, "com.example.alpha")
        insert_record(db, 3, 1, body="approve old")
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "bundle_id": "com.example.zeta",
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}],
                }
            ),
            encoding="utf-8",
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = gns.main(
                ["--list-apps", "--db", str(db)],
                log_path=self.log_path,
            )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().splitlines(), ["com.example.alpha", "com.example.zeta"])

        code = gns.main(
            ["--once", "--db", str(db), "--state", str(state), "--config", str(config)],
            log_path=self.log_path,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.played, [])
        self.assertEqual(read_state(state), 3)

        insert_record(db, 4, 1, body="please approve")
        code = gns.main(
            ["--once", "--db", str(db), "--state", str(state), "--config", str(config)],
            log_path=self.log_path,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.played), 1)
        self.assertEqual(read_state(state), 4)

    def test_list_sounds_missing_directory(self):
        config = self.root / "empty.json"
        config.write_text("{}\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = gns.main(["--list-sounds", "--config", str(config)], log_path=self.log_path)
        self.assertEqual(code, 0)
        self.assertIn("Glass: bright glassy chime", out.getvalue().splitlines())

    def test_db_candidates_start_with_the_group_container(self):
        candidates = gns.notification_db_candidates()
        self.assertEqual(
            candidates[0],
            Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db",
        )
        explicit = Path("/tmp/grokbot-notify-explicit-db")
        self.assertEqual(gns.resolve_db_path(explicit), explicit)
