"""
Core bot class with improved structure.
"""

import discord
from discord.ext import commands
from typing import List
from .storage import StorageManager


class MainBot(commands.Bot):
    """Main bot class with dependency injection."""
    
    def __init__(self, guild_ids: List[int], storage: StorageManager):
        intents = discord.Intents.default()
        intents.reactions = True
        intents.members = True
        intents.message_content = True
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.guild_ids = guild_ids
        self.storage = storage

    async def setup_hook(self):
        """Load cogs and sync commands."""
        # Load all cogs
        await self.load_extension("cogs.roles_board")
        await self.load_extension("cogs.modpack")
        await self.load_extension("cogs.autosend")
        await self.load_extension("cogs.amp")
        
        # Sync commands to guilds
        for guild_id in self.guild_ids:
            guild_obj = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)

    async def on_ready(self):
        """Called when bot is ready."""
        print(f"🤖 Logged in as {self.user}")
        print(f"📊 Serving {len(self.guilds)} guilds")
