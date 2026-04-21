# 🧠 Second Brain Agent 2.0

> A personal AI agent for macOS that runs in the background and talks to you via Telegram.
> Send a message in plain text — it creates tasks, searches your notes, organizes files, and delivers a morning briefing every day.

Built on **[Claude Agent SDK](https://github.com/anthropics/claude-code)** (Anthropic) with 3 specialized agents and 6 background daemons.

---

## What it does

```
You (Telegram)  ──▶  Main Agent (Claude)
                          │
          ┌───────────────┼───────────────┬───────────────┐
          ▼               ▼               ▼               ▼
    Google Tasks    Apple Notes     Google Drive   Google Calendar
    (create task)   (save note)    (upload file)  (create event)
          │               │               │               │
          └───────────────┴───────────────┴───────────────┘
                          │
                    Research Agent ──▶ Web Search
                          │
                    SQLite + FTS5
                    (knowledge base)
```

**At 08:00, 12:00, 15:00, 18:00, 21:00** → Inbox processor:
- Picks up new files from Google Drive Inbox
- Picks up new notes from Apple Notes Inbox
- Classifies each item: action / archive / delete

**Every day at 08:00** → Finance reminders:
- Checks recurring payments due today or overdue
- Sends a reminder to Telegram with the amount and due date
- Saves a daily balance snapshot for all accounts

**Every day at 21:00** → Evening finance check-in:
- Reports how many transactions were logged today
- Reminds to log any missed expenses/income

**Every Sunday at 21:00** → Weekly forecast:
- Upcoming fixed recurring payments until end of month
- Estimated variable spending based on historical averages
- Projected end-of-month balance

**Every day at 09:00** → Legacy processor:
- Walks Google Drive top-down, sends folder decision buttons to Telegram (📂 Deeper / 📝 Summary)
- On "Summary": AI generates a markdown description from file names, saves `_sba_summary.md` in Drive, indexes in FTS5
- On "Deeper": descends into subfolders on the next run (up to `legacy_folders_per_run` decisions per run)
- Posts completed tasks to your Goal Tracker Diary Telegram channel
- Rolls over overdue tasks to today

**Every day at 09:15** → Digest Agent sends a briefing:
- Tasks due today from Google Tasks
- Current weather from wttr.in (GPS location or default city)
- Top posts from your Telegram channels (via Telethon, up to 35 posts from 24+ channels)
- Categorized news: geopolitics, AI, local news, humor, health

**Quarterly (Jan/Apr/Jul/Oct 1st at 09:30)** → Finance report:
- Balance across all accounts and liabilities
- Net assets vs. nisab threshold
- Zakat status with live gold price (Yahoo Finance)

---

## Tech stack

| Layer | Technology |
|---|---|
| AI agents | [Claude Agent SDK](https://github.com/anthropics/claude-code) (Anthropic) |
| LLM model | Claude Haiku 4.5 (configurable) |
| Task manager | Google Tasks API |
| File storage | Google Drive API (OAuth2) |
| Notes | Apple Notes via JXA (JavaScript for Automation) |
| Calendar | Google Calendar API (OAuth2) |
| Messaging | Telegram Bot API (aiogram 3.x) |
| Telegram reader | Telethon (userbot for channel reading) |
| Database | SQLite with FTS5 full-text search |
| Scheduler | macOS launchd (6 background daemons) |
| Language | Python 3.12 |

---

## Native macOS alternative

By default the agent uses Google services (Tasks, Calendar, Drive). If you prefer a fully offline, native macOS stack, both Apple integration files are already included — you just need to swap the tool handlers in `agent.py`:

| Google service | Apple alternative | Existing file |
|---|---|---|
| Google Calendar | Apple Calendar | `sba/integrations/apple_calendar.py` |
| Google Drive | — | No native equivalent with a programmable API |

Apple Notes is used regardless of which stack you choose.

---

## What you can say to the bot

No commands needed — just plain text:

| Message | What happens |
|---|---|
| `what's today?` | Shows tasks due today or overdue from Google Tasks |
| `what's this week?` | Tasks for the next 7 days |
| `remind me to call the doctor on Friday` | Creates a task in Google Tasks with the right category |
| `find my notes about the project` | Full-text search across Google Drive + Apple Notes |
| `research the topic of AI in healthcare` | Launches Research Agent: web search + personal base |
| `save this link` | Creates a note in Apple Notes |
| *(forward a PDF/TXT bank statement)* | Parses transactions with Claude Haiku, shows preview with confirm/cancel. Transfers between own accounts detected via "С Карт X" / "На Карт X" patterns |
| *(forward PDF/DOCX/TXT document)* | Agent reads content via `parse_document` (pymupdf) and responds. Add caption as instruction: "translate", "summarize" |
| *(share location)* | Saves GPS coords to `~/.sba/last_location.json`, replies with tomorrow's forecast. Used in morning digest and evening check-in |
| `YouTube link` | Fetches transcript and formats it. Specify format: "make chapters", "write thread", "write article", "quotes" |
| `what's the weather?` | Forecast from saved GPS location or default city (Astana) |
| `I paid the gym` | Marks recurring payment as paid for current month (suppresses reminders until next month) |
| *(forward any other file or photo)* | Uploads to Google Drive Inbox for processing |
| `how much is on my accounts?` | Shows balance across all accounts |
| `spent 5000 on gas` | Logs a transaction in the finance module |
| `what are the last expenses on main account?` | Shows recent transactions for an account |
| `paid installment 89960` | Reduces installment debt and logs expense |
| `paid back John 100000` | Reduces liability; auto-closes and congratulates when paid off |
| `zakat status` | Calculates zakat status (nisab via live gold price) |
| `add debt John 777000` | Adds a new liability to track |

Technical commands: `/status` (DB stats), `/log` (last 20 log lines)

CLI commands for re-authorization:
```bash
.venv/bin/sba auth google       # re-authorize Google Drive + Tasks (opens browser)
.venv/bin/sba auth userbot      # re-authorize Telegram userbot for digest channel reading
```

---

## Life categories (GTD-style)

Everything is classified into 7 categories — tasks, notes, and files:

| # | Category | Covers |
|---|---|---|
| 1 | `1_Health_Energy` | health, sport, nutrition, medicine |
| 2 | `2_Business_Career` | work, projects, career |
| 3 | `3_Finance` | money, investments, budget |
| 4 | `4_Family_Relationships` | family, relationships, friends |
| 5 | `5_Personal Growth` | learning, self-development |
| 6 | `6_Brightness life` | travel, hobbies, entertainment |
| 7 | `7_Spirituality` | values, meaning, reflection |

Google Tasks lists and Google Drive folders are created automatically per category.

---

## Prerequisites

- macOS (tested on macOS 15+, Apple Silicon)
- Python 3.12
- Telegram account + a bot token from [@BotFather](https://t.me/BotFather)
- Anthropic API key → [console.anthropic.com](https://console.anthropic.com)
- Google account with Drive and Tasks enabled
- Apple Notes access (Full Disk Access for python3.12 in System Settings)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/magzoom/second-brain-agent-v2.git
cd second-brain-agent-v2
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install .
```

### 2. Create config

```bash
mkdir -p ~/.sba
cp config.yaml.example ~/.sba/config.yaml
nano ~/.sba/config.yaml   # fill in your tokens
```

**What to fill in `~/.sba/config.yaml`:**

```yaml
owner:
  telegram_chat_id: 123456789        # your Telegram chat ID — get it from @userinfobot

telegram_bot:
  token: "1234567890:AAF..."         # from @BotFather

telegram_userbot:
  api_id: 12345678                   # from https://my.telegram.org → API development tools
  api_hash: "abcdef1234567890..."    # same page

anthropic:
  api_key: "sk-ant-api03-..."        # from https://console.anthropic.com

goal_tracker:
  channel_id: -1001234567890         # Telegram channel ID for Goal Tracker Diary (optional)
```

### 3. Authorize Google (Drive + Tasks)

Put your `credentials.json` from [Google Cloud Console](https://console.cloud.google.com) into `~/.sba/google_credentials.json`, then:

```bash
.venv/bin/sba auth google    # opens browser, saves token to ~/.sba/google_token.json
```

Required Google API scopes (enabled in Cloud Console):
- Google Drive API
- Google Tasks API
- Google Calendar API

### 4. Authorize Telegram userbot (for digest channel reading)

```bash
.venv/bin/sba auth userbot    # interactive: enter phone + Telegram code
```

### 5. Allow macOS permissions

System Settings → Privacy & Security → Automation → allow `python3.12` access to **Notes**.

### 6. Check everything

```bash
.venv/bin/sba check
```

Expected output:
```
✅  Apple Notes          NoteStore.sqlite found
✅  Google Tasks         accessible (7 lists)
✅  Google Drive         API accessible
✅  Telegram Bot         @your_bot_name
✅  Telegram Userbot     Session file found
✅  Claude API           accessible
```

### 7. Install background daemons

```bash
.venv/bin/sba service install all
.venv/bin/sba service status
```

---

## CLI reference

```bash
.venv/bin/sba check               # check all integrations
.venv/bin/sba status              # database statistics
.venv/bin/sba auth google         # re-authorize Google OAuth2 (opens browser)
.venv/bin/sba auth userbot        # re-authorize Telegram userbot (interactive)

.venv/bin/sba inbox               # run inbox processor manually
.venv/bin/sba legacy              # run legacy processor manually
.venv/bin/sba digest              # run morning digest manually
.venv/bin/sba finance             # run quarterly finance report manually
.venv/bin/sba fin-remind          # run daily payment reminders manually

.venv/bin/sba backup              # backup database

.venv/bin/sba service install all      # install all 6 daemons
.venv/bin/sba service uninstall all    # remove all daemons
.venv/bin/sba service status           # show daemon status
.venv/bin/sba service logs bot         # tail bot log
```

---

## Project structure

```
sba/
├── agent.py              # Main Agent — Claude SDK orchestrator (GTD + Finance tools)
├── digest_agent.py       # Digest Agent — morning briefing
├── inbox_processor.py    # Inbox daemon — runs at 08:00, 12:00, 15:00, 18:00, 21:00
├── legacy_processor.py   # Legacy daemon — indexes archive + Goal Tracker
├── finance_processor.py  # Finance daemon — quarterly report (Jan/Apr/Jul/Oct 1st)
├── fin_remind_processor.py # Finance reminders (08:00) + evening check-in (21:00) + Sunday forecast
├── finance.py            # Zakat calc, account aliases, gold price (Yahoo Finance)
├── lock.py               # Shared fcntl process lock
├── cli.py                # CLI entry point (Click)
├── db.py                 # SQLite + FTS5 knowledge base + fin_* tables
├── notifier.py           # Telegram send helpers
├── service_manager.py    # launchd plist generator
├── bot/
│   ├── bot.py            # aiogram 3.x setup
│   ├── handlers.py       # message and callback handlers
│   └── keyboards.py      # inline keyboards (deletion + folder decisions)
└── integrations/
    ├── apple_notes.py    # JXA read + AppleScript write
    ├── apple_calendar.py # AppleScript calendar events (kept for compatibility)
    ├── google_drive.py   # Drive API (OAuth2, changes, upload, summary files)
    ├── google_tasks.py   # Tasks API (create, today, rollover)
    ├── google_calendar.py # Calendar API (OAuth2, create events)
    └── checker.py        # integration health checks

~/.sba/
├── config.yaml           # your configuration (never committed)
├── sba.db                # SQLite database
├── google_credentials.json
├── google_token.json
├── telegram_userbot.session
├── logs/                 # sba-bot.log, sba-inbox.log, ...
├── backups/              # auto DB backups (last 7)
└── locks/                # process lock files
```

---

## How agents work

**Main Agent** (15 turns max) handles every Telegram message:
1. Reads your message + last 5 messages for context
2. Decides what tool to call: create task, search notes, move file, etc.
3. Returns a plain-text response

**Research Agent** (subagent, called by Main Agent):
- Searches the web (WebSearch + WebFetch)
- Searches your personal knowledge base (FTS5)
- Returns a synthesized answer

**Digest Agent** (standalone, runs at 09:15):
- Gets tasks due today from Google Tasks
- Reads last 24h posts from all your Telegram channels
- Selects top items by category
- Sends one formatted message

---

## Troubleshooting

**Bot doesn't respond**
```bash
launchctl list | grep com.sba.bot   # should show a PID
.venv/bin/sba service logs bot      # check the log
```

**Apple Notes takes too long (>30 sec)**
→ System Settings → Privacy & Security → Automation → allow python3.12

**Google auth error**
```bash
.venv/bin/sba auth google    # re-authorize, this recreates the token
```

**Digest doesn't run from CLI**
→ This is expected: Claude Agent SDK cannot run nested inside a Claude Code session. It works correctly from launchd.

---

## License

MIT
