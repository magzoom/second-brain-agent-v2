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
from claude_agent_sdk.types import AssistantMessage, TextBlock

from sba.db import Database
from sba.notifier import Notifier
from sba.integrations import apple_notes, google_tasks, google_calendar
from sba import research_agent as _research_module

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
    await _db.index_content(
        source_id=args.get("source_id", ""),
        source_type=args.get("source_type", ""),
        title=args.get("title", ""),
        content=args.get("content", ""),
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

При вопросе пользователя:
- "что на сегодня" → get_reminders_today
- "что на неделе" → get_reminders_upcoming
- "найди про X" → search_knowledge, если нет — вызови Research Agent через Task tool
- "изучи тему Y" → вызови Research Agent через Task tool

Если задач на сегодня нет — ответь: "На сегодня задач нет. Свободный день!"
Если поиск ничего не нашёл — честно скажи и предложи Research Agent.

Индексация базы знаний:
- FTS5 индекс наполняется постепенно: новые файлы индексируются сразу при обработке через inbox.
- Старые файлы из категорийных папок Google Drive индексирует фоновый процесс legacy: каждый день в 09:00, по 3 файла, в порядке категорий 1_Health_Energy → 2_Business_Career → ... → 7_Spirituality.
- ВАЖНО: если пользователь просит проиндексировать папку или спрашивает почему файлы не находятся — НИКОГДА не говори "не могу". Вместо этого объясни: "Папка будет проиндексирована автоматически через фоновый процесс legacy (каждый день по 3 файла). Чтобы ускорить — запусти `sba legacy` вручную в терминале несколько раз, или временно подними legacy_limit_drive в ~/.sba/config.yaml."

Технические ошибки объясняй по-русски. Не используй английский.

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
            "Task",  # для вызова Research Agent
        ],
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        max_turns=15,
        env={
            "ANTHROPIC_API_KEY": api_key,
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    )


async def run_main_agent(message: str, db: Database, notifier: Notifier, config: dict) -> str:
    """
    Run Main Agent for a single message. Returns agent's text response.
    Used by inbox/legacy processors and bot handlers.
    """
    setup(db, notifier, config)
    system_prompt = await _build_system_prompt()
    options = _build_options(system_prompt)

    result_text = ""
    try:
        async for msg in query(prompt=message, options=options):
            if hasattr(msg, "result") and msg.result:
                result_text = msg.result
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        result_text = block.text  # keep last text block as fallback
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return f"Что-то пошло не так: {e}"

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
