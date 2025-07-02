#!/usr/bin/env python3
"""
Main entry point for the Discord bot.
"""

import asyncio
import discord
from discord.ext import commands
from pathlib import Path
import sys

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.bot import MainBot
from core.storage import StorageManager

# Import config - you'll need to create this file
try:
    import config
    GUILD_IDS = config.GUILD_IDS
    BOT_TOKEN = config.BOT_TOKEN
except ImportError:
    print("Please create config.py with your bot settings!")
    print("Required variables: GUILD_IDS, BOT_TOKEN, AMP_API_URL, AMP_USER, AMP_PASS")
    sys.exit(1)


async def main():
    """Main function to run the bot."""
    # Initialize storage
    storage = StorageManager()
    
    # Create bot instance
    bot = MainBot(guild_ids=GUILD_IDS, storage=storage)
    
    # Run the bot
    try:
        await bot.start(BOT_TOKEN)
    except KeyboardInterrupt:
        print("\nShutting down bot...")
        await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
