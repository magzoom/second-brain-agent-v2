"""
Google Tasks integration.

Uses same OAuth credentials as Google Drive — combined SCOPES.
Task lists correspond to GTD categories (created automatically if missing).
"""

import logging
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Combined scopes — Drive + Tasks in one token
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
]

CATEGORY_LISTS = [
    "1_Health_Energy",
    "2_Business_Career",
    "3_Finance",
    "4_Family_Relationships",
    "5_Personal Growth",
    "6_Brightness life",
    "7_Spirituality",
]


def build_service(config: dict):
    """Build and return an authenticated Tasks API service."""
    creds_file = Path(
        config.get("google_drive", {}).get("credentials_file", "~/.sba/google_credentials.json")
    ).expanduser()
    token_file = Path(
        config.get("google_drive", {}).get("token_file", "~/.sba/google_token.json")
    ).expanduser()

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            for attempt in range(3):
                try:
                    creds.refresh(Request())
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning(f"Token refresh attempt {attempt + 1} failed: {e}, retrying...")
                    time.sleep(2 ** attempt)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=8085, access_type="offline", prompt="consent")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())

    return build("tasks", "v1", credentials=creds)


def _get_or_create_list(service, name: str) -> str:
    """Get task list ID by name, creating it if missing."""
    result = service.tasklists().list(maxResults=100).execute()
    for tl in result.get("items", []):
        if tl["title"] == name:
            return tl["id"]
    new_list = service.tasklists().insert(body={"title": name}).execute()
    logger.info(f"Created task list: {name}")
    return new_list["id"]


def _to_rfc3339_utc(date_str: str, time_str: str = "09:00") -> str:
    """Convert YYYY-MM-DD HH:MM (local time) to RFC 3339 UTC string."""
    dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").astimezone()
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def create_task(
    service,
    title: str,
    list_name: str = "1_Health_Energy",
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    notes: Optional[str] = None,
    priority: Optional[str] = None,
) -> Optional[str]:
    """Create a task in Google Tasks. Returns task ID on success."""
    list_id = _get_or_create_list(service, list_name)

    body: dict = {"title": title, "status": "needsAction"}
    if due_date:
        body["due"] = _to_rfc3339_utc(due_date, due_time or "09:00")

    notes_parts = []
    if notes:
        notes_parts.append(notes)
    if priority and priority != "medium":
        notes_parts.append(f"Приоритет: {priority}")
    if notes_parts:
        body["notes"] = "\n".join(notes_parts)

    try:
        task = service.tasks().insert(tasklist=list_id, body=body).execute()
        logger.info(f"Created task '{title}' in list '{list_name}': {task['id']}")
        return task["id"]
    except Exception as e:
        logger.error(f"Failed to create task '{title}': {e}")
        return None


def get_tasks_today(service) -> list[dict]:
    """Return incomplete tasks due today or overdue (all lists)."""
    end_dt = datetime.now().replace(hour=23, minute=59, second=59).astimezone(timezone.utc)
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    result = service.tasklists().list(maxResults=100).execute()
    items = []
    for tl in result.get("items", []):
        try:
            tasks_result = service.tasks().list(
                tasklist=tl["id"],
                dueMax=end_str,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            for t in tasks_result.get("items", []):
                if t.get("status") == "needsAction":
                    items.append({
                        "title": t["title"],
                        "list": tl["title"],
                        "due_date": t.get("due", "")[:10],
                    })
        except Exception:
            pass
    return items


def get_tasks_upcoming(service, days: int = 7) -> list[dict]:
    """Return incomplete tasks due in the next N days (all lists)."""
    end_date = (date.today() + timedelta(days=days)).isoformat()
    end_str = f"{end_date}T23:59:59.000Z"

    result = service.tasklists().list(maxResults=100).execute()
    items = []
    for tl in result.get("items", []):
        try:
            tasks_result = service.tasks().list(
                tasklist=tl["id"],
                dueMax=end_str,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            for t in tasks_result.get("items", []):
                if t.get("status") == "needsAction":
                    items.append({
                        "title": t["title"],
                        "list": tl["title"],
                        "due_date": t.get("due", "")[:10],
                    })
        except Exception:
            pass
    return items


def rollover_overdue_tasks(service) -> int:
    """Move all overdue incomplete tasks to today. Returns count of updated tasks."""
    from datetime import date as _date, timedelta as _timedelta
    yesterday_str = (_date.today() - _timedelta(days=1)).isoformat()
    # dueMax = yesterday means strictly overdue (before today)
    cutoff = f"{yesterday_str}T23:59:59.000Z"
    new_due = _date.today().strftime("%Y-%m-%dT09:00:00.000Z")

    result = service.tasklists().list(maxResults=100).execute()
    updated = 0
    for tl in result.get("items", []):
        try:
            tasks_result = service.tasks().list(
                tasklist=tl["id"],
                dueMax=cutoff,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            for t in tasks_result.get("items", []):
                if t.get("status") == "needsAction" and t.get("due"):
                    service.tasks().patch(
                        tasklist=tl["id"],
                        task=t["id"],
                        body={"due": new_due},
                    ).execute()
                    updated += 1
        except Exception:
            pass
    return updated


def get_completed_with_list(service, days: int = 3) -> list[tuple[str, str, str]]:
    """Return [(title, list_name, task_id)] for tasks completed within last N days (category lists only)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    result = service.tasklists().list(maxResults=100).execute()
    completed = []
    for tl in result.get("items", []):
        if tl["title"] not in CATEGORY_LISTS:
            continue
        try:
            tasks_result = service.tasks().list(
                tasklist=tl["id"],
                showCompleted=True,
                showHidden=True,
                completedMin=cutoff_str,
                maxResults=100,
            ).execute()
            for t in tasks_result.get("items", []):
                if t.get("status") == "completed":
                    completed.append((t["title"], tl["title"], t["id"]))
        except Exception:
            pass
    return completed
