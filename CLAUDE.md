# SBA 2.0 — Second Brain Agent

## Архитектура

**3 агента через Claude Agent SDK:**
- `sba/agent.py` — Main Agent (оркестратор, GTD + Finance tools, 15 turns)
- `sba/digest_agent.py` — Digest Agent (утренний брифинг, независимый)
- Research Agent — subagent внутри agent.py (AgentDefinition, WebSearch + FTS5)

**6 демонов (launchd):**
- `com.sba.bot` — Telegram long polling, KeepAlive + ThrottleInterval=30
- `com.sba.inbox` — 5 запусков в день: **08:00, 12:00, 15:00, 18:00, 21:00** (StartCalendarInterval, не cron)
- `com.sba.legacy` — **09:00**, обработка накопленного + Goal Tracker (первым)
- `com.sba.digest` — **09:15**, утренний брифинг (после legacy, читает актуальные задачи)
- `com.sba.finance` — **1 янв/апр/июл/окт в 09:30**, квартальный финансовый отчёт + закят
- `com.sba.fin_remind` — **08:00** (напоминания + снапшот) + **21:00** (вечерний чек-ин); **воскресенье 21:00** — дополнительно недельный прогноз

## Ключевые файлы

```
sba/
  agent.py          — Main Agent + tools (GTD + Finance)
  digest_agent.py   — Digest Agent (Telethon + Google Tasks)
  inbox_processor.py — Inbox (lock: inbox_v2.lock)
  legacy_processor.py — Legacy (lock: legacy_v2.lock)
  finance_processor.py — Квартальный отчёт (lock: finance_v2.lock)
  fin_remind_processor.py — Ежедневные напоминания (lock: fin_remind_v2.lock)
  finance.py        — Логика закята, псевдонимы счетов, курс золота (Yahoo Finance)
  lock.py           — Shared fcntl lock (acquire/release)
  cli.py            — Click CLI (entry: sba=sba.cli:cli)
  service_manager.py — launchd plist builder/manager
  db.py             — SQLite + FTS5 (shared ~/.sba/sba.db)
  notifier.py       — Telegram send helpers
  bot/
    bot.py          — aiogram 3.x setup
    handlers.py     — conversational handlers + folder_deep/folder_summary callbacks
    keyboards.py    — inline keyboards: confirm_delete + folder_decision
  integrations/
    apple_notes.py
    google_drive.py
    google_tasks.py
    google_calendar.py
    checker.py
```

## Конфиг и данные

- `~/.sba/config.yaml` — общий с v1 (не трогать v1), **chmod 600**
- `~/.sba/sba.db` — общая БД с v1
- `.venv/` — Python 3.12 venv
- Логи: `~/.sba/logs/sba-{bot,inbox,legacy,digest,finance,fin_remind}.log`
- Бэкапы: `~/.sba/backups/sba_YYYYMMDD_HHMMSS.db` (last 7)
- Замки: `~/.sba/locks/inbox_v2.lock`, `legacy_v2.lock`, `finance_v2.lock`, `fin_remind_v2.lock`

## Установка после изменений

```bash
cd ~/Desktop/second-brain-agent-v2
~/.sba/venv/bin/pip install . --no-deps -q   # production venv (launchd использует этот)
.venv/bin/sba service install bot            # перезапустить нужный демон
```

**Два venv:**
- `~/.sba/venv/` — production, используется launchd-демонами
- `.venv/` — dev, только для запуска CLI-команд (`sba service install`, `sba check` и т.д.)

Устанавливать нужно в **оба** при изменении кода, или только в `~/.sba/venv/` если меняешь только логику демонов.

## CLI

```bash
.venv/bin/sba check          # проверить интеграции
.venv/bin/sba status         # статистика БД
.venv/bin/sba auth google    # переавторизация Google (Drive + Tasks)
.venv/bin/sba inbox          # inbox вручную
.venv/bin/sba legacy         # legacy вручную
.venv/bin/sba digest         # дайджест вручную
.venv/bin/sba finance        # квартальный финансовый отчёт вручную
.venv/bin/sba fin-remind     # ежедневные напоминания вручную
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

## Finance модуль

### Новые таблицы БД (db.py)
- `fin_accounts` — счета (account_main=Kaspi основной, account_2=Kaspi второй, account_3=Freedom, account_4=Halyk, account_5=RBK/Tayyab, account_biz=Kaspi Business)
- `fin_transactions` — все транзакции (amount, category, label, source)
- `fin_liabilities` — обязательства (кредиты, долги людям)
- `fin_zakat_profile` — профиль закята (year_start, hawl_start)
- `fin_recurring` — регулярные платежи (day_of_month=0 → ежедневно)

### Новые инструменты агента (agent.py)
- `finance_get_balance` — баланс счетов
- `finance_add_transaction` — добавить расход/доход
- `finance_update_account` — обновить остаток на счёте
- `finance_manage_liability` — добавить/обновить/закрыть долг/кредит. При остатке ≤ 0 автоматически ставит is_active=0 и поздравляет
- `finance_get_zakat` — статус закята (расчёт через Yahoo Finance GC=F + KZT=X)
- `finance_get_summary` — сводка за период
- `finance_get_transactions` — последние транзакции по счёту или всем счетам
- `finance_manage_recurring` — управление регулярными платежами
- `finance_list_recurring` — список регулярных платежей

### Псевдонимы счетов (finance.py)
ACCOUNT_ALIASES: "основной", "main" → account_main; "второй", "second" → account_2; "бизнес", "business" → account_biz. Настраиваются под свои банки.

### Расчёт закята
- Нисаб = 85г × цена золота (GC=F через Yahoo Finance) × курс USD/KZT (KZT=X)
- Зakat обязателен если net_assets ≥ nisab (≈ 5.8 млн ₸ на март 2026)
- Текущий статус: закят НЕ обязателен (долги превышают активы)

### Регулярные платежи (seeded)
17 записей: подписки (Apple, Google, YouTube, Telegram, Perplexity, Grok, Claude), Kaspi-кредиты (тренажёрка, импланты), ОтбасыБанк депозит, ИП, коммуналка, интернет, Аниса-математика, бензин, садака (ежедневно 100₸)

### Логика проверки оплаты (fin_remind_processor.py + agent.py)

**Утреннее напоминание (08:00):**
- Ежедневные (day_of_month=0) — всегда в обычный список, без проверки транзакций
- Разовые — `fin_find_matching_transactions(strict=True)` для каждого
  - Совпадение найдено → отдельное сообщение с кнопками ❓ "Да, оплачено" / "Нет, не оплачено"
  - Нет совпадений → обычное напоминание
- `paid_month` уже выставлен → платёж скипается полностью

**Callbacks (bot/handlers.py):**
- `recur_paid:{id}` → `fin_mark_recurring_paid(id, YYYY-MM)` — молчит до следующего месяца
- `recur_unpaid:{id}` → убирает кнопки, платёж остаётся активным

**finance_list_recurring (agent.py), mode=upcoming (default):**
- paid_month == current_month → скип
- day_of_month < today → `fin_find_matching_transactions(strict=False)` (keyword only); найдено → скип; нет → "просрочен"
- day_of_month >= today → показывается с датой

**finance_list_recurring, mode=all:**
- Все активные, оплаченные помечены ✅

**Триггеры в промпте:**
- "предстоящие/ближайшие платежи", "что платить" → mode=upcoming
- "регулярные платежи", "мои подписки", "список напоминаний" → mode=all

## Иерархическая индексация Drive (legacy)

- `_scan_folder(service, db, notifier, config, folder_id, path_stack, decisions_counter, ...)` — рекурсивный обход
- `_send_folder_decision(...)` — регистрирует как `pending_decision`, вызывает Haiku для подсказки, шлёт кнопки в Telegram
- Статусы папок (type='folder'): `pending_decision` → `pending_deep` | `folder_summary` | `folder_done`
- `path_stack: list[str]` — хлебные крошки; путь хранится в поле `path` в БД, восстанавливается при `pending_deep`
- `_sba_summary.md` создаётся в Drive через `create_summary_file()`, регистрируется как `processed` (inbox пропускает)
- `decisions_counter: dict` — ограничивает только новые решения (status=None); `pending_deep` рекурсируются ВСЕГДА (не прерывает итерацию); файлы внутри папок legacy НЕ обрабатывает (только медиа-уведомление)
- Старт только из корневых категорий — `pending_deep` НЕ добавляются отдельно в start list (иначе двойное сканирование)
- `asyncio.to_thread(lambda: list(_list(service, folder_id, False)))` — generator→list in thread

## DB — методы для папок (db.py)

- `upsert_folder(source, source_id, title, path)` → `(reg_id, is_new)` — INSERT OR IGNORE, не перезаписывает статус
- `get_folder_status(source, source_id)` → `Optional[str]`
- `set_folder_status(source, source_id, status)`, `set_folder_status_by_id(reg_id, status)`, `get_file_by_id(reg_id)`
- `get_folders_by_status(status)` → `list`
- `get_entry_type(source, source_id)` → `Optional[str]`
- `upsert_file` — добавлен `entry_type: str = "file"`, UPDATE ветка включает `type=?`; не сбрасывает статус `pending` (файлы в ожидании удаления не повторно обрабатываются)
- `mark_deletion_executed` — UPDATE только если `status='confirmed'`; защита от race condition при отмене

## DB — методы для pending_deletions (db.py)

- `get_confirmed_deletions()` → `list` — элементы со статусом `confirmed`, ещё не удалённые
- `cancel_deletion(deletion_id)` — ставит статус `cancelled`
- `get_stale_pending_deletions(hours=20)` → `list` — просроченные `waiting` запросы
- `update_stale_deletion_msg(deletion_id, new_msg_id)` — обновляет msg_id и сбрасывает `created_at`
- Прямой доступ к `db._conn` вне `db.py` **запрещён** — все SQL-запросы только через методы класса

## DB — finance методы (db.py)

- `fin_add_transaction(account, amount, tx_type, category, description, tx_date)` — добавить транзакцию
- `fin_transaction_exists(account, tx_date, amount, description)` → `bool` — проверка дубля по 4 полям
- `fin_get_today_transactions(today_str)` → `list` — все транзакции за дату
- `fin_get_upcoming_recurring(today_day, days_in_month, current_month=None)` → `list` — платежи после сегодня до конца месяца; если передан current_month — скипает оплаченные (paid_month)
- `fin_find_matching_transactions(label, amount, month_str, strict=True)` → `list` — ищет expense-транзакции за месяц по совпадению; strict=True: AND(сумма±2%, ключевые слова); strict=False: только ключевые слова (для прошедших платежей с курсовой разницей). Переводы (tx_type=transfer) исключаются. Игнорирует короткие/общие слова: банк, bank, депозит, deposit, платёж, payment, оплата
- `fin_get_recurring_by_id(id)` → `dict|None` — одна запись по id
- `fin_mark_recurring_paid(id, month_str)` — выставить paid_month; сбрасывается автоматически в новом месяце
- `fin_get_avg_variable_spend(excluded_categories: set)` → `float` — среднемесячные переменные расходы (last 2 months)
- `fin_get_month_variable_spend(month_str, excluded_categories: set)` → `float` — переменные расходы за месяц
- `fin_get_total_balance()` → `float` — сумма всех счетов
- `fin_count_months_with_data()` → `int` — кол-во месяцев с данными (для оценки надёжности прогноза)
- `fin_save_all_snapshots(source)` — снапшот всех счетов в `fin_balance_snapshots`
- `cleanup_stale_new_files(source, days)` — перевести старые `new` в `skipped` (для gdrive)

## Finance модуль — прогноз и чек-ин (fin_remind_processor.py)

- **Утро 08:00**: снапшот + напоминания о сегодняшних платежах → отдельное сообщение
- **Вечер 21:00**: чек-ин — сколько транзакций внесено за день, напоминание если 0
- **Воскресенье 21:00**: дополнительно прогноз до конца месяца (фиксированные + переменные)
- Исключаемые категории из прогноза: `переводы людям`, `подарки`, `долги`, `семья`
- Прогноз требует минимум 1 месяц данных; с < 3 месяцев добавляет пометку «мало данных»

## Парсинг банковских выписок (bot/handlers.py)

- PDF и TXT файлы с ключевыми словами в имени (kaspi, выписка, halyk, freedom, депозит, statement) → автоматически маршрутизируются на парсинг вместо Drive
- Парсинг через Claude Haiku API (~$0.02/файл, только при ручной отправке)
- Показывает превью с кнопками `✅ Импортировать / ❌ Отмена`
- Дубли проверяются по `(account, tx_date, amount, description)` — повторная загрузка той же выписки безопасна
- Определение счёта по имени файла: gold/kaspi → account_main, депозит/d1000 → account_2, freedom → account_3, halyk → account_4
- `_pending_statements: dict[chat_id, list]` — временное хранение до подтверждения

## Ключевые паттерны

- AppleScript даты: property-based (`set year/month/day`), не строка
- Apple Notes ID: `note.id()` из JXA (`x-coredata://...`) — стабильный
- Apple Notes move: через AppleScript `move note to folder` (НЕ JXA `container=` — не работает на macOS 26, ошибка -10003)
- `asyncio.to_thread()` для всех блокирующих Apple/Drive вызовов
- Google Drive таймаут: `httplib2.Http(timeout=60)` в `get_file_content()`
- fcntl lock: `LOCK_EX | LOCK_NB` — OS auto-release при краше
- Goal Tracker: JXA batch reads → Haiku трансформирует → постит в канал
- FTS5: `tokenize='unicode61'` для русского текста
- Digest: MAX_POSTS=35, MAX_PER_CHANNEL=2, msg.text[:120], max_turns=3, parse_mode=HTML (не markdown); fallback отправляет msg.result если send_digest не был вызван; окно Telegram-постов 16ч; показываются ВСЕ задачи на сегодня + просроченные, ⚠️ если due < today
- Медиа-уведомления: кнопка "Ознакомлен" → `media_ack:{reg_id}` callback → `folder_done`; `_scan_folder` делает early return если own status == `folder_done`
- `send_legacy_report` не отправляется если `processed == 0 and errors == 0` (карточки папок уже отправлены индивидуально)
- Legacy auth failure: при `invalid_grant` ставит `stats["auth_failed"]=True`, шлёт одно сообщение с инструкцией и останавливается (Apple Notes и итоговый отчёт не запускаются). После `sba auth google` нужен ручной запуск или ждать 09:00.
- macOS Full Disk Access: оба python3.12 (symlink и реальный `/opt/homebrew/Frameworks/Python.framework/Versions/3.12/bin/python3.12`) должны быть включены в System Settings → Privacy → Full Disk Access
- Вложенный запуск из Claude Code: `CLAUDECODE=""` перед командой (иначе SDK падает с "nested session")

## Контроль расходов (agent.py / inbox_processor.py / legacy_processor.py)

- `ResultMessage.total_cost_usd` — логируется после каждого вызова агента
- `_cost_accumulator: list` — передаётся в `run_main_agent()`, суммируется за запуск
- `inbox.max_items_per_run` (config.yaml, default: 20) — лимит файлов за один запуск inbox
- `inbox.max_session_cost_usd` (config.yaml) — hard stop inbox + уведомление в Telegram
- `legacy.max_session_cost_usd` (config.yaml) — hard stop legacy + уведомление в Telegram
- `timezone` (config.yaml, default: `"Asia/Almaty"`) — IANA timezone для Google Calendar событий; google_tasks.py использует системный timezone
- При "Credit balance is too low" — агент шлёт уведомление в Telegram и возвращает понятное сообщение

## Google Drive — inbox фильтр

- `_process_gdrive()` обрабатывает ТОЛЬКО файлы с `inbox_folder_id` в parents (как v1)
- Файлы вне Inbox папки игнорируются — обычная работа в Drive не триггерит агента
- Причина бага (2026-03-11): Changes API возвращал ВСЕ изменения Drive → 219 файлов → 215 вызовов → $7.57 за ночь

## Связь с v1

- v1: `~/Desktop/second-brain-agent/` — не трогать
- v2 использует ту же `~/.sba/sba.db` и `~/.sba/config.yaml`
- Замки v2 (`inbox_v2.lock`, `legacy_v2.lock`) не конфликтуют с v1
- После перехода на v2: отключить v1 дальше (`launchctl unload`)
