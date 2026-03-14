"""In-memory metadata cache with TTL."""

from __future__ import annotations

import time
from typing import Any


class MetadataCache:
    """Simple in-memory cache with per-key TTL for Data Model Navigator metadata.

    Stores class hierarchies, field definitions, and relationship types fetched
    from ServiceNow. Default TTL is 1 hour, configurable at init.
    """

    def __init__(self, ttl: int = 3600) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """Return cached value if present and not expired, else None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Store a value with the configured TTL."""
        self._store[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._store.clear()
