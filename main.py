"""
CalmBot - A feature-rich Discord bot for Minecraft server communities.

Main entry point with improved config loading, logging, and error handling.
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

from cogs.utils import setup_logging, get_logger

# =============================================================================
# CONFIGURATION
# =============================================================================

log = get_logger("main")


class Config:
    """Bot configuration with validation."""
    
    def __init__(self):
        self.bot_token: str = ""
        self.guild_ids: list[int] = []
        self.amp_api_url: str = ""
        self.amp_user: str = ""
        self.amp_pass: str = ""
    
    @classmethod
    def load(cls) -> "Config":
        """
        Load configuration from config.py file.
        
        Returns:
            Config instance with validated settings
            
        Raises:
            SystemExit: If config is missing or invalid
        """
        config = cls()
        config_path = Path("config.py")
        
        if not config_path.exists():
            log.critical("config.py not found! Please create it with your bot settings.")
            log.info("See README.md for configuration example.")
            sys.exit(1)
        
        try:
            # Import config module
            import importlib.util
            spec = importlib.util.spec_from_file_location("config", config_path)
            if spec is None or spec.loader is None:
                raise ImportError("Failed to load config.py spec")
            
            config_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(config_module)
            
            # Required settings
            config.bot_token = getattr(config_module, 'BOT_TOKEN', '')
            config.guild_ids = getattr(config_module, 'GUILD_IDS', [])
            
            # Optional AMP settings
            config.amp_api_url = getattr(config_module, 'AMP_API_URL', '')
            config.amp_user = getattr(config_module, 'AMP_USER', '')
            config.amp_pass = getattr(config_module, 'AMP_PASS', '')
            
            # Validation
            if not config.bot_token:
                log.critical("BOT_TOKEN is missing in config.py!")
                sys.exit(1)
            
            if not config.guild_ids:
                log.warning("GUILD_IDS is empty - commands won't sync to any guilds")
            
            log.info(f"Configuration loaded: {len(config.guild_ids)} guild(s) configured")
            return config
            
        except Exception as e:
            log.critical(f"Failed to load config.py: {e}")
            sys.exit(1)


# Make config accessible globally for cogs
_config: Config = None


def get_config() -> Config:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


# =============================================================================
# BOT CLASS
# =============================================================================

class CalmBot(commands.Bot):
    """
    Main bot class with improved startup and cog management.
    """
    
    def __init__(self, config: Config):
        # Set up intents
        intents = discord.Intents.default()
        intents.reactions = True
        intents.members = True
        intents.message_content = True
        
        super().__init__(
            command_prefix="?",
            intents=intents,
            help_command=None
        )
        
        self.config = config
        self._cogs_loaded = False
    
    async def setup_hook(self):
        """Called when the bot is starting up."""
        await self._load_cogs()
        await self._sync_commands()
    
    async def _load_cogs(self):
        """Load all cogs from the cogs directory."""
        cogs_dir = Path("./cogs")
        
        if not cogs_dir.exists():
            log.warning("Cogs directory not found!")
            return
        
        # Track results
        loaded = []
        failed = []
        
        for filename in sorted(os.listdir(cogs_dir)):
            if not filename.endswith(".py"):
                continue
            if filename == "utils.py":  # Skip utility module
                continue
            
            cog_name = f"cogs.{filename[:-3]}"
            
            try:
                await self.load_extension(cog_name)
                loaded.append(cog_name)
            except Exception as e:
                failed.append((cog_name, str(e)))
                log.error(f"Failed to load {cog_name}: {e}")
        
        # Summary
        if loaded:
            log.info(f"Loaded {len(loaded)} cog(s): {', '.join(c.split('.')[-1] for c in loaded)}")
        
        if failed:
            log.warning(f"Failed to load {len(failed)} cog(s)")
            for cog, error in failed:
                log.warning(f"  - {cog}: {error}")
        
        self._cogs_loaded = True
    
    async def _sync_commands(self):
        """Sync slash commands to configured guilds."""
        if not self.config.guild_ids:
            log.warning("No guild IDs configured - skipping command sync")
            return
        
        synced_count = 0
        
        for guild_id in self.config.guild_ids:
            try:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                synced_count += len(synced)
                log.debug(f"Synced {len(synced)} command(s) to guild {guild_id}")
            except discord.HTTPException as e:
                log.error(f"Failed to sync commands to guild {guild_id}: {e}")
        
        if synced_count:
            log.info(f"Synced commands to {len(self.config.guild_ids)} guild(s)")
    
    async def on_ready(self):
        """Called when the bot is fully ready."""
        log.info("=" * 50)
        log.info(f"  Logged in as: {self.user.name}#{self.user.discriminator}")
        log.info(f"  User ID: {self.user.id}")
        log.info(f"  Guilds: {len(self.guilds)}")
        log.info(f"  discord.py: {discord.__version__}")
        log.info("=" * 50)
        log.info("Bot is ready!")
    
    async def on_connect(self):
        """Called when connected to Discord."""
        log.debug("Connected to Discord gateway")
    
    async def on_disconnect(self):
        """Called when disconnected from Discord."""
        log.warning("Disconnected from Discord gateway")
    
    async def on_resumed(self):
        """Called when session is resumed."""
        log.info("Session resumed")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    """Main entry point."""
    # Set up root logging
    setup_logging("calmbot", logging.INFO)
    
    # Also configure discord.py logging
    discord_log = logging.getLogger("discord")
    discord_log.setLevel(logging.WARNING)
    
    log.info("Starting CalmBot...")
    
    # Load config
    config = get_config()
    
    # Ensure data directory exists
    os.makedirs("data", exist_ok=True)
    
    # Create and run bot
    bot = CalmBot(config)
    
    try:
        bot.run(config.bot_token, log_handler=None)  # We handle our own logging
    except discord.LoginFailure:
        log.critical("Invalid bot token! Check your config.py")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    except Exception as e:
        log.critical(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
