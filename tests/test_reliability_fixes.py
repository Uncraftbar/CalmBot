import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from cogs.utils import setup_logging
from cogs.chat_bridge import ChatBridge

class ReliabilityFixTests(unittest.TestCase):
    def test_calmbot_child_loggers_do_not_propagate_duplicates(self):
        self.assertFalse(setup_logging("calmbot.test_reliability").propagate)
    def test_amp_queue_drop_warning_is_coalesced(self):
        bridge = ChatBridge.__new__(ChatBridge)
        bridge.ws_entry_queues = {"Busy": asyncio.Queue(maxsize=1)}
        bridge.ws_drop_counts = {}; bridge.ws_drop_last_log = {}
        bridge.ws_entry_queues["Busy"].put_nowait(SimpleNamespace(contents="old"))
        event = {"Timestamp": "2026-08-23T00:00:00Z", "Contents": "new"}
        with patch("cogs.chat_bridge.log.warning") as warning:
            for _ in range(20): bridge._queue_ws_console_entry("Busy", event)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(bridge.ws_entry_queues["Busy"].qsize(), 1)

class OpenAIRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_connection_failure_retries(self):
        from cogs.ai_chat import AIChat
        cog = AIChat.__new__(AIChat)
        response = AsyncMock(); response.status = 200; response.headers = {}
        response.json = AsyncMock(return_value={"choices": []})
        context = AsyncMock()
        context.__aenter__.side_effect = [__import__("aiohttp").ClientConnectionError("temporary"), response]
        session = SimpleNamespace(post=lambda *a, **k: context)
        cog._http = AsyncMock(return_value=session); cog._load_openai_key = lambda: "hidden"
        with patch("cogs.ai_chat.asyncio.sleep", new=AsyncMock()):
            result = await cog._openai_post("https://example.invalid", {"model": "x"})
        self.assertEqual(result, {"choices": []})
        self.assertEqual(context.__aenter__.call_count, 2)
