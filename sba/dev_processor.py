"""
Dev processor: triggered by launchd WatchPaths when ~/.sba/dev_request.json appears.
Runs Claude Code CLI to implement the requested tool in sba/agent.py.
"""
import json
import logging
import os
import subprocess
import time
from pathlib import Path

DEV_REQUEST_FILE = Path.home() / ".sba" / "dev_request.json"
RESUME_FILE = Path.home() / ".sba" / "bot_resume.json"
PROJECT_DIR = Path.home() / "Desktop" / "second-brain-agent-v2"
CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")
LOG_FILE = Path.home() / ".sba" / "logs" / "sba-dev.log"


def _notify(chat_id: int, text: str, config: dict) -> None:
    token = config.get("telegram_bot", {}).get("token", "")
    if not token or not chat_id:
        return
    import urllib.request
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logging.warning(f"Telegram notify failed: {e}")


def _load_config() -> dict:
    config_file = Path.home() / ".sba" / "config.yaml"
    try:
        import yaml
        with open(config_file) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not DEV_REQUEST_FILE.exists():
        return

    data = json.loads(DEV_REQUEST_FILE.read_text(encoding="utf-8"))
    if data.get("status") != "pending":
        return

    config = _load_config()
    chat_id = int(data.get("chat_id", 0))
    tool_name = data.get("tool_name", "")
    task = data.get("task", "")
    resume_message = data.get("resume_message", "")

    # Mark as processing
    data["status"] = "processing"
    DEV_REQUEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(f"Processing dev request for tool: {tool_name}")
    _notify(chat_id, f"⚙️ Claude Code разрабатывает {tool_name}...", config)

    prompt = f"""Read ~/Desktop/second-brain-agent-v2/CLAUDE.md for project context.
Read ~/Desktop/second-brain-agent-v2/sba/agent.py to understand the @tool patterns.

Task: {task}

IMPORTANT — before writing any code, investigate first:
1. Search locally for existing solutions:
   - `find ~ -type f -iname "*<keyword>*" 2>/dev/null | grep -v ".cache\|node_modules\|.git"` — find related tools/scripts
   - `find ~ -type d -iname "*mcp*" 2>/dev/null | grep -v ".cache\|node_modules"` — find MCP servers
   - `ls ~/.local/bin/ /opt/homebrew/bin/` — find installed CLI tools
   - `~/.sba/venv/bin/pip list | grep <keyword>` — find installed Python packages
2. Check actual API of any library you plan to use:
   - `~/.sba/venv/bin/pip show <package>` — version
   - `~/.sba/venv/bin/python -c "import <pkg>; print(dir(<pkg>))"` — actual methods
3. If tool already exists in agent.py — read it, understand what's wrong, fix in place
4. Choose the simplest reliable solution — prefer existing local tools over writing new code

Only after investigation, implement the fix:
1. Backup agent.py to agent.py.bak
2. If tool already exists — edit it in place. If new — add before '_main_server = create_sdk_mcp_server('
   - All imports inside the function body (lazy imports)
   - Use _ok() helper for return values
   - Use full paths for CLI tools: str(Path.home() / ".sba" / "venv" / "bin" / "tool-name")
   - Follow existing @tool decorator pattern exactly
3. If new tool: add to tools=[] list after '_request_capability_development_tool,'
4. If new tool: add 'mcp__sba__{tool_name}' to allowed_tools after 'mcp__sba__request_capability_development'
5. Validate: `~/.sba/venv/bin/python -c "from sba import agent"`
6. If PASSES: delete agent.py.bak, write {{"status": "ready", "tool_name": "{tool_name}"}} to ~/.sba/dev_request.json
7. If FAILS: restore from agent.py.bak, write {{"status": "error", "message": "<error>"}} to ~/.sba/dev_request.json

Do not modify any other files. Do not restart the bot."""

    # Run CC inside a tmux window so output is visible for debugging
    tmux_session = "sba-dev"
    log_file = str(LOG_FILE.parent / "sba-dev-cc.log")
    full_path = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    cc_env = {**os.environ, "HOME": str(Path.home()), "PATH": full_path}

    # Ensure tmux session exists
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_session],
        capture_output=True, env=cc_env,
    )
    # Send a visible header to the tmux window
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session,
         f"echo '=== SBA Dev: {tool_name} ===' && date", "Enter"],
        capture_output=True, env=cc_env,
    )

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=str(PROJECT_DIR),
            text=True,
            timeout=300,
            env=cc_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Write output to tmux and log file
        cc_output = result.stdout + result.stderr
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"\n=== {tool_name} ===\n{cc_output}\n")
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_session,
             f"echo 'CC exit={result.returncode}' && tail -20 {log_file}", "Enter"],
            capture_output=True, env=cc_env,
        )
        logging.info(f"CC exit={result.returncode} stdout={result.stdout[:300]}")
        if result.stderr:
            logging.warning(f"CC stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        logging.error("CC timed out")
        _fail(data, chat_id, config, "Claude Code превысил лимит времени (5 минут)")
        return
    except Exception as e:
        logging.error(f"CC launch failed: {e}")
        _fail(data, chat_id, config, str(e))
        return

    # Read updated status from CC
    if not DEV_REQUEST_FILE.exists():
        _fail(data, chat_id, config, "dev_request.json исчез во время обработки")
        return

    updated = json.loads(DEV_REQUEST_FILE.read_text(encoding="utf-8"))
    status = updated.get("status")

    if status == "ready":
        logging.info(f"Tool {tool_name} ready, saving resume and restarting bot")
        # Auto-commit the new/updated tool (no sensitive data check needed — agent.py has no secrets)
        git_env = {
            **os.environ,
            "HOME": str(Path.home()),
            "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "SBA Dev Bot",
            "GIT_COMMITTER_NAME": "SBA Dev Bot",
            "GIT_AUTHOR_EMAIL": "sba@local",
            "GIT_COMMITTER_EMAIL": "sba@local",
        }
        subprocess.run(
            ["git", "add", "sba/agent.py"],
            cwd=str(PROJECT_DIR), capture_output=True, env=git_env,
        )
        commit_result = subprocess.run(
            ["git", "commit", "-m",
             f"feat: add {tool_name} tool via CC auto-development\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>"],
            cwd=str(PROJECT_DIR), capture_output=True, text=True, env=git_env,
        )
        if commit_result.returncode == 0:
            logging.info(f"Auto-committed {tool_name} tool")
        else:
            logging.warning(f"Git commit skipped: {commit_result.stderr[:200]}")

        # Save resume context — increment retry_count from existing file to prevent infinite loops
        if resume_message:
            existing_retry = 0
            if RESUME_FILE.exists():
                try:
                    existing_retry = json.loads(RESUME_FILE.read_text(encoding="utf-8")).get("retry_count", 0)
                except Exception:
                    pass
            new_retry = existing_retry + 1
            RESUME_FILE.write_text(json.dumps({
                "chat_id": chat_id,
                "message": resume_message,
                "retry_count": new_retry,
                "ts": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
            logging.info(f"Resume saved with retry_count={new_retry}")
        # Install updated package into production venv
        subprocess.run(
            [str(Path.home() / ".sba" / "venv" / "bin" / "pip"), "install", str(PROJECT_DIR), "--no-deps", "-q"],
            capture_output=True,
        )
        # Restart bot
        uid = os.getuid()
        subprocess.Popen(["launchctl", "kickstart", "-k", f"gui/{uid}/com.sba.bot"])
        DEV_REQUEST_FILE.unlink(missing_ok=True)

    elif status == "error":
        msg = updated.get("message", "неизвестная ошибка")
        logging.error(f"CC reported error: {msg}")
        _notify(chat_id, f"❌ Не удалось создать {tool_name}:\n{msg[:300]}", config)
        DEV_REQUEST_FILE.unlink(missing_ok=True)

    else:
        # CC didn't update status
        _fail(data, chat_id, config, f"CC завершился без обновления статуса (код {result.returncode})")


def _fail(data: dict, chat_id: int, config: dict, message: str) -> None:
    logging.error(f"Dev request failed: {message}")
    data["status"] = "error"
    data["message"] = message
    DEV_REQUEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _notify(chat_id, f"❌ Ошибка разработки инструмента:\n{message[:300]}", config)
    DEV_REQUEST_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
