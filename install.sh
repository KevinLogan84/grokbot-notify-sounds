#!/bin/bash
# Install the notification-sound agent for the current user.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! PY="$(command -v python3)"; then
  echo "python3 was not found on PATH" >&2
  exit 1
fi
case "${PY}" in
  /*) ;;
  *)
    echo "python3 must resolve to an absolute path (got ${PY})" >&2
    exit 1
    ;;
esac

SUPPORT="${HOME}/Library/Application Support/GrokBotNotifySounds"
LOG_DIR="${HOME}/Library/Logs"
CONFIG_DIR="${HOME}/.config/grokbot-sounds"
AGENTS="${HOME}/Library/LaunchAgents"
SCRIPT_PATH="${SUPPORT}/grokbot_notify_sounds.py"
PLIST_DEST="${AGENTS}/com.grokbot.notify-sounds.plist"

mkdir -p "${SUPPORT}" "${LOG_DIR}" "${CONFIG_DIR}" "${AGENTS}"

cp -f "${ROOT}/grokbot_notify_sounds.py" "${SCRIPT_PATH}"
cp -f "${ROOT}/config.example.jsonc" "${CONFIG_DIR}/config.example.jsonc"
if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  cp "${ROOT}/config.json" "${CONFIG_DIR}/config.json"
fi

xml_escape() {
  local s="$1"
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  printf '%s' "${s}"
}

PYTHON_ESC="$(xml_escape "${PY}")"
SCRIPT_ESC="$(xml_escape "${SCRIPT_PATH}")"
TEMPLATE="$(<"${ROOT}/com.grokbot.notify-sounds.plist")"
PLIST="${TEMPLATE//__PYTHON__/${PYTHON_ESC}}"
PLIST="${PLIST//__SCRIPT__/${SCRIPT_ESC}}"
printf '%s\n' "${PLIST}" > "${PLIST_DEST}"

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}/com.grokbot.notify-sounds" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DEST}"
launchctl enable "gui/${UID_NUM}/com.grokbot.notify-sounds"

echo "LaunchAgent Python: ${PY}"
echo "Grant Full Disk Access to that Python binary. See README.md."
