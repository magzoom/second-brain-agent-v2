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
_DEV_WAIT_SECONDS = 1800  # 30 minutes


def wait_if_dev_active() -> bool:
    """Check if dev_processor is running. If so, wait 30 min and re-check once.
    Returns True if safe to proceed, False if still active after waiting (skip run)."""

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

    logger.info("Dev processor active — deferring run by 30 minutes")
    time.sleep(_DEV_WAIT_SECONDS)

    if _is_active():
        logger.info("Dev processor still active after 30 min — skipping this run")
        return False

    logger.info("Dev processor finished — proceeding with run")
    return True


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
