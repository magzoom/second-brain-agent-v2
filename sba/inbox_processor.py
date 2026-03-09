"""
Inbox Processor — runs every 2 hours via launchd.

Sources:
  - Google Drive (changes API with pageToken — incremental sync)
  - Apple Notes folder "Inbox"

Each item is sent to Main Agent for processing.
Uses fcntl-based lock (OS auto-releases on crash).
"""

import asyncio
import hashlib
import logging
from pathlib import Path

from sba.db import Database, get_db_path
from sba.lock import acquire_lock, release_lock
from sba.notifier import Notifier

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "inbox_v2.lock"


async def run(config: dict) -> None:
    """Main entry point for inbox processing."""
    # Check pause flag
    if (Path.home() / ".sba" / "PAUSED").exists():
        logger.info("Inbox: paused, skipping")
        return

    notifier = Notifier(config)
    db_path = get_db_path(config)
    lock_fd = acquire_lock(LOCK_FILE)  # exits silently if already running

    stats = {"processed": 0, "actions": 0, "info": 0, "errors": 0}

    try:
        async with Database(db_path) as db:
            await _process_gdrive(db, notifier, config, stats)
            await _process_gdrive_inbox_folder(db, notifier, config, stats)
            await _process_apple_notes(db, notifier, config, stats)

    except Exception as e:
        logger.error(f"Fatal error in inbox: {e}", exc_info=True)
        await notifier.send_message(f"⚠️ SBA inbox упал: {type(e).__name__}: {e}")
        raise
    finally:
        release_lock(lock_fd)

    # Read actual results from DB (agent updates files_registry directly)
    async with Database(db_path) as db:
        actual = await db.get_inbox_run_stats()

    await notifier.send_inbox_report(
        processed=actual.get("processed", stats["processed"]),
        actions_created=actual.get("actions", 0),
        info_moved=actual.get("info", 0),
        errors=stats["errors"],
    )


async def _process_gdrive(db: Database, notifier: Notifier, config: dict, stats: dict) -> None:
    """Process new Google Drive files via incremental changes API."""
    try:
        from sba.integrations.google_drive import (
            build_service, get_changes, get_start_page_token,
            get_file_content, metadata_hash,
        )
    except ImportError:
        logger.warning("Google Drive integration unavailable")
        return

    try:
        service = await asyncio.to_thread(build_service, config)
    except Exception as e:
        logger.error(f"Google Drive auth failed: {e}")
        stats["errors"] += 1
        return

    page_token = await db.get_gdrive_page_token()
    if not page_token:
        page_token = await asyncio.to_thread(get_start_page_token, service)
        await db.set_gdrive_page_token(page_token)
        logger.info("Google Drive: initialized page token")
        return

    try:
        changes, new_token = await asyncio.to_thread(get_changes, service, page_token)
    except Exception as e:
        logger.error(f"Failed to get Drive changes: {e}")
        stats["errors"] += 1
        return

    for file_info in changes:
        mime = file_info.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            continue

        file_id = file_info.get("id", "")
        title = file_info.get("name", "Untitled")
        c_hash = metadata_hash(file_info)

        reg_id, is_new = await db.upsert_file(
            source="gdrive", source_id=file_id,
            content_hash=c_hash, title=title,
            path=file_info.get("webViewLink", ""),
        )
        if not is_new:
            continue

        content_text = ""
        try:
            content_bytes = await asyncio.to_thread(get_file_content, service, file_id, mime)
            if content_bytes:
                content_text = content_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.debug(f"Could not download content for '{title}' ({file_id}): {e}")

        await _run_agent_on_item(
            db=db, notifier=notifier, config=config,
            source="gdrive", source_id=file_id,
            title=title, content=content_text, stats=stats,
        )

    # Save token AFTER processing — if we crash mid-loop, we'll re-process on next run (safe)
    await db.set_gdrive_page_token(new_token)


async def _process_gdrive_inbox_folder(db: Database, notifier: Notifier, config: dict, stats: dict) -> None:
    """Directly scan Drive Inbox folder — catches files missed by changes API."""
    inbox_folder_id = config.get("google_drive", {}).get("inbox_folder_id", "")
    if not inbox_folder_id:
        logger.warning("Google Drive: inbox_folder_id not set in config, skipping folder scan")
        return

    try:
        from sba.integrations.google_drive import (
            build_service, list_folder_contents, get_file_content, metadata_hash,
        )
    except ImportError:
        return

    try:
        service = await asyncio.to_thread(build_service, config)
    except Exception as e:
        logger.error(f"Google Drive auth failed (inbox folder scan): {e}")
        stats["errors"] += 1
        return

    try:
        files = await asyncio.to_thread(list_folder_contents, service, inbox_folder_id, False)
    except Exception as e:
        logger.error(f"Failed to list Drive Inbox folder: {e}")
        stats["errors"] += 1
        return

    for file_info in files:
        mime = file_info.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            continue

        file_id = file_info.get("id", "")
        title = file_info.get("name", "Untitled")
        c_hash = metadata_hash(file_info)

        _, is_new = await db.upsert_file(
            source="gdrive", source_id=file_id,
            content_hash=c_hash, title=title,
            path=file_info.get("webViewLink", ""),
        )
        if not is_new:
            continue

        content_text = ""
        try:
            content_bytes = await asyncio.to_thread(get_file_content, service, file_id, mime)
            if content_bytes:
                content_text = content_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.debug(f"Could not download content for '{title}' ({file_id}): {e}")

        await _run_agent_on_item(
            db=db, notifier=notifier, config=config,
            source="gdrive", source_id=file_id,
            title=title, content=content_text, stats=stats,
        )


async def _process_apple_notes(db: Database, notifier: Notifier, config: dict, stats: dict) -> None:
    """Process new notes in Apple Notes Inbox folder."""
    from sba.integrations import apple_notes

    try:
        notes = await asyncio.to_thread(apple_notes.get_notes_in_folder, "Inbox")
    except Exception as e:
        logger.error(f"Failed to read Apple Notes Inbox: {e}")
        stats["errors"] += 1
        return

    for note in notes:
        note_id = str(note.get("id", ""))
        title = note.get("title", "Untitled")
        content = note.get("content_text", "")
        c_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

        reg_id, is_new = await db.upsert_file(
            source="apple_notes", source_id=note_id,
            content_hash=c_hash, title=title,
        )
        if not is_new:
            continue

        await _run_agent_on_item(
            db=db, notifier=notifier, config=config,
            source="apple_notes", source_id=note_id,
            title=title, content=content, stats=stats,
        )


async def _run_agent_on_item(
    db: Database, notifier: Notifier, config: dict,
    source: str, source_id: str, title: str, content: str, stats: dict,
) -> None:
    """Send a single item to Main Agent for processing."""
    from sba import agent as main_agent

    message = (
        f"Обработай входящий элемент.\n"
        f"Источник: {source}\nID: {source_id}\n"
        f"Название: {title}\nСодержимое: {content[:2000]}"
    )

    try:
        await main_agent.run_main_agent(message, db=db, notifier=notifier, config=config)
        stats["processed"] += 1
    except Exception as e:
        logger.error(f"Agent failed for '{title}': {e}")
        stats["errors"] += 1
