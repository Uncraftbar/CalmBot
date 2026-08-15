"""
Roles board management for CalmBot.
Handles reaction roles for game and server notifications.
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


class RebuildReactionsView(discord.ui.View):
    """Confirmation prompt for the one destructive editor operation."""

    def __init__(self, editor):
        super().__init__(timeout=60)
        self.editor = editor

    @discord.ui.button(label="Rebuild reaction order", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.editor.owner_id:
            await interaction.response.send_message("This editor belongs to another admin.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.editor.cog._get_board_message()
        if not message:
            await interaction.followup.send(embed=error_embed("Board Missing", "The configured role-board message could not be found."), ephemeral=True)
            return
        await message.clear_reactions()
        failed = []
        for item in self.editor.cog.roles_board.get("roles", []):
            try:
                await message.add_reaction(item["emoji"])
            except Exception:
                failed.append(item.get("name", "Unknown"))
        text = "Reaction buttons were rebuilt in the editor's current order. Existing user reaction marks were cleared; their Discord roles were not removed."
        if failed:
            text += f" Failed to add: {', '.join(failed)}."
        await interaction.edit_original_response(embed=success_embed("Reaction Order Rebuilt", text), view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Reaction rebuild cancelled.", embed=None, view=None)
        self.stop()


class RolesBoardEditorView(discord.ui.View):
    """Admin editor for selecting, removing, and ordering every board entry."""

    def __init__(self, cog, owner_id: int, guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.guild = guild
        self.selected_index = 0 if cog.roles_board.get("roles") else None
        self._build_items()

    def _build_items(self):
        self.clear_items()
        roles = self.cog.roles_board.get("roles", [])
        if roles:
            self.selected_index = min(self.selected_index if self.selected_index is not None else 0, len(roles) - 1)
            options = []
            guild = self.guild
            for index, item in enumerate(roles[:25]):
                role = guild.get_role(item.get("role_id")) if guild else None
                status = role.name if role else "Discord role missing"
                options.append(discord.SelectOption(
                    label=f"{index + 1}. {item.get('emoji', '?')} {item.get('name', 'Unnamed')}"[:100],
                    value=str(index),
                    description=f"{status} · ID {item.get('role_id', '?')}"[:100],
                    default=index == self.selected_index,
                ))
            select = discord.ui.Select(placeholder="Choose a board entry", options=options)
            select.callback = self._select
            self.add_item(select)

        up = discord.ui.Button(label="Move up", emoji="⬆️", style=discord.ButtonStyle.primary, disabled=not roles)
        down = discord.ui.Button(label="Move down", emoji="⬇️", style=discord.ButtonStyle.primary, disabled=not roles)
        remove = discord.ui.Button(label="Remove entry", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=not roles)
        rebuild = discord.ui.Button(label="Rebuild reaction order", emoji="🔄", style=discord.ButtonStyle.secondary, disabled=not roles)
        close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary)
        up.callback = self._move_up
        down.callback = self._move_down
        remove.callback = self._remove
        rebuild.callback = self._rebuild
        close.callback = self._close
        for button in (up, down, remove, rebuild, close):
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This editor belongs to another admin.", ephemeral=True)
            return False
        return True

    def render(self):
        roles = self.cog.roles_board.get("roles", [])
        lines = []
        guild = self.guild
        for index, item in enumerate(roles):
            marker = "➡️" if index == self.selected_index else "•"
            role = guild.get_role(item.get("role_id")) if guild else None
            suffix = role.mention if role else "**missing role**"
            lines.append(f"{marker} `{index + 1:02}` {item.get('emoji', '?')} **{item.get('name', 'Unnamed')}** — {suffix}")
        body = "\n".join(lines) if lines else "*The board has no entries.*"
        embed = info_embed("Role Board Editor", body)
        embed.set_footer(text="Changes save immediately. Rebuild only if the reaction buttons must match the new order.")
        return embed

    async def _select(self, interaction: discord.Interaction):
        self.selected_index = int(interaction.data["values"][0])
        self._build_items()
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def _persist_and_refresh(self, interaction: discord.Interaction):
        save_json(ROLES_BOARD_FILE, self.cog.roles_board)
        updated = await self.cog.update_roles_board()
        self.cog._reload()
        self._build_items()
        if updated:
            await interaction.response.edit_message(embed=self.render(), view=self)
        else:
            await interaction.response.edit_message(embed=warning_embed("Saved, Board Message Missing", "The order was saved, but CalmBot could not update the configured board message."), view=self)

    async def _move_up(self, interaction: discord.Interaction):
        if self.selected_index is not None and self.selected_index > 0:
            roles = self.cog.roles_board["roles"]
            roles[self.selected_index - 1], roles[self.selected_index] = roles[self.selected_index], roles[self.selected_index - 1]
            self.selected_index -= 1
        await self._persist_and_refresh(interaction)

    async def _move_down(self, interaction: discord.Interaction):
        roles = self.cog.roles_board.get("roles", [])
        if self.selected_index is not None and self.selected_index < len(roles) - 1:
            roles[self.selected_index + 1], roles[self.selected_index] = roles[self.selected_index], roles[self.selected_index + 1]
            self.selected_index += 1
        await self._persist_and_refresh(interaction)

    async def _remove(self, interaction: discord.Interaction):
        roles = self.cog.roles_board.get("roles", [])
        if self.selected_index is None or self.selected_index >= len(roles):
            await interaction.response.edit_message(embed=self.render(), view=self)
            return
        removed = roles.pop(self.selected_index)
        self.selected_index = min(self.selected_index, len(roles) - 1) if roles else None
        save_json(ROLES_BOARD_FILE, self.cog.roles_board)
        message = await self.cog._get_board_message()
        # Do not clear an emoji still used by another entry.
        if message and not any(item.get("emoji") == removed.get("emoji") for item in roles):
            try:
                await message.clear_reaction(removed["emoji"])
            except Exception as exc:
                log.warning(f"Could not clear removed role reaction: {exc}")
        await self.cog.update_roles_board()
        self.cog._reload()
        self._build_items()
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def _rebuild(self, interaction: discord.Interaction):
        warning = warning_embed(
            "Rebuild Reaction Order?",
            "Discord cannot reorder reaction buttons in place. This clears all reaction marks and adds the buttons again in the board order. Existing member roles are **not** removed.",
        )
        await interaction.response.send_message(embed=warning, view=RebuildReactionsView(self), ephemeral=True)

    async def _close(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=self.render(), view=self)
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
            description="React to get roles for game and server updates!",
            color=discord.Color.blue()
        )
        
        # The stored list is the administrator-defined display order.
        for role_data in self.roles_board["roles"]:
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
    
    async def _get_board_message(self):
        """Return the configured board message, or None when it is unavailable."""
        self._reload()
        channel_id = self.roles_board.get("channel_id")
        message_id = self.roles_board.get("message_id")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not channel or not message_id:
            return None
        try:
            return await channel.fetch_message(message_id)
        except Exception:
            return None

    @app_commands.command(name="edit_roles_board", description="Remove and rearrange role-board entries")
    @admin_only()
    async def edit_roles_board(self, interaction: discord.Interaction):
        """Open the complete role-board editor; all entries are selectable."""
        self._reload()
        if not interaction.guild:
            await interaction.response.send_message(embed=error_embed("Error", "Must be used in a server."), ephemeral=True)
            return
        view = RolesBoardEditorView(self, interaction.user.id, interaction.guild)
        await interaction.response.send_message(embed=view.render(), view=view, ephemeral=True)

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
        description: str = "React to get roles for game and server updates"
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
        
        # Keep the old board live until its replacement has been sent and saved.
        old_channel_id = self.roles_board.get("channel_id")
        old_message_id = self.roles_board.get("message_id")

        # Create new embed
        embed = discord.Embed(
            title=f"📋 {title}",
            description=description,
            color=discord.Color.blue()
        )
        
        for role_data in self.roles_board.get("roles", []):
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
            
            updated_board = dict(self.roles_board)
            updated_board["channel_id"] = channel.id
            updated_board["message_id"] = message.id
            save_json(ROLES_BOARD_FILE, updated_board)
            self.roles_board = updated_board

            # Add reactions
            failed = []
            for role_data in self.roles_board.get("roles", []):
                try:
                    await message.add_reaction(role_data["emoji"])
                except Exception as e:
                    failed.append(f"{role_data['emoji']}: {e}")
            
            # The replacement is durable now; deleting the old board is best-effort.
            if old_channel_id and old_message_id and old_message_id != message.id:
                try:
                    old_channel = self.bot.get_channel(old_channel_id)
                    if old_channel:
                        old_msg = await old_channel.fetch_message(old_message_id)
                        await old_msg.delete()
                except Exception as e:
                    log.warning(f"Could not delete old roles board message: {e}")

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
