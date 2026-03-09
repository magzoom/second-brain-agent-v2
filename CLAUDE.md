# SBA 2.0 — Second Brain Agent

## Архитектура

**3 агента через Claude Agent SDK:**
- `sba/agent.py` — Main Agent (оркестратор, GTD tools, 15 turns)
- `sba/digest_agent.py` — Digest Agent (утренний брифинг, независимый)
- Research Agent — subagent внутри agent.py (AgentDefinition, WebSearch + FTS5)

**4 демона (launchd):**
- `com.sba.bot` — Telegram long polling, KeepAlive
- `com.sba.inbox` — каждые 2 ч, Google Drive changes + Apple Notes Inbox
- `com.sba.legacy` — 09:00, обработка накопленного + Goal Tracker
- `com.sba.digest` — 08:00, утренний дайджест

## Ключевые файлы

```
sba/
  agent.py          — Main Agent + tools
  digest_agent.py   — Digest Agent (Telethon + Google Tasks)
  inbox_processor.py — Inbox (lock: inbox_v2.lock)
  legacy_processor.py — Legacy (lock: legacy_v2.lock)
  lock.py           — Shared fcntl lock (acquire/release)
  cli.py            — Click CLI (entry: sba=sba.cli:cli)
  service_manager.py — launchd plist builder/manager
  db.py             — SQLite + FTS5 (shared ~/.sba/sba.db)
  notifier.py       — Telegram send helpers
  bot/
    bot.py          — aiogram 3.x setup
    handlers.py     — conversational handlers (no /commands)
    keyboards.py    — inline keyboards
  integrations/
    apple_notes.py
    apple_calendar.py
    google_drive.py
    google_tasks.py
    checker.py
```

## Конфиг и данные

- `~/.sba/config.yaml` — общий с v1 (не трогать v1)
- `~/.sba/sba.db` — общая БД с v1
- `.venv/` — Python 3.12 venv
- Логи: `~/.sba/logs/sba-{bot,inbox,legacy,digest}.log`
- Бэкапы: `~/.sba/backups/sba_YYYYMMDD_HHMMSS.db` (last 7)
- Замки: `~/.sba/locks/inbox_v2.lock`, `legacy_v2.lock`

## Установка после изменений

```bash
cd ~/Desktop/second-brain-agent-v2
.venv/bin/pip install . --force-reinstall
```

**НЕ** editable install (launchd нужен установленный пакет).

## CLI

```bash
.venv/bin/sba check          # проверить интеграции
.venv/bin/sba status         # статистика БД
.venv/bin/sba auth google    # переавторизация Google (Drive + Tasks)
.venv/bin/sba inbox          # inbox вручную
.venv/bin/sba legacy         # legacy вручную
.venv/bin/sba digest         # дайджест вручную
.venv/bin/sba service install all
.venv/bin/sba service status
.venv/bin/sba service logs bot
```

## Agent SDK

- Реальный API: `query()`, `create_sdk_mcp_server()`, `AgentDefinition`
- Декоратор `@tool(name, description, input_schema)` — schema передаётся явно
- Tool handlers: `async def handler(args: dict) -> {"content": [{"type": "text", "text": "..."}]}`
- SDK запускает Claude Code CLI как subprocess → нельзя вложенно из Claude Code сессии
- Module-level globals `_db`, `_notifier`, `_config` — injected via `setup()`

## Ключевые паттерны

- AppleScript даты: property-based (`set year/month/day`), не строка
- Apple Notes ID: `note.id()` из JXA (`x-coredata://...`) — стабильный
- `asyncio.to_thread()` для всех блокирующих Apple/Drive вызовов
- Google Drive таймаут: `httplib2.Http(timeout=60)` в `get_file_content()`
- fcntl lock: `LOCK_EX | LOCK_NB` — OS auto-release при краше
- Goal Tracker: JXA batch reads → Haiku трансформирует → постит в канал
- FTS5: `tokenize='unicode61'` для русского текста

## Связь с v1

- v1: `~/Desktop/second-brain-agent/` — не трогать
- v2 использует ту же `~/.sba/sba.db` и `~/.sba/config.yaml`
- Замки v2 (`inbox_v2.lock`, `legacy_v2.lock`) не конфликтуют с v1
- После перехода на v2: отключить v1 дальше (`launchctl unload`)
