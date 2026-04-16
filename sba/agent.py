"""
Main Agent — SBA 2.0 conversational orchestrator.

Uses Claude Agent SDK with custom MCP tools for Apple/Drive/DB integrations.
Research Agent is a subagent (AgentDefinition) with WebSearch + WebFetch.

Tool handlers access module-level globals (db, notifier, config) set via setup().
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import query, ClaudeAgentOptions, tool, create_sdk_mcp_server
from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage

from sba.db import Database
from sba.notifier import Notifier
from sba.integrations import apple_notes, google_tasks, google_calendar
from sba import research_agent as _research_module
from sba import finance as _finance_module
from sba.security import scan_content
from sba import extension_registry as _ext_registry

logger = logging.getLogger(__name__)

# ── Module-level state (injected via setup()) ─────────────────────────────────

_db: Optional[Database] = None
_notifier: Optional[Notifier] = None
_config: dict = {}

CATEGORIES = [
    "1_Health_Energy", "2_Business_Career", "3_Finance",
    "4_Family_Relationships", "5_Personal Growth", "6_Brightness life", "7_Spirituality",
]


def setup(db: Database, notifier: Notifier, config: dict) -> None:
    """Inject shared state before running the agent."""
    global _db, _notifier, _config
    _db = db
    _notifier = notifier
    _config = config
    _research_module.setup_research(db)


def _category_to_folder_id(category: str) -> str:
    key_map = {
        "1_Health_Energy": "folder_1_health_energy",
        "2_Business_Career": "folder_2_business_career",
        "3_Finance": "folder_3_finance",
        "4_Family_Relationships": "folder_4_family_relationships",
        "5_Personal Growth": "folder_5_personal_growth",
        "6_Brightness life": "folder_6_brightness_life",
        "7_Spirituality": "folder_7_spirituality",
    }
    key = key_map.get(category, "")
    return _config.get("google_drive", {}).get(key, "")


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


async def _update_patterns(category: str) -> None:
    """Update user behaviour patterns. Called after each create/move operation."""
    if not _db:
        return
    try:
        counts = json.loads(await _db.get_pattern("category_counts") or "{}")
        counts[category] = counts.get(category, 0) + 1
        await _db.set_pattern("category_counts", json.dumps(counts))
        top3 = ", ".join(sorted(counts, key=counts.get, reverse=True)[:3])
        await _db.set_pattern("top_categories", top3)
        hour = datetime.now().hour
        hours = json.loads(await _db.get_pattern("active_hours_list") or "[]")
        hours.append(hour)
        hours = hours[-100:]
        if hours:
            avg = sum(hours) // len(hours)
            await _db.set_pattern("active_hours", f"{avg-1}:00–{avg+1}:00")
        await _db.set_pattern("active_hours_list", json.dumps(hours))
    except Exception as e:
        logger.warning(f"update_patterns failed: {e}")


# ── TOOLS ─────────────────────────────────────────────────────────────────────

@tool("create_reminder", "Создать задачу в Google Tasks.", {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Название задачи"},
        "category": {"type": "string", "description": "Одна из 7 категорий жизни"},
        "due_date": {"type": "string", "description": "Дата YYYY-MM-DD (опционально)"},
        "due_time": {"type": "string", "description": "Время HH:MM (опционально)"},
        "priority": {"type": "string", "description": "high/medium/low", "default": "medium"},
        "notes": {"type": "string", "description": "Дополнительные заметки"},
    },
    "required": ["title", "category"],
})
async def _create_reminder_tool(args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title", "")
    category = args.get("category", "1_Health_Energy")
    due_date = args.get("due_date") or None
    due_time = args.get("due_time") or None
    notes = args.get("notes") or None
    priority = args.get("priority") or None

    try:
        service = await asyncio.to_thread(google_tasks.build_service, _config)
        await asyncio.to_thread(
            google_tasks.create_task,
            service, title, category, due_date, due_time, notes, priority,
        )
    except Exception as e:
        logger.error(f"Google Tasks create_task failed: {e}")
        return _ok(f"Ошибка создания задачи: {e}")

    if _db:
        await _update_patterns(category)
    date_part = f" на {due_date}" if due_date else ""
    return _ok(f"Создана задача {title}{date_part} в {category}.")


@tool("get_reminders_today", "Получить задачи на сегодня из Google Tasks.", {
    "type": "object", "properties": {}, "required": [],
})
async def _get_reminders_today_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        service = await asyncio.to_thread(google_tasks.build_service, _config)
        tasks = await asyncio.to_thread(google_tasks.get_tasks_today, service)
    except Exception as e:
        return _ok(f"Ошибка получения задач: {e}")
    if not tasks:
        return _ok("На сегодня задач нет. Свободный день!")
    lines = [f"• {t['title']} [{t['list']}]" for t in tasks]
    return _ok("Задачи на сегодня:\n" + "\n".join(lines))


@tool("get_reminders_upcoming", "Получить задачи на ближайшие N дней из Google Tasks.", {
    "type": "object",
    "properties": {"days": {"type": "integer", "description": "Количество дней", "default": 7}},
    "required": [],
})
async def _get_reminders_upcoming_tool(args: dict[str, Any]) -> dict[str, Any]:
    days = int(args.get("days", 7))
    try:
        service = await asyncio.to_thread(google_tasks.build_service, _config)
        tasks = await asyncio.to_thread(google_tasks.get_tasks_upcoming, service, days)
    except Exception as e:
        return _ok(f"Ошибка получения задач: {e}")
    if not tasks:
        return _ok(f"На ближайшие {days} дней задач нет.")
    lines = [f"• {t['title']} [{t['list']}] — {t.get('due_date', '')[:10]}" for t in tasks]
    return _ok(f"Задачи на {days} дней:\n" + "\n".join(lines))


@tool("create_note", "Создать заметку в Apple Notes.", {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "content": {"type": "string"},
        "category": {"type": "string", "description": "Папка/категория"},
    },
    "required": ["title", "content", "category"],
})
async def _create_note_tool(args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title", "")
    content = args.get("content", "")
    category = args.get("category", "Inbox")
    body_html = f"<p>{content.replace(chr(10), '<br>')}</p>"
    ok = await asyncio.to_thread(apple_notes.create_note, title, body_html, category)
    if _db:
        await _update_patterns(category)
    return _ok(f"Заметка создана: '{title}' в папке '{category}'" if ok else f"Не удалось создать заметку '{title}'")


@tool("move_note_to_category", "Переместить заметку из Inbox в категорийную папку по её ID.", {
    "type": "object",
    "properties": {
        "note_id": {"type": "string", "description": "ID заметки (x-coredata://...)"},
        "category": {"type": "string"},
    },
    "required": ["note_id", "category"],
})
async def _move_note_tool(args: dict[str, Any]) -> dict[str, Any]:
    note_id = args.get("note_id", "")
    category = args.get("category", "")
    ok = await asyncio.to_thread(apple_notes.move_note_by_id, note_id, category)
    if ok and _db:
        await _update_patterns(category)
    return _ok(f"Заметка перемещена в '{category}'" if ok else "Не удалось переместить заметку")


@tool("create_calendar_event", "Создать событие в Google Calendar.", {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "time": {"type": "string", "description": "HH:MM"},
        "duration_minutes": {"type": "integer", "default": 60},
        "notes": {"type": "string", "description": "Описание события (опционально)"},
    },
    "required": ["title", "date"],
})
async def _create_calendar_event_tool(args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title", "")
    date_str = args.get("date", "")
    time_str = args.get("time", "09:00")
    duration = int(args.get("duration_minutes", 60))
    notes = args.get("notes") or None
    ok = await asyncio.to_thread(
        google_calendar.create_event, _config, title, date_str, time_str, duration, notes
    )
    return _ok(f"Событие создано: '{title}' {date_str} {time_str}" if ok else "Не удалось создать событие")


@tool("move_drive_file", "Переместить файл Google Drive в категорийную папку.", {
    "type": "object",
    "properties": {
        "file_id": {"type": "string", "description": "Google Drive file ID"},
        "category": {"type": "string"},
    },
    "required": ["file_id", "category"],
})
async def _move_drive_file_tool(args: dict[str, Any]) -> dict[str, Any]:
    from sba.integrations.google_drive import build_service, move_file_to_folder
    file_id = args.get("file_id", "")
    category = args.get("category", "")
    folder_id = _category_to_folder_id(category)
    if not folder_id:
        return _ok(f"Не найден folder_id для категории '{category}'")
    try:
        service = await asyncio.to_thread(build_service, _config)
        ok = await asyncio.to_thread(move_file_to_folder, service, file_id, folder_id)
        if ok and _db:
            await _update_patterns(category)
        return _ok(f"Файл перемещён в '{category}'" if ok else "Не удалось переместить файл")
    except Exception as e:
        return _ok(f"Ошибка перемещения файла: {e}")


@tool("index_content", "Добавить файл или заметку в FTS5 поисковый индекс.", {
    "type": "object",
    "properties": {
        "source_id": {"type": "string"},
        "source_type": {"type": "string", "description": "gdrive/apple_notes"},
        "title": {"type": "string"},
        "content": {"type": "string"},
        "category": {"type": "string"},
    },
    "required": ["source_id", "source_type", "title"],
})
async def _index_content_tool(args: dict[str, Any]) -> dict[str, Any]:
    if not _db:
        return _ok("DB not initialized")
    content = args.get("content", "")
    title = args.get("title", "")
    threat = scan_content(content) or scan_content(title)
    if threat:
        source_id = args.get("source_id", "unknown")
        warning = f"⚠️ Подозрительный контент заблокирован: {source_id}\nОбнаружено: {threat}"
        logger.warning("Security: blocked indexing of %s — %s", source_id, threat)
        if _notifier:
            await _notifier.send(warning)
        return _ok(f"Заблокировано: контент содержит подозрительный паттерн ({threat}). Индексация отменена.")
    await _db.index_content(
        source_id=args.get("source_id", ""),
        source_type=args.get("source_type", ""),
        title=title,
        content=content,
        category=args.get("category", ""),
    )
    return _ok("Добавлено в индекс")


@tool("search_knowledge", "Поиск по личной базе знаний (Drive + Notes).", {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer", "default": 5},
    },
    "required": ["query"],
})
async def _search_knowledge_tool(args: dict[str, Any]) -> dict[str, Any]:
    if not _db:
        return _ok("DB not initialized")
    results = await _db.search_fts(args.get("query", ""), int(args.get("limit", 5)))
    if not results:
        return _ok("По базе знаний ничего не найдено. Попробуй поиск в интернете через Research Agent.")
    lines = [f"• {r['title']} [{r['source_type']}] — {r.get('snippet', '')}" for r in results]
    return _ok("Найдено в базе знаний:\n" + "\n".join(lines))


@tool("request_deletion", "Запросить подтверждение удаления через Telegram. Никогда не удалять напрямую.", {
    "type": "object",
    "properties": {
        "item_id": {"type": "string"},
        "title": {"type": "string"},
        "source": {"type": "string", "description": "gdrive/apple_notes"},
    },
    "required": ["item_id", "title", "source"],
})
async def _request_deletion_tool(args: dict[str, Any]) -> dict[str, Any]:
    if not _db:
        return _ok("DB not initialized")
    await _db.create_pending_deletion(
        source_id=args.get("item_id", ""),
        title=args.get("title", ""),
        source=args.get("source", ""),
    )
    return _ok("Запрос на удаление создан, ожидает подтверждения пользователя")


@tool("finance_get_balance", "Получить текущие балансы всех счетов и список обязательств.", {
    "type": "object", "properties": {}, "required": [],
})
async def _finance_get_balance_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    accounts = await _db.fin_get_accounts()
    liabilities = await _db.fin_get_liabilities()
    total_cash = sum(a["balance"] for a in accounts if a["balance"] > 0)
    total_debt = sum(l["amount"] for l in liabilities)
    # Sort: main first, then by balance desc
    order = {"account_main": 0, "account_2": 1, "account_3": 2, "account_4": 3, "account_5": 4, "account_biz": 5}
    accounts_sorted = sorted(accounts, key=lambda a: order.get(a["name"], 9))
    lines = ["💳 Счета:"]
    for a in accounts_sorted:
        if a["balance"] > 0:
            lines.append(f"  {a['label']}: {a['balance']:,.0f} ₸")
    lines.append(f"  Итого: {total_cash:,.0f} ₸")
    lines.append("\n📋 Обязательства:")
    for l in liabilities:
        mp = f" / {l['monthly_payment']:,.0f} ₸/мес" if l.get("monthly_payment") else ""
        lines.append(f"  {l['creditor'] or l['name']}: {l['amount']:,.0f} ₸{mp}")
    lines.append(f"  Итого: {total_debt:,.0f} ₸")
    net = total_cash - total_debt
    sign = "+" if net >= 0 else ""
    lines.append(f"\n📊 Чистые активы: {sign}{net:,.0f} ₸")
    return _ok("\n".join(lines))


@tool("finance_get_balance_on_date", "Получить баланс счёта на конкретную дату (из сохранённых снимков).", {
    "type": "object",
    "properties": {
        "account":  {"type": "string", "description": "Название счёта (account_main, account_2, и т.д.). Если не указан — все счета."},
        "date":     {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
    },
    "required": ["date"],
})
async def _finance_get_balance_on_date_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from sba import finance as _fin
    from datetime import date as _date
    target_date = args.get("date", "")
    account_raw = args.get("account", "")
    account = _fin.resolve_account(account_raw) if account_raw else None

    if account:
        snap = await _db.fin_get_snapshot_on_date(account, target_date)
        if not snap:
            return _ok(f"Нет данных о балансе {account} на {target_date}. "
                       f"Снимки сохраняются начиная с даты первого обновления баланса.")
        acc = await _db.fin_get_account(account)
        label = acc["label"] if acc else account
        return _ok(f"{label} на {snap['snapshot_date']}: {snap['balance']:,.0f} ₸ (источник: {snap['source']})")
    else:
        accounts = await _db.fin_get_accounts()
        lines = [f"Балансы на {target_date} (ближайший снимок):"]
        found_any = False
        for a in accounts:
            snap = await _db.fin_get_snapshot_on_date(a["name"], target_date)
            if snap:
                found_any = True
                lines.append(f"  {a['label']}: {snap['balance']:,.0f} ₸ ({snap['snapshot_date']}, {snap['source']})")
            else:
                lines.append(f"  {a['label']}: нет данных")
        if not found_any:
            return _ok(f"Нет снимков баланса за {target_date} и ранее. "
                       f"Снимки начнут накапливаться автоматически с сегодняшнего дня.")
        return _ok("\n".join(lines))


@tool("finance_add_transaction", "Добавить доход или расход.", {
    "type": "object",
    "properties": {
        "account":     {"type": "string", "description": "Название счёта (account_main, account_2, account_3, account_4, account_5, account_biz)"},
        "amount":      {"type": "number",  "description": "Сумма (всегда положительная)"},
        "tx_type":     {"type": "string",  "description": "income, expense, transfer, debt_taken, debt_paid"},
        "category":    {"type": "string",  "description": "Категория (еда, кафе, транспорт, коммуналка, зарплата и т.д.)"},
        "description": {"type": "string",  "description": "Описание транзакции"},
        "tx_date":     {"type": "string",  "description": "Дата YYYY-MM-DD (если не сегодня)"},
    },
    "required": ["amount", "tx_type"],
})
async def _finance_add_transaction_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from sba import finance as _fin
    account_raw = args.get("account", "")
    account = _fin.resolve_account(account_raw) if account_raw else None
    amount = float(args.get("amount", 0))
    tx_type = args.get("tx_type", "expense")
    category = args.get("category", "")
    description = args.get("description", "")
    tx_date = args.get("tx_date", "")
    await _db.fin_add_transaction(account, amount, tx_type, category, description, tx_date)
    sign = "⇄" if tx_type == "transfer" else ("+" if tx_type in ("income", "debt_taken") else "-")
    acc_label = account or "без счёта"
    return _ok(f"Записано: {sign}{amount:,.0f} ₸ [{category or tx_type}] {acc_label}")


@tool("finance_update_account", "Обновить баланс счёта (пользователь сообщил актуальный баланс).", {
    "type": "object",
    "properties": {
        "account":     {"type": "string", "description": "Название счёта (account_main, account_2, account_3, account_4, account_5, account_biz)"},
        "new_balance": {"type": "number", "description": "Новый баланс в тенге"},
        "note":        {"type": "string", "description": "Комментарий (опционально)"},
    },
    "required": ["account", "new_balance"],
})
async def _finance_update_account_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from sba import finance as _fin
    account = _fin.resolve_account(args.get("account", ""))
    new_balance = float(args.get("new_balance", 0))
    note = args.get("note", "")
    acc = await _db.fin_get_account(account)
    if not acc:
        return _ok(f"Счёт '{account}' не найден. Доступные: account_main, account_2, account_3, account_4, account_5, account_biz")
    old = acc["balance"]
    await _db.fin_update_balance(account, new_balance, note)
    diff = new_balance - old
    sign = "+" if diff >= 0 else ""
    return _ok(f"{acc['label']} обновлён: {old:,.0f} → {new_balance:,.0f} ₸ ({sign}{diff:,.0f} ₸)")


@tool("finance_manage_liability", "Добавить новый долг или обновить остаток существующего.", {
    "type": "object",
    "properties": {
        "action":      {"type": "string",  "description": "add_new или update_amount"},
        "name":        {"type": "string",  "description": "Внутренний идентификатор (people_debt, kaspi_installment, transport_tax или новый)"},
        "creditor":    {"type": "string",  "description": "Имя кредитора / название"},
        "amount":      {"type": "number",  "description": "Сумма долга (для add_new) или новый остаток (для update_amount)"},
        "lib_type":    {"type": "string",  "description": "personal, installment, tax, loan"},
        "monthly_payment": {"type": "number", "description": "Ежемесячный платёж (опционально)"},
        "due_date":    {"type": "string",  "description": "Дата погашения YYYY-MM-DD (опционально)"},
        "notes":       {"type": "string",  "description": "Примечания"},
    },
    "required": ["action", "name", "amount"],
})
async def _finance_manage_liability_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from sba import finance as _fin
    action = args.get("action", "add_new")
    name = _fin.resolve_liability(args.get("name", ""))
    amount = float(args.get("amount", 0))

    if action == "update_amount":
        ok, closed = await _db.fin_update_liability_amount(name, amount)
        if not ok:
            return _ok(f"Обязательство '{name}' не найдено")
        if closed:
            return _ok(f"🎉 Поздравляю! Долг '{name}' полностью погашен и закрыт.")
        return _ok(f"Остаток по '{name}' обновлён: {amount:,.0f} ₸")
    else:
        creditor = args.get("creditor", name)
        lib_type = args.get("lib_type", "personal")
        monthly = args.get("monthly_payment")
        due = args.get("due_date")
        notes = args.get("notes", "")
        await _db.fin_upsert_liability(name, creditor, amount, lib_type,
                                        float(monthly) if monthly else None, due, notes)
        return _ok(f"Обязательство '{creditor}' сохранено: {amount:,.0f} ₸")


@tool("finance_get_zakat", "Рассчитать текущий статус закята.", {
    "type": "object", "properties": {}, "required": [],
})
async def _finance_get_zakat_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from sba import finance as _fin
    status = await _fin.calculate_zakat_status(_db)
    lines = [
        f"Нисаб (85г золота): {status['nisab_kzt']:,.0f} ₸",
        f"Деньги на счетах: {status['cash_assets']:,.0f} ₸",
        f"Обязательства: {status['total_liabilities']:,.0f} ₸",
        f"Чистые активы: {status['net_assets']:,.0f} ₸",
        "",
        "ЗАКЯТ: " + ("ОБЯЗАТЕЛЕН" if status["obligatory"] else "не обязателен"),
        status["reason"],
    ]
    if status["obligatory"]:
        lines.append(f"Сумма к оплате: {status['amount_due']:,.0f} ₸")
    if status.get("price_is_stale"):
        lines.append("\n⚠️ Курс золота недоступен (Yahoo Finance), использован устаревший fallback 80 000 ₸/г")
    return _ok("\n".join(lines))


@tool("finance_get_summary", "Получить финансовую сводку за текущий или прошлый месяц.", {
    "type": "object",
    "properties": {
        "period": {"type": "string", "description": "this_month или last_month", "default": "this_month"},
    },
    "required": [],
})
async def _finance_get_summary_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from datetime import date
    today = date.today()
    period = args.get("period", "this_month")
    if period == "last_month":
        month = today.month - 1 or 12
        year = today.year if today.month > 1 else today.year - 1
    else:
        month = today.month
        year = today.year
    summary = await _db.fin_get_monthly_summary(year, month)
    income = summary["income"]
    expense = summary["expense"]
    lines = [
        f"Сводка за {month:02d}/{year}:",
        f"  Доходы:  +{income:,.0f} ₸",
        f"  Расходы: -{expense:,.0f} ₸",
        f"  Баланс:  {income - expense:+,.0f} ₸",
    ]
    cats: dict = {}
    for r in summary["rows"]:
        if r["tx_type"] == "expense" and r["category"]:
            cats[r["category"]] = cats.get(r["category"], 0) + r["total"]
    if cats:
        lines.append("\nРасходы по категориям:")
        for cat, total in sorted(cats.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: {total:,.0f} ₸")
    return _ok("\n".join(lines))


@tool("finance_get_transactions", "Показать последние транзакции по счёту или все счета.", {
    "type": "object",
    "properties": {
        "account": {"type": "string", "description": "Название счёта (основной, второй, kaspi, freedom и т.д.). Если не указан — все счета."},
        "limit":   {"type": "integer", "description": "Сколько транзакций показать (default 15)", "default": 15},
    },
    "required": [],
})
async def _finance_get_transactions_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    from sba.finance import resolve_account
    account_raw = args.get("account")
    account = resolve_account(account_raw) if account_raw else None
    limit = int(args.get("limit", 15))
    rows = await _db.fin_get_recent_transactions(account=account, limit=limit)
    if not rows:
        return _ok("Транзакций не найдено.")
    lines = [f"Последние {len(rows)} транзакций{f' ({account_raw})' if account_raw else ''}:"]
    for r in rows:
        if r["tx_type"] == "transfer":
            sign = "⇄"
        elif r["tx_type"] in ("income", "debt_taken"):
            sign = "+"
        else:
            sign = "-"
        acc = r.get("account") or "—"
        desc = r.get("description") or r.get("category") or ""
        lines.append(f"  {r['tx_date']}  {sign}{r['amount']:,.0f} ₸  [{acc}]  {desc}")
    return _ok("\n".join(lines))


@tool("finance_manage_recurring", "Добавить или удалить регулярное финансовое напоминание.", {
    "type": "object",
    "properties": {
        "action":       {"type": "string",  "description": "add или delete"},
        "label":        {"type": "string",  "description": "Описание: 'Коммуналка', 'Google AI Pro', 'Садака', 'Школа'"},
        "day_of_month": {"type": "integer", "description": "День месяца 1-31. 0 = ежедневно"},
        "amount":       {"type": "number",  "description": "Сумма в тенге (опционально)"},
        "remind_days_before": {"type": "integer", "description": "За сколько дней до срока напоминать (default 0)", "default": 0},
        "item_id":      {"type": "integer", "description": "ID записи для удаления (только для action=delete)"},
    },
    "required": ["action"],
})
async def _finance_manage_recurring_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    action = args.get("action", "add")
    if action == "delete":
        item_id = args.get("item_id")
        if not item_id:
            return _ok("Укажи ID записи для удаления")
        await _db.fin_delete_recurring(int(item_id))
        return _ok(f"Напоминание #{item_id} отключено")
    else:
        label = args.get("label", "")
        day = int(args.get("day_of_month", 0))
        amount = args.get("amount")
        remind_before = int(args.get("remind_days_before", 0))
        if not label:
            return _ok("Укажи описание напоминания")
        row_id = await _db.fin_upsert_recurring(label, day, float(amount) if amount else None, remind_before)
        day_str = "ежедневно" if day == 0 else f"{day}-го числа каждого месяца"
        amount_str = f" ({amount:,.0f} ₸)" if amount else ""
        return _ok(f"Напоминание #{row_id} добавлено: {label}{amount_str} — {day_str}")


@tool("finance_list_recurring", "Показать регулярные платежи. mode=upcoming — только предстоящие и просроченные; mode=all — все.", {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "description": "upcoming (default) или all", "default": "upcoming"},
    },
    "required": [],
})
async def _finance_list_recurring_tool(args: dict) -> dict:
    if not _db:
        return _ok("DB not initialized")
    import calendar
    from datetime import date
    today = date.today()
    today_day = today.day
    current_month = today.strftime("%Y-%m")
    mode = args.get("mode", "upcoming")

    items = await _db.fin_get_recurring()
    if not items:
        return _ok("Регулярных напоминаний нет.")

    if mode == "upcoming":
        daily, overdue, upcoming = [], [], []
        for item in items:
            if item.get("paid_month") == current_month:
                continue  # confirmed paid — skip
            dom = item["day_of_month"]
            amount_str = f" — {item['amount']:,.0f} ₸" if item.get("amount") else ""
            label = f"{item['label']}{amount_str}"
            if dom == 0:
                daily.append(f"  • {label} — ежедневно")
            elif dom < today_day:
                # Past due — check if transaction exists before marking overdue
                # strict=False: keyword match alone is enough (amount may vary due to FX)
                matches = await _db.fin_find_matching_transactions(
                    item["label"], item.get("amount"), current_month, strict=False
                )
                if matches:
                    continue  # transaction found — treat as paid, skip
                overdue.append(f"  • {label} — просрочен ({dom}-е)")
            elif dom == today_day:
                upcoming.append(f"  • {label} — сегодня ({dom}-е)")
            else:
                days_left = dom - today_day
                upcoming.append(f"  • {label} — через {days_left} дн. ({dom}-е)")

        lines = []
        if overdue:
            lines.append("⚠️ Просроченные:")
            lines.extend(overdue)
        if upcoming:
            lines.append("📅 Предстоящие в этом месяце:")
            lines.extend(upcoming)
        if daily:
            lines.append("🔁 Ежедневные:")
            lines.extend(daily)
        if not lines:
            return _ok("Все платежи этого месяца оплачены ✅")
        return _ok("\n".join(lines))
    else:
        # mode=all — full list with paid markers
        lines = ["Все регулярные платежи:"]
        for item in items:
            dom = item["day_of_month"]
            day_str = "ежедневно" if dom == 0 else f"{dom}-го числа"
            amount_str = f" — {item['amount']:,.0f} ₸" if item.get("amount") else ""
            paid_mark = " ✅" if item.get("paid_month") == current_month else ""
            lines.append(f"  #{item['id']} {item['label']}{amount_str} — {day_str}{paid_mark}")
        return _ok("\n".join(lines))


@tool("request_capability_development",
    "Запросить разработку нового инструмента через Claude Code сессию. Используй когда нужен инструмент которого нет в списке.",
    {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "snake_case имя инструмента (например: get_youtube_transcript)"},
            "task": {"type": "string", "description": "Подробное описание что должен делать инструмент, какие входные параметры принимать, что возвращать"},
            "resume_message": {"type": "string", "description": "Исходный запрос пользователя — будет выполнен автоматически после добавления инструмента"},
        },
        "required": ["tool_name", "task", "resume_message"],
    }
)
async def _request_capability_development_tool(args: dict) -> dict:
    import json, time
    from pathlib import Path as _Path

    dev_file = _Path.home() / ".sba" / "dev_request.json"

    if dev_file.exists():
        return _ok("Запрос на разработку уже в очереди. Не повторяй вызов.")

    chat_id = _config.get("owner", {}).get("telegram_chat_id", 0)
    tool_name = args.get("tool_name", "")
    task = args.get("task", "")
    resume_message = args.get("resume_message", "")

    dev_file.write_text(json.dumps({
        "status": "pending",
        "tool_name": tool_name,
        "task": task,
        "resume_message": resume_message,
        "chat_id": int(chat_id),
        "ts": time.time(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    await _notifier.send_message(
        f"🔧 Нужен инструмент <code>{tool_name}</code>.\n"
        f"Передаю задачу Claude Code — разработает и установит автоматически."
    )
    return _ok(f"Запрос на разработку инструмента '{tool_name}' отправлен. Claude Code займётся этим.")


@tool("propose_capability_extension",
    "Предложить расширение возможностей бота. Используй когда нужна недостающая библиотека, API-ключ или перезапуск.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Краткое название (например: 'Установить веб-поиск')"},
            "description": {"type": "string", "description": "Что это даст и зачем нужно"},
            "action": {"type": "string", "enum": ["pip_install", "add_config_value", "restart_bot"],
                       "description": "pip_install — установить пакет; add_config_value — добавить ключ в config; restart_bot — перезапустить бота"},
            "package": {"type": "string", "description": "Имя pip-пакета (только для pip_install)"},
            "config_path": {"type": "string", "description": "Путь в config.yaml через точку, например finance.brave_api_key"},
            "involves_personal_data": {"type": "boolean",
                                       "description": "True если действие передаёт персональные данные на внешний сервис"},
        },
        "required": ["title", "description", "action"],
    }
)
async def _propose_extension_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Register a pending extension and send approval request to user."""
    involves_data = args.get("involves_personal_data", False)
    ext_id = _ext_registry.register(args)

    data_note = "\n⚠️ <b>Затрагивает персональные данные.</b>" if involves_data else ""
    action_detail = ""
    if args.get("package"):
        action_detail = f"\nПакет: <code>{args['package']}</code>"
    elif args.get("config_path"):
        action_detail = f"\nКлюч конфига: <code>{args['config_path']}</code>"

    text = (
        f"🔧 <b>{args['title']}</b>\n\n"
        f"{args['description']}{data_note}{action_detail}"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Разрешить", "callback_data": f"ext_ok:{ext_id}"},
        {"text": "❌ Отменить", "callback_data": f"ext_deny:{ext_id}"},
    ]]}
    if _notifier:
        await _notifier.send_message(text, reply_markup=keyboard)
    return _ok(f"Предложение #{ext_id} отправлено. Жду подтверждения.")


@tool("get_youtube_transcript", "Получить транскрипт/субтитры YouTube-видео по ссылке. Предпочитает русский язык, fallback — английский.", {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Ссылка на YouTube-видео (любой формат: youtube.com/watch?v=..., youtu.be/..., shorts/...)"},
    },
    "required": ["url"],
})
async def _get_youtube_transcript_tool(args: dict[str, Any]) -> dict[str, Any]:
    import re
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

    url = args.get("url", "").strip()

    # Extract video ID from various YouTube URL formats
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    video_id = None
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            video_id = m.group(1)
            break

    if not video_id:
        return _ok(f"Не удалось извлечь video ID из URL: {url}")

    try:
        api = YouTubeTranscriptApi()
        transcript_list = await asyncio.to_thread(api.list, video_id)

        transcript = None
        # Prefer Russian, then English, then any
        for lang in ("ru", "en"):
            try:
                transcript = transcript_list.find_transcript([lang])
                break
            except NoTranscriptFound:
                continue

        if transcript is None:
            # Try any generated transcript
            try:
                transcript = transcript_list.find_generated_transcript(["ru", "en"])
            except NoTranscriptFound:
                # Take first available
                transcript = next(iter(transcript_list))

        fetched = await asyncio.to_thread(transcript.fetch)
        entries = fetched.to_raw_data()
        text = " ".join(e.get("text", "") for e in entries if e.get("text", "").strip())
        lang_used = transcript.language_code
        return _ok(f"Транскрипт видео {video_id} [{lang_used}]:\n\n{text}")

    except TranscriptsDisabled:
        return _ok(f"Субтитры отключены для видео {video_id}.")
    except Exception as e:
        return _ok(f"Ошибка получения транскрипта {video_id}: {e}")


# ── MCP servers ───────────────────────────────────────────────────────────────

_main_server = create_sdk_mcp_server(
    name="sba",
    tools=[
        _create_reminder_tool,
        _get_reminders_today_tool,
        _get_reminders_upcoming_tool,
        _create_note_tool,
        _move_note_tool,
        _create_calendar_event_tool,
        _move_drive_file_tool,
        _index_content_tool,
        _search_knowledge_tool,
        _request_deletion_tool,
        _finance_get_balance_tool,
        _finance_get_balance_on_date_tool,
        _finance_add_transaction_tool,
        _finance_update_account_tool,
        _finance_manage_liability_tool,
        _finance_get_zakat_tool,
        _finance_get_summary_tool,
        _finance_get_transactions_tool,
        _finance_manage_recurring_tool,
        _finance_list_recurring_tool,
        _propose_extension_tool,
        _request_capability_development_tool,
        _get_youtube_transcript_tool,
    ],
)

# ── System prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT_BASE = """Ты — персональный разговорный ассистент. Всегда отвечай на русском языке.

Твоя задача: GTD + организация жизни. Обрабатывай запросы пользователя.

Категории жизни:
1_Health_Energy, 2_Business_Career, 3_Finance,
4_Family_Relationships, 5_Personal Growth, 6_Brightness life, 7_Spirituality

При входящем элементе (указан Источник и ID):
- action/review → create_reminder в Google Tasks (+ create_calendar_event в Google Calendar если есть дата), затем index_content
- info → move_drive_file или move_note_to_category, затем index_content
- мусор → request_deletion
- ЗАПРЕЩЕНО вызывать Research Agent для обработки входящих элементов — только для явных запросов пользователя типа "найди" или "изучи".

ВАЖНО: Если в сообщении написано "он уже находится в организованной папке, НЕ перемещай его" — ТОЛЬКО вызови index_content. Никаких move_drive_file, никаких задач. Только индексация.

При вопросе пользователя:
- "что на сегодня" → get_reminders_today
- "что на неделе" → get_reminders_upcoming
- "найди про X", "поищи X", "сколько стоит X", "где купить X", "что такое X" → сначала search_knowledge; если нет — WebSearch напрямую
- "изучи тему Y" подробно → WebSearch + WebFetch для нескольких источников

Если задач на сегодня нет — ответь: "На сегодня задач нет. Свободный день!"
ВАЖНО: При запросах поиска ВСЕГДА пробуй WebSearch. Никогда не говори "нет доступа к поиску" без попытки.
ВАЖНО: Когда прямой доступ к контенту невозможен (видео, платный сайт, соцсети и т.п.) — НЕ останавливайся. Рассуждай: какой самый прямой способ получить именно этот контент?
- Видео (YouTube и др.) → субтитры/транскрипт (например youtube-transcript-api) — это точнее, чем поиск по теме
- Платная статья → WebSearch по заголовку, abstract, цитатам
- GitHub репо → WebFetch на README или конкретный файл
- Если нужного инструмента нет → propose_capability_extension
Всегда выбирай самый прямой путь к содержимому, а не обходной.

Самодостраивание (ВАЖНО):
Если задача требует отсутствующего инструмента — НИКОГДА не говори "не могу", "нет доступа", "эта функция недоступна".
Вместо этого выбери путь:

Путь A — нужен новый инструмент (например: get_youtube_transcript, parse_pdf, send_email):
→ вызови request_capability_development с названием инструмента, подробным описанием задачи и исходным запросом пользователя.
Claude Code разработает инструмент автоматически и бот перезапустится.

Путь Б — нужна внешняя зависимость (pip-пакет, API-ключ, перезапуск):
1. Оцени: потребует ли ручной настройки (QR-код, регистрация и т.п.)?
   - Да → честно опиши что нужно вручную
2. Оцени: передаются ли персональные данные наружу?
   - Нет → вызови propose_capability_extension
   - Да → объясни и попроси данные явно

Путь В — инструмент существует, но вернул ошибку или не справился с задачей:
→ НЕ объясняй пользователю как сделать вручную. НЕ сдавайся.
→ Вызови request_capability_development с тем же tool_name, опиши ошибку и что нужно улучшить (альтернативный метод, обход блокировки, другая библиотека и т.п.).
→ Примеры: get_youtube_transcript вернул ошибку блокировки → попроси добавить yt-dlp как fallback; инструмент вернул пустой результат → попроси улучшить логику.
Claude Code исправит инструмент автоматически и бот перезапустится.

КРИТИЧНО: после вызова request_capability_development или propose_capability_extension — НЕМЕДЛЕННО СТОП. Не повторяй. Жди.

Индексация базы знаний:
- FTS5 индекс наполняется постепенно: новые файлы индексируются сразу при обработке через inbox.
- Старые файлы из категорийных папок Google Drive индексирует фоновый процесс legacy: каждый день в 09:00, по 3 файла, в порядке категорий 1_Health_Energy → 2_Business_Career → ... → 7_Spirituality.
- ВАЖНО: если пользователь просит проиндексировать папку или спрашивает почему файлы не находятся — НИКОГДА не говори "не могу". Вместо этого объясни: "Папка будет проиндексирована автоматически через фоновый процесс legacy (каждый день по 3 файла). Чтобы ускорить — запусти `sba legacy` вручную в терминале несколько раз, или временно подними legacy_limit_drive в ~/.sba/config.yaml."

Технические ошибки объясняй по-русски. Не используй английский.

Финансы (личный финансист):
ВАЖНО: Любые вопросы про деньги, счета, балансы, расходы, долги, закят — это финансовые запросы. НЕ используй search_knowledge для финансовых запросов.
ВАЖНО: Результат финансовых инструментов (finance_get_balance, finance_get_balance_on_date, finance_get_transactions, finance_get_summary) передавай пользователю ДОСЛОВНО, без пересказа и сокращений. Не перефразируй числа и названия счетов.
- "баланс", "сколько денег", "мои счета", "на счетах", "на счету", "сколько на счёте", "финансы", "деньги на счёте" → finance_get_balance
- "баланс на X", "сколько было X апреля", "остаток на [дата]", "сколько было на счёте [дата]" → finance_get_balance_on_date
- "потратил X на Y", "купил X за Y", "заплатил X за Y", "списалось X" → finance_add_transaction (tx_type=expense)
- "получил зарплату", "зарплата пришла", "пришло X от X" → finance_add_transaction (tx_type=income, account=account_3 — зарплата приходит на Freedom)
- "перевёл X с X на X", "перекинул X на основной", "перевёл с депозита", "перевёл с Фридом" → finance_add_transaction (tx_type=transfer) — переводы между своими счетами НЕ являются доходом или расходом, только tx_type=transfer
- "взял в долг у X сумма" → finance_manage_liability (action=add_new) + finance_add_transaction (tx_type=debt_taken)
- "отдал X долг сумма" → finance_manage_liability (action=update_amount) + finance_add_transaction (tx_type=debt_paid)
- "оплата рассрочки", "заплатил рассрочку", "рассрочка Каспи сумма" → finance_manage_liability (action=update_amount, name=kaspi_installment, amount=текущий_остаток - сумма_платежа) + finance_add_transaction (tx_type=expense, category=кредиты)
- "Каспи X тенге", "баланс Каспи X", "на Каспи X", "обнови счёт X" → finance_update_account
- "закят", "зякат" → finance_get_zakat
- "итоги месяца", "сводка", "расходы за месяц" → finance_get_summary (переводы между счетами исключаются из сводки автоматически)
- "последние транзакции", "последние расходы", "что было по счёту", "с чего продолжить", "какая последняя транзакция" → finance_get_transactions
- "предстоящие платежи", "ближайшие платежи", "что платить", "какие платежи" → finance_list_recurring(mode=upcoming)
- "регулярные платежи", "мои подписки", "список напоминаний" → finance_list_recurring(mode=all)
- ВСЕГДА вызывать инструмент, не отвечать по памяти

Маппинг счетов (используй эти имена в инструментах):
  основной / main / каспи основной → account_main  (Kaspi основной — основной расчётный)
  депозит / второй / каспи депозит → account_2     (Kaspi Депозит — накопительный, переводы отсюда = transfer)
  фридом / freedom / зарплатный → account_3         (Freedom Bank — зарплата приходит сюда, потом transfer на основной)
  халык / halyk → account_4
  рбк / rbk / tayyab → account_5
  бизнес / business → account_biz

Маппинг обязательств:
  рассрочка Каспи / каспи рассрочка → kaspi_installment
  Муратбек → debt_muratbek
  Нурлан → debt_nurlan
  Саят → debt_sayat

Если пользователь не уточнил счёт — спроси с какого. Никогда не придумывай счёт.
Даты: если пользователь говорит "вчера", "три дня назад" — вычисли правильную дату YYYY-MM-DD.

Регулярные напоминания:
- "напоминай каждый день X", "ежедневно X" → finance_manage_recurring (action=add, day_of_month=0)
- "напоминай N-го числа Y", "каждый месяц N-го" → finance_manage_recurring (action=add, day_of_month=N)
- "мои регулярные платежи", "список напоминаний" → finance_list_recurring
- "удали напоминание #N" → finance_manage_recurring (action=delete, item_id=N)

Стиль ответов — строго:
- Максимум 2-3 коротких предложения. Не больше.
- НИКОГДА не заканчивай вопросом ("Какой вариант?", "Что выбираешь?" и т.п.) — это запрещено.
- НИКОГДА не перечисляй варианты с номерами если пользователь не просил выбор.
- Просто сообщи факт и одно действие. Пример хорошего ответа: "Папка будет проиндексирована через legacy автоматически. Чтобы ускорить — запусти sba legacy в терминале."

При создании задачи отвечай строго: "Создана задача [название] на [ДД/ММ/ГГГГ] в [список]."
При создании заметки: "Создана заметка [название] в [папка]."
Никаких пояснений, никаких "появится утром", никаких "ссылка сохранена".

Форматирование: только простой текст. Никаких **звёздочек**, никакого Markdown.
Для выделения используй эмодзи или дефисы. Telegram показывает сообщения как обычный текст."""



async def _build_system_prompt() -> str:
    """Build system prompt with current user patterns context."""
    if not _db:
        return SYSTEM_PROMPT_BASE
    try:
        patterns = await _db.get_user_patterns()
        extra = ""
        if patterns.get("top_categories"):
            extra += f"\nПользователь чаще всего работает с: {patterns['top_categories']}."
        if patterns.get("active_hours"):
            extra += f"\nОбычно активен в: {patterns['active_hours']}."
        return SYSTEM_PROMPT_BASE + extra
    except Exception:
        return SYSTEM_PROMPT_BASE


def _build_options(system_prompt: str) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for the main agent."""
    model = _config.get("classifier", {}).get("model", "claude-haiku-4-5-20251001")
    api_key = _config.get("anthropic", {}).get("api_key", "")

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        mcp_servers={
            "sba": _main_server,
            "research_tools": _research_module.research_mcp_server,
        },
        agents={
            "research": _research_module.research_agent_definition,
        },
        allowed_tools=[
            "mcp__sba__create_reminder",
            "mcp__sba__get_reminders_today",
            "mcp__sba__get_reminders_upcoming",
            "mcp__sba__create_note",
            "mcp__sba__move_note_to_category",
            "mcp__sba__create_calendar_event",
            "mcp__sba__move_drive_file",
            "mcp__sba__index_content",
            "mcp__sba__search_knowledge",
            "mcp__sba__request_deletion",
            "mcp__sba__finance_get_balance",
            "mcp__sba__finance_get_balance_on_date",
            "mcp__sba__finance_add_transaction",
            "mcp__sba__finance_update_account",
            "mcp__sba__finance_manage_liability",
            "mcp__sba__finance_get_zakat",
            "mcp__sba__finance_get_summary",
            "mcp__sba__finance_get_transactions",
            "mcp__sba__finance_manage_recurring",
            "mcp__sba__finance_list_recurring",
            "mcp__sba__propose_capability_extension",
            "mcp__sba__request_capability_development",
            "mcp__sba__get_youtube_transcript",
            "WebSearch",   # прямой веб-поиск
            "WebFetch",    # чтение страниц
            "Task",        # вызов Research Agent (для сложных multi-step запросов)
        ],
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        max_turns=15,
        env={
            "ANTHROPIC_API_KEY": api_key,
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    )


async def run_main_agent(
    message: str, db: Database, notifier: Notifier, config: dict,
    _cost_accumulator: list | None = None,
) -> str:
    """
    Run Main Agent for a single message. Returns agent's text response.
    Used by inbox/legacy processors and bot handlers.
    _cost_accumulator: optional list to accumulate cost (append float).
    """
    setup(db, notifier, config)
    system_prompt = await _build_system_prompt()
    options = _build_options(system_prompt)

    result_text = ""
    last_error: Exception | None = None
    for attempt in range(2):  # 1 retry on failure
        try:
            async for msg in query(prompt=message, options=options):
                if isinstance(msg, ResultMessage):
                    cost = msg.total_cost_usd or 0.0
                    turns = msg.num_turns
                    usage = msg.usage or {}
                    in_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    logger.info(
                        f"Agent call: ${cost:.4f} | {turns} turns | "
                        f"{in_tok} in / {out_tok} out tokens"
                    )
                    if _cost_accumulator is not None:
                        _cost_accumulator.append(cost)
                    # Detect billing errors and notify immediately
                    if msg.is_error and msg.result and "Credit balance is too low" in msg.result:
                        logger.error("Anthropic API: credit balance too low")
                        await _notifier.send_message(
                            "⛔ <b>Лимит расходов Anthropic исчерпан</b>\n\n"
                            "API возвращает «Credit balance is too low».\n"
                            "Возможные причины:\n"
                            "• Закончился баланс на счёте\n"
                            "• Достигнут месячный лимит расходов (Spend limit)\n\n"
                            "Проверь: console.anthropic.com/billing\n"
                            "Бот не будет отвечать до пополнения / повышения лимита."
                        )
                        return "⛔ Лимит расходов Anthropic исчерпан. Проверь console.anthropic.com/billing"
                    if msg.result:
                        result_text = msg.result
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            result_text = block.text  # keep last text block as fallback
            last_error = None
            break  # success
        except Exception as e:
            last_error = e
            logger.error(f"Agent error (attempt {attempt + 1}): {e}", exc_info=True)
            if attempt == 0:
                await asyncio.sleep(5)
    if last_error is not None:
        return "Что-то пошло не так. Попробуй ещё раз."

    # After agent run: check for new pending deletions and notify via Telegram
    try:
        pending = await db.get_new_pending_deletions()
        for item in pending:
            msg_id = await notifier.send_deletion_request(
                deletion_id=item["id"],
                item_title=item.get("title", "?"),
                item_source=item.get("source", "?"),
            )
            if msg_id:
                await db.set_deletion_telegram_msg(item["id"], msg_id)
    except Exception as e:
        logger.warning(f"Failed to process pending deletions: {e}")

    return result_text
