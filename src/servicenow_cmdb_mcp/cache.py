"""In-memory metadata cache with TTL and async stampede protection."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class MetadataCache:
    """In-memory cache with per-key TTL for Data Model Navigator metadata.

    Stores class hierarchies, field definitions, and relationship types fetched
    from ServiceNow. Default TTL is 1 hour, configurable at init.

    Provides `get_or_fetch` for coroutine-safe cache population — only one
    coroutine will fetch while others wait, preventing thundering-herd on
    cold or expired keys.
    """

    def __init__(self, ttl: int = 3600) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        self._locks.clear()

    async def get_or_fetch(self, key: str, fetch_fn: Any) -> Any:
        """Return cached value, or call fetch_fn() exactly once to populate.

        Uses a per-key asyncio.Lock so concurrent callers for the same key
        coalesce into a single fetch instead of stampeding ServiceNow.

        Args:
            key: Cache key.
            fetch_fn: Async callable returning the value to cache.

        Returns:
            The cached or freshly fetched value.
        """
        # Fast path — no lock needed if value is cached and valid
        cached = self.get(key)
        if cached is not None:
            return cached

        # Slow path — acquire per-key lock to prevent stampede
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        lock = self._locks[key]

        async with lock:
            # Double-check after acquiring lock
            cached = self.get(key)
            if cached is not None:
                return cached

            value = await fetch_fn()
            self.set(key, value)
            return value
