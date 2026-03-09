"""
Integration checker — verifies all external services are accessible.
Used by `sba check` command.
"""

import asyncio
import subprocess
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATUS_OK = "✅"
STATUS_FAIL = "❌"
STATUS_WARN = "⚠️"


async def check_all(config: dict) -> dict[str, dict]:
    """Run all integration checks concurrently."""
    results = await asyncio.gather(
        check_apple_notes(),
        check_google_tasks(config),
        check_google_drive(config),
        check_telegram_bot(config),
        check_telegram_userbot(config),
        check_claude_api(config),
        return_exceptions=True,
    )

    keys = [
        "apple_notes", "google_tasks",
        "google_drive", "telegram_bot", "telegram_userbot", "claude_api",
    ]

    output = {}
    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            output[key] = {"status": "fail", "message": str(result)}
        else:
            output[key] = result
    return output


def print_report(results: dict[str, dict]) -> None:
    icons = {"ok": STATUS_OK, "fail": STATUS_FAIL, "warn": STATUS_WARN}
    labels = {
        "apple_notes": "Apple Notes",
        "google_tasks": "Google Tasks",
        "google_drive": "Google Drive",
        "telegram_bot": "Telegram Bot",
        "telegram_userbot": "Telegram Userbot",
        "claude_api": "Claude API",
    }

    print("\n── Integration Check ─────────────────────────────")
    for key, result in results.items():
        icon = icons.get(result["status"], STATUS_WARN)
        label = labels.get(key, key)
        print(f"  {icon}  {label:<22} {result['message']}")
    print("──────────────────────────────────────────────────\n")

    all_ok = all(r["status"] == "ok" for r in results.values())
    if all_ok:
        print("All integrations are working. You're good to go!\n")
    else:
        failed = [k for k, r in results.items() if r["status"] == "fail"]
        print(f"Issues found: {', '.join(failed)}")
        print("Check config.yaml and macOS permissions.\n")


async def check_apple_notes() -> dict:
    note_store = Path.home() / "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
    if note_store.exists():
        return {"status": "ok", "message": "NoteStore.sqlite found"}
    return {"status": "warn", "message": "NoteStore.sqlite not found — grant Full Disk Access"}


async def check_google_tasks(config: dict) -> dict:
    try:
        from sba.integrations.google_tasks import build_service
        service = await asyncio.to_thread(build_service, config)
        result = service.tasklists().list(maxResults=10).execute()
        count = len(result.get("items", []))
        return {"status": "ok", "message": f"Google Tasks accessible ({count} lists)"}
    except Exception as e:
        return {"status": "fail", "message": str(e)[:100]}


async def check_google_drive(config: dict) -> dict:
    creds_file = Path(config.get("google_drive", {}).get("credentials_file", "~/.sba/google_credentials.json")).expanduser()
    token_file = Path(config.get("google_drive", {}).get("token_file", "~/.sba/google_token.json")).expanduser()

    if not creds_file.exists():
        return {"status": "fail", "message": f"credentials.json not found at {creds_file}"}
    if not token_file.exists():
        return {"status": "warn", "message": "token.json missing — run `sba auth google` to authorize"}

    try:
        from sba.integrations.google_drive import build_service
        service = build_service(config)
        service.about().get(fields="user").execute()
        return {"status": "ok", "message": "Google Drive API accessible"}
    except Exception as e:
        return {"status": "fail", "message": str(e)[:100]}


async def check_telegram_bot(config: dict) -> dict:
    token = config.get("telegram_bot", {}).get("token", "")
    if not token or token == "BOT_TOKEN_HERE":
        return {"status": "warn", "message": "Bot token not configured"}

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
        if data.get("ok"):
            username = data["result"].get("username", "")
            return {"status": "ok", "message": f"@{username}"}
        return {"status": "fail", "message": data.get("description", "invalid token")}
    except Exception as e:
        return {"status": "fail", "message": str(e)[:100]}


async def check_telegram_userbot(config: dict) -> dict:
    session_file = Path.home() / ".sba" / "telegram_userbot.session"
    api_id = config.get("telegram_userbot", {}).get("api_id", 0)
    api_hash = config.get("telegram_userbot", {}).get("api_hash", "")

    if not api_id or api_hash == "hash_here":
        return {"status": "warn", "message": "api_id/api_hash not configured"}
    if not session_file.exists():
        return {"status": "warn", "message": "No session — run `sba auth telegram` to authorize"}
    return {"status": "ok", "message": "Session file found"}


async def check_claude_api(config: dict) -> dict:
    api_key = config.get("anthropic", {}).get("api_key", "")
    if not api_key or api_key.startswith("sk-ant-..."):
        return {"status": "warn", "message": "API key not configured"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        return {"status": "ok", "message": "Claude API accessible"}
    except Exception as e:
        return {"status": "fail", "message": str(e)[:100]}


def _run_osascript(script: str) -> dict[str, Any]:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
