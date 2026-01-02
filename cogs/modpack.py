import discord
from discord import app_commands
from discord.ext import commands
from cogs.utils import has_admin_or_mod_permissions, find_category_by_name, load_json, save_json

ROLES_BOARD_FILE = "roles_board.json"

class Modpack(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})

    def reload_roles_board(self):
        # Always reload the latest roles_board.json from disk
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})

    @app_commands.command(name="setup_modpack", description="Set up modpack category and channels")
    @app_commands.describe(
        name="Name of the modpack",
        modloader="The modloader for this modpack",
        modpack_link="Link to the modpack",
        connection_ip="Connection IP or URL",
        role_emoji="Emoji for users to react with to get the role"
    )
    @app_commands.choices(modloader=[
        app_commands.Choice(name="NEOFORGE", value="NEOFORGE"),
        app_commands.Choice(name="FORGE", value="FORGE"),
        app_commands.Choice(name="FABRIC", value="FABRIC")
    ])
    async def setup_modpack(self, interaction: discord.Interaction, name: str, modloader: str, modpack_link: str, connection_ip: str, role_emoji: str = None):
        self.reload_roles_board()  # Ensure latest roles board config
        if not await has_admin_or_mod_permissions(interaction):
            return
        guild = interaction.guild
        category_name = f"{name} [{modloader}]"
        await interaction.response.send_message(f"Setting up **{category_name}**...", ephemeral=True)
        if discord.utils.get(guild.categories, name=category_name):
            await interaction.edit_original_response(content=f"? Category **{category_name}** already exists.")
            return
        category = await guild.create_category(category_name)
        general = await guild.create_text_channel("general", category=category)
        await guild.create_text_channel("technical-help", category=category)
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                guild.me: discord.PermissionOverwrite(send_messages=True)
            }
            connection_info = await guild.create_text_channel("connection-info", overwrites=overwrites, category=category)
            msg = f"**Modpack URL:** {modpack_link}\n**Connection URL:** {connection_ip}"
            await connection_info.send(msg)
            role_message = ""
            if role_emoji:
                try:
                    role = await guild.create_role(name=f"{name} Updates", mentionable=True, reason="Modpack Updates Role")
                    if not self.roles_board["channel_id"] or not self.roles_board["message_id"]:
                        role_message = f"\n⚠️ Created role '{role.name}' but no roles board is configured. Use /setup_roles_board to create one."
                    else:
                        roles_channel = self.bot.get_channel(self.roles_board["channel_id"])
                        if roles_channel:
                            message = await roles_channel.fetch_message(self.roles_board["message_id"])
                            try:
                                await message.add_reaction(role_emoji)
                                emoji_key = role_emoji
                                self.roles_board["roles"].append({
                                    "name": f"{name} Updates",
                                    "emoji": emoji_key,
                                    "role_id": role.id
                                })
                                save_json(ROLES_BOARD_FILE, self.roles_board)
                                roles_cog = self.bot.get_cog("RolesBoard")
                                if roles_cog and await roles_cog.update_roles_board():
                                    role_message = f"\n✅ Created role '{role.name}' with reaction {role_emoji} on the roles board"
                                else:
                                    role_message = f"\n⚠️ Created role '{role.name}' but could not update the roles board"
                            except discord.HTTPException:
                                role_message = f"\n⚠️ Created role '{role.name}' but '{role_emoji}' is not a valid emoji"
                        else:
                            role_message = f"\n⚠️ Created role '{role.name}' but could not find the roles board channel"
                except discord.Forbidden:
                    role_message = "\n⚠️ I don't have permission to create roles."
                except Exception as e:
                    role_message = f"\n⚠️ Error creating role: {str(e)}"
            await interaction.edit_original_response(content=f"✅ Created **{category_name}** with required channels.{role_message}")
        except discord.Forbidden as e:
            await interaction.edit_original_response(content=f"⚠️ Cannot create or use connection-info channel properly. Error: {str(e)}")
            return

    @app_commands.command(name="delete_modpack", description="Delete a modpack category, all its channels, and associated role")
    @app_commands.describe(
        category_name="The name of the modpack (with or without [MODLOADER])"
    )
    async def delete_modpack(self, interaction: discord.Interaction, category_name: str):
        guild = interaction.guild
        if not await has_admin_or_mod_permissions(interaction):
            return
        await interaction.response.send_message(f"Searching for modpack **{category_name}**...", ephemeral=True)
        category = await find_category_by_name(guild, category_name)
        if not category:
            await interaction.edit_original_response(content=f"❌ Category containing **{category_name}** not found.")
            return
        actual_category_name = category.name
        role_name = None
        if "[" in actual_category_name and "]" in actual_category_name:
            modpack_name = actual_category_name.split("[")[0].strip()
            role_name = f"{modpack_name} Updates"
        role = discord.utils.get(guild.roles, name=role_name) if role_name else None
        channels_info = [f"#{channel.name}" for channel in category.channels]
        confirmation_msg = f"**Warning!** You are about to delete:\n"
        confirmation_msg += f"• Category: **{actual_category_name}**\n"
        confirmation_msg += f"• Channels ({len(channels_info)}): {', '.join(channels_info)}\n"
        emoji_to_remove = None
        if role:
            confirmation_msg += f"• Role: **{role.name}**\n"
            for role_data in self.roles_board["roles"]:
                if role_data["role_id"] == role.id:
                    emoji_to_remove = role_data["emoji"]
                    confirmation_msg += f"• Role emoji: {emoji_to_remove} will be removed from roles board\n"
                    break
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.confirmed = None
            @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
            async def confirm_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                self.confirmed = True
                self.stop()
                await button_interaction.response.defer()
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                self.confirmed = False
                self.stop()
                await button_interaction.response.defer()
        view = ConfirmView()
        await interaction.edit_original_response(content=confirmation_msg, view=view)
        await view.wait()
        if not view.confirmed:
            await interaction.edit_original_response(content="Deletion cancelled.", view=None)
            return
        await interaction.edit_original_response(content=f"Deleting modpack **{actual_category_name}**...", view=None)
        deletion_log = []
        success = True
        for channel in category.channels:
            try:
                await channel.delete(reason=f"Modpack deletion requested by {interaction.user.display_name}")
                deletion_log.append(f"✅ Deleted channel #{channel.name}")
            except Exception as e:
                deletion_log.append(f"❌ Failed to delete channel #{channel.name}: {str(e)}")
                success = False
        try:
            await category.delete(reason=f"Modpack deletion requested by {interaction.user.display_name}")
            deletion_log.append(f"✅ Deleted category **{actual_category_name}**")
        except Exception as e:
            deletion_log.append(f"❌ Failed to delete category **{actual_category_name}**: {str(e)}")
            success = False
        if role:
            try:
                role_index_to_remove = None
                for i, role_data in enumerate(self.roles_board["roles"]):
                    if role_data["role_id"] == role.id:
                        role_index_to_remove = i
                        break
                if role_index_to_remove is not None:
                    deleted_role_data = self.roles_board["roles"].pop(role_index_to_remove)
                    save_json(ROLES_BOARD_FILE, self.roles_board)
                    if self.roles_board["channel_id"] and self.roles_board["message_id"]:
                        try:
                            channel = self.bot.get_channel(self.roles_board["channel_id"])
                            if channel:
                                message = await channel.fetch_message(self.roles_board["message_id"])
                                if message:
                                    try:
                                        await message.clear_reaction(deleted_role_data["emoji"])
                                        deletion_log.append(f"✅ Removed reaction {deleted_role_data['emoji']} from roles board")
                                    except Exception as e:
                                        deletion_log.append(f"⚠️ Could not remove reaction: {str(e)}")
                        except Exception as e:
                            deletion_log.append(f"⚠️ Could not access roles board: {str(e)}")
                    roles_cog = self.bot.get_cog("RolesBoard")
                    update_result = roles_cog and await roles_cog.update_roles_board()
                    if update_result:
                        deletion_log.append("✅ Updated roles board message")
                    else:
                        deletion_log.append("⚠️ Failed to update roles board message")
                role_name = role.name
                await role.delete(reason=f"Modpack deletion requested by {interaction.user.display_name}")
                deletion_log.append(f"✅ Deleted role '{role_name}'")
            except Exception as e:
                deletion_log.append(f"❌ Failed to delete role: {str(e)}")
                success = False
        summary = "\n".join(deletion_log)
        status_emoji = "✅" if success else "⚠️"
        await interaction.edit_original_response(
            content=f"{status_emoji} Modpack **{actual_category_name}** deletion summary:\n\n{summary}"
        )

    @app_commands.command(name="migrate_modpack", description="Migrate an existing manually created modpack category to the bot")
    @app_commands.describe(
        category_name="Name of the existing modpack (with or without [MODLOADER])",
        role_name="Name for the notification role (e.g. 'ATM10 Updates')",
        modpack_link="Link to the modpack",
        connection_ip="Connection IP or URL",
        role_emoji="Emoji for users to react with to get the role"
    )
    async def migrate_modpack(self, interaction: discord.Interaction, category_name: str, role_name: str, modpack_link: str, connection_ip: str, role_emoji: str):
        if not await has_admin_or_mod_permissions(interaction):
            return
        guild = interaction.guild
        await interaction.response.send_message(f"Searching for category **{category_name}**...", ephemeral=True)
        category = await find_category_by_name(guild, category_name)
        if not category:
            await interaction.edit_original_response(content=f"❌ Category containing **{category_name}** not found.")
            return
        actual_category_name = category.name
        connection_info = discord.utils.get(category.channels, name="connection-info")
        if not connection_info:
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
                    guild.me: discord.PermissionOverwrite(send_messages=True)
                }
                connection_info = await guild.create_text_channel("connection-info", overwrites=overwrites, category=category)
                await interaction.edit_original_response(content=f"Created missing connection-info channel in **{actual_category_name}**...")
            except discord.Forbidden as e:
                await interaction.edit_original_response(content=f"⚠️ Cannot create connection-info channel. Error: {str(e)}")
                return
            except Exception as e:
                await interaction.edit_original_response(content=f"⚠️ Error creating connection-info channel: {str(e)}")
                return
        try:
            msg = f"**Modpack URL:** {modpack_link}\n**Connection URL:** {connection_ip}"
            await connection_info.send(msg)
        except Exception as e:
            await interaction.edit_original_response(content=f"⚠️ Error posting info to connection-info channel: {str(e)}")
            return
        role_message = ""
        try:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                role_message = f"\n⚠️ Role '{role_name}' already exists - using existing role."
            else:
                role = await guild.create_role(name=role_name, mentionable=True, reason="Modpack Updates Role")
                role_message = f"\n✅ Created role '{role_name}'."
            if not self.roles_board["channel_id"] or not self.roles_board["message_id"]:
                role_message += f"\n⚠️ No roles board is configured. Use /setup_roles_board to create one."
            else:
                for role_data in self.roles_board["roles"]:
                    if role_data["role_id"] == role.id:
                        await interaction.edit_original_response(
                            content=f"✅ Successfully migrated **{category_name}**.\n⚠️ Role '{role_name}' already exists on the roles board with emoji {role_data['emoji']}."
                        )
                        return
                roles_channel = self.bot.get_channel(self.roles_board["channel_id"])
                if roles_channel:
                    message = await roles_channel.fetch_message(self.roles_board["message_id"])
                    try:
                        await message.add_reaction(role_emoji)
                        emoji_key = role_emoji
                        self.roles_board["roles"].append({
                            "name": role_name,
                            "emoji": emoji_key,
                            "role_id": role.id
                        })
                        save_json(ROLES_BOARD_FILE, self.roles_board)
                        roles_cog = self.bot.get_cog("RolesBoard")
                        if roles_cog and await roles_cog.update_roles_board():
                            role_message += f"\n✅ Added role to the roles board with reaction {role_emoji}."
                        else:
                            role_message += f"\n⚠️ Could not update the roles board after adding role."
                    except discord.HTTPException:
                        role_message += f"\n⚠️ '{role_emoji}' is not a valid emoji - role not added to roles board."
                else:
                    role_message += f"\n⚠️ Could not find the roles board channel."
        except discord.Forbidden:
            await interaction.edit_original_response(content=f"⚠️ I don't have permission to create roles.")
            return
        except Exception as e:
            await interaction.edit_original_response(content=f"⚠️ Error during migration: {str(e)}")
            return
        await interaction.edit_original_response(
            content=f"✅ Successfully migrated **{actual_category_name}** to the bot system.{role_message}"
        )

    @app_commands.command(name="edit_connection_info", description="Edit the connection info message in a modpack category")
    @app_commands.describe(
        category_name="Name of the modpack (with or without [MODLOADER])",
        modpack_link="New link to the modpack (leave empty to keep current)",
        connection_ip="New connection IP or URL (leave empty to keep current)",
        additional_info="Optional additional information (use 'REMOVE' to clear existing info)"
    )
    async def edit_connection_info(self, interaction: discord.Interaction, category_name: str, modpack_link: str = None, connection_ip: str = None, additional_info: str = None):
        if not await has_admin_or_mod_permissions(interaction):
            return
        guild = interaction.guild
        await interaction.response.send_message(f"Looking for connection info in **{category_name}**...", ephemeral=True)
        category = await find_category_by_name(guild, category_name)
        if not category:
            await interaction.edit_original_response(content=f"❌ Category containing **{category_name}** not found.")
            return
        actual_category_name = category.name
        connection_info = discord.utils.get(category.channels, name="connection-info")
        if not connection_info:
            await interaction.edit_original_response(
                content=f"❌ No `connection-info` channel found in **{actual_category_name}** category.\nUse `/migrate_modpack` to set up a proper connection-info channel first."
            )
            return
        bot_member = guild.get_member(self.bot.user.id)
        channel_perms = connection_info.permissions_for(bot_member)
        if not channel_perms.send_messages:
            await interaction.edit_original_response(
                content=f"❌ Bot doesn't have permission to send messages in {connection_info.mention}."
            )
            return
        try:
            last_bot_message = None
            current_modpack_link = None
            current_connection_ip = None
            current_additional_info = None
            async for message in connection_info.history(limit=10):
                if message.author == self.bot.user and "Modpack URL:" in message.content:
                    last_bot_message = message
                    content = message.content
                    if "**Modpack URL:**" in content:
                        modpack_part = content.split("**Modpack URL:**")[1].split("\n")[0].strip()
                        current_modpack_link = modpack_part
                    if "**Connection URL:**" in content:
                        connection_part = content.split("**Connection URL:**")[1].split("\n")[0].strip()
                        current_connection_ip = connection_part
                    if "**Additional Information:**" in content:
                        additional_part = content.split("**Additional Information:**")[1].strip()
                        current_additional_info = additional_part
                    break
            final_modpack_link = modpack_link if modpack_link is not None else current_modpack_link
            final_connection_ip = connection_ip if connection_ip is not None else current_connection_ip
            if final_modpack_link is None and final_connection_ip is None:
                await interaction.edit_original_response(
                    content=f"❌ No existing connection info found and no new values provided."
                )
                return
            new_info_msg = ""
            if final_modpack_link is not None:
                new_info_msg += f"**Modpack URL:** {final_modpack_link}\n"
            if final_connection_ip is not None:
                new_info_msg += f"**Connection URL:** {final_connection_ip}"
            if additional_info is not None:
                if additional_info.upper() == "REMOVE":
                    pass
                else:
                    new_info_msg += f"\n\n**Additional Information:**\n{additional_info}"
            elif current_additional_info is not None:
                new_info_msg += f"\n\n**Additional Information:**\n{current_additional_info}"
            changes = []
            if modpack_link is not None:
                changes.append("modpack link")
            if connection_ip is not None:
                changes.append("connection IP")
            if additional_info is not None:
                if additional_info.upper() == "REMOVE":
                    changes.append("removed additional information")
                else:
                    changes.append("additional information")
            change_summary = "no fields" if not changes else ", ".join(changes)
            if last_bot_message:
                await last_bot_message.edit(content=new_info_msg)
                await interaction.edit_original_response(
                    content=f"✅ Updated {change_summary} in {connection_info.mention}."
                )
            else:
                await connection_info.send(new_info_msg)
                await interaction.edit_original_response(
                    content=f"✅ Created new connection info message in {connection_info.mention}."
                )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=f"❌ Missing permissions to edit messages in {connection_info.mention}."
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Error updating connection info: {str(e)}"
            )

async def setup(bot):
    await bot.add_cog(Modpack(bot))
