"""
fcntl-based process lock. Used by inbox and legacy processors.
OS automatically releases the lock when the process crashes.
"""

import fcntl
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


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
