import discord
from discord.ext import commands, tasks
import os
import itertools

INTERVAL = 60

class StatusRotator(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.status_file = "statuses.txt"
        self.statuses = []
        self.status_iterator = None
        self.load_statuses()
        self.status_loop.start()

    def load_statuses(self):
        if os.path.exists(self.status_file):
            with open(self.status_file, "r", encoding="utf-8") as f:
                self.statuses = [line.strip() for line in f if line.strip()]
        
        if not self.statuses:
            self.statuses = ["Default Status"]
        
        self.status_iterator = itertools.cycle(self.statuses)

    def cog_unload(self):
        self.status_loop.cancel()

    @tasks.loop(seconds=INTERVAL)
    async def status_loop(self):
        current_status = next(self.status_iterator)
        await self.bot.change_presence(activity=discord.CustomActivity(name=current_status))

    @status_loop.before_loop
    async def before_status_loop(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(StatusRotator(bot))
