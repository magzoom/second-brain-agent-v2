"""
Shared Anthropic API client with connection-pool reuse.

Instead of creating a new anthropic.Anthropic() per call (which opens a new
HTTPS connection each time), callers use get_anthropic_client() which returns
a cached instance keyed by API key.
"""

import anthropic
from typing import Optional

_clients: dict[str, anthropic.Anthropic] = {}


def get_anthropic_client(config: dict, timeout: float = 30.0) -> anthropic.Anthropic:
    """Return a cached Anthropic client for the given config."""
    api_key = config.get("anthropic", {}).get("api_key", "")
    if api_key not in _clients:
        _clients[api_key] = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    return _clients[api_key]
