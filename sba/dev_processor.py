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
CLAUDE_BIN = "/Applications/cmux.app/Contents/Resources/bin/claude"
LOG_FILE = Path.home() / ".sba" / "logs" / "sba-dev.log"


def _notify(chat_id: int, text: str, config: dict) -> None:
    token = config.get("telegram_bot", {}).get("token", "")
    if not token or not chat_id:
        return
    import urllib.request
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(url, body, timeout=10)
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
    _notify(chat_id, f"⚙️ Claude Code разрабатывает <code>{tool_name}</code>...", config)

    prompt = f"""Read ~/Desktop/second-brain-agent-v2/CLAUDE.md for project context.
Read ~/Desktop/second-brain-agent-v2/sba/agent.py to understand the @tool patterns.

Task: {task}

Add the tool to sba/agent.py:
1. Backup agent.py to agent.py.bak
2. Define the async function before the line '_main_server = create_sdk_mcp_server('
   - All imports must be inside the function body (lazy imports)
   - Use _ok() helper for return values
   - Follow existing @tool decorator pattern exactly
3. Add function reference to tools=[] list after line '_request_capability_development_tool,'
4. Add 'mcp__sba__{tool_name}' to allowed_tools list after 'mcp__sba__request_capability_development'
5. Validate: run `~/.sba/venv/bin/python -c "from sba import agent"` from project directory
6. If validation PASSES:
   - Delete agent.py.bak
   - Write to ~/.sba/dev_request.json: {{"status": "ready", "tool_name": "{tool_name}"}}
7. If validation FAILS:
   - Restore agent.py from agent.py.bak, delete agent.py.bak
   - Write to ~/.sba/dev_request.json: {{"status": "error", "message": "<error>"}}

Do not modify any other files. Do not restart the bot."""

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerouslySkipPermissions"],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=300,
            env={
                **os.environ,
                "HOME": str(Path.home()),
                "PATH": f"/Applications/cmux.app/Contents/Resources/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            },
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
        # Save resume context
        if resume_message:
            RESUME_FILE.write_text(json.dumps({
                "chat_id": chat_id,
                "message": resume_message,
                "retry_count": 1,
                "ts": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
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
        _notify(chat_id, f"❌ Не удалось создать <code>{tool_name}</code>:\n<code>{msg[:300]}</code>", config)
        DEV_REQUEST_FILE.unlink(missing_ok=True)

    else:
        # CC didn't update status
        _fail(data, chat_id, config, f"CC завершился без обновления статуса (код {result.returncode})")


def _fail(data: dict, chat_id: int, config: dict, message: str) -> None:
    logging.error(f"Dev request failed: {message}")
    data["status"] = "error"
    data["message"] = message
    DEV_REQUEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _notify(chat_id, f"❌ Ошибка разработки инструмента:\n<code>{message[:300]}</code>", config)
    DEV_REQUEST_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
