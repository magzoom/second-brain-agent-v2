# 🧠 Second Brain Agent 2.0

> A personal AI agent for macOS that runs in the background and talks to you via Telegram.
> Send a message in plain text — it creates tasks, searches your notes, organizes files, and delivers a morning briefing every day.

Built on **[Claude Agent SDK](https://github.com/anthropics/claude-code)** (Anthropic) with 3 specialized agents and 4 background daemons.

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

**Every morning at 08:00** → Digest Agent sends a briefing:
- Tasks due today from Google Tasks
- Top posts from your Telegram channels (via Telethon)
- Categorized news: geopolitics, AI, local news, humor, health

**Every 2 hours** → Inbox processor:
- Picks up new files from Google Drive Inbox
- Picks up new notes from Apple Notes Inbox
- Classifies each item: action / archive / delete

**Every day at 09:00** → Legacy processor:
- Indexes unclassified files from Google Drive and Apple Notes
- Posts completed tasks to your Goal Tracker Diary Telegram channel
- Rolls over overdue tasks to today

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
| Scheduler | macOS launchd (4 background daemons) |
| Language | Python 3.12 |

---

## Native macOS alternative

By default the agent uses Google services (Tasks, Calendar, Drive). If you prefer a fully offline, native macOS stack, both Apple integration files are already included — you just need to swap the tool handlers in `agent.py`:

| Google service | Apple alternative | Existing file |
|---|---|---|
| Google Tasks | Apple Reminders | `sba/integrations/apple_reminders.py` |
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
| *(forward a file or photo)* | Uploads to Google Drive Inbox for processing |

Technical commands: `/status` (DB stats), `/log` (last 20 log lines)

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
.venv/bin/pip install -e .
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

### 4. Authorize Telegram userbot (for digest channel reading)

```bash
.venv/bin/python -c "
from telethon.sync import TelegramClient
from pathlib import Path
import yaml
config = yaml.safe_load(open(Path.home() / '.sba/config.yaml'))
ub = config['telegram_userbot']
client = TelegramClient(str(Path.home() / '.sba/telegram_userbot'), ub['api_id'], ub['api_hash'])
client.start()
client.disconnect()
print('Done')
"
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
.venv/bin/sba auth google         # re-authorize Google OAuth2

.venv/bin/sba inbox               # run inbox processor manually
.venv/bin/sba legacy              # run legacy processor manually
.venv/bin/sba digest              # run morning digest manually

.venv/bin/sba backup              # backup database

.venv/bin/sba service install all      # install all 4 daemons
.venv/bin/sba service uninstall all    # remove all daemons
.venv/bin/sba service status           # show daemon status
.venv/bin/sba service logs bot         # tail bot log
```

---

## Project structure

```
sba/
├── agent.py              # Main Agent — Claude SDK orchestrator
├── digest_agent.py       # Digest Agent — morning briefing
├── inbox_processor.py    # Inbox daemon — processes new items every 2h
├── legacy_processor.py   # Legacy daemon — indexes archive + Goal Tracker
├── lock.py               # Shared fcntl process lock
├── cli.py                # CLI entry point (Click)
├── db.py                 # SQLite + FTS5 knowledge base
├── notifier.py           # Telegram send helpers
├── service_manager.py    # launchd plist generator
├── bot/
│   ├── bot.py            # aiogram 3.x setup
│   ├── handlers.py       # message and callback handlers
│   └── keyboards.py      # inline keyboards for deletion confirmations
└── integrations/
    ├── apple_notes.py    # JXA read + AppleScript write
    ├── apple_calendar.py # AppleScript calendar events
    ├── google_drive.py   # Drive API (OAuth2, changes, upload)
    ├── google_tasks.py   # Tasks API (create, today, rollover)
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

**Digest Agent** (standalone, runs at 08:00):
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
