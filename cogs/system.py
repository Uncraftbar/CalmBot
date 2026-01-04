"""
System commands for CalmBot.
Provides administrative utilities like cog reloading.
"""

import os

import discord
from discord.ext import commands
from discord import app_commands

from cogs.utils import get_logger, admin_only, success_embed, error_embed, info_embed

log = get_logger("system")


class System(commands.Cog):
    """System administration commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("System cog initialized")
    
    @app_commands.command(name="reload", description="Reload a cog or all cogs")
    @app_commands.describe(extension="The cog to reload (e.g. 'autosend') or 'all'")
    @admin_only()
    async def reload(self, interaction: discord.Interaction, extension: str):
        """Reload bot extensions without restarting."""
        await interaction.response.defer(ephemeral=True)
        
        if extension.lower() == "all":
            results = []
            for filename in sorted(os.listdir("./cogs")):
                if not filename.endswith(".py") or filename == "utils.py":
                    continue
                
                cog_name = f"cogs.{filename[:-3]}"
                try:
                    await self.bot.reload_extension(cog_name)
                    results.append(f"✅ {cog_name.split('.')[-1]}")
                except commands.ExtensionNotLoaded:
                    try:
                        await self.bot.load_extension(cog_name)
                        results.append(f"🆕 {cog_name.split('.')[-1]}")
                    except Exception as e:
                        results.append(f"❌ {cog_name.split('.')[-1]}: {e}")
                except Exception as e:
                    results.append(f"❌ {cog_name.split('.')[-1]}: {e}")
            
            success_count = sum(1 for r in results if r.startswith(("✅", "🆕")))
            
            await interaction.followup.send(
                embed=info_embed(
                    "Reload Complete",
                    f"Reloaded {success_count}/{len(results)} cogs:\n" + "\n".join(results)
                )
            )
            log.info(f"Reloaded all cogs: {success_count}/{len(results)} successful")
        else:
            cog_name = f"cogs.{extension}"
            try:
                await self.bot.reload_extension(cog_name)
                await interaction.followup.send(
                    embed=success_embed("Reloaded", f"Successfully reloaded `{cog_name}`")
                )
                log.info(f"Reloaded {cog_name}")
            except commands.ExtensionNotLoaded:
                try:
                    await self.bot.load_extension(cog_name)
                    await interaction.followup.send(
                        embed=success_embed("Loaded", f"Loaded new extension `{cog_name}`")
                    )
                    log.info(f"Loaded new extension {cog_name}")
                except Exception as e:
                    await interaction.followup.send(
                        embed=error_embed("Failed", f"Could not load `{cog_name}`: {e}")
                    )
            except Exception as e:
                await interaction.followup.send(
                    embed=error_embed("Failed", f"Could not reload `{cog_name}`: {e}")
                )
    
    @reload.autocomplete('extension')
    async def reload_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Provide autocomplete for extension names."""
        cogs = ["all"]
        for filename in os.listdir("./cogs"):
            if filename.endswith(".py") and filename != "utils.py":
                cogs.append(filename[:-3])
        
        return [
            app_commands.Choice(name=cog, value=cog)
            for cog in cogs
            if current.lower() in cog.lower()
        ][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(System(bot))
