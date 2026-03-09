"""
Apple Calendar integration via AppleScript (osascript).
"""

import json
import subprocess
import logging
from typing import Optional
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)


def create_event(
    title: str,
    start_date: str,          # "2025-03-01"
    start_time: str = "09:00",
    duration_minutes: int = 60,
    calendar_name: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
) -> bool:
    """Create a calendar event in Apple Calendar. Returns True on success."""
    try:
        start_dt = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
    except ValueError as e:
        logger.error(f"Invalid date/time: {e}")
        return False

    end_dt = start_dt + timedelta(minutes=duration_minutes)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%B %-d, %Y at %-I:%M %p")

    safe_title = _esc(title)
    start_str = fmt(start_dt)
    end_str = fmt(end_dt)

    cal_selector = ""
    if calendar_name:
        safe_cal = _esc(calendar_name)
        cal_selector = f'set targetCal to first calendar whose name is "{safe_cal}"'
    else:
        cal_selector = "set targetCal to default calendar"

    description_parts = []
    if notes:
        description_parts.append(notes)
    if url:
        description_parts.append(f"Ссылка: {url}")
    desc_str = _esc("\n\n".join(description_parts)) if description_parts else ""
    desc_prop = f', description:"{desc_str}"' if desc_str else ""

    script = f"""
    tell application "Calendar"
        {cal_selector}
        set newEvent to make new event at targetCal with properties {{
            summary:"{safe_title}",
            start date:date "{start_str}",
            end date:date "{end_str}"
            {desc_prop}
        }}
        return uid of newEvent
    end tell
    """

    result = _run(script)
    if result["returncode"] == 0:
        logger.info(f"Created calendar event: '{title}' on {start_date} {start_time}")
        return True

    logger.error(f"Failed to create event '{title}': {result['stderr']}")
    return False


def get_events_today() -> list[dict]:
    """Return events scheduled for today from Apple Calendar via JXA."""
    today = date.today()
    jxa = f"""
var app = Application('Calendar');
var today = new Date('{today.isoformat()}');
today.setHours(0, 0, 0, 0);
var tomorrow = new Date(today);
tomorrow.setDate(tomorrow.getDate() + 1);
var result = [];
var cals = app.calendars();
for (var i = 0; i < cals.length; i++) {{
    try {{
        var events = cals[i].events();
        for (var j = 0; j < events.length; j++) {{
            try {{
                var sd = events[j].startDate();
                if (sd >= today && sd < tomorrow) {{
                    result.push({{
                        title: events[j].summary(),
                        start: sd.toISOString(),
                        calendar: cals[i].name()
                    }});
                }}
            }} catch(e) {{}}
        }}
    }} catch(e) {{}}
}}
JSON.stringify(result);
"""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout.strip())
    except Exception:
        return []


def list_calendars() -> list[str]:
    """Return names of all calendars."""
    result = _run('tell application "Calendar" to get name of calendars')
    if result["returncode"] == 0:
        return [c.strip() for c in result["stdout"].split(",") if c.strip()]
    return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run(script: str) -> dict:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
