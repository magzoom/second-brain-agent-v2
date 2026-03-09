"""
launchd daemon manager for SBA 2.0.

Daemons:
  bot     — Telegram bot, KeepAlive + ThrottleInterval=30, runs forever
  inbox   — Every 2 h (StartInterval 7200)
  legacy  — Daily at 09:00 (StartCalendarInterval)
  digest  — Daily at 09:09 (StartCalendarInterval)

Labels match v1 (com.sba.*) so installing v2 replaces v1 plists.
Bot label is com.sba.bot (v1 used com.sba.telegram-bot — handled explicitly).
Plist files installed to ~/Library/LaunchAgents/.
"""

import subprocess
import sys
from pathlib import Path


# ── Configuration ─────────────────────────────────────────────────────────────

SBA_VENV = Path.home() / "Desktop" / "second-brain-agent-v2" / ".venv"
SBA_PYTHON = SBA_VENV / "bin" / "python3.12"
SBA_EXE = SBA_VENV / "bin" / "sba"
LOG_DIR = Path.home() / ".sba" / "logs"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

# Use com.sba.* labels (same as v1 for inbox/legacy) so v2 install replaces v1
DAEMONS = {
    "bot": "com.sba.bot",
    "inbox": "com.sba.inbox",
    "legacy": "com.sba.legacy",
    "digest": "com.sba.digest",
}

# v1 used a different label for the bot; unload it when installing v2 bot
V1_BOT_PLIST = LAUNCH_AGENTS / "com.sba.telegram-bot.plist"


def get_log_path(daemon: str) -> str:
    return str(LOG_DIR / f"sba-{daemon}.log")


def _plist_path(daemon: str) -> Path:
    return LAUNCH_AGENTS / f"{DAEMONS[daemon]}.plist"


# ── Plist builders ────────────────────────────────────────────────────────────

def _bot_plist() -> str:
    log = get_log_path("bot")
    python = str(SBA_PYTHON)
    exe = str(SBA_EXE)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sba.bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{exe}</string>
    <string>bot</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{SBA_VENV}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>"""


def _inbox_plist() -> str:
    log = get_log_path("inbox")
    python = str(SBA_PYTHON)
    exe = str(SBA_EXE)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sba.inbox</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{exe}</string>
    <string>inbox</string>
  </array>
  <key>StartInterval</key>
  <integer>7200</integer>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{SBA_VENV}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>"""


def _legacy_plist() -> str:
    log = get_log_path("legacy")
    python = str(SBA_PYTHON)
    exe = str(SBA_EXE)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sba.legacy</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{exe}</string>
    <string>legacy</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{SBA_VENV}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>"""


def _digest_plist() -> str:
    log = get_log_path("digest")
    python = str(SBA_PYTHON)
    exe = str(SBA_EXE)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sba.digest</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{exe}</string>
    <string>digest</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>9</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{SBA_VENV}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>"""


_BUILDERS = {
    "bot": _bot_plist,
    "inbox": _inbox_plist,
    "legacy": _legacy_plist,
    "digest": _digest_plist,
}


# ── Public API ────────────────────────────────────────────────────────────────

def install_daemon(name: str, config: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    plist_text = _BUILDERS[name]()
    plist_file = _plist_path(name)

    # For bot: also stop v1's com.sba.telegram-bot if it exists
    if name == "bot" and V1_BOT_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(V1_BOT_PLIST)], capture_output=True)

    # Unload current plist if already loaded
    subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True)

    plist_file.write_text(plist_text)
    result = subprocess.run(
        ["launchctl", "load", str(plist_file)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Failed to load {name}: {result.stderr}")


def uninstall_daemon(name: str) -> None:
    plist_file = _plist_path(name)
    if plist_file.exists():
        subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True)
        plist_file.unlink()


def daemon_status(name: str) -> str:
    label = DAEMONS[name]
    result = subprocess.run(
        ["launchctl", "list", label],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "not loaded"
    for line in result.stdout.splitlines():
        if '"PID"' in line or line.strip().startswith('"PID"'):
            return f"running ({line.strip()})"
    return "loaded (not running)"
