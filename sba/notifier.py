"""
Telegram notification sender.
Works independently from the bot daemon — uses direct HTTP calls.
"""

import asyncio
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)


class Notifier:
    """Async Telegram notification sender via Bot API."""

    def __init__(self, config: dict):
        self.token = config.get("telegram_bot", {}).get("token", "")
        self.chat_id = config.get("owner", {}).get("telegram_chat_id", 0)
        self._enabled = bool(self.token and self.token != "BOT_TOKEN_HERE" and self.chat_id)
        if not self._enabled:
            logger.warning("Telegram notifications disabled — check config")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a text message to the owner."""
        if not self._enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return True
                    logger.error(f"Telegram API error: {data.get('description')}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False

    # Alias for v2 agent tools
    async def send_message(self, text: str, reply_markup: dict = None) -> bool:
        if reply_markup is not None:
            return await self.send_with_inline_keyboard(text, reply_markup)
        return await self.send(text)

    async def send_with_inline_keyboard(self, text: str, inline_keyboard: dict) -> bool:
        """Send a message with an inline keyboard (raw dict format for Bot API)."""
        if not self._enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": inline_keyboard,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return True
                    logger.error(f"Telegram API error: {data.get('description')}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send inline keyboard message: {e}")
            return False

    async def send_inbox_report(self, processed: int, errors: int = 0) -> None:
        if processed == 0 and errors == 0:
            return
        parts = [f"✅ <b>Inbox обработан</b>\n📋 Обработано: {processed}"]
        if errors:
            parts.append(f"❌ Ошибок: {errors}")
        await self.send("\n".join(parts))

    async def send_legacy_report(self, processed: int, actions_created: int, pending_deletions: int, errors: int = 0, folders_decided: int = 0) -> None:
        # Folder/note cards are sent individually — no summary needed
        pass

    async def send_error(self, error_msg: str, module: str = "SBA") -> None:
        msg = f"❌ <b>Ошибка [{module}]</b>\n{error_msg}"
        await self.send(msg)

    async def send_deletion_request(
        self, deletion_id: int, item_title: str, item_source: str, reason: str = "",
    ) -> Optional[int]:
        """Send deletion confirmation request with inline buttons. Returns Telegram message ID."""
        if not self._enabled:
            return None

        text = (f"🗑 <b>Подтвердить удаление?</b>\n\n"
                f"📄 {item_title}\n📦 Источник: {item_source}")
        if reason:
            text += f"\n💡 Причина: {reason}"
        text += f"\n\nID: <code>{deletion_id}</code>"

        inline_keyboard = {"inline_keyboard": [[
            {"text": "✅ Удалить", "callback_data": f"confirm_del:{deletion_id}"},
            {"text": "❌ Оставить", "callback_data": f"cancel_del:{deletion_id}"},
        ]]}

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "reply_markup": inline_keyboard}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return data["result"]["message_id"]
                    logger.error(f"Failed to send deletion request: {data}")
                    return None
        except Exception as e:
            logger.error(f"Failed to send deletion request: {e}")
            return None

    async def send_folder_decision(
        self, reg_id: int, title: str, path: str,
        subfolder_count: int, file_count: int,
        suggestion: str, has_subfolders: bool,
    ) -> Optional[int]:
        """Send folder classification request with [Глубже] [Саммари] buttons."""
        if not self._enabled:
            return None

        text = f"📁 <b>{title}</b>\nПуть: {path}\n"
        counts = []
        if subfolder_count:
            counts.append(f"{subfolder_count} папок")
        if file_count:
            counts.append(f"{file_count} файлов")
        if counts:
            text += f"Внутри: {', '.join(counts)}\n"
        if suggestion:
            text += f"\nАгент: {suggestion}"

        buttons = []
        if has_subfolders:
            buttons.append({"text": "📂 Глубже", "callback_data": f"folder_deep:{reg_id}"})
        buttons.append({"text": "📝 Саммари", "callback_data": f"folder_summary:{reg_id}"})

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [buttons]},
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return data["result"]["message_id"]
                    logger.error(f"send_folder_decision failed: {data}")
                    return None
        except Exception as e:
            logger.error(f"send_folder_decision failed: {e}")
            return None

    async def send_media_notification(self, path: str, media_files: list, reg_id: int = 0) -> None:
        """Notify about media files that may belong in Google Photos."""
        if not self._enabled or not media_files:
            return
        names = ", ".join(media_files[:5])
        if len(media_files) > 5:
            names += f" и ещё {len(media_files) - 5}"
        text = (f"📷 <b>Медиафайлы</b>\nПуть: {path}\n"
                f"{len(media_files)} файлов: {names}\n\nВозможно стоит перенести в Google Photos.")
        if not reg_id:
            await self.send(text)
            return
        inline_keyboard = {"inline_keyboard": [[
            {"text": "✅ Ознакомлен", "callback_data": f"media_ack:{reg_id}"},
        ]]}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "reply_markup": inline_keyboard}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        logger.error(f"send_media_notification failed: {data}")
        except Exception as e:
            logger.error(f"send_media_notification failed: {e}")

    async def edit_message(self, message_id: int, text: str) -> bool:
        """Edit an existing bot message."""
        if not self._enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    return data.get("ok", False)
        except Exception as e:
            logger.error(f"edit_message failed: {e}")
            return False

    async def post_to_goal_tracker_channel(
        self, entries: list[tuple[str, str]], channel_id: int,
    ) -> bool:
        """Post completed tasks to Goal Tracker Diary channel."""
        if not entries:
            return True

        from datetime import date, timedelta
        from collections import defaultdict

        yesterday = date.today() - timedelta(days=1)
        date_tag = yesterday.strftime("#%d%m%Yгод")
        by_category: dict[str, list[str]] = defaultdict(list)
        for title, list_name in entries:
            by_category[list_name].append(title)

        lines = [date_tag, ""]
        for category, titles in by_category.items():
            task_lines = "\n".join(f"- {t}" if t.endswith(".") else f"- {t}." for t in titles)
            lines.append(f"<b>{category}</b>")
            lines.append(f"<blockquote>{task_lines}</blockquote>")
            lines.append("")

        text = "\n".join(lines).strip()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": channel_id, "text": text, "parse_mode": "HTML"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return True
                    logger.error(f"Goal Tracker post failed: {data.get('description')}")
                    return False
        except Exception as e:
            logger.error(f"Failed to post to Goal Tracker channel: {e}")
            return False


def notify_sync(config: dict, text: str) -> None:
    notifier = Notifier(config)
    if notifier.enabled:
        asyncio.run(notifier.send(text))


async def notify_auth_error(notifier: "Notifier", service: str, error: Exception) -> None:
    """Send auth error notification with 12h cooldown to avoid spam."""
    import time
    from pathlib import Path
    flag_file = Path.home() / ".sba" / "locks" / f"auth_error_{service}.flag"
    flag_file.parent.mkdir(parents=True, exist_ok=True)

    # Cooldown: don't spam if already notified in last 12 hours
    if flag_file.exists():
        last_sent = flag_file.stat().st_mtime
        if time.time() - last_sent < 43200:  # 12 hours
            logger.warning(f"Auth error for {service} (notification suppressed by cooldown)")
            return

    flag_file.write_text(str(time.time()))
    msg = (
        f"🔐 <b>Авторизация слетела: {service}</b>\n\n"
        f"<code>{error}</code>\n\n"
        f"Запусти в терминале:\n"
        f"<code>cd ~/Desktop/second-brain-agent-v2 &amp;&amp; CLAUDECODE=\"\" .venv/bin/sba auth google</code>\n\n"
        f"После авторизации {service} продолжит работу автоматически."
    )
    await notifier.send(msg)
