import discord
from typing import Optional, List, Tuple
from ..models.modpack import ModpackInfo, ConnectionInfo, ModLoader
from ..core.storage import StorageManager
from ..utils.discord_helpers import DiscordHelpers
from ..core.permissions import PermissionChecker


class ModpackService:
    """Business logic for modpack management."""
    
    def __init__(self, storage: StorageManager):
        self.storage = storage
        self.roles_board_file = "roles_board.json"
    
    async def create_modpack_category(self, guild: discord.Guild, 
                                     modpack_info: ModpackInfo) -> Tuple[bool, str]:
        """Create modpack category with channels."""
        category_name = modpack_info.category_name
        
        # Check if category already exists
        if discord.utils.get(guild.categories, name=category_name):
            return False, f"Category **{category_name}** already exists."
        
        try:
            # Create category and channels
            category = await guild.create_category(category_name)
            await guild.create_text_channel("general", category=category)
            await guild.create_text_channel("technical-help", category=category)
            
            # Create connection-info channel with restricted permissions
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    send_messages=False, add_reactions=True
                ),
                guild.me: discord.PermissionOverwrite(send_messages=True)
            }
            
            connection_info_channel = await guild.create_text_channel(
                "connection-info", overwrites=overwrites, category=category
            )
            
            # Post connection information
            connection_info = ConnectionInfo(
                modpack_link=modpack_info.modpack_link,
                connection_ip=modpack_info.connection_ip
            )
            await connection_info_channel.send(connection_info.format_message())
            
            modpack_info.category_id = category.id
            return True, f"Created **{category_name}** with required channels."
            
        except discord.Forbidden as e:
            return False, f"Cannot create channels properly. Error: {str(e)}"
    
    async def create_modpack_role(self, guild: discord.Guild, 
                                 modpack_info: ModpackInfo) -> Tuple[bool, str]:
        """Create role for modpack updates."""
        if not modpack_info.role_emoji:
            return True, ""  # No role creation requested
        
        try:
            role = await guild.create_role(
                name=modpack_info.role_name,
                mentionable=True,
                reason="Modpack Updates Role"
            )
            modpack_info.role_id = role.id
            return True, f"Created role '{role.name}'"
            
        except discord.Forbidden:
            return False, "I don't have permission to create roles."
        except Exception as e:
            return False, f"Error creating role: {str(e)}"
    
    async def add_role_to_board(self, bot, modpack_info: ModpackInfo) -> Tuple[bool, str]:
        """Add role to the roles board."""
        if not modpack_info.role_id or not modpack_info.role_emoji:
            return True, ""
        
        roles_board = self.storage.load(self.roles_board_file, {
            "channel_id": None, "message_id": None, "roles": []
        })
        
        if not roles_board.get("channel_id") or not roles_board.get("message_id"):
            return False, "No roles board is configured. Use /setup_roles_board to create one."
        
        try:
            channel = bot.get_channel(roles_board["channel_id"])
            if not channel:
                return False, "Could not find the roles board channel."
            
            message = await channel.fetch_message(roles_board["message_id"])
            await message.add_reaction(modpack_info.role_emoji)
            
            # Add to roles board data
            roles_board["roles"].append({
                "name": modpack_info.role_name,
                "emoji": modpack_info.role_emoji,
                "role_id": modpack_info.role_id
            })
            
            self.storage.save(self.roles_board_file, roles_board)
            return True, f"Added role to the roles board with reaction {modpack_info.role_emoji}"
            
        except discord.HTTPException:
            return False, f"'{modpack_info.role_emoji}' is not a valid emoji"
        except Exception as e:
            return False, f"Error adding to roles board: {str(e)}"
    
    async def delete_modpack(self, guild: discord.Guild, category_name: str) -> Tuple[bool, str, List[str]]:
        """Delete modpack category, channels, and role."""
        category = await DiscordHelpers.find_category_by_name(guild, category_name)
        if not category:
            return False, f"Category containing **{category_name}** not found.", []
        
        deletion_log = []
        success = True
        
        # Determine role name
        base_name = DiscordHelpers.extract_base_name(category.name)
        role_name = f"{base_name} Updates"
        role = discord.utils.get(guild.roles, name=role_name)
        
        # Delete channels
        for channel in category.channels:
            try:
                await channel.delete(reason="Modpack deletion")
                deletion_log.append(f"✅ Deleted channel #{channel.name}")
            except Exception as e:
                deletion_log.append(f"❌ Failed to delete channel #{channel.name}: {str(e)}")
                success = False
        
        # Delete category
        try:
            await category.delete(reason="Modpack deletion")
            deletion_log.appendf"✅ Deleted category **{category.name}**")
        except Exception as e:
            deletion_log.append(f"❌ Failed to delete category: {str(e)}")
            success = False
        
        # Delete role and remove from roles board
        if role:
            success_role, message = await self._delete_role_and_update_board(role)
            deletion_log.append(message)
            if not success_role:
                success = False
        
        return success, category.name, deletion_log
    
    async def _delete_role_and_update_board(self, role: discord.Role) -> Tuple[bool, str]:
        """Delete role and remove from roles board."""
        try:
            # Remove from roles board data
            roles_board = self.storage.load(self.roles_board_file, {
                "channel_id": None, "message_id": None, "roles": []
            })
            
            role_index = None
            for i, role_data in enumerate(roles_board["roles"]):
                if role_data["role_id"] == role.id:
                    role_index = i
                    break
            
            if role_index is not None:
                deleted_role_data = roles_board["roles"].pop(role_index)
                self.storage.save(self.roles_board_file, roles_board)
                
                # Remove reaction from roles board if exists
                if roles_board.get("channel_id") and roles_board.get("message_id"):
                    await self._remove_reaction_from_board(role, deleted_role_data["emoji"], roles_board)
            
            # Delete the role
            role_name = role.name
            await role.delete(reason="Modpack deletion")
            return True, f"✅ Deleted role '{role_name}'"
            
        except Exception as e:
            return False, f"❌ Failed to delete role: {str(e)}"
    
    async def _remove_reaction_from_board(self, role: discord.Role, emoji: str, roles_board: dict):
        """Remove reaction from roles board message."""
        try:
            guild = role.guild
            bot = guild.me  # This would need to be passed in properly
            channel = bot.get_channel(roles_board["channel_id"])
            
            if channel:
                message = await channel.fetch_message(roles_board["message_id"])
                await message.clear_reaction(emoji)
                
        except Exception:
            pass  # Non-critical operation