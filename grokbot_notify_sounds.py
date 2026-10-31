#!/usr/bin/env python3
"""Play a macOS alert when a configured app posts a notification.

Stdlib only. Reads the Notification Center database in read-only mode and
never uses the network or the Keychain.
"""

from __future__ import annotations

import os
import sys

_DRY_ENV_KEYS = ("GROKBOT_SOUNDS_DRY_RUN", "GROKBOT_SOUNDS_PLAYER")


def _sounds_blocked_by_env() -> bool:
    """Either flag means: do not exec afplay, say, or launchctl."""
    return os.environ.get("GROKBOT_SOUNDS_DRY_RUN") == "1" or os.environ.get("GROKBOT_SOUNDS_PLAYER") == "fake"


def _forward_sound_env(base: dict | None = None) -> dict:
    """Copy the dry-run flags into a child environment."""
    env = os.environ.copy() if base is None else dict(base)
    for key in _DRY_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _should_fast_notify(argv: list[str]) -> bool:
    """Parent --notify returns before the heavy imports and before say -v '?'."""
    if "--notify-worker" in argv or "--drain-notify" in argv:
        return False
    return "--notify" in argv


def _notify_bot_argument(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--bot" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--bot="):
            return arg.split("=", 1)[1]
    return None


def _fast_notify_main(argv: list[str]) -> int:
    """Minimum argv parse, spawn the worker, exit. Does not import the player."""
    import subprocess

    if not _notify_bot_argument(argv):
        print("--notify requires --bot NAME.", file=sys.stderr)
        return 2
    forwarded: list[str] = []
    args = list(argv)
    if args and not str(args[0]).startswith("-"):
        args = args[1:]
    for arg in args:
        if arg == "--notify":
            continue
        forwarded.append(arg)
    command = [sys.executable, os.path.abspath(__file__), "--notify-worker", *forwarded]
    proc = subprocess.Popen(
        command,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_forward_sound_env(),
    )
    _record_child_pid(proc.pid)
    return 0


def _record_child_pid(pid: int) -> None:
    """Append a detached child's pid to $GROKBOT_NOTIFY_CHILD_PIDS so tests can reap it."""
    target = os.environ.get("GROKBOT_NOTIFY_CHILD_PIDS", "")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{int(pid)}\n")
    except OSError:
        pass


if __name__ == "__main__" and _should_fast_notify(sys.argv):
    sys.exit(_fast_notify_main(sys.argv))

import argparse
import fcntl
import json
import logging
import logging.handlers
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("grokbot_notify_sounds")
LOG.addHandler(logging.NullHandler())
LOG.setLevel(logging.INFO)
LOG.propagate = False

SYSTEM_SOUNDS_DIR = Path("/System/Library/Sounds")
CUSTOM_EXTENSIONS = (".aiff", ".mp3", ".wav", ".m4a")
SUMMARY_LIMIT = 120
SAY_TEXT_LIMIT = 300
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUPS = 3
LOCK_NAME = "playback.lock"
_SAY_FIELDS = re.compile(r"\{(bot|title|body)\}")
_STATE_LOCK = threading.Lock()

BUILTIN_SOUND_LINES = (
    "Basso: deep downward thud",
    "Blow: short breathy whoosh",
    "Bottle: hollow bottle pop",
    "Frog: short croak",
    "Funk: muted bass hit",
    "Glass: bright glassy chime",
    "Hero: short rising fanfare",
    "Morse: quick telegraph beep",
    "Ping: soft high ping",
    "Pop: sharp cork pop",
    "Purr: soft low purr",
    "Sosumi: four-note chime",
    "Submarine: sonar ping",
    "Tink: short metal tink",
)


class ConfigError(Exception):
    """The config text or object failed validation."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def default_config_path() -> Path:
    return Path.home() / ".config" / "grokbot-sounds" / "config.json"


def default_state_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "GrokBotNotifySounds" / "state.json"


def default_log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "grokbot-notify-sounds.log"


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas. Strings are preserved."""
    if text.startswith("\ufeff"):
        text = text[1:]
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = n if i + 1 >= n else i + 2
            continue
        out.append(char)
        i += 1
    return _strip_trailing_commas("".join(out))


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(char)
        i += 1
    return "".join(out)


def drop_comments(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: drop_comments(item)
            for key, item in value.items()
            if key != "_comment"
        }
    if isinstance(value, list):
        return [drop_comments(item) for item in value]
    return value


def _is_finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _is_volume(value: object) -> bool:
    return type(value) is int and 0 <= value <= 100


def classify_sound(sound: str) -> tuple[str, str]:
    """Return ('builtin', stem) or ('file', absolute path)."""
    if not isinstance(sound, str):
        raise ValueError("sound must be a string")
    raw = sound.strip()
    if not raw:
        raise ValueError("sound must not be empty")
    expanded = os.path.expanduser(raw)
    suffix = Path(expanded).suffix.lower()
    is_custom = raw.startswith("~") or Path(expanded).is_absolute()
    if is_custom:
        if suffix not in CUSTOM_EXTENSIONS:
            raise ValueError(
                f"custom sound {raw!r} must end with .aiff, .mp3, .wav, or .m4a"
            )
        return "file", str(Path(expanded))
    if "/" in raw or "\\" in raw or suffix in CUSTOM_EXTENSIONS:
        raise ValueError(
            f"sound {raw!r} must be a built-in name or an absolute or ~ path"
        )
    if raw in {".", ".."}:
        raise ValueError(f"invalid built-in sound {raw!r}")
    return "builtin", raw


def afplay_volume_str(volume: int, master: int) -> str:
    """Format (volume/100) * (master/100) for afplay -v without binary float noise."""
    product = int(volume) * int(master)
    whole, frac = divmod(product, 10000)
    if frac == 0:
        return str(whole)
    digits = f"{frac:04d}".rstrip("0")
    return f"{whole}.{digits}"


def _quiet_defaults() -> dict:
    return {"enabled": False, "start": "22:00", "end": "07:00", "mode": "mute"}


def _parse_hhmm(value: object, label: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value.strip()):
        errors.append(f"{label} must be HH:MM")
        return "00:00"
    hour_text, minute_text = value.strip().split(":")
    hour = int(hour_text)
    minute = int(minute_text)
    if hour > 23 or minute > 59:
        errors.append(f"{label} must be a real time")
        return "00:00"
    return f"{hour:02d}:{minute:02d}"


def _normalize_quiet(raw: object, errors: list[str]) -> dict:
    quiet = _quiet_defaults()
    if raw is None:
        return quiet
    if not isinstance(raw, dict):
        errors.append("quiet_hours must be an object")
        return quiet
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        errors.append("quiet_hours.enabled must be a boolean")
    else:
        quiet["enabled"] = bool(raw.get("enabled", False))
    if "start" in raw:
        quiet["start"] = _parse_hhmm(raw["start"], "quiet_hours.start", errors)
    elif quiet["enabled"]:
        errors.append("quiet_hours.start is required when quiet hours are enabled")
    if "end" in raw:
        quiet["end"] = _parse_hhmm(raw["end"], "quiet_hours.end", errors)
    elif quiet["enabled"]:
        errors.append("quiet_hours.end is required when quiet hours are enabled")
    if "mode" in raw:
        if raw["mode"] not in ("mute", "soft"):
            errors.append("quiet_hours.mode must be 'mute' or 'soft'")
        else:
            quiet["mode"] = raw["mode"]
    elif quiet["enabled"]:
        errors.append("quiet_hours.mode is required when quiet hours are enabled")
    return quiet


def _normalize_default(raw: object, errors: list[str]) -> dict:
    default = {"sound": "Tink", "volume": 100, "say": False, "voice": None}
    if raw is None:
        return default
    if not isinstance(raw, dict):
        errors.append("default must be an object")
        return default
    if "sound" in raw:
        if not isinstance(raw["sound"], str) or not raw["sound"].strip():
            errors.append("default.sound must be a non-empty string")
        else:
            try:
                classify_sound(raw["sound"])
            except ValueError as exc:
                errors.append(f"default.sound: {exc}")
            else:
                default["sound"] = raw["sound"].strip()
    if "volume" in raw:
        if not _is_volume(raw["volume"]):
            errors.append("default.volume must be an integer from 0 to 100")
        else:
            default["volume"] = raw["volume"]
    if "say" in raw:
        if not isinstance(raw["say"], bool):
            errors.append("default.say must be a boolean")
        else:
            default["say"] = raw["say"]
    if "voice" in raw and raw["voice"] is not None:
        if not isinstance(raw["voice"], str):
            errors.append("default.voice must be a string or null")
        else:
            voice = raw["voice"].strip()
            default["voice"] = voice or None
    return default


def _require_int(value: object, label: str, minimum: int, errors: list[str], fallback: int) -> int:
    if type(value) is not int or value < minimum:
        errors.append(f"{label} must be an integer >= {minimum}")
        return fallback
    return value


def _normalize_contains(value: object, label: str, errors: list[str]) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        phrases = [value]
    elif isinstance(value, list):
        phrases = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                errors.append(f"{label} contains[{index}] must be a string")
            else:
                phrases.append(item)
    else:
        errors.append(f"{label} contains must be a string or a list of strings")
        return None
    cleaned = [phrase.strip() for phrase in phrases if isinstance(phrase, str) and phrase.strip()]
    return cleaned or None


def _normalize_rule(item: dict, index: int, errors: list[str]) -> dict | None:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"rules[{index}].name must be a non-empty string")
        label = f"rules[{index}]"
        name_text = ""
    else:
        name_text = name.strip()
        label = f"Rule {name_text!r}"

    bot = None
    if "bot" in item and item["bot"] is not None:
        if not isinstance(item["bot"], str):
            errors.append(f"{label} bot must be a string")
        else:
            bot = item["bot"].strip() or None

    contains = None
    if "contains" in item:
        contains = _normalize_contains(item["contains"], label, errors)

    regex = None
    if "regex" in item and item["regex"] is not None:
        if not isinstance(item["regex"], str):
            errors.append(f"{label} regex must be a string")
        else:
            regex = item["regex"].strip() or None
            if regex is not None:
                try:
                    re.compile(regex)
                except re.error as exc:
                    errors.append(f"{label} has an invalid regex: {exc}")

    if bot is None and contains is None and regex is None:
        errors.append(f"{label} needs a bot, contains, or regex")

    match_in = item.get("match_in", "both")
    if match_in not in ("title", "body", "both"):
        errors.append(f"{label} match_in must be 'title', 'body', or 'both'")
        match_in = "both"

    sound = ""
    if "sound" not in item:
        errors.append(f"{label} needs a sound")
    elif not isinstance(item["sound"], str) or not item["sound"].strip():
        errors.append(f"{label} sound must be a non-empty string")
    else:
        try:
            classify_sound(item["sound"])
        except ValueError as exc:
            errors.append(f"{label} sound: {exc}")
        else:
            sound = item["sound"].strip()

    volume = None
    if "volume" in item and item["volume"] is not None:
        if not _is_volume(item["volume"]):
            errors.append(f"{label} volume must be an integer from 0 to 100")
        else:
            volume = item["volume"]

    say = False
    if "say" in item:
        if not isinstance(item["say"], bool):
            errors.append(f"{label} say must be a boolean")
        else:
            say = item["say"]

    voice = None
    if "voice" in item and item["voice"] is not None:
        if not isinstance(item["voice"], str):
            errors.append(f"{label} voice must be a string or null")
        else:
            voice = item["voice"].strip() or None

    say_text = None
    if "say_text" in item and item["say_text"] is not None:
        if not isinstance(item["say_text"], str):
            errors.append(f"{label} say_text must be a string")
        else:
            say_text = item["say_text"]

    enabled = True
    if "enabled" in item:
        if not isinstance(item["enabled"], bool):
            errors.append(f"{label} enabled must be a boolean")
        else:
            enabled = item["enabled"]

    category = None
    if "category" in item and item["category"] is not None:
        if not isinstance(item["category"], str):
            errors.append(f"{label} category must be a string")
        else:
            category = item["category"].strip() or None

    if not name_text or not sound:
        return None
    return {
        "name": name_text,
        "bot": bot,
        "contains": contains,
        "regex": regex,
        "match_in": match_in,
        "sound": sound,
        "volume": volume,
        "say": say,
        "say_text": say_text,
        "voice": voice,
        "enabled": enabled,
        "category": category,
    }


def normalize_config(data: object) -> dict:
    if not isinstance(data, dict):
        raise ConfigError(["Config must be a JSON object"])
    data = drop_comments(data)
    assert isinstance(data, dict)
    errors: list[str] = []

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append("enabled must be a boolean")
        enabled = True

    paused = data.get("paused", False)
    if not isinstance(paused, bool):
        errors.append("paused must be a boolean")
        paused = False

    poll = data.get("poll_interval_seconds", 2)
    if not _is_finite_number(poll) or poll <= 0:
        errors.append("poll_interval_seconds must be a number greater than 0")
        poll = 2.0
    else:
        poll = float(poll)

    bundle_id = data.get("bundle_id", "")
    if not isinstance(bundle_id, str):
        errors.append("bundle_id must be a string")
        bundle_id = ""

    master = 100
    if "master_volume" in data:
        if not _is_volume(data["master_volume"]):
            errors.append("master_volume must be an integer from 0 to 100")
        else:
            master = data["master_volume"]

    cooldown = 0.0
    if "cooldown_seconds" in data:
        raw_cooldown = data["cooldown_seconds"]
        if not _is_finite_number(raw_cooldown) or raw_cooldown < 0:
            errors.append("cooldown_seconds must be a number >= 0")
        else:
            cooldown = float(raw_cooldown)

    burst_threshold = 3
    if "burst_threshold" in data:
        burst_threshold = _require_int(data["burst_threshold"], "burst_threshold", 0, errors, 3)
    burst_window = 5.0
    if "burst_window_seconds" in data:
        raw_window = data["burst_window_seconds"]
        if not _is_finite_number(raw_window) or raw_window < 0:
            errors.append("burst_window_seconds must be a number >= 0")
        else:
            burst_window = float(raw_window)
    dedupe_window = None
    if "dedupe_window_seconds" in data:
        raw_dedupe = data["dedupe_window_seconds"]
        if not _is_finite_number(raw_dedupe) or raw_dedupe < 0:
            errors.append("dedupe_window_seconds must be a number >= 0")
        else:
            dedupe_window = float(raw_dedupe)
    queue_max = 20
    if "queue_max" in data:
        queue_max = _require_int(data["queue_max"], "queue_max", 1, errors, 20)

    mode = data.get("mode", "watch")
    if mode not in ("watch", "notify"):
        errors.append("mode must be 'watch' or 'notify'")
        mode = "watch"

    notify_bots: list[str] = []
    if "notify_bots" in data and data["notify_bots"] is not None:
        raw_bots = data["notify_bots"]
        if not isinstance(raw_bots, list) or any(not isinstance(item, str) for item in raw_bots):
            errors.append("notify_bots must be a list of strings")
        else:
            notify_bots = [item.strip() for item in raw_bots if item.strip()]

    quiet = _normalize_quiet(data.get("quiet_hours"), errors)
    default = _normalize_default(data.get("default"), errors)

    rules_raw = data.get("rules", [])
    rules: list[dict] = []
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        errors.append("rules must be a list")
    else:
        seen: set[str] = set()
        for index, item in enumerate(rules_raw):
            if not isinstance(item, dict):
                errors.append(f"rules[{index}] must be an object")
                continue
            rule = _normalize_rule(item, index, errors)
            if rule is None:
                continue
            if rule["name"] in seen:
                errors.append(f"Duplicate rule name {rule['name']!r}")
            seen.add(rule["name"])
            rules.append(rule)

    if errors:
        raise ConfigError(errors)
    normalized = {
        "enabled": enabled,
        "paused": paused,
        "poll_interval_seconds": poll,
        "bundle_id": bundle_id,
        "master_volume": master,
        "cooldown_seconds": cooldown,
        "burst_threshold": burst_threshold,
        "burst_window_seconds": burst_window,
        "queue_max": queue_max,
        "mode": mode,
        "notify_bots": notify_bots,
        "quiet_hours": quiet,
        "default": default,
        "rules": rules,
    }
    if dedupe_window is not None:
        normalized["dedupe_window_seconds"] = dedupe_window
    return normalized


def builtin_defaults() -> dict:
    """Same behavior as the shipped config.json, plus quiet hours off and no cooldown."""
    return normalize_config(
        {
            "enabled": True,
            "poll_interval_seconds": 2,
            "bundle_id": "",
            "master_volume": 100,
            "default": {"sound": "Tink", "volume": 40},
            "rules": [],
        }
    )


def parse_config_text(text: str) -> dict:
    try:
        data = json.loads(strip_jsonc(text))
    except json.JSONDecodeError as exc:
        raise ConfigError([f"Invalid JSON: {exc.msg} (line {exc.lineno})"]) from exc
    return normalize_config(data)


def load_config(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"Cannot read config: {exc}"]) from exc
    return parse_config_text(text)


class ConfigSource:
    """Reload config when the file mtime changes. Keep the last good copy."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.config: dict | None = None
        self._seen_mtime: int | None = None
        self._reported_missing = False

    def current(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError as exc:
            if not self._reported_missing:
                if self.config is None:
                    LOG.error(
                        "Config file %s is missing (%s). Using built-in defaults.",
                        self.path,
                        exc,
                    )
                else:
                    LOG.error(
                        "Config file %s is missing (%s). Keeping the last good config.",
                        self.path,
                        exc,
                    )
                self._reported_missing = True
            if self.config is None:
                self.config = builtin_defaults()
            return self.config

        self._reported_missing = False
        if self._seen_mtime == mtime and self.config is not None:
            return self.config
        try:
            loaded = load_config(self.path)
        except ConfigError as exc:
            if self.config is None:
                LOG.error(
                    "Config file %s is invalid (%s). Using built-in defaults.",
                    self.path,
                    exc,
                )
                self.config = builtin_defaults()
            else:
                LOG.error("config invalid, keeping last good: %s", exc)
            self._seen_mtime = mtime
            return self.config
        self.config = loaded
        self._seen_mtime = mtime
        LOG.info("config reloaded (%s rules)", len(loaded["rules"]))
        return self.config


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def quiet_active(now: datetime, quiet: dict) -> bool:
    if not quiet.get("enabled"):
        return False
    start = _minutes(quiet["start"])
    end = _minutes(quiet["end"])
    current = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _haystack(title: str, body: str, match_in: str) -> str:
    if match_in == "title":
        return title
    if match_in == "body":
        return body
    return f"{title}\n{body}"


def _rule_matches(rule: dict, title: str, subtitle: str, body: str, sender: str | None = None) -> bool:
    if not rule["enabled"]:
        return False
    if rule["bot"] is not None:
        needle = rule["bot"].casefold()
        fields = [title.casefold(), subtitle.casefold()]
        if sender:
            fields.append(sender.casefold())
        if not any(needle in field for field in fields):
            return False
    if rule["contains"] is None and rule["regex"] is None:
        return True
    haystack = _haystack(title, body, rule["match_in"])
    if rule["contains"] is not None:
        folded = haystack.casefold()
        if not any(phrase.casefold() in folded for phrase in rule["contains"]):
            return False
    if rule["regex"] is not None:
        if re.search(rule["regex"], haystack, re.IGNORECASE) is None:
            return False
    return True


def _resolved_volume(rule_volume: int | None, config: dict) -> int:
    if rule_volume is not None:
        return rule_volume
    return int(config["default"]["volume"])


class Match:
    def __init__(
        self,
        rule_name: str,
        sound: str,
        volume: int,
        say: bool,
        voice: str | None,
        say_text: str | None = None,
        bot: str | None = None,
    ):
        self.rule_name = rule_name
        self.sound = sound
        self.volume = volume
        self.say = say
        self.voice = voice
        self.say_text = say_text
        self.bot = bot


class Outcome:
    def __init__(
        self,
        match: Match,
        result: str,
        commands: list[list[str]],
        master_volume: int,
        effective_sound: str | None = None,
        effective_volume: int | None = None,
        effective_say: bool | None = None,
    ):
        self.match = match
        self.result = result
        self.commands = commands
        self.master_volume = master_volume
        self.effective_sound = effective_sound
        self.effective_volume = effective_volume
        self.effective_say = effective_say


def classify(title: str, subtitle: str, body: str, config: dict, sender: str | None = None) -> Match:
    for rule in config["rules"]:
        if _rule_matches(rule, title, subtitle, body, sender):
            return Match(
                rule["name"],
                rule["sound"],
                _resolved_volume(rule["volume"], config),
                bool(rule["say"]),
                rule["voice"],
                rule.get("say_text"),
                rule.get("bot"),
            )
    default = config["default"]
    return Match(
        "default",
        default["sound"],
        int(default["volume"]),
        bool(default["say"]),
        default["voice"],
    )


def clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def summary_text(title: str, subtitle: str, body: str, limit: int = SUMMARY_LIMIT) -> str:
    parts = []
    for part in (title, subtitle, body):
        text = (part or "").strip()
        if text:
            parts.append(text)
    return clip_text(" ".join(parts), limit)


def render_say_text(template: str, bot: str, title: str, body: str) -> str:
    values = {"bot": bot or "", "title": title or "", "body": body or ""}
    spoken = _SAY_FIELDS.sub(lambda match: values[match.group(1)], template)
    return clip_text(spoken, SAY_TEXT_LIMIT)


def build_commands(
    sound: str,
    volume: int,
    master: int,
    say: bool,
    voice: str | None,
    title: str,
    subtitle: str,
    body: str,
    say_text: str | None = None,
    bot: str | None = None,
) -> list[list[str]]:
    kind, ident = classify_sound(sound)
    if kind == "builtin":
        path = str(SYSTEM_SOUNDS_DIR / f"{ident}.aiff")
    else:
        path = ident
    commands = [["afplay", "-v", afplay_volume_str(volume, master), path]]
    if not say:
        return commands
    if say_text is not None:
        spoken = render_say_text(say_text, bot or "", title, body)
    else:
        spoken = summary_text(title, subtitle, body)
    if not spoken:
        return commands
    chosen = playback_voice_name(voice)
    if chosen:
        commands.append(["say", "-v", chosen, spoken])
    else:
        commands.append(["say", spoken])
    return commands


def plan_notification(
    title: str,
    subtitle: str,
    body: str,
    config: dict,
    *,
    now: datetime,
    cooldowns: dict[str, datetime],
    mark: bool = False,
    sender: str | None = None,
) -> Outcome:
    match = classify(title, subtitle, body, config, sender)
    master = int(config["master_volume"])

    def skipped(result: str) -> Outcome:
        return Outcome(match, result, [], master)

    if not config["enabled"]:
        return skipped("skip-disabled")
    if config.get("paused"):
        return skipped("skip-paused")

    quiet = config["quiet_hours"]
    sound = match.sound
    volume = match.volume
    say = match.say
    voice = match.voice
    result = "play"
    effective = False
    if quiet_active(now, quiet):
        if quiet["mode"] == "mute":
            return skipped("skip-mute")
        default = config["default"]
        sound = default["sound"]
        volume = int(default["volume"])
        say = bool(default["say"])
        voice = default["voice"]
        result = "play-soft"
        effective = True

    window = float(config["cooldown_seconds"])
    previous = cooldowns.get(match.rule_name)
    if window > 0 and previous is not None:
        if (now - previous).total_seconds() < window:
            return skipped("skip-cooldown")

    template = None if result == "play-soft" else match.say_text
    commands = build_commands(
        sound,
        volume,
        master,
        say,
        voice,
        title,
        subtitle,
        body,
        say_text=template,
        bot=match.bot,
    )
    if mark:
        cooldowns[match.rule_name] = now
    return Outcome(
        match,
        result,
        commands,
        master,
        effective_sound=sound if effective else None,
        effective_volume=volume if effective else None,
        effective_say=say if effective else None,
    )


def render_test(outcome: Outcome) -> str:
    match = outcome.match
    sound = outcome.effective_sound or match.sound
    volume = outcome.effective_volume if outcome.effective_volume is not None else match.volume
    lines = [
        f"rule: {match.rule_name}",
        f"sound: {sound}",
        f"volume: {volume}",
        f"afplay_volume: {afplay_volume_str(volume, outcome.master_volume)}",
        f"say: {'yes' if outcome.effective_say else 'no'}" if outcome.effective_say is not None else f"say: {'yes' if match.say else 'no'}",
    ]
    if match.voice:
        lines.append(f"voice: {match.voice}")
    if outcome.result == "soft":
        lines.append("result=soft")
    else:
        lines.append(f"result: {outcome.result}")
    if outcome.result in ("play-soft", "soft"):
        lines.append(f"effective_sound: {outcome.effective_sound}")
        lines.append(f"effective_volume: {outcome.effective_volume}")
        lines.append(f"effective_say: {'yes' if outcome.effective_say else 'no'}")
    return "\n".join(lines) + "\n"


def _dash(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def format_rule_line(index: int, rule: dict, config: dict) -> str:
    pieces = []
    if rule.get("contains"):
        pieces.append("contains:" + "|".join(rule["contains"]))
    if rule.get("regex"):
        pieces.append("regex:" + rule["regex"])
    match = "+".join(pieces) if pieces else "-"
    volume = _resolved_volume(rule.get("volume"), config)
    return (
        f"{index} name={rule['name']} bot={_dash(rule.get('bot'))} match={match} "
        f"match_in={rule['match_in']} sound={rule['sound']} volume={volume} "
        f"enabled={_format_bool(bool(rule['enabled']))} say={_format_bool(bool(rule['say']))} "
        f"say_text={_dash(rule.get('say_text'))} voice={_dash(rule.get('voice'))}"
    )


def format_rule_lines(config: dict) -> list[str]:
    return [format_rule_line(index, rule, config) for index, rule in enumerate(config["rules"], start=1)]


def iter_sounds(config: dict):
    yield "default", config["default"]["sound"]
    for rule in config["rules"]:
        yield rule["name"], rule["sound"]


def sound_report(config: dict, sounds_dir: Path) -> tuple[list[str], str | None]:
    check_builtin = sounds_dir.is_dir()
    note = None
    if not check_builtin:
        note = f"Built-in sounds were not checked because {sounds_dir} is missing."
    errors: list[str] = []
    for label, sound in iter_sounds(config):
        try:
            kind, ident = classify_sound(sound)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if kind == "file":
            if not Path(ident).is_file():
                errors.append(f"{label}: missing custom sound file {ident}")
        elif check_builtin:
            candidate = sounds_dir / f"{ident}.aiff"
            if not candidate.is_file():
                errors.append(f"{label}: unknown built-in sound {ident!r}")
    return errors, note


def validation_report(
    text: str,
    sounds_dir: Path | None = None,
    voice_listing: str | None = None,
) -> tuple[list[str], str | None]:
    try:
        config = parse_config_text(text)
    except ConfigError as exc:
        return list(exc.errors), None
    directory = active_sounds_dir() if sounds_dir is None else Path(sounds_dir)
    errors, note = sound_report(config, directory)
    if not configured_voices(config):
        return errors, note
    if voice_listing is None:
        voices = load_voices()
    else:
        voices = parse_voices(voice_listing)
    errors.extend(voice_errors(config, voices))
    return errors, note


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def decode_notification(data: object) -> tuple[str, str, str]:
    if isinstance(data, memoryview):
        data = data.tobytes()
    elif isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, (bytes,)):
        raise ValueError("notification payload is not a binary plist")
    obj = plistlib.loads(data)
    req = obj.get("req") if isinstance(obj, dict) else None
    if not isinstance(req, dict):
        req = {}
    return _as_text(req.get("titl")), _as_text(req.get("subt")), _as_text(req.get("body"))


def notification_db_candidates() -> list[Path]:
    candidates = [
        Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"
    ]
    darwin = _darwin_user_dir()
    if darwin is not None:
        candidates.append(darwin / "com.apple.notificationcenter" / "db2" / "db")
    return candidates


def _darwin_user_dir() -> Path | None:
    try:
        result = subprocess.run(
            ["getconf", "DARWIN_USER_DIR"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    if not value:
        return None
    return Path(value)


def resolve_db_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    candidates = notification_db_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def list_bundle_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT identifier
        FROM app
        WHERE identifier IS NOT NULL AND identifier != ''
        """
    ).fetchall()
    return sorted({row[0] for row in rows if isinstance(row[0], str)})


def _max_rec_id(conn: sqlite3.Connection, bundle_id: str) -> int:
    if not bundle_id:
        return 0
    row = conn.execute(
        """
        SELECT MAX(record.rec_id)
        FROM record
        JOIN app ON record.app_id = app.app_id
        WHERE app.identifier = ?
        """,
        (bundle_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _new_records(conn: sqlite3.Connection, bundle_id: str, last_seen: int) -> list[tuple[int, object]]:
    if not bundle_id:
        return []
    rows = conn.execute(
        """
        SELECT record.rec_id, record.data
        FROM record
        JOIN app ON record.app_id = app.app_id
        WHERE app.identifier = ?
          AND record.rec_id > ?
        ORDER BY record.rec_id ASC
        """,
        (bundle_id, last_seen),
    ).fetchall()
    return [(int(row[0]), row[1]) for row in rows]


def _read_state_document(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state_document(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(data) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_state(path: Path) -> int | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data["last_seen"]
        if type(value) is not int:
            raise ValueError("last_seen must be an integer")
        return value
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        LOG.error("State file %s is unreadable (%s). Treating this as a first run.", path, exc)
        return None


def load_cooldowns(path: Path) -> dict[str, datetime]:
    raw = _read_state_document(path).get("cooldowns")
    found: dict[str, datetime] = {}
    if not isinstance(raw, dict):
        return found
    for name, stamp in raw.items():
        if isinstance(name, str) and type(stamp) in (int, float):
            found[name] = datetime.fromtimestamp(float(stamp))
    return found


def save_state(path: Path, last_seen: int) -> None:
    path = Path(path)
    with _STATE_LOCK:
        data = _read_state_document(path)
        data["last_seen"] = int(last_seen)
        if not isinstance(data.get("cooldowns"), dict):
            data["cooldowns"] = {}
        _write_state_document(path, data)


def save_cooldown(path: Path, name: str, when: datetime) -> None:
    path = Path(path)
    with _STATE_LOCK:
        data = _read_state_document(path)
        cool = data.get("cooldowns")
        if not isinstance(cool, dict):
            cool = {}
        cool[str(name)] = when.timestamp()
        data["cooldowns"] = cool
        _write_state_document(path, data)


def dedupe_window_seconds(config: dict) -> float:
    """Absent key follows the burst window. Zero turns dedupe off."""
    if "dedupe_window_seconds" in config:
        return float(config["dedupe_window_seconds"])
    return float(config.get("burst_window_seconds", 5))


def load_dedupe(path: Path) -> list[dict]:
    raw = _read_state_document(path).get("dedupe")
    found = []
    if not isinstance(raw, list):
        return found
    for item in raw:
        if not isinstance(item, dict):
            continue
        rule = item.get("rule")
        bot = item.get("bot")
        at = item.get("at")
        if isinstance(rule, str) and isinstance(bot, str) and type(at) in (int, float):
            found.append({"rule": rule, "bot": bot, "at": float(at)})
    return found


def save_dedupe_play(path: Path, rule: str, bot: str, when: float) -> None:
    path = Path(path)
    with _STATE_LOCK:
        data = _read_state_document(path)
        rows = []
        for item in data.get("dedupe") if isinstance(data.get("dedupe"), list) else []:
            if isinstance(item, dict) and item.get("rule") == rule and item.get("bot") == bot:
                continue
            rows.append(item)
        rows.append({"rule": rule, "bot": bot, "at": float(when)})
        data["dedupe"] = rows
        _write_state_document(path, data)


def is_deduped(path: Path, rule: str, bot: str, now: float, window: float) -> bool:
    if window <= 0 or not bot:
        return False
    for row in load_dedupe(path):
        if row["rule"] == rule and row["bot"] == bot and (now - row["at"]) < window:
            return True
    return False


class PlaybackError(Exception):
    def __init__(self, code: int):
        self.code = int(code)
        super().__init__(f"playback failed (exit {self.code})")


PLAYER_BIN_ENV = "GROKBOT_NOTIFY_BIN_DIR"


def player_tool(name: str) -> str:
    """Resolve afplay or say. $GROKBOT_NOTIFY_BIN_DIR forces fakes from that directory (tests)."""
    override = os.environ.get(PLAYER_BIN_ENV, "")
    if override and name in ("afplay", "say"):
        return os.path.join(override, name)
    return name


DRY_RUN_PLAYS: list[dict] = []


def _describe_playback(commands: list[list[str]]) -> tuple[str, str, str]:
    sound = ""
    volume = ""
    spoken = "none"
    for command in commands:
        if not command:
            continue
        if command[0] == "afplay" and len(command) >= 4:
            volume = str(command[2])
            name = Path(str(command[3])).name
            sound = name[:-5] if name.endswith(".aiff") else str(command[3])
        elif command[0] == "say" and command[-1]:
            spoken = str(command[-1])
    return sound, volume, spoken


def _record_dry_run(commands: list[list[str]]) -> None:
    sound, volume, spoken = _describe_playback(commands)
    DRY_RUN_PLAYS.append({"sound": sound, "volume": volume, "say": spoken})
    LOG.info("dry-run sound=%s volume=%s say=%s", sound, volume, spoken)


def play_commands(commands: list[list[str]]) -> None:
    """Run afplay and say, one after another. Tests replace this function."""
    if _sounds_blocked_by_env():
        _record_dry_run(commands)
        return
    for command in commands:
        command = [player_tool(command[0]), *command[1:]] if command else command
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise PlaybackError(result.returncode)


def poll_once(
    db_path: Path,
    config: dict,
    state_path: Path,
    cooldowns: dict[str, datetime],
    clock,
    play=None,
    queue=None,
) -> None:
    if play is None and queue is None:
        play = play_commands
    try:
        conn = connect_readonly(db_path)
    except (sqlite3.Error, OSError) as exc:
        LOG.error(
            "Cannot read Notification Center database %s (%s). "
            "Grant Full Disk Access to the Python binary running this agent. Will retry.",
            db_path,
            exc,
        )
        return
    try:
        last_seen = load_state(state_path)
        bundle_id = config["bundle_id"]
        if last_seen is None:
            current = _max_rec_id(conn, bundle_id)
            save_state(state_path, current)
            LOG.info(
                "First run: last-seen rec_id=%s. Existing notifications will not play.",
                current,
            )
            return
        for rec_id, blob in _new_records(conn, bundle_id, last_seen):
            try:
                title, subtitle, body = decode_notification(blob)
            except Exception:
                LOG.error("rec_id=%s has an unreadable notification payload", rec_id)
                save_state(state_path, rec_id)
                continue
            if bot_is_notify_only(title, subtitle, config.get("notify_bots") or []):
                LOG.info("rec_id=%s result=skip-notify-bot", rec_id)
                save_state(state_path, rec_id)
                continue
            try:
                outcome = plan_notification(
                    title,
                    subtitle,
                    body,
                    config,
                    now=clock(),
                    cooldowns=cooldowns,
                    mark=queue is None,
                )
                sound_name = outcome.effective_sound or outcome.match.sound
                bot_key = outcome.match.bot or ""
                if outcome.commands and bot_key and is_deduped(
                    state_path,
                    outcome.match.rule_name,
                    bot_key,
                    time.time(),
                    dedupe_window_seconds(config),
                ):
                    LOG.info(
                        "rec_id=%s rule=%s sound=%s result=dup",
                        rec_id,
                        outcome.match.rule_name,
                        sound_name,
                    )
                    save_state(state_path, rec_id)
                    continue
                if outcome.commands:
                    if queue is not None:
                        queue.enqueue(item_from_outcome(outcome, queue.clock()))
                    else:
                        play(outcome.commands)
                        if bot_key:
                            save_dedupe_play(state_path, outcome.match.rule_name, bot_key, time.time())
                LOG.info(
                    "rec_id=%s rule=%s sound=%s result=%s",
                    rec_id,
                    outcome.match.rule_name,
                    sound_name,
                    outcome.result,
                )
            except Exception as exc:
                LOG.error("rec_id=%s could not be handled (%s)", rec_id, type(exc).__name__)
            save_state(state_path, rec_id)
    finally:
        conn.close()


def setup_logging(path: Path | None = None) -> None:
    path = Path(path) if path is not None else default_log_path()
    for handler in list(LOG.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            LOG.removeHandler(handler)
            handler.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        LOG.addHandler(stream)
        LOG.error("Could not open log file %s (%s).", path, exc)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)


def _print_errors(errors: list[str], note: str | None) -> int:
    if note:
        print(note, file=sys.stderr)
    if not errors:
        return 0
    print("; ".join(errors), file=sys.stderr)
    return 2


def _load_text_or_exit(path: Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Cannot read config {path}: {exc}", file=sys.stderr)
        return None


NULLABLE_RULE_KEYS = {"bot", "contains", "regex", "volume", "voice", "category", "say_text"}
SET_BOOL_KEYS = {"enabled", "paused", "quiet_hours.enabled", "default.say"}
SET_INT_KEYS = {"master_volume", "default.volume", "burst_threshold", "queue_max"}
SET_NUMBER_KEYS = {"poll_interval_seconds", "cooldown_seconds", "burst_window_seconds", "dedupe_window_seconds"}
SET_STRING_KEYS = {
    "bundle_id",
    "default.sound",
    "default.voice",
    "quiet_hours.start",
    "quiet_hours.end",
    "quiet_hours.mode",
}
SET_JSON_KEYS = {"notify_bots", "rules", "default", "quiet_hours"}
AGENT_LABEL = "com.grokbot.notify-sounds"


def minimal_document() -> dict:
    return {
        "enabled": True,
        "poll_interval_seconds": 2,
        "bundle_id": "",
        "master_volume": 100,
        "default": {"sound": "Tink", "volume": 40},
        "rules": [],
    }


def load_raw_document(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return minimal_document()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"Cannot read config: {exc}"]) from exc
    try:
        data = json.loads(strip_jsonc(text))
    except json.JSONDecodeError as exc:
        raise ConfigError([f"Invalid JSON: {exc.msg} (line {exc.lineno})"]) from exc
    if not isinstance(data, dict):
        raise ConfigError(["Config must be a JSON object"])
    return data


def write_config_atomic(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def commit_document(path: Path, mutator) -> dict:
    raw = load_raw_document(path)
    mutator(raw)
    config = normalize_config(raw)
    problems = media_errors(config)
    if problems:
        raise ConfigError(problems)
    write_config_atomic(path, raw)
    return raw


def _rules_of(raw: dict) -> list:
    rules = raw.get("rules")
    if rules is None:
        rules = []
        raw["rules"] = rules
    if not isinstance(rules, list):
        raise ConfigError(["rules must be a list"])
    return rules


def _find_rule(raw: dict, name: str) -> tuple[int, dict]:
    target = name.strip()
    for index, rule in enumerate(_rules_of(raw)):
        if isinstance(rule, dict) and str(rule.get("name", "")).strip() == target:
            return index, rule
    raise ConfigError([f"No rule named {name!r}"])


def add_rule(raw: dict, rule: object) -> None:
    if not isinstance(rule, dict):
        raise ConfigError(["Rule must be a JSON object"])
    _rules_of(raw).append(rule)


def remove_rule(raw: dict, name: str) -> None:
    index, _rule = _find_rule(raw, name)
    del raw["rules"][index]


def update_rule(raw: dict, name: str, patch: object) -> None:
    if not isinstance(patch, dict):
        raise ConfigError(["Rule update must be a JSON object"])
    _index, rule = _find_rule(raw, name)
    for key, value in patch.items():
        if value is None:
            if key not in NULLABLE_RULE_KEYS:
                raise ConfigError([f"Cannot delete {key!r}"])
            rule.pop(key, None)
        else:
            rule[key] = value


def move_rule(raw: dict, name: str, position: int) -> None:
    rules = _rules_of(raw)
    index, rule = _find_rule(raw, name)
    count = len(rules)
    if type(position) is not int or position < 1 or position > count:
        raise ConfigError([f"Position must be from 1 to {count}"])
    rules.pop(index)
    rules.insert(position - 1, rule)


def _seed_default(raw: dict) -> dict:
    current = raw.get("default")
    if not isinstance(current, dict):
        current = {"sound": "Tink", "volume": 40}
        raw["default"] = current
    return current


def _seed_quiet(raw: dict) -> dict:
    current = raw.get("quiet_hours")
    if not isinstance(current, dict):
        current = _quiet_defaults()
        raw["quiet_hours"] = current
    return current


def _known_setting(key: str) -> bool:
    return key in SET_BOOL_KEYS or key in SET_INT_KEYS or key in SET_NUMBER_KEYS or key in SET_STRING_KEYS or key in SET_JSON_KEYS


def coerce_setting(key: str, value: str) -> object:
    if not _known_setting(key):
        raise ConfigError([f"Unknown setting {key!r}"])
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError([f"Invalid JSON: {exc.msg} (line {exc.lineno})"]) from exc
    if key == "notify_bots":
        return [value]
    if key in SET_JSON_KEYS:
        raise ConfigError([f"{key} must be a JSON list or object"])
    if key in SET_BOOL_KEYS:
        lowered = value.casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ConfigError([f"{key} must be true or false"])
    if key in SET_INT_KEYS:
        if not re.fullmatch(r"-?\d+", value):
            raise ConfigError([f"{key} must be an integer"])
        return int(value)
    if key in SET_NUMBER_KEYS:
        if not re.fullmatch(r"-?\d+(\.\d+)?", value):
            raise ConfigError([f"{key} must be a number"])
        return float(value) if "." in value else int(value)
    if key in SET_STRING_KEYS:
        return value
    raise ConfigError([f"Unknown setting {key!r}"])


def clear_setting(raw: dict, key: str) -> None:
    if not _known_setting(key):
        raise ConfigError([f"Unknown setting {key!r}"])
    if "." in key:
        parent_name, child = key.split(".", 1)
        parent = raw.get(parent_name)
        if isinstance(parent, dict):
            parent.pop(child, None)
        return
    raw.pop(key, None)


def apply_setting(raw: dict, key: str, value: str) -> None:
    if value == "":
        clear_setting(raw, key)
        return
    coerced = coerce_setting(key, value)
    if key in SET_JSON_KEYS or key == "notify_bots":
        raw[key] = coerced
        return
    if key.startswith("default."):
        _seed_default(raw)[key.split(".", 1)[1]] = coerced
        return
    if key.startswith("quiet_hours."):
        _seed_quiet(raw)[key.split(".", 1)[1]] = coerced
        return
    raw[key] = coerced


def apply_settings(raw: dict, pairs: list[str]) -> None:
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ConfigError([f"Expected key=value, got {pair!r}"])
        apply_setting(raw, key, value)


def unset_settings(raw: dict, keys: list[str]) -> None:
    for key in keys:
        clear_setting(raw, key)


def parse_json_value(text: str) -> object:
    if text == "-":
        text = sys.stdin.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError([f"Invalid JSON: {exc.msg} (line {exc.lineno})"]) from exc


def edit_config(path: Path, mutator, success: str | None = None) -> int:
    try:
        commit_document(path, mutator)
    except ConfigError as exc:
        print("; ".join(exc.errors), file=sys.stderr)
        return 2
    if success:
        print(success)
    return 0


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _format_number(value: float) -> str:
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def format_quiet_hours(quiet: dict) -> str:
    if not quiet.get("enabled"):
        return "off"
    return f"{quiet['start']}-{quiet['end']} {quiet['mode']}"


def db_status(path: Path, opener=None) -> str:
    opener = connect_readonly if opener is None else opener
    if not Path(path).exists():
        return "missing"
    try:
        conn = opener(path)
    except (sqlite3.Error, OSError):
        return "not permitted"
    try:
        conn.close()
    except Exception:
        pass
    return "readable"


def probe_agent(which=None, run=None) -> str:
    finder = shutil.which if which is None else which
    if not finder("launchctl"):
        return "unknown"
    if run is None and _sounds_blocked_by_env():
        return "unknown"
    runner = subprocess.run if run is None else run
    try:
        result = runner(
            ["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if getattr(result, "returncode", 1) == 0:
        return "loaded"
    return "not loaded"


def render_status(
    config: dict,
    *,
    config_path: Path,
    state_path: Path,
    last_seen: int | None,
    db_path: Path,
    db: str,
    agent: str,
) -> str:
    lines = [
        f"enabled: {_format_bool(bool(config['enabled']))}",
        f"paused: {_format_bool(bool(config.get('paused', False)))}",
        f"mode: {config.get('mode', 'watch')}",
        f"notify_bots: {json.dumps(list(config.get('notify_bots') or []), ensure_ascii=False)}",
        f"master_volume: {config['master_volume']}",
        f"poll_interval_seconds: {_format_number(config['poll_interval_seconds'])}",
        f"cooldown_seconds: {_format_number(config['cooldown_seconds'])}",
        f"quiet_hours: {format_quiet_hours(config['quiet_hours'])}",
        f"rules: {len(config['rules'])}",
        f"config: {config_path}",
        f"state: {state_path}",
        f"last_seen: {'none' if last_seen is None else last_seen}",
        f"db: {db}",
        f"db_path: {db_path}",
        f"agent: {agent}",
    ]
    return "\n".join(lines) + "\n"


def load_runtime_config(path: Path) -> dict:
    if not Path(path).exists():
        return builtin_defaults()
    return load_config(path)


def play_reported(commands: list[list[str]], lock_path: Path) -> int:
    if not commands:
        return 0
    try:
        play_with_lock(lock_path, commands, play_commands)
    except FileNotFoundError as exc:
        missing = exc.filename or "playback command"
        print(
            f"{missing} was not found. The match is shown above; nothing was played.",
            file=sys.stderr,
        )
        return 0
    except PlaybackError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"Playback failed ({exc}). The match is shown above.", file=sys.stderr)
        return 1
    return 0


def render_rule_preview(rule: dict, config: dict) -> str:
    volume = _resolved_volume(rule["volume"], config)
    lines = [
        f"rule: {rule['name']}",
        f"sound: {rule['sound']}",
        f"afplay_volume: {afplay_volume_str(volume, int(config['master_volume']))}",
        f"enabled: {_format_bool(bool(rule['enabled']))}",
    ]
    return "\n".join(lines) + "\n"


def uninstall_agent(home: Path | None = None, *, exists=None, run=None) -> int:
    home = Path.home() if home is None else Path(home)
    finder = shutil.which if exists is None else exists
    if finder("launchctl") and not (run is None and _sounds_blocked_by_env()):
        command = ["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"]
        try:
            if run is None:
                subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                run(command)
        except (OSError, subprocess.SubprocessError):
            pass
    plist = home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
    support = home / "Library" / "Application Support" / "GrokBotNotifySounds"
    try:
        plist.unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(support, ignore_errors=True)
    shim = home / ".local" / "bin" / "grokbot-notify-sounds"
    try:
        shim.unlink()
    except FileNotFoundError:
        pass
    print("uninstalled")
    return 0


_SOUNDS_DIR_OVERRIDE: Path | None = None
_VOICE_OVERRIDE_SET = False
_VOICE_LISTING_OVERRIDE = ""
_VOICE_CACHE: list | None = None
_VOICE_FAILED = False
_VOICE_LINE = re.compile(r"^(.*?)\s+([A-Za-z]{2,3}_[A-Za-z0-9]+)\s+#\s*(.*)$")
_VOICE_STATE_PATH: Path | None = None
VOICE_CACHE_NAME = "voices.json"


class Voice:
    def __init__(self, name: str, lang: str):
        self.name = name
        self.lang = lang


def set_sounds_dir(path: Path | None) -> None:
    global _SOUNDS_DIR_OVERRIDE
    _SOUNDS_DIR_OVERRIDE = None if path is None else Path(path)


def active_sounds_dir() -> Path:
    return SYSTEM_SOUNDS_DIR if _SOUNDS_DIR_OVERRIDE is None else _SOUNDS_DIR_OVERRIDE


def set_voice_state_path(path: Path | None) -> None:
    global _VOICE_STATE_PATH
    _VOICE_STATE_PATH = None if path is None else Path(path)


def set_voice_listing(text: str) -> None:
    global _VOICE_OVERRIDE_SET, _VOICE_LISTING_OVERRIDE, _VOICE_CACHE, _VOICE_FAILED
    _VOICE_OVERRIDE_SET = True
    _VOICE_LISTING_OVERRIDE = text
    _VOICE_CACHE = None
    _VOICE_FAILED = False


def clear_voice_listing() -> None:
    global _VOICE_OVERRIDE_SET, _VOICE_CACHE, _VOICE_FAILED
    _VOICE_OVERRIDE_SET = False
    _VOICE_CACHE = None
    _VOICE_FAILED = False


def clear_voice_cache() -> None:
    global _VOICE_CACHE, _VOICE_FAILED
    _VOICE_CACHE = None
    _VOICE_FAILED = False


def read_voice_listing() -> str | None:
    if _VOICE_OVERRIDE_SET:
        return _VOICE_LISTING_OVERRIDE
    if _sounds_blocked_by_env():
        return None
    try:
        result = subprocess.run([player_tool("say"), "-v", "?"], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def parse_voices(text: str) -> list:
    voices = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        match = _VOICE_LINE.match(line.strip())
        if match is None:
            continue
        name = match.group(1).strip()
        lang = match.group(2)
        key = (name, lang)
        if key in seen:
            continue
        seen.add(key)
        voices.append(Voice(name, lang))
    return voices


def bare_voice_name(name: str) -> str:
    """Strip trailing balanced parenthetical groups. Flo (English (US)) becomes Flo."""
    text = name.strip()
    while text.endswith(")"):
        depth = 0
        start = None
        for index in range(len(text) - 1, -1, -1):
            char = text[index]
            if char == ")":
                depth += 1
            elif char == "(":
                depth -= 1
                if depth == 0:
                    start = index
                    break
        if start is None or depth != 0:
            break
        text = text[:start].rstrip()
    return text


def resolve_voice(query: str, voices: list):
    folded = query.strip().casefold()
    if not folded or not voices:
        return None
    for voice in voices:
        if voice.name.casefold() == folded:
            return voice
    matches = [voice for voice in voices if bare_voice_name(voice.name).casefold() == folded]
    if not matches:
        return None

    def rank(voice: Voice):
        if "(Premium)" in voice.name:
            tier = 0
        elif "(Enhanced)" in voice.name:
            tier = 1
        elif "(" not in voice.name:
            tier = 2
        else:
            tier = 3
        return (tier, voices.index(voice))

    matches.sort(key=rank)
    return matches[0]


def _voice_cache_file() -> Path | None:
    if _VOICE_STATE_PATH is None:
        return None
    return Path(_VOICE_STATE_PATH).parent / VOICE_CACHE_NAME


def _read_voice_cache() -> list | None:
    path = _voice_cache_file()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    voices = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("lang"), str):
            voices.append(Voice(item["name"], item["lang"]))
    return voices or None


def _write_voice_cache(voices: list) -> None:
    path = _voice_cache_file()
    if path is None:
        return
    payload = [{"name": voice.name, "lang": voice.lang} for voice in voices]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return


def load_voices():
    global _VOICE_CACHE, _VOICE_FAILED
    if _VOICE_OVERRIDE_SET:
        if _VOICE_CACHE is None:
            _VOICE_CACHE = parse_voices(_VOICE_LISTING_OVERRIDE)
        return _VOICE_CACHE
    if _VOICE_CACHE is not None:
        return _VOICE_CACHE
    if _VOICE_FAILED:
        return None
    cached = _read_voice_cache()
    if cached is not None:
        _VOICE_CACHE = cached
        return cached
    text = read_voice_listing()
    if text is None:
        _VOICE_FAILED = True
        return None
    _VOICE_CACHE = parse_voices(text)
    _write_voice_cache(_VOICE_CACHE)
    return _VOICE_CACHE


def playback_voice_name(voice: str | None) -> str | None:
    """Resolve a stored voice to the listing display name. None picks a natural voice."""
    if voice:
        voices = load_voices()
        if not voices:
            return voice
        match = resolve_voice(voice, voices)
        return match.name if match is not None else voice
    return pick_natural_voice()


def _voice_sort_key(voice: Voice, index: int):
    if "(Premium)" in voice.name:
        tier = 0
    elif "(Enhanced)" in voice.name:
        tier = 1
    else:
        return None
    if voice.lang == "en_US":
        lang = 0
    elif str(voice.lang).startswith("en_"):
        lang = 1
    else:
        return None
    return (tier, lang, index)


def pick_natural_voice() -> str | None:
    voices = load_voices()
    if not voices:
        return None
    ranked = []
    for index, voice in enumerate(voices):
        key = _voice_sort_key(voice, index)
        if key is not None:
            ranked.append((key, voice.name))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][1]


def format_voice_report(voices: list) -> str:
    ranked = []
    for index, voice in enumerate(voices):
        key = _voice_sort_key(voice, index)
        if key is not None:
            ranked.append((key, voice.name))
    picked = ranked[0][1] if ranked else None
    lines = []
    if not any("(Premium)" in voice.name or "(Enhanced)" in voice.name for voice in voices):
        lines.append("No Premium or Enhanced voice is installed.")
        lines.append(
            "Download one in System Settings > Accessibility > Spoken Content > System voice > Manage Voices."
        )
    for voice in voices:
        tags = []
        if "(Premium)" in voice.name:
            tags.append("Premium")
        if "(Enhanced)" in voice.name:
            tags.append("Enhanced")
        star = "*" if voice.name == picked else " "
        suffix = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"{star} {voice.name} {voice.lang}{suffix}")
    return "\n".join(lines) + ("\n" if lines else "")


def sound_problem(sound: str, sounds_dir: Path) -> str | None:
    try:
        kind, ident = classify_sound(sound)
    except ValueError as exc:
        return str(exc)
    if kind == "file":
        if not Path(ident).is_file():
            return f"missing sound file {ident}"
        return None
    if not Path(sounds_dir).is_dir():
        return None
    if not (Path(sounds_dir) / f"{ident}.aiff").is_file():
        return f"unknown built-in sound {ident!r}"
    return None


def configured_voices(config: dict) -> list[tuple[str, str]]:
    found = []
    voice = config["default"].get("voice")
    if voice:
        found.append(("default", voice))
    for rule in config["rules"]:
        if rule.get("voice"):
            found.append((rule["name"], rule["voice"]))
    return found


def voice_errors(config: dict, voices) -> list[str]:
    needed = configured_voices(config)
    if not needed:
        return []
    if voices is None:
        return [
            f"{label}: voice {name!r} could not be checked because say -v '?' failed"
            for label, name in needed
        ]
    errors = []
    for label, name in needed:
        if resolve_voice(name, voices) is None:
            errors.append(f"{label}: unknown voice {name!r}")
    return errors


def media_errors(config: dict) -> list[str]:
    errors = []
    directory = active_sounds_dir()
    for label, sound in iter_sounds(config):
        problem = sound_problem(sound, directory)
        if problem:
            errors.append(f"{label}: {problem}")
    if configured_voices(config):
        errors.extend(voice_errors(config, load_voices()))
    return errors


def custom_sound_lines(config: dict) -> list[str]:
    seen: list[str] = []
    lines = []
    for _label, sound in iter_sounds(config):
        try:
            kind, ident = classify_sound(sound)
        except ValueError:
            continue
        if kind != "file" or ident in seen:
            continue
        seen.append(ident)
        lines.append(f"{ident}: custom file")
    return lines


def lock_path_for(state_path: Path) -> Path:
    return Path(state_path).parent / LOCK_NAME


def play_with_lock(lock_path: Path, commands, player) -> None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            player(commands)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class CooldownBook:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.values = load_cooldowns(self.path)

    def snapshot(self) -> dict[str, datetime]:
        with self._lock:
            return dict(self.values)

    def mark(self, name: str, when: datetime) -> None:
        with self._lock:
            self.values[name] = when
            save_cooldown(self.path, name, when)


class PlayItem:
    def __init__(self, commands, rule_name: str, enqueued_at: float, say: bool, sound: str = "", bot: str | None = None, source: str = ""):
        self.commands = commands
        self.rule_name = rule_name
        self.enqueued_at = enqueued_at
        self.say = bool(say)
        self.sound = sound
        self.bot = bot
        self.source = source


def item_from_outcome(outcome, enqueued_at: float) -> PlayItem:
    say = outcome.effective_say if outcome.effective_say is not None else outcome.match.say
    sound = outcome.effective_sound or outcome.match.sound
    return PlayItem(
        outcome.commands,
        outcome.match.rule_name,
        enqueued_at,
        say,
        sound=sound,
        bot=outcome.match.bot,
    )


class PlaybackQueue:
    """One worker plays sound, then say, then the next item. Never in parallel."""

    def __init__(self, config_getter, player, lock_path, book=None, clock=None, now=None, hold_for_burst=False):
        self.config_getter = config_getter
        self.player = player
        self.lock_path = Path(lock_path)
        self.book = book
        self.clock = clock or time.monotonic
        self.now = now or datetime.now
        self.hold_for_burst = bool(hold_for_burst)
        self._items: deque = deque()
        self._overflow = 0
        self._cv = threading.Condition()
        self._stop = False
        self._busy = False
        self._thread = None
        self._played_at: dict[tuple[str, str], float] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="playback", daemon=True)
        self._thread.start()

    def _bot_key(self, item: PlayItem) -> str:
        bot = item.bot
        return bot if isinstance(bot, str) else ""

    def _is_dup(self, item: PlayItem, config: dict) -> bool:
        window = dedupe_window_seconds(config)
        if window <= 0:
            return False
        key = (item.rule_name, self._bot_key(item))
        if not key[1]:
            return False
        now = time.time()
        previous = self._played_at.get(key)
        if previous is not None and (now - previous) < window:
            return True
        if self.book is None:
            return False
        return is_deduped(self.book.path, key[0], key[1], now, window)

    def _remember_play(self, item: PlayItem) -> None:
        key = (item.rule_name, self._bot_key(item))
        if not key[1]:
            return
        now = time.time()
        self._played_at[key] = now
        if self.book is not None:
            save_dedupe_play(self.book.path, key[0], key[1], now)

    def _log_burst_summary(self, commands, head: PlayItem | None, config: dict) -> None:
        sound = head.sound if head is not None and head.sound else ""
        volume = ""
        spoken = "none"
        if commands:
            first = commands[0]
            if len(first) >= 3 and first[0] == "afplay":
                volume = str(first[2])
            if not sound and len(first) >= 4:
                name = Path(str(first[3])).name
                sound = name[:-5] if name.endswith(".aiff") else str(first[3])
        if not sound:
            sound = str(config["default"]["sound"])
        for command in commands[1:]:
            if command and command[0] == "say" and command[-1]:
                spoken = str(command[-1])
        LOG.info("burst summary: sound=%s volume=%s say=%s", sound, volume, spoken)

    def enqueue(self, item: PlayItem) -> None:
        config = self.config_getter()
        cap = int(config.get("queue_max", 20))
        with self._cv:
            if len(self._items) >= cap:
                self._overflow += 1
            else:
                self._items.append(item)
            self._cv.notify_all()

    def idle(self) -> bool:
        with self._cv:
            return (not self._items) and self._overflow == 0 and not self._busy

    def drain(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cv:
                idle = (not self._items) and self._overflow == 0 and not self._busy
            if idle:
                return
            time.sleep(0.005)
        raise TimeoutError("playback queue did not drain")

    def close(self) -> None:
        try:
            self.drain(timeout=2)
        except TimeoutError:
            pass
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _cooling(self, item: PlayItem, config: dict) -> bool:
        window = float(config.get("cooldown_seconds", 0))
        if window <= 0 or self.book is None:
            return False
        previous = self.book.snapshot().get(item.rule_name)
        if previous is None:
            return False
        return (self.now() - previous).total_seconds() < window

    def _play_locked(self, commands) -> None:
        play_with_lock(self.lock_path, commands, self.player)

    def _mark(self, item: PlayItem) -> None:
        if self.book is not None:
            self.book.mark(item.rule_name, self.now())

    def _log_notify(self, item: PlayItem, result: str) -> None:
        if getattr(item, "source", "") != "notify":
            return
        LOG.info(
            "notify bot=%s rule=%s sound=%s result=%s",
            item.bot or "",
            item.rule_name,
            item.sound or "",
            result,
        )

    def _take_in_window(self, deadline: float) -> list:
        with self._cv:
            taken = []
            kept: deque = deque()
            for item in self._items:
                if item.enqueued_at <= deadline:
                    taken.append(item)
                else:
                    kept.append(item)
            self._items = kept
            return taken

    def _summary_commands(self, head: PlayItem | None, config: dict, speech_n: int) -> list:
        if head is None:
            default = config["default"]
            speak = bool(default.get("say")) and speech_n > 0
            return build_commands(
                default["sound"],
                int(default["volume"]),
                int(config["master_volume"]),
                speak,
                default.get("voice") if speak else None,
                "",
                "",
                "",
                say_text=f"and {speech_n} more" if speak else None,
            )
        commands = [list(head.commands[0])]
        if speech_n > 0 and head.say:
            voice = None
            for command in head.commands[1:]:
                if command and command[0] == "say" and len(command) >= 4 and command[1] == "-v":
                    voice = command[2]
                    break
            spoken = f"and {speech_n} more"
            if voice:
                commands.append(["say", "-v", voice, spoken])
            else:
                commands.append(["say", spoken])
            return commands
        if speech_n <= 0:
            commands.extend(list(command) for command in head.commands[1:])
        return commands

    def _handle_first(self, first: PlayItem) -> None:
        config = self.config_getter()
        threshold = int(config.get("burst_threshold", 3))
        window = float(config.get("burst_window_seconds", 5))
        deadline = first.enqueued_at + window
        goal = max(threshold - 1, 0)
        batch = [first]
        played = 0
        while played < goal:
            batch.extend(self._take_in_window(deadline))
            if not batch:
                if self.hold_for_burst and not self._stop and self.clock() < deadline:
                    time.sleep(0.02)
                    continue
                break
            item = batch.pop(0)
            fresh = self.config_getter()
            if self._cooling(item, fresh):
                self._log_notify(item, "skip-cooldown")
                continue
            if self._is_dup(item, fresh):
                self._log_notify(item, "dup")
                continue
            self._play_locked(item.commands)
            self._mark(item)
            self._remember_play(item)
            self._log_notify(item, "play")
            played += 1
        if self.hold_for_burst:
            while not self._stop and self.clock() < deadline:
                time.sleep(0.02)
        batch.extend(self._take_in_window(deadline))
        kept = []
        for item in batch:
            if self._is_dup(item, self.config_getter()):
                self._log_notify(item, "dup")
            else:
                kept.append(item)
        batch = kept
        with self._cv:
            overflow = self._overflow
            self._overflow = 0
        count = len(batch) + overflow
        if count <= 0:
            return
        head = batch[0] if batch else None
        fresh = self.config_getter()
        summary = self._summary_commands(head, fresh, count - 1)
        self._log_burst_summary(summary, head, fresh)
        self._play_locked(summary)
        if head is not None:
            self._remember_play(head)
        for item in batch:
            self._log_notify(item, "collapsed")
        LOG.info("burst collapsed (%s)", count)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._items and not self._stop:
                    self._cv.wait()
                if not self._items and self._stop:
                    return
                first = self._items.popleft()
                self._busy = True
            try:
                self._handle_first(first)
            except Exception as exc:
                LOG.error("Playback failed (%s).", exc)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()


def clock_now() -> datetime:
    return datetime.now()


def bot_is_notify_only(title: str, subtitle: str, names: list) -> bool:
    fields = [title.casefold(), subtitle.casefold()]
    for name in names:
        if not isinstance(name, str):
            continue
        needle = name.strip().casefold()
        if needle and any(needle in field for field in fields):
            return True
    return False


NOTIFY_QUEUE_NAME = "notify-queue.json"
NOTIFY_PID_NAME = "notify-drainer.pid"
NOTIFY_SPAWN_LOCK = "notify-spawn.lock"
NOTIFY_QUEUE_LOCK = "notify-queue.lock"


def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def notify_pid_path(state_path: Path) -> Path:
    return Path(state_path).parent / NOTIFY_PID_NAME


def read_drainer_pid(state_path: Path) -> int | None:
    try:
        return int(notify_pid_path(state_path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def drainer_alive(state_path: Path) -> bool:
    pid = read_drainer_pid(state_path)
    return pid is not None and _pid_alive(pid)


def write_drainer_pid(state_path: Path, pid: int) -> None:
    path = notify_pid_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(pid)}\n", encoding="utf-8")


def clear_drainer_pid(state_path: Path, pid: int) -> None:
    if read_drainer_pid(state_path) != pid:
        return
    try:
        notify_pid_path(state_path).unlink()
    except FileNotFoundError:
        pass


def _queue_file(state_path: Path) -> Path:
    return Path(state_path).parent / NOTIFY_QUEUE_NAME


def append_notify_item(state_path: Path, item: dict) -> None:
    path = _queue_file(state_path)
    fd = _exclusive_lock(Path(state_path).parent / NOTIFY_QUEUE_LOCK)
    try:
        items: list = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    items = loaded
            except (OSError, json.JSONDecodeError):
                items = []
        items.append(item)
        _write_state_document(path, items)
    finally:
        _release_lock(fd)


def take_notify_items(state_path: Path) -> list:
    path = _queue_file(state_path)
    fd = _exclusive_lock(Path(state_path).parent / NOTIFY_QUEUE_LOCK)
    try:
        items: list = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    items = loaded
            except (OSError, json.JSONDecodeError):
                items = []
        if path.exists() or items:
            _write_state_document(path, [])
        return items
    finally:
        _release_lock(fd)


def _play_item_from_raw(raw: dict) -> PlayItem | None:
    commands = raw.get("commands")
    if not isinstance(commands, list):
        return None
    try:
        enqueued_at = float(raw.get("enqueued_at"))
    except (TypeError, ValueError):
        enqueued_at = time.time()
    item = PlayItem(
        commands,
        str(raw.get("rule_name") or "default"),
        enqueued_at,
        bool(raw.get("say")),
        sound=str(raw.get("sound") or ""),
        bot=raw.get("bot") if isinstance(raw.get("bot"), str) else None,
        source=str(raw.get("source") or ""),
    )
    return item


def spawn_drainer(config_path: Path, state_path: Path) -> int:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--drain-notify",
        "--config",
        str(config_path),
        "--state",
        str(state_path),
    ]
    proc = subprocess.Popen(
        command,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_forward_sound_env(),
    )
    _record_child_pid(proc.pid)
    return int(proc.pid)


def ensure_drainer(config_path: Path, state_path: Path) -> None:
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _exclusive_lock(state_path.parent / NOTIFY_SPAWN_LOCK)
    try:
        if drainer_alive(state_path):
            return
        pid = spawn_drainer(Path(config_path), state_path)
        write_drainer_pid(state_path, pid)
    finally:
        _release_lock(fd)


def enqueue_notification(
    config: dict,
    config_path: Path,
    state_path: Path,
    *,
    bot: str,
    title: str,
    body: str,
    now: datetime,
) -> str:
    outcome = plan_notification(
        title,
        "",
        body,
        config,
        now=now,
        cooldowns=load_cooldowns(state_path),
        mark=False,
        sender=bot,
    )
    sound = outcome.effective_sound or outcome.match.sound
    if not outcome.commands:
        LOG.info(
            "notify bot=%s rule=%s sound=%s result=%s",
            bot,
            outcome.match.rule_name,
            sound,
            outcome.result,
        )
        return outcome.result
    if is_deduped(
        state_path,
        outcome.match.rule_name,
        bot,
        time.time(),
        dedupe_window_seconds(config),
    ):
        LOG.info(
            "notify bot=%s rule=%s sound=%s result=dup",
            bot,
            outcome.match.rule_name,
            sound,
        )
        return "dup"
    say = outcome.effective_say if outcome.effective_say is not None else outcome.match.say
    append_notify_item(
        state_path,
        {
            "id": f"{time.time()}-{os.getpid()}",
            "rule_name": outcome.match.rule_name,
            "sound": sound,
            "commands": outcome.commands,
            "say": bool(say),
            "bot": bot,
            "source": "notify",
            "enqueued_at": time.time(),
        },
    )
    ensure_drainer(config_path, state_path)
    return outcome.result


def _path_under_temp(path: Path) -> bool:
    try:
        resolved = Path(path).resolve()
        root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _temp_playback_refused(config_path: Path) -> bool:
    """A worker that outlived the suite and lost the dry-run flag must not play."""
    return not _sounds_blocked_by_env() and _path_under_temp(config_path)


def drain_notify_queue(config_path: Path, state_path: Path, player=None, clock=None) -> None:
    state_path = Path(state_path)
    config_path = Path(config_path)
    pid = os.getpid()
    write_drainer_pid(state_path, pid)
    queue = None
    try:
        if _temp_playback_refused(config_path):
            LOG.error("refusing to play: config is under the temp dir and dry-run is not set")
            return
        player = play_commands if player is None else player
        clock = time.time if clock is None else clock
        book = CooldownBook(state_path)
        holder = {"config": builtin_defaults()}
        try:
            holder["config"] = load_runtime_config(config_path)
        except ConfigError:
            pass

        def config_getter():
            try:
                holder["config"] = load_runtime_config(config_path)
            except ConfigError:
                pass
            return holder["config"]

        queue = PlaybackQueue(
            config_getter=config_getter,
            player=player,
            lock_path=lock_path_for(state_path),
            book=book,
            clock=clock,
            hold_for_burst=True,
        )
        queue.start()
        idle_rounds = 0
        while idle_rounds < 8:
            for raw in take_notify_items(state_path):
                item = _play_item_from_raw(raw)
                if item is not None:
                    queue.enqueue(item)
            if queue.idle():
                idle_rounds += 1
            else:
                idle_rounds = 0
            time.sleep(0.02)
        try:
            queue.drain(timeout=5)
        except TimeoutError:
            pass
    finally:
        if queue is not None:
            queue.close()
        clear_drainer_pid(state_path, pid)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_agent_plist(home: Path, python: str, script: str) -> Path:
    agents = Path(home) / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    dest = agents / f"{AGENT_LABEL}.plist"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>{AGENT_LABEL}</string>
	<key>ProgramArguments</key>
	<array>
		<string>{_xml_escape(python)}</string>
		<string>{_xml_escape(script)}</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
</dict>
</plist>
"""
    dest.write_text(body, encoding="utf-8")
    return dest


def _run_launchctl(runner, args: list[str]) -> tuple[int, str]:
    if runner is subprocess.run and _sounds_blocked_by_env():
        if args and args[0] == "bootstrap":
            return 0, ""
        return 1, "dry-run"
    command = ["launchctl", *args]
    try:
        result = runner(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except TypeError:
        try:
            result = runner(command)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    if result is None:
        return 0, ""
    code = getattr(result, "returncode", 0)
    err = getattr(result, "stderr", "") or ""
    out = getattr(result, "stdout", "") or ""
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    text = str(err).strip() or str(out).strip()
    return int(code if code is not None else 0), text


def sync_agent_mode(mode: str, home: Path | None = None, *, finder=None, run=None) -> str | None:
    """Load or unload the agent.

    None means the agent was loaded or unloaded. ``agent was not changed`` means
    launchctl is missing. Any other string is a load failure.
    """
    home = Path.home() if home is None else Path(home)
    finder = shutil.which if finder is None else finder
    if not finder("launchctl") or (run is None and _sounds_blocked_by_env()):
        return "agent was not changed"
    runner = subprocess.run if run is None else run
    label = f"gui/{os.getuid()}/{AGENT_LABEL}"
    if mode == "notify":
        _run_launchctl(runner, ["bootout", label])
        return None
    script = home / "Library" / "Application Support" / "GrokBotNotifySounds" / "grokbot_notify_sounds.py"
    if not script.is_file():
        script = Path(__file__).resolve()
    plist = write_agent_plist(home, sys.executable, str(script))
    code, _err = _run_launchctl(runner, ["bootout", label])
    if code == 0:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            printed, _text = _run_launchctl(runner, ["print", label])
            if printed != 0:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))
    last_err = ""
    for attempt in range(3):
        code, last_err = _run_launchctl(runner, ["bootstrap", f"gui/{os.getuid()}", str(plist)])
        if code == 0:
            _run_launchctl(runner, ["enable", label])
            return None
        if attempt < 2:
            time.sleep(1)
    detail = last_err.strip() or "launchctl bootstrap failed"
    return f"The agent could not be loaded: {detail}"


def set_rule_enabled(raw: dict, name: str, enabled: bool) -> None:
    _index, rule = _find_rule(raw, name)
    rule["enabled"] = enabled


def _checked_master(text: str) -> int:
    if not re.fullmatch(r"\d+", text):
        raise ConfigError(["master_volume must be an integer from 0 to 100"])
    value = int(text)
    if value > 100:
        raise ConfigError(["master_volume must be an integer from 0 to 100"])
    return value


def _set_success(pairs: list[str]) -> str:
    sets = []
    unsets = []
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if sep and value == "":
            unsets.append(key)
        else:
            sets.append(f"{key}={value}" if sep else pair)
    parts = []
    if sets:
        parts.append("set " + " ".join(sets))
    if unsets:
        parts.append("unset " + " ".join(unsets))
    return " ".join(parts)


def _volume_argument(text: str) -> int:
    if not re.fullmatch(r"\d+", text):
        raise argparse.ArgumentTypeError("volume must be an integer from 0 to 100")
    value = int(text)
    if value > 100:
        raise argparse.ArgumentTypeError("volume must be an integer from 0 to 100")
    return value


def _position_argument(text: str) -> int:
    if not re.fullmatch(r"\d+", text):
        raise argparse.ArgumentTypeError("position must be an integer from 1")
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("position must be an integer from 1")
    return value


def run_loop(config_source: ConfigSource, db_path: Path, state_path: Path, once: bool, clock) -> None:
    holder = {"config": builtin_defaults()}
    book = CooldownBook(state_path)
    queue = PlaybackQueue(
        config_getter=lambda: holder["config"],
        player=play_commands,
        lock_path=lock_path_for(state_path),
        book=book,
    )
    queue.start()
    try:
        while True:
            config = config_source.current()
            holder["config"] = config
            try:
                poll_once(db_path, config, state_path, book.snapshot(), clock, queue=queue)
            except Exception as exc:
                LOG.error("Poll failed (%s). Will retry.", type(exc).__name__)
            if once:
                queue.drain()
                return
            time.sleep(config["poll_interval_seconds"])
    finally:
        queue.close()


def _selected_actions(args: argparse.Namespace) -> list[str]:
    names = []
    if args.list_sounds:
        names.append("list-sounds")
    if args.list_apps:
        names.append("list-apps")
    if args.list_rules:
        names.append("list-rules")
    if args.list_voices:
        names.append("list-voices")
    if args.validate:
        names.append("validate")
    if args.test is not None:
        names.append("test")
    if args.test_rule:
        names.append("test-rule")
    if args.play:
        names.append("play")
    if args.notify or getattr(args, "notify_worker", False):
        names.append("notify")
    if args.status:
        names.append("status")
    if args.pause:
        names.append("pause")
    if args.resume:
        names.append("resume")
    if args.set_master_volume is not None:
        names.append("set-master-volume")
    if args.enable:
        names.append("enable")
    if args.disable:
        names.append("disable")
    if args.enable_rule:
        names.append("enable-rule")
    if args.disable_rule:
        names.append("disable-rule")
    if args.add_rule is not None:
        names.append("add-rule")
    if args.remove_rule:
        names.append("remove-rule")
    if args.update_rule:
        names.append("update-rule")
    if args.move_rule:
        names.append("move-rule")
    if args.set:
        names.append("set")
    if args.unset:
        names.append("unset")
    if args.set_mode:
        names.append("set-mode")
    if args.uninstall:
        names.append("uninstall")
    if args.once:
        names.append("once")
    return names


def main(argv: list[str] | None = None, *, log_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grokbot_notify_sounds",
        description="Play alert sounds for notifications from a configured app. There is no settings window.",
    )
    parser.add_argument("--config", type=Path, help="Config path (default: ~/.config/grokbot-sounds/config.json)")
    parser.add_argument("--state", type=Path, help="Cursor state path")
    parser.add_argument("--db", type=Path, help="Notification Center database path")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--list-apps", action="store_true", help="Print bundle ids from the database")
    parser.add_argument("--list-sounds", action="store_true", help="Print built-in sounds and custom paths")
    parser.add_argument("--list-voices", action="store_true", help="Print installed voices and the auto-picked one")
    parser.add_argument("--list-rules", action="store_true", help="Print rules in list order")
    parser.add_argument("--validate", action="store_true", help="Check the config and exit")
    parser.add_argument("--test", metavar="TEXT", help="Treat TEXT as the notification body and play")
    parser.add_argument("--test-rule", metavar="NAME", help="Play one rule's sound at its effective volume")
    parser.add_argument("--play", metavar="SOUND", help="Play one sound at --volume")
    parser.add_argument("--volume", type=_volume_argument, default=None, help="Preview volume 0-100 for --play (default 100)")
    parser.add_argument("--title", default="", help="Title used with --test")
    parser.add_argument("--subtitle", default="", help="Subtitle used with --test")
    parser.add_argument("--bot", default=None, help="Sender name used to match a rule bot, for --test or --notify")
    parser.add_argument("--body", default="", help="Body used with --notify")
    parser.add_argument("--notify", action="store_true", help="Queue a notification from a bot and return")
    parser.add_argument("--notify-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--drain-notify", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--respect-cooldown", action="store_true", help="With --test, honor the shared cooldown")
    parser.add_argument("--respect-quiet-hours", action="store_true", help="With --test, honor quiet hours")
    parser.add_argument("--as-live", action="store_true", help="With --test, honor quiet hours and cooldown")
    parser.add_argument("--ignore-quiet-hours", action="store_true", help="With --test, play the matched rule and skip quiet hours")
    parser.add_argument("--ignore-cooldown", action="store_true", help="With --test, play even when the rule is cooling down")
    parser.add_argument("--status", action="store_true", help="Print a parseable status report")
    parser.add_argument("--pause", action="store_true", help="Set paused true and write the config")
    parser.add_argument("--resume", action="store_true", help="Set paused false and write the config")
    parser.add_argument("--enable", action="store_true", help="Set enabled true and write the config")
    parser.add_argument("--disable", action="store_true", help="Set enabled false and write the config")
    parser.add_argument("--enable-rule", metavar="NAME", help="Set one rule's enabled flag true")
    parser.add_argument("--disable-rule", metavar="NAME", help="Set one rule's enabled flag false")
    parser.add_argument("--set-master-volume", metavar="N", help="Set master_volume to N (0-100)")
    parser.add_argument("--add-rule", metavar="JSON", help="Append one rule object. '-' reads stdin")
    parser.add_argument("--remove-rule", metavar="NAME", help="Remove the rule with this name")
    parser.add_argument("--update-rule", nargs=2, metavar=("NAME", "JSON"), help="Merge a partial rule object. '-' reads stdin")
    parser.add_argument("--move-rule", metavar="NAME", help="Move a rule to --to N (1-based)")
    parser.add_argument("--to", type=_position_argument, default=None, help="1-based position for --move-rule")
    parser.add_argument("--set", action="append", default=None, metavar="KEY=VALUE", help="Set a dotted config key. Repeatable")
    parser.add_argument("--unset", action="append", default=None, metavar="KEY", help="Remove an optional key --set accepts")
    parser.add_argument("--set-mode", metavar="MODE", help="Set mode to watch or notify and load or unload the agent")
    parser.add_argument("--uninstall", action="store_true", help="Remove the LaunchAgent, the shim, and the installed script")
    args = parser.parse_args(argv)
    setup_logging(log_path)

    actions = _selected_actions(args)
    if len(actions) > 1:
        print("Pass one command.", file=sys.stderr)
        return 2
    if args.volume is not None and "play" not in actions:
        print("--volume is only used with --play.", file=sys.stderr)
        return 2
    if args.to is not None and "move-rule" not in actions:
        print("--to is only used with --move-rule.", file=sys.stderr)
        return 2
    if "move-rule" in actions and args.to is None:
        print("--move-rule requires --to N.", file=sys.stderr)
        return 2
    if args.respect_cooldown and "test" not in actions:
        print("--respect-cooldown is only used with --test.", file=sys.stderr)
        return 2
    if args.respect_quiet_hours and "test" not in actions:
        print("--respect-quiet-hours is only used with --test.", file=sys.stderr)
        return 2
    if args.as_live and "test" not in actions:
        print("--as-live is only used with --test.", file=sys.stderr)
        return 2
    if args.ignore_quiet_hours and "test" not in actions:
        print("--ignore-quiet-hours is only used with --test.", file=sys.stderr)
        return 2
    if args.ignore_cooldown and "test" not in actions:
        print("--ignore-cooldown is only used with --test.", file=sys.stderr)
        return 2
    if (args.notify or args.notify_worker) and not args.bot:
        print("--notify requires --bot NAME.", file=sys.stderr)
        return 2

    config_path = Path(args.config) if args.config is not None else default_config_path()
    state_path = Path(args.state) if args.state is not None else default_state_path()
    set_voice_state_path(state_path)
    if args.notify_worker:
        delay = os.environ.get("GROKBOT_NOTIFY_WORKER_DELAY", "")
        if delay:
            time.sleep(float(delay))

    if args.drain_notify:
        if actions:
            print("Pass one command.", file=sys.stderr)
            return 2
        drain_notify_queue(config_path, state_path)
        return 0

    if "list-sounds" in actions:
        try:
            listed = load_runtime_config(config_path)
        except ConfigError as exc:
            print("; ".join(exc.errors), file=sys.stderr)
            return 2
        for line in BUILTIN_SOUND_LINES:
            print(line)
        for line in custom_sound_lines(listed):
            print(line)
        return 0

    if "list-voices" in actions:
        voices = load_voices()
        if voices is None:
            print("Could not list voices because say -v '?' failed.", file=sys.stderr)
            return 2
        sys.stdout.write(format_voice_report(voices))
        return 0

    if "list-apps" in actions:
        db_path = resolve_db_path(args.db)
        try:
            conn = connect_readonly(db_path)
        except (sqlite3.Error, OSError) as exc:
            print(
                f"Cannot read Notification Center database {db_path}: {exc}. "
                "Grant Full Disk Access to this Python binary.",
                file=sys.stderr,
            )
            return 1
        try:
            for bundle_id in list_bundle_ids(conn):
                print(bundle_id)
        finally:
            conn.close()
        return 0

    if "uninstall" in actions:
        return uninstall_agent()

    if "notify" in actions:
        try:
            config = load_runtime_config(config_path)
        except ConfigError as exc:
            print("; ".join(exc.errors), file=sys.stderr)
            return 2
        enqueue_notification(
            config,
            config_path,
            state_path,
            bot=args.bot,
            title=args.title,
            body=args.body or "",
            now=clock_now(),
        )
        return 0

    db_path = resolve_db_path(args.db)

    if "list-rules" in actions or "validate" in actions:
        text = _load_text_or_exit(config_path)
        if text is None:
            return 2
        if "validate" in actions:
            errors, note = validation_report(text)
            code = _print_errors(errors, note)
            if code == 0:
                print("ok")
            return code
        try:
            config = parse_config_text(text)
        except ConfigError as exc:
            return _print_errors(list(exc.errors), None)
        for line in format_rule_lines(config):
            print(line)
        return 0

    if "status" in actions:
        try:
            config = load_runtime_config(config_path)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        sys.stdout.write(
            render_status(
                config,
                config_path=config_path,
                state_path=state_path,
                last_seen=load_state(state_path),
                db_path=db_path,
                db=db_status(db_path),
                agent=probe_agent(),
            )
        )
        return 0

    if "pause" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("paused", True), "paused")
    if "resume" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("paused", False), "resumed")
    if "enable" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("enabled", True), "enabled")
    if "disable" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("enabled", False), "disabled")
    if "enable-rule" in actions:
        rule_name = args.enable_rule
        return edit_config(
            config_path,
            lambda raw: set_rule_enabled(raw, rule_name, True),
            f'enabled rule "{rule_name}"',
        )
    if "disable-rule" in actions:
        rule_name = args.disable_rule
        return edit_config(
            config_path,
            lambda raw: set_rule_enabled(raw, rule_name, False),
            f'disabled rule "{rule_name}"',
        )
    if "set-master-volume" in actions:
        try:
            volume = _checked_master(args.set_master_volume)
        except ConfigError as exc:
            print(exc.errors[0], file=sys.stderr)
            return 2
        return edit_config(
            config_path,
            lambda raw: raw.__setitem__("master_volume", volume),
            f"master volume set to {volume}",
        )
    if "add-rule" in actions:
        try:
            rule = parse_json_value(args.add_rule)
        except ConfigError as exc:
            print("; ".join(exc.errors), file=sys.stderr)
            return 2
        added = rule.get("name").strip() if isinstance(rule, dict) and isinstance(rule.get("name"), str) else ""
        return edit_config(config_path, lambda raw: add_rule(raw, rule), f'added rule "{added}"')
    if "remove-rule" in actions:
        removed = args.remove_rule
        return edit_config(config_path, lambda raw: remove_rule(raw, removed), f'removed rule "{removed}"')
    if "update-rule" in actions:
        name, payload = args.update_rule
        try:
            patch = parse_json_value(payload)
        except ConfigError as exc:
            print("; ".join(exc.errors), file=sys.stderr)
            return 2
        updated = name
        if isinstance(patch, dict) and isinstance(patch.get("name"), str) and patch["name"].strip():
            updated = patch["name"].strip()
        return edit_config(config_path, lambda raw: update_rule(raw, name, patch), f'updated rule "{updated}"')
    if "move-rule" in actions:
        moved = args.move_rule
        position = args.to
        return edit_config(
            config_path,
            lambda raw: move_rule(raw, moved, position),
            f'moved rule "{moved}" to {position}',
        )
    if "set" in actions:
        pairs = list(args.set)
        return edit_config(config_path, lambda raw: apply_settings(raw, pairs), _set_success(pairs))
    if "unset" in actions:
        keys = list(args.unset)
        return edit_config(config_path, lambda raw: unset_settings(raw, keys), "unset " + " ".join(keys))
    if "set-mode" in actions:
        mode = str(args.set_mode)
        if mode not in ("watch", "notify"):
            print("mode must be watch or notify", file=sys.stderr)
            return 2
        code = edit_config(config_path, lambda raw: raw.__setitem__("mode", mode))
        if code != 0:
            return code
        note = sync_agent_mode(mode)
        if note == "agent was not changed":
            print(f"mode set to {mode}; {note}")
            return 0
        if note:
            print(note, file=sys.stderr)
            return 1
        print(f"mode set to {mode}")
        return 0

    if "test-rule" in actions or "play" in actions or "test" in actions:
        try:
            config = load_runtime_config(config_path)
        except ConfigError as exc:
            print("; ".join(exc.errors), file=sys.stderr)
            return 2
        lock_path = lock_path_for(state_path)
        if "test-rule" in actions:
            match = next((rule for rule in config["rules"] if rule["name"] == args.test_rule), None)
            if match is None:
                print(f"No rule named {args.test_rule!r}", file=sys.stderr)
                return 2
            problem = sound_problem(match["sound"], active_sounds_dir())
            if problem and Path(active_sounds_dir()).is_dir():
                print(problem, file=sys.stderr)
                return 2
            sys.stdout.write(render_rule_preview(match, config))
            volume = _resolved_volume(match["volume"], config)
            commands = build_commands(
                match["sound"],
                volume,
                int(config["master_volume"]),
                False,
                None,
                "",
                "",
                "",
            )
            return play_reported(commands, lock_path)
        if "play" in actions:
            volume = 100 if args.volume is None else args.volume
            try:
                classify_sound(args.play)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            problem = sound_problem(args.play, active_sounds_dir())
            if problem:
                print(problem, file=sys.stderr)
                LOG.info("play sound=%s result=error %s", args.play, problem)
                return 2
            afplay = afplay_volume_str(volume, int(config["master_volume"]))
            sys.stdout.write(
                "\n".join(
                    [
                        f"sound: {args.play}",
                        f"volume: {volume}",
                        f"afplay_volume: {afplay}",
                    ]
                )
                + "\n"
            )
            commands = build_commands(args.play, volume, int(config["master_volume"]), False, None, "", "", "")
            code = play_reported(commands, lock_path)
            if code == 0:
                LOG.info("play sound=%s result=played", args.play)
            else:
                LOG.info("play sound=%s result=error exit %s", args.play, code)
            return code
        match = classify(args.title, args.subtitle, args.test, config, args.bot)
        now = datetime.now()
        honor_quiet = not args.ignore_quiet_hours
        honor_cooldown = not args.ignore_cooldown
        softened = False
        if honor_quiet and quiet_active(now, config["quiet_hours"]):
            if config["quiet_hours"]["mode"] == "mute":
                print("result=muted")
                LOG.info("test rule=%s sound=%s result=muted", match.rule_name, match.sound)
                return 0
            softened = True
        if honor_cooldown:
            window = float(config["cooldown_seconds"])
            previous = load_cooldowns(state_path).get(match.rule_name)
            if window > 0 and previous is not None and (now - previous).total_seconds() < window:
                print("suppressed by cooldown")
                LOG.info("test rule=%s result=suppressed", match.rule_name)
                return 0
        if softened:
            default = config["default"]
            sound = default["sound"]
            volume = int(default["volume"])
            say = bool(default["say"])
            voice = default.get("voice")
            say_text = None
            outcome = Outcome(
                match,
                "soft",
                [],
                int(config["master_volume"]),
                effective_sound=sound,
                effective_volume=volume,
                effective_say=say,
            )
        else:
            sound = match.sound
            volume = match.volume
            say = match.say
            voice = match.voice
            say_text = match.say_text
            outcome = Outcome(match, "play", [], int(config["master_volume"]))
        problem = sound_problem(sound, active_sounds_dir())
        if problem:
            print(problem, file=sys.stderr)
            LOG.info("test rule=%s sound=%s result=error %s", match.rule_name, sound, problem)
            return 2
        outcome.commands = build_commands(
            sound,
            volume,
            int(config["master_volume"]),
            say,
            voice,
            args.title,
            args.subtitle,
            args.test,
            say_text=say_text,
            bot=match.bot or args.bot,
        )
        sys.stdout.write(render_test(outcome))
        code = play_reported(outcome.commands, lock_path)
        if code == 0:
            save_cooldown(state_path, match.rule_name, datetime.now())
            if softened:
                LOG.info("test rule=%s sound=%s result=soft", match.rule_name, sound)
            else:
                LOG.info("test rule=%s sound=%s result=played", match.rule_name, sound)
        else:
            LOG.info("test rule=%s sound=%s result=error exit %s", match.rule_name, sound, code)
        return code

    source = ConfigSource(config_path)
    run_loop(source, db_path, state_path, "once" in actions, datetime.now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
