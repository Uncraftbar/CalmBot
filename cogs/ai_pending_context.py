"""Race-safe, bounded state for messages folded into an active LLM request."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


def message_channel_key(message: Any) -> tuple[int, int] | None:
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    guild_id = getattr(guild, "id", None)
    channel_id = getattr(channel, "id", None)
    if guild_id is None or channel_id is None:
        return None
    return int(guild_id), int(channel_id)


class PendingContext:
    """Additional messages for one in-flight request; the trigger lives separately."""

    def __init__(self, trigger: Any, limit: int):
        key = message_channel_key(trigger)
        if key is None:
            raise ValueError("pending LLM context requires a guild channel")
        self.key = key
        self.trigger_id = int(trigger.id)
        self.limit = max(1, int(limit))
        self._messages: deque[Any] = deque()
        self._ids: set[int] = set()
        self.revision = 0
        self.changed = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, message: Any) -> bool:
        """Append once when channel-scoped, evicting old additions at the hard cap."""
        message_id = int(getattr(message, "id", 0) or 0)
        if (self._closed or not message_id or message_id == self.trigger_id
                or message_id in self._ids or message_channel_key(message) != self.key):
            return False
        if len(self._messages) >= self.limit:
            removed = self._messages.popleft()
            self._ids.discard(int(removed.id))
        self._messages.append(message)
        self._ids.add(message_id)
        self.revision += 1
        self.changed.set()
        return True

    def snapshot(self) -> tuple[list[Any], int, asyncio.Event]:
        """Return a stable ordered snapshot and a fresh change notification handle."""
        self.changed.clear()
        return list(self._messages), self.revision, self.changed

    def close_if_unchanged(self, revision: int) -> bool:
        """Atomically seal this snapshot if no newer message was appended."""
        if self._closed or self.revision != revision:
            return False
        self._closed = True
        return True


# main.py discovers Python files under cogs; this module is import-only.
async def setup(bot):
    return None
