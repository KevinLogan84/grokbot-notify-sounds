#!/bin/bash
# Install the notification-sound agent for the current user.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="watch"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      if [[ $# -lt 2 ]]; then
        echo "mode must be watch or notify" >&2
        exit 1
      fi
      MODE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done
if [[ "${MODE}" != "watch" && "${MODE}" != "notify" ]]; then
  echo "mode must be watch or notify" >&2
  exit 1
fi

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
BIN_DIR="${HOME}/.local/bin"
SHIM="${BIN_DIR}/grokbot-notify-sounds"

mkdir -p "${SUPPORT}" "${LOG_DIR}" "${CONFIG_DIR}" "${BIN_DIR}"

cp -f "${ROOT}/grokbot_notify_sounds.py" "${SCRIPT_PATH}"
cp -f "${ROOT}/config.example.jsonc" "${CONFIG_DIR}/config.example.jsonc"
if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  cp "${ROOT}/config.json" "${CONFIG_DIR}/config.json"
fi

"${PY}" - "${CONFIG_DIR}/config.json" "${MODE}" <<'PY'
import json
import sys

path, mode = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
data["mode"] = mode
text = json.dumps(data, indent=2) + "\n"
with open(path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY

cat > "${SHIM}" <<EOF
#!/bin/sh
exec $(printf '%q' "${PY}") $(printf '%q' "${SCRIPT_PATH}") "\$@"
EOF
chmod +x "${SHIM}"

case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo "${BIN_DIR} is not on PATH" ;;
esac

if [[ "${MODE}" == "notify" ]]; then
  echo 'When you send me a message, also run on my Mac: ~/.local/bin/grokbot-notify-sounds --notify --bot "<your bot name>" --title "<short title>"'
  exit 0
fi

mkdir -p "${AGENTS}"

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
