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
        conn.execute("PRAGMA busy_timeout=5000")
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
    """)


async def get_connection(db_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
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
            "SELECT id, content_hash FROM files_registry WHERE source=? AND source_id=?",
            (source, source_id),
        ) as cur:
            row = await cur.fetchone()

        if row["content_hash"] != content_hash:
            await self._conn.execute(
                "UPDATE files_registry SET content_hash=?, title=?, path=?, type=?, status='new', processed_at=NULL WHERE id=?",
                (content_hash, title, path, entry_type, row["id"]),
            )
            await self._conn.commit()
            return row["id"], True

        return row["id"], False

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

    async def get_unprocessed_files(self, source: str = None, limit: int = 50) -> list:
        query = "SELECT * FROM files_registry WHERE status='new'"
        params = []
        if source:
            query += " AND source=?"
            params.append(source)
        query += f" ORDER BY added_at ASC LIMIT {limit}"
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

        await self._conn.execute(
            "UPDATE pending_deletions SET status='confirmed', confirmed_at=CURRENT_TIMESTAMP WHERE id=?",
            (deletion_id,),
        )
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
            "UPDATE pending_deletions SET status='deleted' WHERE id=?", (deletion_id,)
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

    async def search_fts(self, query: str, limit: int = 5) -> list:
        try:
            async with self._conn.execute(
                "SELECT source_id, source_type, title, category, "
                "snippet(fts_index, 3, '**', '**', '...', 20) as snippet "
                "FROM fts_index WHERE fts_index MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
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

    async def get_inbox_run_stats(self) -> dict:
        """Count files processed in the last hour by classification."""
        async with self._conn.execute(
            """SELECT classification, COUNT(*) as cnt
               FROM files_registry
               WHERE status='processed'
               AND processed_at >= datetime('now', '-1 hour')
               GROUP BY classification"""
        ) as cur:
            rows = await cur.fetchall()
        result = {"processed": 0, "actions": 0, "info": 0}
        for row in rows:
            c = row["classification"] or ""
            cnt = row["cnt"]
            result["processed"] += cnt
            if c in ("action", "review"):
                result["actions"] += cnt
            elif c == "info":
                result["info"] += cnt
        return result

    async def set_deletion_telegram_msg(self, deletion_id: int, msg_id: int) -> None:
        await self._conn.execute(
            "UPDATE pending_deletions SET telegram_msg_id=? WHERE id=?",
            (msg_id, deletion_id),
        )
        await self._conn.commit()

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
