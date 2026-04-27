"""
SQLite database layer with WAL mode.
Shared with v1 (sba.db) — v2 adds FTS5 index and user_patterns tables.
All CREATE TABLE use IF NOT EXISTS — safe to run on existing v1 DB.
"""

import sqlite3
import aiosqlite
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".sba" / "sba.db"


def get_db_path(config: Optional[dict] = None) -> Path:
    if config and "paths" in config and "db" in config["paths"]:
        return Path(config["paths"]["db"]).expanduser()
    return DB_PATH


def init_db_sync(db_path: Path) -> None:
    """Synchronous DB initialization — used at startup before async loop."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _create_tables(conn)
        conn.commit()
        logger.info(f"Database initialized at {db_path}")
    finally:
        conn.close()


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        -- v2.1 migrations (safe to re-run)
        -- task_id added to goal_tracker_posts for stable dedup by Google Task ID

        -- Performance indexes (safe to re-run — CREATE INDEX IF NOT EXISTS)
        CREATE INDEX IF NOT EXISTS idx_fin_tx_account_date
            ON fin_transactions(account, tx_date);
        CREATE INDEX IF NOT EXISTS idx_fin_tx_date
            ON fin_transactions(tx_date);
        CREATE INDEX IF NOT EXISTS idx_fin_tx_type
            ON fin_transactions(tx_type);
        CREATE INDEX IF NOT EXISTS idx_pending_del_status
            ON pending_deletions(status);
        CREATE INDEX IF NOT EXISTS idx_files_status_source
            ON files_registry(status, source);
    """)
    # Column migration: task_id (may already exist on upgraded DBs)
    try:
        conn.execute("ALTER TABLE goal_tracker_posts ADD COLUMN task_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # already exists
    # Column migration: type for hierarchical indexing (file/folder_summary/folder_skipped)
    try:
        conn.execute("ALTER TABLE files_registry ADD COLUMN type TEXT DEFAULT 'file'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # already exists
    # Column migration: paid_month for recurring payment confirmation
    try:
        conn.execute("ALTER TABLE fin_recurring ADD COLUMN paid_month TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # already exists
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_tracker_task_id "
            "ON goal_tracker_posts(task_id) WHERE task_id IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # already exists

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files_registry (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            source_id       TEXT NOT NULL,
            content_hash    TEXT,
            title           TEXT,
            path            TEXT,
            status          TEXT DEFAULT 'new',
            category        TEXT,
            classification  TEXT,
            added_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            processed_at    DATETIME,
            UNIQUE(source, source_id)
        );

        CREATE TABLE IF NOT EXISTS processing_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         INTEGER NOT NULL REFERENCES files_registry(id),
            priority        INTEGER DEFAULT 2,
            status          TEXT DEFAULT 'pending',
            attempts        INTEGER DEFAULT 0,
            error_log       TEXT,
            scheduled_for   DATE DEFAULT (date('now')),
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pending_deletions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         INTEGER NOT NULL REFERENCES files_registry(id),
            reminder_id     TEXT,
            telegram_msg_id INTEGER,
            status          TEXT DEFAULT 'waiting',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            confirmed_at    DATETIME
        );

        CREATE TABLE IF NOT EXISTS knowledge_graph (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         INTEGER NOT NULL REFERENCES files_registry(id),
            category        TEXT,
            tags            TEXT,
            summary         TEXT,
            embedding       BLOB,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS actionable_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         INTEGER NOT NULL REFERENCES files_registry(id),
            reminder_id     TEXT,
            calendar_event_id TEXT,
            status          TEXT DEFAULT 'created',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at    DATETIME
        );

        CREATE TABLE IF NOT EXISTS gdrive_sync_state (
            id              INTEGER PRIMARY KEY,
            page_token      TEXT,
            last_sync_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        INSERT OR IGNORE INTO gdrive_sync_state (id, page_token) VALUES (1, NULL);

        CREATE TABLE IF NOT EXISTS goal_tracker_posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_title  TEXT NOT NULL,
            list_name   TEXT NOT NULL,
            posted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            task_id     TEXT,
            UNIQUE(task_title, list_name)
        );

        -- v2: FTS5 knowledge search index
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
            source_id,
            source_type,
            title,
            content,
            category,
            tokenize='unicode61'
        );

        -- v2: User behaviour patterns for adaptive system prompt
        CREATE TABLE IF NOT EXISTS user_patterns (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Finance tables
        CREATE TABLE IF NOT EXISTS fin_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            label       TEXT NOT NULL,
            balance     REAL NOT NULL DEFAULT 0,
            currency    TEXT NOT NULL DEFAULT 'KZT',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS fin_transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account     TEXT,
            amount      REAL NOT NULL,
            tx_type     TEXT NOT NULL,
            category    TEXT,
            description TEXT,
            tx_date     DATE NOT NULL DEFAULT (date('now')),
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS fin_liabilities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            creditor        TEXT,
            amount          REAL NOT NULL DEFAULT 0,
            monthly_payment REAL,
            due_date        DATE,
            lib_type        TEXT NOT NULL DEFAULT 'personal',
            notes           TEXT,
            is_active       INTEGER NOT NULL DEFAULT 1,
            updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS fin_zakat_profile (
            id                  INTEGER PRIMARY KEY,
            nisab_crossed_at    DATE,
            last_check_at       DATE,
            gold_grams_wife     REAL DEFAULT 0,
            notes               TEXT,
            updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Recurring finance reminders
        CREATE TABLE IF NOT EXISTS fin_recurring (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            label           TEXT NOT NULL,
            day_of_month    INTEGER NOT NULL DEFAULT 0,
            amount          REAL,
            remind_days_before INTEGER NOT NULL DEFAULT 0,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Daily balance snapshots
        CREATE TABLE IF NOT EXISTS fin_balance_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account     TEXT NOT NULL,
            balance     REAL NOT NULL,
            snapshot_date DATE NOT NULL DEFAULT (date('now')),
            source      TEXT NOT NULL DEFAULT 'auto',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account, snapshot_date)
        );
    """)


    # Seed initial finance data (idempotent via INSERT OR IGNORE)
    _accounts = [
        ("account_main",  "Основной счёт",  0.0),
        ("account_2",     "Второй счёт",    0.0),
        ("account_3",     "Счёт 3",         0.0),
        ("account_4",     "Счёт 4",         0.0),
        ("account_5",     "Счёт 5",         0.0),
        ("account_biz",   "Бизнес счёт",    0.0),
        ("account_otbasy", "ОтбасыБанк",    0.0),
    ]
    for n, l, b in _accounts:
        conn.execute(
            "INSERT OR IGNORE INTO fin_accounts (name, label, balance) VALUES (?,?,?)", (n, l, b)
        )
    _liabilities = [
        ("people_debt",       "Долги людям",        0.0, None, None, "personal",    ""),
        ("kaspi_installment", "Рассрочка Kaspi",    0.0, None, None, "installment", ""),
        ("transport_tax",     "Налог на транспорт",  0.0, None, None, "tax",         ""),
    ]
    for n, c, a, mp, dd, lt, nt in _liabilities:
        conn.execute(
            "INSERT OR IGNORE INTO fin_liabilities (name, creditor, amount, monthly_payment, due_date, lib_type, notes) VALUES (?,?,?,?,?,?,?)",
            (n, c, a, mp, dd, lt, nt)
        )
    conn.execute("INSERT OR IGNORE INTO fin_zakat_profile (id) VALUES (1)")
    conn.commit()


async def get_connection(db_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=30000")
    await conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = aiosqlite.Row
    return conn


class Database:
    """Async database wrapper. Use as async context manager."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self):
        self._conn = await get_connection(self.db_path)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            await self._conn.close()

    # ── files_registry ────────────────────────────────────────────────────────

    async def upsert_file(
        self, source: str, source_id: str, content_hash: str = "",
        title: str = "", path: str = "", entry_type: str = "file",
    ) -> tuple[int, bool]:
        # Atomic INSERT OR IGNORE — eliminates SELECT→INSERT race condition
        async with self._conn.execute(
            "INSERT OR IGNORE INTO files_registry (source, source_id, content_hash, title, path, type) VALUES (?,?,?,?,?,?)",
            (source, source_id, content_hash, title, path, entry_type),
        ) as cur:
            was_inserted = cur.rowcount == 1
            if was_inserted:
                file_id = cur.lastrowid
                await self._conn.commit()
                return file_id, True

        # Row existed — fetch and check if content changed
        async with self._conn.execute(
            "SELECT id, content_hash, status FROM files_registry WHERE source=? AND source_id=?",
            (source, source_id),
        ) as cur:
            row = await cur.fetchone()

        # Don't reset files pending deletion — avoids reprocessing after agent-initiated deletion
        if row["content_hash"] != content_hash and row["status"] != "pending":
            await self._conn.execute(
                "UPDATE files_registry SET content_hash=?, title=?, path=?, type=?, status='new', processed_at=NULL WHERE id=?",
                (content_hash, title, path, entry_type, row["id"]),
            )
            await self._conn.commit()
            return row["id"], True

        return row["id"], False

    async def upsert_folder(
        self, source: str, source_id: str, title: str, path: str = "",
    ) -> tuple[int, bool]:
        """Register a folder in files_registry. Does not overwrite existing status."""
        async with self._conn.execute(
            "INSERT OR IGNORE INTO files_registry (source, source_id, content_hash, title, path, type) VALUES (?,?,?,?,?,?)",
            (source, source_id, "", title, path, "folder"),
        ) as cur:
            if cur.rowcount == 1:
                await self._conn.commit()
                return cur.lastrowid, True
        async with self._conn.execute(
            "SELECT id FROM files_registry WHERE source=? AND source_id=?",
            (source, source_id),
        ) as cur:
            row = await cur.fetchone()
            return row["id"], False

    async def get_folder_status(self, source: str, source_id: str) -> Optional[str]:
        """Return status of a registered folder, or None if not registered."""
        async with self._conn.execute(
            "SELECT status FROM files_registry WHERE source=? AND source_id=? AND type='folder'",
            (source, source_id),
        ) as cur:
            row = await cur.fetchone()
            return row["status"] if row else None

    async def set_folder_status(self, source: str, source_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE files_registry SET status=? WHERE source=? AND source_id=?",
            (status, source, source_id),
        )
        await self._conn.commit()

    async def set_folder_status_by_id(self, reg_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE files_registry SET status=? WHERE id=?", (status, reg_id)
        )
        await self._conn.commit()

    async def get_file_by_id(self, reg_id: int) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM files_registry WHERE id=?", (reg_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_file_status(self, reg_id: int) -> Optional[str]:
        async with self._conn.execute(
            "SELECT status FROM files_registry WHERE id=?", (reg_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["status"] if row else None

    async def get_folders_by_status(self, status: str) -> list:
        async with self._conn.execute(
            "SELECT * FROM files_registry WHERE type='folder' AND status=? ORDER BY added_at ASC",
            (status,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def is_registered(self, source: str, source_id: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM files_registry WHERE source=? AND source_id=?",
            (source, source_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_entry_type(self, source: str, source_id: str) -> Optional[str]:
        """Return the 'type' field for a registered entry, or None if not registered."""
        async with self._conn.execute(
            "SELECT type FROM files_registry WHERE source=? AND source_id=?",
            (source, source_id),
        ) as cur:
            row = await cur.fetchone()
            return row["type"] if row else None

    async def update_file_status(
        self, file_id: int, status: str,
        category: str = None, classification: str = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE files_registry SET status=?, category=COALESCE(?, category), "
            "classification=COALESCE(?, classification), processed_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, category, classification, file_id),
        )
        await self._conn.commit()

    async def cleanup_stale_new_files(self, source: str, days: int = 7) -> int:
        """Mark files stuck in 'new' status for >days as 'skipped'. Returns count."""
        import time as _time
        cutoff = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(_time.time() - days * 86400))
        async with self._conn.execute(
            "UPDATE files_registry SET status='skipped' "
            "WHERE source=? AND status='new' AND type='file' AND added_at < ?",
            (source, cutoff),
        ) as cur:
            rows = cur.rowcount
        await self._conn.commit()
        return rows

    async def get_unprocessed_files(self, source: str = None, limit: int = 50) -> list:
        query = "SELECT * FROM files_registry WHERE status='new'"
        params = []
        if source:
            query += " AND source=?"
            params.append(source)
        query += " ORDER BY added_at ASC LIMIT ?"
        params.append(limit)
        async with self._conn.execute(query, params) as cur:
            return await cur.fetchall()

    # ── pending_deletions ─────────────────────────────────────────────────────

    async def add_pending_deletion(
        self, file_id: int, reminder_id: str = None, telegram_msg_id: int = None,
    ) -> int:
        async with self._conn.execute(
            "INSERT INTO pending_deletions (file_id, reminder_id, telegram_msg_id) VALUES (?, ?, ?)",
            (file_id, reminder_id, telegram_msg_id),
        ) as cur:
            row_id = cur.lastrowid
        await self._conn.commit()
        return row_id

    async def create_pending_deletion(self, source_id: str, title: str, source: str) -> int:
        """Create pending deletion, looking up or creating file_id. Used by agent tools."""
        async with self._conn.execute(
            "SELECT id FROM files_registry WHERE source=? AND source_id=?",
            (source, source_id),
        ) as cur:
            row = await cur.fetchone()

        if row:
            file_id = row["id"]
        else:
            async with self._conn.execute(
                "INSERT OR IGNORE INTO files_registry (source, source_id, title, content_hash, status) VALUES (?, ?, ?, 'agent', 'pending')",
                (source, source_id, title),
            ) as cur:
                file_id = cur.lastrowid
            await self._conn.commit()

        # Prevent duplicates — reuse existing waiting request for the same file
        async with self._conn.execute(
            "SELECT id FROM pending_deletions WHERE file_id=? AND status='waiting'",
            (file_id,),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            logger.debug(f"Reusing existing pending deletion id={existing['id']} for file_id={file_id}")
            return existing["id"]

        async with self._conn.execute(
            "INSERT INTO pending_deletions (file_id) VALUES (?)", (file_id,)
        ) as cur:
            deletion_id = cur.lastrowid
        await self._conn.commit()
        return deletion_id

    async def get_new_pending_deletions(self) -> list:
        """Get pending deletions not yet sent to Telegram."""
        async with self._conn.execute(
            """SELECT pd.id, f.source, f.source_id, f.title
               FROM pending_deletions pd
               JOIN files_registry f ON f.id = pd.file_id
               WHERE pd.status='waiting' AND pd.telegram_msg_id IS NULL"""
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def confirm_deletion(self, deletion_id: int) -> Optional[dict]:
        async with self._conn.execute(
            """SELECT pd.*, f.source, f.source_id, f.path, f.title
               FROM pending_deletions pd
               JOIN files_registry f ON f.id = pd.file_id
               WHERE pd.id=? AND pd.status='waiting'""",
            (deletion_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return None

        # Guard against double-confirm race: only update if still 'waiting'
        async with self._conn.execute(
            "UPDATE pending_deletions SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting'",
            (deletion_id,),
        ) as upd:
            if upd.rowcount == 0:
                return None  # another concurrent confirm already changed the status
        await self._conn.commit()
        return dict(row)

    async def get_waiting_deletions(self) -> list:
        async with self._conn.execute(
            """SELECT pd.*, f.source, f.source_id, f.path, f.title
               FROM pending_deletions pd
               JOIN files_registry f ON f.id = pd.file_id
               WHERE pd.status='waiting'"""
        ) as cur:
            return await cur.fetchall()

    async def mark_deletion_executed(self, deletion_id: int) -> None:
        await self._conn.execute(
            "UPDATE pending_deletions SET status='deleted' WHERE id=? AND status='confirmed'", (deletion_id,)
        )
        await self._conn.commit()

    async def get_confirmed_deletions(self) -> list:
        """Return items confirmed for deletion but not yet executed."""
        async with self._conn.execute(
            """SELECT pd.id, pd.file_id, f.source, f.source_id, f.path, f.title
               FROM pending_deletions pd
               JOIN files_registry f ON f.id = pd.file_id
               WHERE pd.status='confirmed'"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def cancel_deletion(self, deletion_id: int) -> None:
        await self._conn.execute(
            "UPDATE pending_deletions SET status='cancelled' WHERE id=?", (deletion_id,)
        )
        await self._conn.commit()

    async def get_stale_pending_deletions(self, hours: int = 20) -> list:
        """Return pending deletions older than N hours that are still waiting."""
        async with self._conn.execute(
            """SELECT pd.id, pd.telegram_msg_id, f.title, f.source
               FROM pending_deletions pd
               JOIN files_registry f ON f.id = pd.file_id
               WHERE pd.status = 'waiting'
                 AND pd.created_at < datetime('now', ? || ' hours')""",
            (f"-{hours}",),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_stale_deletion_msg(self, deletion_id: int, new_msg_id: int) -> None:
        await self._conn.execute(
            "UPDATE pending_deletions SET telegram_msg_id=?, created_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_msg_id, deletion_id),
        )
        await self._conn.commit()

    # ── knowledge_graph ───────────────────────────────────────────────────────

    async def add_knowledge(self, file_id: int, category: str, tags: str, summary: str, embedding: bytes = None) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO knowledge_graph (file_id, category, tags, summary, embedding) VALUES (?, ?, ?, ?, ?)",
            (file_id, category, tags, summary, embedding),
        )
        await self._conn.commit()

    # ── actionable_items ──────────────────────────────────────────────────────

    async def add_actionable(self, file_id: int, reminder_id: str = None, calendar_event_id: str = None) -> None:
        await self._conn.execute(
            "INSERT INTO actionable_items (file_id, reminder_id, calendar_event_id) VALUES (?, ?, ?)",
            (file_id, reminder_id, calendar_event_id),
        )
        await self._conn.commit()

    # ── gdrive_sync_state ─────────────────────────────────────────────────────

    async def get_gdrive_page_token(self) -> Optional[str]:
        async with self._conn.execute("SELECT page_token FROM gdrive_sync_state WHERE id=1") as cur:
            row = await cur.fetchone()
        return row["page_token"] if row else None

    async def set_gdrive_page_token(self, token: str) -> None:
        await self._conn.execute(
            "UPDATE gdrive_sync_state SET page_token=?, last_sync_at=CURRENT_TIMESTAMP WHERE id=1",
            (token,),
        )
        await self._conn.commit()

    # ── goal tracker ──────────────────────────────────────────────────────────

    async def is_goal_tracker_posted(self, task_title: str, list_name: str, task_id: str = "") -> bool:
        """Check if already posted. Prefer task_id lookup (stable), fall back to title+list."""
        if task_id:
            async with self._conn.execute(
                "SELECT 1 FROM goal_tracker_posts WHERE task_id=?", (task_id,)
            ) as cur:
                if await cur.fetchone() is not None:
                    return True
        async with self._conn.execute(
            "SELECT 1 FROM goal_tracker_posts WHERE task_title=? AND list_name=?",
            (task_title, list_name),
        ) as cur:
            return await cur.fetchone() is not None

    async def add_goal_tracker_post(self, task_title: str, list_name: str, task_id: str = "") -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO goal_tracker_posts (task_title, list_name, task_id) VALUES (?, ?, ?)",
            (task_title, list_name, task_id or None),
        )
        await self._conn.commit()

    # ── FTS5 index ────────────────────────────────────────────────────────────

    async def index_content(
        self, source_id: str, source_type: str, title: str,
        content: str = "", category: str = "",
    ) -> None:
        await self._conn.execute(
            "DELETE FROM fts_index WHERE source_id=? AND source_type=?",
            (source_id, source_type),
        )
        await self._conn.execute(
            "INSERT INTO fts_index(source_id, source_type, title, content, category) VALUES(?,?,?,?,?)",
            (source_id, source_type, title, content[:10000], category),
        )
        await self._conn.commit()

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Escape FTS5 special characters so raw user input doesn't cause parse errors."""
        # Wrap the whole query in double-quotes for phrase search if it contains special chars.
        # FTS5 specials: " * ^ : ( ) NOT AND OR
        fts_specials = set('"*^:()\\')
        if any(c in fts_specials for c in query) or query.upper() in ("NOT", "AND", "OR"):
            # Escape internal double-quotes and wrap
            return '"' + query.replace('"', '""') + '"'
        return query

    async def search_fts(self, query: str, limit: int = 5) -> list:
        safe_query = self._sanitize_fts_query(query)
        try:
            async with self._conn.execute(
                "SELECT source_id, source_type, title, category, "
                "snippet(fts_index, 3, '**', '**', '...', 20) as snippet "
                "FROM fts_index WHERE fts_index MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit),
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]
        except Exception as e:
            logger.warning(f"FTS5 search error (query={query!r}): {e}", exc_info=True)
            return []

    # ── user_patterns ─────────────────────────────────────────────────────────

    async def get_pattern(self, key: str) -> Optional[str]:
        async with self._conn.execute(
            "SELECT value FROM user_patterns WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_pattern(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO user_patterns (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
        await self._conn.commit()

    async def get_user_patterns(self) -> dict:
        """Return all user patterns as dict."""
        async with self._conn.execute("SELECT key, value FROM user_patterns") as cur:
            rows = await cur.fetchall()
        return {row["key"]: row["value"] for row in rows}


    async def set_deletion_telegram_msg(self, deletion_id: int, msg_id: int) -> None:
        await self._conn.execute(
            "UPDATE pending_deletions SET telegram_msg_id=? WHERE id=?",
            (msg_id, deletion_id),
        )
        await self._conn.commit()

    # ── Finance ───────────────────────────────────────────────────────────────

    async def fin_get_accounts(self) -> list:
        async with self._conn.execute(
            "SELECT * FROM fin_accounts ORDER BY name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fin_get_account(self, name: str) -> Optional[dict]:
        async with self._conn.execute(
            "SELECT * FROM fin_accounts WHERE name=?", (name,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fin_update_balance(self, account_name: str, new_balance: float, note: str = "") -> None:
        """Update account balance, record implied transaction."""
        acc = await self.fin_get_account(account_name)
        if acc:
            diff = new_balance - acc["balance"]
            if abs(diff) > 0.01:
                tx_type = "income" if diff > 0 else "expense"
                desc = note or ("Корректировка баланса" if not note else note)
                await self.fin_add_transaction(account_name, abs(diff), tx_type, "корректировка", desc)
        await self._conn.execute(
            "UPDATE fin_accounts SET balance=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
            (new_balance, account_name),
        )
        await self._conn.commit()
        # Auto-save snapshot when user explicitly reports balance
        await self.fin_save_snapshot(account_name, new_balance, source="user")

    async def fin_set_balance_direct(self, account_name: str, new_balance: float) -> None:
        """Set account balance directly without creating a correction transaction. Use for statement imports."""
        await self._conn.execute(
            "UPDATE fin_accounts SET balance=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
            (new_balance, account_name),
        )
        await self._conn.commit()
        await self.fin_save_snapshot(account_name, new_balance, source="statement")

    async def fin_save_snapshot(self, account: str, balance: float, snapshot_date: str = "", source: str = "auto") -> None:
        """Save (or update) daily balance snapshot for an account."""
        from datetime import date as _date
        if not snapshot_date:
            snapshot_date = _date.today().isoformat()
        await self._conn.execute(
            """INSERT INTO fin_balance_snapshots (account, balance, snapshot_date, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account, snapshot_date) DO UPDATE SET
                 balance=excluded.balance, source=excluded.source, created_at=CURRENT_TIMESTAMP""",
            (account, balance, snapshot_date, source),
        )
        await self._conn.commit()

    async def fin_save_all_snapshots(self, source: str = "auto") -> None:
        """Save today's snapshot for all accounts (called by fin_remind at 08:00)."""
        accounts = await self.fin_get_accounts()
        for acc in accounts:
            await self.fin_save_snapshot(acc["name"], acc["balance"], source=source)

    async def fin_get_snapshot_on_date(self, account: str, target_date: str) -> dict | None:
        """Get balance snapshot for account on a specific date (or nearest earlier date)."""
        async with self._conn.execute(
            """SELECT account, balance, snapshot_date, source FROM fin_balance_snapshots
               WHERE account=? AND snapshot_date <= ?
               ORDER BY snapshot_date DESC LIMIT 1""",
            (account, target_date),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fin_get_balance_history(self, account: str, days: int = 30) -> list:
        """Get balance snapshots for account for last N days."""
        async with self._conn.execute(
            """SELECT account, balance, snapshot_date, source FROM fin_balance_snapshots
               WHERE account=? AND snapshot_date >= date('now', ?)
               ORDER BY snapshot_date DESC""",
            (account, f"-{days} days"),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fin_add_transaction(
        self, account: Optional[str], amount: float, tx_type: str,
        category: str = "", description: str = "", tx_date: str = "",
    ) -> int:
        """Add transaction and update account balance atomically. Returns transaction id."""
        if not tx_date:
            from datetime import date
            tx_date = date.today().isoformat()
        # Wrap INSERT + UPDATE in an explicit savepoint so a crash between them
        # does not leave the balance inconsistent with the transaction log.
        await self._conn.execute("SAVEPOINT fin_tx")
        try:
            async with self._conn.execute(
                "INSERT INTO fin_transactions (account, amount, tx_type, category, description, tx_date) VALUES (?,?,?,?,?,?)",
                (account, amount, tx_type, category or "", description or "", tx_date),
            ) as cur:
                tx_id = cur.lastrowid

            if account:
                if tx_type in ("income", "debt_taken", "transfer_in"):
                    await self._conn.execute(
                        "UPDATE fin_accounts SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                        (amount, account),
                    )
                elif tx_type in ("expense", "debt_paid", "transfer_out"):
                    await self._conn.execute(
                        "UPDATE fin_accounts SET balance=balance-?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                        (amount, account),
                    )
                # tx_type='transfer' (legacy/unknown direction): no balance change

            await self._conn.execute("RELEASE SAVEPOINT fin_tx")
            await self._conn.commit()
        except Exception:
            await self._conn.execute("ROLLBACK TO SAVEPOINT fin_tx")
            await self._conn.execute("RELEASE SAVEPOINT fin_tx")
            raise
        return tx_id

    async def fin_get_liabilities(self) -> list:
        async with self._conn.execute(
            "SELECT * FROM fin_liabilities WHERE is_active=1 ORDER BY lib_type, name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fin_upsert_liability(
        self, name: str, creditor: str, amount: float,
        lib_type: str = "personal", monthly_payment: Optional[float] = None,
        due_date: Optional[str] = None, notes: str = "",
    ) -> None:
        await self._conn.execute(
            """INSERT INTO fin_liabilities (name, creditor, amount, lib_type, monthly_payment, due_date, notes)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 creditor=excluded.creditor, amount=excluded.amount, lib_type=excluded.lib_type,
                 monthly_payment=excluded.monthly_payment, due_date=excluded.due_date,
                 notes=excluded.notes, updated_at=CURRENT_TIMESTAMP""",
            (name, creditor, amount, lib_type, monthly_payment, due_date, notes or ""),
        )
        await self._conn.commit()

    async def fin_update_liability_amount(self, name: str, new_amount: float) -> tuple[bool, bool]:
        """Returns (changed, closed). closed=True if liability was set to 0 and deactivated."""
        if new_amount <= 0:
            async with self._conn.execute(
                "UPDATE fin_liabilities SET amount=0, is_active=0, updated_at=CURRENT_TIMESTAMP WHERE name=? AND is_active=1",
                (name,),
            ) as cur:
                changed = cur.rowcount > 0
            await self._conn.commit()
            return changed, True
        async with self._conn.execute(
            "UPDATE fin_liabilities SET amount=?, updated_at=CURRENT_TIMESTAMP WHERE name=? AND is_active=1",
            (new_amount, name),
        ) as cur:
            changed = cur.rowcount > 0
        await self._conn.commit()
        return changed, False

    async def fin_get_transactions(self, days: int = 30, account: Optional[str] = None) -> list:
        q = "SELECT * FROM fin_transactions WHERE tx_date >= date('now', ? || ' days')"
        params: list = [f"-{days}"]
        if account:
            q += " AND account=?"
            params.append(account)
        q += " ORDER BY tx_date DESC, created_at DESC LIMIT 100"
        async with self._conn.execute(q, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fin_get_zakat_profile(self) -> Optional[dict]:
        async with self._conn.execute("SELECT * FROM fin_zakat_profile WHERE id=1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fin_update_zakat_profile(
        self, nisab_crossed_at: Optional[str] = None,
        gold_grams_wife: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> None:
        fields, vals = [], []
        if nisab_crossed_at is not None:
            fields.append("nisab_crossed_at=?"); vals.append(nisab_crossed_at)
        if gold_grams_wife is not None:
            fields.append("gold_grams_wife=?"); vals.append(gold_grams_wife)
        if notes is not None:
            fields.append("notes=?"); vals.append(notes)
        if not fields:
            return
        fields.append("updated_at=CURRENT_TIMESTAMP")
        vals.append(1)
        await self._conn.execute(
            f"UPDATE fin_zakat_profile SET {', '.join(fields)} WHERE id=?", vals
        )
        await self._conn.commit()

    async def fin_get_monthly_summary(self, year: int, month: int) -> dict:
        """Return income, expense totals and category breakdown for a given month.
        Excludes transfer transactions (inter-account moves are not income/expense).
        """
        prefix = f"{year:04d}-{month:02d}"
        async with self._conn.execute(
            """SELECT tx_type, category, SUM(amount) as total
               FROM fin_transactions
               WHERE tx_date LIKE ? AND tx_type != 'transfer'
               GROUP BY tx_type, category""",
            (f"{prefix}%",),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        income = sum(r["total"] for r in rows if r["tx_type"] == "income")
        expense = sum(r["total"] for r in rows if r["tx_type"] == "expense")
        return {"year": year, "month": month, "income": income, "expense": expense, "rows": rows}

    async def fin_get_recent_transactions(self, account: str | None = None, limit: int = 20) -> list:
        """Return recent transactions, optionally filtered by account."""
        if account:
            async with self._conn.execute(
                "SELECT * FROM fin_transactions WHERE account=? ORDER BY tx_date DESC, id DESC LIMIT ?",
                (account, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        else:
            async with self._conn.execute(
                "SELECT * FROM fin_transactions ORDER BY tx_date DESC, id DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Recurring reminders ───────────────────────────────────────────────────

    async def fin_get_recurring(self, active_only: bool = True) -> list:
        q = "SELECT * FROM fin_recurring"
        if active_only:
            q += " WHERE is_active=1"
        q += " ORDER BY day_of_month, label"
        async with self._conn.execute(q) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fin_upsert_recurring(
        self, label: str, day_of_month: int, amount: float = None,
        remind_days_before: int = 0,
    ) -> int:
        async with self._conn.execute(
            """INSERT INTO fin_recurring (label, day_of_month, amount, remind_days_before)
               VALUES (?,?,?,?)""",
            (label, day_of_month, amount, remind_days_before),
        ) as cur:
            row_id = cur.lastrowid
        await self._conn.commit()
        return row_id

    async def fin_delete_recurring(self, item_id: int) -> bool:
        await self._conn.execute(
            "UPDATE fin_recurring SET is_active=0 WHERE id=?", (item_id,)
        )
        await self._conn.commit()
        return True

    async def fin_get_due_recurring(
        self, today_day: int, days_in_month: int = 31, current_month: str = None
    ) -> list:
        """Return reminders due today: day_of_month==today OR day_of_month==0 (daily)
        OR advance reminder fires remind_days_before days before day_of_month,
        with wraparound across month boundaries.
        Skips items already confirmed paid this month (paid_month == current_month)."""
        async with self._conn.execute(
            "SELECT * FROM fin_recurring WHERE is_active=1 ORDER BY day_of_month, label"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        result = []
        for r in rows:
            # Skip if already confirmed paid this month
            if current_month and r.get("paid_month") == current_month:
                continue
            dom = r["day_of_month"]
            rdb = r.get("remind_days_before") or 0
            if dom == 0:
                # daily
                result.append(r)
            elif dom == today_day:
                # exact due day
                result.append(r)
            elif rdb > 0:
                # advance reminder with wraparound: trigger_day = dom - rdb, wrapping into prev month
                trigger = dom - rdb
                if trigger <= 0:
                    trigger += days_in_month
                if trigger == today_day:
                    result.append(r)
        return result

    async def fin_find_matching_transactions(
        self, label: str, amount: float | None, month_str: str, strict: bool = True
    ) -> list:
        """Find transactions this month that might correspond to a recurring payment.
        Matches by amount (within 10% or 50₸) OR by keywords from label in description."""
        month_start = f"{month_str}-01"
        # Next month boundary
        y, m = int(month_str[:4]), int(month_str[5:7])
        m += 1
        if m > 12:
            m, y = 1, y + 1
        month_end = f"{y:04d}-{m:02d}-01"

        async with self._conn.execute(
            """SELECT * FROM fin_transactions
               WHERE tx_date >= ? AND tx_date < ?
               ORDER BY tx_date DESC""",
            (month_start, month_end),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        # Build keyword list from label (words with len > 3, skip generic finance words)
        # "kaspi" excluded — too generic, matches unrelated transactions like TOO KASPI MAGAZIN
        _GENERIC_WORDS = {
            "банк", "bank", "депозит", "deposit", "платёж", "payment", "оплата",
            "kaspi", "каспи", "кредит", "credit",
        }
        keywords = [
            w.lower() for w in label.split()
            if len(w) > 3 and w.lower() not in _GENERIC_WORDS
        ]

        matches = []
        for tx in rows:
            # Only look at expenses — ignore transfers between own accounts
            if tx.get("tx_type") in ("transfer", "transfer_in", "transfer_out"):
                continue

            desc = (tx.get("description") or "").lower()
            tx_amount = abs(tx.get("amount") or 0)

            amount_match = False
            if amount and amount > 0:
                # Tight tolerance: 2% or 100₸ max
                tolerance = min(max(50, amount * 0.02), 100)
                amount_match = abs(tx_amount - amount) <= tolerance

            keyword_match = any(kw in desc for kw in keywords) if keywords else False

            if strict:
                # Strict (for upcoming reminders): require BOTH amount AND keyword
                if keywords:
                    is_match = amount_match and keyword_match
                else:
                    is_match = bool(amount and abs(tx_amount - amount) <= 50)
            else:
                # Lenient (for overdue check): keyword match alone is enough;
                # subscriptions often vary slightly in amount due to exchange rates
                if keywords:
                    is_match = keyword_match
                else:
                    is_match = bool(amount and abs(tx_amount - amount) <= 50)

            if is_match:
                matches.append(tx)

        return matches

    async def fin_get_recurring_by_id(self, recurring_id: int) -> dict | None:
        """Return a single recurring payment row by id."""
        async with self._conn.execute(
            "SELECT * FROM fin_recurring WHERE id=?", (recurring_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def fin_mark_recurring_paid(self, recurring_id: int, month_str: str) -> None:
        """Mark a recurring payment as confirmed paid for the given month."""
        await self._conn.execute(
            "UPDATE fin_recurring SET paid_month=? WHERE id=?",
            (month_str, recurring_id),
        )
        await self._conn.commit()

    async def fin_transaction_exists(self, account: str, tx_date: str, amount: float, description: str) -> bool:
        """Return True if a transaction with same account/date/amount/description already exists.

        Also catches duplicates where description differs slightly: if a record with same
        account/date/amount/tx_type exists and descriptions overlap (one contains the other),
        treat as duplicate.
        """
        # Exact match first
        async with self._conn.execute(
            """SELECT 1 FROM fin_transactions
               WHERE account=? AND tx_date=? AND ABS(amount - ?) < 0.01 AND description=?
               LIMIT 1""",
            (account, tx_date, amount, description),
        ) as cur:
            if await cur.fetchone():
                return True
        # Fuzzy match: same account/date/amount, description is substring of existing or vice versa
        desc_lower = (description or "").lower().strip()
        if desc_lower:
            async with self._conn.execute(
                """SELECT description FROM fin_transactions
                   WHERE account=? AND tx_date=? AND ABS(amount - ?) < 0.01
                   LIMIT 10""",
                (account, tx_date, amount),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                existing = (row[0] or "").lower().strip()
                if existing and (desc_lower in existing or existing in desc_lower):
                    return True
        # Same-amount match: if same account/date/amount already exists (manual entry vs statement),
        # treat as duplicate regardless of description to prevent double-counting.
        # Excludes transfers (they can legitimately have two legs with the same amount).
        async with self._conn.execute(
            """SELECT 1 FROM fin_transactions
               WHERE account=? AND tx_date=? AND ABS(amount - ?) < 0.01
               AND tx_type NOT IN ('transfer','transfer_in','transfer_out')
               LIMIT 1""",
            (account, tx_date, amount),
        ) as cur:
            if await cur.fetchone():
                return True
        return False

    async def fin_get_today_transactions(self, today_str: str) -> list:
        """Return all transactions for a specific date."""
        async with self._conn.execute(
            "SELECT * FROM fin_transactions WHERE tx_date=? ORDER BY id",
            (today_str,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fin_get_upcoming_recurring(
        self, today_day: int, days_in_month: int, current_month: str = None
    ) -> list:
        """Return active recurring payments due after today and before end of month.
        Skips items already confirmed paid this month if current_month provided."""
        async with self._conn.execute(
            """SELECT * FROM fin_recurring
               WHERE is_active=1 AND day_of_month > ? AND day_of_month <= ?
               ORDER BY day_of_month""",
            (today_day, days_in_month),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if current_month:
            rows = [r for r in rows if r.get("paid_month") != current_month]
        return rows

    async def fin_get_total_balance(self) -> float:
        """Return sum of all account balances."""
        async with self._conn.execute(
            "SELECT COALESCE(SUM(balance), 0) as total FROM fin_accounts"
        ) as cur:
            row = await cur.fetchone()
            return float(row["total"]) if row else 0.0

    async def cleanup_old_snapshots(self, keep_days: int = 730) -> int:
        """Delete balance snapshots older than keep_days. Returns deleted count."""
        async with self._conn.execute(
            "DELETE FROM fin_balance_snapshots WHERE snapshot_date < date('now', ?)",
            (f"-{keep_days} days",),
        ) as cur:
            deleted = cur.rowcount
        await self._conn.commit()
        return deleted

    async def fin_count_months_with_data(self) -> int:
        """Return number of distinct months that have transaction data."""
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT strftime('%Y-%m', tx_date)) as cnt FROM fin_transactions"
        ) as cur:
            row = await cur.fetchone()
            return int(row["cnt"]) if row else 0

    async def fin_get_avg_variable_spend(self, excluded_categories: set) -> float:
        """Return average monthly variable spending, excluding given categories.
        Uses last 2 full months of data."""
        placeholders = ",".join("?" * len(excluded_categories))
        params = list(excluded_categories)
        async with self._conn.execute(
            f"""SELECT strftime('%Y-%m', tx_date) as month, SUM(amount) as total
                FROM fin_transactions
                WHERE tx_type='expense' AND category NOT IN ({placeholders})
                GROUP BY month
                ORDER BY month DESC
                LIMIT 2""",
            params,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return 0.0
        return sum(r["total"] for r in rows) / len(rows)

    async def fin_get_month_variable_spend(self, month_str: str, excluded_categories: set) -> float:
        """Return total variable spending for a given YYYY-MM month, excluding categories."""
        placeholders = ",".join("?" * len(excluded_categories))
        params = [f"{month_str}%"] + list(excluded_categories)
        async with self._conn.execute(
            f"""SELECT COALESCE(SUM(amount), 0) as total
                FROM fin_transactions
                WHERE tx_date LIKE ? AND tx_type='expense'
                  AND category NOT IN ({placeholders})""",
            params,
        ) as cur:
            row = await cur.fetchone()
            return float(row["total"]) if row else 0.0

    # ── statistics ────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        stats = {}
        async with self._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM files_registry GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
        stats["files"] = {row["status"]: row["cnt"] for row in rows}

        async with self._conn.execute(
            "SELECT COUNT(*) as cnt FROM pending_deletions WHERE status='waiting'"
        ) as cur:
            row = await cur.fetchone()
        stats["pending_deletions"] = row["cnt"] if row else 0

        async with self._conn.execute(
            "SELECT COUNT(*) as cnt FROM processing_queue WHERE status='pending'"
        ) as cur:
            row = await cur.fetchone()
        stats["queue_pending"] = row["cnt"] if row else 0

        return stats
