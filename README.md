# Grok Bot notification sounds

Plays a macOS alert when Notification Center delivers a notification from an app you choose. An ordered list of rules maps the sender and the text to a sound. The seeded config has no rules: you pick every sound and every volume.

There is no settings UI. Change behavior with the flags below, or by editing the config file. The only GUI step is the one-time Full Disk Access grant.

The agent is local. It does not use the network or the Keychain. It opens the Notification Center database read-only. The log records record ids, rule names, and sounds. It does not store full notification text.

## Install

1. Clone this repository.
2. Run `./install.sh` (any working directory works; the script finds its own files).
3. Grant Full Disk Access to the absolute `python3` that `install.sh` prints — the same binary written into the LaunchAgent plist — not to Terminal.

`install.sh` is safe to run again. It refreshes the installed script and `config.example.jsonc`, and it copies `config.json` only when `~/.config/grokbot-sounds/config.json` does not already exist.

## Full Disk Access

System Settings → Privacy & Security → Full Disk Access → add that absolute `python3`.

Check with that same binary:

```sh
/absolute/path/to/python3 "$HOME/Library/Application Support/GrokBotNotifySounds/grokbot_notify_sounds.py" --status
```

`db: readable` means the grant works. `db: not permitted` means it does not. `db: missing` means the database path is not there. `--status` exits 0 when the report prints, including when the database cannot be read. It exits non-zero only when the config file itself is invalid.

Copy one bundle id into `bundle_id`. An empty `bundle_id` matches nothing. `--list-apps` still works when it is empty.

Set `bundle_id` before the first real run. The first launch records the current high-water mark and does not play notifications that are already in the database. After you change `bundle_id`, delete `~/Library/Application Support/GrokBotNotifySounds/state.json` if you want that same skip-the-backlog behavior again.

## Config

Live file: `~/.config/grokbot-sounds/config.json`. The agent reloads it when the file mtime changes, on the next poll, without a restart. If a new version is missing, unreadable, or invalid, the last good config stays active and the error is appended to `~/Library/Logs/grokbot-notify-sounds.log`.

Check a file before you rely on it:

```sh
python3 grokbot_notify_sounds.py --validate
python3 grokbot_notify_sounds.py --list-rules
```

`--validate` checks the schema, reports missing custom sound files, and, when `/System/Library/Sounds` exists, reports unknown built-in names. When that directory is missing it says built-ins were not checked and still fails on schema errors and missing custom files. `--list-rules` prints `name: sound` in list order, including disabled rules. It does not open the database.

Volumes are integers from 0 to 100. `master_volume` defaults to 100 when omitted. A rule with no `volume` uses `default.volume`, or 100 if that is omitted too. `afplay -v` is `(volume / 100) * (master_volume / 100)`. A rule volume of 80 with a master volume of 50 plays at `0.4`.

`cooldown_seconds` and `quiet_hours` are optional. When they are absent, cooldown is off and quiet hours are off.

### Fields

- `enabled` (bool). `false` still advances the cursor and plays nothing.
- `paused` (bool, default false). Separate from `enabled`. `true` plays nothing and speaks nothing, and still advances the cursor, so resume does not replay a backlog. A sound plays only when enabled, not paused, and not suppressed by quiet hours, cooldown, or a disabled rule.
- `poll_interval_seconds` (number greater than 0).
- `bundle_id` (string).
- `master_volume` (integer 0–100).
- `cooldown_seconds` (number, `0` off). The timer is per rule name. The fallback uses the name `default`. A skipped play does not reset the window. The timer lives in the running agent, not in `--test`.
- `quiet_hours.enabled` (bool), `quiet_hours.start` and `quiet_hours.end` (`HH:MM`, local), `quiet_hours.mode` (`mute` or `soft`). If start is later than end, the window wraps midnight. `mute` plays nothing and does not speak. `soft` plays only the default sound and volume, and speaks only when `default.say` is true.
- `default.sound`, `default.volume` (integer 0–100). Optional `default.say` (bool, default false) and `default.voice` (string or null).
- `rules` (ordered list). First match wins.

Each rule:

- `name` (string, required, unique).
- `bot` (string, optional). Case-insensitive substring of the title or the subtitle.
- `contains` (string, or a list of strings). Case-insensitive substring. A list matches if any phrase matches.
- `regex` (string). Case-insensitive search. When `contains` and `regex` are both set, both must match.
- `match_in`: `title`, `body`, or `both` (default `both`). This applies to `contains` and `regex` only. Text does not use the subtitle. `bot` is not affected.
- A rule needs `bot`, `contains`, or `regex`. Bot and text together mean both have to match.
- `sound` (required). A built-in stem (`Glass` → `/System/Library/Sounds/Glass.aiff`), or an absolute or `~` path ending in `.aiff`, `.mp3`, `.wav`, or `.m4a`.
- `volume` (optional integer 0–100).
- `say` (optional bool, default false) and `voice` (optional string or null). Speech is a short summary of the title, subtitle, and body, cut at about 120 characters. Mute quiet hours skip it.
- `enabled` (optional bool, default true). A disabled rule is skipped.
- `category` (optional string). A label only. It does not change match order.

`_comment` keys are ignored. `config.example.jsonc` is the commented form of the same settings (`//` and `/* */` comments, trailing commas allowed).

### Bot only

```json
{
  "name": "Alfred answers",
  "bot": "Alfred",
  "sound": "Glass",
  "volume": 80
}
```

### Text only

```json
{
  "name": "Needs approval",
  "contains": ["approve", "needs your OK"],
  "match_in": "both",
  "sound": "Sosumi"
}
```

### Bot plus text

```json
{
  "name": "Alfred shipped",
  "bot": "Alfred",
  "regex": "shipped|delivered",
  "match_in": "body",
  "sound": "Hero",
  "volume": 60,
  "say": true,
  "voice": "Zarvox"
}
```

`config.example.jsonc` lists those three rules with the specific Alfred rule first, so a shipped note is not swallowed by the broader Alfred rule.

Custom file, quiet hours, and cooldown:

```json
{
  "master_volume": 100,
  "cooldown_seconds": 30,
  "quiet_hours": {
    "enabled": true,
    "start": "22:00",
    "end": "07:00",
    "mode": "mute"
  },
  "rules": [
    {
      "name": "Build finished",
      "contains": "build finished",
      "regex": "build finished|build failed",
      "sound": "~/Sounds/done.wav",
      "volume": 70,
      "say": true,
      "voice": "Zarvox"
    }
  ]
}
```

Edits reload on the next poll when the file mtime changes. A command that rewrites the file does it atomically: a temp file in the same directory, fsync, then rename. If the edit would be invalid, nothing is written and the previous file stays as it was. Rewriting may drop `//` comments. Every other key is kept, including keys this program does not use.

`--pause` sets `paused` to true. `--resume` sets it to false. `--enable` and `--disable` set `enabled`. `--set-master-volume N` sets `master_volume` (integer 0–100). If the config file is missing, these commands create it from the built-in defaults and then apply the change.

`--set key=value` is repeatable (`--set a=b --set c=d`). It is how you set cooldown and quiet hours. Keys:

- `enabled`, `paused` (`true` or `false`)
- `master_volume`, `default.volume` (integers 0–100)
- `poll_interval_seconds`, `cooldown_seconds` (numbers)
- `bundle_id`, `default.sound`
- `quiet_hours.enabled`, `quiet_hours.start`, `quiet_hours.end`, `quiet_hours.mode` (`mute` or `soft`)

`default` and `quiet_hours` are created if they are missing.

`--add-rule JSON` appends one rule object. `-` reads stdin. A duplicate `name` or an invalid rule is rejected and the file is left unchanged. `--remove-rule NAME` removes that rule and errors if it is missing. `--update-rule NAME JSON` merges a partial object into that rule. Arrays and scalars replace the old value. JSON `null` deletes an optional key: `bot`, `contains`, `regex`, `volume`, `voice`, `category`. The file is checked again after the merge. A rename via `name` must stay unique. `--move-rule NAME --to N` moves that rule to a 1-based position. `N=1` is the first rule. The name must exist and `N` must be from 1 through the number of rules.

Speech is per rule, not global. Turn it on with a merge, or by editing the rule:

```sh
python3 grokbot_notify_sounds.py --update-rule "Alfred shipped" '{"say": true, "voice": "Zarvox"}'
```

```json
"say": true,
"voice": "Zarvox"
```

`voice` is any voice installed on the Mac. Omit it to use the default voice.

## Commands

Paths used by more than one command:

- `--config PATH` — config file. Default `~/.config/grokbot-sounds/config.json`.
- `--state PATH` — cursor file. Default `~/Library/Application Support/GrokBotNotifySounds/state.json`.
- `--db PATH` — Notification Center database.

Read-only and playback:

- `--status` — `key: value` lines: enabled, paused, master_volume, poll_interval_seconds, cooldown_seconds, quiet_hours (`off` or `start-end mode`), rules (count), config, state, last_seen, db (`readable`, `not permitted`, or `missing`), db_path, agent (`loaded`, `not loaded`, or `unknown` when launchctl is absent).
- `--validate` — schema, missing custom files, and unknown built-in names when `/System/Library/Sounds` exists.
- `--list-rules` — `name: sound` in list order, including disabled rules. No database.
- `--list-sounds` — built-in stems. If that directory is missing, one line on stderr and exit 0.
- `--list-apps` — bundle ids, one per line, sorted.
- `--test TEXT` — TEXT is the body. Optional `--title`, `--subtitle`, and `--bot`. `--bot` is the sender matched against a rule's `bot` field and does not also have to appear in the title. Prints the winning rule name, sound, and effective afplay volume, then plays unless paused, disabled, quiet hours, or cooldown would suppress it.
- `--test-rule NAME` — print that rule and play its sound at the effective volume (rule volume, or the default, times master). Plays even when the rule is disabled, and prints that. Exit non-zero if the name does not exist.
- `--play SOUND` — preview a built-in name or a custom path. `--volume N` is 0–100 and defaults to 100. Effective afplay volume is `(N / 100) * (master_volume / 100)`.
- `--once` — poll once and exit. With no command flag, the process keeps polling.

If `afplay` is missing, `--test`, `--test-rule`, and `--play` still print what would play and exit 0.

Writes:

- `--pause`, `--resume`
- `--enable`, `--disable`
- `--set-master-volume N`
- `--set key=value`
- `--add-rule JSON`
- `--remove-rule NAME`
- `--update-rule NAME JSON`
- `--move-rule NAME --to N`

`--uninstall` matches `./uninstall.sh`. It is idempotent, boots out `gui/$(id -u)/com.grokbot.notify-sounds` when `launchctl` exists, and removes the LaunchAgent plist and `~/Library/Application Support/GrokBotNotifySounds`. It does not delete `~/.config/grokbot-sounds` or the log, and it does not prompt. When `launchctl` is missing, that step is skipped and the files are still removed.

## Stop and uninstall

Stop without uninstalling:

```sh
launchctl bootout gui/$(id -u)/com.grokbot.notify-sounds
```

Uninstall:

```sh
./uninstall.sh
```

or `python3 grokbot_notify_sounds.py --uninstall`.

That unloads `com.grokbot.notify-sounds`, deletes the LaunchAgent plist, and deletes `~/Library/Application Support/GrokBotNotifySounds` (the installed script and the cursor state). It leaves `~/.config/grokbot-sounds` and `~/Library/Logs/grokbot-notify-sounds.log` in place, and it prints how to remove those by hand.

Run `./install.sh` again to update the installed script.

## Tests

```sh
python3 -m unittest discover -s tests -t . -v
```
