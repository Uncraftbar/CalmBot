from dataclasses import dataclass
from typing import Optional, List
from enum import Enum


class ModLoader(Enum):
    NEOFORGE = "NEOFORGE"
    FORGE = "FORGE"
    FABRIC = "FABRIC"


@dataclass
class ModpackInfo:
    """Data model for modpack information."""
    name: str
    modloader: ModLoader
    modpack_link: str
    connection_ip: str
    category_id: Optional[int] = None
    role_id: Optional[int] = None
    role_emoji: Optional[str] = None
    
    @property
    def category_name(self) -> str:
        """Generate category name with modloader."""
        return f"{self.name} [{self.modloader.value}]"
    
    @property
    def role_name(self) -> str:
        """Generate role name for updates."""
        return f"{self.name} Updates"


@dataclass
class ConnectionInfo:
    """Data model for connection information."""
    modpack_link: str
    connection_ip: str
    additional_info: Optional[str] = None
    
    def format_message(self) -> str:
        """Format connection info as Discord message."""
        msg = f"**Modpack URL:** {self.modpack_link}\n"
        msg += f"**Connection URL:** {self.connection_ip}"
        
        if self.additional_info:
            msg += f"\n\n**Additional Information:**\n{self.additional_info}"
        
        return msg