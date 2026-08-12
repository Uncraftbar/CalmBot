import unittest
from types import SimpleNamespace

from cogs.ai_pending_context import PendingBatch, PendingContext


def message(message_id, guild_id=1, channel_id=10, author_id=None):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id or message_id),
    )


class PendingContextTests(unittest.TestCase):
    def test_inflight_messages_never_replace_original(self):
        pending = PendingContext(message(1), limit=3)
        self.assertTrue(pending.append(message(2)))
        self.assertTrue(pending.append(message(3)))
        self.assertEqual(pending.trigger_id, 1)
        followup = pending.followup()
        self.assertEqual(followup.trigger.id, 3)
        self.assertEqual([item.id for item in followup.context_messages], [2])

    def test_duplicate_and_cross_channel_messages_are_rejected(self):
        pending = PendingContext(message(1), limit=3)
        self.assertTrue(pending.append(message(2)))
        self.assertFalse(pending.append(message(2)))
        self.assertFalse(pending.append(message(3, channel_id=11)))
        self.assertFalse(pending.append(message(4, guild_id=2)))
        self.assertFalse(pending.append(message(1)))

    def test_accumulation_is_bounded_and_keeps_latest_trigger(self):
        pending = PendingContext(message(1), limit=3)
        for message_id in (2, 3, 4, 5):
            self.assertTrue(pending.append(message(message_id)))
        followup = pending.followup()
        self.assertEqual(followup.trigger.id, 5)
        self.assertEqual([item.id for item in followup.context_messages], [3, 4])

    def test_messages_before_followup_start_merge_into_same_batch(self):
        pending = PendingContext(message(1), limit=4)
        pending.append(message(2))
        pending.append(message(3))
        batch = pending.followup()
        self.assertTrue(batch.followup)
        self.assertTrue(batch.append(message(4)))
        self.assertTrue(batch.append(message(5)))
        self.assertEqual(batch.trigger.id, 5)
        self.assertEqual([item.id for item in batch.context_messages], [2, 3, 4])

    def test_queued_batch_is_bounded_scoped_and_delegates_trigger(self):
        batch = PendingBatch(message(2), limit=3, followup=True)
        for message_id in (3, 4, 5):
            self.assertTrue(batch.append(message(message_id)))
        self.assertEqual(batch.id, 5)
        self.assertEqual(batch.author.id, 5)
        self.assertEqual(batch.anchor.id, 2)
        self.assertEqual([item.id for item in batch.context_messages], [3, 4])
        self.assertFalse(batch.append(message(6, channel_id=99)))
        self.assertFalse(batch.append(message(5)))


if __name__ == "__main__":
    unittest.main()
