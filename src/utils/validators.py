import re
from typing import Optional
from urllib.parse import urlparse


class Validators:
    """Input validation utilities."""
    
    URL_REGEX = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    
    HEX_COLOR_REGEX = re.compile(r'^#?([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$')
    
    @classmethod
    def is_valid_url(cls, url: Optional[str]) -> bool:
        """Validate URL format."""
        if not url or not isinstance(url, str):
            return False
        return bool(cls.URL_REGEX.match(url))
    
    @classmethod
    def is_valid_hex_color(cls, color: Optional[str]) -> bool:
        """Validate hex color format."""
        if not color or not isinstance(color, str):
            return False
        return bool(cls.HEX_COLOR_REGEX.match(color))
    
    @classmethod
    def sanitize_hex_color(cls, color: Optional[str]) -> Optional[int]:
        """Convert hex color string to integer."""
        if not cls.is_valid_hex_color(color):
            return None
        
        try:
            return int(color.replace("#", ""), 16)
        except ValueError:
            return None
    
    @classmethod
    def is_valid_discord_id(cls, id_str: str) -> bool:
        """Validate Discord ID format."""
        try:
            discord_id = int(id_str)
            return 17 <= len(str(discord_id)) <= 20
        except ValueError:
            return False