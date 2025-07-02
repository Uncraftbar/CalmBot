import discord
from typing import List


class PermissionChecker:
    """Centralized permission checking logic."""
    
    MOD_ROLE_NAMES = ["Moderators", "Admins"]
    
    @classmethod
    def has_admin_permissions(cls, user: discord.Member) -> bool:
        """Check if user has administrator permissions."""
        return user.guild_permissions.administrator
    
    @classmethod
    def has_mod_permissions(cls, user: discord.Member) -> bool:
        """Check if user has moderator permissions."""
        return any(
            role.name in cls.MOD_ROLE_NAMES or 
            role.permissions.manage_guild or 
            role.permissions.manage_channels
            for role in user.roles
        )
    
    @classmethod
    def has_admin_or_mod_permissions(cls, user: discord.Member) -> bool:
        """Check if user has admin or mod permissions."""
        return cls.has_admin_permissions(user) or cls.has_mod_permissions(user)
    
    @classmethod
    async def check_interaction_permissions(cls, interaction: discord.Interaction) -> bool:
        """Check permissions and respond with error if insufficient."""
        if cls.has_admin_or_mod_permissions(interaction.user):
            return True
        
        await interaction.response.send_message(
            "You don't have permission to use this command. "
            "Only administrators and moderators can use it.", 
            ephemeral=True
        )
        return False