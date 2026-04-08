"""
Telegram bot handlers for SBA 2.0.

Conversational interface — no /commands for the user.
Only technical callbacks: ✅/❌ for deletion confirmations.

Message flow:
  Text  → Main Agent (with chat history for context)
  Voice → mlx-whisper transcription → Main Agent (same path as text)
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

# Pending bank statement transactions awaiting user confirmation: chat_id → list[dict]
_pending_statements: dict[int, list] = {}

# Filename keywords that indicate a bank statement PDF
_STATEMENT_KEYWORDS = ["выписка", "statement", "kaspi", "каспи", "halyk", "халык", "freedom", "deposit", "депозит"]


def setup(config: dict) -> None:
    import os
    global _config, _owner_chat_id
    _config = config
    _owner_chat_id = int(config.get("owner", {}).get("telegram_chat_id", 0))
    # Ensure homebrew binaries (ffmpeg etc.) are available when running under launchd
    homebrew = "/opt/homebrew/bin"
    if homebrew not in os.environ.get("PATH", ""):
        os.environ["PATH"] = homebrew + ":" + os.environ.get("PATH", "")


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


# ── Shared agent runner ───────────────────────────────────────────────────────

async def _run_agent(message: Message, text: str, status_msg) -> None:
    """Common logic for calling Main Agent. Used by text and voice handlers."""
    chat_id = message.chat.id
    if chat_id not in _chat_history:
        _chat_history[chat_id] = deque(maxlen=5)
    history = _chat_history[chat_id]
    context = "\n".join(f"{r}: {t}" for r, t in history)
    full_message = f"{context}\nuser: {text}" if context else text

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

        if len(result) > 4000:
            result = result[:3900] + "\n\n[сообщение обрезано, запроси детали отдельно]"

        await status_msg.edit_text(result, parse_mode=None)

        history.append(("user", text))
        history.append(("assistant", result[:200]))

    except asyncio.TimeoutError:
        await status_msg.edit_text("Запрос занял слишком много времени. Попробуй упростить.")
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        await status_msg.edit_text("Что-то пошло не так. Попробуй ещё раз или проверь /log")


# ── Text input → Main Agent ───────────────────────────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: Message) -> None:
    if not _is_owner(message):
        return
    text = message.text.strip()
    if not text:
        return
    status_msg = await message.answer("⏳ Обрабатываю...")
    await _run_agent(message, text, status_msg)


# ── Voice input → mlx-whisper → Main Agent ───────────────────────────────────

@router.message(F.voice)
async def handle_voice_input(message: Message, bot: Bot) -> None:
    if not _is_owner(message):
        return

    status_msg = await message.answer("🎙 Распознаю речь...")

    ogg_path = None
    try:
        tg_file = await bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            ogg_path = Path(tmp.name)
        await bot.download_file(tg_file.file_path, destination=str(ogg_path))

        import mlx_whisper
        initial_prompt = (
            "Kaspi, Халык, Halyk, Freedom, RBK, Тайяб, ОтбасыБанк, SmartThings, "
            "тенге, тиын, ₸, тысяч тенге, миллион тенге,"
            "садака, закят, нисаб, рассрочка, депозит, транш, "
            "Toyota Prado, RAV4, Toyota RAV4, "
            "ВРЦ, реабилитационный центр, "
            "основной счёт, второй счёт, бизнес счёт, "
            "расход, доход, перевод, оплата, долг, кредит."
        )
        result = await asyncio.to_thread(
            mlx_whisper.transcribe,
            str(ogg_path),
            path_or_hf_repo="mlx-community/whisper-small-mlx",
            initial_prompt=initial_prompt,
        )

        text = result.get("text", "").strip()
        if not text:
            await status_msg.edit_text("❌ Не удалось распознать речь")
            return

        await status_msg.edit_text(f"🎙 <i>{text}</i>\n\n⏳ Обрабатываю...", parse_mode="HTML")
        await _run_agent(message, text, status_msg)

    except Exception as e:
        logger.error(f"Voice transcription error: {e}", exc_info=True)
        await status_msg.edit_text("❌ Ошибка распознавания речи")
    finally:
        if ogg_path:
            ogg_path.unlink(missing_ok=True)


# ── File / photo input ────────────────────────────────────────────────────────

def _is_bank_statement(file_name: str, mime_type: str) -> bool:
    """Return True if the file looks like a bank statement PDF."""
    if mime_type != "application/pdf":
        return False
    fn = file_name.lower()
    return any(k in fn for k in _STATEMENT_KEYWORDS)


def _detect_account_from_filename(file_name: str) -> str | None:
    """Guess account_id from filename."""
    fn = file_name.lower()
    if any(k in fn for k in ["депозит", "deposit", "d1000"]):
        return "account_2"
    if any(k in fn for k in ["freedom"]):
        return "account_3"
    if any(k in fn for k in ["halyk", "халык"]):
        return "account_4"
    if any(k in fn for k in ["gold", "kaspi", "каспи"]):
        return "account_main"
    return None


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

        # Bank statement PDF → parse and import instead of uploading to Drive
        if _is_bank_statement(file_name, mime_type):
            await _handle_bank_statement(message, bot, tmp_path, file_name)
            return

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


async def _handle_bank_statement(message: Message, bot: Bot, tmp_path: Path, file_name: str) -> None:
    """Parse a bank statement PDF and offer to import transactions."""
    status_msg = await message.answer("🏦 Читаю банковскую выписку...")
    try:
        import anthropic
        import base64
        import json

        pdf_bytes = tmp_path.read_bytes()
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

        account = _detect_account_from_filename(file_name)
        account_hint = f"\nСчёт из имени файла: {account}" if account else ""
        accounts_info = (
            "account_main=Kaspi основной, account_2=Kaspi Депозит, "
            "account_3=Freedom Bank, account_4=Halyk, account_5=RBK/Tayyab, account_biz=Kaspi Business"
        )

        client = anthropic.Anthropic(api_key=_config.get("anthropic", {}).get("api_key", ""))
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Извлеки все транзакции из этой банковской выписки.{account_hint}\n"
                            f"Счета: {accounts_info}\n\n"
                            "Верни ТОЛЬКО JSON массив без пояснений:\n"
                            '[{"tx_date":"2026-04-01","amount":5000.0,"tx_type":"expense",'
                            '"category":"еда","description":"Название операции","account":"account_main"},...]\n\n'
                            "Правила:\n"
                            "- tx_type: expense (расход), income (доход), transfer (перевод между своими счетами)\n"
                            "- Переводы между своими счетами → transfer\n"
                            "- Зарплата/поступления → income\n"
                            "- amount: всегда положительное число\n"
                            "- Категории: еда, транспорт, кафе, коммуналка, интернет, подписки, "
                            "здоровье, красота, одежда, развлечения, сбережения, кредиты, налоги, "
                            "дом, переводы людям, подарки, садака, разное"
                        ),
                    },
                ],
            }],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        transactions = json.loads(raw)
        if not transactions:
            await status_msg.edit_text("❌ Транзакции не найдены в файле")
            return

        _pending_statements[message.chat.id] = transactions

        total_exp = sum(t["amount"] for t in transactions if t["tx_type"] == "expense")
        total_inc = sum(t["amount"] for t in transactions if t["tx_type"] == "income")
        total_tr = sum(t["amount"] for t in transactions if t["tx_type"] == "transfer")

        preview_lines = [
            f"🏦 <b>Выписка распознана: {len(transactions)} операций</b>\n",
            f"📤 Расходы: {total_exp:,.0f} ₸" if total_exp else "",
            f"📥 Доходы: {total_inc:,.0f} ₸" if total_inc else "",
            f"↔️ Переводы: {total_tr:,.0f} ₸" if total_tr else "",
            "",
            "<b>Первые 10 операций:</b>",
        ]
        preview_lines = [l for l in preview_lines if l != ""]

        for t in transactions[:10]:
            sign = "−" if t["tx_type"] == "expense" else ("+" if t["tx_type"] == "income" else "↔")
            desc = t.get("description", "")[:35]
            preview_lines.append(f"  {t['tx_date']} {sign}{t['amount']:,.0f} ₸ {desc}")

        if len(transactions) > 10:
            preview_lines.append(f"  ... и ещё {len(transactions) - 10}")

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"✅ Импортировать {len(transactions)} операций",
                callback_data="stmt_confirm",
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data="stmt_cancel"),
        ]])

        await status_msg.edit_text("\n".join(preview_lines), reply_markup=kb)

    except Exception as e:
        logger.error(f"Bank statement parsing failed: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Не удалось распознать выписку: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


@router.callback_query(F.data == "stmt_confirm")
async def callback_stmt_confirm(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    await callback.answer()
    chat_id = callback.from_user.id
    transactions = _pending_statements.pop(chat_id, None)
    if not transactions:
        await callback.message.edit_text("⚠️ Данные устарели — отправь выписку заново")
        return
    try:
        await callback.message.edit_text("⏳ Импортирую...")
        from sba.db import Database, get_db_path
        async with Database(get_db_path(_config)) as db:
            for t in transactions:
                await db.fin_add_transaction(
                    account=t.get("account", "account_main"),
                    amount=float(t["amount"]),
                    tx_type=t["tx_type"],
                    category=t.get("category", "разное"),
                    description=t.get("description", ""),
                    tx_date=t.get("tx_date"),
                )
        await callback.message.edit_text(
            f"✅ Импортировано {len(transactions)} операций.\n"
            "Если нужно — обнови остатки на счетах: напиши «баланс основного X»"
        )
    except Exception as e:
        logger.error(f"Statement import failed: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Ошибка импорта: {e}")


@router.callback_query(F.data == "stmt_cancel")
async def callback_stmt_cancel(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    await callback.answer()
    _pending_statements.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ Импорт отменён")


# ── Folder indexing callbacks ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("folder_deep:"))
async def callback_folder_deep(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed (expired?): {e}")
    reg_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        row = await db.get_file_by_id(reg_id)
        if not row:
            try:
                await callback.message.edit_text("⚠️ Запись не найдена")
            except Exception as e:
                logger.warning(f"callback_folder_deep: record {reg_id} not found, edit_text failed: {e}")
            return
        if row.get("status") not in ("pending_decision", "pending_deep"):
            return
        await db.set_folder_status_by_id(reg_id, "pending_deep")
        try:
            await callback.message.edit_text(
                f"📂 <b>{row['title']}</b>\n✅ Добавлено в очередь — обработаю при следующем запуске"
            )
        except Exception as e:
            logger.warning(f"callback_folder_deep edit_text failed: {e}")


@router.callback_query(F.data.startswith("folder_summary:"))
async def callback_folder_summary(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed (expired?): {e}")
    reg_id = int(callback.data.split(":")[1])

    async with Database(get_db_path(_config)) as db:
        row = await db.get_file_by_id(reg_id)
    if not row:
        try:
            await callback.message.edit_text("⚠️ Запись не найдена")
        except Exception as e:
            logger.warning(f"callback_folder_summary: record {reg_id} not found, edit_text failed: {e}")
        return
    if row.get("status") == "folder_summary":
        return

    try:
        await callback.message.edit_text("⏳ Создаю саммари...")
    except Exception as e:
        logger.warning(f"callback_folder_summary edit_text failed: {e}")
        return

    try:
        import hashlib
        async with Database(get_db_path(_config)) as db:
            row = await db.get_file_by_id(reg_id)
            if not row:
                await callback.message.edit_text("⚠️ Запись не найдена")
                return

            title = row["title"]
            path = row["path"] or title
            source_id = row["source_id"]

            # Run blocking Drive + Haiku calls in thread
            result = await asyncio.to_thread(
                _blocking_create_summary, _config, source_id, title, path
            )

            if result:
                file_id, summary_text, web_link = result
                # Register summary file as already processed (prevent inbox re-processing)
                c_hash = hashlib.sha256(summary_text.encode()).hexdigest()
                summary_reg_id, _ = await db.upsert_file(
                    source="gdrive", source_id=file_id,
                    content_hash=c_hash, title="_sba_summary.md",
                    path=web_link,
                )
                await db.update_file_status(summary_reg_id, "processed")
                # Index in FTS5
                await db.index_content(
                    source_id=file_id, source_type="gdrive",
                    title=f"Саммари: {title}", content=summary_text,
                )

            await db.set_folder_status_by_id(reg_id, "folder_summary")
            await callback.message.edit_text(
                f"📝 <b>{title}</b>\n✅ Саммари создан и добавлен в базу знаний"
            )
    except Exception as e:
        logger.error(f"callback_folder_summary failed: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Ошибка создания саммари: {e}")


def _blocking_create_summary(config: dict, folder_id: str, title: str, path: str) -> tuple:
    """Sync: list folder, call Haiku, create _sba_summary.md in Drive. Returns (file_id, text, link)."""
    import anthropic
    from sba.integrations.google_drive import build_service, list_folder_contents, create_summary_file

    service = build_service(config)
    items = list(list_folder_contents(service, folder_id, False))

    lines = []
    for item in items[:30]:
        prefix = "📁" if item.get("mimeType") == "application/vnd.google-apps.folder" else "📄"
        lines.append(f"{prefix} {item.get('name', '')}")
    if len(items) > 30:
        lines.append(f"... и ещё {len(items) - 30} элементов")
    listing = "\n".join(lines)

    prompt = (
        f"Создай краткое саммари для папки в системе личных знаний.\n\n"
        f"Папка: {title}\nПуть: {path}\nСодержимое:\n{listing}\n\n"
        f"Напиши markdown файл с разделами: # [название], ## Путь, ## Содержимое (список), "
        f"## Описание (2-3 предложения что тут хранится и зачем). Только markdown."
    )

    client = anthropic.Anthropic(
        api_key=config.get("anthropic", {}).get("api_key", ""),
        timeout=30.0,
    )
    model = config.get("classifier", {}).get("model", "claude-haiku-4-5-20251001")
    response = client.messages.create(
        model=model, max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    summary_text = response.content[0].text.strip()

    file_info = create_summary_file(service, folder_id, summary_text)
    return file_info["id"], summary_text, file_info.get("webViewLink", "")


# ── Media acknowledgement callback ───────────────────────────────────────────

@router.callback_query(F.data.startswith("media_ack:"))
async def callback_media_ack(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed (expired?): {e}")
    reg_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        await db.set_folder_status_by_id(reg_id, "folder_done")
        try:
            original = callback.message.html_text or callback.message.text or ""
            await callback.message.edit_text(
                original + "\n\n✅ Ознакомлен — повторных уведомлений не будет",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"callback_media_ack edit_text failed: {e}")


# ── Deletion callbacks ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_del:"))
async def callback_confirm_del(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed (expired?): {e}")
    deletion_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        result = await db.confirm_deletion(deletion_id)
    try:
        if result:
            await callback.message.edit_text(f"✅ Удаление подтверждено: {result.get('title')}")
        else:
            await callback.message.edit_text(f"⚠️ Запись #{deletion_id} не найдена")
    except Exception as e:
        logger.warning(f"callback_confirm_del edit_text failed: {e}")


@router.callback_query(F.data.startswith("cancel_del:"))
async def callback_cancel_del(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed (expired?): {e}")
    deletion_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        await db.cancel_deletion(deletion_id)
    try:
        await callback.message.edit_text(f"❌ Удаление #{deletion_id} отменено — элемент сохранён")
    except Exception as e:
        logger.warning(f"callback_cancel_del edit_text failed: {e}")
