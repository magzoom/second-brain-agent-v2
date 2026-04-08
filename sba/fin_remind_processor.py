"""
Daily recurring finance reminders processor for SBA 2.0.

Runs daily at 08:00. Checks fin_recurring table and sends
due reminders to Telegram.
"""

import asyncio
import calendar
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "fin_remind_v2.lock"


async def run(config: dict) -> None:
    from sba.lock import acquire_lock, release_lock
    lock_fd = acquire_lock(LOCK_FILE)
    try:
        await _run(config)
    finally:
        release_lock(lock_fd)


async def _run(config: dict) -> None:
    from sba.db import Database, get_db_path
    from sba.notifier import Notifier

    notifier = Notifier(config)
    db_path = get_db_path(config)
    today = date.today()
    today_day = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    async with Database(db_path) as db:
        due = await db.fin_get_due_recurring(today_day, days_in_month)
        # Save daily balance snapshot for all accounts
        await db.fin_save_all_snapshots(source="auto")
        logger.info("Saved daily balance snapshots for all accounts")

    if not due:
        logger.info("No recurring reminders due today")
        return

    lines = [f"💳 <b>Регулярные платежи — {today.strftime('%d.%m.%Y')}</b>\n"]
    for item in due:
        amount_str = f" — {item['amount']:,.0f} ₸" if item.get("amount") else ""
        if item["day_of_month"] == 0:
            day_str = "ежедневно"
        elif item["day_of_month"] == today_day:
            day_str = f"срок сегодня ({today_day}-е)"
        else:
            days_left = item["day_of_month"] - today_day
            day_str = f"через {days_left} дн. ({item['day_of_month']}-е)"
        lines.append(f"• {item['label']}{amount_str} — {day_str}")

    await notifier.send_message("\n".join(lines))
    logger.info(f"Sent {len(due)} recurring reminders")
