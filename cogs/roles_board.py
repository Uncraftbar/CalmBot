"""
Roles board management for CalmBot.
Handles reaction roles for modpack notifications.
"""

import discord
from discord.ext import commands
from discord import app_commands

from cogs.utils import (
    get_logger,
    load_json,
    save_json,
    check_permissions,
    admin_only,
    success_embed,
    error_embed,
    warning_embed,
    info_embed,
    ROLES_BOARD_FILE,
    REACTION_ROLES_FILE
)

log = get_logger("roles_board")


class SyncRolesView(discord.ui.View):
    """View for selecting invalid roles to remove."""
    
    def __init__(self, bot: commands.Bot, roles_board: dict, invalid_roles: list):
        super().__init__(timeout=180)
        self.bot = bot
        self.roles_board = roles_board
        self.invalid_roles = invalid_roles
        
        options = []
        for i, role_data in enumerate(invalid_roles[:25]):
            label = f"{role_data['name']} ({role_data['emoji']})"[:100]
            desc = role_data.get("error", f"ID: {role_data['role_id']}")[:100]
            options.append(discord.SelectOption(label=label, value=str(i), description=desc))
        
        if options:
            self.select = discord.ui.Select(
                placeholder="Select roles to remove",
                options=options,
                min_values=1,
                max_values=len(options)
            )
            self.select.callback = self.select_callback
            self.add_item(self.select)
    
    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        indices = [int(v) for v in self.select.values]
        roles_to_remove = [self.invalid_roles[i] for i in indices]
        
        # Remove from roles list
        new_roles = []
        for role_data in self.roles_board["roles"]:
            should_keep = True
            for to_remove in roles_to_remove:
                if role_data["role_id"] == to_remove["role_id"]:
                    should_keep = False
                    break
            if should_keep:
                new_roles.append(role_data)
        
        self.roles_board["roles"] = new_roles
        save_json(ROLES_BOARD_FILE, self.roles_board)
        
        # Remove reactions from message
        if self.roles_board.get("channel_id") and self.roles_board.get("message_id"):
            try:
                channel = self.bot.get_channel(self.roles_board["channel_id"])
                if channel:
                    message = await channel.fetch_message(self.roles_board["message_id"])
                    for role_data in roles_to_remove:
                        try:
                            await message.clear_reaction(role_data["emoji"])
                        except Exception:
                            pass
            except Exception:
                pass
        
        # Update board message
        roles_cog = self.bot.get_cog("RolesBoard")
        if roles_cog and hasattr(roles_cog, 'update_roles_board'):
            await roles_cog.update_roles_board()
        
        await interaction.followup.send(
            embed=success_embed("Cleaned Up", f"Removed {len(roles_to_remove)} invalid role(s)."),
            ephemeral=True
        )
        self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Sync cancelled.", ephemeral=True)
        self.stop()


class RolesBoard(commands.Cog):
    """Reaction roles board management."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reaction_roles = load_json(REACTION_ROLES_FILE, {})
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})
        log.info("RolesBoard cog initialized")
    
    def _reload(self):
        """Reload data from disk."""
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})
    
    async def update_roles_board(self) -> bool:
        """Update the roles board message with current roles."""
        self._reload()
        
        if not self.roles_board.get("channel_id") or not self.roles_board.get("message_id"):
            return False
        
        channel = self.bot.get_channel(self.roles_board["channel_id"])
        if not channel:
            return False
        
        try:
            message = await channel.fetch_message(self.roles_board["message_id"])
        except Exception:
            return False
        
        # Build embed
        embed = discord.Embed(
            title="📋 Available Server Roles",
            description="React to get roles for modpack updates and notifications!",
            color=discord.Color.blue()
        )
        
        sorted_roles = sorted(self.roles_board["roles"], key=lambda x: x["name"])
        for role_data in sorted_roles:
            role = channel.guild.get_role(role_data["role_id"])
            if role:
                embed.add_field(
                    name=f"{role_data['emoji']} {role_data['name']}",
                    value=f"React with {role_data['emoji']} for {role.mention}",
                    inline=False
                )
        
        embed.set_footer(text="React to get roles • Managed by CalmBot")
        
        await message.edit(content="", embed=embed)
        
        # Ensure all reactions are present
        for role_data in self.roles_board["roles"]:
            try:
                await message.add_reaction(role_data["emoji"])
            except Exception:
                pass
        
        return True
    
    @app_commands.command(name="sync_roles_board", description="Check and clean up orphaned roles")
    @admin_only()
    async def sync_roles_board(self, interaction: discord.Interaction):
        """Scan for missing or deleted roles and offer cleanup."""
        self._reload()
        guild = interaction.guild
        
        if not guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "Must be used in a server."),
                ephemeral=True
            )
            return
        
        # Check channel/message
        channel_status = "✅ Found"
        message_status = "✅ Found"
        
        channel = guild.get_channel(self.roles_board.get("channel_id")) if self.roles_board.get("channel_id") else None
        
        if not channel:
            channel_status = "❌ Missing"
            message_status = "❌ Missing (no channel)"
        else:
            try:
                message = await channel.fetch_message(self.roles_board["message_id"])
                if not message:
                    message_status = "❌ Missing"
            except Exception:
                message_status = "❌ Missing"
        
        # Check roles
        invalid_roles = []
        valid_count = 0
        
        for role_data in self.roles_board["roles"]:
            role = guild.get_role(role_data["role_id"])
            if not role:
                role_data["error"] = "Role deleted from server"
                invalid_roles.append(role_data)
                continue
            
            # Check for orphaned roles (no matching modpack category)
            role_name = role.name
            modpack_name = role_name[:-8].strip() if role_name.lower().endswith(" updates") else role_name
            
            found_category = False
            for cat in guild.categories:
                if cat.name == modpack_name:
                    found_category = True
                    break
                if "[" in cat.name and "]" in cat.name:
                    base = cat.name.split("[")[0].strip()
                    if base.lower() == modpack_name.lower():
                        found_category = True
                        break
            
            if not found_category:
                role_data["error"] = f"No category '{modpack_name}'"
                invalid_roles.append(role_data)
                continue
            
            valid_count += 1
        
        # Build report
        report = f"**Roles Board Status**\n"
        report += f"• Channel: {channel_status}\n"
        report += f"• Message: {message_status}\n"
        report += f"• Valid Roles: {valid_count}\n"
        report += f"• Invalid/Orphaned: {len(invalid_roles)}\n"
        
        if not invalid_roles:
            await interaction.response.send_message(
                report + "\n✅ All roles are synced!",
                ephemeral=True
            )
            return
        
        report += "\n**Select roles to remove:**"
        await interaction.response.send_message(
            report,
            view=SyncRolesView(self.bot, self.roles_board, invalid_roles),
            ephemeral=True
        )
    
    @app_commands.command(name="setup_roles_board", description="Create or update the roles board message")
    @app_commands.describe(
        channel="Channel for the roles board",
        title="Title for the board (default: 'Modpack Update Notifications')",
        description="Description text"
    )
    @admin_only()
    async def setup_roles_board(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = "Modpack Update Notifications",
        description: str = "React to get roles for modpack updates and notifications"
    ):
        """Create a new roles board or update the existing one."""
        guild = interaction.guild
        
        if not guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "Must be used in a server."),
                ephemeral=True
            )
            return
        
        # Check permissions
        bot_member = guild.get_member(self.bot.user.id)
        if not bot_member:
            await interaction.response.send_message(
                embed=error_embed("Error", "Cannot find bot member."),
                ephemeral=True
            )
            return
        
        perms = channel.permissions_for(bot_member)
        missing = []
        if not perms.send_messages:
            missing.append("Send Messages")
        if not perms.embed_links:
            missing.append("Embed Links")
        if not perms.add_reactions:
            missing.append("Add Reactions")
        if not perms.read_message_history:
            missing.append("Read Message History")
        if not bot_member.guild_permissions.manage_roles:
            missing.append("Manage Roles")
        
        if missing:
            await interaction.response.send_message(
                embed=error_embed(
                    "Missing Permissions",
                    "I need these permissions:\n" + "\n".join(f"• {p}" for p in missing)
                ),
                ephemeral=True
            )
            return
        
        # Delete old message if exists
        if self.roles_board.get("channel_id") and self.roles_board.get("message_id"):
            try:
                old_channel = self.bot.get_channel(self.roles_board["channel_id"])
                if old_channel:
                    old_msg = await old_channel.fetch_message(self.roles_board["message_id"])
                    await old_msg.delete()
            except Exception:
                pass
        
        # Create new embed
        embed = discord.Embed(
            title=f"📋 {title}",
            description=description,
            color=discord.Color.blue()
        )
        
        sorted_roles = sorted(self.roles_board.get("roles", []), key=lambda x: x["name"])
        for role_data in sorted_roles:
            role = guild.get_role(role_data["role_id"])
            if role:
                embed.add_field(
                    name=f"{role_data['emoji']} {role_data['name']}",
                    value=f"React with {role_data['emoji']} for {role.mention}",
                    inline=False
                )
        
        embed.set_footer(text="React to get roles • Managed by CalmBot")
        
        try:
            message = await channel.send(embed=embed)
            
            self.roles_board["channel_id"] = channel.id
            self.roles_board["message_id"] = message.id
            save_json(ROLES_BOARD_FILE, self.roles_board)
            
            # Add reactions
            failed = []
            for role_data in self.roles_board.get("roles", []):
                try:
                    await message.add_reaction(role_data["emoji"])
                except Exception as e:
                    failed.append(f"{role_data['emoji']}: {e}")
            
            result = f"Roles board created in {channel.mention}!"
            if failed:
                result += f"\n⚠️ Failed reactions: {', '.join(failed)}"
            
            await interaction.response.send_message(
                embed=success_embed("Roles Board Created", result),
                ephemeral=True
            )
            log.info(f"Created roles board in {channel.name}")
            
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("Permission Error", f"Cannot send messages in {channel.mention}"),
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                embed=error_embed("Error", str(e)),
                ephemeral=True
            )
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reaction add for role assignment."""
        if payload.user_id == self.bot.user.id:
            return
        
        self._reload()
        
        # Check if it's our roles board
        if not self.roles_board.get("message_id"):
            return
        
        if str(payload.message_id) != str(self.roles_board["message_id"]):
            # Check legacy reaction roles
            await self._handle_legacy_reaction(payload, add=True)
            return
        
        emoji = payload.emoji.name if not payload.emoji.id else str(payload.emoji)
        role_id = None
        
        for role_data in self.roles_board["roles"]:
            if emoji == role_data["emoji"] or emoji == role_data["emoji"].strip():
                role_id = role_data["role_id"]
                break
        
        if not role_id:
            return
        
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        
        role = guild.get_role(role_id)
        if not role:
            return
        
        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return
        
        try:
            await member.add_roles(role, reason="Reaction role from roles board")
            log.debug(f"Added role {role.name} to {member}")
        except Exception as e:
            log.error(f"Failed to add role: {e}")
    
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handle reaction remove for role removal."""
        if payload.user_id == self.bot.user.id:
            return
        
        self._reload()
        
        if not self.roles_board.get("message_id"):
            return
        
        if str(payload.message_id) != str(self.roles_board["message_id"]):
            await self._handle_legacy_reaction(payload, add=False)
            return
        
        emoji = payload.emoji.name if not payload.emoji.id else str(payload.emoji)
        role_id = None
        
        for role_data in self.roles_board["roles"]:
            if role_data["emoji"] == emoji:
                role_id = role_data["role_id"]
                break
        
        if not role_id:
            return
        
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        
        role = guild.get_role(role_id)
        if not role:
            return
        
        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return
        
        try:
            await member.remove_roles(role, reason="Reaction role removed")
            log.debug(f"Removed role {role.name} from {member}")
        except Exception as e:
            log.error(f"Failed to remove role: {e}")
    
    async def _handle_legacy_reaction(self, payload: discord.RawReactionActionEvent, add: bool):
        """Handle legacy reaction roles from separate config."""
        message_id = str(payload.message_id)
        if message_id not in self.reaction_roles:
            return
        
        emoji = payload.emoji.name if not payload.emoji.id else str(payload.emoji)
        if emoji not in self.reaction_roles[message_id]:
            return
        
        role_id = self.reaction_roles[message_id][emoji]
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        
        role = guild.get_role(role_id)
        if not role:
            return
        
        member = guild.get_member(payload.user_id)
        if not member:
            return
        
        try:
            if add:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role removed")
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesBoard(bot))
