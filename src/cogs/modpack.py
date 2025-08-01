import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from ..core.permissions import PermissionChecker
from ..core.storage import StorageManager
from ..services.modpack_service import ModpackService
from ..models.modpack import ModpackInfo, ModLoader
from ..utils.validators import Validators


class ModpackCog(commands.Cog):
    """Modpack management commands."""
    
    def __init__(self, bot):
        self.bot = bot
        self.storage = StorageManager()
        self.service = ModpackService(self.storage)
    
    @app_commands.command(name="setup_modpack", description="Set up modpack category and channels")
    @app_commands.describe(
        name="Name of the modpack",
        modloader="The modloader for this modpack",
        modpack_link="Link to the modpack",
        connection_ip="Connection IP or URL",
        role_emoji="Emoji for users to react with to get the role"
    )
    @app_commands.choices(modloader=[
        app_commands.Choice(name="NEOFORGE", value="NEOFORGE"),
        app_commands.Choice(name="FORGE", value="FORGE"),
        app_commands.Choice(name="FABRIC", value="FABRIC")
    ])
    async def setup_modpack(self, interaction: discord.Interaction, 
                           name: str, modloader: str, modpack_link: str, 
                           connection_ip: str, role_emoji: Optional[str] = None):
        """Set up a new modpack with category, channels, and optional role."""
        
        if not await PermissionChecker.check_interaction_permissions(interaction):
            return
        
        await interaction.response.send_message(f"Setting up **{name} [{modloader}]**...", ephemeral=True)
        
        # Validate inputs
        if not Validators.is_valid_url(modpack_link):
            await interaction.edit_original_response(
                content="❌ Invalid modpack link. Please provide a valid URL."
            )
            return
        
        # Create modpack info
        modpack_info = ModpackInfo(
            name=name,
            modloader=ModLoader(modloader),
            modpack_link=modpack_link,
            connection_ip=connection_ip,
            role_emoji=role_emoji
        )
        
        # Create category and channels
        success, message = await self.service.create_modpack_category(interaction.guild, modpack_info)
        if not success:
            await interaction.edit_original_response(content=f"❌ {message}")
            return
        
        result_message = f"✅ {message}"
        
        # Create role if requested
        if role_emoji:
            role_success, role_message = await self.service.create_modpack_role(
                interaction.guild, modpack_info
            )
            
            if role_success and role_message:
                # Try to add to roles board
                board_success, board_message = await self.service.add_role_to_board(
                    self.bot, modpack_info
                )
                
                if board_success and board_message:
                    result_message += f"\n✅ {role_message} with reaction {role_emoji} on the roles board"
                elif board_message:
                    result_message += f"\n⚠️ {role_message}"
                else:
                    result_message += f"\n✅ {role_message}"
            else:
                result_message += f"\n⚠️ {role_message}"
        
        await interaction.edit_original_response(content=result_message)
    
    @app_commands.command(name="delete_modpack", description="Delete a modpack category and associated role")
    @app_commands.describe(category_name="The name of the modpack (with or without [MODLOADER])")
    async def delete_modpack(self, interaction: discord.Interaction, category_name: str):
        """Delete a modpack with confirmation."""
        
        if not await PermissionChecker.check_interaction_permissions(interaction):
            return
        
        await interaction.response.send_message(f"Searching for modpack **{category_name}**...", ephemeral=True)
        
        # Create confirmation view
        confirmation_view = ModpackDeleteConfirmationView(self.service, category_name)
        confirmation_message = await confirmation_view.create_confirmation_message(interaction.guild)
        
        if not confirmation_message:
            await interaction.edit_original_response(
                content=f"❌ Category containing **{category_name}** not found."
            )
            return
        
        await interaction.edit_original_response(content=confirmation_message, view=confirmation_view)


class ModpackDeleteConfirmationView(discord.ui.View):
    """Confirmation view for modpack deletion."""
    
    def __init__(self, service: ModpackService, category_name: str):
        super().__init__(timeout=60)
        self.service = service
        self.category_name = category_name
        self.confirmed = None
    
    async def create_confirmation_message(self, guild: discord.Guild) -> Optional[str]:
        """Create confirmation message with details."""
        from ..utils.discord_helpers import DiscordHelpers
        
        category = await DiscordHelpers.find_category_by_name(guild, self.category_name)
        if not category:
            return None
        
        channels_info = [f"#{channel.name}" for channel in category.channels]
        base_name = DiscordHelpers.extract_base_name(category.name)
        role = discord.utils.ge(guild.roles, name=f"{base_name} Updates")
        
        msg = f"**Warning!** You are about to delete:\n"
        msg += f"• Category: **{category.name}**\n"
        msg += f"• Channels ({len(channels_info)}): {', '.join(channels_info)}\n"
        
        if role:
            msg += f"• Role: **{role.name}**\n"
            msg += f"• Role will be removed from roles board\n"
        
        return msg
    
    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle confirmation."""
        self.confirmed = True
        self.stop()
        
        await interaction.response.edit_message(
            content=f"Deleting modpack **{self.category_name}**...", view=None
        )
        
        # Perform deletion
        success, actual_name, deletion_log = await self.service.delete_modpack(
            interaction.guild, self.category_name
        )
        
        summary = "\n".join(deletion_log)
        status_emoji = "✅" if success else "⚠️"
        
        await interaction.edit_original_response(
            content=f"{status_emoji} Modpack **{actual_name}** deletion summary:\n\n{summary}"
        )
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle cancellation."""
        self.confirmed = False
        self.stop()
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)


async def setup(bot):
    await bot.add_cog(ModpackCog(bot))