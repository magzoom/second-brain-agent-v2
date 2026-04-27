# SBA 2.1 — Second Brain Agent

## Changelog v2.1 (2026-04-27)

Полный security-аудит + исправления:
- **C: parse_document whitelist** — агент может читать только `~/.sba/tmp/`, отказ на config.yaml и любые пути вне белого списка
- **C: Path traversal (Telegram)** — `file_name` обрезается до `Path(file_name).name` перед сохранением
- **C: Plist XML injection** — все переменные в plist-builders экранируются через `xml.sax.saxutils.escape()`
- **C: Dev processor task validation** — `tool_name` валидируется regex, `task` сканируется `scan_content()` перед передачей в CC; авто-git-commit удалён
- **C: JXA escaping** — `\n`, `\r`, backtick экранируются в `_escape_applescript()`
- **H: Атомарность fin_add_transaction** — INSERT + UPDATE баланса завёрнуты в SAVEPOINT
- **H: Race condition confirm_deletion** — UPDATE добавлен `AND status='waiting'`
- **H: PDF size limit** — 10 МБ лимит перед base64-кодированием
- **H: Blocking sleep** — `wait_if_dev_active()` теперь poll 60s × 15min вместо одного sleep(1800)
- **H: extension_registry thread-safe** — `threading.Lock` вокруг `_counter`
- **M: SQL indexes** — добавлены индексы на `fin_transactions(account, tx_date)`, `pending_deletions(status)`, `files_registry(status, source)`
- **M: FTS5 query sanitization** — спецсимволы FTS5 экранируются перед MATCH
- **M: WAL-safe backup** — `shutil.copy2` заменён на `sqlite3.backup()`
- **M: Рекурсия _scan_folder** — `_visited: set` + `_MAX_SCAN_DEPTH=20` против циклов
- **M: URL encoding** — `location` в wttr.in URL кодируется через `urllib.parse.quote()`
- **M: get_changes pagination guard** — `_MAX_PAGES=200` + проверка повторного токена
- **L: Атомарная запись resume** — write-then-rename для `bot_resume.json`
- **L: config validation** — пустой config.yaml → `sys.exit(1)` вместо молчаливой работы
- **Security scanner** — добавлено 11 русских паттернов + jailbreak phrases в `security.py`
- **cleanup_old_snapshots** — новый метод DB для очистки снапшотов старше 2 лет
- **Q: Shared Anthropic client** — `sba/api_client.py`: singleton `get_anthropic_client(config)`, connection-pool reuse вместо новых соединений на каждый запрос
- **Q: mlx_whisper timeout** — `asyncio.wait_for(..., timeout=300)` вокруг транскрипции аудио
- **Q: aiogram 3.7.0 → 3.13.1** — security fixes, совместимость
- **Q: Digest ranking** — посты ранжируются по `views + forwards*5 + reactions*2`; топ-N по каналу
- **Q: Digest dedup** — `digest_seen_posts` (SQLite): посты не повторяются между дайджестами; URL-дедупликация одной истории из разных каналов; очистка записей старше 7 дней
- **Q: Digest flexible limits** — `priority_channels` (config) → `priority_channel_limit` постов; остальные → `default_channel_limit`; `max_posts` — общий потолок
- **Q: Digest noise filter** — список `noise_words` (config) — фильтрует рекламные посты до передачи агенту
- **Q: Digest mood** — `digest.mood` в config.yaml → стиль подачи инжектируется в system prompt

## Архитектура

**3 агента через Claude Agent SDK:**
- `sba/agent.py` — Main Agent (оркестратор, GTD + Finance tools, 15 turns)
- `sba/digest_agent.py` — Digest Agent (утренний брифинг, независимый)
- Research Agent — subagent внутри agent.py (AgentDefinition, WebSearch + FTS5)

**7 демонов (launchd):**
- `com.sba.bot` — Telegram long polling, KeepAlive + ThrottleInterval=30
- `com.sba.inbox` — 5 запусков в день: **08:00, 12:00, 15:00, 18:00, 21:00** (StartCalendarInterval, не cron)
- `com.sba.legacy` — **09:00**, обработка накопленного + Goal Tracker (первым)
- `com.sba.digest` — **09:15**, утренний брифинг (после legacy, читает актуальные задачи)
- `com.sba.finance` — **1 янв/апр/июл/окт в 09:30**, квартальный финансовый отчёт + закят
- `com.sba.fin_remind` — **08:00** (напоминания + снапшот) + **21:00** (вечерний чек-ин); **воскресенье 21:00** — дополнительно недельный прогноз
- `com.sba.dev` — **WatchPaths** на `~/.sba/dev_request.json`; запускается автоматически когда агент вызывает `request_capability_development`; запускает Claude Code CLI для написания нового инструмента в `agent.py`; после этого бот перезапускается и выполняет исходный запрос

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
  extension_registry.py — shared pending extension actions (agent → handlers)
  security.py       — FTS5 input scanner (prompt injection, exfil, invisible chars)
  bot/
    bot.py          — aiogram 3.x setup
    handlers.py     — conversational handlers + folder_deep/folder_summary callbacks + ext_ok/ext_deny callbacks
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

## Правила для CC при автоматической разработке инструментов

Когда dev_processor.py запускает CC для добавления нового инструмента в `sba/agent.py`, код должен соответствовать prod-окружению (launchd), а не интерактивной сессии разработчика:

**PATH в launchd ограничен** — только `/usr/bin:/bin:/usr/sbin:/sbin`. Нет homebrew, нет venv bin, нет ~/.local/bin.
- Никогда не вызывай CLI-утилиты по короткому имени (`yt-dlp`, `ffmpeg`, `node`...)
- Всегда используй полный путь: `str(Path.home() / ".sba" / "venv" / "bin" / "yt-dlp")`
- Добавляй `os.path.exists(full_path)` проверку + fallback на короткое имя

**Версии библиотек** — всегда проверяй актуальный API установленной версии:
- `~/.sba/venv/bin/pip show <package>` — версия
- `~/.sba/venv/bin/python -c "import pkg; help(pkg.SomeClass.some_method)"` — сигнатура
- Не полагайся на API из памяти — библиотеки меняют интерфейс между версиями

**Импорты** — все внутри тела функции (lazy), не на уровне модуля

**Валидация** — обязательно запустить `~/.sba/venv/bin/python -c "from sba import agent"` после изменений

## Самодостраивание (agent.py)

- Инструмент `propose_capability_extension` — отправляет кнопки в Telegram: `✅ Разрешить / ❌ Отменить`
- Поддерживаемые actions: `pip_install`, `add_config_value`, `restart_bot`
- `extension_registry.py` — хранит pending actions, доступен и из agent.py, и из handlers.py
- Callbacks `ext_ok:{id}` / `ext_deny:{id}` в handlers.py — выполняют или отменяют действие
- Безопасность: валидация package name (только `[a-zA-Z0-9_\-]`), запрет на системные команды
- После pip_install или add_config_value — автоматический перезапуск бота через launchctl
- WebSearch и WebFetch добавлены напрямую в Main Agent (не через Research subagent)
- Системный промпт: агент НИКОГДА не говорит "не могу" — предлагает расширение. Если расширение требует ручной настройки — сначала честно описывает что нужно сделать вручную

## Finance модуль

### Новые таблицы БД (db.py)
- `fin_accounts` — счета (account_main=Kaspi основной, account_2=Kaspi Депозит, account_3=Freedom, account_4=Halyk, account_5=RBK/Tayyab, account_biz=Kaspi Business, **account_otbasy=ОтбасыБанк**)
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
- `finance_manage_recurring` — управление регулярными платежами; action=mark_paid отмечает платёж оплаченным в текущем месяце (не трогает балансы — только флаг)
- `finance_list_recurring` — список регулярных платежей
- `get_youtube_transcript` — транскрипт YouTube-видео + трансформация: summary (по умолч.), chapters, thread, blog, quotes. Параметры: `video_url`, `format`, `language`
- `parse_document` — извлечь текст из PDF/DOCX/TXT/MD/CSV через pymupdf (fitz) с pdfminer fallback. Параметры: `file_path`, `max_chars` (default 15000)
- `get_weather` — прогноз погоды через wttr.in (без API-ключа). Читает `~/.sba/last_location.json` если есть, иначе `digest.location` из config. Параметры: `location` (опц.), `day` (today/tomorrow). Название города: если строка ("Astana") — используется напрямую; если GPS-координаты — берётся `nearest_area` из API (wttr.in возвращает районы типа "Zaozernuy" для строки)

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
- `fin_transaction_exists(account, tx_date, amount, description)` → `bool` — 3 уровня дедупликации: (1) точное совпадение по 4 полям, (2) нечёткое: одно описание содержит другое при совпадении суммы/даты/счёта, (3) same-amount: совпадение account+date+amount при любом описании (исключает переводы); предотвращает дубль «ручная запись + выписка»
- `fin_set_balance_direct(account_name, new_balance)` — прямое обновление баланса БЕЗ транзакции корректировки (+ снапшот); используется при импорте выписок вместо `fin_update_balance`
- `fin_get_today_transactions(today_str)` → `list` — все транзакции за дату
- `fin_get_upcoming_recurring(today_day, days_in_month, current_month=None)` → `list` — платежи после сегодня до конца месяца; если передан current_month — скипает оплаченные (paid_month)
- `fin_find_matching_transactions(label, amount, month_str, strict=True)` → `list` — ищет expense-транзакции за месяц по совпадению; strict=True: AND(сумма±2%, ключевые слова); strict=False: только ключевые слова (для прошедших платежей с курсовой разницей). Переводы (tx_type IN transfer/transfer_in/transfer_out) исключаются. Игнорирует короткие/общие слова: банк, bank, депозит, deposit, платёж, payment, оплата, kaspi, каспи, кредит, credit
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
- Исключаемые категории из прогноза: `переводы людям`, `подарки`, `долги`, `семья`, `кредиты`, `коммуналка`, `подписки`, `интернет`, `сбережения`, `налоги`, `садака`, `корректировка` (фиксированные — не двоятся с разделом fixed)
- Прогноз требует минимум 1 месяц данных; с < 3 месяцев добавляет пометку «мало данных»

## Парсинг банковских выписок (bot/handlers.py)

- PDF и TXT: сначала по ключевым словам в имени файла; если UUID/неизвестное имя — `_peek_pdf_text()` читает первые 2000 символов через pdfminer, ищет ≥3 признаков выписки
- Парсинг через Claude Haiku API (~$0.02/файл, только при ручной отправке)
- Показывает превью с кнопками `✅ Импортировать / ❌ Отмена`
- После импорта: показывает актуальные балансы затронутых счетов (не подсказку)
- Дубли: 3 уровня — (1) точное совпадение (account, tx_date, amount, description), (2) нечёткое (одно описание содержит другое при совпадении суммы/даты/счёта), (3) same-amount (совпадение account+date+amount при любом описании, кроме переводов)
- Баланс: после импорта НЕ обновляется автоматически — только транзакции; `fin_set_balance_direct` используется только по явному запросу
- Halyk commissions: если в строке «Сумма операции» = 0, но есть ненулевое «Комиссия» — это комиссия банка: tx_type=expense, amount=значение комиссии. Строки с amount=0 не создаются.
- Определение счёта: по имени файла → по содержимому PDF (`_detect_account_from_content`)
- Карты/IBAN привязаны к счетам в промпте Haiku → генерирует ОБЕ стороны переводов на РАЗНЫЕ счета
  - Реквизиты хранятся в `~/.sba/config.yaml` → `finance.account_cards` (не в коде)
- Направление переводов: 'С Карт X' = деньги пришли С X НА счёт выписки (X: transfer_out, счёт выписки: transfer_in). 'На Карт X' = наоборот. Явно описано в промпте с двумя примерами.
- `_pending_statements: dict[chat_id, tuple[list, float|None]]` — хранит (транзакции, ending_balance); сбрасывается при перезапуске; «Данные устарели» → «Сессия сброшена (перезапуск бота)»

## Security (sba/security.py)

- `scan_content(text)` → `Optional[str]` — сканирует текст перед записью в FTS5
- 9 паттернов: prompt injection, disregard rules, bypass restrictions, deception, sys_prompt_override, exfil (curl/wget + $SECRET), read_secrets (.env/credentials)
- Invisible chars: только `\u202e` (RIGHT-TO-LEFT OVERRIDE) — остальные zero-width chars распространены в обычном тексте
- Интегрирован в `_index_content_tool` — блокирует индексацию + уведомление в Telegram

## Inbox (inbox_processor.py)

- Папки в Inbox теперь обрабатываются (не пропускаются)
- Логика: Haiku классифицирует по названию/содержимому → `pending_decision` в БД → карточка в Telegram
- Кнопки: ✅ Категория | 📂 Другая | 🗑 Удалить
- "📂 Другая" → показывает все 7 категорий
- "🗑 Удалить" → `add_pending_deletion()` + кнопка подтверждения
- Папка перемещается целиком (через Drive `parents` update — все файлы внутри переезжают)
- Резюме "Inbox обработан N" не отправляется (карточки уже отправлены индивидуально)

## Ключевые паттерны

- AppleScript даты: property-based (`set year/month/day`), не строка
- Apple Notes ID: `note.id()` из JXA (`x-coredata://...`) — стабильный
- Apple Notes move: через AppleScript `move note to folder` (НЕ JXA `container=` — не работает на macOS 26, ошибка -10003)
- `asyncio.to_thread()` для всех блокирующих Apple/Drive вызовов
- Google Drive таймаут: `httplib2.Http(timeout=60)` в `get_file_content()`
- fcntl lock: `LOCK_EX | LOCK_NB` — OS auto-release при краше
- Telethon session версии: Telethon 1.42.0 ожидает схему v7 (5 колонок без `tmp_auth_key`); если сессия v8 (6 колонок) — ValueError "too many values to unpack". Фикс: пересоздать таблицу sessions без `tmp_auth_key`, установить version=7 через Python+sqlite3
- Goal Tracker: JXA batch reads → Haiku трансформирует → постит в канал
- FTS5: `tokenize='unicode61'` для русского текста
- Digest: `max_posts=35`, `default_channel_limit=2`, `priority_channel_limit=5` (config); посты ранжируются по `views+forwards*5+reactions*2`; дедупликация через `digest_seen_posts` + URL cross-channel; `noise_words` фильтр; `mood` стиль подачи; msg.text[:150]; max_turns=3; parse_mode=HTML; окно 16ч; ВСЕ задачи на сегодня + просроченные (⚠️)
- Digest weather: секция 🌤 ПОГОДА между СЕГОДНЯ и ДАЙДЖЕСТ; источник wttr.in; координаты из `~/.sba/last_location.json` → fallback `digest.location` в config (сейчас "Astana")
- fin_remind вечер (21:00): если есть `~/.sba/last_location.json` — добавляет прогноз на завтра в конец чек-ин сообщения
- handlers.py location: `F.location` handler сохраняет координаты в `~/.sba/last_location.json` и отвечает прогнозом на завтра
- handlers.py parse_document: PDF/DOCX/TXT от пользователя → сохраняется в `~/.sba/tmp/`, передаётся агенту → агент вызывает `parse_document(file_path)`; caption сообщения = задача для агента
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
