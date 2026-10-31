import unittest
from datetime import datetime

from tests.helpers import config_with
import grokbot_notify_sounds as gns


NOW = datetime(2026, 10, 31, 12, 0, 0)


def plan(title, subtitle, body, config, **kwargs):
    return gns.plan_notification(
        title,
        subtitle,
        body,
        config,
        now=kwargs.pop("now", NOW),
        cooldowns=kwargs.pop("cooldowns", {}),
        mark=kwargs.pop("mark", False),
    )


class ClassifyTests(unittest.TestCase):
    def test_text_only_contains_is_case_insensitive_and_ignores_subtitle(self):
        config = config_with(
            [
                {
                    "name": "Needs approval",
                    "contains": ["approve", "needs your OK"],
                    "match_in": "both",
                    "sound": "Sosumi",
                }
            ]
        )
        title_hit = gns.classify("", "", "Please APPROVE this", config)
        self.assertEqual(title_hit.rule_name, "Needs approval")
        phrase_hit = gns.classify("Needs your ok today", "", "", config)
        self.assertEqual(phrase_hit.rule_name, "Needs approval")
        subtitle_only = gns.classify("Hello", "please approve", "nothing", config)
        self.assertEqual(subtitle_only.rule_name, "default")

    def test_contains_list_matches_any_phrase_not_as_regex(self):
        config = config_with(
            [{"name": "Literal", "contains": ["a.b", "shipped"], "sound": "Ping"}]
        )
        self.assertEqual(gns.classify("", "", "we shipped it", config).rule_name, "Literal")
        self.assertEqual(gns.classify("", "", "axb", config).rule_name, "default")
        self.assertEqual(gns.classify("", "", "see a.b now", config).rule_name, "Literal")

    def test_regex_is_case_insensitive_search(self):
        config = config_with(
            [{"name": "Shipped", "regex": "shipped|delivered", "match_in": "body", "sound": "Hero"}]
        )
        self.assertEqual(gns.classify("", "", "We SHIPPED it", config).rule_name, "Shipped")
        self.assertEqual(gns.classify("shipped", "", "hello", config).rule_name, "default")

    def test_contains_and_regex_both_required(self):
        config = config_with(
            [
                {
                    "name": "Both",
                    "contains": "approve",
                    "regex": "now",
                    "sound": "Ping",
                }
            ]
        )
        self.assertEqual(gns.classify("", "", "approve now", config).rule_name, "Both")
        self.assertEqual(gns.classify("", "", "approve", config).rule_name, "default")
        self.assertEqual(gns.classify("", "", "now", config).rule_name, "default")

    def test_match_in_title_versus_body(self):
        config = config_with(
            [
                {"name": "In title", "contains": "approve", "match_in": "title", "sound": "Ping"},
                {"name": "In body", "contains": "approve", "match_in": "body", "sound": "Pop"},
            ]
        )
        self.assertEqual(gns.classify("Please approve", "", "nope", config).rule_name, "In title")
        self.assertEqual(gns.classify("nope", "", "please approve", config).rule_name, "In body")
        self.assertEqual(gns.classify("nope", "approve", "nope", config).rule_name, "default")

    def test_first_match_wins(self):
        config = config_with(
            [
                {"name": "First", "contains": "shipped", "sound": "Glass"},
                {"name": "Second", "contains": "shipped", "sound": "Hero"},
            ]
        )
        match = gns.classify("", "", "shipped", config)
        self.assertEqual(match.rule_name, "First")
        self.assertEqual(match.sound, "Glass")

    def test_disabled_rule_is_skipped(self):
        config = config_with(
            [
                {"name": "Off", "contains": "shipped", "sound": "Basso", "enabled": False},
                {"name": "On", "contains": "shipped", "sound": "Glass"},
            ]
        )
        self.assertEqual(gns.classify("", "", "shipped", config).rule_name, "On")
        lines = gns.format_rule_lines(config)
        self.assertTrue(lines[0].startswith("1 "))
        self.assertIn("name=Off", lines[0])
        self.assertIn("sound=Basso", lines[0])
        self.assertIn("enabled=false", lines[0])
        self.assertIn("name=On", lines[1])
        self.assertIn("sound=Glass", lines[1])

    def test_default_when_nothing_matches(self):
        config = config_with(
            [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi"}]
        )
        match = gns.classify("Hello", "there", "all done", config)
        self.assertEqual(match.rule_name, "default")
        self.assertEqual(match.sound, "Tink")
        self.assertEqual(match.volume, 40)
        self.assertFalse(match.say)

    def test_bot_only_matches_title_or_subtitle_not_body(self):
        config = config_with(
            [{"name": "Helper answers", "bot": "Helper", "sound": "Glass", "volume": 80}]
        )
        self.assertEqual(gns.classify("Hello HELPER", "", "x", config).rule_name, "Helper answers")
        self.assertEqual(gns.classify("Hello", "helper here", "x", config).rule_name, "Helper answers")
        self.assertEqual(gns.classify("Hello", "", "Helper wrote this", config).rule_name, "default")
        self.assertEqual(
            gns.classify("Hello", "", "body", config, sender="Helper").rule_name,
            "Helper answers",
        )
        self.assertEqual(gns.classify("Hello HELPER", "", "x", config).volume, 80)

    def test_bot_and_text_are_anded(self):
        config = config_with(
            [
                {
                    "name": "Helper shipped",
                    "bot": "Helper",
                    "regex": "shipped|delivered",
                    "match_in": "body",
                    "sound": "Hero",
                    "volume": 60,
                    "say": True,
                    "voice": "Zarvox",
                },
                {"name": "Needs approval", "contains": "approve", "sound": "Sosumi"},
            ]
        )
        hit = gns.classify("Helper", "", "we shipped it", config)
        self.assertEqual(hit.rule_name, "Helper shipped")
        self.assertTrue(hit.say)
        self.assertEqual(hit.voice, "Zarvox")
        self.assertEqual(gns.classify("Note from Helper", "", "hello", config).rule_name, "default")
        self.assertEqual(gns.classify("Someone", "", "shipped", config).rule_name, "default")
        self.assertEqual(
            gns.classify("Someone", "helper", "delivered today", config).rule_name,
            "Helper shipped",
        )
        self.assertEqual(
            gns.classify("Helper", "", "please approve", config).rule_name,
            "Needs approval",
        )

    def test_non_matching_bot_falls_through(self):
        config = config_with(
            [
                {
                    "name": "Helper shipped",
                    "bot": "Helper",
                    "contains": "shipped",
                    "sound": "Hero",
                },
                {"name": "Needs approval", "contains": "approve", "sound": "Sosumi"},
            ]
        )
        self.assertEqual(gns.classify("Someone", "", "please approve", config).rule_name, "Needs approval")

    def test_category_does_not_change_order(self):
        config = config_with(
            [
                {"name": "Second label", "category": "zzz", "contains": "ping", "sound": "Ping"},
                {"name": "First label", "category": "aaa", "contains": "ping", "sound": "Glass"},
            ]
        )
        self.assertEqual(gns.classify("", "", "ping", config).rule_name, "Second label")


class PlanTests(unittest.TestCase):
    def test_disabled_config_skips_without_marking_cooldown(self):
        rules = [{"name": "Needs approval", "contains": "approve", "sound": "Sosumi", "say": True}]
        off = config_with(rules, enabled=False)
        on = config_with(rules)
        cooldowns = {}
        outcome = plan("t", "s", "approve", off, cooldowns=cooldowns, mark=True)
        self.assertEqual(outcome.result, "skip-disabled")
        self.assertEqual(outcome.commands, [])
        self.assertEqual(cooldowns, {})
        played = plan("t", "s", "approve", on, cooldowns=cooldowns, mark=True)
        self.assertEqual(played.result, "play")
        self.assertIn("Needs approval", cooldowns)
