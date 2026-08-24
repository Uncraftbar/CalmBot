import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.llm_tools import LLMToolRuntime


def runtime():
    guild = SimpleNamespace(owner_id=999)
    author = SimpleNamespace(id=1, guild=guild, guild_permissions=SimpleNamespace(administrator=False))
    return LLMToolRuntime(SimpleNamespace(), SimpleNamespace(author=author, guild=guild))


class AMPToolReliabilityTests(unittest.TestCase):
    def test_discovery_failure_is_not_verified_healthy(self):
        async def exercise():
            tool = runtime()
            with patch.object(tool, "_public_instances", AsyncMock(side_effect=asyncio.TimeoutError())):
                status = await tool._server_status()
                self.assertFalse(status["verified"])
                diagnostic = await tool._connection_diagnostic({})
                self.assertFalse(diagnostic["verified"])
                self.assertFalse(diagnostic["healthy"])
        asyncio.run(exercise())

    def test_status_failure_is_explicit(self):
        async def exercise():
            instance = SimpleNamespace(instance_name="Broken", friendly_name="Broken", application_endpoints=[])
            instance.get_instance_status = AsyncMock(side_effect=RuntimeError("secret details"))
            tool = runtime()
            with patch.object(tool, "_public_instances", AsyncMock(return_value=[instance])):
                status = await tool._server_status()
            item = status["servers"][0]
            self.assertTrue(status["verified"])
            self.assertFalse(item["amp_instance_reachable"])
            self.assertIn("RuntimeError", item["error"])
            self.assertNotIn("secret details", item["error"])
        asyncio.run(exercise())

    def test_running_minecraft_requires_protocol_probe_for_healthy(self):
        async def exercise():
            tool = runtime()
            status = {"verified": True, "servers": [{"server": "Pack", "game": "Minecraft", "state": "Running", "amp_instance_reachable": True, "endpoint": {"address": "0.0.0.0:25565"}}]}
            with patch.object(tool, "_server_status", AsyncMock(return_value=status)), patch.object(tool, "_probe_minecraft", AsyncMock(return_value={"supported": True, "reachable": False, "error": "failed"})):
                result = await tool._connection_diagnostic({"server": "Pack"})
            self.assertTrue(result["verified"])
            self.assertFalse(result["healthy"])
            self.assertFalse(result["servers"][0]["healthy"])
        asyncio.run(exercise())


if __name__ == "__main__": unittest.main()
