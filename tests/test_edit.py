import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

import grokbot_notify_sounds as gns


def document(rules=None, **extra):
    data = {
        "enabled": True,
        "paused": False,
        "poll_interval_seconds": 2,
        "bundle_id": "com.example.grokbot",
        "master_volume": 50,
        "cooldown_seconds": 0,
        "default": {"sound": "Tink", "volume": 40},
        "rules": [] if rules is None else rules,
        "note": "keep-me",
    }
    data.update(extra)
    return data


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.log_path = Path(self.tmp.name) / "agent.log"
        self.path.write_text(json.dumps(document(), indent=2) + "\n", encoding="utf-8")
        self.original_play = gns.play_commands
        self.original_probe = gns.probe_agent
        self.played = []
        gns.play_commands = self.played.append
        gns.probe_agent = lambda: "unknown"
        gns.set_voice_listing(
            "Zarvox               en_US    # Zarvox\nSamantha             en_US    # Hello\n"
        )
        self.addCleanup(gns.clear_voice_listing)
        self.addCleanup(self._restore)

    def _restore(self):
        gns.play_commands = self.original_play
        gns.probe_agent = self.original_probe

    def run_main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(argv + ["--config", str(self.path)], log_path=self.log_path)
        return code, out.getvalue(), err.getvalue()

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_add_remove_update_and_move_are_atomic(self):
        code, _out, err = self.run_main(
            ["--add-rule", json.dumps({"name": "Needs approval", "contains": "approve", "sound": "Sosumi"})]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_main(
            ["--add-rule", json.dumps({"name": "Helper answers", "bot": "Helper", "sound": "Glass", "volume": 80})]
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self.run_main(
            [
                "--add-rule",
                json.dumps(
                    {
                        "name": "Helper shipped",
                        "bot": "Helper",
                        "regex": "shipped|delivered",
                        "sound": "Hero",
                        "volume": 60,
                    }
                ),
            ]
        )
        self.assertEqual(code, 0, err)
        names = [rule["name"] for rule in self.load()["rules"]]
        self.assertEqual(names, ["Needs approval", "Helper answers", "Helper shipped"])
        self.assertEqual(self.load()["note"], "keep-me")
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

        before = self.path.read_bytes()
        code, _out, err = self.run_main(
            ["--add-rule", json.dumps({"name": "Helper answers", "contains": "x", "sound": "Ping"})]
        )
        self.assertEqual(code, 2)
        self.assertIn("Duplicate rule name", err)
        self.assertEqual(self.path.read_bytes(), before)

        code, _out, err = self.run_main(
            ["--update-rule", "Helper answers", json.dumps({"volume": 70, "bot": None, "contains": "hello"})]
        )
        self.assertEqual(code, 0, err)
        updated = self.load()["rules"][1]
        self.assertEqual(updated["volume"], 70)
        self.assertEqual(updated["contains"], "hello")
        self.assertNotIn("bot", updated)

        before = self.path.read_bytes()
        code, _out, err = self.run_main(
            ["--update-rule", "Needs approval", json.dumps({"name": "Helper shipped"})]
        )
        self.assertEqual(code, 2)
        self.assertIn("Duplicate rule name", err)
        self.assertEqual(self.path.read_bytes(), before)

        code, _out, err = self.run_main(["--move-rule", "Helper shipped", "--to", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [rule["name"] for rule in self.load()["rules"]],
            ["Helper shipped", "Needs approval", "Helper answers"],
        )

        before = self.path.read_bytes()
        code, _out, err = self.run_main(["--move-rule", "Missing", "--to", "1"])
        self.assertEqual(code, 2)
        self.assertIn("No rule named", err)
        self.assertEqual(self.path.read_bytes(), before)
        code, _out, err = self.run_main(["--move-rule", "Helper shipped", "--to", "4"])
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), before)

        code, _out, err = self.run_main(["--remove-rule", "Needs approval"])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [rule["name"] for rule in self.load()["rules"]],
            ["Helper shipped", "Helper answers"],
        )
        before = self.path.read_bytes()
        code, _out, err = self.run_main(["--remove-rule", "Needs approval"])
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_set_quiet_hours_cooldown_and_preserves_unknown_keys(self):
        code, _out, err = self.run_main(
            [
                "--set",
                "quiet_hours.enabled=true",
                "--set",
                "quiet_hours.start=22:00",
                "--set",
                "quiet_hours.end=07:00",
                "--set",
                "quiet_hours.mode=mute",
                "--set",
                "cooldown_seconds=15",
                "--set",
                "enabled=false",
            ]
        )
        self.assertEqual(code, 0, err)
        data = self.load()
        self.assertEqual(data["cooldown_seconds"], 15)
        self.assertIs(data["enabled"], False)
        self.assertEqual(
            data["quiet_hours"],
            {"enabled": True, "start": "22:00", "end": "07:00", "mode": "mute"},
        )
        self.assertEqual(data["note"], "keep-me")
        self.assertIn("\n  ", self.path.read_text(encoding="utf-8"))

    def test_pause_resume_and_master_volume(self):
        code, _out, err = self.run_main(["--pause"])
        self.assertEqual(code, 0, err)
        self.assertIs(self.load()["paused"], True)
        self.assertIs(self.load()["enabled"], True)
        code, _out, err = self.run_main(["--disable"])
        self.assertEqual(code, 0, err)
        self.assertIs(self.load()["paused"], True)
        self.assertIs(self.load()["enabled"], False)
        code, _out, err = self.run_main(["--resume"])
        self.assertEqual(code, 0, err)
        self.assertIs(self.load()["paused"], False)
        code, _out, err = self.run_main(["--set-master-volume", "80"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load()["master_volume"], 80)
        before = self.path.read_bytes()
        code, _out, _err = self.run_main(["--set", "master_volume=50.5"])
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_is_created_from_defaults(self):
        self.path.unlink()
        code, _out, err = self.run_main(["--pause"])
        self.assertEqual(code, 0, err)
        data = self.load()
        self.assertIs(data["paused"], True)
        self.assertEqual(data["default"]["sound"], "Tink")
        self.assertEqual(data["rules"], [])
        self.assertEqual(data["master_volume"], 100)

    def test_play_uses_master_volume(self):
        code, out, err = self.run_main(["--play", "Glass", "--volume", "80"])
        self.assertEqual(code, 0, err)
        self.assertIn("afplay_volume: 0.4", out)
        self.assertEqual(
            self.played[0][0],
            ["afplay", "-v", "0.4", "/System/Library/Sounds/Glass.aiff"],
        )

    def test_play_missing_command_exits_zero(self):
        def missing(commands):
            raise FileNotFoundError(2, "No such file or directory", "afplay")

        gns.play_commands = missing
        code, out, err = self.run_main(["--play", "Ping"])
        self.assertEqual(code, 0)
        self.assertIn("afplay_volume:", out)
        self.assertIn("afplay", err)

    def test_test_rule_plays_when_disabled(self):
        self.path.write_text(
            json.dumps(
                document(
                    [
                        {
                            "name": "Needs approval",
                            "contains": "approve",
                            "sound": "Sosumi",
                            "volume": 80,
                            "enabled": False,
                        }
                    ]
                )
            ),
            encoding="utf-8",
        )
        code, out, err = self.run_main(["--test-rule", "Needs approval"])
        self.assertEqual(code, 0, err)
        self.assertIn("rule: Needs approval", out)
        self.assertIn("enabled: false", out)
        self.assertIn("afplay_volume: 0.4", out)
        self.assertEqual(self.played[0][0][0], "afplay")
        code, _out, err = self.run_main(["--test-rule", "Missing"])
        self.assertEqual(code, 2)
        self.assertIn("No rule named", err)

    def test_test_bot_does_not_need_the_title(self):
        self.path.write_text(
            json.dumps(
                document([{"name": "Helper answers", "bot": "Helper", "sound": "Glass", "volume": 80}])
            ),
            encoding="utf-8",
        )
        code, out, err = self.run_main(
            ["--test", "hello", "--bot", "Helper", "--state", str(Path(self.tmp.name) / "state.json")]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("rule: Helper answers", out)
        self.assertIn("afplay_volume: 0.4", out)
        self.assertEqual(self.played[0][0][2], "0.4")

    def test_status_db_readable_and_not_permitted(self):
        db = Path(self.tmp.name) / "db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier TEXT)")
        conn.commit()
        conn.close()
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gns.main(
                ["--status", "--config", str(self.path), "--db", str(db), "--state", str(Path(self.tmp.name) / "state.json")],
                log_path=self.log_path,
            )
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("db: readable", text)
        self.assertIn("paused: false", text)
        self.assertIn("master_volume: 50", text)
        self.assertIn("quiet_hours: off", text)
        self.assertIn("rules: 0", text)
        self.assertIn("agent: unknown", text)
        self.assertIn(f"db_path: {db}", text)

        blocked = Path(self.tmp.name) / "blocked"
        blocked.write_bytes(b"")

        def deny(path):
            raise sqlite3.OperationalError("authorization denied")

        self.assertEqual(gns.db_status(blocked, opener=deny), "not permitted")
        self.assertEqual(gns.db_status(Path(self.tmp.name) / "absent"), "missing")
        self.assertEqual(gns.db_status(db), "readable")

        def explode(*_args, **_kwargs):
            raise AssertionError("launchctl was called")

        self.assertEqual(self.original_probe(which=lambda _name: None, run=explode), "unknown")

        class Result:
            def __init__(self, code):
                self.returncode = code

        self.assertEqual(
            self.original_probe(which=lambda _name: "/bin/launchctl", run=lambda *_a, **_k: Result(0)),
            "loaded",
        )
        self.assertEqual(
            self.original_probe(which=lambda _name: "/bin/launchctl", run=lambda *_a, **_k: Result(1)),
            "not loaded",
        )

        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code = gns.main(["--status", "--config", str(bad)], log_path=self.log_path)
        self.assertEqual(code, 1)
        self.assertIn("Invalid JSON", err.getvalue())

    def test_update_from_stdin_and_uninstall_skips_missing_launchctl(self):
        payload = '{"say": true, "voice": "Zarvox"}\n'
        self.path.write_text(
            json.dumps(document([{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}])),
            encoding="utf-8",
        )
        old_stdin = gns.sys.stdin
        gns.sys.stdin = io.StringIO(payload)
        try:
            code, _out, err = self.run_main(["--update-rule", "Needs approval", "-"])
        finally:
            gns.sys.stdin = old_stdin
        self.assertEqual(code, 0, err)
        rule = self.load()["rules"][0]
        self.assertIs(rule["say"], True)
        self.assertEqual(rule["voice"], "Zarvox")

        home = Path(self.tmp.name) / "home"
        support = home / "Library" / "Application Support" / "GrokBotNotifySounds"
        plist = home / "Library" / "LaunchAgents" / "com.grokbot.notify-sounds.plist"
        config_dir = home / ".config" / "grokbot-sounds"
        log = home / "Library" / "Logs" / "grokbot-notify-sounds.log"
        support.mkdir(parents=True)
        (support / "grokbot_notify_sounds.py").write_text("print('x')\n", encoding="utf-8")
        plist.parent.mkdir(parents=True)
        plist.write_text("plist\n", encoding="utf-8")
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text("{}\n", encoding="utf-8")
        log.parent.mkdir(parents=True)
        log.write_text("log\n", encoding="utf-8")
        calls = []
        code = gns.uninstall_agent(home, exists=lambda _name: None, run=lambda command: calls.append(command))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertFalse(support.exists())
        self.assertFalse(plist.exists())
        self.assertTrue((config_dir / "config.json").exists())
        self.assertTrue(log.exists())
        code = gns.uninstall_agent(home, exists=lambda _name: None)
        self.assertEqual(code, 0)
