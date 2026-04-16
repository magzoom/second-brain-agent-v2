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
    await asyncio.sleep(2)  # let polling settle
    chat_id = resume.get("chat_id")
    text = resume.get("message", "")
    if not chat_id or not text:
        return
    try:
        status = await bot.send_message(chat_id, f"↩️ Продолжаю выполнение:\n<i>{text[:200]}</i>")
        from sba.bot.handlers import _run_agent
        await _run_agent(status, text, status, timeout=300)
    except Exception as e:
        logger.error(f"Resume failed: {e}", exc_info=True)


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
