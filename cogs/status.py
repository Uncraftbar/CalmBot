"""
Status rotator for CalmBot.
Randomly cycles through custom status messages.
"""

import os
import random

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
