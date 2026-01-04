"""
Global error handler for CalmBot.
Catches unhandled exceptions and provides user-friendly error messages.
"""

import traceback
import discord
from discord.ext import commands
from discord import app_commands

from cogs.utils import get_logger, error_embed, warning_embed

log = get_logger("errors")


class ErrorHandler(commands.Cog):
    """Handles errors globally for both app commands and traditional commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register the app command error handler
        self.bot.tree.on_error = self.on_app_command_error
    
    async def cog_unload(self):
        # Reset error handler when cog is unloaded
        self.bot.tree.on_error = self.bot.tree.__class__.on_error
    
    async def on_app_command_error(
        self, 
        interaction: discord.Interaction, 
        error: app_commands.AppCommandError
    ):
        """Handle errors from slash commands."""
        
        # Unwrap the error if it's wrapped
        original = getattr(error, 'original', error)
        command_name = interaction.command.name if interaction.command else "unknown"
        
        # Handle specific error types
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = warning_embed(
                "Command on Cooldown",
                f"Please wait **{error.retry_after:.1f}s** before using this command again."
            )
            await self._send_error(interaction, embed)
            return
        
        if isinstance(error, app_commands.MissingPermissions):
            missing = ", ".join(error.missing_permissions)
            embed = error_embed(
                "Missing Permissions",
                f"You need the following permissions: **{missing}**"
            )
            await self._send_error(interaction, embed)
            return
        
        if isinstance(error, app_commands.BotMissingPermissions):
            missing = ", ".join(error.missing_permissions)
            embed = error_embed(
                "Bot Missing Permissions",
                f"I need the following permissions to run this command: **{missing}**"
            )
            await self._send_error(interaction, embed)
            return
        
        if isinstance(error, app_commands.CheckFailure):
            # Usually handled by the check itself, but catch any unhandled ones
            embed = error_embed(
                "Access Denied",
                "You don't have permission to use this command."
            )
            await self._send_error(interaction, embed)
            return
        
        if isinstance(error, app_commands.CommandNotFound):
            # Silently ignore - usually stale command cache
            log.debug(f"Command not found: {command_name}")
            return
        
        # Handle Discord API errors
        if isinstance(original, discord.Forbidden):
            embed = error_embed(
                "Permission Error",
                "I don't have permission to perform this action. "
                "Please check my role permissions."
            )
            await self._send_error(interaction, embed)
            log.warning(f"Forbidden error in /{command_name}: {original}")
            return
        
        if isinstance(original, discord.NotFound):
            embed = error_embed(
                "Not Found",
                "The requested resource (channel, message, role, etc.) was not found. "
                "It may have been deleted."
            )
            await self._send_error(interaction, embed)
            log.warning(f"NotFound error in /{command_name}: {original}")
            return
        
        if isinstance(original, discord.HTTPException):
            embed = error_embed(
                "Discord API Error",
                "Discord returned an error. Please try again in a moment."
            )
            await self._send_error(interaction, embed)
            log.error(f"HTTPException in /{command_name}: {original}")
            return
        
        # Unhandled error - log full traceback
        log.error(
            f"Unhandled error in /{command_name} by {interaction.user}:\n"
            f"{''.join(traceback.format_exception(type(original), original, original.__traceback__))}"
        )
        
        embed = error_embed(
            "Unexpected Error",
            "Something went wrong while processing your command.\n"
            "The error has been logged and will be investigated.",
            footer=f"Command: /{command_name}"
        )
        await self._send_error(interaction, embed)
    
    async def _send_error(self, interaction: discord.Interaction, embed: discord.Embed):
        """Send error embed, handling already-responded interactions."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            # Last resort - can't even send the error message
            log.error("Failed to send error message to user")
    
    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handle errors from prefix commands (if any)."""
        
        original = getattr(error, 'original', error)
        
        # Ignore command not found for prefix commands
        if isinstance(error, commands.CommandNotFound):
            return
        
        # Log unexpected errors
        if not isinstance(error, (commands.CheckFailure, commands.UserInputError)):
            log.error(
                f"Error in ?{ctx.command} by {ctx.author}:\n"
                f"{''.join(traceback.format_exception(type(original), original, original.__traceback__))}"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorHandler(bot))
