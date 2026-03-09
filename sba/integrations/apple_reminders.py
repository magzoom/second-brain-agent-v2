"""
Apple Reminders integration via AppleScript (osascript).

GTD categories (must match exact list names in Apple Reminders):
  1_Health_Energy, 2_Business_Career, 3_Finance, 4_Family_Relationships,
  5_Personal Growth, 6_Brightness life, 7_Spirituality

IMPORTANT: Property-based date setting (set year/month/day) — NOT string format.
String dates break on Russian locale. This is a known macOS limitation.
"""

import subprocess
import logging
from typing import Optional
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

CATEGORY_LISTS = [
    "1_Health_Energy",
    "2_Business_Career",
    "3_Finance",
    "4_Family_Relationships",
    "5_Personal Growth",      # space, not underscore
    "6_Brightness life",      # space, not underscore
    "7_Spirituality",
]


def get_all_list_names() -> list[str]:
    result = _run('tell application "Reminders" to get name of lists')
    if result["returncode"] == 0:
        return [n.strip() for n in result["stdout"].split(",") if n.strip()]
    return []


def create_task(
    title: str,
    list_name: str = "Inbox",
    due_date: Optional[str] = None,   # "2025-03-01"
    due_time: Optional[str] = None,   # "10:00"
    notes: Optional[str] = None,
    url: Optional[str] = None,
    priority: int = 0,                # 0=none, 1=high, 5=medium, 9=low
) -> Optional[str]:
    """
    Create a reminder task. Returns task name on success.
    Uses property-based date setting (locale-independent).
    If due date is in the past, advances year by 1.
    """
    safe_title = _esc(title)
    safe_list = _esc(list_name or "Inbox")

    props = f'name:"{safe_title}"'

    dt = None
    if due_date:
        time_part = due_time or "09:00"
        dt_str = f"{due_date} {time_part}"
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            if dt.date() < date.today():
                dt = dt.replace(year=dt.year + 1)
        except ValueError:
            logger.warning(f"Invalid due date/time: {dt_str}")

    if notes or url:
        combined = ""
        if notes:
            combined += notes
        if url:
            combined += f"\n\nСсылка: {url}"
        props += f', body:"{_esc(combined)}"'

    if priority:
        props += f", priority:{priority}"

    due_date_block = ""
    if dt:
        due_date_block = f"""
        set dueDate to current date
        set year of dueDate to {dt.year}
        set month of dueDate to {dt.month}
        set day of dueDate to {dt.day}
        set hours of dueDate to {dt.hour}
        set minutes of dueDate to {dt.minute}
        set seconds of dueDate to 0
        set due date of newReminder to dueDate"""

    script = f"""
    tell application "Reminders"
        set targetList to missing value
        try
            set targetList to list "{safe_list}"
        end try
        if targetList is missing value then
            set targetList to list "Inbox"
        end if
        set newReminder to make new reminder at targetList with properties {{{props}}}{due_date_block}
        return name of newReminder
    end tell
    """

    result = _run(script)
    if result["returncode"] == 0:
        logger.info(f"Created reminder: '{title}' in '{list_name}'")
        return result["stdout"]

    logger.error(f"Failed to create reminder '{title}': {result['stderr']}")
    return None


def get_reminders_today() -> list[dict]:
    """Return tasks due today or overdue (incomplete). Uses AppleScript whose-filter — fast."""
    script = """
tell application "Reminders"
    set cutoff to current date
    set hours of cutoff to 23
    set minutes of cutoff to 59
    set seconds of cutoff to 59
    set outLines to {}
    repeat with theList in (every list)
        set listName to name of theList
        try
            set dueItems to (every reminder of theList whose completed is false and due date ≤ cutoff)
            repeat with r in dueItems
                set end of outLines to (name of r) & "|||" & listName
            end repeat
        end try
    end repeat
    if (count of outLines) = 0 then return ""
    set AppleScript's text item delimiters to linefeed
    return outLines as text
end tell
"""
    result = _run(script, timeout=60)
    if result["returncode"] != 0 or not result["stdout"].strip():
        return []
    items = []
    for line in result["stdout"].strip().splitlines():
        parts = line.split("|||")
        if len(parts) == 2:
            items.append({"title": parts[0], "list": parts[1], "due_date": None})
    return items


def get_reminders_upcoming(days: int = 7) -> list[dict]:
    """Return incomplete tasks due in the next N days. Single whose-condition for speed."""
    from datetime import date as _date
    end_date_str = (_date.today() + timedelta(days=days)).isoformat()
    script = f"""
tell application "Reminders"
    set endDate to current date
    set year of endDate to (year of (date "{end_date_str}"))
    set month of endDate to (month of (date "{end_date_str}") as integer)
    set day of endDate to (day of (date "{end_date_str}"))
    set hours of endDate to 23
    set minutes of endDate to 59
    set seconds of endDate to 59
    set outLines to {{}}
    repeat with theList in (every list)
        set listName to name of theList
        try
            set dueItems to (every reminder of theList whose completed is false and due date ≤ endDate)
            repeat with r in dueItems
                set dd to due date of r
                set dStr to (year of dd as text) & "-" & text -2 thru -1 of ("0" & ((month of dd as integer) as text)) & "-" & text -2 thru -1 of ("0" & (day of dd as text))
                set end of outLines to (name of r) & "|||" & listName & "|||" & dStr
            end repeat
        end try
    end repeat
    if (count of outLines) = 0 then return ""
    set AppleScript's text item delimiters to linefeed
    return outLines as text
end tell
"""
    result = _run(script, timeout=60)
    if result["returncode"] != 0 or not result["stdout"].strip():
        return []
    items = []
    for line in result["stdout"].strip().splitlines():
        parts = line.split("|||")
        if len(parts) >= 2:
            items.append({
                "title": parts[0],
                "list": parts[1],
                "due_date": parts[2] if len(parts) > 2 else None,
            })
    return items


def get_all_completed_with_list(days: int = 3) -> list[tuple[str, str]]:
    """
    Return [(title, list_name)] for tasks completed within last N days.
    Uses JXA batch access — significantly faster than AppleScript.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    existing = set(get_all_list_names())
    lists_to_check = [lst for lst in CATEGORY_LISTS if lst in existing]
    if not lists_to_check:
        return []

    list_names_json = str(lists_to_check).replace("'", '"')
    jxa = f"""
var app = Application('Reminders');
var targetLists = {list_names_json};
var cutoff = new Date('{cutoff}');
cutoff.setHours(0, 0, 0, 0);
var output = [];
targetLists.forEach(function(listName) {{
    try {{
        var matched = app.lists.whose({{name: listName}});
        if (matched.length === 0) return;
        var completed = matched[0].reminders.whose({{completed: true}});
        var names = completed.name();
        var dates = completed.completionDate();
        for (var i = 0; i < names.length; i++) {{
            if (dates[i] && dates[i] >= cutoff) {{
                output.push(names[i] + '|||' + listName);
            }}
        }}
    }} catch(e) {{}}
}});
output.join('~~~');
"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", jxa],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.warning("get_all_completed_with_list: JXA timed out (>300s)")
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    results = []
    for chunk in result.stdout.strip().split("~~~"):
        chunk = chunk.strip()
        if "|||" in chunk:
            title, list_name = chunk.split("|||", 1)
            results.append((title.strip(), list_name.strip()))
    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run(script: str, timeout: int = 30) -> dict:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
