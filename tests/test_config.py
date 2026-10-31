import json
import logging
import os
import re
import unittest
from pathlib import Path

import grokbot_notify_sounds as gns
from tests.helpers import config_with

ROOT = Path(__file__).resolve().parents[1]


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class ConfigTests(unittest.TestCase):
    def test_shipped_config_is_strict_json_and_has_no_rules(self):
        text = (ROOT / "config.json").read_text(encoding="utf-8")
        json.loads(text)
        config = gns.parse_config_text(text)
        self.assertEqual(config["rules"], [])
        self.assertEqual(config["bundle_id"], "")
        self.assertEqual(config["master_volume"], 100)
        self.assertEqual(config["default"]["sound"], "Tink")
        self.assertEqual(config["default"]["volume"], 40)
        self.assertEqual(config["cooldown_seconds"], 0)
        self.assertFalse(config["quiet_hours"]["enabled"])
        self.assertNotIn("_comment", config)
        self.assertNotIn("_comment", config["default"])
        self.assertEqual(config["default"]["sound"], gns.builtin_defaults()["default"]["sound"])

    def test_example_jsonc_loads_three_rules(self):
        text = (ROOT / "config.example.jsonc").read_text(encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(text)
        config = gns.parse_config_text(text)
        self.assertEqual(
            [rule["name"] for rule in config["rules"]],
            ["Alfred shipped", "Needs approval", "Alfred answers"],
        )
        shipped, approval, answers = config["rules"]
        self.assertEqual(shipped["bot"], "Alfred")
        self.assertEqual(shipped["regex"], "shipped|delivered")
        self.assertEqual(shipped["match_in"], "body")
        self.assertEqual(shipped["sound"], "Hero")
        self.assertEqual(shipped["volume"], 60)
        self.assertTrue(shipped["say"])
        self.assertEqual(shipped["voice"], "Zarvox")
        self.assertEqual(approval["contains"], ["approve", "needs your OK"])
        self.assertIsNone(approval["bot"])
        self.assertEqual(approval["sound"], "Sosumi")
        self.assertIsNone(approval["volume"])
        self.assertEqual(answers["bot"], "Alfred")
        self.assertIsNone(answers["contains"])
        self.assertIsNone(answers["regex"])
        self.assertEqual(answers["sound"], "Glass")
        self.assertEqual(answers["volume"], 80)
        self.assertNotIn("bots", config)
        self.assertNotIn("categories", config)

    def test_jsonc_preserves_strings_and_strips_comments(self):
        text = """
        {
          // line comment with "quotes"
          "bundle_id": "com.example.app // not a comment",
          "default": {"sound": "Tink", "volume": 40},
          /* block
             comment */
          "rules": [
            {
              "name": "Star",
              "contains": "a /* b */ c",
              "sound": "Ping",
            },
          ],
        }
        """
        config = gns.parse_config_text(text)
        self.assertEqual(config["bundle_id"], "com.example.app // not a comment")
        self.assertEqual(config["rules"][0]["contains"], ["a /* b */ c"])

    def test_invalid_regex_duplicate_name_and_missing_matcher(self):
        with self.assertRaises(gns.ConfigError) as caught:
            gns.parse_config_text(
                json.dumps(
                    {
                        "default": {"sound": "Tink", "volume": 40},
                        "rules": [
                            {"name": "Bad", "regex": "(", "sound": "Ping"},
                            {"name": "Same", "contains": "a", "sound": "Ping"},
                            {"name": "Same", "contains": "b", "sound": "Ping"},
                            {"name": "Empty", "sound": "Ping"},
                        ],
                    }
                )
            )
        text = str(caught.exception)
        self.assertIn("invalid regex", text)
        self.assertIn("Duplicate rule name", text)
        self.assertIn("needs a bot, contains, or regex", text)

    def test_volume_rejects_fractions_bools_and_out_of_range(self):
        for bad in (50.5, 50.0, True, -1, 101, "80"):
            with self.assertRaises(gns.ConfigError):
                gns.normalize_config(
                    {
                        "master_volume": 100,
                        "default": {"sound": "Tink", "volume": 40},
                        "rules": [
                            {"name": "A", "contains": "hi", "sound": "Glass", "volume": bad}
                        ],
                    }
                )
        for bad in (50.5, True, -1, 101):
            with self.assertRaises(gns.ConfigError):
                gns.normalize_config({"master_volume": bad, "default": {"sound": "Tink"}})
            with self.assertRaises(gns.ConfigError):
                gns.normalize_config({"default": {"sound": "Tink", "volume": bad}})

    def test_omitted_volume_uses_default_then_100(self):
        using_default = config_with(
            [{"name": "A", "contains": "hi", "sound": "Glass"}]
        )
        self.assertEqual(gns.classify("x", "", "hi", using_default).volume, 40)
        omitted = gns.normalize_config(
            {
                "default": {"sound": "Tink"},
                "rules": [{"name": "A", "contains": "hi", "sound": "Glass"}],
            }
        )
        self.assertEqual(gns.classify("x", "", "hi", omitted).volume, 100)
        self.assertEqual(omitted["master_volume"], 100)

    def test_other_schema_errors(self):
        with self.assertRaises(gns.ConfigError):
            gns.normalize_config({"poll_interval_seconds": 0, "default": {"sound": "Tink"}})
        with self.assertRaises(gns.ConfigError):
            gns.normalize_config(
                {
                    "default": {"sound": "Tink"},
                    "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00", "mode": "loud"},
                }
            )
        with self.assertRaises(gns.ConfigError):
            gns.normalize_config({"default": {"sound": "Tink"}, "bots": []})
        with self.assertRaises(gns.ConfigError):
            gns.normalize_config(
                {
                    "default": {"sound": "Tink"},
                    "rules": [{"name": "Rel", "contains": "a", "sound": "sounds/bell.wav"}],
                }
            )

    def test_reload_follows_mtime_and_keeps_last_good_config(self):
        with self.subTest("files"):
            self._reload_case()

    def _reload_case(self):
        import tempfile

        handler = ListHandler()
        gns.LOG.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "default": {"sound": "Tink", "volume": 40},
                            "rules": [{"name": "Alpha", "contains": "hello", "sound": "Glass"}],
                        }
                    ),
                    encoding="utf-8",
                )
                source = gns.ConfigSource(path)
                self.assertEqual(source.current()["rules"][0]["sound"], "Glass")
                stamp = path.stat().st_mtime_ns

                path.write_text(
                    json.dumps(
                        {
                            "default": {"sound": "Tink", "volume": 40},
                            "rules": [{"name": "Alpha", "contains": "hello", "sound": "Hero"}],
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(path, ns=(stamp, stamp))
                self.assertEqual(source.current()["rules"][0]["sound"], "Glass")

                os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
                self.assertEqual(source.current()["rules"][0]["sound"], "Hero")
                self.assertEqual(gns.classify("", "", "hello", source.current()).sound, "Hero")

                path.write_text("{", encoding="utf-8")
                os.utime(path, ns=(stamp + 4_000_000_000, stamp + 4_000_000_000))
                self.assertEqual(source.current()["rules"][0]["sound"], "Hero")

                path.write_text(
                    json.dumps(
                        {
                            "default": {"sound": "Tink", "volume": 40},
                            "rules": [{"name": "Alpha", "regex": "(", "sound": "Basso"}],
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(path, ns=(stamp + 6_000_000_000, stamp + 6_000_000_000))
                self.assertEqual(source.current()["rules"][0]["sound"], "Hero")
                self.assertTrue(any("last good config" in message for message in handler.messages))
        finally:
            gns.LOG.removeHandler(handler)

    def test_first_invalid_config_uses_builtin_defaults(self):
        import tempfile

        handler = ListHandler()
        gns.LOG.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text("{", encoding="utf-8")
                source = gns.ConfigSource(path)
                config = source.current()
                self.assertEqual(config["rules"], [])
                self.assertEqual(config["default"]["sound"], "Tink")
                self.assertEqual(config["default"]["volume"], 40)
                self.assertTrue(any("built-in defaults" in message for message in handler.messages))
                path.write_text(
                    json.dumps(
                        {
                            "default": {"sound": "Glass", "volume": 10},
                            "rules": [{"name": "Later", "contains": "x", "sound": "Ping"}],
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
                self.assertEqual(source.current()["rules"][0]["name"], "Later")
        finally:
            gns.LOG.removeHandler(handler)

    def test_repo_has_no_email_addresses(self):
        pattern = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".md", ".json", ".jsonc", ".plist", ".sh", ".gitignore"}:
                continue
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(pattern.search(text), str(path))

    def test_program_does_not_shell_out_or_touch_keyring(self):
        source = (ROOT / "grokbot_notify_sounds.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("keyring", source)
        self.assertNotIn("security", source)
