"""
Google Calendar integration.

Uses same OAuth credentials as Google Drive + Tasks.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
]


def build_service(config: dict):
    """Build and return an authenticated Calendar API service."""
    import time as _time
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds_file = Path(
        config.get("google_drive", {}).get("credentials_file", "~/.sba/google_credentials.json")
    ).expanduser()
    token_file = Path(
        config.get("google_drive", {}).get("token_file", "~/.sba/google_token.json")
    ).expanduser()

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
                    _time.sleep(2 ** attempt)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=8085, access_type="offline", prompt="consent")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def create_event(
    config: dict,
    title: str,
    start_date: str,           # "2025-03-01"
    start_time: str = "09:00",
    duration_minutes: int = 60,
    notes: Optional[str] = None,
    calendar_id: str = "primary",
) -> bool:
    """Create a Google Calendar event. Returns True on success."""
    try:
        start_dt = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
    except ValueError as e:
        logger.error(f"Invalid date/time: {e}")
        return False

    end_dt = start_dt + timedelta(minutes=duration_minutes)

    event_body = {
        "summary": title,
        "start": {
            "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "Asia/Almaty",
        },
        "end": {
            "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": "Asia/Almaty",
        },
    }
    if notes:
        event_body["description"] = notes

    try:
        service = build_service(config)
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        logger.info(f"Created calendar event: '{title}' on {start_date} {start_time} — {event.get('id')}")
        return True
    except Exception as e:
        logger.error(f"Failed to create calendar event '{title}': {e}")
        return False
