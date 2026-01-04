import discord
from discord.ext import commands
from cogs.utils import load_json, save_json, ROLES_BOARD_FILE
from discord import app_commands

REACTION_ROLES_FILE = "data/reaction_roles.json"

class SyncRolesView(discord.ui.View):
    def __init__(self, bot, roles_board_data, invalid_roles):
        super().__init__(timeout=180)
        self.bot = bot
        self.roles_board_data = roles_board_data
        self.invalid_roles = invalid_roles
        
        # Max select options is 25. If more, we might need a different approach,
        # but for now we assume <25 invalid roles.
        options = []
        for i, role_data in enumerate(invalid_roles):
             # Value is index in the invalid_roles list
            label = f"{role_data['name']} ({role_data['emoji']})"
            desc = role_data.get("error", f"ID: {role_data['role_id']}")
            options.append(discord.SelectOption(label=label[:100], value=str(i), description=desc[:100]))
            if len(options) >= 25:
                break
        
        self.select = discord.ui.Select(placeholder="Select roles to remove from board", options=options, min_values=1, max_values=len(options))
        self.select.callback = self.select_callback
        self.add_item(self.select)
        self.add_item(self.CancelButton())

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        indices_to_remove = [int(v) for v in self.select.values]
        
        # Determine actual roles to remove based on indices
        roles_to_remove = [self.invalid_roles[i] for i in indices_to_remove]
        
        # Filter main roles list
        new_roles_list = []
        for role_data in self.roles_board_data["roles"]:
            should_keep = True
            for r_rem in roles_to_remove:
                if r_rem["role_id"] == role_data["role_id"] and r_rem["emoji"] == role_data["emoji"]:
                    should_keep = False
                    break
            if should_keep:
                new_roles_list.append(role_data)
        
        self.roles_board_data["roles"] = new_roles_list
        save_json(ROLES_BOARD_FILE, self.roles_board_data)
        
        # Remove reactions from the actual message
        roles_cog = self.bot.get_cog("RolesBoard")
        if roles_cog and self.roles_board_data["channel_id"] and self.roles_board_data["message_id"]:
            try:
                channel = self.bot.get_channel(self.roles_board_data["channel_id"])
                if channel:
                    message = await channel.fetch_message(self.roles_board_data["message_id"])
                    if message:
                        for role_rem in roles_to_remove:
                            try:
                                await message.clear_reaction(role_rem["emoji"])
                            except Exception:
                                pass # Ignore if reaction removal fails (e.g., already gone)
            except Exception:
                pass # Ignore channel/message fetch errors here, update_roles_board will handle the rest

        # Update the actual message content
        if roles_cog:
            if await roles_cog.update_roles_board():
                 await interaction.followup.send(f"✅ Removed {len(roles_to_remove)} invalid roles and updated the board.", ephemeral=True)
            else:
                 await interaction.followup.send(f"✅ Removed {len(roles_to_remove)} invalid roles from config, but could not update the board message (channel/message might be missing).", ephemeral=True)
        else:
             await interaction.followup.send(f"✅ Removed {len(roles_to_remove)} invalid roles from config.", ephemeral=True)
        self.stop()

    class CancelButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_message("Sync cancelled.", ephemeral=True)
            self.view.stop()

class RolesBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reaction_roles = load_json(REACTION_ROLES_FILE, {})
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})

    def reload_roles_board(self):
        self.roles_board = load_json(ROLES_BOARD_FILE, {"channel_id": None, "message_id": None, "roles": []})

    @app_commands.command(name="sync_roles_board", description="Check for missing roles/channels and clean up the roles board")
    async def sync_roles_board(self, interaction: discord.Interaction):
        from cogs.utils import has_admin_or_mod_permissions, find_category_by_name
        if not await has_admin_or_mod_permissions(interaction):
            return
        
        self.reload_roles_board()
        guild = interaction.guild
        
        # 1. Check Channel/Message
        channel_id = self.roles_board.get("channel_id")
        message_id = self.roles_board.get("message_id")
        
        channel_status = "✅ Found"
        message_status = "✅ Found"
        
        channel = guild.get_channel(channel_id) if channel_id else None
        
        if not channel:
            channel_status = "❌ Missing (Configured ID not found)"
            message_status = "❌ Missing (Channel not found)"
        else:
            try:
                message = await channel.fetch_message(message_id) if message_id else None
                if not message:
                     message_status = "❌ Missing (ID not found in channel)"
            except:
                message_status = "❌ Missing (Fetch failed)"

        # 2. Check Roles & Modpacks
        invalid_roles = []
        valid_roles_count = 0
        
        for role_data in self.roles_board["roles"]:
            role = guild.get_role(role_data["role_id"])
            if not role:
                role_data["error"] = "Role deleted from server"
                invalid_roles.append(role_data)
                continue
            
            # Check for associated modpack category
            # Heuristic: 
            # 1. "Name Updates" -> Category "Name [LOADER]" or "Name"
            # 2. "Name" -> Category "Name [LOADER]" or "Name"
            
            role_name = role.name
            modpack_name = role_name
            
            if role_name.lower().endswith(" updates"):
                modpack_name = role_name[:-8].strip() # Remove " Updates"
            
            # Search for category
            found_category = False
            for cat in guild.categories:
                # Check exact match
                if cat.name == modpack_name:
                    found_category = True
                    break
                # Check "Name [LOADER]" format
                if "[" in cat.name and "]" in cat.name:
                    base_name = cat.name.split("[")[0].strip()
                    if base_name.lower() == modpack_name.lower():
                        found_category = True
                        break
            
            if not found_category:
                role_data["error"] = f"Orphaned (No category '{modpack_name}')"
                invalid_roles.append(role_data)
                continue

            valid_roles_count += 1
                
        # Report
        report = f"**Roles Board Status**\n"
        report += f"• Channel: {channel_status}\n"
        report += f"• Message: {message_status}\n"
        report += f"• Valid Roles: {valid_roles_count}\n"
        report += f"• Invalid/Orphaned Roles: {len(invalid_roles)}\n"
        
        if not invalid_roles:
            await interaction.response.send_message(report + "\n✅ All roles are synced!", ephemeral=True)
            return
            
        report += "\n**Invalid/Orphaned Roles found!** Select the ones you want to remove:"
        view = SyncRolesView(self.bot, self.roles_board, invalid_roles)
        await interaction.response.send_message(report, view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        self.reload_roles_board()
        if payload.user_id == self.bot.user.id:
            return
        message_id = str(payload.message_id)
        if self.roles_board["message_id"] and str(self.roles_board["message_id"]) == message_id:
            emoji = payload.emoji.name if payload.emoji.id else str(payload.emoji)
            role_id = None
            for role_data in self.roles_board["roles"]:
                stored_emoji = role_data["emoji"]
                if emoji == stored_emoji or emoji == stored_emoji.strip():
                    role_id = role_data["role_id"]
                    break
            if role_id:
                guild = self.bot.get_guild(payload.guild_id)
                if not guild:
                    return
                role = guild.get_role(role_id)
                if role:
                    member = guild.get_member(payload.user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(payload.user_id)
                        except Exception:
                            return
                    if member:
                        try:
                            await member.add_roles(role, reason="Reaction role from roles board")
                        except Exception:
                            pass
            return
        if message_id in self.reaction_roles:
            emoji = payload.emoji.name if payload.emoji.id else str(payload.emoji)
            if emoji in self.reaction_roles[message_id]:
                role_id = self.reaction_roles[message_id][emoji]
                guild = self.bot.get_guild(payload.guild_id)
                role = guild.get_role(role_id)
                if role:
                    member = guild.get_member(payload.user_id)
                    if member:
                        try:
                            await member.add_roles(role, reason="Reaction role")
                        except Exception:
                            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        self.reload_roles_board()
        if payload.user_id == self.bot.user.id:
            return
        message_id = str(payload.message_id)
        if self.roles_board["message_id"] and str(self.roles_board["message_id"]) == message_id:
            emoji = payload.emoji.name if payload.emoji.id else str(payload.emoji)
            role_id = None
            for role_data in self.roles_board["roles"]:
                if role_data["emoji"] == emoji:
                    role_id = role_data["role_id"]
                    break
            if role_id:
                guild = self.bot.get_guild(payload.guild_id)
                if not guild:
                    return
                role = guild.get_role(role_id)
                if role:
                    member = guild.get_member(payload.user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(payload.user_id)
                        except Exception:
                            return
                    if member:
                        try:
                            await member.remove_roles(role, reason="Reaction role removed from roles board")
                        except Exception:
                            pass
            return
        if message_id in self.reaction_roles:
            emoji = payload.emoji.name if payload.emoji.id else str(payload.emoji)
            if emoji in self.reaction_roles[message_id]:
                role_id = self.reaction_roles[message_id][emoji]
                guild = self.bot.get_guild(payload.guild_id)
                role = guild.get_role(role_id)
                if role:
                    member = guild.get_member(payload.user_id)
                    if member:
                        try:
                            await member.remove_roles(role, reason="Reaction role removed")
                        except Exception:
                            pass

    async def update_roles_board(self):
        self.reload_roles_board()
        if not self.roles_board["channel_id"] or not self.roles_board["message_id"]:
            return False
        channel = self.bot.get_channel(self.roles_board["channel_id"])
        if not channel:
            return False
        try:
            message = await channel.fetch_message(self.roles_board["message_id"])
        except:
            return False
        embed = discord.Embed(
            title="Available Server Roles",
            description="React to get roles for modpack updates and notifications",
            color=discord.Color.blue()
        )
        sorted_roles = sorted(self.roles_board["roles"], key=lambda x: x["name"])
        for role_data in sorted_roles:
            role = channel.guild.get_role(role_data["role_id"])
            if role:
                embed.add_field(
                    name=f"{role_data['emoji']} {role_data['name']}",
                    value=f"React with {role_data['emoji']} to get the {role.mention} role",
                    inline=False
                )
        embed.set_footer(text="React to get roles | Managed by CalmBot")
        await message.edit(content="", embed=embed)
        for role_data in self.roles_board["roles"]:
            try:
                await message.add_reaction(role_data["emoji"])
            except:
                pass
        return True

    @app_commands.command(name="setup_roles_board", description="Set up a central message for all role reactions")
    @app_commands.describe(
        channel="The channel to send the roles board message to (mention with #)",
        title="Title for the roles board (default: 'Modpack Update Notifications')",
        description="Description for the roles board (default message about reactions)"
    )
    async def setup_roles_board(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str = "Modpack Update Notifications", description: str = "React to get roles for modpack updates and notifications"):
        from cogs.utils import has_admin_or_mod_permissions, save_json
        if not await has_admin_or_mod_permissions(interaction):
            return
        guild = interaction.guild
        bot_member = guild.get_member(self.bot.user.id)
        missing_permissions = []
        channel_perms = channel.permissions_for(bot_member)
        if not channel_perms.send_messages:
            missing_permissions.append("Send Messages")
        if not channel_perms.embed_links:
            missing_permissions.append("Embed Links")
        if not channel_perms.add_reactions:
            missing_permissions.append("Add Reactions")
        if not channel_perms.read_message_history:
            missing_permissions.append("Read Message History")
        guild_perms = bot_member.guild_permissions
        if not guild_perms.manage_roles:
            missing_permissions.append("Manage Roles")
        if missing_permissions:
            await interaction.response.send_message(
                f"⚠️ The bot is missing the following required permissions:\n" +
                "\n".join([f"• {perm}" for perm in missing_permissions]) +
                "\n\nPlease give the bot these permissions and try again.",
                ephemeral=True
            )
            return
        if self.roles_board["channel_id"] and self.roles_board["message_id"]:
            try:
                old_channel = self.bot.get_channel(self.roles_board["channel_id"])
                if old_channel:
                    try:
                        old_message = await old_channel.fetch_message(self.roles_board["message_id"])
                        await old_message.delete()
                    except:
                        pass
            except:
                pass
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue()
        )
        sorted_roles = sorted(self.roles_board["roles"], key=lambda x: x["name"])
        for role_data in sorted_roles:
            try:
                role = interaction.guild.get_role(role_data["role_id"])
                if role:
                    embed.add_field(
                        name=f"{role_data['emoji']} {role_data['name']}",
                        value=f"React with {role_data['emoji']} to get the {role.mention} role",
                        inline=False
                    )
            except:
                pass
        embed.set_footer(text="React to get roles | Managed by CalmBot")
        try:
            message = await channel.send(embed=embed)
            self.roles_board["channel_id"] = channel.id
            self.roles_board["message_id"] = message.id
            save_json(ROLES_BOARD_FILE, self.roles_board)
            failed_reactions = []
            for role_data in self.roles_board["roles"]:
                try:
                    await message.add_reaction(role_data["emoji"])
                except discord.Forbidden:
                    failed_reactions.append(f"{role_data['emoji']} (missing permission)")
                except discord.HTTPException:
                    failed_reactions.append(f"{role_data['emoji']} (invalid emoji or API error)")
                except Exception as e:
                    failed_reactions.append(f"{role_data['emoji']} ({str(e)})")
            success_message = f"✅ Roles board set up in {channel.mention}!"
            if failed_reactions:
                success_message += f"\n⚠️ Failed to add some reactions: {', '.join(failed_reactions)}"
            success_message += f"\nRoles will automatically be added when you create new modpacks with the `/setup_modpack` command."
            await interaction.response.send_message(success_message, ephemeral=True)
        except discord.Forbidden as e:
            await interaction.response.send_message(
                f"⚠️ Missing permissions to set up roles board: {str(e)}\n\n"
                f"Please make sure the bot has the following permissions in {channel.mention}:\n"
                f"• Send Messages\n• Embed Links\n• Add Reactions\n• Read Message History",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ Error setting up roles board: {str(e)}\n\n"
                f"This could be due to permission issues or network problems.",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(RolesBoard(bot))
