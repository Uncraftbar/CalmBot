import asyncio
import json
import os
import stat
import tempfile
import unittest
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs import utils
from cogs.chat_bridge import (
    ChatBridge,
    GAME_CHAT_MENTIONS,
    INSTANCE_MISSING_RECHECK_SECONDS,
    INSTANCE_RECHECK_SECONDS,
    INSTANCE_RETRY_SECONDS,
    RELAY_ECHO_RE,
)


def fake_member(*, owner=False, administrator=False, role_names=()):
    no_perms = SimpleNamespace(manage_guild=False, manage_channels=False)
    return SimpleNamespace(
        id=1,
        guild=SimpleNamespace(owner_id=1 if owner else 2),
        guild_permissions=SimpleNamespace(administrator=administrator),
        roles=[SimpleNamespace(name=name, permissions=no_perms) for name in role_names],
    )


class PermissionTests(unittest.TestCase):
    def test_administrator_check_is_stricter_than_the_mod_check(self):
        self.assertTrue(utils.is_administrator(fake_member(owner=True)))
        self.assertTrue(utils.is_administrator(fake_member(administrator=True)))
        mod = fake_member(role_names=("Staff",))
        self.assertTrue(utils.has_mod_permissions(mod))
        self.assertFalse(utils.is_administrator(mod))

    def test_staff_roles_are_never_self_assignable(self):
        self.assertFalse(utils.is_elevated_role(SimpleNamespace(name="ATM10 Updates", permissions=discord.Permissions.none())))
        self.assertTrue(utils.is_elevated_role(SimpleNamespace(name="Admin", permissions=discord.Permissions.none())))
        for permission in ("administrator", "manage_roles", "manage_channels", "ban_members", "mention_everyone"):
            role = SimpleNamespace(name="Fun", permissions=discord.Permissions(**{permission: True}))
            self.assertTrue(utils.is_elevated_role(role), permission)


class JsonPersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.path = os.path.join(self.dir, "state.json")

    def test_unserializable_data_leaves_the_old_file_intact(self):
        self.assertTrue(utils.save_json(self.path, {"keep": 1}))
        with patch.object(utils._json_log, "error"):
            self.assertFalse(utils.save_json(self.path, {"bad": {1, 2}}))
        self.assertEqual(utils.load_json(self.path), {"keep": 1})
        self.assertEqual([name for name in os.listdir(self.dir) if name.endswith(".tmp")], [])

    def test_save_keeps_the_existing_file_mode(self):
        utils.save_json(self.path, {})
        os.chmod(self.path, 0o644)
        utils.save_json(self.path, {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)

    def test_corrupt_file_is_moved_aside_instead_of_overwritten(self):
        with open(self.path, "w") as handle:
            handle.write('{"half": ')
        with patch.object(utils._json_log, "error"):
            self.assertEqual(utils.load_json(self.path, {"default": True}), {"default": True})
        self.assertFalse(os.path.exists(self.path))
        moved = [name for name in os.listdir(self.dir) if name.startswith("state.corrupt-") and name.endswith(".json")]
        self.assertEqual(len(moved), 1)


def make_bridge(**state):
    bridge = ChatBridge.__new__(ChatBridge)
    bridge.instances = {}
    bridge.instance_running = {}
    bridge.failure_counts = {}
    bridge.bridge_data = {"groups": {"G": {"servers": ["A", "B"], "active": True}}}
    for name, value in state.items():
        setattr(bridge, name, value)
    return bridge


class BridgeRecoveryTests(unittest.TestCase):
    def test_instance_relist_cadence_by_state(self):
        both = {"A": object(), "B": object()}
        self.assertEqual(make_bridge()._instance_refresh_interval(), INSTANCE_RETRY_SECONDS)
        self.assertIsNone(make_bridge(instances=both)._instance_refresh_interval())
        self.assertEqual(make_bridge(instances={"A": object()})._instance_refresh_interval(),
                         INSTANCE_MISSING_RECHECK_SECONDS)
        self.assertEqual(make_bridge(instances=both, instance_running={"B": False})._instance_refresh_interval(),
                         INSTANCE_RECHECK_SECONDS)
        self.assertEqual(make_bridge(instances=both, failure_counts={"A": 3})._instance_refresh_interval(),
                         INSTANCE_RECHECK_SECONDS)

    def test_relay_echoes_are_skipped_but_bracketed_chat_is_kept(self):
        self.assertIsNotNone(RELAY_ECHO_RE.match("[ATM10] <Steve> hi"))
        self.assertIsNone(RELAY_ECHO_RE.match("[AFK] brb"))
        self.assertIsNone(RELAY_ECHO_RE.match("[1.21] anyone know a good mod?"))


class BridgeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_tick_does_not_stop_the_loop(self):
        bridge = make_bridge()
        bridge._sync_tick = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("cogs.chat_bridge.log.exception") as logged:
            await ChatBridge.sync_loop.coro(bridge)
            await ChatBridge.sync_loop.coro(bridge)
        self.assertEqual(bridge._sync_tick.await_count, 2)
        self.assertEqual(logged.call_count, 1)  # repeated failures are logged at most once a minute

    async def test_bridge_relists_instances_when_amp_was_unreachable_at_startup(self):
        bridge = make_bridge(last_instance_refresh=datetime.min.replace(tzinfo=timezone.utc),
                             ws_tasks={}, ws_connected=set())
        bridge._refresh_instances = AsyncMock()
        await bridge._sync_tick()
        bridge._refresh_instances.assert_awaited_once()

    async def test_deleted_webhook_is_forgotten_and_fallback_cannot_ping_everyone(self):
        dead = SimpleNamespace(id=7, send=AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Unknown Webhook")))
        bridge = make_bridge(webhook_cache={42: dead}, bridge_webhook_ids=set())
        bridge._get_or_create_webhook = AsyncMock(return_value=dead)
        channel = SimpleNamespace(id=42, send=AsyncMock())
        await bridge._send_discord_message_webhook(channel, "Steve", "@everyone hi", "ATM10")
        self.assertNotIn(42, bridge.webhook_cache)
        sent = channel.send.await_args.kwargs["allowed_mentions"]
        self.assertIs(sent, GAME_CHAT_MENTIONS)
        self.assertFalse(sent.everyone)
        self.assertFalse(sent.roles)


class AmpActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_dashboard_action_reports_success(self):
        from cogs.amp import ConfirmServerActionModal
        from cogs.moderator_actions import ActionResult

        modal = ConfirmServerActionModal(SimpleNamespace(friendly_name="ATM10", instance_name="ATM10"), "restart")
        gateway = SimpleNamespace(execute_confirmed_amp_action=AsyncMock(
            return_value=ActionResult(success=True, title="Restarted", message="ATM10 is restarting")))
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda name: gateway),
        )
        with patch("cogs.amp.check_permissions", AsyncMock(return_value=True)):
            await modal.on_submit(interaction)
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertEqual(embed.colour, utils.success_embed("x").colour)


class LLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_error_text_extracts_the_message(self):
        from cogs.ai_chat import provider_error_text

        def response(body: bytes):
            return SimpleNamespace(content=SimpleNamespace(read=AsyncMock(return_value=body)))

        auth = json.dumps({"error": {"message": "Authentication Fails, Your api key: ****1b9c is invalid"}}).encode()
        self.assertEqual(await provider_error_text(response(auth)),
                         "Authentication Fails, Your api key: ****1b9c is invalid")
        truncated = b'{"error":{"code":"invalid_prompt","message":"Invalid Responses API request"},"metadata":{"raw":"[\\n {'
        self.assertEqual(await provider_error_text(response(truncated)), "Invalid Responses API request")
        self.assertEqual(await provider_error_text(SimpleNamespace()), "")

    def test_responses_api_urls_are_recognised(self):
        from cogs.ai_chat import is_responses_api_url
        self.assertTrue(is_responses_api_url("https://openrouter.ai/api/v1/responses"))
        self.assertFalse(is_responses_api_url("https://openrouter.ai/api/v1/chat/completions"))
        self.assertFalse(is_responses_api_url("https://api.deepseek.com/v1"))

    def _cog(self, side_effect):
        from cogs.ai_chat import AIChat
        cog = AIChat.__new__(AIChat)
        cog.settings = {"fallback_model": "backup"}
        cog._provider = lambda: "openai"
        cog._model = lambda: "primary"
        cog._openai = AsyncMock(side_effect=side_effect)
        return cog

    async def test_auth_failures_skip_the_fallback_model(self):
        from cogs.ai_chat import LLMHTTPError
        cog = self._cog(LLMHTTPError("LLM endpoint returned HTTP 401: invalid key", 401))
        with self.assertRaises(LLMHTTPError):
            await cog._generate([], "system", None)
        self.assertEqual(cog._openai.await_count, 1)

    async def test_other_failures_still_try_the_fallback_model(self):
        from cogs.ai_chat import LLMHTTPError
        cog = self._cog([LLMHTTPError("LLM endpoint returned HTTP 400: unknown model", 400), "fallback answer"])
        with patch("cogs.ai_chat.log.warning"):
            self.assertEqual(await cog._generate([], "system", None), "fallback answer")
        self.assertEqual(cog._openai.await_args.args[-1], "backup")

    async def test_cancel_clears_the_queue_ui_on_the_batch_anchor(self):
        from cogs.ai_chat import AIChat
        cog = AIChat.__new__(AIChat)
        cog._lock = asyncio.Lock()
        cog._queue_wake = asyncio.Event()
        cog._clocked_requests = {111}
        cog.bot = SimpleNamespace(user=object())
        anchor = SimpleNamespace(id=111, remove_reaction=AsyncMock())
        item = SimpleNamespace(anchor=anchor, id=222, guild=SimpleNamespace(id=1),
                               channel=SimpleNamespace(id=2), author=SimpleNamespace(id=3))
        cog._pending = deque([item])
        interaction = SimpleNamespace(guild_id=1, channel_id=2, user=SimpleNamespace(id=3),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        await AIChat.cancel.callback(cog, interaction)
        anchor.remove_reaction.assert_awaited_once()
        self.assertEqual(cog._clocked_requests, set())
        self.assertEqual(len(cog._pending), 0)
        self.assertIn("cancelled", interaction.response.send_message.await_args.args[0])


class AutosendLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_hourly_send_does_not_stop_the_loop(self):
        from cogs.autosend import AutoSend
        cog = AutoSend.__new__(AutoSend)
        cog._process_time_triggers = AsyncMock(side_effect=discord.DiscordServerError(MagicMock(status=503), "unavailable"))
        top_of_hour = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
        with patch("discord.utils.utcnow", return_value=top_of_hour), patch("cogs.autosend.log.exception") as logged:
            await AutoSend.time_loop.coro(cog)
        cog._process_time_triggers.assert_awaited_once_with("hourly")
        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
