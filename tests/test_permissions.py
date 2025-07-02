import pytest
import discord
from unittest.mock import MagicMock
from src.core.permissions import PermissionChecker


class TestPermissionChecker:
    
    def test_has_admin_permissions_true(self):
        """Test admin permission check returns True for administrator."""
        user = MagicMock(spec=discord.Member)
        user.guild_permissions.administrator = True
        
        assert PermissionChecker.has_admin_permissions(user) is True
    
    def test_has_admin_permissions_false(self):
        """Test admin permission check returns False for non-administrator."""
        user = MagicMock(spec=discord.Member)
        user.guild_permissions.administrator = False
        
        assert PermissionChecker.has_admin_permissions(user) is False
    
    def test_has_mod_permissions_by_role_name(self):
        """Test mod permission check with role name."""
        user = MagicMock(spec=discord.Member)
        
        mod_role = MagicMock(spec=discord.Role)
        mod_role.name = "Moderators"
        mod_role.permissions.manage_guild = False
        mod_role.permissions.manage_channels = False
        
        user.roles = [mod_role]
        
        assert PermissionChecker.has_mod_permissions(user) is True
    
    def test_has_mod_permissions_by_guild_permission(self):
        """Test mod permission check with manage_guild permission."""
        user = MagicMock(spec=discord.Member)
        
        role = MagicMock(spec=discord.Role)
        role.name = "CustomRole"
        role.permissions.manage_guild = True
        role.permissions.manage_channels = False
        
        user.roles = [role]
        
        assert PermissionChecker.has_mod_permissions(user) is True
    
    def test_has_mod_permissions_by_channel_permission(self):
        """Test mod permission check with manage_channels permission."""
        user = MagicMock(spec=discord.Member)
        
        role = MagicMock(spec=discord.Role)
        role.name = "CustomRole"
        role.permissions.manage_guild = False
        role.permissions.manage_channels = True
        
        user.roles = [role]
        
        assert PermissionChecker.has_mod_permissions(user) is True
    
    def test_has_mod_permissions_false(self):
        """Test mod permission check returns False for regular user."""
        user = MagicMock(spec=discord.Member)
        
        role = MagicMock(spec=discord.Role)
        role.name = "Member"
        role.permissions.manage_guild = False
        role.permissions.manage_channels = False
        
        user.roles = [role]
        
        assert PermissionChecker.has_mod_permissions(user) is False
    
    @pytest.mark.asyncio
    async def test_check_interaction_permissions_success(self, mock_interaction):
        """Test interaction permission check with valid permissions."""
        mock_interaction.user.guild_permissions.administrator = True
        
        result = await PermissionChecker.check_interaction_permissions(mock_interaction)
        
        assert result is True
        mock_interaction.response.send_message.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_check_interaction_permissions_failure(self, mock_interaction):
        """Test interaction permission check with insufficient permissions."""
        mock_interaction.user.guild_permissions.administrator = False
        mock_interaction.user.roles = []
        
        result = await PermissionChecker.check_interaction_permissions(mock_interaction)
        
        assert result is False
        mock_interaction.response.send_message.assert_called_once()