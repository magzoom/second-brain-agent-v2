"""
Apple Notes integration.

Read → JXA (JavaScript for Automation) — stabile note.id(), fast folder access
Write → AppleScript — ensures iCloud sync integrity
"""

import json
import subprocess
import logging
import time

logger = logging.getLogger(__name__)


# ── Reading ───────────────────────────────────────────────────────────────────

def get_all_notes(limit: int = 500) -> list[dict]:
    """
    Read notes from Apple Notes via JXA.
    Returns list of {id, title, content_text, folder}.
    Slow (1-3 min on large libraries) — wrap in asyncio.to_thread().
    """
    jxa_script = f"""
var app = Application("Notes");
var notes = app.notes();
var result = [];
var limit = {limit};
for (var i = 0; i < Math.min(notes.length, limit); i++) {{
    var note = notes[i];
    try {{
        var folderName = "";
        try {{ folderName = note.container().name(); }} catch(e) {{}}
        result.push({{
            id: note.id(),
            title: note.name(),
            content_text: note.plaintext(),
            folder: folderName
        }});
    }} catch(e) {{
        try {{ result.push({{id: note.id(), title: note.name(), content_text: "", folder: ""}}); }} catch(e2) {{}}
    }}
}}
JSON.stringify(result);
"""
    logger.info(f"Reading up to {limit} Apple Notes via JXA (may take 1-3 min)...")
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        logger.error(f"Failed to read Apple Notes via JXA: {result.stderr.strip()}")
        return []
    try:
        notes_data = json.loads(result.stdout.strip())
        logger.info(f"Read {len(notes_data)} notes from Apple Notes")
        return notes_data
    except Exception as e:
        logger.error(f"Failed to parse Apple Notes JXA output: {e}")
        return []


def get_notes_modified_since(since_ms: int, limit: int = 500) -> list[dict]:
    """
    Return notes modified after since_ms (Unix milliseconds) via JXA.
    Used by legacy processor for incremental indexing — avoids full re-scan.
    """
    jxa_script = f"""
var app = Application("Notes");
var notes = app.notes();
var result = [];
var cutoffTime = {since_ms};
var limit = {limit};
for (var i = 0; i < notes.length && result.length < limit; i++) {{
    var note = notes[i];
    try {{
        var modDate = note.modificationDate();
        if (modDate.getTime() <= cutoffTime) continue;
        var folderName = "";
        try {{ folderName = note.container().name(); }} catch(e) {{}}
        result.push({{
            id: note.id(),
            title: note.name(),
            content_text: note.plaintext(),
            folder: folderName
        }});
    }} catch(e) {{
        try {{ result.push({{id: note.id(), title: note.name(), content_text: "", folder: ""}}); }} catch(e2) {{}}
    }}
}}
JSON.stringify(result);
"""
    logger.info(f"Reading Apple Notes modified since {since_ms} (limit={limit})...")
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        logger.error(f"Failed to read Apple Notes modified since {since_ms}: {result.stderr.strip()}")
        return []
    try:
        notes_data = json.loads(result.stdout.strip())
        logger.info(f"Found {len(notes_data)} notes modified since {since_ms}")
        return notes_data
    except Exception as e:
        logger.error(f"Failed to parse Apple Notes JXA output: {e}")
        return []


def get_notes_in_folder(folder_name: str) -> list[dict]:
    """Return all notes in a specific folder via targeted JXA query. Fast (~0.5s)."""
    safe_folder = folder_name.replace("\\", "\\\\").replace('"', '\\"')
    jxa_script = f"""
var app = Application("Notes");
var result = [];
var folders = app.folders();
var targetFolder = null;
for (var i = 0; i < folders.length; i++) {{
    if (folders[i].name() === "{safe_folder}") {{
        targetFolder = folders[i];
        break;
    }}
}}
if (targetFolder) {{
    var notes = targetFolder.notes();
    for (var i = 0; i < notes.length; i++) {{
        var note = notes[i];
        try {{
            result.push({{
                id: note.id(),
                title: note.name(),
                content_text: note.plaintext(),
                folder: "{safe_folder}"
            }});
        }} catch(e) {{
            try {{ result.push({{id: note.id(), title: note.name(), content_text: "", folder: "{safe_folder}"}}); }} catch(e2) {{}}
        }}
    }}
}}
JSON.stringify(result);
"""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        logger.error(f"Failed to read Notes folder '{folder_name}': {result.stderr.strip()}")
        return []
    try:
        return json.loads(result.stdout.strip())
    except Exception as e:
        logger.error(f"Failed to parse Notes JXA output: {e}")
        return []


# ── Writing ───────────────────────────────────────────────────────────────────

def create_note(title: str, body_html: str, folder: str = "Inbox") -> bool:
    """Create a new note in Apple Notes. Returns True on success."""
    safe_title = _escape_applescript(title)
    safe_body = _escape_applescript(body_html)
    safe_folder = _escape_applescript(folder)

    script = f"""
    tell application "Notes"
        if not (exists folder "{safe_folder}") then
            make new folder with properties {{name:"{safe_folder}"}}
        end if
        set targetFolder to folder "{safe_folder}"
        make new note at targetFolder with properties {{name:"{safe_title}", body:"{safe_body}"}}
    end tell
    """
    result = _run_osascript(script)
    if result["returncode"] != 0:
        logger.error(f"Failed to create note '{title}': {result['stderr']}")
        return False
    logger.info(f"Created note '{title}' in folder '{folder}'")
    return True


def move_note_to_folder(note_title: str, target_folder: str) -> bool:
    """Move a note to another folder (matched by title)."""
    safe_title = _escape_applescript(note_title)
    safe_folder = _escape_applescript(target_folder)

    script = f"""
    tell application "Notes"
        if not (exists folder "{safe_folder}") then
            make new folder with properties {{name:"{safe_folder}"}}
        end if
        set targetFolder to folder "{safe_folder}"
        set matchedNote to first note whose name is "{safe_title}"
        move matchedNote to targetFolder
    end tell
    """
    result = _run_osascript(script)
    if result["returncode"] != 0:
        logger.error(f"Failed to move note '{note_title}': {result['stderr']}")
        return False
    logger.info(f"Moved note '{note_title}' → '{target_folder}'")
    return True


def move_note_by_id(note_id: str, target_folder: str, retries: int = 3, retry_delay: float = 3.0) -> bool:
    """Move a note to another folder by its stable JXA ID (x-coredata://...).
    Uses AppleScript (not JXA) — JXA container= assignment fails with -10003 on macOS 26.
    Retries on transient -10003 errors (iCloud sync in progress).
    """
    safe_id = _escape_applescript(note_id)
    safe_folder = _escape_applescript(target_folder)

    script = f"""
    tell application "Notes"
        if not (exists folder "{safe_folder}") then
            make new folder with properties {{name:"{safe_folder}"}}
        end if
        set targetFolder to folder "{safe_folder}"
        set matchedNote to note id "{safe_id}"
        move matchedNote to targetFolder
    end tell
    """
    for attempt in range(1, retries + 1):
        result = _run_osascript(script)
        if result["returncode"] == 0:
            logger.info(f"Moved note {note_id} → '{target_folder}'")
            return True
        stderr = result["stderr"]
        if "-10003" in stderr and attempt < retries:
            logger.warning(f"move_note_by_id attempt {attempt}/{retries} failed (-10003, Notes syncing?), retrying in {retry_delay}s")
            time.sleep(retry_delay)
        else:
            logger.error(f"Failed to move note by id '{note_id}': {stderr}")
            return False
    return False


def delete_note_by_id(note_id: str) -> bool:
    """Delete a note by its stable JXA ID (x-coredata://...)."""
    safe_id = note_id.replace("\\", "\\\\").replace('"', '\\"')
    jxa_script = f"""
var app = Application("Notes");
var notes = app.notes();
for (var i = 0; i < notes.length; i++) {{
    if (notes[i].id() === "{safe_id}") {{
        notes[i].delete();
        "ok";
        break;
    }}
}}
"not_found";
"""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        logger.error(f"Failed to delete note by id '{note_id}': {result.stderr.strip()}")
        return False
    success = "ok" in result.stdout
    not_found = not success and "not_found" in result.stdout
    if success:
        logger.info(f"Deleted note {note_id}")
    elif not_found:
        logger.info(f"Note {note_id} already deleted (not found)")
    return success or not_found  # both mean goal achieved — note is gone


def list_folders() -> list[str]:
    """Return all folder names in Apple Notes."""
    script = 'tell application "Notes" to get name of folders'
    result = _run_osascript(script)
    if result["returncode"] == 0:
        return [f.strip() for f in result["stdout"].split(",") if f.strip()]
    return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _escape_applescript(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    return text


def _run_osascript(script: str) -> dict:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
