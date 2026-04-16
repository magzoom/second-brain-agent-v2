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


_RESUME_FILE = Path.home() / ".sba" / "bot_resume.json"


def _save_resume(chat_id: int, message_text: str) -> None:
    """Save pending resume context before bot restart."""
    import json, time
    _RESUME_FILE.write_text(json.dumps({
        "chat_id": chat_id,
        "message": message_text,
        "ts": time.time(),
    }), encoding="utf-8")


def _load_resume() -> dict | None:
    """Load and delete resume context on startup."""
    import json
    if not _RESUME_FILE.exists():
        return None
    try:
        data = json.loads(_RESUME_FILE.read_text(encoding="utf-8"))
        _RESUME_FILE.unlink()
        return data
    except Exception:
        _RESUME_FILE.unlink(missing_ok=True)
        return None


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

_STATEMENT_CONTENT_KEYWORDS = [
    "выписка", "statement", "по счёту", "по карте",
    "halyk", "халык", "kaspi", "каспи", "freedom", "rbk", "отбасы",
    "ибн", "иин", "бин", "остаток", "пополнение", "списание",
]

_BANK_HINTS = {
    "halyk": "account_4",
    "халык": "account_4",
    "freedom": "account_3",
    "kaspi": "account_main",
    "каспи": "account_main",
    "rbk": "account_5",
    "отбасы": "account_otbasy",
    "отбасыбанк": "account_otbasy",
}


def _peek_pdf_text(path: Path, max_chars: int = 2000) -> str:
    """Extract first max_chars of text from a PDF using pdfminer."""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(path), maxpages=2)
        return (text or "")[:max_chars].lower()
    except Exception:
        return ""


def _is_bank_statement(file_name: str, mime_type: str, file_path: Path | None = None) -> bool:
    """Return True if the file looks like a bank statement (PDF or TXT)."""
    if mime_type not in ("application/pdf", "text/plain"):
        return False
    fn = file_name.lower()
    if any(k in fn for k in _STATEMENT_KEYWORDS):
        return True
    # Filename gave no clue — peek into PDF content
    if mime_type == "application/pdf" and file_path:
        text = _peek_pdf_text(file_path)
        matches = sum(1 for k in _STATEMENT_CONTENT_KEYWORDS if k in text)
        return matches >= 3
    return False


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


def _detect_account_from_content(text: str) -> str | None:
    """Guess account_id from PDF text content."""
    for keyword, account in _BANK_HINTS.items():
        if keyword in text:
            return account
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
        if _is_bank_statement(file_name, mime_type, tmp_path):
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

        account = _detect_account_from_filename(file_name)
        if not account:
            pdf_text = _peek_pdf_text(tmp_path)
            account = _detect_account_from_content(pdf_text)
        account_hint = f"\nСчёт: {account}" if account else ""
        accounts_info = (
            "account_main=Kaspi основной, account_2=Kaspi Депозит, "
            "account_3=Freedom Bank, account_4=Halyk, account_5=RBK/Tayyab, "
            "account_biz=Kaspi Business, account_otbasy=ОтбасыБанк"
        )
        _card_labels = {
            "account_main": "Kaspi основной",
            "account_2": "Kaspi Депозит",
            "account_3": "Freedom Bank",
            "account_4": "Halyk",
            "account_5": "RBK/Tayyab",
            "account_biz": "Kaspi Business",
            "account_otbasy": "ОтбасыБанк",
        }
        _account_cards: dict = _config.get("finance", {}).get("account_cards", {})
        if _account_cards:
            card_lines = "\n".join(
                f"- {acc} ({_card_labels.get(acc, acc)}): {hint}"
                for acc, hint in _account_cards.items()
            )
            card_hints = (
                "Известные реквизиты счетов (используй для определения получателя/отправителя при переводах):\n"
                + card_lines + "\n"
            )
        else:
            card_hints = ""
        extraction_prompt = (
            f"Извлеки все транзакции из этой банковской выписки.{account_hint}\n"
            f"Счета: {accounts_info}\n\n"
            f"{card_hints}\n"
            "Верни ТОЛЬКО JSON массив без пояснений:\n"
            '[{"tx_date":"2026-04-01","amount":5000.0,"tx_type":"expense",'
            '"category":"еда","description":"Название операции","account":"account_main"},...]\n\n'
            "Правила:\n"
            "- tx_type: expense (расход), income (доход), transfer_out (перевод С этого счёта), transfer_in (перевод НА этот счёт)\n"
            "- Переводы между своими счетами (перечислены выше): генерируй ДВЕ РАЗНЫЕ записи:\n"
            "  * ОДНА запись на счёт-источник (откуда ушли деньги): tx_type=transfer_out\n"
            "  * ОДНА запись на счёт-получатель (куда пришли деньги): tx_type=transfer_in\n"
            "  * Обе записи — на РАЗНЫЕ счета, никогда не дублируй на одном счёте\n"
            "  * Если в выписке виден номер карты/IBAN — сопоставь с реквизитами выше для определения второго счёта\n"
            "  * Пример: выписка по account_main, операция 'С Карт Депозита +170,000' →\n"
            "    {account: account_2, tx_type: transfer_out, amount: 170000} +\n"
            "    {account: account_main, tx_type: transfer_in, amount: 170000}\n"
            "- Зарплата/поступления извне → income\n"
            "- amount: всегда положительное число\n"
            "- Категории: еда, транспорт, кафе, коммуналка, интернет, подписки, "
            "здоровье, красота, одежда, развлечения, сбережения, кредиты, налоги, "
            "дом, переводы людям, подарки, садака, разное"
        )

        client = anthropic.Anthropic(api_key=_config.get("anthropic", {}).get("api_key", ""))
        is_txt = file_name.lower().endswith(".txt")

        if is_txt:
            txt_content = tmp_path.read_text(errors="replace")[:30000]
            content = [{"type": "text", "text": f"Выписка:\n\n{txt_content}\n\n{extraction_prompt}"}]
        else:
            pdf_bytes = tmp_path.read_bytes()
            pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": extraction_prompt},
            ]

        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
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
        total_tr = sum(t["amount"] for t in transactions if t["tx_type"] in ("transfer", "transfer_out", "transfer_in"))

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
            inserted = 0
            skipped = 0
            for t in transactions:
                is_dup = await db.fin_transaction_exists(
                    account=t.get("account", "account_main"),
                    tx_date=t.get("tx_date", ""),
                    amount=float(t["amount"]),
                    description=t.get("description", ""),
                )
                if is_dup:
                    skipped += 1
                    continue
                await db.fin_add_transaction(
                    account=t.get("account", "account_main"),
                    amount=float(t["amount"]),
                    tx_type=t["tx_type"],
                    category=t.get("category", "разное"),
                    description=t.get("description", ""),
                    tx_date=t.get("tx_date"),
                )
                inserted += 1

        skip_note = f" ({skipped} дублей пропущено)" if skipped else ""

        # Show current balances of affected accounts
        affected_accounts = {t.get("account", "account_main") for t in transactions}
        async with Database(get_db_path(_config)) as db2:
            rows = await db2.fin_get_accounts()
        _ACCOUNT_LABELS = {
            "account_main": "Kaspi основной",
            "account_2": "Kaspi Депозит",
            "account_3": "Freedom",
            "account_4": "Halyk",
            "account_5": "RBK/Tayyab",
            "account_biz": "Kaspi Business",
            "account_otbasy": "ОтбасыБанк",
        }
        balance_lines = []
        for r in rows:
            if r["name"] in affected_accounts:
                label = _ACCOUNT_LABELS.get(r["name"], r["name"])
                balance_lines.append(f"  {label}: {r['balance']:,.0f} ₸")
        balance_block = "\n".join(balance_lines)
        await callback.message.edit_text(
            f"✅ Импортировано {inserted} операций{skip_note}.\n\n"
            f"Балансы счетов:\n{balance_block}"
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


# ── Recurring payment check callbacks ────────────────────────────────────────

@router.callback_query(F.data.startswith("recur_paid:"))
async def callback_recur_paid(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed: {e}")
    from datetime import date
    recurring_id = int(callback.data.split(":")[1])
    month_str = date.today().strftime("%Y-%m")
    async with Database(get_db_path(_config)) as db:
        await db.fin_mark_recurring_paid(recurring_id, month_str)
        row = await db.fin_get_recurring_by_id(recurring_id)
    label = row["label"] if row else f"#{recurring_id}"
    try:
        await callback.message.edit_text(
            f"✅ <b>{label}</b> — отмечено как оплачено в {month_str}.\n"
            "Напоминание не будет повторяться до следующего месяца.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"callback_recur_paid edit_text failed: {e}")


@router.callback_query(F.data.startswith("recur_unpaid:"))
async def callback_recur_unpaid(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception as e:
        logger.debug(f"callback.answer() failed: {e}")
    recurring_id = int(callback.data.split(":")[1])
    async with Database(get_db_path(_config)) as db:
        row = await db.fin_get_recurring_by_id(recurring_id)
    label = row["label"] if row else f"#{recurring_id}"
    try:
        await callback.message.edit_text(
            f"⚠️ <b>{label}</b> — платёж ещё не проведён.\n"
            "Напоминание останется активным.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"callback_recur_unpaid edit_text failed: {e}")


# ── Inbox suggestion callbacks ───────────────────────────────────────────────

_CATEGORY_KEY_MAP = {
    "1_Health_Energy": "folder_1_health_energy",
    "2_Business_Career": "folder_2_business_career",
    "3_Finance": "folder_3_finance",
    "4_Family_Relationships": "folder_4_family_relationships",
    "5_Personal Growth": "folder_5_personal_growth",
    "6_Brightness life": "folder_6_brightness_life",
    "7_Spirituality": "folder_7_spirituality",
}

_CATEGORY_LABELS = {
    "1_Health_Energy": "🏋 Здоровье",
    "2_Business_Career": "💼 Карьера",
    "3_Finance": "💰 Финансы",
    "4_Family_Relationships": "👨‍👩‍👧 Семья",
    "5_Personal Growth": "📚 Рост",
    "6_Brightness life": "✨ Яркость",
    "7_Spirituality": "🕌 Духовность",
}


async def _do_inbox_move(reg_id: int, category: str) -> str:
    """Move file/folder to category. Returns result message."""
    from sba.integrations.google_drive import build_service, move_file_to_folder
    from sba.integrations import apple_notes as _apple_notes

    async with Database(get_db_path(_config)) as db:
        row = await db.get_file_by_id(reg_id)
        if not row:
            return "⚠️ Запись не найдена"

        source = row["source"]
        source_id = row["source_id"]
        title = row["title"]

        try:
            if source == "gdrive":
                folder_key = _CATEGORY_KEY_MAP.get(category, "")
                folder_id = _config.get("google_drive", {}).get(folder_key, "")
                if not folder_id:
                    return f"⚠️ Папка для категории {category} не настроена в config.yaml"
                service = await asyncio.to_thread(build_service, _config)
                ok = await asyncio.to_thread(move_file_to_folder, service, source_id, folder_id)
                if not ok:
                    return f"❌ Не удалось переместить в Drive"
            elif source == "apple_notes":
                ok = await asyncio.to_thread(_apple_notes.move_note_by_id, source_id, category)
                if not ok:
                    return f"❌ Не удалось переместить заметку"

            await db.update_file_status(reg_id, "processed", category=category)
            cat_label = _CATEGORY_LABELS.get(category, category)
            return f"✅ <b>{title}</b>\n→ {cat_label}"

        except Exception as e:
            logger.error(f"inbox move failed for reg_id={reg_id}: {e}", exc_info=True)
            return f"❌ Ошибка: {e}"


@router.callback_query(F.data.startswith("inbox_ok:"))
async def callback_inbox_ok(callback: CallbackQuery) -> None:
    """User agreed with suggested category — move the item."""
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception:
        pass
    reg_id = int(callback.data.split(":")[1])

    async with Database(get_db_path(_config)) as db:
        row = await db.get_file_by_id(reg_id)

    if not row or not row.get("category"):
        try:
            await callback.message.edit_text("⚠️ Категория не найдена", reply_markup=None)
        except Exception:
            pass
        return

    category = row["category"]
    result = await _do_inbox_move(reg_id, category)
    try:
        await callback.message.edit_text(result, reply_markup=None)
    except Exception as e:
        logger.warning(f"callback_inbox_ok edit_text failed: {e}")


@router.callback_query(F.data.startswith("inbox_other:"))
async def callback_inbox_other(callback: CallbackQuery) -> None:
    """User wants to pick a different category — show all 7."""
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception:
        pass
    reg_id = int(callback.data.split(":")[1])

    from sba.bot.keyboards import inbox_all_categories_keyboard
    try:
        await callback.message.edit_reply_markup(
            reply_markup=inbox_all_categories_keyboard(reg_id)
        )
    except Exception as e:
        logger.warning(f"callback_inbox_other edit_reply_markup failed: {e}")


@router.callback_query(F.data.startswith("inbox_pick:"))
async def callback_inbox_pick(callback: CallbackQuery) -> None:
    """User picked a specific category from the full list."""
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception:
        pass
    # Format: inbox_pick:{reg_id}:{category}  (category may have spaces)
    parts = callback.data.split(":", 2)
    reg_id = int(parts[1])
    category = parts[2]

    result = await _do_inbox_move(reg_id, category)
    try:
        await callback.message.edit_text(result, reply_markup=None)
    except Exception as e:
        logger.warning(f"callback_inbox_pick edit_text failed: {e}")


@router.callback_query(F.data.startswith("inbox_del:"))
async def callback_inbox_del(callback: CallbackQuery) -> None:
    """User chose to delete the inbox item — request deletion confirmation."""
    if not _is_owner_callback(callback):
        return
    try:
        await callback.answer()
    except Exception:
        pass
    reg_id = int(callback.data.split(":")[1])

    async with Database(get_db_path(_config)) as db:
        row = await db.get_file_by_id(reg_id)
        if not row:
            try:
                await callback.message.edit_text("⚠️ Запись не найдена", reply_markup=None)
            except Exception:
                pass
            return

        deletion_id = await db.add_pending_deletion(file_id=reg_id)

    from sba.notifier import Notifier
    notifier = Notifier(_config)
    msg_id = await notifier.send_deletion_request(
        deletion_id=deletion_id,
        item_title=row["title"],
        item_source=row["source"],
        reason="Запрошено из inbox",
    )
    if msg_id:
        async with Database(get_db_path(_config)) as db:
            await db.set_deletion_telegram_msg(deletion_id, msg_id)

    try:
        await callback.message.edit_text(
            f"🗑 <b>{row['title']}</b>\nЗапрос на удаление отправлен.",
            reply_markup=None,
        )
    except Exception as e:
        logger.warning(f"callback_inbox_del edit_text failed: {e}")


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


@router.callback_query(F.data.startswith("ext_ok:"))
async def callback_ext_ok(callback: CallbackQuery) -> None:
    """Execute an approved capability extension."""
    if not _is_owner_callback(callback):
        return
    await callback.answer()
    ext_id = int(callback.data.split(":")[1])

    from sba import extension_registry as _ext_registry
    ext = _ext_registry.get(ext_id)
    if not ext:
        await callback.message.edit_text("⚠️ Запрос устарел или уже выполнен.")
        return

    action = ext.get("action")
    title = ext.get("title", action)

    # Grab last user message from chat history for resume after restart
    chat_id = callback.message.chat.id
    history = _chat_history.get(chat_id, [])
    last_user_msg = next(
        (t for r, t in reversed(list(history)) if r == "user"), None
    )

    def _restart(save_resume: bool = True) -> None:
        import subprocess as sp, os
        if save_resume and last_user_msg:
            _save_resume(chat_id, last_user_msg)
        sp.Popen(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.sba.bot"])

    try:
        if action == "pip_install":
            package = ext.get("package", "").strip()
            import re
            if not package or not re.match(r'^[a-zA-Z0-9._\-\[\]]+$', package):
                await callback.message.edit_text("❌ Недопустимое имя пакета.")
                return
            await callback.message.edit_text(f"⏳ Устанавливаю {package}...")
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", package, "-q"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                await callback.message.edit_text(f"❌ Ошибка установки {package}:\n{result.stderr[:300]}")
                return
            await callback.message.edit_text(
                f"✅ <b>{package}</b> установлен.\n\nПерезапускаю бота, продолжу выполнение запроса..."
            )
            _restart()

        elif action == "add_config_value":
            config_path = ext.get("config_path", "").strip()
            config_value = ext.get("config_value", "").strip()
            if not config_path or not config_value:
                await callback.message.edit_text("❌ Не указан путь или значение конфига.")
                return
            import yaml
            from pathlib import Path as _Path
            cfg_file = _Path.home() / ".sba" / "config.yaml"
            with open(cfg_file) as f:
                cfg = yaml.safe_load(f) or {}
            keys = config_path.split(".")
            node = cfg
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = config_value
            with open(cfg_file, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            await callback.message.edit_text(
                f"✅ Конфиг обновлён: <code>{config_path}</code>\n\nПерезапускаю бота, продолжу выполнение запроса..."
            )
            _restart()

        elif action == "restart_bot":
            await callback.message.edit_text("🔄 Перезапускаю бота...")
            _restart(save_resume=False)

        else:
            await callback.message.edit_text(f"❌ Неизвестное действие: {action}")

    except Exception as e:
        logger.error(f"Extension execution failed: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Ошибка выполнения: {e}")


@router.callback_query(F.data.startswith("ext_deny:"))
async def callback_ext_deny(callback: CallbackQuery) -> None:
    if not _is_owner_callback(callback):
        return
    await callback.answer()
    ext_id = int(callback.data.split(":")[1])
    from sba import extension_registry as _ext_registry
    _ext_registry.get(ext_id)  # clear from registry
    try:
        await callback.message.edit_text("❌ Расширение отменено.")
    except Exception:
        pass


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
