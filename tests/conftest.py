import pytest
import asyncio
import discord
from unittest.mock import AsyncMock, MagicMock
from src.core.storage import StorageManager
from src.services.modpack_service import ModpackService


@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_guild():
    """Create a mock Discord guild."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 123456789
    guild.name = "Test Guild"
    guild.categories = []
    guild.roles = []
    guild.me = MagicMock(spec=discord.Member)
    guild.default_role = MagicMock(spec=discord.Role)
    
    # Mock create methods
    guild.create_category = AsyncMock()
    guild.create_text_channel = AsyncMock()
    guild.create_role = AsyncMock()
    
    return guild


@pytest.fixture
def mock_user():
    """Create a mock Discord user with admin permissions."""
    user = MagicMock(spec=discord.Member)
    user.id = 987654321
    user.name = "TestUser"
    user.guild_permissions.administrator = True
    user.roles = []
    return user


@pytest.fixture
def mock_interaction(mock_guild, mock_user):
    """Create a mock Discord interaction."""
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.guild = mock_guild
    interaction.user = mock_user
    interaction.response.send_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.fixture
def storage_manager(tmp_path):
    """Create a StorageManager with temporary directory."""
    return StorageManager(str(tmp_path))


@pytest.fixture
def modpack_service(storage_manager):
    """Create a ModpackService instance."""
    return ModpackService(storage_manager)