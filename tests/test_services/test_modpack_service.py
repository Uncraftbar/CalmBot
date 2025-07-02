import pytest
from unittest.mock import AsyncMock, MagicMock
import discord
from src.services.modpack_service import ModpackService
from src.models.modpack import ModpackInfo, ModLoader


class TestModpackService:
    
    @pytest.mark.asyncio
    async def test_create_modpack_category_success(self, modpack_service, mock_guild):
        """Test successful modpack category creation."""
        modpack_info = ModpackInfo(
            name="TestPack",
            modloader=ModLoader.FORGE,
            modpack_link="https://example.com/pack",
            connection_ip="mc.example.com"
        )
        
        # Mock category creation
        mock_category = MagicMock(spec=discord.CategoryChannel)
        mock_category.id = 123456
        mock_guild.create_category.return_value = mock_category
        
        # Mock channel creation
        mock_guild.create_text_channel.return_value = MagicMock(spec=discord.TextChannel)
        
        success, message = await modpack_service.create_modpack_category(mock_guild, modpack_info)
        
        assert success is True
        assert "Created **TestPack [FORGE]** with required channels" in message
        assert modpack_info.category_id == 123456
        
        # Verify methods were called
        mock_guild.create_category.assert_called_once_with("TestPack [FORGE]")
        assert mock_guild.create_text_channel.call_count == 3  # general, technical-help, connection-info
    
    @pytest.mark.asyncio
    async def test_create_modpack_category_already_exists(self, modpack_service, mock_guild):
        """Test modpack category creation when category already exists."""
        modpack_info = ModpackInfo(
            name="TestPack",
            modloader=ModLoader.FORGE,
            modpack_link="https://example.com/pack",
            connection_ip="mc.example.com"
        )
        
        # Mock existing category
        existing_category = MagicMock(spec=discord.CategoryChannel)
        existing_category.name = "TestPack [FORGE]"
        mock_guild.categories = [existing_category]
        
        # Mock discord.utils.get to return the existing category
        with pytest.MonkeyPatch().context() as m:
            m.setattr("discord.utils.get", lambda categories, name: existing_category)
            
            success, message = await modpack_service.create_modpack_category(mock_guild, modpack_info)
        
        assert success is False
        assert "already exists" in message
    
    @pytest.mark.asyncio
    async def test_create_modpack_role_success(self, modpack_service, mock_guild):
        """Test successful modpack role creation."""
        modpack_info = ModpackInfo(
            name="TestPack",
            modloader=ModLoader.FORGE,
            modpack_link="https://example.com/pack",
            connection_ip="mc.example.com",
            role_emoji="🎮"
        )
        
        # Mock role creation
        mock_role = MagicMock(spec=discord.Role)
        mock_role.id = 987654
        mock_role.name = "TestPack Updates"
        mock_guild.create_role.return_value = mock_role
        
        success, message = await modpack_service.create_modpack_role(mock_guild, modpack_info)
        
        assert success is True
        assert "Created role 'TestPack Updates'" in message
        assert modpack_info.role_id == 987654
    
    @pytest.mark.asyncio
    async def test_create_modpack_role_no_emoji(self, modpack_service, mock_guild):
        """Test modpack role creation when no emoji is provided."""
        modpack_info = ModpackInfo(
            name="TestPack",
            modloader=ModLoader.FORGE,
            modpack_link="https://example.com/pack",
            connection_ip="mc.example.com"
            # No role_emoji
        )
        
        success, message = await modpack_service.create_modpack_role(mock_guild, modpack_info)
        
        assert success is True
        assert message == ""  # No role creation requested
        mock_guild.create_role.assert_not_called()