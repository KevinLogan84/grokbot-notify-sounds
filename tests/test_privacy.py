"""The files that ship do not import or shell out to the network."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IMPORT_RE = re.compile(
    r"(?m)^[ \t]*(?:import|from)[ \t]+(socket|urllib|http|ssl|ftplib|smtplib|xmlrpc)\b"
)
ASYNC_RE = re.compile(r"asyncio\.(?:open_connection|start_server)\b")
SHELL_RE = re.compile(r"(?<![\w.-])(?:curl|wget|nc|ssh)(?![\w.-])")


def _shim_text(install: str) -> str:
    marker = 'cat > "${SHIM}" <<EOF\n'
    start = install.index(marker) + len(marker)
    end = install.index("\nEOF", start)
    return install[start:end]


class PrivacyTests(unittest.TestCase):
    def test_shipped_files_do_not_use_the_network(self):
        files = {
            "grokbot_notify_sounds.py": (ROOT / "grokbot_notify_sounds.py").read_text(encoding="utf-8"),
            "install.sh": (ROOT / "install.sh").read_text(encoding="utf-8"),
            "uninstall.sh": (ROOT / "uninstall.sh").read_text(encoding="utf-8"),
            "com.grokbot.notify-sounds.plist": (ROOT / "com.grokbot.notify-sounds.plist").read_text(encoding="utf-8"),
        }
        files["shim"] = _shim_text(files["install.sh"])
        for name, text in files.items():
            self.assertIsNone(IMPORT_RE.search(text), name)
            self.assertIsNone(ASYNC_RE.search(text), name)
            self.assertIsNone(SHELL_RE.search(text), name)


if __name__ == "__main__":
    unittest.main()
