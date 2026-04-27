"""
Shared registry for pending capability extension requests.
Used by agent.py (to register) and handlers.py (to execute after user approval).
"""

import threading
from typing import Optional

_pending: dict[int, dict] = {}
_counter: int = 0
_lock = threading.Lock()


def register(action: dict) -> int:
    global _counter
    with _lock:
        _counter += 1
        _id = _counter
        _pending[_id] = action
    return _id


def get(ext_id: int) -> Optional[dict]:
    """Pop and return a pending extension, or None if not found."""
    with _lock:
        return _pending.pop(ext_id, None)
