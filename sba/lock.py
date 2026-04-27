"""
fcntl-based process lock. Used by inbox and legacy processors.
OS automatically releases the lock when the process crashes.
"""

import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEV_REQUEST_FILE = Path.home() / ".sba" / "dev_request.json"
_DEV_POLL_INTERVAL = 60   # seconds between checks
_DEV_WAIT_MAX = 900       # max total wait: 15 minutes (was 30, reduced to avoid overlapping launchd runs)


def wait_if_dev_active() -> bool:
    """Check if dev_processor is running. If so, poll every 60 s up to 15 min.
    Returns True if safe to proceed, False if still active after timeout (skip run)."""

    def _is_active() -> bool:
        if not _DEV_REQUEST_FILE.exists():
            return False
        try:
            data = json.loads(_DEV_REQUEST_FILE.read_text(encoding="utf-8"))
            return data.get("status") in ("pending", "processing")
        except Exception:
            return False

    if not _is_active():
        return True

    waited = 0
    logger.info("Dev processor active — polling every 60s (max 15 min)")
    while waited < _DEV_WAIT_MAX:
        time.sleep(_DEV_POLL_INTERVAL)
        waited += _DEV_POLL_INTERVAL
        if not _is_active():
            logger.info(f"Dev processor finished after {waited}s — proceeding with run")
            return True

    logger.info(f"Dev processor still active after {waited}s — skipping this run")
    return False


def acquire_lock(lock_file: Path) -> object:
    """Acquire an exclusive non-blocking lock. Exits if already held."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_file, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info(f"Lock {lock_file.name} already held by another process, exiting")
        sys.exit(0)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def release_lock(fd) -> None:
    """Release the lock and remove the lock file."""
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()
    try:
        Path(fd.name).unlink(missing_ok=True)
    except Exception:
        pass
