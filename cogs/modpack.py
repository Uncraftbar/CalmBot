"""
Modpack management for CalmBot.
Handles modpack category creation, migration, and connection info.
"""

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import (
    get_logger,
    load_json,
    save_json,
    check_permissions,
    admin_only,
    find_category_by_name,
    success_embed,
    error_embed,
    warning_embed,
    info_embed,
    ROLES_BOARD_FILE
)

log = get_logger("modpack")


class ConfirmDeleteView(discord.ui.View):
    """Confirmation view for modpack deletion."""
    
    def __init__(self):
        super().__init__(timeout=60)
        self.confirmed = None
    
    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


class Modpack(commands.Cog):
    """Modpack category and channel management."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})
        log.info("Modpack cog initialized")
    
    def _reload_roles_board(self):
        """Reload roles board data from disk."""
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})
    
    @app_commands.command(name="setup_modpack", description="Create a modpack category with channels and role")
    @app_commands.describe(
        name="Name of the modpack",
        modloader="The modloader for this modpack",
        modpack_link="Link to the modpack",
        connection_ip="Connection IP or URL",
        role_emoji="Emoji for the notification role (optional)"
    )
    @app_commands.choices(modloader=[
        app_commands.Choice(name="NEOFORGE", value="NEOFORGE"),
        app_commands.Choice(name="FORGE", value="FORGE"),
        app_commands.Choice(name="FABRIC", value="FABRIC")
    ])
    @admin_only()
    async def setup_modpack(
        self,
        interaction: discord.Interaction,
        name: str,
        modloader: str,
        modpack_link: str,
        connection_ip: str,
        role_emoji: str = None
    ):
        """Create a new modpack with category, channels, and optional role."""
        self._reload_roles_board()
        guild = interaction.guild
        
        if not guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "This command must be used in a server."),
                ephemeral=True
            )
            return
        
        category_name = f"{name} [{modloader}]"
        await interaction.response.send_message(
            f"⏳ Setting up **{category_name}**...",
            ephemeral=True
        )
        
        # Check if category exists
        if discord.utils.get(guild.categories, name=category_name):
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Already Exists", f"Category **{category_name}** already exists.")
            )
            return
        
        try:
            # Create category and channels
            category = await guild.create_category(category_name)
            await guild.create_text_channel("general", category=category)
            await guild.create_text_channel("technical-help", category=category)
            
            # Create connection-info with restricted permissions
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                guild.me: discord.PermissionOverwrite(send_messages=True)
            }
            connection_info = await guild.create_text_channel(
                "connection-info",
                overwrites=overwrites,
                category=category
            )
            
            # Send connection info message
            await connection_info.send(
                f"**Modpack URL:** {modpack_link}\n**Connection URL:** {connection_ip}"
            )
            
            # Handle role creation
            role_message = ""
            if role_emoji:
                role_message = await self._create_modpack_role(guild, name, role_emoji)
            
            await interaction.edit_original_response(
                content=None,
                embed=success_embed(
                    "Modpack Created",
                    f"Created **{category_name}** with channels.{role_message}"
                )
            )
            log.info(f"Created modpack: {category_name}")
            
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Permission Error", "I don't have permission to create channels.")
            )
        except Exception as e:
            log.error(f"Failed to create modpack: {e}")
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Error", f"Failed to create modpack: {e}")
            )
    
    async def _create_modpack_role(self, guild: discord.Guild, name: str, emoji: str) -> str:
        """Create a modpack notification role and add to roles board."""
        try:
            role = await guild.create_role(
                name=f"{name} Updates",
                mentionable=True,
                reason="Modpack Updates Role"
            )
            
            if not self.roles_board.get("channel_id") or not self.roles_board.get("message_id"):
                return f"\n⚠️ Created role **{role.name}** but no roles board configured. Use `/setup_roles_board`."
            
            # Add to roles board
            roles_channel = self.bot.get_channel(self.roles_board["channel_id"])
            if not roles_channel:
                return f"\n⚠️ Created role **{role.name}** but roles board channel not found."
            
            try:
                message = await roles_channel.fetch_message(self.roles_board["message_id"])
                await message.add_reaction(emoji)
                
                self.roles_board["roles"].append({
                    "name": f"{name} Updates",
                    "emoji": emoji,
                    "role_id": role.id
                })
                save_json(ROLES_BOARD_FILE, self.roles_board)
                
                # Update roles board message
                roles_cog = self.bot.get_cog("RolesBoard")
                if roles_cog:
                    await roles_cog.update_roles_board()
                
                return f"\n✅ Created role **{role.name}** with reaction {emoji}"
                
            except discord.HTTPException:
                return f"\n⚠️ Created role **{role.name}** but `{emoji}` is not a valid emoji."
                
        except discord.Forbidden:
            return "\n⚠️ I don't have permission to create roles."
        except Exception as e:
            return f"\n⚠️ Error creating role: {e}"
    
    @app_commands.command(name="delete_modpack", description="Delete a modpack category and its role")
    @app_commands.describe(category_name="Name of the modpack (with or without [MODLOADER])")
    @admin_only()
    async def delete_modpack(self, interaction: discord.Interaction, category_name: str):
        """Delete a modpack category, channels, and associated role."""
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "This command must be used in a server."),
                ephemeral=True
            )
            return
        
        await interaction.response.send_message(
            f"⏳ Searching for modpack **{category_name}**...",
            ephemeral=True
        )
        
        category = await find_category_by_name(guild, category_name)
        if not category:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Not Found", f"Category **{category_name}** not found.")
            )
            return
        
        actual_name = category.name
        
        # Find associated role
        role = None
        role_name = None
        if "[" in actual_name and "]" in actual_name:
            modpack_name = actual_name.split("[")[0].strip()
            role_name = f"{modpack_name} Updates"
            role = discord.utils.get(guild.roles, name=role_name)
        
        # Build confirmation message
        channels = [f"#{c.name}" for c in category.channels]
        confirm_msg = f"**You are about to delete:**\n"
        confirm_msg += f"• Category: **{actual_name}**\n"
        confirm_msg += f"• Channels: {', '.join(channels)}\n"
        if role:
            confirm_msg += f"• Role: **{role.name}**\n"
        
        view = ConfirmDeleteView()
        await interaction.edit_original_response(content=confirm_msg, view=view)
        await view.wait()
        
        if not view.confirmed:
            await interaction.edit_original_response(
                content=None,
                embed=info_embed("Cancelled", "Deletion cancelled."),
                view=None
            )
            return
        
        await interaction.edit_original_response(
            content=f"⏳ Deleting **{actual_name}**...",
            view=None
        )
        
        # Delete channels and category
        results = []
        for channel in category.channels:
            try:
                await channel.delete(reason=f"Modpack deletion by {interaction.user}")
                results.append(f"✅ Deleted #{channel.name}")
            except Exception as e:
                results.append(f"❌ Failed #{channel.name}: {e}")
        
        try:
            await category.delete(reason=f"Modpack deletion by {interaction.user}")
            results.append(f"✅ Deleted category **{actual_name}**")
        except Exception as e:
            results.append(f"❌ Failed category: {e}")
        
        # Delete role and clean up roles board
        if role:
            try:
                # Remove from roles board
                self._reload_roles_board()
                role_data = None
                for i, rd in enumerate(self.roles_board["roles"]):
                    if rd["role_id"] == role.id:
                        role_data = self.roles_board["roles"].pop(i)
                        save_json(ROLES_BOARD_FILE, self.roles_board)
                        break
                
                # Remove reaction if possible
                if role_data and self.roles_board.get("channel_id") and self.roles_board.get("message_id"):
                    try:
                        channel = self.bot.get_channel(self.roles_board["channel_id"])
                        if channel:
                            message = await channel.fetch_message(self.roles_board["message_id"])
                            await message.clear_reaction(role_data["emoji"])
                    except Exception:
                        pass
                
                # Update roles board
                roles_cog = self.bot.get_cog("RolesBoard")
                if roles_cog:
                    await roles_cog.update_roles_board()
                
                await role.delete(reason=f"Modpack deletion by {interaction.user}")
                results.append(f"✅ Deleted role **{role.name}**")
            except Exception as e:
                results.append(f"❌ Failed role: {e}")
        
        success = all("✅" in r for r in results)
        embed = success_embed if success else warning_embed
        
        await interaction.edit_original_response(
            content=None,
            embed=embed(
                "Deletion Complete" if success else "Deletion Partial",
                "\n".join(results)
            )
        )
        log.info(f"Deleted modpack: {actual_name}")
    
    @app_commands.command(name="migrate_modpack", description="Migrate an existing category to the bot's system")
    @app_commands.describe(
        category_name="Name of existing category",
        role_name="Name for the notification role",
        modpack_link="Link to the modpack",
        connection_ip="Connection IP or URL",
        role_emoji="Emoji for the role"
    )
    @admin_only()
    async def migrate_modpack(
        self,
        interaction: discord.Interaction,
        category_name: str,
        role_name: str,
        modpack_link: str,
        connection_ip: str,
        role_emoji: str
    ):
        """Convert an existing manual category to bot-managed."""
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "This command must be used in a server."),
                ephemeral=True
            )
            return
        
        await interaction.response.send_message(
            f"⏳ Migrating **{category_name}**...",
            ephemeral=True
        )
        
        category = await find_category_by_name(guild, category_name)
        if not category:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Not Found", f"Category **{category_name}** not found.")
            )
            return
        
        actual_name = category.name
        
        # Create or find connection-info channel
        connection_info = discord.utils.get(category.channels, name="connection-info")
        if not connection_info:
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                    guild.me: discord.PermissionOverwrite(send_messages=True)
                }
                connection_info = await guild.create_text_channel(
                    "connection-info",
                    overwrites=overwrites,
                    category=category
                )
            except Exception as e:
                await interaction.edit_original_response(
                    content=None,
                    embed=error_embed("Error", f"Failed to create connection-info channel: {e}")
                )
                return
        
        # Send connection info
        try:
            await connection_info.send(
                f"**Modpack URL:** {modpack_link}\n**Connection URL:** {connection_ip}"
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Error", f"Failed to send connection info: {e}")
            )
            return
        
        # Create role
        role_message = ""
        try:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                role_message = f"\n⚠️ Role **{role_name}** already exists."
            else:
                role = await guild.create_role(name=role_name, mentionable=True, reason="Modpack Migration")
                role_message = f"\n✅ Created role **{role_name}**"
            
            # Add to roles board
            self._reload_roles_board()
            
            # Check if already in board
            if any(rd["role_id"] == role.id for rd in self.roles_board["roles"]):
                role_message += f"\n⚠️ Role already on roles board."
            elif self.roles_board.get("channel_id") and self.roles_board.get("message_id"):
                try:
                    channel = self.bot.get_channel(self.roles_board["channel_id"])
                    if channel:
                        message = await channel.fetch_message(self.roles_board["message_id"])
                        await message.add_reaction(role_emoji)
                        
                        self.roles_board["roles"].append({
                            "name": role_name,
                            "emoji": role_emoji,
                            "role_id": role.id
                        })
                        save_json(ROLES_BOARD_FILE, self.roles_board)
                        
                        roles_cog = self.bot.get_cog("RolesBoard")
                        if roles_cog:
                            await roles_cog.update_roles_board()
                        
                        role_message += f"\n✅ Added to roles board with {role_emoji}"
                except discord.HTTPException:
                    role_message += f"\n⚠️ `{role_emoji}` is not a valid emoji."
            else:
                role_message += f"\n⚠️ No roles board configured."
                
        except discord.Forbidden:
            role_message = "\n⚠️ I don't have permission to create roles."
        except Exception as e:
            role_message = f"\n⚠️ Error with role: {e}"
        
        await interaction.edit_original_response(
            content=None,
            embed=success_embed(
                "Migration Complete",
                f"Migrated **{actual_name}** to bot management.{role_message}"
            )
        )
        log.info(f"Migrated modpack: {actual_name}")
    
    @app_commands.command(name="edit_connection_info", description="Edit the connection info message")
    @app_commands.describe(
        category_name="Name of the modpack",
        modpack_link="New modpack link (leave empty to keep current)",
        connection_ip="New connection IP (leave empty to keep current)",
        additional_info="Additional info (use 'REMOVE' to clear)"
    )
    @admin_only()
    async def edit_connection_info(
        self,
        interaction: discord.Interaction,
        category_name: str,
        modpack_link: str = None,
        connection_ip: str = None,
        additional_info: str = None
    ):
        """Update the connection info message in a modpack category."""
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "This command must be used in a server."),
                ephemeral=True
            )
            return
        
        await interaction.response.send_message(
            f"⏳ Updating connection info for **{category_name}**...",
            ephemeral=True
        )
        
        category = await find_category_by_name(guild, category_name)
        if not category:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Not Found", f"Category **{category_name}** not found.")
            )
            return
        
        connection_info = discord.utils.get(category.channels, name="connection-info")
        if not connection_info:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed(
                    "Not Found",
                    f"No connection-info channel in **{category.name}**.\nUse `/migrate_modpack` first."
                )
            )
            return
        
        # Find existing bot message
        last_message = None
        current_modpack = None
        current_connection = None
        current_additional = None
        
        async for message in connection_info.history(limit=10):
            if message.author == self.bot.user and "Modpack URL:" in message.content:
                last_message = message
                content = message.content
                
                if "**Modpack URL:**" in content:
                    current_modpack = content.split("**Modpack URL:**")[1].split("\n")[0].strip()
                if "**Connection URL:**" in content:
                    current_connection = content.split("**Connection URL:**")[1].split("\n")[0].strip()
                if "**Additional Information:**" in content:
                    current_additional = content.split("**Additional Information:**")[1].strip()
                break
        
        # Build new message
        final_modpack = modpack_link or current_modpack
        final_connection = connection_ip or current_connection
        
        if not final_modpack and not final_connection:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("No Data", "No connection info found and no new values provided.")
            )
            return
        
        new_content = ""
        if final_modpack:
            new_content += f"**Modpack URL:** {final_modpack}\n"
        if final_connection:
            new_content += f"**Connection URL:** {final_connection}"
        
        if additional_info:
            if additional_info.upper() != "REMOVE":
                new_content += f"\n\n**Additional Information:**\n{additional_info}"
        elif current_additional:
            new_content += f"\n\n**Additional Information:**\n{current_additional}"
        
        # Update or create message
        try:
            if last_message:
                await last_message.edit(content=new_content)
                action = "Updated"
            else:
                await connection_info.send(new_content)
                action = "Created"
            
            await interaction.edit_original_response(
                content=None,
                embed=success_embed(
                    "Connection Info Updated",
                    f"{action} connection info in {connection_info.mention}"
                )
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Permission Error", "I can't edit messages in that channel.")
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=None,
                embed=error_embed("Error", str(e))
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Modpack(bot))
