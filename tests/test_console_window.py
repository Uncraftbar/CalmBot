"""AMP's 40-line console window and console permission refusals (Nov 2026 bridge outage)."""
import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.chat_bridge import (
    AMP_CONSOLE_WINDOW,
    CONSOLE_DENIED_RETRY_SECONDS,
    CONSOLE_LINE_BUDGET,
    POLL_MIN_SECONDS,
    POLL_PLAYERS_HINT_SECONDS,
    POLL_QUIET_SECONDS,
    ChatBridge,
)

NOW = 1_000_000.0


def make_bridge(**state):
    bridge = ChatBridge.__new__(ChatBridge)
    bridge.failure_counts = {}
    bridge.console_listeners = []
    bridge.last_player_activity = {}
    bridge.amp_player_counts = {}
    bridge.poll_modes = {}
    bridge.console_rates = {}
    bridge._console_overflows = {}
    bridge._last_overflow_log = {}
    bridge.console_denied = {}
    bridge._last_denied_log = {}
    bridge.send_locks = {}
    bridge.send_failed = set()
    bridge._send_health_alert = AsyncMock()
    for name, value in state.items():
        setattr(bridge, name, value)
    return bridge


def interval(bridge, name="Server"):
    with patch("cogs.chat_bridge.log.info"):
        return bridge._poll_interval(name, NOW)


class ConsoleVolumeTests(unittest.TestCase):
    def test_a_busy_console_is_polled_before_amps_window_fills(self):
        bridge = make_bridge(amp_player_counts={"Server": 0}, console_rates={"Server": 2.0})
        self.assertEqual(interval(bridge), CONSOLE_LINE_BUDGET / 2.0)  # not the 30 s quiet cadence
        bridge.console_rates["Server"] = 1000.0
        self.assertEqual(interval(bridge), POLL_MIN_SECONDS)
        bridge.console_rates["Server"] = 0.01  # a near-silent console keeps the mode's cadence
        self.assertEqual(interval(bridge), POLL_QUIET_SECONDS)
        bridge.amp_player_counts["Server"] = 3
        self.assertEqual(interval(bridge), POLL_PLAYERS_HINT_SECONDS)

    def test_rate_follows_polls_but_not_a_sessions_backlog(self):
        bridge = make_bridge()
        bridge._note_console_volume("Server", AMP_CONSOLE_WINDOW, None)  # first batch of a session
        bridge._note_console_volume("Server", 30, 0.5)  # back-to-back command poll
        self.assertNotIn("Server", bridge.console_rates)
        bridge._note_console_volume("Server", 10, 10.0)
        self.assertEqual(bridge.console_rates["Server"], 1.0)
        bridge._note_console_volume("Server", 30, 10.0)
        self.assertAlmostEqual(bridge.console_rates["Server"], 1.5)  # smoothed towards 3 lines/s

    def test_an_overflowing_poll_speeds_up_at_once_and_is_reported_once_an_hour(self):
        bridge = make_bridge(console_rates={"Server": 0.2}, amp_player_counts={"Server": 0})
        with patch("cogs.chat_bridge.log.warning") as warning:
            bridge._note_console_volume("Server", AMP_CONSOLE_WINDOW, 30.0)
            bridge._note_console_volume("Server", AMP_CONSOLE_WINDOW, 30.0)
        self.assertEqual(warning.call_count, 1)
        self.assertAlmostEqual(bridge.console_rates["Server"], 2 * AMP_CONSOLE_WINDOW / 30.0)
        self.assertLess(interval(bridge) * bridge.console_rates["Server"], AMP_CONSOLE_WINDOW / 2)


class FetchUpdateTests(unittest.IsolatedAsyncioTestCase):
    def instance(self, lines, relogin=False):
        sessions = {"inst": SimpleNamespace(id="s1")}
        updates = SimpleNamespace(console_entries=[object()] * lines, status=None)

        async def get_updates(format_data=True):
            if relogin:
                sessions["inst"] = SimpleNamespace(id="s2")
            return updates

        return SimpleNamespace(_bridge=SimpleNamespace(_sessions=sessions), instance_id="inst",
                               get_updates=get_updates)

    async def test_poll_records_console_volume(self):
        bridge = make_bridge()
        name, updates = await bridge._fetch_update_safe("Server", self.instance(12), 6.0)
        self.assertEqual(name, "Server")
        self.assertEqual(bridge.console_rates["Server"], 2.0)

    async def test_a_fresh_login_backlog_is_not_mistaken_for_volume(self):
        bridge = make_bridge()
        with patch("cogs.chat_bridge.log.warning") as warning:
            await bridge._fetch_update_safe("Server", self.instance(AMP_CONSOLE_WINDOW, relogin=True), 6.0)
        self.assertNotIn("Server", bridge.console_rates)
        warning.assert_not_called()


class ConsolePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sleeper = patch("cogs.chat_bridge.asyncio.sleep", new=AsyncMock())
        sleeper.start()
        self.addCleanup(sleeper.stop)

    async def test_a_console_refused_twice_is_reported_and_left_alone_for_a_while(self):
        bridge = make_bridge()
        target = SimpleNamespace(send_console_message=AsyncMock(side_effect=PermissionError("no permission")))
        with patch("cogs.chat_bridge.log.warning") as warning:
            self.assertFalse(await bridge._send_message_safe(target, "tellraw @a []", "ATM9"))
            self.assertFalse(await bridge._send_message_safe(target, "tellraw @a []", "ATM9"))
        self.assertEqual(target.send_console_message.await_count, 2)  # the second message never hit AMP
        self.assertEqual(warning.call_count, 1)
        self.assertIn("console permission", warning.call_args.args[0])
        # After the retry delay AMP is asked again, and success clears the block.
        bridge.console_denied["ATM9"] -= CONSOLE_DENIED_RETRY_SECONDS + 1
        target.send_console_message = AsyncMock()
        self.assertTrue(await bridge._send_message_safe(target, "tellraw @a []", "ATM9"))
        self.assertNotIn("ATM9", bridge.console_denied)

    async def test_one_refusal_then_success_is_an_expired_session_not_a_block(self):
        bridge = make_bridge()
        target = SimpleNamespace(send_console_message=AsyncMock(side_effect=[PermissionError("expired"), None]))
        self.assertTrue(await bridge._send_message_safe(target, "tellraw @a []", "ATM11"))
        self.assertNotIn("ATM11", bridge.console_denied)

    async def test_other_failures_keep_the_plain_warning(self):
        bridge = make_bridge()
        target = SimpleNamespace(send_console_message=AsyncMock(side_effect=ConnectionError("down")))
        with patch("cogs.chat_bridge.log.warning") as warning:
            self.assertFalse(await bridge._send_message_safe(target, "tellraw @a []", "ATM11"))
        self.assertNotIn("ATM11", bridge.console_denied)
        self.assertIn("after 2 attempts", warning.call_args.args[0])


def console(contents, source="Server thread/INFO", kind="Console"):
    return SimpleNamespace(timestamp=datetime.now(timezone.utc), contents=contents, source=source, type=kind)


class UntaggedChatTests(unittest.IsolatedAsyncioTestCase):
    """AMP sometimes leaves modded servers' chat untagged ("Console" type)."""

    async def relay(self, *entries, comp_mode=False):
        bridge = make_bridge(
            bridge_data={"groups": {"G": {"servers": ["ATM11"], "active": True, "channel_id": 5}},
                         "instance_settings": {"ATM11": {"comp_mode": comp_mode}}},
            instances={"ATM11": object()}, instance_running={}, last_console_poll={},
            high_water_marks={}, _untagged_chat_servers=set(),
            bot=SimpleNamespace(get_channel=lambda channel_id: SimpleNamespace(id=channel_id)),
        )
        bridge._instance_refresh_interval = lambda: None
        bridge._active_instances = lambda: {"ATM11": object()}
        bridge._poll_interval = lambda name, now: 0
        bridge._remember_console_entries = lambda *args: None
        bridge._is_minecraft = lambda instance: True
        bridge._fetch_update_safe = AsyncMock(return_value=("ATM11", SimpleNamespace(console_entries=list(entries))))
        bridge._send_discord_message_webhook = AsyncMock()
        bridge._update_channel_topic = AsyncMock()
        bridge._enqueue_send = MagicMock()
        with patch("cogs.chat_bridge.log.info"):
            await bridge._sync_tick()
        await asyncio.sleep(0)
        sent = [(c.args[1], c.args[2]) for c in bridge._send_discord_message_webhook.call_args_list]
        return bridge, sent

    async def test_plain_and_not_secure_chat_is_relayed_and_never_taken_for_a_death(self):
        bridge, sent = await self.relay(console("<Steve> I fell off a cliff"), console("[Not Secure] <Alex> hi all"))
        self.assertEqual(sent, [("Steve", "I fell off a cliff"), ("Alex", "hi all")])
        self.assertIn("ATM11", bridge._untagged_chat_servers)

    async def test_tagged_chat_still_relays(self):
        _, sent = await self.relay(console("hello", source="Steve", kind="Chat"))
        self.assertEqual(sent, [("Steve", "hello")])

    async def test_comp_mode_rank_format(self):
        _, sent = await self.relay(console("<[Member] Bob> hello"), comp_mode=True)
        self.assertEqual(sent, [("Bob", "hello")])

    async def test_console_noise_is_not_chat(self):
        _, sent = await self.relay(
            console("\tat sun.net.www.protocol.https.HttpsClient.<init>(HttpsClient.java:265) ~[?:?] {}", source="Console"),
            console("<Steve> warned about something", source="Server thread/WARN"),
            console("Named entity Witch['Grim Witch'/1, l='ServerLevel[world]']: died: Grim Witch was slain by Steve"),
            console("Twilight Forest: 20.000 TPS (1.789 ms/tick)"),
            console("<[Member] Bob> rank chat without comp mode"),
        )
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
