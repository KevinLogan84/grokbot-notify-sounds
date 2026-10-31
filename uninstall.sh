#!/bin/bash
# Remove the LaunchAgent and the installed script. Config and logs stay.
set -euo pipefail

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}/com.grokbot.notify-sounds" 2>/dev/null || true

rm -f "${HOME}/Library/LaunchAgents/com.grokbot.notify-sounds.plist"
rm -rf "${HOME}/Library/Application Support/GrokBotNotifySounds"
rm -f "${HOME}/.local/bin/grokbot-notify-sounds"

echo "Removed the LaunchAgent and ~/Library/Application Support/GrokBotNotifySounds."
echo "Left the config and the log in place. To remove those by hand:"
echo "  rm -rf \"${HOME}/.config/grokbot-sounds\""
echo "  rm -f \"${HOME}/Library/Logs/grokbot-notify-sounds.log\" \"${HOME}/Library/Logs/grokbot-notify-sounds.log.\"*"
