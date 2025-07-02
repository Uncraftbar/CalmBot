import discord
from typing import Optional, List


class DiscordHelpers:
    """Discord-specific utility functions."""
    
    @staticmethod
    async def find_category_by_name(guild: discord.Guild, input_name: str) -> Optional[discord.CategoryChannel]:
        """Find category by exact name or base name (without [MODLOADER])."""
        # Try exact match first
        for category in guild.categories:
            if category.name == input_name:
                return category
        
        # Try base name match
        for category in guild.categories:
            if "[" in category.name and "]" in category.name:
                base_name = category.name.split("[")[0].strip()
                if base_name.lower() == input_name.lower():
                    return category
        
        return None
    
    @staticmethod
    def extract_base_name(category_name: str) -> str:
        """Extract base name from category (remove [MODLOADER] part)."""
        if "[" in category_name and "]" in category_name:
            return category_name.split("[")[0].strip()
        return category_name
    
    @staticmethod
    def create_safe_embed(title: str = None, description: str = None, 
                         color: int = None, **kwargs) -> discord.Embed:
        """Create embed with safe defaults."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color or discord.Color.blue()
        )
        
        # Add optional fields
        if kwargs.get('footer'):
            embed.set_footer(text=kwargs['footer'], icon_url=kwargs.get('footer_icon'))
        
        if kwargs.get('thumbnail'):
            embed.set_thumbnail(url=kwargs['thumbnail'])
        
        if kwargs.get('image_url'):
            embed.set_image(url=kwargs['image_url'])
        
        if kwargs.get('url'):
            embed.url = kwargs['url']
        
        if kwargs.get('timestamp'):
            embed.timestamp = discord.utils.utcnow()
        
        return embed