"""
Google Drive integration.

Uses OAuth2 with changes().list + pageToken for incremental sync —
only new/modified files are fetched, not a full scan every time.
"""

import logging
import hashlib
import httplib2
import time
from pathlib import Path
from typing import Optional, Generator

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
]


def build_service(config: dict):
    """Build and return an authenticated Drive API service."""
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
                    time.sleep(2 ** attempt)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=8085, access_type="offline", prompt="consent")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def authorize(config: dict) -> None:
    """Interactive OAuth flow — run once to generate token.json."""
    build_service(config)
    print("Google Drive authorized successfully.")


def get_start_page_token(service) -> str:
    """Get the current start page token for changes tracking."""
    response = service.changes().getStartPageToken().execute()
    return response.get("startPageToken")


def get_changes(service, page_token: str) -> tuple[list[dict], str]:
    """
    Fetch changes since page_token using changes().list.
    Returns (list_of_changed_files, new_page_token).
    Only returns non-trashed files (additions/modifications).
    """
    changes = []
    new_token = page_token

    while True:
        response = service.changes().list(
            pageToken=page_token,
            spaces="drive",
            fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,parents,webViewLink,size,modifiedTime,md5Checksum))",
            includeRemoved=False,
        ).execute()

        for change in response.get("changes", []):
            if change.get("removed"):
                continue
            file_info = change.get("file")
            if file_info and not _is_google_workspace_type(file_info.get("mimeType", "")):
                changes.append(file_info)
            elif file_info:
                # Google Workspace files (Docs, Sheets, etc.) — include without md5
                changes.append(file_info)

        if "nextPageToken" in response:
            page_token = response["nextPageToken"]
        else:
            new_token = response.get("newStartPageToken", new_token)
            break

    logger.info(f"Fetched {len(changes)} changed files from Google Drive")
    return changes, new_token


def list_folder_contents(
    service,
    folder_id: str,
    recursive: bool = False,
) -> Generator[dict, None, None]:
    """
    List files in a specific Drive folder.
    Yields file metadata dicts.
    """
    query = f"'{folder_id}' in parents and trashed=false"
    page_token = None

    while True:
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken,files(id,name,mimeType,parents,webViewLink,size,modifiedTime,md5Checksum)",
            pageToken=page_token,
        ).execute()

        for file_info in response.get("files", []):
            yield file_info
            if recursive and file_info.get("mimeType") == "application/vnd.google-apps.folder":
                yield from list_folder_contents(service, file_info["id"], recursive=True)

        page_token = response.get("nextPageToken")
        if not page_token:
            break


def get_file_content(service, file_id: str, mime_type: str = None) -> Optional[bytes]:
    """
    Download file content.
    For Google Workspace files (Docs/Sheets) exports as plain text.
    Returns None on error.
    """
    try:
        if mime_type and _is_google_workspace_type(mime_type):
            export_mime = _get_export_mime(mime_type)
            if export_mime:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                logger.warning(f"Cannot export mime type: {mime_type}")
                return None
        else:
            request = service.files().get_media(fileId=file_id)

        http = httplib2.Http(timeout=60)
        return request.execute(http=http)
    except HttpError as e:
        if e.resp.status == 403:
            logger.warning(f"No permission to download file {file_id}")
        else:
            logger.error(f"Failed to download file {file_id}: {e}")
        return None


def move_file_to_folder(service, file_id: str, target_folder_id: str) -> bool:
    """Move a file to a different folder (update parents)."""
    try:
        # Get current parents
        file = service.files().get(fileId=file_id, fields="parents").execute()
        current_parents = ",".join(file.get("parents", []))

        service.files().update(
            fileId=file_id,
            addParents=target_folder_id,
            removeParents=current_parents,
            fields="id,parents",
        ).execute()
        return True
    except HttpError as e:
        logger.error(f"Failed to move file {file_id}: {e}")
        return False


def upload_file(
    service,
    local_path: Path,
    file_name: str,
    mime_type: str,
    parent_folder_id: str = "",
) -> dict:
    """
    Upload a local file to Google Drive.
    Returns the created file metadata dict (includes webViewLink).
    """
    from googleapiclient.http import MediaFileUpload

    metadata = {"name": file_name}
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
    ).execute()
    logger.info(f"Uploaded '{file_name}' to Drive: {file.get('id')}")
    return file


def trash_file(service, file_id: str) -> bool:
    """Move a file to trash (reversible deletion). Returns True if trashed or already gone."""
    try:
        service.files().update(
            fileId=file_id,
            body={"trashed": True},
        ).execute()
        logger.info(f"Trashed Drive file: {file_id}")
        return True
    except HttpError as e:
        if e.resp.status in (404, 410):
            logger.info(f"Drive file {file_id} already deleted (HTTP {e.resp.status})")
            return True  # goal achieved — file is gone
        logger.error(f"Failed to trash file {file_id}: {e}")
        return False


def find_folder_by_name(service, folder_name: str, parent_id: str = None) -> Optional[str]:
    """
    Search for a folder by exact name in Drive.
    Returns folder ID or None if not found.
    Optionally restrict to children of parent_id.
    """
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    try:
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="files(id,name)",
            pageSize=1,
        ).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None
    except HttpError as e:
        logger.error(f"Failed to find folder '{folder_name}': {e}")
        return None


def get_file_metadata(service, file_id: str) -> Optional[dict]:
    """Fetch metadata for a single file."""
    try:
        return service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,parents,webViewLink,size,modifiedTime,md5Checksum",
        ).execute()
    except HttpError:
        return None


def content_hash(content: bytes) -> str:
    """SHA-256 hash of file content."""
    return hashlib.sha256(content).hexdigest()


def metadata_hash(file_info: dict) -> str:
    """Use Drive's own md5Checksum if available, else hash the modifiedTime."""
    if file_info.get("md5Checksum"):
        return file_info["md5Checksum"]
    return hashlib.sha256(
        (file_info.get("modifiedTime", "") + file_info.get("id", "")).encode()
    ).hexdigest()


def create_summary_file(service, folder_id: str, content: str) -> dict:
    """Create _sba_summary.md in a Drive folder. Returns file metadata (id, webViewLink)."""
    from googleapiclient.http import MediaInMemoryUpload

    # Delete existing summary file if present
    query = f"name='_sba_summary.md' and '{folder_id}' in parents and trashed=false"
    existing = service.files().list(q=query, fields="files(id)").execute().get("files", [])
    for f in existing:
        try:
            service.files().delete(fileId=f["id"]).execute()
        except HttpError:
            pass

    metadata = {"name": "_sba_summary.md", "parents": [folder_id], "mimeType": "text/markdown"}
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown")
    file = service.files().create(
        body=metadata, media_body=media, fields="id,name,webViewLink,mimeType",
    ).execute()
    logger.info(f"Created _sba_summary.md in folder {folder_id}: {file.get('id')}")
    return file


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_google_workspace_type(mime_type: str) -> bool:
    return mime_type.startswith("application/vnd.google-apps.")


def _get_export_mime(workspace_mime: str) -> Optional[str]:
    mapping = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
        "application/vnd.google-apps.drawing": "image/png",
    }
    return mapping.get(workspace_mime)
