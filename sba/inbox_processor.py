"""
Inbox Processor — runs every 2 hours via launchd.

Sources:
  - Google Drive (changes API with pageToken — incremental sync)
  - Apple Notes folder "Inbox"

Each item is classified by Haiku and sent as a Telegram suggestion card.
User confirms/picks category via inline buttons; agent is NOT called automatically.
Uses fcntl-based lock (OS auto-releases on crash).
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from sba.api_client import get_anthropic_client

from sba.db import Database, get_db_path
from sba.lock import acquire_lock, release_lock, wait_if_dev_active
from sba.notifier import Notifier
from sba.integrations import apple_notes
from sba.integrations.google_drive import (
    build_service, get_changes, get_start_page_token,
    list_folder_contents, get_file_content, metadata_hash,
)

CATEGORIES = [
    "1_Health_Energy", "2_Business_Career", "3_Finance",
    "4_Family_Relationships", "5_Personal Growth", "6_Brightness life", "7_Spirituality",
]

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "inbox_v2.lock"


async def run(config: dict) -> None:
    """Main entry point for inbox processing."""
    # Check pause flag
    if (Path.home() / ".sba" / "PAUSED").exists():
        logger.info("Inbox: paused, skipping")
        return

    if not wait_if_dev_active():
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
        from sba.notifier import notify_auth_error
        await notify_auth_error(notifier, "Google Drive (inbox)", e)
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

    if len(changes) > 200:
        logger.warning(f"Google Drive: large change batch ({len(changes)} files) — likely pageToken reset. inbox_folder_id filter is active.")

    inbox_folder_id = config.get("google_drive", {}).get("inbox_folder_id", "")
    limit_hit = False

    for file_info in changes:
        mime = file_info.get("mimeType", "")
        is_folder = (mime == "application/vnd.google-apps.folder")

        # Only process files/folders that are in the Inbox folder
        parents = file_info.get("parents", [])
        if not inbox_folder_id or inbox_folder_id not in parents:
            continue

        file_id = file_info.get("id", "")
        title = file_info.get("name", "Untitled")
        c_hash = metadata_hash(file_info)

        # Skip internal SBA files
        if title.startswith("_sba"):
            continue

        if is_folder:
            reg_id, is_new = await db.upsert_folder(
                source="gdrive", source_id=file_id,
                title=title, path=file_info.get("webViewLink", ""),
            )
        else:
            reg_id, is_new = await db.upsert_file(
                source="gdrive", source_id=file_id,
                content_hash=c_hash, title=title,
                path=file_info.get("webViewLink", ""),
            )

        if not is_new:
            status = await db.get_file_status(reg_id)
            if status != "new":
                continue

        if stats["processed"] >= stats.get("max", 20):
            limit_hit = True
            logger.info(f"Inbox: reached max_items_per_run limit ({stats['max']}), not advancing page_token")
            break

        content_text = ""
        if not is_folder:
            try:
                content_bytes = await asyncio.to_thread(get_file_content, service, file_id, mime)
                if content_bytes:
                    content_text = content_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                logger.debug(f"Could not download content for '{title}' ({file_id}): {e}")

        await _classify_and_suggest(
            db=db, notifier=notifier, config=config,
            source="gdrive", source_id=file_id,
            title=title, content=content_text,
            is_folder=is_folder, reg_id=reg_id, stats=stats,
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
        from sba.notifier import notify_auth_error
        await notify_auth_error(notifier, "Google Drive (inbox scan)", e)
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
        is_folder = (mime == "application/vnd.google-apps.folder")
        file_id = file_info.get("id", "")
        title = file_info.get("name", "Untitled")
        c_hash = metadata_hash(file_info)

        # Skip internal SBA files
        if title.startswith("_sba"):
            continue

        if is_folder:
            reg_id, is_new = await db.upsert_folder(
                source="gdrive", source_id=file_id,
                title=title, path=file_info.get("webViewLink", ""),
            )
        else:
            reg_id, is_new = await db.upsert_file(
                source="gdrive", source_id=file_id,
                content_hash=c_hash, title=title,
                path=file_info.get("webViewLink", ""),
            )

        if not is_new:
            status = await db.get_file_status(reg_id)
            if status != "new":
                continue

        content_text = ""
        if not is_folder:
            try:
                content_bytes = await asyncio.to_thread(get_file_content, service, file_id, mime)
                if content_bytes:
                    content_text = content_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                logger.debug(f"Could not download content for '{title}' ({file_id}): {e}")

        await _classify_and_suggest(
            db=db, notifier=notifier, config=config,
            source="gdrive", source_id=file_id,
            title=title, content=content_text,
            is_folder=is_folder, reg_id=reg_id, stats=stats,
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
            status = await db.get_file_status(reg_id)
            if status in ("processed", "error", "pending_decision"):
                continue

        await _classify_and_suggest(
            db=db, notifier=notifier, config=config,
            source="apple_notes", source_id=note_id,
            title=title, content=content,
            is_folder=False, reg_id=reg_id, stats=stats,
        )


async def _classify_item_haiku(title: str, content: str, is_folder: bool, config: dict) -> tuple[str, str]:
    """
    Call Claude Haiku to suggest a category and classification.
    Returns (category, classification). Falls back to safe defaults on error.
    """
    prompt = (
        f"Определи категорию для {'папки' if is_folder else 'файла'}.\n"
        f"Название: {title}\n"
        + (f"Содержимое: {content[:400]}\n" if content else "")
        + f"\nКатегории: {', '.join(CATEGORIES)}\n"
        f"Тип: info | action | review | trash\n\n"
        f"Ответь ТОЛЬКО JSON без markdown:\n"
        f'{{\"category\": \"2_Business_Career\", \"classification\": \"info\"}}'
    )
    try:
        client = get_anthropic_client(config, timeout=20.0)
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if any
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        category = data.get("category", "2_Business_Career")
        classification = data.get("classification", "info")
        if category not in CATEGORIES:
            category = "2_Business_Career"
        if classification not in ("info", "action", "review", "trash"):
            classification = "info"
        return category, classification
    except Exception as e:
        logger.warning(f"Haiku classification failed for '{title}': {e}")
        return "2_Business_Career", "info"


async def _classify_and_suggest(
    db: Database, notifier: Notifier, config: dict,
    source: str, source_id: str, title: str, content: str,
    is_folder: bool, reg_id: int, stats: dict,
) -> None:
    """Classify item with Haiku, save suggestion, send Telegram card."""
    try:
        category, classification = await _classify_item_haiku(title, content, is_folder, config)

        # Save suggestion to DB (status = pending_decision until user responds)
        await db.update_file_status(
            reg_id, "pending_decision",
            category=category,
            classification=classification,
        )

        # Send Telegram suggestion card
        await notifier.send_inbox_suggestion(
            reg_id=reg_id,
            title=title,
            source=source,
            suggested_category=category,
            is_folder=is_folder,
            classification=classification,
        )

        stats["processed"] += 1
        logger.info(f"Inbox suggestion sent: '{title}' → {category} ({classification})")

    except Exception as e:
        logger.error(f"_classify_and_suggest failed for '{title}': {e}", exc_info=True)
        stats["errors"] += 1
        await db.update_file_status(reg_id, "error")
