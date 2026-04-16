"""
Telegram bot daemon. Long polling via aiogram 3.x.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from sba.bot import handlers

logger = logging.getLogger(__name__)


async def _send_resume(bot: Bot, resume: dict, config: dict) -> None:
    """After restart, re-run the last user request automatically."""
    import json as _json
    from pathlib import Path as _Path
    _RESUME_FILE = _Path.home() / ".sba" / "bot_resume.json"

    await asyncio.sleep(2)  # let polling settle
    chat_id = resume.get("chat_id")
    text = resume.get("message", "")
    retry_count = resume.get("retry_count", 0)
    if not chat_id or not text:
        return
    from sba.bot.handlers import _run_agent, _MAX_RESUME_RETRIES
    if retry_count > _MAX_RESUME_RETRIES:
        await bot.send_message(
            chat_id,
            f"⚠️ Не удалось выполнить запрос автоматически после {retry_count} попыток.\n\n"
            f"Запрос: <i>{text[:200]}</i>\n\nПопробуй отправить вручную или обратись к разработчику.",
            parse_mode="HTML",
        )
        return
    # Re-write resume file so dev_processor can read retry_count if agent triggers CC.
    # _load_resume() already deleted it — restore it for the duration of this run.
    _RESUME_FILE.write_text(_json.dumps(resume, ensure_ascii=False), encoding="utf-8")
    try:
        status = await bot.send_message(chat_id, f"↩️ Продолжаю выполнение (попытка {retry_count}):\n<i>{text[:200]}</i>")
        await _run_agent(status, text, status, timeout=300)
        # Agent completed without triggering CC — clean up resume
        _RESUME_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Resume failed: {e}", exc_info=True)
        _RESUME_FILE.unlink(missing_ok=True)


async def run_bot(config: dict) -> None:
    """Start the Telegram bot (long polling). Runs indefinitely."""
    token = config.get("telegram_bot", {}).get("token", "")
    if not token or token == "BOT_TOKEN_HERE":
        logger.error("Telegram bot token not configured. Bot will not start.")
        return

    handlers.setup(config)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(handlers.router)

    logger.info("Starting Telegram bot (long polling)...")

    # Check for resume context saved before last restart
    resume = handlers._load_resume()
    if resume:
        asyncio.create_task(_send_resume(bot, resume, config))

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    except Exception as e:
        logger.exception(f"Bot polling failed: {e}")
        raise
    finally:
        await bot.session.close()


def start(config: dict) -> None:
    asyncio.run(run_bot(config))
