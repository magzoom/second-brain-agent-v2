# Second Brain Agent 2.0

Персональный разговорный AI-агент на macOS. Общаешься в Telegram свободным текстом — агент сам создаёт задачи, ищет информацию, организует файлы и заметки.

---

## Как это работает

**3 агента через Claude Agent SDK:**
- **Main Agent** — отвечает на сообщения в Telegram, создаёт задачи, ищет, организует
- **Research Agent** — ищет в интернете и личной базе знаний (вызывается Main Agent'ом)
- **Digest Agent** — утренний дайджест в 08:00: задачи + события + каналы

**4 фоновых процесса (launchd):**
- `com.sba.bot` — Telegram бот, работает постоянно
- `com.sba.inbox` — каждые 2 часа обрабатывает новое из Google Drive и Apple Notes Inbox
- `com.sba.legacy` — ежедневно в 09:00 обрабатывает архивные файлы + Goal Tracker Diary
- `com.sba.digest` — ежедневно в 08:00 отправляет утренний брифинг

---

## Что умеет бот

Пиши свободным текстом:

| Что написать | Что произойдёт |
|---|---|
| «что на сегодня?» | Задачи на сегодня из Reminders |
| «что на неделе?» | Задачи на ближайшие 7 дней |
| «напомни купить молоко в пятницу» | Задача в Reminders с датой |
| «найди мои заметки про ВРЦ» | Поиск по FTS5 базе знаний |
| «изучи тему AI в медицине» | Research Agent ищет в интернете |
| «сохрани эту ссылку» | Заметка в Apple Notes |
| пересланный файл/фото | Загружается в Google Drive Inbox |

Технические команды: `/status`, `/log`

---

## Категории жизни

| Категория | Содержание |
|---|---|
| `1_Health_Energy` | здоровье, спорт, питание, медицина |
| `2_Business_Career` | работа, проекты, карьера |
| `3_Finance` | деньги, инвестиции, бюджет |
| `4_Family_Relationships` | семья, отношения, друзья |
| `5_Personal Growth` | обучение, саморазвитие |
| `6_Brightness life` | путешествия, хобби, развлечения |
| `7_Spirituality` | ценности, смысл, рефлексия |

---

## Управление

```bash
# Проверить интеграции
.venv/bin/sba check

# Статистика базы
.venv/bin/sba status

# Запустить вручную
.venv/bin/sba inbox
.venv/bin/sba legacy
.venv/bin/sba digest

# Бэкап базы
.venv/bin/sba backup

# Демоны
.venv/bin/sba service install all
.venv/bin/sba service status
.venv/bin/sba service logs bot
.venv/bin/sba service uninstall all
```

---

## После изменений кода

```bash
cd ~/Desktop/second-brain-agent-v2
.venv/bin/pip install . --force-reinstall
.venv/bin/sba service install all
```

---

## Структура данных

```
~/.sba/
├── config.yaml                  # конфигурация (общая с v1)
├── sba.db                       # SQLite база знаний
├── google_credentials.json      # OAuth credentials
├── google_token.json            # OAuth токен
├── telegram_userbot.session     # Telethon сессия
├── logs/
│   ├── sba-bot.log
│   ├── sba-inbox.log
│   ├── sba-legacy.log
│   └── sba-digest.log
├── backups/                     # автобэкапы БД (последние 7)
└── locks/
    ├── inbox_v2.lock
    └── legacy_v2.lock
```

---

## Решение проблем

**Бот не отвечает**
→ `launchctl list | grep com.sba.bot` — должен быть PID
→ `.venv/bin/sba service logs bot` — смотри лог

**Apple Reminders/Notes/Calendar зависают (>30 сек)**
→ System Settings → Privacy & Security → Automation → разрешить python3.12

**Google Drive: ошибка авторизации**
→ Удалить `~/.sba/google_token.json`, переавторизоваться

**Digest не запускается вручную**
→ Норма: SDK нельзя запустить внутри Claude Code сессии. Из launchd работает.
