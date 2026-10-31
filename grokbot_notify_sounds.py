#!/usr/bin/env python3
"""Play a macOS alert when a configured app posts a notification.

Stdlib only. Reads the Notification Center database in read-only mode and
never uses the network or the Keychain.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("grokbot_notify_sounds")
LOG.addHandler(logging.NullHandler())
LOG.setLevel(logging.INFO)
LOG.propagate = False

SYSTEM_SOUNDS_DIR = Path("/System/Library/Sounds")
CUSTOM_EXTENSIONS = (".aiff", ".mp3", ".wav", ".m4a")
SUMMARY_LIMIT = 120
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUPS = 3


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
    return {
        "enabled": enabled,
        "paused": paused,
        "poll_interval_seconds": poll,
        "bundle_id": bundle_id,
        "master_volume": master,
        "cooldown_seconds": cooldown,
        "quiet_hours": quiet,
        "default": default,
        "rules": rules,
    }


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
                LOG.error(
                    "Config file %s is invalid (%s). Keeping the last good config.",
                    self.path,
                    exc,
                )
            self._seen_mtime = mtime
            return self.config
        self.config = loaded
        self._seen_mtime = mtime
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
    def __init__(self, rule_name: str, sound: str, volume: int, say: bool, voice: str | None):
        self.rule_name = rule_name
        self.sound = sound
        self.volume = volume
        self.say = say
        self.voice = voice


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
            )
    default = config["default"]
    return Match(
        "default",
        default["sound"],
        int(default["volume"]),
        bool(default["say"]),
        default["voice"],
    )


def summary_text(title: str, subtitle: str, body: str, limit: int = SUMMARY_LIMIT) -> str:
    parts = []
    for part in (title, subtitle, body):
        text = (part or "").strip()
        if text:
            parts.append(text)
    joined = " ".join(parts)
    if len(joined) <= limit:
        return joined
    if limit <= 1:
        return joined[:limit]
    return joined[: limit - 1] + "…"


def build_commands(
    sound: str,
    volume: int,
    master: int,
    say: bool,
    voice: str | None,
    title: str,
    subtitle: str,
    body: str,
) -> list[list[str]]:
    kind, ident = classify_sound(sound)
    if kind == "builtin":
        path = str(SYSTEM_SOUNDS_DIR / f"{ident}.aiff")
    else:
        path = ident
    commands = [["afplay", "-v", afplay_volume_str(volume, master), path]]
    if say:
        spoken = summary_text(title, subtitle, body)
        if spoken:
            if voice:
                commands.append(["say", "-v", voice, spoken])
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

    commands = build_commands(sound, volume, master, say, voice, title, subtitle, body)
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
    lines.append(f"result: {outcome.result}")
    if outcome.result == "play-soft":
        lines.append(f"effective_sound: {outcome.effective_sound}")
        lines.append(f"effective_volume: {outcome.effective_volume}")
        lines.append(f"effective_say: {'yes' if outcome.effective_say else 'no'}")
    return "\n".join(lines) + "\n"


def format_rule_lines(config: dict) -> list[str]:
    return [f"{rule['name']}: {rule['sound']}" for rule in config["rules"]]


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


def validation_report(text: str, sounds_dir: Path | None = None) -> tuple[list[str], str | None]:
    try:
        config = parse_config_text(text)
    except ConfigError as exc:
        return list(exc.errors), None
    directory = SYSTEM_SOUNDS_DIR if sounds_dir is None else Path(sounds_dir)
    return sound_report(config, directory)


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


def save_state(path: Path, last_seen: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"last_seen": int(last_seen)}) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def play_commands(commands: list[list[str]]) -> None:
    """Run afplay and say. Tests replace this function."""
    for command in commands:
        subprocess.run(command, check=False)


def poll_once(
    db_path: Path,
    config: dict,
    state_path: Path,
    cooldowns: dict[str, datetime],
    clock,
    play=None,
) -> None:
    if play is None:
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
            try:
                outcome = plan_notification(
                    title,
                    subtitle,
                    body,
                    config,
                    now=clock(),
                    cooldowns=cooldowns,
                    mark=True,
                )
                if outcome.commands:
                    play(outcome.commands)
                LOG.info(
                    "rec_id=%s rule=%s sound=%s result=%s",
                    rec_id,
                    outcome.match.rule_name,
                    outcome.effective_sound or outcome.match.sound,
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
    for error in errors:
        print(error, file=sys.stderr)
    return 1


def _load_text_or_exit(path: Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Cannot read config {path}: {exc}", file=sys.stderr)
        return None


NULLABLE_RULE_KEYS = {"bot", "contains", "regex", "volume", "voice", "category"}
SET_BOOL_KEYS = {"enabled", "paused", "quiet_hours.enabled"}
SET_INT_KEYS = {"master_volume", "default.volume"}
SET_NUMBER_KEYS = {"poll_interval_seconds", "cooldown_seconds"}
SET_STRING_KEYS = {
    "bundle_id",
    "default.sound",
    "quiet_hours.start",
    "quiet_hours.end",
    "quiet_hours.mode",
}
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
    normalize_config(raw)
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


def coerce_setting(key: str, value: str) -> object:
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


def apply_setting(raw: dict, key: str, value: str) -> None:
    coerced = coerce_setting(key, value)
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


def parse_json_value(text: str) -> object:
    if text == "-":
        text = sys.stdin.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError([f"Invalid JSON: {exc.msg} (line {exc.lineno})"]) from exc


def edit_config(path: Path, mutator) -> int:
    try:
        commit_document(path, mutator)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
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


def play_reported(commands: list[list[str]]) -> int:
    if not commands:
        return 0
    try:
        play_commands(commands)
    except FileNotFoundError as exc:
        missing = exc.filename or "playback command"
        print(
            f"{missing} was not found. The match is shown above; nothing was played.",
            file=sys.stderr,
        )
        return 0
    except OSError as exc:
        print(f"Playback failed ({exc}). The match is shown above.", file=sys.stderr)
        return 0
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
    if finder("launchctl"):
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
    print("Removed the LaunchAgent and ~/Library/Application Support/GrokBotNotifySounds.")
    print("Left the config and the log in place. To remove those by hand:")
    print(f'  rm -rf "{home}/.config/grokbot-sounds"')
    print(
        f'  rm -f "{home}/Library/Logs/grokbot-notify-sounds.log" '
        f'"{home}/Library/Logs/grokbot-notify-sounds.log."*'
    )
    return 0


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
    cooldowns: dict[str, datetime] = {}
    while True:
        config = config_source.current()
        try:
            poll_once(db_path, config, state_path, cooldowns, clock)
        except Exception as exc:
            LOG.error("Poll failed (%s). Will retry.", type(exc).__name__)
        if once:
            return
        time.sleep(config["poll_interval_seconds"])


def _selected_actions(args: argparse.Namespace) -> list[str]:
    names = []
    if args.list_sounds:
        names.append("list-sounds")
    if args.list_apps:
        names.append("list-apps")
    if args.list_rules:
        names.append("list-rules")
    if args.validate:
        names.append("validate")
    if args.test is not None:
        names.append("test")
    if args.test_rule:
        names.append("test-rule")
    if args.play:
        names.append("play")
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
    parser.add_argument("--list-sounds", action="store_true", help="Print built-in sound names")
    parser.add_argument("--list-rules", action="store_true", help="Print rule name and sound")
    parser.add_argument("--validate", action="store_true", help="Check the config and exit")
    parser.add_argument("--test", metavar="TEXT", help="Treat TEXT as the notification body and play")
    parser.add_argument("--test-rule", metavar="NAME", help="Play one rule's sound at its effective volume")
    parser.add_argument("--play", metavar="SOUND", help="Play one sound at --volume")
    parser.add_argument("--volume", type=_volume_argument, default=None, help="Preview volume 0-100 for --play (default 100)")
    parser.add_argument("--title", default="", help="Title used with --test")
    parser.add_argument("--subtitle", default="", help="Subtitle used with --test")
    parser.add_argument("--bot", default=None, help="Sender name used to match a rule bot, for --test")
    parser.add_argument("--status", action="store_true", help="Print a parseable status report")
    parser.add_argument("--pause", action="store_true", help="Set paused true and write the config")
    parser.add_argument("--resume", action="store_true", help="Set paused false and write the config")
    parser.add_argument("--enable", action="store_true", help="Set enabled true and write the config")
    parser.add_argument("--disable", action="store_true", help="Set enabled false and write the config")
    parser.add_argument("--set-master-volume", type=_volume_argument, metavar="N", help="Set master_volume to N (0-100)")
    parser.add_argument("--add-rule", metavar="JSON", help="Append one rule object. '-' reads stdin")
    parser.add_argument("--remove-rule", metavar="NAME", help="Remove the rule with this name")
    parser.add_argument("--update-rule", nargs=2, metavar=("NAME", "JSON"), help="Merge a partial rule object. '-' reads stdin")
    parser.add_argument("--move-rule", metavar="NAME", help="Move a rule to --to N (1-based)")
    parser.add_argument("--to", type=_position_argument, default=None, help="1-based position for --move-rule")
    parser.add_argument("--set", action="append", default=None, metavar="KEY=VALUE", help="Set a dotted config key. Repeatable")
    parser.add_argument("--uninstall", action="store_true", help="Remove the LaunchAgent and the installed script")
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

    if "list-sounds" in actions:
        if not SYSTEM_SOUNDS_DIR.is_dir():
            print(f"No system sounds directory at {SYSTEM_SOUNDS_DIR}", file=sys.stderr)
            return 0
        names = sorted(path.stem for path in SYSTEM_SOUNDS_DIR.glob("*.aiff") if path.is_file())
        for name in names:
            print(name)
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

    config_path = Path(args.config) if args.config is not None else default_config_path()
    state_path = Path(args.state) if args.state is not None else default_state_path()
    db_path = resolve_db_path(args.db)

    if "list-rules" in actions or "validate" in actions:
        text = _load_text_or_exit(config_path)
        if text is None:
            return 1
        if "validate" in actions:
            errors, note = validation_report(text)
            return _print_errors(errors, note)
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
        return edit_config(config_path, lambda raw: raw.__setitem__("paused", True))
    if "resume" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("paused", False))
    if "enable" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("enabled", True))
    if "disable" in actions:
        return edit_config(config_path, lambda raw: raw.__setitem__("enabled", False))
    if "set-master-volume" in actions:
        volume = args.set_master_volume
        return edit_config(config_path, lambda raw: raw.__setitem__("master_volume", volume))
    if "add-rule" in actions:
        try:
            rule = parse_json_value(args.add_rule)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return edit_config(config_path, lambda raw: add_rule(raw, rule))
    if "remove-rule" in actions:
        return edit_config(config_path, lambda raw: remove_rule(raw, args.remove_rule))
    if "update-rule" in actions:
        name, payload = args.update_rule
        try:
            patch = parse_json_value(payload)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return edit_config(config_path, lambda raw: update_rule(raw, name, patch))
    if "move-rule" in actions:
        return edit_config(config_path, lambda raw: move_rule(raw, args.move_rule, args.to))
    if "set" in actions:
        pairs = list(args.set)
        return edit_config(config_path, lambda raw: apply_settings(raw, pairs))

    if "test-rule" in actions or "play" in actions or "test" in actions:
        try:
            config = load_runtime_config(config_path)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if "test-rule" in actions:
            match = next((rule for rule in config["rules"] if rule["name"] == args.test_rule), None)
            if match is None:
                print(f"No rule named {args.test_rule!r}", file=sys.stderr)
                return 1
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
            return play_reported(commands)
        if "play" in actions:
            volume = 100 if args.volume is None else args.volume
            try:
                classify_sound(args.play)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
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
            return play_reported(
                build_commands(args.play, volume, int(config["master_volume"]), False, None, "", "", "")
            )
        outcome = plan_notification(
            args.title,
            args.subtitle,
            args.test,
            config,
            now=datetime.now(),
            cooldowns={},
            mark=False,
            sender=args.bot,
        )
        sys.stdout.write(render_test(outcome))
        return play_reported(outcome.commands)

    source = ConfigSource(config_path)
    run_loop(source, db_path, state_path, "once" in actions, datetime.now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
