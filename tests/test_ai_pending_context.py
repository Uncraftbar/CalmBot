import asyncio
import unittest
from types import SimpleNamespace

from cogs.ai_pending_context import PendingContext


def message(message_id, guild_id=1, channel_id=10):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(id=channel_id),
    )


class PendingContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_is_not_replaced_and_additions_are_ordered(self):
        original = message(1)
        pending = PendingContext(original, limit=3)
        self.assertTrue(pending.append(message(2)))
        self.assertTrue(pending.append(message(3)))
        additions, revision, _ = pending.snapshot()
        self.assertEqual(pending.trigger_id, original.id)
        self.assertEqual([item.id for item in additions], [2, 3])
        self.assertEqual(revision, 2)

    async def test_duplicate_and_cross_channel_messages_are_rejected(self):
        pending = PendingContext(message(1), limit=3)
        self.assertTrue(pending.append(message(2)))
        self.assertFalse(pending.append(message(2)))
        self.assertFalse(pending.append(message(3, channel_id=11)))
        self.assertFalse(pending.append(message(4, guild_id=2)))
        self.assertFalse(pending.append(message(1)))
        additions, revision, _ = pending.snapshot()
        self.assertEqual([item.id for item in additions], [2])
        self.assertEqual(revision, 1)

    async def test_accumulation_is_bounded_and_keeps_newest_context(self):
        pending = PendingContext(message(1), limit=2)
        for message_id in (2, 3, 4):
            self.assertTrue(pending.append(message(message_id)))
        additions, _, _ = pending.snapshot()
        self.assertEqual([item.id for item in additions], [3, 4])
        # An evicted id can safely re-enter as a genuinely later delivery.
        self.assertTrue(pending.append(message(2)))
        additions, _, _ = pending.snapshot()
        self.assertEqual([item.id for item in additions], [4, 2])

    async def test_stale_snapshot_cannot_close_or_lose_a_new_message(self):
        pending = PendingContext(message(1), limit=2)
        _, revision, _ = pending.snapshot()
        self.assertTrue(pending.append(message(2)))
        self.assertFalse(pending.close_if_unchanged(revision))
        additions, current_revision, _ = pending.snapshot()
        self.assertEqual([item.id for item in additions], [2])
        self.assertTrue(pending.close_if_unchanged(current_revision))
        self.assertFalse(pending.append(message(3)))

    async def test_change_notification_has_no_snapshot_race(self):
        pending = PendingContext(message(1), limit=2)
        _, revision, changed = pending.snapshot()
        waiter = asyncio.create_task(changed.wait())
        self.assertTrue(pending.append(message(2)))
        await asyncio.wait_for(waiter, timeout=0.2)
        self.assertGreater(pending.revision, revision)


if __name__ == "__main__":
    unittest.main()
