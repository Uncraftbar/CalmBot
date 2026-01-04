"""
Shared utilities for CalmBot.
Provides logging, permissions, embed helpers, and common functions.
"""

import json
import os
import logging
import functools
from enum import Enum
from typing import Optional, Any, Callable
from datetime import datetime

import discord
from discord import app_commands
from ampapi import AMPControllerInstance

# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging(name: str = "calmbot", level: int = logging.INFO) -> logging.Logger:
    """
    Creates and configures a logger with consistent formatting.
    
    Args:
        name: Logger name (usually module name)
        level: Logging level
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        logger.setLevel(level)
        
        # Console handler with formatting
        handler = logging.StreamHandler()
        handler.setLevel(level)
        
        formatter = logging.Formatter(
            fmt="%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s",
            datefmt="%H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    
    return logger


# Create root logger for the bot
log = setup_logging("calmbot")


def get_logger(name: str) -> logging.Logger:
    """Get a child logger for a specific module."""
    return setup_logging(f"calmbot.{name}")


# =============================================================================
# FILE PATHS
# =============================================================================

DATA_DIR = "data"
ROLES_BOARD_FILE = os.path.join(DATA_DIR, "roles_board.json")
CHAT_BRIDGE_FILE = os.path.join(DATA_DIR, "chat_bridge.json")
AUTOSEND_FILE = os.path.join(DATA_DIR, "autosend.json")
REACTION_ROLES_FILE = os.path.join(DATA_DIR, "reaction_roles.json")


# =============================================================================
# JSON HELPERS
# =============================================================================

_json_log = get_logger("json")


def load_json(filename: str, default: Any = None) -> Any:
    """
    Safely load JSON from a file with error handling.
    
    Args:
        filename: Path to JSON file
        default: Default value if file doesn't exist or is invalid
        
    Returns:
        Parsed JSON data or default value
    """
    if default is None:
        default = {}
        
    if not os.path.exists(filename):
        return default
        
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        _json_log.error(f"Invalid JSON in {filename}: {e}")
        return default
    except OSError as e:
        _json_log.error(f"Cannot read {filename}: {e}")
        return default


def save_json(filename: str, data: Any) -> bool:
    """
    Safely save data to a JSON file.
    
    Args:
        filename: Path to JSON file
        data: Data to serialize
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(filename) or '.', exist_ok=True)
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        _json_log.error(f"Cannot write {filename}: {e}")
        return False


# =============================================================================
# EMBED BUILDER - CONSISTENT UX
# =============================================================================

class EmbedType(Enum):
    """Standardized embed types for consistent UX."""
    SUCCESS = ("✅", discord.Color.green())
    ERROR = ("❌", discord.Color.red())
    WARNING = ("⚠️", discord.Color.orange())
    INFO = ("ℹ️", discord.Color.blue())
    LOADING = ("⏳", discord.Color.greyple())


def make_embed(
    title: str,
    description: str = None,
    embed_type: EmbedType = EmbedType.INFO,
    *,
    fields: list[tuple[str, str, bool]] = None,
    footer: str = None,
    thumbnail: str = None,
    image: str = None,
    timestamp: bool = False
) -> discord.Embed:
    """
    Create a standardized embed with consistent styling.
    
    Args:
        title: Embed title (emoji prefix added automatically)
        description: Main content
        embed_type: Type determining color and emoji
        fields: List of (name, value, inline) tuples
        footer: Footer text
        thumbnail: Thumbnail URL
        image: Main image URL
        timestamp: Whether to add current timestamp
        
    Returns:
        Configured discord.Embed
    """
    emoji, color = embed_type.value
    
    embed = discord.Embed(
        title=f"{emoji} {title}",
        description=description,
        color=color
    )
    
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    
    if footer:
        embed.set_footer(text=footer)
    
    if thumbnail and is_valid_url(thumbnail):
        embed.set_thumbnail(url=thumbnail)
    
    if image and is_valid_url(image):
        embed.set_image(url=image)
    
    if timestamp:
        embed.timestamp = datetime.utcnow()
    
    return embed


def success_embed(title: str, description: str = None, **kwargs) -> discord.Embed:
    """Shortcut for success embeds."""
    return make_embed(title, description, EmbedType.SUCCESS, **kwargs)


def error_embed(title: str, description: str = None, **kwargs) -> discord.Embed:
    """Shortcut for error embeds."""
    return make_embed(title, description, EmbedType.ERROR, **kwargs)


def warning_embed(title: str, description: str = None, **kwargs) -> discord.Embed:
    """Shortcut for warning embeds."""
    return make_embed(title, description, EmbedType.WARNING, **kwargs)


def info_embed(title: str, description: str = None, **kwargs) -> discord.Embed:
    """Shortcut for info embeds."""
    return make_embed(title, description, EmbedType.INFO, **kwargs)


# =============================================================================
# PERMISSION HELPERS
# =============================================================================

_perm_log = get_logger("permissions")

# Configurable mod role names
MOD_ROLE_NAMES = {"Moderators", "Admins", "Mods", "Staff", "Admin", "Moderator"}


def has_mod_permissions(member: discord.Member) -> bool:
    """
    Check if a member has moderator-level permissions.
    
    Args:
        member: The member to check
        
    Returns:
        True if member has admin/mod permissions
    """
    # Guild owner always has permission
    if member.guild.owner_id == member.id:
        return True
    
    # Direct admin permission
    if member.guild_permissions.administrator:
        return True
    
    # Check for mod roles or permissions
    for role in member.roles:
        if role.name in MOD_ROLE_NAMES:
            return True
        if role.permissions.manage_guild or role.permissions.manage_channels:
            return True
    
    return False


async def check_permissions(interaction: discord.Interaction) -> bool:
    """
    Check permissions and send denial message if unauthorized.
    
    Args:
        interaction: The interaction to check
        
    Returns:
        True if authorized, False if denied (message sent)
    """
    if has_mod_permissions(interaction.user):
        return True
    
    _perm_log.debug(f"Permission denied for {interaction.user} on /{interaction.command.name if interaction.command else 'unknown'}")
    
    embed = error_embed(
        "Permission Denied",
        "This command requires **Administrator** or **Moderator** permissions."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


def admin_only():
    """
    Decorator for app commands that require admin/mod permissions.
    
    Usage:
        @app_commands.command()
        @admin_only()
        async def my_command(self, interaction): ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        return await check_permissions(interaction)
    
    return app_commands.check(predicate)


# Legacy function for backwards compatibility
async def has_admin_or_mod_permissions(interaction: discord.Interaction) -> bool:
    """Legacy permission check. Use check_permissions() or @admin_only() instead."""
    return await check_permissions(interaction)


# =============================================================================
# VALIDATION HELPERS
# =============================================================================

def is_valid_url(url: Optional[str]) -> bool:
    """Check if a string is a valid HTTP(S) URL."""
    if not url or not isinstance(url, str):
        return False
    return url.startswith("http://") or url.startswith("https://")


def safe_embed_color(color_input: Optional[str]) -> int:
    """
    Safely parse a color string to discord color int.
    
    Args:
        color_input: Hex color string (e.g., "#FF0000" or "FF0000")
        
    Returns:
        Color int, or default green if invalid
    """
    if not color_input:
        return discord.Color.green().value
    
    try:
        # Remove hash and parse
        clean = str(color_input).replace("#", "").strip()
        return int(clean, 16)
    except (ValueError, TypeError):
        return discord.Color.green().value


# =============================================================================
# DISCORD HELPERS
# =============================================================================

async def find_category_by_name(
    guild: discord.Guild, 
    input_name: str
) -> Optional[discord.CategoryChannel]:
    """
    Find a category by name, supporting [MODLOADER] suffix format.
    
    Args:
        guild: The guild to search
        input_name: Category name to find
        
    Returns:
        The category if found, None otherwise
    """
    # Exact match first
    category = discord.utils.get(guild.categories, name=input_name)
    if category:
        return category
    
    # Try matching base name (before [MODLOADER])
    input_lower = input_name.lower().strip()
    for category in guild.categories:
        name = category.name
        if "[" in name and "]" in name:
            base_name = name.split("[")[0].strip()
            if base_name.lower() == input_lower:
                return category
    
    return None


# =============================================================================
# AMP HELPERS
# =============================================================================

_amp_log = get_logger("amp")


async def fetch_valid_instances() -> list:
    """
    Fetch and filter AMP instances, excluding ADS/Controller.
    
    Returns:
        List of valid managed instances
    """
    try:
        ads = AMPControllerInstance()
        
        # Force session clear to prevent server-side caching
        if hasattr(ads, '_bridge') and hasattr(ads._bridge, '_sessions'):
            ads._bridge._sessions.clear()

        fetched_instances = await ads.get_instances(format_data=True)
        
        if not fetched_instances:
            _amp_log.debug("No instances returned from AMP API")
            return []

        valid_instances = []
        for inst in fetched_instances:
            # Filter out ADS/Controller
            mod_name = str(getattr(inst, 'module_display_name', '')).lower()
            friendly_name = str(getattr(inst, 'friendly_name', '')).strip().lower()
            
            if mod_name in ('application deployment service', 'ads module', 'controller'):
                continue
            if friendly_name == 'ads':
                continue
            if not hasattr(inst, 'instance_name'):
                continue
                
            valid_instances.append(inst)
        
        _amp_log.debug(f"Fetched {len(valid_instances)} valid AMP instances")
        return valid_instances
        
    except Exception as e:
        _amp_log.error(f"Failed to fetch AMP instances: {e}")
        return []


def get_instance_state(status) -> str:
    """
    Extract human-readable state from AMP instance status.
    
    Args:
        status: AMP instance status object
        
    Returns:
        Human-readable state string
    """
    try:
        if hasattr(status, 'state') and status.state:
            state_str = str(status.state)
            if '.' in state_str:
                state_val = state_str.split('.')[-1].replace('_', ' ').capitalize()
            else:
                state_val = state_str.replace('_', ' ').capitalize()
            
            # Treat 'Ready' as 'Running' for user clarity
            return 'Running' if state_val.lower() == 'ready' else state_val
            
        elif hasattr(status, 'running'):
            return 'Running' if status.running else 'Stopped'
            
    except Exception:
        pass
    
    return 'Unknown'
