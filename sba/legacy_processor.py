"""
Legacy Processor — runs daily at 09:00 via launchd.

Steps per run:
1. Execute confirmed deletions (status='confirmed' in pending_deletions)
2. Goal Tracker: post completed tasks to Telegram channel
3. Process Google Drive legacy files (limit_drive per run)
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

logger = logging.getLogger(__name__)

LOCK_FILE = Path.home() / ".sba" / "locks" / "legacy_v2.lock"


async def run(config: dict) -> None:
    """Main entry point for legacy processing."""
    notifier = Notifier(config)
    db_path = get_db_path(config)
    lock_fd = acquire_lock()

    # Backup DB before processing
    _backup_db(db_path)

    schedule = config.get("schedule", {})
    limit_drive = int(schedule.get("legacy_limit_drive", 3))
    limit_notes = int(schedule.get("legacy_limit_notes", 3))

    stats = {"processed": 0, "actions": 0, "deletions": 0, "errors": 0}

    try:
        async with Database(db_path) as db:
            await _execute_confirmed_deletions(db, config)
            await _rollover_overdue_tasks(config)
            await _goal_tracker(db, notifier, config)
            await _process_gdrive_legacy(db, notifier, config, stats, limit_drive)
            await _process_apple_notes_legacy(db, notifier, config, stats, limit_notes)

    except Exception as e:
        logger.error(f"Fatal error in legacy: {e}", exc_info=True)
        await notifier.send_message(f"⚠️ SBA legacy упал: {type(e).__name__}: {e}")
        raise
    finally:
        release_lock(lock_fd)

    await notifier.send_legacy_report(
        processed=stats["processed"],
        actions_created=stats["actions"],
        pending_deletions=stats["deletions"],
        errors=stats["errors"],
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
        from sba.integrations import google_tasks
        service = await asyncio.to_thread(google_tasks.build_service, config)
        count = await asyncio.to_thread(google_tasks.rollover_overdue_tasks, service)
        if count:
            logger.info(f"Rolled over {count} overdue tasks to today")
    except Exception as e:
        logger.warning(f"Task rollover failed: {e}")


async def _execute_confirmed_deletions(db: Database, config: dict) -> None:
    """Execute physical deletion for items user confirmed via Telegram."""
    async with db._conn.execute(
        """SELECT pd.id, pd.file_id, f.source, f.source_id, f.path, f.title
           FROM pending_deletions pd
           JOIN files_registry f ON f.id = pd.file_id
           WHERE pd.status='confirmed'"""
    ) as cur:
        confirmed = await cur.fetchall()

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
            from sba.integrations.google_drive import build_service, trash_file
            service = await asyncio.to_thread(build_service, config)
            return await asyncio.to_thread(trash_file, service, source_id)
        except Exception as e:
            logger.error(f"Failed to trash Drive file {source_id}: {e}")
            return False

    elif source == "apple_notes":
        from sba.integrations.apple_notes import delete_note_by_id
        return await asyncio.to_thread(delete_note_by_id, source_id)

    return False


async def _goal_tracker(db: Database, notifier: Notifier, config: dict) -> None:
    """Post completed tasks from last 3 days to Goal Tracker Diary channel."""
    from sba.integrations import google_tasks
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
        for title, list_name, task_id in new_entries:
            await db.add_goal_tracker_post(title, list_name, task_id)
        logger.info(f"Goal Tracker: posted {len(new_entries)} achievements")


async def _process_gdrive_legacy(
    db: Database, notifier: Notifier, config: dict, stats: dict, limit: int
) -> None:
    """Process unclassified files in Google Drive category folders.

    Uses hierarchical strategy from config index_rules:
    - skip_folders: register as folder_skipped, do not descend
    - summary_only_folders: index only README/main file, register as folder_summary
    - everything else: full file-by-file processing (up to limit)
    """
    try:
        from sba.integrations.google_drive import (
            build_service, list_folder_contents, get_file_content, metadata_hash,
        )
    except ImportError:
        return

    try:
        service = await asyncio.to_thread(build_service, config)
    except Exception as e:
        logger.error(f"Google Drive auth failed: {e}")
        stats["errors"] += 1
        return

    index_rules = config.get("index_rules", {})
    skip_patterns = index_rules.get("skip_folders", [])
    summary_patterns = index_rules.get("summary_only_folders", [])

    categories = config.get("categories", [])
    counter = {"n": 0}  # mutable counter shared across recursive _walk_folder calls

    for folder_name in categories:
        if counter["n"] >= limit:
            break

        config_key = f"folder_{folder_name.replace(' ', '_').lower()}"
        folder_id = config.get("google_drive", {}).get(config_key, "")
        if not folder_id:
            logger.warning(f"No folder_id for '{folder_name}' (key: {config_key})")
            continue

        try:
            await _walk_folder(
                service=service, db=db, notifier=notifier, config=config,
                folder_id=folder_id, skip_patterns=skip_patterns, summary_patterns=summary_patterns,
                stats=stats, limit=limit, counter=counter,
                _list=list_folder_contents, _get=get_file_content, _hash=metadata_hash,
            )
        except Exception as e:
            logger.error(f"Error processing Drive folder '{folder_name}': {e}")
            stats["errors"] += 1


def _matches(name: str, patterns: list[str]) -> bool:
    """Check if folder name matches any fnmatch pattern (case-insensitive)."""
    import fnmatch
    name_lower = name.lower()
    return any(fnmatch.fnmatch(name_lower, p.lower()) for p in patterns)


async def _walk_folder(
    service, db: Database, notifier: Notifier, config: dict,
    folder_id: str, skip_patterns: list, summary_patterns: list,
    stats: dict, limit: int, counter: dict,
    _list, _get, _hash,
) -> None:
    """Non-recursive controlled traversal of a Drive folder.

    For each subfolder encountered:
    - name matches skip_patterns → register as folder_skipped (once), stop descent
    - name matches summary_patterns → find README, index it, register as folder_summary (once)
    - else → recurse
    Files are processed normally (existing logic).
    """
    FOLDER_MIME = "application/vnd.google-apps.folder"

    items = await asyncio.to_thread(_list, service, folder_id, False)

    for item in items:
        if counter["n"] >= limit:
            return

        mime = item.get("mimeType", "")
        item_id = item.get("id", "")
        title = item.get("name", "Untitled")

        if mime == FOLDER_MIME:
            if _matches(title, skip_patterns):
                existing = await db.get_entry_type("gdrive", item_id)
                if existing != "folder_skipped":
                    await db.upsert_file(
                        source="gdrive", source_id=item_id,
                        title=title, path=item.get("webViewLink", ""),
                        entry_type="folder_skipped",
                    )
                    logger.info(f"Folder skipped (rule): {title}")
                # do not descend
            elif _matches(title, summary_patterns):
                existing = await db.get_entry_type("gdrive", item_id)
                if existing == "folder_summary":
                    continue  # already indexed
                readme = await _find_readme(service, item_id, _list)
                if readme:
                    await _process_gdrive_file(
                        file_info=readme, service=service, db=db, notifier=notifier,
                        config=config, stats=stats, counter=counter, limit=limit,
                        _get=_get, _hash=_hash,
                    )
                await db.upsert_file(
                    source="gdrive", source_id=item_id,
                    title=title, path=item.get("webViewLink", ""),
                    entry_type="folder_summary",
                )
                logger.info(f"Folder summary indexed: {title}")
                # do not descend further
            else:
                # Recurse into subfolder
                await _walk_folder(
                    service=service, db=db, notifier=notifier, config=config,
                    folder_id=item_id, skip_patterns=skip_patterns, summary_patterns=summary_patterns,
                    stats=stats, limit=limit, counter=counter,
                    _list=_list, _get=_get, _hash=_hash,
                )
        else:
            await _process_gdrive_file(
                file_info=item, service=service, db=db, notifier=notifier,
                config=config, stats=stats, counter=counter, limit=limit,
                _get=_get, _hash=_hash,
            )


async def _find_readme(service, folder_id: str, _list) -> dict | None:
    """Find a README or main summary file in a folder. Returns file_info dict or None."""
    README_PATTERNS = ("readme", "_index", "_о папке", "summary", "overview", "main", "about")
    try:
        items = await asyncio.to_thread(_list, service, folder_id, False)
        for item in items:
            if item.get("mimeType", "") == "application/vnd.google-apps.folder":
                continue
            name_lower = item.get("name", "").lower()
            if any(name_lower.startswith(p) for p in README_PATTERNS):
                return item
    except Exception as e:
        logger.warning(f"_find_readme failed for folder {folder_id}: {e}")
    return None


async def _process_gdrive_file(
    file_info: dict, service, db: Database, notifier: Notifier, config: dict,
    stats: dict, counter: dict, limit: int, _get, _hash,
) -> None:
    """Process a single Drive file: upsert registry, read content, run agent."""
    if counter["n"] >= limit:
        return

    file_id = file_info.get("id", "")
    mime = file_info.get("mimeType", "")
    title = file_info.get("name", "Untitled")
    c_hash = _hash(file_info)

    _, is_new = await db.upsert_file(
        source="gdrive", source_id=file_id,
        content_hash=c_hash, title=title,
        path=file_info.get("webViewLink", ""),
    )
    if not is_new:
        return

    content_text = ""
    try:
        content_bytes = await asyncio.to_thread(_get, service, file_id, mime)
        if content_bytes:
            content_text = content_bytes.decode("utf-8", errors="ignore")
    except Exception:
        pass

    await _run_agent_on_legacy_item(
        db=db, notifier=notifier, config=config,
        source="gdrive", source_id=file_id,
        title=title, content=content_text, stats=stats,
    )
    counter["n"] += 1


async def _process_apple_notes_legacy(
    db: Database, notifier: Notifier, config: dict, stats: dict, limit: int
) -> None:
    """Process Apple Notes not yet in files_registry (excluding Inbox).
    Uses incremental tracking — reads only notes modified since last run.
    """
    from sba.integrations import apple_notes
    import time as _time

    # Incremental: read only notes modified since last run
    last_run_ms_str = await db.get_pattern("legacy_notes_last_run_ms")
    since_ms = int(last_run_ms_str) if last_run_ms_str else 0

    logger.info(f"Reading Apple Notes modified since {since_ms} (incremental)...")
    all_notes = await asyncio.to_thread(apple_notes.get_notes_modified_since, since_ms, 500)
    now_ms = int(_time.time() * 1000)

    processed = 0

    for note in all_notes:
        if processed >= limit:
            break

        folder = note.get("folder", "")
        if folder == "Inbox":
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

    # Save timestamp AFTER processing — if we crash mid-loop, next run re-reads modified notes
    await db.set_pattern("legacy_notes_last_run_ms", str(now_ms))


async def _run_agent_on_legacy_item(
    db: Database, notifier: Notifier, config: dict,
    source: str, source_id: str, title: str, content: str, stats: dict,
) -> None:
    """Send a single legacy item to Main Agent."""
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


