"""
Finance quarterly check processor for SBA 2.0.

Runs 4 times a year (1 Jan, 1 Apr, 1 Jul, 1 Oct).
Sends financial summary + zakat status to Telegram.
Asks user to confirm or update data.
"""

import asyncio
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "finance_v2.lock"


async def run(config: dict) -> None:
    from sba.lock import acquire_lock, release_lock, wait_if_dev_active
    if not wait_if_dev_active():
        return
    lock_fd = acquire_lock(LOCK_FILE)
    try:
        await _run(config)
    finally:
        release_lock(lock_fd)


async def _run(config: dict) -> None:
    from sba.db import Database, get_db_path
    from sba.notifier import Notifier
    from sba import finance as fin

    notifier = Notifier(config)
    db_path = get_db_path(config)

    async with Database(db_path) as db:
        accounts = await db.fin_get_accounts()
        liabilities = await db.fin_get_liabilities()
        zakat = await fin.calculate_zakat_status(db)

    today = date.today().strftime("%d.%m.%Y")

    # Build accounts block
    acc_lines = "\n".join(
        f"  {a['label']}: {a['balance']:,.0f} ₸"
        for a in accounts
    )
    total_cash = sum(a["balance"] for a in accounts if a["balance"] > 0)

    # Build liabilities block
    lib_lines = []
    for l in liabilities:
        line = f"  {l['creditor'] or l['name']}: {l['amount']:,.0f} ₸"
        if l.get("monthly_payment"):
            line += f" (ежемес. {l['monthly_payment']:,.0f} ₸)"
        if l.get("due_date"):
            line += f" до {l['due_date']}"
        lib_lines.append(line)
    total_liabilities = sum(l["amount"] for l in liabilities)

    # Zakat status
    if zakat["obligatory"]:
        zakat_line = f"⚠️ ЗАКЯТ ОБЯЗАТЕЛЕН — {zakat['amount_due']:,.0f} ₸ (2.5%)"
    else:
        zakat_line = f"✅ Закят не обязателен — {zakat['reason']}"

    stale_warning = "\n⚠️ <i>Курс золота недоступен (Yahoo Finance), использован устаревший fallback 80 000 ₸/г</i>" if zakat.get("price_is_stale") else ""

    message = (
        f"📊 <b>Квартальный финансовый отчёт</b> — {today}\n\n"
        f"<b>Счета:</b>\n{acc_lines}\n"
        f"Итого: <b>{total_cash:,.0f} ₸</b>\n\n"
        f"<b>Обязательства:</b>\n" + "\n".join(lib_lines) + "\n"
        f"Итого: <b>{total_liabilities:,.0f} ₸</b>\n\n"
        f"<b>Чистые активы:</b> {zakat['net_assets']:,.0f} ₸\n\n"
        f"<b>Закят (нисаб по золоту, 85г = {zakat['nisab_kzt']:,.0f} ₸):</b>\n"
        f"{zakat_line}{stale_warning}\n\n"
        f"Если что-то изменилось — напиши мне об этом (баланс счёта, новый долг, погашение и т.д.)."
    )

    await notifier.send_message(message)
    logger.info("Quarterly finance report sent")
