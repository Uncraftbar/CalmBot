import discord
from discord.ext import commands
from discord import app_commands
import os
from cogs.utils import has_admin_or_mod_permissions

class System(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="reload", description="Reload a specific cog or all cogs")
    @app_commands.describe(extension="The extension to reload (e.g. 'autosend') or 'all'")
    async def reload(self, interaction: discord.Interaction, extension: str):
        if not await has_admin_or_mod_permissions(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        if extension.lower() == "all":
            msg = []
            for filename in os.listdir("./cogs"):
                if filename.endswith(".py") and filename != "utils.py":
                    cog_name = f"cogs.{filename[:-3]}"
                    try:
                        await self.bot.reload_extension(cog_name)
                        msg.append(f"✅ Reloaded `{cog_name}`")
                    except commands.ExtensionNotLoaded:
                        try:
                            await self.bot.load_extension(cog_name)
                            msg.append(f"🆕 Loaded `{cog_name}`")
                        except Exception as e:
                            msg.append(f"❌ Failed to load `{cog_name}`: {e}")
                    except Exception as e:
                        msg.append(f"❌ Failed `{cog_name}`: {e}")
            await interaction.followup.send("\n".join(msg) or "No cogs found.")
        else:
            cog_name = f"cogs.{extension}"
            try:
                await self.bot.reload_extension(cog_name)
                await interaction.followup.send(f"✅ Successfully reloaded `{cog_name}`")
            except commands.ExtensionNotLoaded:
                try:
                    await self.bot.load_extension(cog_name)
                    await interaction.followup.send(f"✅ Successfully loaded new extension `{cog_name}`")
                except Exception as e:
                    await interaction.followup.send(f"❌ Failed to load/reload `{cog_name}`: {e}")
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to reload `{cog_name}`: {e}")

    @reload.autocomplete('extension')
    async def reload_autocomplete(self, interaction: discord.Interaction, current: str):
        cogs = ["all"]
        for filename in os.listdir("./cogs"):
            if filename.endswith(".py") and filename != "utils.py":
                cogs.append(filename[:-3])
        return [
            app_commands.Choice(name=cog, value=cog)
            for cog in cogs if current.lower() in cog.lower()
        ][:25]

async def setup(bot):
    await bot.add_cog(System(bot))