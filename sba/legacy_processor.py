"""
Legacy Processor — runs daily at 09:00 via launchd.

Steps per run:
1. Execute confirmed deletions (status='confirmed' in pending_deletions)
2. Goal Tracker: post completed tasks to Telegram channel
3. Scan Google Drive folders — send Telegram decisions for new/pending_deep folders
4. Process Apple Notes legacy (limit_notes per run)

Uses fcntl-based lock (OS auto-releases on crash).
"""

import asyncio
import hashlib
import logging
from pathlib import Path

from sba.db import Database, get_db_path
from sba.lock import acquire_lock, release_lock
from sba.notifier import Notifier
from sba.integrations import apple_notes, google_tasks
from sba.integrations.google_drive import (
    build_service, trash_file, list_folder_contents,
)

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "legacy_v2.lock"


async def run(config: dict) -> None:
    """Main entry point for legacy processing."""
    notifier = Notifier(config)
    db_path = get_db_path(config)
    lock_fd = acquire_lock(LOCK_FILE)

    # Backup DB before processing
    _backup_db(db_path)

    schedule = config.get("schedule", {})
    limit_notes = int(schedule.get("legacy_limit_notes", 3))

    cost_log: list = []
    stats = {"processed": 0, "actions": 0, "deletions": 0, "errors": 0, "folders_decided": 0, "cost_log": cost_log}

    try:
        async with Database(db_path) as db:
            await _execute_confirmed_deletions(db, config)
            await _resend_stale_pending_deletions(db, notifier)
            await _rollover_overdue_tasks(config)
            await _goal_tracker(db, notifier, config)
            await _process_gdrive_legacy(db, notifier, config, stats)
            await _process_apple_notes_legacy(db, notifier, config, stats, limit_notes)

    except Exception as e:
        logger.error(f"Fatal error in legacy: {e}", exc_info=True)
        await notifier.send_message(f"⚠️ SBA legacy упал: {type(e).__name__}: {e}")
        raise
    finally:
        release_lock(lock_fd)

    total_cost = sum(cost_log)
    if cost_log:
        logger.info(
            f"Legacy run cost: ${total_cost:.4f} total | "
            f"{len(cost_log)} agent calls | avg ${total_cost/len(cost_log):.4f}/call"
        )

    await notifier.send_legacy_report(
        processed=stats["processed"],
        actions_created=stats["actions"],
        pending_deletions=stats["deletions"],
        errors=stats["errors"],
        folders_decided=stats["folders_decided"],
    )


def _backup_db(db_path: Path) -> None:
    """Create a backup before processing. Keep last 7."""
    import shutil
    from datetime import datetime
    backup_dir = Path.home() / ".sba" / "backups"
    backup_dir.mkdir(exist_ok=True)
    dst = backup_dir / f"sba_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    try:
        shutil.copy2(db_path, dst)
        backups = sorted(backup_dir.glob("sba_*.db"))
        for old in backups[:-7]:
            old.unlink()
    except Exception as e:
        logger.warning(f"Backup failed: {e}")


async def _rollover_overdue_tasks(config: dict) -> None:
    """Move overdue incomplete Google Tasks to today."""
    try:
        service = await asyncio.to_thread(google_tasks.build_service, config)
        count = await asyncio.to_thread(google_tasks.rollover_overdue_tasks, service)
        if count:
            logger.info(f"Rolled over {count} overdue tasks to today")
    except Exception as e:
        logger.warning(f"Task rollover failed: {e}")


async def _resend_stale_pending_deletions(db: Database, notifier: Notifier) -> None:
    """Re-send pending deletion requests older than 20 hours that are still waiting."""
    stale = await db.get_stale_pending_deletions(hours=20)

    for item in stale:
        del_id = item["id"]
        old_msg_id = item["telegram_msg_id"]
        title = item["title"]
        source = item["source"]

        # Mark old message as outdated
        if old_msg_id and notifier.enabled:
            await notifier.edit_message(
                old_msg_id,
                f"⚠️ <b>Запрос устарел, повторно отправлен сегодня</b>\n📄 {title}",
            )

        # Send fresh request
        new_msg_id = await notifier.send_deletion_request(del_id, title, source)
        if new_msg_id:
            await db.update_stale_deletion_msg(del_id, new_msg_id)
            logger.info(f"Re-sent stale deletion request #{del_id} '{title}' → msg_id={new_msg_id}")


async def _execute_confirmed_deletions(db: Database, config: dict) -> None:
    """Execute physical deletion for items user confirmed via Telegram."""
    confirmed = await db.get_confirmed_deletions()

    for item in confirmed:
        item = dict(item)
        success = await _delete_item(item, config)
        if success:
            await db.mark_deletion_executed(item["id"])
            logger.info(f"Deleted: [{item['source']}] {item['title']}")
        else:
            logger.error(f"Failed to delete: [{item['source']}] {item['title']}")


async def _delete_item(item: dict, config: dict) -> bool:
    source = item.get("source", "")
    source_id = item.get("source_id", "")

    if source == "gdrive":
        try:
            service = await asyncio.to_thread(build_service, config)
            return await asyncio.to_thread(trash_file, service, source_id)
        except Exception as e:
            logger.error(f"Failed to trash Drive file {source_id}: {e}")
            return False

    elif source == "apple_notes":
        return await asyncio.to_thread(apple_notes.delete_note_by_id, source_id)

    return False


async def _goal_tracker(db: Database, notifier: Notifier, config: dict) -> None:
    """Post completed tasks from last 3 days to Goal Tracker Diary channel."""
    import anthropic

    channel_id = config.get("goal_tracker", {}).get("channel_id")
    if not channel_id:
        logger.warning("Goal Tracker: channel_id not configured")
        return

    model = config.get("classifier", {}).get("model", "claude-haiku-4-5-20251001")
    api_key = config.get("anthropic", {}).get("api_key", "")

    try:
        service = await asyncio.to_thread(google_tasks.build_service, config)
        completed = await asyncio.to_thread(google_tasks.get_completed_with_list, service, 3)
    except Exception as e:
        logger.error(f"Goal Tracker: failed to get completed tasks: {e}")
        return

    if not completed:
        return

    # Filter out already posted (check by task_id for stable dedup)
    new_entries = []
    for title, list_name, task_id in completed:
        if not await db.is_goal_tracker_posted(title, list_name, task_id):
            new_entries.append((title, list_name, task_id))

    if not new_entries:
        logger.info("Goal Tracker: no new completed tasks")
        return

    # Transform task names → achievements via Claude
    task_list = "\n".join(f"- {t} [{lst}]" for t, lst, _ in new_entries)
    transform_prompt = (
        f"Преобразуй эти названия выполненных задач в достижения для дневника целей. "
        f"Каждая строка — отдельное достижение. Сохрани формат '- [текст] [категория]'. "
        f"Сделай формулировки позитивными и в прошедшем времени (что сделал).\n\n{task_list}"
    )

    transformed_entries = [(t, lst) for t, lst, _ in new_entries]  # fallback
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": transform_prompt}],
        )
        transformed_text = response.content[0].text
        parsed = []
        for line in transformed_text.strip().split("\n"):
            line = line.strip("- ").strip()
            if "[" in line and "]" in line:
                bracket_start = line.rfind("[")
                bracket_end = line.rfind("]")
                list_name = line[bracket_start + 1:bracket_end]
                title_part = line[:bracket_start].strip()
                original_list = next((lst for _, lst, _ in new_entries if lst == list_name), list_name)
                parsed.append((title_part, original_list))
            else:
                for orig_title, orig_list, _ in new_entries:
                    if orig_title[:20] in line:
                        parsed.append((line, orig_list))
                        break
        if parsed:
            transformed_entries = parsed
    except Exception as e:
        logger.warning(f"Goal Tracker transform failed: {e}, using original titles")

    # Post to channel
    ok = await notifier.post_to_goal_tracker_channel(transformed_entries, channel_id)
    if ok:
        # Mark only the entries that were actually posted (transformed_entries may be
        # shorter than new_entries if Claude's output was truncated — preserve order)
        posted_count = len(transformed_entries)
        for title, list_name, task_id in new_entries[:posted_count]:
            await db.add_goal_tracker_post(title, list_name, task_id)
        logger.info(f"Goal Tracker: posted {posted_count} achievements")


FOLDER_MIME = "application/vnd.google-apps.folder"

MEDIA_MIMES = {
    "video/", "image/", "audio/",
}
BINARY_MIMES = {
    "application/zip", "application/x-zip-compressed",
    "application/x-dmg", "application/octet-stream",
    "application/x-iso9660-image",
}


def _is_media(mime: str) -> bool:
    return any(mime.startswith(m) for m in MEDIA_MIMES)


def _is_binary(mime: str) -> bool:
    return mime in BINARY_MIMES


async def _process_gdrive_legacy(
    db: Database, notifier: Notifier, config: dict, stats: dict
) -> None:
    """Process Google Drive category folders using hierarchical indexing strategy.

    Each run:
    1. Scans category folders and pending_deep folders for unclassified subfolders
    2. Sends Telegram decisions for new folders (limit: legacy_folders_per_run per run)
    Files inside folders are NOT processed by legacy — use inbox for file indexing.
    """
    try:
        service = await asyncio.to_thread(build_service, config)
    except Exception as e:
        logger.error(f"Google Drive auth failed: {e}")
        stats["errors"] += 1
        return

    folders_per_run = int(config.get("schedule", {}).get("legacy_folders_per_run", 5))
    decisions_counter = {"n": 0}

    # Collect category roots as entry points — DFS will recurse into pending_deep naturally
    entered: list[tuple[str, list[str]]] = []

    categories = config.get("categories", [])
    for folder_name in categories:
        config_key = f"folder_{folder_name.replace(' ', '_').lower()}"
        folder_id = config.get("google_drive", {}).get(config_key, "")
        if folder_id:
            entered.append((folder_id, [folder_name]))
        else:
            logger.warning(f"No folder_id for '{folder_name}' (key: {config_key})")
    # Note: pending_deep folders are NOT added separately — _scan_folder recurses into them
    # automatically when encountered during DFS from category roots, avoiding duplicate API calls.

    for folder_id, path_stack in entered:
        if decisions_counter["n"] >= folders_per_run:
            break
        try:
            await _scan_folder(
                service=service, db=db, notifier=notifier, config=config,
                folder_id=folder_id, path_stack=path_stack,
                decisions_counter=decisions_counter, folders_per_run=folders_per_run,
                stats=stats,
                _list=list_folder_contents,
            )
        except Exception as e:
            logger.error(f"Error scanning folder '{' / '.join(path_stack)}': {e}")
            stats["errors"] += 1


async def _scan_folder(
    service, db: Database, notifier: Notifier, config: dict,
    folder_id: str, path_stack: list[str],
    decisions_counter: dict, folders_per_run: int,
    stats: dict, _list,
) -> None:
    """Scan one folder: send decisions for unclassified subfolders. Files are not processed."""
    items = await asyncio.to_thread(lambda: list(_list(service, folder_id, False)))

    subfolders = [i for i in items if i.get("mimeType") == FOLDER_MIME]
    files = [i for i in items if i.get("mimeType") != FOLDER_MIME]

    # ── Subfolders: send decision or recurse into pending_deep ──────────────
    for subfolder in subfolders:
        sub_id = subfolder.get("id", "")
        sub_title = subfolder.get("name", "")
        sub_path = " / ".join(path_stack + [sub_title])

        status = await db.get_folder_status("gdrive", sub_id)

        if status is None:
            if decisions_counter["n"] < folders_per_run:
                await _send_folder_decision(
                    service=service, db=db, notifier=notifier, config=config,
                    folder_item=subfolder, path_stack=path_stack, sub_path=sub_path,
                    _list=_list,
                )
                decisions_counter["n"] += 1
                stats["folders_decided"] = stats.get("folders_decided", 0) + 1
            # else: limit reached — skip new decisions but keep iterating (don't break)

        elif status == "pending_deep":
            # Always recurse regardless of decisions counter — user already decided
            await _scan_folder(
                service=service, db=db, notifier=notifier, config=config,
                folder_id=sub_id, path_stack=path_stack + [sub_title],
                decisions_counter=decisions_counter, folders_per_run=folders_per_run,
                stats=stats, _list=_list,
            )
        # pending_decision / folder_summary / folder_done / folder_partial → skip

    # ── Media: notify only (no AI processing of files in legacy) ────────────
    media_files = [f for f in files if _is_media(f.get("mimeType", ""))]
    if media_files:
        await notifier.send_media_notification(
            path=" / ".join(path_stack),
            media_files=[f.get("name", "") for f in media_files],
        )


async def _send_folder_decision(
    service, db: Database, notifier: Notifier, config: dict,
    folder_item: dict, path_stack: list[str], sub_path: str, _list,
) -> None:
    """Register folder as pending_decision and send Telegram notification."""
    import anthropic

    folder_id = folder_item.get("id", "")
    title = folder_item.get("name", "")

    # List contents for the notification and agent suggestion
    try:
        contents = await asyncio.to_thread(lambda: list(_list(service, folder_id, False)))
    except Exception:
        contents = []

    subfolders = [i for i in contents if i.get("mimeType") == FOLDER_MIME]
    files = [i for i in contents if i.get("mimeType") != FOLDER_MIME]

    # Register in DB as pending_decision
    reg_id, _ = await db.upsert_folder("gdrive", folder_id, title, sub_path)
    await db.set_folder_status("gdrive", folder_id, "pending_decision")

    # Generate agent suggestion (Haiku, only names — no content reads)
    suggestion = ""
    try:
        lines = [f"📁 {i.get('name')}" for i in subfolders[:15]]
        lines += [f"📄 {i.get('name')}" for i in files[:15]]
        if len(contents) > 30:
            lines.append(f"... и ещё {len(contents) - 30}")
        listing = "\n".join(lines)

        api_key = config.get("anthropic", {}).get("api_key", "")
        model = config.get("classifier", {}).get("model", "claude-haiku-4-5-20251001")
        client = anthropic.Anthropic(api_key=api_key)
        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=model, max_tokens=80,
                messages=[{"role": "user", "content":
                    f"Папка: {title}\nПуть: {sub_path}\nСодержимое:\n{listing}\n\n"
                    f"Одним предложением: что это за папка? Только описание."}],
            )
        )
        suggestion = resp.content[0].text.strip()
    except Exception as e:
        logger.warning(f"Folder suggestion failed for '{title}': {e}")

    await notifier.send_folder_decision(
        reg_id=reg_id, title=title, path=sub_path,
        subfolder_count=len(subfolders), file_count=len(files),
        suggestion=suggestion, has_subfolders=bool(subfolders),
    )
    logger.info(f"Sent folder decision: {sub_path}")



async def _process_apple_notes_legacy(
    db: Database, notifier: Notifier, config: dict, stats: dict, limit: int
) -> None:
    """Process Apple Notes not yet in files_registry (excluding Inbox).
    Uses incremental tracking — reads only notes modified since last run.
    """
    import time as _time

    # Incremental: read only notes modified since last run
    last_run_ms_str = await db.get_pattern("legacy_notes_last_run_ms")
    try:
        since_ms = int(last_run_ms_str) if last_run_ms_str else 0
    except ValueError:
        logger.warning(f"Invalid legacy_notes_last_run_ms value: {last_run_ms_str!r}, resetting to 0")
        since_ms = 0

    logger.info(f"Reading Apple Notes modified since {since_ms} (incremental)...")
    now_ms = int(_time.time() * 1000)  # capture before query to avoid missing notes modified during query
    all_notes = await asyncio.to_thread(apple_notes.get_notes_modified_since, since_ms, 500)

    processed = 0
    notes_exhausted = True  # will be set False if we hit the limit mid-loop

    for note in all_notes:
        if processed >= limit:
            notes_exhausted = False
            break

        folder = note.get("folder", "")
        if not folder or folder == "Inbox":
            continue

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

        await _run_agent_on_legacy_item(
            db=db, notifier=notifier, config=config,
            source="apple_notes", source_id=note_id,
            title=title, content=content, stats=stats,
        )
        processed += 1

    # Only advance timestamp if we processed all notes — if limit was hit,
    # keep old timestamp so remaining notes are picked up on the next run.
    if notes_exhausted:
        await db.set_pattern("legacy_notes_last_run_ms", str(now_ms))
    else:
        logger.info(f"Apple Notes: hit limit ({limit}), not advancing timestamp — {len(all_notes) - processed} notes remain")


async def _run_agent_on_legacy_item(
    db: Database, notifier: Notifier, config: dict,
    source: str, source_id: str, title: str, content: str, stats: dict,
) -> None:
    """Send a single legacy item to Main Agent."""
    from sba import agent as main_agent

    # Hard cost limit check (same as inbox)
    cost_log: list = stats.get("cost_log", [])
    current_cost = sum(cost_log)
    limit = config.get("legacy", {}).get("max_session_cost_usd", 0.0)
    if limit and current_cost >= limit:
        if not stats.get("cost_limit_notified"):
            stats["cost_limit_notified"] = True
            logger.warning(f"Legacy: cost limit ${limit:.2f} reached (spent ${current_cost:.4f}), stopping")
            await notifier.send_message(
                f"⛔ <b>Legacy: лимит расходов (${limit:.2f}) исчерпан</b>\n\n"
                f"Потрачено: ${current_cost:.4f} за {len(cost_log)} вызовов.\n"
                f"Чтобы изменить лимит: <code>legacy.max_session_cost_usd</code> в config.yaml"
            )
        return

    message = (
        f"Обработай входящий элемент.\n"
        f"Источник: {source}\nID: {source_id}\n"
        f"Название: {title}\nСодержимое: {content[:2000]}"
    )

    try:
        await main_agent.run_main_agent(message, db=db, notifier=notifier, config=config,
                                        _cost_accumulator=cost_log)
        stats["processed"] += 1
    except Exception as e:
        logger.error(f"Agent failed for '{title}': {e}")
        stats["errors"] += 1


