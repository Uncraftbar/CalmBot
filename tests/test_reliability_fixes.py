import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from cogs.utils import setup_logging

class ReliabilityFixTests(unittest.TestCase):
    def test_calmbot_child_loggers_do_not_propagate_duplicates(self):
        self.assertFalse(setup_logging("calmbot.test_reliability").propagate)

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
