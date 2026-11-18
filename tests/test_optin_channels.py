import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs.roles_board import OptInSetupError, RolesBoard


class Obj:
    """Hashable stand-in for Discord objects (usable as overwrite keys)."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def http_error(status=400, text="Unknown Emoji"):
    return discord.HTTPException(MagicMock(status=status, reason="Bad Request"), text)


class OptInSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {"channel_id": 10, "message_id": 20,
                      "roles": [{"name": "ATM10 Updates", "emoji": "🔥", "role_id": 1}]}
        self.board = Obj(add_reaction=AsyncMock(), remove_reaction=AsyncMock(),
                         jump_url="https://discord.com/channels/1/10/20")
        board_channel = Obj(fetch_message=AsyncMock(return_value=self.board))
        self.cog = RolesBoard.__new__(RolesBoard)
        self.cog.bot = SimpleNamespace(user=Obj(id=999), get_channel=lambda channel_id: board_channel)
        self.cog.update_roles_board = AsyncMock(return_value=True)

        self.role = Obj(id=500, name="Elden Ring Spoilers", mention="<@&500>", delete=AsyncMock())
        self.category = Obj(id=600, name="Elden Ring Spoilers", delete=AsyncMock())
        self.text = Obj(id=700, name="elden-ring-spoilers", mention="<#700>", delete=AsyncMock())
        self.voice = Obj(id=800, name="Elden Ring Spoilers", mention="<#800>", delete=AsyncMock())
        self.guild = Obj(
            default_role=Obj(name="@everyone"), me=Obj(name="CalmBot"),
            roles=[Obj(name="ATM10 Updates")], categories=[],
            create_role=AsyncMock(return_value=self.role),
            create_category=AsyncMock(return_value=self.category),
            create_text_channel=AsyncMock(return_value=self.text),
            create_voice_channel=AsyncMock(return_value=self.voice),
        )
        self.saved = []
        patches = [
            patch("cogs.roles_board.load_json", side_effect=lambda *a, **k: copy.deepcopy(self.store)),
            patch("cogs.roles_board.save_json", side_effect=self._save),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _save(self, filename, data):
        self.saved.append(copy.deepcopy(data))
        return True

    async def _create(self, name="Elden Ring Spoilers", emoji="🙈", category=None):
        return await self.cog.create_optin_channels(self.guild, name, emoji, category, actor="Admin")

    def _assert_nothing_created(self):
        self.guild.create_role.assert_not_awaited()
        self.guild.create_category.assert_not_awaited()
        self.guild.create_text_channel.assert_not_awaited()
        self.assertEqual(self.saved, [])

    async def test_creates_private_channels_role_and_board_entry(self):
        summary = await self._create()
        self.guild.create_role.assert_awaited_once()
        self.assertEqual(self.guild.create_role.await_args.kwargs["name"], "Elden Ring Spoilers")
        self.assertFalse(self.guild.create_role.await_args.kwargs["mentionable"])
        overwrites = self.guild.create_text_channel.await_args.kwargs["overwrites"]
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        self.assertTrue(overwrites[self.role].view_channel)
        voice_overwrites = self.guild.create_voice_channel.await_args.kwargs["overwrites"]
        self.assertTrue(voice_overwrites[self.role].connect)
        self.assertFalse(voice_overwrites[self.guild.default_role].view_channel)
        self.guild.create_category.assert_awaited_once()
        self.assertIs(self.guild.create_text_channel.await_args.kwargs["category"], self.category)
        self.assertEqual(self.saved[-1]["roles"][-1], {"name": "Elden Ring Spoilers", "emoji": "🙈", "role_id": 500})
        self.board.add_reaction.assert_awaited_once_with("🙈")
        self.cog.update_roles_board.assert_awaited_once()
        self.assertIn("<#700>", summary)
        self.assertIn("<@&500>", summary)

    async def test_uses_an_existing_category_when_given(self):
        existing = Obj(id=601, name="Spoilers")
        summary = await self._create(category=existing)
        self.guild.create_category.assert_not_awaited()
        self.assertIs(self.guild.create_voice_channel.await_args.kwargs["category"], existing)
        self.assertIn("**Spoilers**", summary)

    async def test_refuses_an_emoji_already_on_the_board(self):
        with self.assertRaisesRegex(OptInSetupError, "already used"):
            await self._create(emoji="🔥")
        self.board.add_reaction.assert_not_awaited()
        self._assert_nothing_created()

    async def test_refuses_existing_or_staff_role_names(self):
        for name in ("ATM10 Updates", "Admin"):
            with self.assertRaises(OptInSetupError):
                await self._create(name=name)
        self._assert_nothing_created()

    async def test_refuses_without_a_roles_board(self):
        self.store = {"channel_id": None, "message_id": None, "roles": []}
        with self.assertRaisesRegex(OptInSetupError, "setup_roles_board"):
            await self._create()
        self._assert_nothing_created()

    async def test_refuses_an_unusable_emoji_before_creating_anything(self):
        self.board.add_reaction.side_effect = http_error()
        with self.assertRaisesRegex(OptInSetupError, "not an emoji"):
            await self._create(emoji="notanemoji")
        self._assert_nothing_created()

    async def test_failure_part_way_rolls_everything_back(self):
        self.guild.create_voice_channel.side_effect = http_error(500, "boom")
        with patch("cogs.roles_board.log.exception"), self.assertRaisesRegex(OptInSetupError, "rolled back"):
            await self._create()
        for item in (self.text, self.category, self.role):
            item.delete.assert_awaited_once()
        self.board.remove_reaction.assert_awaited_once_with("🙈", self.cog.bot.user)
        self.assertEqual(self.saved, [])
        self.cog.update_roles_board.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
