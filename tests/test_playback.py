import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import grokbot_notify_sounds as gns
from tests.helpers import config_with

ROOT = Path(__file__).resolve().parents[1]


def at(hour, minute):
    return datetime(2026, 10, 31, hour, minute, 30)


def plan(body, config, now, cooldowns=None, title="", subtitle="", mark=False):
    return gns.plan_notification(
        title,
        subtitle,
        body,
        config,
        now=now,
        cooldowns={} if cooldowns is None else cooldowns,
        mark=mark,
    )


class VolumeTests(unittest.TestCase):
    def test_formula_and_argv(self):
        config = config_with(
            [{"name": "A", "contains": "hello", "sound": "Glass", "volume": 80, "say": True, "voice": "Zarvox"}],
            master_volume=50,
        )
        outcome = plan("hello", config, at(12, 0), title="Hi", subtitle="There")
        self.assertEqual(
            outcome.commands[0],
            ["afplay", "-v", "0.4", "/System/Library/Sounds/Glass.aiff"],
        )
        self.assertEqual(outcome.commands[1], ["say", "-v", "Zarvox", "Hi There hello"])
        self.assertEqual(gns.afplay_volume_str(25, 50), "0.125")
        self.assertEqual(gns.afplay_volume_str(100, 100), "1")
        self.assertEqual(gns.afplay_volume_str(0, 100), "0")
        self.assertEqual(gns.afplay_volume_str(100, 0), "0")

    def test_omitted_rule_volume_uses_default(self):
        config = config_with([{"name": "A", "contains": "hello", "sound": "Ping"}])
        outcome = plan("hello", config, at(12, 0))
        self.assertEqual(outcome.match.volume, 40)
        self.assertEqual(outcome.commands[0][2], "0.4")
        self.assertEqual(outcome.commands[0][3], "/System/Library/Sounds/Ping.aiff")
        self.assertEqual(len(outcome.commands), 1)

    def test_custom_paths_and_summary_limit(self):
        wav = gns.build_commands("~/Sounds/done.wav", 100, 100, False, None, "", "", "hi")
        self.assertEqual(
            wav[0],
            ["afplay", "-v", "1", str(Path.home() / "Sounds" / "done.wav")],
        )
        m4a = gns.build_commands("/tmp/bell.m4a", 100, 100, False, None, "", "", "hi")
        self.assertEqual(m4a[0], ["afplay", "-v", "1", "/tmp/bell.m4a"])
        spoken = gns.build_commands("Glass", 100, 100, True, None, "", "", "x" * 200)
        self.assertEqual(spoken[1][0], "say")
        self.assertEqual(len(spoken[1][1]), 120)
        self.assertTrue(spoken[1][1].endswith("…"))


class QuietHoursTests(unittest.TestCase):
    def _config(self, mode):
        return config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "volume": 100, "say": True}],
            quiet_hours={"enabled": True, "start": "22:00", "end": "07:00", "mode": mode},
        )

    def test_mute_overnight_window(self):
        config = self._config("mute")
        for hour, minute in ((23, 0), (6, 30), (22, 0)):
            outcome = plan("approve", config, at(hour, minute))
            self.assertEqual(outcome.result, "skip-mute", f"{hour:02d}:{minute:02d}")
            self.assertEqual(outcome.commands, [])
            self.assertEqual(outcome.match.rule_name, "Needs approval")
        for hour, minute in ((21, 59), (7, 0), (12, 0)):
            outcome = plan("approve", config, at(hour, minute))
            self.assertEqual(outcome.result, "play", f"{hour:02d}:{minute:02d}")
            self.assertEqual(outcome.commands[0][3], "/System/Library/Sounds/Sosumi.aiff")
            self.assertEqual(outcome.commands[1][0], "say")

    def test_soft_uses_default_sound_and_default_say(self):
        config = self._config("soft")
        inside = plan("approve", config, at(23, 30))
        self.assertEqual(inside.result, "play-soft")
        self.assertEqual(inside.commands[0][3], "/System/Library/Sounds/Tink.aiff")
        self.assertEqual(inside.commands[0][2], "0.4")
        self.assertEqual(len(inside.commands), 1)
        self.assertEqual(inside.effective_say, False)
        outside = plan("approve", config, at(12, 0))
        self.assertEqual(outside.commands[0][3], "/System/Library/Sounds/Sosumi.aiff")

        speaking = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "say": True}],
            default={"sound": "Tink", "volume": 40, "say": True, "voice": "Zarvox"},
            quiet_hours={"enabled": True, "start": "09:00", "end": "17:00", "mode": "soft"},
        )
        noon = plan("please approve", speaking, at(12, 0), title="Title")
        self.assertEqual(noon.result, "play-soft")
        self.assertEqual(noon.commands[0][3], "/System/Library/Sounds/Tink.aiff")
        self.assertEqual(noon.commands[1], ["say", "-v", "Zarvox", "Title please approve"])
        self.assertEqual(plan("approve", speaking, at(8, 0)).result, "play")
        self.assertEqual(plan("approve", speaking, at(17, 0)).result, "play")

    def test_daytime_window_edges(self):
        config = config_with(
            [{"name": "A", "contains": "hi", "sound": "Ping"}],
            quiet_hours={"enabled": True, "start": "09:00", "end": "17:00", "mode": "mute"},
        )
        self.assertEqual(plan("hi", config, at(9, 0)).result, "skip-mute")
        self.assertEqual(plan("hi", config, at(16, 59)).result, "skip-mute")
        self.assertEqual(plan("hi", config, at(8, 59)).result, "play")
        self.assertEqual(plan("hi", config, at(17, 0)).result, "play")


class CooldownTests(unittest.TestCase):
    def test_same_rule_is_suppressed_until_the_window_passes(self):
        config = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}],
            cooldown_seconds=10,
        )
        cooldowns = {}
        first = plan("approve", config, at(12, 0), cooldowns=cooldowns, mark=True)
        second = plan("approve again", config, at(12, 0) + timedelta(seconds=5), cooldowns=cooldowns, mark=True)
        third = plan("approve later", config, at(12, 0) + timedelta(seconds=10), cooldowns=cooldowns, mark=True)
        self.assertEqual(first.result, "play")
        self.assertEqual(second.result, "skip-cooldown")
        self.assertEqual(second.commands, [])
        self.assertEqual(third.result, "play")

    def test_skipped_play_does_not_reset_the_window(self):
        rules = [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "say": True}]
        live = config_with(
            rules,
            cooldown_seconds=10,
            quiet_hours={"enabled": True, "start": "22:00", "end": "07:00", "mode": "mute"},
        )
        cooldowns = {}
        self.assertEqual(plan("approve", live, at(12, 0), cooldowns=cooldowns, mark=True).result, "play")
        self.assertEqual(
            plan("approve", live, at(23, 0), cooldowns=cooldowns, mark=True).result,
            "skip-mute",
        )
        again = plan("approve", live, at(12, 0) + timedelta(seconds=10), cooldowns=cooldowns, mark=True)
        self.assertEqual(again.result, "play")

    def test_cooldown_is_per_rule_name(self):
        config = config_with(
            [
                {"name": "Needs approval", "contains": "approve", "sound": "Sosumi"},
                {"name": "Shipped", "contains": "shipped", "sound": "Hero"},
            ],
            cooldown_seconds=60,
        )
        cooldowns = {}
        now = at(12, 0)
        self.assertEqual(plan("approve", config, now, cooldowns=cooldowns, mark=True).result, "play")
        self.assertEqual(plan("shipped", config, now, cooldowns=cooldowns, mark=True).result, "play")
        self.assertEqual(plan("approve", config, now + timedelta(seconds=1), cooldowns=cooldowns).result, "skip-cooldown")
        fallback = config_with([], cooldown_seconds=10)
        box = {}
        self.assertEqual(plan("hello", fallback, now, cooldowns=box, mark=True).match.rule_name, "default")
        self.assertEqual(plan("again", fallback, now + timedelta(seconds=1), cooldowns=box).result, "skip-cooldown")


class ValidateTests(unittest.TestCase):
    def test_unknown_builtin_and_missing_custom_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sounds = Path(tmp) / "sounds"
            sounds.mkdir()
            (sounds / "Tink.aiff").write_bytes(b"")
            present = Path(tmp) / "bell.m4a"
            present.write_bytes(b"")
            missing = Path(tmp) / "missing.wav"
            text = json.dumps(
                {
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [
                        {"name": "Known", "contains": "a", "sound": "Tink"},
                        {"name": "Nope", "contains": "b", "sound": "Glass", "enabled": False},
                        {"name": "Missing", "contains": "c", "sound": str(missing)},
                        {"name": "Present", "bot": "Alfred", "sound": str(present)},
                    ],
                }
            )
            errors, note = gns.validation_report(text, sounds)
            self.assertIsNone(note)
            joined = "\n".join(errors)
            self.assertIn("unknown built-in sound 'Glass'", joined)
            self.assertIn("missing custom sound file", joined)
            self.assertIn(str(missing), joined)
            self.assertNotIn("Tink", joined)
            self.assertNotIn(str(present), joined)

            absent = Path(tmp) / "no-such-sounds"
            builtin_only = json.dumps({"default": {"sound": "Tink", "volume": 40}, "rules": []})
            errors, note = gns.validation_report(builtin_only, absent)
            self.assertEqual(errors, [])
            self.assertIn("Built-in sounds were not checked", note)

            custom = json.dumps(
                {
                    "default": {"sound": "Tink", "volume": 40},
                    "rules": [{"name": "Missing", "contains": "c", "sound": str(missing)}],
                }
            )
            errors, note = gns.validation_report(custom, absent)
            self.assertIn("Built-in sounds were not checked", note)
            self.assertTrue(any("missing custom sound file" in error for error in errors))

    def test_shipped_example_validates_against_a_temp_sound_dir(self):
        import tempfile

        text = (ROOT / "config.example.jsonc").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            sounds = Path(tmp)
            for name in ("Tink", "Hero", "Sosumi", "Glass"):
                (sounds / f"{name}.aiff").write_bytes(b"")
            errors, note = gns.validation_report(text, sounds)
            self.assertEqual(errors, [])
            self.assertIsNone(note)
