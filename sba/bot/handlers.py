"""
Telegram bot handlers for SBA 2.0.

Conversational interface — no /commands for the user.
Only technical callbacks: ✅/❌ for deletion confirmations.

Message flow:
  Text  → Main Agent (with chat history for context)
  File/Photo → Upload to Google Drive Inbox → answer

Chat history kept in-memory (last 5 messages per chat).
"""

import asyncio
import logging
import tempfile
from collections import deque
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from sba.db import Database, get_db_path

logger = logging.getLogger(__name__)

router = Router()

_config: dict = {}
_owner_chat_id: int = 0

# Short-term conversation memory: chat_id → deque of (role, text)
_chat_history: dict[int, deque] = {}


def setup(config: dict) -> None:
    global _config, _owner_chat_id
    _config = config
    _owner_chat_id = int(config.get("owner", {}).get("telegram_chat_id", 0))


def _is_owner(message: Message) -> bool:
    return message.chat.id == _owner_chat_id


def _is_owner_callback(callback: CallbackQuery) -> bool:
    return callback.from_user.id == _owner_chat_id


# ── /start (minimal — system info only) ──────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer(
        "👋 <b>Second Brain Agent 2.0</b>\n\n"
        "Пиши мне свободным текстом — я сам разберусь что делать.\n\n"
        "Примеры:\n"
        "• «Что у меня сегодня?»\n"
        "• «Напомни позвонить врачу в пятницу»\n"
        "• «Найди мои заметки про ВРЦ»\n"
        "• «Изучи тему ИИ в медицине»\n\n"
        "Файлы и фото — пересылай прямо сюда, попадут в очередь обработки."
    )


# ── /status ───────────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_owner(message):
        return
    try:
        async with Database(get_db_path(_config)) as db:
            stats = await db.get_stats()
    except Exception as e:
        await message.answer(f"❌ Не удалось получить статистику: {e}")
        return

    files = stats.get("files", {})
    total = sum(files.values())
    processed = files.get("processed", 0)
    pending = files.get("pending", 0)
    deletions = stats.get("pending_deletions", 0)

    await message.answer(
        f"📊 <b>SBA 2.0 Статус</b>\n\n"
        f"📋 Всего элементов: {total}\n"
        f"✅ Обработано: {processed}\n"
        f"⏳ Ожидают: {pending}\n"
        f"🗑 Ожидают удаления: {deletions}"
    )


# ── /log ──────────────────────────────────────────────────────────────────────

@router.message(Command("log"))
async def cmd_log(message: Message) -> None:
    if not _is_owner(message):
        return
    from sba.service_manager import get_log_path
    log_file = Path(get_log_path("bot"))
    if not log_file.exists():
        await message.answer("📭 Лог-файл не найден")
        return
    try:
        lines = log_file.read_text().splitlines()
        text = "\n".join(lines[-20:]) or "(пусто)"
        await message.answer(f"<pre>{text[:3000]}</pre>")
    except Exception as e:
        await message.answer(f"❌ Не удалось прочитать лог: {e}")


# ── Text input → Main Agent ───────────────────────────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: Message) -> None:
    if not _is_owner(message):
        return

    text = message.text.strip()
    if not text:
        return

    chat_id = message.chat.id

    # Build context from history
    if chat_id not in _chat_history:
        _chat_history[chat_id] = deque(maxlen=5)
    history = _chat_history[chat_id]
    context = "\n".join(f"{r}: {t}" for r, t in history)
    full_message = f"{context}\nuser: {text}" if context else text

    status_msg = await message.answer("⏳ Обрабатываю...")

    try:
        from sba.notifier import Notifier
        from sba.db import Database, get_db_path
        from sba import agent as main_agent

        notifier = Notifier(_config)
        db_path = get_db_path(_config)

        async with Database(db_path) as db:
            result = await asyncio.wait_for(
                main_agent.run_main_agent(full_message, db=db, notifier=notifier, config=_config),
                timeout=180,
            )

        result = result or "Готово."

        # Truncate if too long
        if len(result) > 4000:
            result = result[:3900] + "\n\n_[сообщение обрезано, запроси детали отдельно]_"

        await status_msg.edit_text(result)

        # Update history
        history.append(("user", text))
        history.append(("assistant", result[:200]))

    except asyncio.TimeoutError:
        await status_msg.edit_text("Запрос занял слишком много времени. Попробуй упростить.")
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        await status_msg.edit_text("Что-то пошло не так. Попробуй ещё раз или проверь /log")


# ── File / photo input ────────────────────────────────────────────────────────

@router.message(F.document | F.photo)
async def handle_file_input(message: Message, bot: Bot) -> None:
    if not _is_owner(message):
        return

    await message.answer("⏳ Получаю файл...")

    try:
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or "attachment"
            mime_type = message.document.mime_type or "application/octet-stream"
        else:
            file_id = message.photo[-1].file_id
            file_name = "photo.jpg"
            mime_type = "image/jpeg"

        tg_file = await bot.get_file(file_id)
        with tempfile.NamedTemporaryFile(suffix=Path(file_name).suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        await bot.download_file(tg_file.file_path, destination=str(tmp_path))

        try:
            from sba.integrations.google_drive import build_service, upload_file
            inbox_folder_id = _config.get("google_drive", {}).get("inbox_folder_id", "")
            service = await asyncio.to_thread(build_service, _config)
            drive_file = await asyncio.to_thread(upload_file, service, tmp_path, file_name, mime_type, inbox_folder_id)
            drive_link = drive_file.get("webViewLink", "")
            await message.answer(
                f"☁️ <b>Добавлено в очередь обработки</b>\n"
                f"📎 {file_name}\n"
                f"🔗 <a href='{drive_link}'>Открыть в Drive</a>\n\n"
                f"Будет обработан при следующем запуске inbox."
            )
        except Exception as drive_err:
            logger.error(f"Drive upload failed: {drive_err}")
            await message.answer(f"⚠️ Не удалось загрузить в Drive: {drive_err}")
        finally:
            tmp_path.unlink(missing_ok=True)

    except Exception as e:
        logger.exception(f"handle_file_input failed: {e}")
        await message.answer(f"❌ Ошибка: {e}")


# ── Deletion callbacks ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_del:"))
async def callback_confirm_del(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return

    deletion_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        result = await db.confirm_deletion(deletion_id)

    if result:
        await callback.message.edit_text(f"✅ Удаление подтверждено: {result.get('title')}")
    else:
        await callback.message.edit_text(f"⚠️ Запись #{deletion_id} не найдена")
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_del:"))
async def callback_cancel_del(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return

    deletion_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        await db._conn.execute(
            "UPDATE pending_deletions SET status='cancelled' WHERE id=?", (deletion_id,)
        )
        await db._conn.commit()

    await callback.message.edit_text(f"❌ Удаление #{deletion_id} отменено — элемент сохранён")
    await callback.answer()
