"""Race-safe, bounded state for deferred LLM follow-up batches."""
from __future__ import annotations

from collections import deque
import time
from typing import Any, Iterable


def message_channel_key(message: Any) -> tuple[int, int] | None:
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    guild_id = getattr(guild, "id", None)
    channel_id = getattr(channel, "id", None)
    if guild_id is None or channel_id is None:
        return None
    return int(guild_id), int(channel_id)


class PendingBatch:
    """One bounded queued request whose newest message is the trigger."""

    def __init__(self, trigger: Any, limit: int, *, followup: bool = False,
                 standalone: bool = False, messages: Iterable[Any] = (),
                 settle_seconds: float = 0):
        key = message_channel_key(trigger)
        if key is None:
            raise ValueError("pending LLM batch requires a guild channel")
        self.key = key
        self.limit = max(1, int(limit))
        self.followup = bool(followup)
        # Standalone requests (for example /ask) never open or participate in
        # automatic channel conversations and do not collect later messages.
        self.standalone = bool(standalone)
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.ready_at = time.monotonic() + self.settle_seconds
        # Preserve the object that owns queue UI (such as the clock reaction),
        # even while later messages advance the effective response target.
        self.anchor = trigger
        self._messages: deque[Any] = deque(maxlen=self.limit)
        self._ids: set[int] = set()
        for message in messages:
            self._append(message)
        self._append(trigger)

    def _append(self, message: Any) -> bool:
        message_id = int(getattr(message, "id", 0) or 0)
        if (not message_id or message_id in self._ids
                or message_channel_key(message) != self.key):
            return False
        if len(self._messages) >= self.limit:
            removed = self._messages.popleft()
            self._ids.discard(int(removed.id))
        self._messages.append(message)
        self._ids.add(message_id)
        # Debounce channel chatter: dispatch only after the newest accepted
        # message has had a brief quiet period. This lets rapid multi-user turns
        # become one model request instead of one response per message.
        self.ready_at = time.monotonic() + self.settle_seconds
        return True

    def append(self, message: Any) -> bool:
        """Merge a later message, advancing the effective trigger."""
        return self._append(message)

    @property
    def trigger(self) -> Any:
        return self._messages[-1]

    @property
    def context_messages(self) -> list[Any]:
        return list(self._messages)[:-1]

    # Queue/rate-limit code can treat a batch like its effective trigger.
    @property
    def id(self):
        return self.trigger.id

    @property
    def guild(self):
        return self.trigger.guild

    @property
    def channel(self):
        return self.trigger.channel

    @property
    def author(self):
        return self.trigger.author


class PendingContext:
    """Messages received after an immutable provider request was submitted."""

    def __init__(self, trigger: Any, limit: int, *, settle_seconds: float = 0):
        key = message_channel_key(trigger)
        if key is None:
            raise ValueError("pending LLM context requires a guild channel")
        self.key = key
        self.trigger_id = int(trigger.id)
        self.limit = max(1, int(limit))
        self.settle_seconds = max(0.0, float(settle_seconds))
        self._messages: deque[Any] = deque()
        self._ids: set[int] = set()

    def append(self, message: Any) -> bool:
        """Accumulate once without ever replacing the submitted trigger."""
        message_id = int(getattr(message, "id", 0) or 0)
        if (not message_id or message_id == self.trigger_id or message_id in self._ids
                or message_channel_key(message) != self.key):
            return False
        if len(self._messages) >= self.limit:
            removed = self._messages.popleft()
            self._ids.discard(int(removed.id))
        self._messages.append(message)
        self._ids.add(message_id)
        return True

    def followup(self) -> PendingBatch | None:
        """Bundle all retained later messages, with the latest as trigger."""
        if not self._messages:
            return None
        messages = list(self._messages)
        return PendingBatch(messages[-1], self.limit, followup=True,
                            messages=messages[:-1], settle_seconds=self.settle_seconds)


# main.py discovers Python files under cogs; this module is import-only.
async def setup(bot):
    return None
