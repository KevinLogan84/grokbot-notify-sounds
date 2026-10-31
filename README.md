# Grok Bot notification sounds

Plays a macOS alert when Notification Center delivers a notification from an app you choose. An ordered list of rules maps the sender and the text to a sound. The seeded config has no rules: you pick every sound and every volume.

There is no settings UI. Change behavior with the flags below, or by editing the config file. The only GUI step is the one-time Full Disk Access grant.

The agent is local. It does not use the network or the Keychain. It opens the Notification Center database read-only. The log records record ids, rule names, and sounds. It does not store full notification text.

## Privacy

No network access, no telemetry, no analytics, no tracking, no crash reporting, no update checks. Nothing leaves your Mac. It reads the local Notification Center database (watch mode only) and its own config, and plays sounds with afplay/say. It never touches the Keychain.

`say -v '?'` is a local process, not a network call. `afplay`, `say`, `launchctl`, and `getconf DARWIN_USER_DIR` are local processes. The tool does not phone home, and it does not download or check a hash.

Reads:

- `~/.config/grokbot-sounds/config.json`
- `~/Library/Application Support/GrokBotNotifySounds/state.json` (cursor, cooldown times, and dedupe times)
- `~/Library/Application Support/GrokBotNotifySounds/voices.json` (cached output of `say -v '?'`)
- `~/Library/Application Support/GrokBotNotifySounds/notify-queue.json` (queued `--notify` items)
- Notification Center, watch mode only, read-only: `~/Library/Group Containers/group.com.apple.usernoted/db2/db` and `$DARWIN_USER_DIR/com.apple.notificationcenter/db2/db` (`DARWIN_USER_DIR` comes from `getconf`)
- `/System/Library/Sounds` when listing or checking built-in sounds
- Custom sound paths named in the config, when a rule or the default uses a file

Writes:

- `~/.config/grokbot-sounds/config.json` (the CLI and the installer). Install copies `config.example.jsonc` to `~/.config/grokbot-sounds/config.example.jsonc` and refreshes that copy. The player does not read the example.
- `~/Library/Application Support/GrokBotNotifySounds/state.json`
- `~/Library/Application Support/GrokBotNotifySounds/playback.lock`
- `~/Library/Application Support/GrokBotNotifySounds/voices.json`
- `~/Library/Application Support/GrokBotNotifySounds/notify-queue.json`
- `~/Library/Application Support/GrokBotNotifySounds/notify-drainer.pid`, `notify-spawn.lock`, and `notify-queue.lock`
- `~/Library/Logs/grokbot-notify-sounds.log` and its rotated backups
- `~/Library/LaunchAgents/com.grokbot.notify-sounds.plist`
- `~/Library/Application Support/GrokBotNotifySounds/grokbot_notify_sounds.py` (the installed copy of this script)
- `~/.local/bin/grokbot-notify-sounds` (the shim install writes)

Config and state writes go through a temporary file in the same directory, then replace the real file.

## Verify your download

Compare the file you have with the SHA-256 published on the release page:

```sh
shasum -a 256 <file>
```

The tool does not download or check that hash itself.

## Install

Pick one mode. If both the LaunchAgent and `--notify` deliver the same message, it can play twice.

Watch mode is the LaunchAgent. It needs Full Disk Access and reads the Notification Center database.

1. Clone this repository.
2. Run `./install.sh` or `./install.sh --mode watch` (any working directory works; the script finds its own files).
3. Grant Full Disk Access to the absolute `python3` that `install.sh` prints — the same binary written into the LaunchAgent plist — not to Terminal.

Notify mode does not install a daemon and does not need Full Disk Access. A bot runs a command on the Mac when it messages you.

```sh
./install.sh --mode notify
```

That prints the Bot line below. It does not write a LaunchAgent and it does not ask for Full Disk Access.

Both modes install `~/.local/bin/grokbot-notify-sounds`, a shell shim that execs the absolute `python3` and the script in Application Support. If that directory is not on `PATH`, install prints one line saying so. The absolute path still works. Install is safe to run again. It refreshes the installed script and `config.example.jsonc`, and it copies `config.json` only when that file does not already exist. It stores `mode` (`watch` or `notify`) in the config.

## Bot line

When you send me a message, also run on my Mac: ~/.local/bin/grokbot-notify-sounds --notify --bot "<your bot name>" --title "<short title>"

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
- `paused` (bool, default false). Separate from `enabled`. `true` plays nothing and speaks nothing, and still advances the cursor, so resume does not replay a backlog. A sound plays only when enabled, not paused, and not suppressed by quiet hours, cooldown, a disabled rule, or a bot listed in `notify_bots`.
- `mode` (`watch` or `notify`, default `watch`).
- `notify_bots` (optional list of bot names). The watch daemon skips playback when the title or subtitle contains one of these names (case-insensitive) and still advances the cursor, so `--notify` is the only player for them. Omit the list and the daemon skips nobody.
- `burst_threshold` (integer, default 3) and `burst_window_seconds` (number, default 5). Inside one window the first `threshold - 1` items each play that item's own rule sound, volume, and speech. Starting with the threshold-th item, the rest collapse into one extra play of that item's sound (the default sound only when that item already fell through to `default`). If say is on for that item and later items were collapsed with it, it says `and N more` (N is the count after that first collapsed item; it does not say `and 0 more`). The log line is `burst collapsed (N)` where N is how many items were not played on their own. That extra play is also logged as `burst summary: sound=<name> volume=<0-1> say=<text>`. `volume` is the value passed to `afplay -v`. When nothing is spoken, `say=none`. `--notify` keeps the playback lock released while it waits out the window. `queue_max` (integer, default 20) caps the queue; extras count toward that collapse.
- `dedupe_window_seconds` (number, optional). When the key is absent, it uses `burst_window_seconds`. `0` turns dedupe off. If an alert has the same rule name and the same bot as one that already played inside this window, it does not play again and logs `result=dup`. A different bot or a different rule still plays. An alert with no bot is not deduped. A duplicate is not a new burst item and is not `result=play` or `result=collapsed`. `--set dedupe_window_seconds=0` stores that value.
- `cooldown_seconds` is still per rule name. Last-played times are stored in the state file so the daemon and the CLI share them. A rule inside its cooldown is not enqueued. Collapsed items do not refresh cooldown except the item that actually played.
- `poll_interval_seconds` (number greater than 0).
- `bundle_id` (string).
- `master_volume` (integer 0–100).
- `cooldown_seconds` (number, `0` off). The fallback key is `default`. A skipped play does not reset the window. `--test` honors cooldown. `--ignore-cooldown` plays anyway. A real `--test` play updates that timestamp.
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
- `say` (optional bool, default false) and `voice` (optional string or null). Speech is off unless `say` is true on the rule, or on `default` for the item actually being spoken. Omitted `say` does not speak and does not pick a voice. Speech is a short summary of the title, subtitle, and body, cut at about 120 characters, unless `say_text` is set. Mute quiet hours skip it.
- `say_text` (optional string). Used only when `say` is true. Placeholders: `{bot}`, `{title}`, `{body}`. Missing values are empty. Spoken text is capped at about 300 characters.
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
  "voice": "Samantha"
}
```

`config.example.jsonc` lists those three rules with speech left off and the specific Alfred rule first, so a shipped note is not swallowed by the broader Alfred rule. The JSON above is the hand edit that turns speech on.

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
      "voice": "Samantha"
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
python3 grokbot_notify_sounds.py --update-rule "Alfred shipped" '{"say": true, "voice": "Samantha"}'
```

```json
"say": true,
"voice": "Samantha"
```

Speech stays off until you opt in. Either of these turns it on:

```sh
python3 grokbot_notify_sounds.py --set default.say=true
python3 grokbot_notify_sounds.py --update-rule "Alfred shipped" '{"say": true, "voice": "Samantha"}'
```

`voice`, when set, must match a line from `say -v '?'`. The match is case-insensitive against the full display name or the bare name (trailing parenthetical groups stripped, so `Flo (English (US))` matches `Flo`). If several voices share a bare name, the full name wins; otherwise `(Premium)`, then `(Enhanced)`, then the plain name. The config keeps the string you set. Playback passes the listing's display name to `say -v`. Omit it and, only when `say` is true, the program picks the first installed `(Premium)` voice, preferring `en_US` then any `en_*`, then the same for `(Enhanced)`. If none of those are installed it uses the system voice and does not pass `-v`. One-time download path: System Settings > Accessibility > Spoken Content > System voice > Manage Voices. `--list-voices` prints the installed voices, tags Premium and Enhanced, and stars the auto-picked one. The same display name and locale are listed once; a different locale stays as its own line. A parsed voice list is cached next to the state file so later runs can skip `say -v '?'`. `say -v '?'` is a local process, not a network call.

`--set key=value` checks the same rules as the rest of the CLI. Volume stays an integer 0–100, a sound must exist, a voice must be installed when it is set, and quiet-hours mode must be `mute` or `soft`. A value that starts with `[` or `{` is JSON, so `--set 'notify_bots=["Shopper · Bot"]'` keeps the middle dot. A plain string for `notify_bots` is a one-element list, and `notify_bots=[]` clears it. An invalid value prints one line on stderr, exits 2, and leaves the file unchanged. Success is one line, for example `set default.voice=Samantha`. `--set default.voice=` and `--unset default.voice` remove that key and print `unset default.voice`. The same empty value and `--unset` work for any optional key `--set` accepts. If removing the key would make the config invalid, nothing is written. These keys work: `default.voice`, `default.sound`, `default.volume`, `default.say`, `quiet_hours.start`, `quiet_hours.end`, `quiet_hours.mode`, `notify_bots`. Missing `default` or `quiet_hours` objects are created. `--status` prints `notify_bots` as JSON.

Built-in names are checked against `/System/Library/Sounds/<Name>.aiff` when that directory exists. Custom paths must exist. `--add-rule`, `--update-rule`, and `--validate` reject an unknown built-in or a missing file. When that directory is missing, `--play` of a built-in is left to `afplay`'s exit code. A non-zero `afplay` or `say` exit code is returned as the process status. If `afplay` itself is missing, the command still prints what would play and exits 0.

The daemon plays from one queue: sound, then say, then the next item. `--test`, `--test-rule`, `--play`, and `--notify` take the same exclusive lock file next to the state file, so they wait instead of overlapping the daemon.

## Commands

Paths used by more than one command:

- `--config PATH` — config file. Default `~/.config/grokbot-sounds/config.json`.
- `--state PATH` — cursor file. Default `~/Library/Application Support/GrokBotNotifySounds/state.json`.
- `--db PATH` — Notification Center database.

Read-only and playback:

- `--status` — `key: value` lines: enabled, paused, mode, notify_bots (JSON), master_volume, poll_interval_seconds, cooldown_seconds, quiet_hours (`off` or `start-end mode`), rules (count), config, state, last_seen, db (`readable`, `not permitted`, or `missing`), db_path, agent (`loaded`, `not loaded`, or `unknown` when launchctl is absent).
- `--validate` — schema, missing custom files, and unknown built-in names when `/System/Library/Sounds` exists. Prints `ok` and exits 0 when the config is valid. Errors stay one line and exit 2.
- `--list-rules` — `name: sound` in list order, including disabled rules. No database.
- `--list-sounds` — one line per built-in, `Glass: bright glassy chime` and the other thirteen names with a short description, then each custom path from `default` or `rules` as `path: custom file`.
- `--list-voices` — installed voices. Stars the auto-picked Premium or Enhanced voice. One line per display name and locale.
- `--notify` — `--notify --bot NAME [--title TEXT] [--body TEXT]`. Does not open the Notification Center database and does not need Full Disk Access. The parent parses `--bot` and spawns a worker, then returns, before importing the player and before `say -v '?'`. The worker runs the same rules, volume, quiet hours, cooldown, dedupe, burst collapse, and playback lock as the daemon. Suppressed items log the reason and play nothing. A missing `--bot` prints one line and exits 2.
- `--list-apps` — bundle ids, one per line, sorted.
- `--test TEXT` — TEXT is the body. Optional `--title`, `--subtitle`, and `--bot`. `--bot` is the sender matched against a rule's `bot` field and does not also have to appear in the title. Quiet hours and cooldown apply, the same as the daemon and `--notify`. Soft quiet hours play the default sound and print and log `result=soft`. Mute quiet hours play nothing and print and log `result=muted`. `--ignore-quiet-hours` plays the matched rule sound and skips quiet hours. `--ignore-cooldown` plays even when the rule is inside its cooldown. The two ignore flags can be combined. `--respect-quiet-hours` and `--as-live` are aliases of this default.
- `--test-rule NAME` — print that rule and play its sound at the effective volume (rule volume, or the default, times master). Plays even when the rule is disabled, and prints that. Exit non-zero if the name does not exist.
- `--play SOUND` — preview a built-in name or a custom path. `--volume N` is 0–100 and defaults to 100. Effective afplay volume is `(N / 100) * (master_volume / 100)`.
- `--once` — poll once and exit. With no command flag, the process keeps polling.

If `afplay` is missing, `--test`, `--test-rule`, and `--play` still print what would play and exit 0.

Writes:

- `--pause`, `--resume`
- `--enable`, `--disable`
- `--set-master-volume N`
- `--set key=value`
- `--unset key`
- `--add-rule JSON`
- `--remove-rule NAME`
- `--update-rule NAME JSON`
- `--move-rule NAME --to N`
- `--enable-rule NAME`, `--disable-rule NAME`
- `--set-mode watch` or `--set-mode notify`

Every write prints one success line, such as `added rule "Alfred answers"`, `paused`, `master volume set to 40`, `set cooldown_seconds=30`, `enabled rule "Alfred answers"`, or `mode set to notify`. A bad value, including `--set-master-volume 150`, prints one line on stderr and exits 2, with no usage text. `--set-mode` also loads or unloads `com.grokbot.notify-sounds`. If `launchctl` is missing, the mode is still saved and the line says the agent was not changed.

`--uninstall` matches `./uninstall.sh`. It is idempotent, boots out `gui/$(id -u)/com.grokbot.notify-sounds` when `launchctl` exists, and removes the LaunchAgent plist, `~/Library/Application Support/GrokBotNotifySounds`, and `~/.local/bin/grokbot-notify-sounds`. It does not delete `~/.config/grokbot-sounds` or the log, and it does not prompt. When `launchctl` is missing, that step is skipped and the files are still removed.

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
