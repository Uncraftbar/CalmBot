import discord
from discord.ext import commands
from discord import app_commands
import os
from cogs.utils import load_json, save_json
import importlib.util


config_file = "config.py"

spec = importlib.util.spec_from_file_location("config", config_file)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
GUILD_IDS = config.GUILD_IDS
BOT_TOKEN = config.BOT_TOKEN

class MainBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.reactions = True
        intents.members = True
        intents.message_content = True  # Enable message content intent for keyword triggers
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.roles_board")
        await self.load_extension("cogs.modpack")
        await self.load_extension("cogs.autosend")
        await self.load_extension("cogs.amp")
        for guild_id in GUILD_IDS:
            guild_obj = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)

    async def on_ready(self):
        print(f"?? Logged in as {self.user}")

if __name__ == "__main__":
    bot = MainBot()
    bot.run(BOT_TOKEN)
