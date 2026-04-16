"""
Shared registry for pending capability extension requests.
Used by agent.py (to register) and handlers.py (to execute after user approval).
"""

from typing import Optional

_pending: dict[int, dict] = {}
_counter: int = 0


def register(action: dict) -> int:
    global _counter
    _counter += 1
    _pending[_counter] = action
    return _counter


def get(ext_id: int) -> Optional[dict]:
    """Pop and return a pending extension, or None if not found."""
    return _pending.pop(ext_id, None)
