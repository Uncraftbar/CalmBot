"""
Status rotator for CalmBot.
Randomly cycles through custom status messages.
"""

import asyncio
import os
import random
from pathlib import Path

import discord
from discord.ext import commands, tasks

from cogs.utils import get_logger

log = get_logger("status")

INTERVAL_SECONDS = 60
STATUS_FILE = "data/statuses.txt"


class StatusRotator(commands.Cog):
    """Rotates the bot's custom status message."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.statuses: list[str] = []
        self._write_lock = asyncio.Lock()
        self._load_statuses()
        self.status_loop.start()
        log.info(f"Status rotator initialized with {len(self.statuses)} statuses")
    
    def _load_statuses(self):
        """Load status messages from file."""
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    self.statuses = [line.strip() for line in f if line.strip()]
            except Exception as e:
                log.error(f"Failed to load statuses: {e}")
                self.statuses = []
        
        if not self.statuses:
            self.statuses = ["CalmBot • /help"]
            log.warning("No statuses found, using default")
    
    async def add_status(self, status: str) -> tuple[bool, str]:
        """Atomically add a validated, non-duplicate status and activate it live."""
        status = " ".join(str(status or "").split()).strip()
        if not status or len(status) > 128:
            return False, "Status must contain 1-128 characters on one line."
        if any(token in status for token in ("http://", "https://", "<@", "<#")):
            return False, "Statuses cannot contain links or Discord mentions."

        async with self._write_lock:
            self._load_statuses()
            if any(existing.casefold() == status.casefold() for existing in self.statuses):
                return False, "That status already exists."
            path = Path(STATUS_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            new_statuses = [*self.statuses, status]
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text("\n".join(new_statuses) + "\n", encoding="utf-8")
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            self.statuses = new_statuses
        log.info("Added LLM-generated status; %d statuses now loaded", len(self.statuses))
        return True, status

    async def cog_unload(self):
        """Clean up when cog is unloaded."""
        self.status_loop.cancel()
    
    @tasks.loop(seconds=INTERVAL_SECONDS)
    async def status_loop(self):
        """Rotate to a random status."""
        try:
            status = random.choice(self.statuses)
            await self.bot.change_presence(
                activity=discord.CustomActivity(name=status)
            )
        except Exception as e:
            log.error(f"Failed to change status: {e}")
    
    @status_loop.before_loop
    async def before_status_loop(self):
        """Wait for bot to be ready before starting loop."""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusRotator(bot))
