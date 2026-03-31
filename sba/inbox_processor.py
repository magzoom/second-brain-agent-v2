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
from sba.integrations import apple_notes
from sba.integrations.google_drive import (
    build_service, get_changes, get_start_page_token,
    list_folder_contents, get_file_content, metadata_hash,
)

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

    max_per_run = config.get("inbox", {}).get("max_items_per_run", 20)
    cost_log: list = []
    stats = {"processed": 0, "actions": 0, "info": 0, "errors": 0, "max": max_per_run, "cost_log": cost_log}

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

    total_cost = sum(cost_log)
    if cost_log:
        logger.info(
            f"Inbox run cost: ${total_cost:.4f} total | "
            f"{len(cost_log)} agent calls | avg ${total_cost/len(cost_log):.4f}/call"
        )

    await notifier.send_inbox_report(
        processed=stats["processed"],
        errors=stats["errors"],
    )


async def _process_gdrive(db: Database, notifier: Notifier, config: dict, stats: dict) -> None:
    """Process new Google Drive files via incremental changes API."""
    try:
        service = await asyncio.to_thread(build_service, config)
    except Exception as e:
        logger.error(f"Google Drive auth failed: {e}")
        await notifier.send_message(f"⚠️ <b>Google Drive авторизация провалилась</b>\nЗапусти <code>sba auth google</code>\nПосле авторизации inbox подхватит токен автоматически при следующем запуске по расписанию.\n\n{e}")
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
        error_str = str(e)
        if "410" in error_str or "Gone" in error_str:
            logger.warning("Google Drive page_token expired (HTTP 410), resetting for next run")
            await db.set_gdrive_page_token(None)
        else:
            logger.error(f"Failed to get Drive changes: {e}")
            stats["errors"] += 1
        return

    inbox_folder_id = config.get("google_drive", {}).get("inbox_folder_id", "")
    limit_hit = False

    for file_info in changes:
        mime = file_info.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            continue

        # Only process files that are in the Inbox folder (same as v1)
        # User's normal Drive activity (moves, edits, etc.) should not trigger agent
        parents = file_info.get("parents", [])
        if not inbox_folder_id or inbox_folder_id not in parents:
            continue

        file_id = file_info.get("id", "")
        title = file_info.get("name", "Untitled")
        c_hash = metadata_hash(file_info)

        # Skip internal SBA files (summaries created by the agent itself)
        if title.startswith("_sba"):
            continue

        reg_id, is_new = await db.upsert_file(
            source="gdrive", source_id=file_id,
            content_hash=c_hash, title=title,
            path=file_info.get("webViewLink", ""),
        )
        if not is_new:
            continue

        if stats["processed"] >= stats.get("max", 20):
            limit_hit = True
            logger.info(f"Inbox: reached max_items_per_run limit ({stats['max']}), not advancing page_token")
            break

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
            from_inbox=True, reg_id=reg_id,
        )

    # Only advance token if we processed the whole batch.
    # If limit was hit, keep old token so remaining files are fetched next run.
    if not limit_hit:
        await db.set_gdrive_page_token(new_token)


async def _process_gdrive_inbox_folder(db: Database, notifier: Notifier, config: dict, stats: dict) -> None:
    """Directly scan Drive Inbox folder — catches files missed by changes API."""
    inbox_folder_id = config.get("google_drive", {}).get("inbox_folder_id", "")
    if not inbox_folder_id:
        logger.warning("Google Drive: inbox_folder_id not set in config, skipping folder scan")
        return

    try:
        service = await asyncio.to_thread(build_service, config)
    except Exception as e:
        logger.error(f"Google Drive auth failed (inbox folder scan): {e}")
        await notifier.send_message(f"⚠️ <b>Google Drive авторизация провалилась</b> (inbox scan)\nЗапусти <code>sba auth google</code>\n\n{e}")
        stats["errors"] += 1
        return

    try:
        files = await asyncio.to_thread(list_folder_contents, service, inbox_folder_id, False)
    except Exception as e:
        logger.error(f"Failed to list Drive Inbox folder: {e}")
        stats["errors"] += 1
        return

    for file_info in files:
        if stats["processed"] >= stats.get("max", 20):
            logger.info(f"Inbox: reached max_items_per_run limit ({stats['max']}), stopping inbox folder scan")
            break

        mime = file_info.get("mimeType", "")
        if mime == "application/vnd.google-apps.folder":
            continue

        file_id = file_info.get("id", "")
        title = file_info.get("name", "Untitled")
        c_hash = metadata_hash(file_info)

        # Skip internal SBA files (summaries created by the agent itself)
        if title.startswith("_sba"):
            continue

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
            reg_id=reg_id,
        )


async def _process_apple_notes(db: Database, notifier: Notifier, config: dict, stats: dict) -> None:
    """Process new notes in Apple Notes Inbox folder."""
    try:
        notes = await asyncio.to_thread(apple_notes.get_notes_in_folder, "Inbox")
    except Exception as e:
        logger.error(f"Failed to read Apple Notes Inbox: {e}")
        stats["errors"] += 1
        return

    for note in notes:
        if stats["processed"] >= stats.get("max", 20):
            logger.info(f"Inbox: reached max_items_per_run limit ({stats['max']}), stopping apple notes")
            break

        note_id = str(note.get("id", ""))
        title = note.get("title", "Untitled")
        content = note.get("content_text", "")
        c_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

        reg_id, is_new = await db.upsert_file(
            source="apple_notes", source_id=note_id,
            content_hash=c_hash, title=title,
        )
        if not is_new:
            # Still in Inbox — retry only if status is not terminal
            status = await db.get_file_status(reg_id)
            if status in ("processed", "error"):
                continue
            logger.info(f"Apple Notes: retrying stuck note '{title}' (status={status})")

        await _run_agent_on_item(
            db=db, notifier=notifier, config=config,
            source="apple_notes", source_id=note_id,
            title=title, content=content, stats=stats,
            reg_id=reg_id,
        )


async def _run_agent_on_item(
    db: Database, notifier: Notifier, config: dict,
    source: str, source_id: str, title: str, content: str, stats: dict,
    from_inbox: bool = True, reg_id: int = None,
) -> None:
    """Send a single item to Main Agent for processing."""
    from sba import agent as main_agent

    # Hard cost limit check
    cost_log: list = stats.get("cost_log", [])
    current_cost = sum(cost_log)
    limit = config.get("inbox", {}).get("max_session_cost_usd", 0.0)
    if limit and current_cost >= limit:
        if not stats.get("cost_limit_notified"):
            stats["cost_limit_notified"] = True
            logger.warning(f"Inbox: cost limit ${limit:.2f} reached (spent ${current_cost:.4f}), stopping")
            await notifier.send_message(
                f"⛔ <b>Лимит расходов (${limit:.2f}) исчерпан</b>\n\n"
                f"Потрачено: ${current_cost:.4f} за {len(cost_log)} вызовов.\n"
                f"Дальнейшие вызовы заблокированы до следующего запуска inbox.\n\n"
                f"Чтобы изменить лимит: <code>inbox.max_session_cost_usd</code> в config.yaml"
            )
        return

    if from_inbox:
        message = (
            f"Обработай входящий элемент.\n"
            f"Источник: {source}\nID: {source_id}\n"
            f"Название: {title}\nСодержимое: {content[:2000]}"
        )
    else:
        message = (
            f"Проиндексируй файл из Google Drive (он уже находится в организованной папке, НЕ перемещай его).\n"
            f"Источник: {source}\nID: {source_id}\n"
            f"Название: {title}\nСодержимое: {content[:2000]}"
        )

    try:
        await main_agent.run_main_agent(
            message, db=db, notifier=notifier, config=config,
            _cost_accumulator=cost_log,
        )
        stats["processed"] += 1
        if reg_id:
            await db.update_file_status(reg_id, "processed")
    except Exception as e:
        logger.error(f"Agent failed for '{title}': {e}")
        stats["errors"] += 1
        if reg_id:
            await db.update_file_status(reg_id, "error")  # prevent infinite retry
