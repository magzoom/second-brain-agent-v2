"""
Daily recurring finance reminders processor for SBA 2.0.

Morning run (08:00): balance snapshot + recurring reminders + weekly forecast (Mondays).
Evening run (21:00): check-in reminder to log today's transactions.
"""

import asyncio
import calendar
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "fin_remind_v2.lock"

# Categories excluded from variable spending forecasts (irregular/uncontrollable)
_FORECAST_EXCLUDED = {"переводы людям", "подарки", "долги", "семья"}


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
    now = datetime.now()
    today = now.date()
    is_evening = now.hour >= 18

    async with Database(db_path) as db:
        if is_evening:
            await _send_evening_checkin(db, notifier, today)
            # Weekly forecast on Sunday evening
            if today.weekday() == 6:
                forecast = await _generate_weekly_forecast(db, today)
                if forecast:
                    await notifier.send_message(forecast)
                    logger.info("Sent weekly forecast (Sunday evening)")
            return

        # ── Morning run ───────────────────────────────────────────────────────
        today_day = today.day
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        month_str = today.strftime("%Y-%m")

        due = await db.fin_get_due_recurring(today_day, days_in_month, current_month=month_str)
        await db.fin_save_all_snapshots(source="auto")
        logger.info("Saved daily balance snapshots for all accounts")

        if due:
            unpaid = []
            maybe_paid = []

            for item in due:
                if item["day_of_month"] == 0:
                    # Daily payments (e.g. садака) — never check transactions, always show
                    unpaid.append((item, None))
                    continue
                matches = await db.fin_find_matching_transactions(
                    item["label"], item.get("amount"), month_str
                )
                if matches:
                    maybe_paid.append((item, matches))
                else:
                    unpaid.append((item, None))

            # Send regular reminder for unpaid items
            if unpaid:
                lines = [f"💳 <b>Регулярные платежи — {today.strftime('%d.%m.%Y')}</b>\n"]
                for item, _ in unpaid:
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

            # Send individual check messages for maybe-paid items
            for item, matches in maybe_paid:
                await _send_paid_check(notifier, item, matches, today_day)

            logger.info(
                f"Recurring reminders: {len(unpaid)} unpaid, {len(maybe_paid)} maybe-paid"
            )
        else:
            logger.info("No recurring reminders due today")



async def _send_paid_check(notifier, item: dict, matches: list, today_day: int) -> None:
    """Send a message asking if a recurring payment was already made."""
    amount_str = f" — {item['amount']:,.0f} ₸" if item.get("amount") else ""
    dom = item["day_of_month"]
    if dom == today_day:
        day_str = f"срок сегодня ({today_day}-е)"
    else:
        days_left = dom - today_day
        day_str = f"через {days_left} дн. ({dom}-е)"

    # Show up to 2 matching transactions as context
    tx_lines = []
    for tx in matches[:2]:
        tx_date = tx.get("tx_date", "")
        tx_amt = abs(tx.get("amount") or 0)
        tx_desc = (tx.get("description") or "")[:40]
        tx_lines.append(f"  <code>{tx_date}  {tx_amt:,.0f} ₸  {tx_desc}</code>")

    tx_block = "\n".join(tx_lines)
    text = (
        f"❓ <b>{item['label']}</b>{amount_str} — {day_str}\n\n"
        f"Нашёл похожую транзакцию за этот месяц:\n{tx_block}\n\n"
        "Это оплата данного платежа?"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Да, оплачено", "callback_data": f"recur_paid:{item['id']}"},
        {"text": "❌ Нет, не оплачено", "callback_data": f"recur_unpaid:{item['id']}"},
    ]]}
    await notifier.send_message(text, reply_markup=keyboard)


async def _send_evening_checkin(db, notifier, today: date) -> None:
    """21:00 — remind to log today's transactions."""
    today_str = today.strftime("%Y-%m-%d")
    rows = await db.fin_get_today_transactions(today_str)
    count = len(rows)
    total_expense = sum(r["amount"] for r in rows if r["tx_type"] == "expense")
    total_income = sum(r["amount"] for r in rows if r["tx_type"] == "income")

    if count == 0:
        text = (
            f"📝 <b>Вечерний чек-ин — {today.strftime('%d.%m.%Y')}</b>\n\n"
            "Сегодня ещё нет записей о тратах.\n"
            "Не забудь внести расходы, доходы или переводы за день.\n\n"
            "<i>Примеры: «потратил 5000 на еду», «перевёл 20 000 с основного на депозит»</i>"
        )
    else:
        parts = []
        if total_expense:
            parts.append(f"расходы: {total_expense:,.0f} ₸")
        if total_income:
            parts.append(f"доходы: {total_income:,.0f} ₸")
        summary = " / ".join(parts) if parts else f"{count} операций"
        text = (
            f"📝 <b>Вечерний чек-ин — {today.strftime('%d.%m.%Y')}</b>\n\n"
            f"Записано {count} операций ({summary}).\n"
            "Если что-то пропустил — добавляй сейчас."
        )

    await notifier.send_message(text)
    logger.info(f"Sent evening check-in: {count} transactions today")


async def _generate_weekly_forecast(db, today: date) -> str | None:
    """Weekly + month-end forecast. Returns formatted HTML string or None."""
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining_days = days_in_month - today.day
    if remaining_days <= 0:
        return None

    months_of_data = await db.fin_count_months_with_data()
    if months_of_data < 1:
        return None

    month_str = today.strftime("%Y-%m")

    upcoming_fixed = await db.fin_get_upcoming_recurring(today.day, days_in_month)
    fixed_total = sum(r["amount"] or 0 for r in upcoming_fixed)

    variable_avg = await db.fin_get_avg_variable_spend(_FORECAST_EXCLUDED)
    spent_this_month = await db.fin_get_month_variable_spend(month_str, _FORECAST_EXCLUDED)
    total_balance = await db.fin_get_total_balance()

    avg_daily = variable_avg / 30 if variable_avg else 0
    variable_forecast = avg_daily * remaining_days
    total_forecast = fixed_total + variable_forecast
    projected_balance = total_balance - total_forecast

    lines = [f"📊 <b>Прогноз до конца месяца</b> (осталось {remaining_days} дн.)\n"]

    if upcoming_fixed:
        lines.append("📌 <b>Фиксированные платежи:</b>")
        for r in upcoming_fixed[:8]:
            amt = f"{r['amount']:,.0f} ₸" if r.get("amount") else "—"
            lines.append(f"  • {r['label']} ({r['day_of_month']}-е) — {amt}")
        if len(upcoming_fixed) > 8:
            lines.append(f"  • ... и ещё {len(upcoming_fixed) - 8}")
        lines.append(f"  <b>Итого:</b> {fixed_total:,.0f} ₸")

    if avg_daily > 0:
        data_note = " <i>(мало данных)</i>" if months_of_data < 3 else ""
        lines.append(f"\n📈 <b>Переменные (оценка){data_note}:</b> ~{variable_forecast:,.0f} ₸")
        lines.append(f"  Уже потрачено в этом месяце: {spent_this_month:,.0f} ₸")

    lines.append(f"\n💰 <b>Остаток сейчас:</b> {total_balance:,.0f} ₸")
    lines.append(f"💸 <b>Прогноз на конец месяца:</b> {projected_balance:,.0f} ₸")

    return "\n".join(lines)
