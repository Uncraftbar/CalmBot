"""
Emergency shutdown cog for CalmBot.
"""

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import get_logger, admin_only, warning_embed

log = get_logger("nuke")


class Nuke(commands.Cog):
    """Emergency shutdown command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("Nuke cog initialized")

    @app_commands.command(name="nuke", description="IMMEDIATELY shuts down the bot")
    @admin_only()
    async def nuke(self, interaction: discord.Interaction):
        """Immediately stops the bot process."""
        log.warning(f"Nuke command initiated by {interaction.user} (ID: {interaction.user.id})")
        
        # Send confirmation before dying
        await interaction.response.send_message(
            embed=warning_embed(
                "SYSTEM SHUTDOWN",
                "Initiating emergency shutdown protocol. Goodbye!"
            ),
            ephemeral=True
        )
        
        # Close the bot connection and exit
        await self.bot.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(Nuke(bot))
