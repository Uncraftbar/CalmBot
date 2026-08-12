import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from cogs.ai_chat import AIChat


class RoleWhitelistTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_whitelist_allows_everyone(self):
        cog = object.__new__(AIChat)
        cog.settings = {"allowed_role_ids": []}
        self.assertTrue(cog._role_allowed(SimpleNamespace(roles=[])))

    def test_matching_role_is_allowed(self):
        cog = object.__new__(AIChat)
        cog.settings = {"allowed_role_ids": [20, 30]}
        member = SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=30)])
        self.assertTrue(cog._role_allowed(member))

    def test_nonmatching_or_missing_roles_are_denied(self):
        cog = object.__new__(AIChat)
        cog.settings = {"allowed_role_ids": [20]}
        self.assertFalse(cog._role_allowed(SimpleNamespace(roles=[SimpleNamespace(id=10)])))
        self.assertFalse(cog._role_allowed(SimpleNamespace()))

    def test_game_bridge_messages_use_owned_webhook_boundary(self):
        bridge = SimpleNamespace(is_game_bridge_message=lambda message: message.webhook_id == 42)
        cog = object.__new__(AIChat)
        cog.bot = SimpleNamespace(get_cog=lambda name: bridge if name == "ChatBridge" else None)
        self.assertTrue(cog._is_game_bridge_message(SimpleNamespace(webhook_id=42)))
        self.assertFalse(cog._is_game_bridge_message(SimpleNamespace(webhook_id=7)))

    async def test_owned_game_bridge_can_trigger_with_discord_role_whitelist(self):
        cog = object.__new__(AIChat)
        cog.settings = {"enabled": True, "allowed_role_ids": [20], "followup_seconds": 0}
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=1))
        cog._conversations = {}
        cog._is_game_bridge_message = lambda message: True
        cog._direct_trigger = AsyncMock(return_value=True)
        message = SimpleNamespace(
            author=SimpleNamespace(bot=True), guild=SimpleNamespace(id=2),
            channel=SimpleNamespace(id=3))
        self.assertTrue(await cog._triggered(message))


if __name__ == "__main__":
    unittest.main()
