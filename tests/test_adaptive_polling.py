import unittest
from types import SimpleNamespace
from unittest.mock import patch

import amp_http
from cogs.chat_bridge import (
    ChatBridge,
    POLL_ACTIVE_SECONDS,
    POLL_COMMAND_SECONDS,
    POLL_MAX_BACKOFF_SECONDS,
    POLL_PLAYERS_HINT_SECONDS,
    POLL_QUIET_SECONDS,
)

NOW = 1_000_000.0


def make_bridge(**state):
    bridge = ChatBridge.__new__(ChatBridge)
    bridge.failure_counts = {}
    bridge.console_listeners = []
    bridge.last_player_activity = {}
    bridge.amp_player_counts = {}
    bridge.poll_modes = {}
    for name, value in state.items():
        setattr(bridge, name, value)
    return bridge


def interval(bridge, name="Server"):
    with patch("cogs.chat_bridge.log.info"):
        return bridge._poll_interval(name, NOW)


class AdaptivePollingTests(unittest.TestCase):
    def test_quiet_server_is_still_polled_slowly(self):
        bridge = make_bridge(amp_player_counts={"Server": 0})
        self.assertEqual(interval(bridge), POLL_QUIET_SECONDS)

    def test_amp_player_count_only_earns_the_middle_cadence(self):
        for reported in (2, None):
            bridge = make_bridge(amp_player_counts={"Server": reported})
            self.assertEqual(interval(bridge), POLL_PLAYERS_HINT_SECONDS)

    def test_console_activity_wins_even_when_amp_reports_nobody(self):
        bridge = make_bridge(amp_player_counts={"Server": 0}, last_player_activity={"Server": NOW - 60})
        self.assertEqual(interval(bridge), POLL_ACTIVE_SECONDS)
        bridge.last_player_activity["Server"] = NOW - 3600
        self.assertEqual(interval(bridge), POLL_QUIET_SECONDS)

    def test_waiting_command_polls_fastest(self):
        bridge = make_bridge(amp_player_counts={"Server": 0}, console_listeners=[{"source": "Server"}])
        self.assertEqual(interval(bridge), POLL_COMMAND_SECONDS)

    def test_failing_server_backs_off_to_a_ceiling(self):
        bridge = make_bridge(last_player_activity={"Server": NOW}, failure_counts={"Server": 2})
        self.assertEqual(interval(bridge), 2 * POLL_ACTIVE_SECONDS)
        bridge.failure_counts["Server"] = 50
        self.assertEqual(interval(bridge), POLL_MAX_BACKOFF_SECONDS)

    def test_mode_changes_are_logged_once(self):
        bridge = make_bridge(amp_player_counts={"Server": 0})
        with patch("cogs.chat_bridge.log.info") as info:
            for _ in range(5):
                bridge._poll_interval("Server", NOW)
        self.assertEqual(info.call_count, 1)


class AmpHttpTests(unittest.IsolatedAsyncioTestCase):
    def test_api_name_strips_the_instance_proxy_prefix(self):
        self.assertEqual(amp_http._api_name("https://amp.example/API/ADSModule/Servers/abc/API/Core/GetUpdates"),
                         "Core/GetUpdates")
        self.assertEqual(amp_http._api_name("https://amp.example/API/ADSModule/GetInstances"),
                         "ADSModule/GetInstances")

    async def test_pooled_context_releases_responses_and_keeps_the_pool(self):
        released = []
        response = SimpleNamespace(release=lambda: released.append(True))

        class FakeSession:
            async def post(self, url, **kwargs):
                return response

        with patch.object(amp_http, "_shared_session", return_value=FakeSession()):
            async with amp_http._PooledSessionContext() as session:
                self.assertIs(await session.post(url="https://amp.example/API/Core/GetUpdates"), response)
        self.assertEqual(released, [True])

    async def test_successful_calls_slide_the_session_expiry(self):
        session = SimpleNamespace(id="abc", ttl=None)
        api = SimpleNamespace(_bridge=SimpleNamespace(_sessions={"inst": session}), instance_id="inst")

        async def call_api(self, api, **kwargs):
            return "ok"

        wrapped = amp_http._with_sliding_session_expiry(call_api)
        self.assertEqual(await wrapped(api, api="Core/GetUpdates"), "ok")
        self.assertIsNotNone(session.ttl)
        session.ttl = None
        await wrapped(api, api="Core/Login")
        self.assertIsNone(session.ttl)


if __name__ == "__main__":
    unittest.main()
