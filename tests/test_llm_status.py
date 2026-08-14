import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cogs import status as status_module
from cogs.llm_tools import LLMToolRuntime, openai_tools, responses_tools


def tool_names(definitions):
    names = set()
    for item in definitions:
        names.add(item.get("name") or item.get("function", {}).get("name"))
    return names


class StatusToolSchemaTests(unittest.TestCase):
    def test_status_writer_is_exposed_only_when_explicitly_requested(self):
        self.assertNotIn("add_status_line", tool_names(openai_tools(True)))
        self.assertNotIn("add_status_line", tool_names(responses_tools(True)))
        self.assertIn(
            "add_status_line",
            tool_names(openai_tools(False, include_status_write=True)),
        )
        self.assertIn(
            "add_status_line",
            tool_names(responses_tools(False, include_status_write=True)),
        )

    def test_runtime_rejects_status_write_outside_status_command(self):
        author = SimpleNamespace(id=42, guild=None)
        message = SimpleNamespace(author=author, guild=None)
        runtime = LLMToolRuntime(SimpleNamespace(), message)
        result = asyncio.run(runtime.execute("add_status_line", {"status": "Nope"}))
        self.assertIn("Denied", result)


class StatusRotatorWriteTests(unittest.TestCase):
    def test_validates_deduplicates_and_persists_atomically(self):
        async def exercise(path):
            rotator = object.__new__(status_module.StatusRotator)
            rotator.statuses = []
            rotator._write_lock = asyncio.Lock()
            rotator._load_statuses()
            self.assertEqual(
                await rotator.add_status("  Watching   context happen. "),
                (True, "Watching context happen."),
            )
            self.assertFalse((await rotator.add_status("watching context happen."))[0])
            self.assertFalse((await rotator.add_status("https://bad.example"))[0])
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "CalmBot • /help\nWatching context happen.\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statuses.txt"
            with patch.object(status_module, "STATUS_FILE", str(path)):
                asyncio.run(exercise(path))


if __name__ == "__main__":
    unittest.main()
