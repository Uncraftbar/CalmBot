import discord
from discord.ext import commands, tasks
from discord import app_commands
from ampapi import Bridge as AMPBridge, AMPControllerInstance
from ampapi.dataclass import APIParams
import config
from cogs.utils import load_json, save_json, CHAT_BRIDGE_FILE, has_admin_or_mod_permissions
import asyncio
import re
import traceback
from datetime import datetime, timezone

class ChatBridge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Data structure: 
        # { "groups": { "group_name": { "servers": ["instance_1", "instance_2"], "active": True } } }
        self.bridge_data = {"groups": {}}
        
        self.amp_url = config.AMP_API_URL
        self.amp_user = config.AMP_USER
        self.amp_pass = config.AMP_PASS
        self.api_params = APIParams(url=self.amp_url, user=self.amp_user, password=self.amp_pass)
        self.amp_bridge = AMPBridge(api_params=self.api_params)
        self.ads = AMPControllerInstance()
        
        self.instances = {}
        # Stores the "High Water Mark" for each server: 
        # { "server_name": { "ts": datetime_obj, "hashes": set() } }
        self.high_water_marks = {}
        
        self.sync_loop.start()

    async def cog_load(self):
        self.bridge_data = load_json(CHAT_BRIDGE_FILE, {"groups": {}})
        await self._refresh_instances()

    async def cog_unload(self):
        self.sync_loop.cancel()

    async def _refresh_instances(self):
        try:
            await self.ads.get_instances(format_data=True)
            self.instances = {}
            for inst in self.ads.instances:
                name = inst.friendly_name or inst.instance_name
                self.instances[name] = inst
        except Exception:
            print("Error refreshing instances:")
            traceback.print_exc()

    async def _fetch_update_safe(self, name, instance):
        try:
            # Return tuple of (name, updates)
            # 5 second timeout to prevent hanging
            updates = await asyncio.wait_for(instance.get_updates(format_data=True), timeout=5.0)
            return name, updates
        except asyncio.TimeoutError:
            print(f"[Bridge] Timeout fetching updates for {name}")
            return name, None
        except Exception:
            # Log error but don't crash; return None updates
            return name, None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if not self.bridge_data.get("groups"): return

        for group_name, group_data in self.bridge_data["groups"].items():
            if not group_data.get("active", True): continue
            
            # Check if this channel is linked to the group
            linked_channel_id = group_data.get("channel_id")
            if not linked_channel_id or message.channel.id != linked_channel_id: continue

            # Broadcast to all servers in the group
            instance_names = group_data.get("servers", [])
            if not instance_names: continue

            user = message.author.display_name
            msg = message.content
            
            # Simple sanitization for tellraw JSON
            safe_user = user.replace('\\', '\\\\').replace('"', '\\"')
            safe_msg = msg.replace('\\', '\\\\').replace('"', '\\"')
            
            cmd = f'tellraw @a ["",{{"text":"[Discord] ", "color": "blue"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'

            for target_name in instance_names:
                target = self.instances.get(target_name)
                if target:
                    asyncio.create_task(self._send_message_safe(target, cmd, target_name))
            
            # We found the group, no need to check others (assuming 1:1 mapping preference)
            break

    @tasks.loop(seconds=2.0)
    async def sync_loop(self):
        if not self.bridge_data.get("groups"): return

        # 1. Identify all unique active servers across all groups
        active_instances = {}
        for group_data in self.bridge_data["groups"].values():
            if not group_data.get("active", True): continue
            for server_name in group_data.get("servers", []):
                if server_name in self.instances:
                    active_instances[server_name] = self.instances[server_name]

        if not active_instances:
            return

        # 2. Fetch updates from all servers concurrently
        tasks = [self._fetch_update_safe(name, inst) for name, inst in active_instances.items()]
        results = await asyncio.gather(*tasks)
        
        # Map: server_name -> updates_object
        updates_map = {name: updates for name, updates in results if updates}

        # 3. First Pass: Identify TRULY NEW messages for each server and update watermarks
        new_messages_per_server = {} # { "server_name": [(user, msg), ...] }

        for source_name, updates in updates_map.items():
            if not updates.console_entries: continue

            # Pre-process and sort entries by timestamp
            parsed_entries = []
            for entry in updates.console_entries:
                ts = getattr(entry, 'timestamp', None)
                if not ts: continue
                
                # If timestamp is already a datetime (converted by ampapi), use it.
                if not isinstance(ts, datetime):
                    try:
                        ts = datetime.fromisoformat(str(ts))
                    except ValueError:
                        continue
                
                if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
                parsed_entries.append((ts, entry))
            
            parsed_entries.sort(key=lambda x: x[0])

            # Initialization (High-Water Mark)
            if source_name not in self.high_water_marks:
                if parsed_entries:
                    latest_ts, latest_entry = parsed_entries[-1]
                    latest_hash = hash(f"{str(getattr(latest_entry, 'source', ''))}:{str(getattr(latest_entry, 'contents', ''))}")
                    self.high_water_marks[source_name] = {'ts': latest_ts, 'hashes': {latest_hash}}
                else:
                    self.high_water_marks[source_name] = {'ts': datetime.now(timezone.utc), 'hashes': set()}
                continue

            watermark = self.high_water_marks[source_name]
            valid_new = []

            for ts, entry in parsed_entries:
                if ts < watermark['ts']: continue
                
                msg = str(getattr(entry, 'contents', ''))
                user = str(getattr(entry, 'source', ''))
                msg_hash = hash(f"{user}:{msg}")

                if ts == watermark['ts']:
                    if msg_hash in watermark['hashes']: continue
                    watermark['hashes'].add(msg_hash)
                elif ts > watermark['ts']:
                    watermark['ts'] = ts
                    watermark['hashes'] = {msg_hash}

                # Filters
                msg_type = str(getattr(entry, 'type', '')).lower()
                if not user or not msg: continue
                if "chat" not in msg_type: continue
                if re.match(r"^\[.+?\] <.+?> .+", msg): continue
                if msg.startswith("[") and "]" in msg: continue
                if len(user) < 3 or len(user) > 16: continue
                
                msg_lower = msg.lower()
                if "tps" in msg_lower and "ms/tick" in msg_lower: continue
                if msg_lower.startswith("private_for_"): continue
                
                system_users = {"server", "console", "rcon", "tip", "ftbteambases", "dimdungeons", "compactmachines", "storage", "twilight", "the", "overworld", "nether", "end", "irons_spellbooks", "ftb", "irregular_implements", "spatial"}
                if user.lower() in system_users: continue

                valid_new.append((user, msg))
            
            if valid_new:
                new_messages_per_server[source_name] = valid_new

        # 4. Second Pass: Dispatch messages to groups AND Discord
        for group_name, group_data in self.bridge_data["groups"].items():
            if not group_data.get("active", True): continue
            
            instance_names = group_data.get("servers", [])
            discord_channel_id = group_data.get("channel_id")
            
            # Skip if no servers and no discord (need at least 2 endpoints effectively)
            if len(instance_names) < 1: continue

            discord_channel = None
            if discord_channel_id:
                discord_channel = self.bot.get_channel(discord_channel_id)

            for source_name in instance_names:
                messages = new_messages_per_server.get(source_name)
                if not messages: continue

                for user, msg in messages:
                    # Send to other Minecraft Servers
                    for target_name in instance_names:
                        if target_name == source_name: continue
                        target = self.instances.get(target_name)
                        if target:
                            safe_user = user.replace('\\', '\\\\').replace('"', '\\"')
                            safe_msg = msg.replace('\\', '\\\\').replace('"', '\\"')
                            safe_source = source_name.replace('\\', '\\\\').replace('"', '\\"')
                            
                            cmd = f'tellraw @a ["",{{"text":"[{safe_source}] ", "color": "aqua"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'
                            asyncio.create_task(self._send_message_safe(target, cmd, target_name))
                    
                    # Send to Discord
                    if discord_channel:
                        asyncio.create_task(self._send_discord_message_webhook(discord_channel, user, msg, source_name))

    async def _send_discord_message_webhook(self, channel, user, msg, source_name):
        try:
            # Clean content
            safe_msg = discord.utils.escape_markdown(msg)
            
            # Try to get or create a webhook
            webhook = await self._get_or_create_webhook(channel)
            
            if webhook:
                # Use webhook to impersonate player
                await webhook.send(
                    content=safe_msg,
                    username=f"{user} [{source_name}]",
                    avatar_url=f"https://mc-heads.net/avatar/{user}",
                    allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                # Fallback if webhook creation failed
                safe_user = discord.utils.escape_markdown(user)
                await channel.send(f"**[{source_name}]** <{safe_user}> {safe_msg}")

        except Exception:
            # Fallback for any other error (permissions, rate limits)
            try:
                print(f"[Bridge] Webhook failed for {channel.id}, falling back to standard message.")
                # traceback.print_exc() 
                safe_user = discord.utils.escape_markdown(user)
                await channel.send(f"**[{source_name}]** <{safe_user}> {safe_msg}")
            except Exception:
                print(f"[Bridge] Failed to send message to Discord channel {channel.id}")

    async def _get_or_create_webhook(self, channel):
        # Check cache or fetch
        # Note: We don't cache webhooks persistently in this simplistic approach to handle deletions, 
        # but for a high-traffic bot you might want to cache 'webhook_id' in bridge_data.
        # For now, fetching listing is reasonably cheap (1 API call per message batch if not cached).
        
        if not isinstance(channel, discord.TextChannel):
            return None

        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                # Use existing webhook if it belongs to the bot or has a generic name we recognize
                if wh.user == self.bot.user or wh.name == "CalmBot Bridge":
                    return wh
            
            # Create new one
            return await channel.create_webhook(name="CalmBot Bridge")
        except discord.Forbidden:
            print(f"[Bridge] Missing Manage Webhooks permission in {channel.name}")
            return None
        except Exception:
            return None

    async def _send_message_safe(self, target, cmd, target_name):
        try:
            # 5 second timeout for sending too
            await asyncio.wait_for(target.send_console_message(cmd), timeout=5.0)
        except asyncio.TimeoutError:
            print(f"[Bridge] Timeout sending message to {target_name}")
        except Exception:
            print(f"Failed to send message to {target_name}:")
            traceback.print_exc()

    @sync_loop.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()
        await self._refresh_instances()

    @app_commands.command(name="bridge", description="Open the Chat Bridge Control Center")
    async def bridge_control(self, interaction: discord.Interaction):
        if not await has_admin_or_mod_permissions(interaction): return
        
        embed = discord.Embed(title="🌉 Chat Bridge Control Center", color=discord.Color.blue())
        embed.description = "Manage your cross-server chat links here."
        view = BridgeControlView(self)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --- Views ---

class BridgeControlView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(BCC_CreateGroupButton(cog))
        self.add_item(BCC_GroupSelect(cog))
        self.add_item(BCC_StatusButton(cog))

class BCC_CreateGroupButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Create Group", style=discord.ButtonStyle.success, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateGroupModal(self.cog))

class BCC_StatusButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Global Status", style=discord.ButtonStyle.secondary, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        data = self.cog.bridge_data["groups"]
        embed = discord.Embed(title="Global Bridge Status", color=discord.Color.gold())
        if not data: embed.description = "No bridge groups created."
        for name, info in data.items():
            status = "🟢 Active" if info.get("active", True) else "🔴 Disabled"
            servers = info.get("servers", [])
            server_text = ", ".join(servers) if servers else "*No servers linked*"
            
            # Add Discord channel status
            channel_id = info.get("channel_id")
            channel_text = ""
            if channel_id:
                channel = self.cog.bot.get_channel(channel_id)
                channel_name = channel.mention if channel else f"Unknown Channel ({channel_id})"
                channel_text = f"\n**Discord:** {channel_name}"
            
            embed.add_field(name=f"{name} ({status})", value=f"**Servers:** {server_text}{channel_text}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class BCC_GroupSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        options = []
        for name in cog.bridge_data["groups"].keys():
            options.append(discord.SelectOption(label=name, value=name))
        if not options: options.append(discord.SelectOption(label="No groups available", value="none"))
        super().__init__(placeholder="Manage a Group...", options=options, disabled=len(options)==0 or options[0].value=="none", row=1)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return
        group_name = self.values[0]
        await interaction.response.send_message(f"Managing Group: **{group_name}**", view=GroupManageView(self.cog, group_name), ephemeral=True)

class GroupManageView(discord.ui.View):
    def __init__(self, cog, group_name):
        super().__init__(timeout=300)
        self.cog = cog
        self.group_name = group_name
        group_data = self.cog.bridge_data["groups"].get(group_name, {})
        
        active = group_data.get("active", True)
        label = "Disable Bridge" if active else "Enable Bridge"
        style = discord.ButtonStyle.danger if active else discord.ButtonStyle.success
        
        self.add_item(GM_ToggleActiveButton(cog, group_name, label, style))
        self.add_item(GM_LinkServerButton(cog, group_name))
        self.add_item(GM_UnlinkServerButton(cog, group_name))
        
        # Add Link/Unlink Discord Channel Buttons
        self.add_item(GM_LinkChannelButton(cog, group_name))
        if group_data.get("channel_id"):
            self.add_item(GM_UnlinkChannelButton(cog, group_name))
            
        self.add_item(GM_DeleteGroupButton(cog, group_name))

class GM_ToggleActiveButton(discord.ui.Button):
    def __init__(self, cog, group_name, label, style):
        super().__init__(label=label, style=style, row=0)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        group_data = self.cog.bridge_data["groups"][self.group_name]
        current = group_data.get("active", True)
        group_data["active"] = not current
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        
        new_state = "Enabled" if not current else "Disabled"
        await interaction.response.edit_message(
            content=f"Bridge **{self.group_name}** is now **{new_state}**.",
            view=GroupManageView(self.cog, self.group_name)
        )

class GM_LinkServerButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Link Server", style=discord.ButtonStyle.primary, row=0)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        await self.cog._refresh_instances()
        options = []
        current_links = self.cog.bridge_data["groups"][self.group_name].get("servers", [])
        for name in self.cog.instances.keys():
            if name not in current_links:
                options.append(discord.SelectOption(label=name[:100], value=name))
        if not options:
            await interaction.response.send_message("No unlinked servers available.", ephemeral=True)
            return
        await interaction.response.send_message(f"Add server to **{self.group_name}**:", view=LinkInstanceView(self.cog, self.group_name, options[:25]), ephemeral=True)

class GM_UnlinkServerButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Unlink Server", style=discord.ButtonStyle.secondary, row=1)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        current_links = self.cog.bridge_data["groups"][self.group_name].get("servers", [])
        if not current_links:
            await interaction.response.send_message("No servers linked to this group.", ephemeral=True)
            return
        options = [discord.SelectOption(label=name[:100], value=name) for name in current_links]
        await interaction.response.send_message(f"Remove server from **{self.group_name}**:", view=UnlinkInstanceView(self.cog, self.group_name, options[:25]), ephemeral=True)

class GM_LinkChannelButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Link Discord Channel", style=discord.ButtonStyle.blurple, row=1)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Select a text channel to link:", view=LinkChannelView(self.cog, self.group_name), ephemeral=True)

class GM_UnlinkChannelButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Unlink Discord Channel", style=discord.ButtonStyle.secondary, row=1)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        if "channel_id" in self.cog.bridge_data["groups"][self.group_name]:
            del self.cog.bridge_data["groups"][self.group_name]["channel_id"]
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.edit_message(content=f"Unlinked Discord channel from **{self.group_name}**.", view=GroupManageView(self.cog, self.group_name))
        else:
            await interaction.response.send_message("No channel linked.", ephemeral=True)

class GM_DeleteGroupButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Delete Group", style=discord.ButtonStyle.danger, row=2)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        if self.group_name in self.cog.bridge_data["groups"]:
            del self.cog.bridge_data["groups"][self.group_name]
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.send_message(f"🗑️ Deleted group **{self.group_name}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Group already deleted.", ephemeral=True)

# --- Modals & Select Views ---

class LinkChannelView(discord.ui.View):
    def __init__(self, cog, group_name):
        super().__init__(timeout=60)
        self.add_item(LinkChannelSelect(cog, group_name))

class LinkChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog, group_name):
        self.cog = cog
        self.group_name = group_name
        # Only allow text channels
        super().__init__(placeholder="Select a channel...", channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        self.cog.bridge_data["groups"][self.group_name]["channel_id"] = channel.id
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        await interaction.response.send_message(f"✅ Linked {channel.mention} to **{self.group_name}**.", ephemeral=True)
        # We can't easily refresh the previous view here without passing the original interaction, but users can re-open or click other buttons.

class CreateGroupModal(discord.ui.Modal, title="Create Bridge Group"):
    name = discord.ui.TextInput(label="Group Name", placeholder="e.g. Survival", required=True)
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    async def on_submit(self, interaction: discord.Interaction):
        name = self.name.value.strip()
        if name in self.cog.bridge_data["groups"]:
            await interaction.response.send_message("Group already exists.", ephemeral=True)
            return
        self.cog.bridge_data["groups"][name] = {"servers": [], "active": True}
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        await interaction.response.send_message(f"✅ Created group **{name}**. Select it in the menu to add servers.", ephemeral=True)

class LinkInstanceView(discord.ui.View):
    def __init__(self, cog, group_name, options):
        super().__init__(timeout=60)
        self.add_item(LinkInstanceSelect(cog, group_name, options))

class LinkInstanceSelect(discord.ui.Select):
    def __init__(self, cog, group_name, options):
        self.cog = cog
        self.group_name = group_name
        super().__init__(placeholder="Select server to add...", options=options)
    async def callback(self, interaction: discord.Interaction):
        server = self.values[0]
        if server not in self.cog.bridge_data["groups"][self.group_name]["servers"]:
            self.cog.bridge_data["groups"][self.group_name]["servers"].append(server)
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.send_message(f"✅ Linked **{server}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Server already linked.", ephemeral=True)

class UnlinkInstanceView(discord.ui.View):
    def __init__(self, cog, group_name, options):
        super().__init__(timeout=60)
        self.add_item(UnlinkInstanceSelect(cog, group_name, options))

class UnlinkInstanceSelect(discord.ui.Select):
    def __init__(self, cog, group_name, options):
        self.cog = cog
        self.group_name = group_name
        super().__init__(placeholder="Select server to remove...", options=options)
    async def callback(self, interaction: discord.Interaction):
        server = self.values[0]
        if server in self.cog.bridge_data["groups"][self.group_name]["servers"]:
            self.cog.bridge_data["groups"][self.group_name]["servers"].remove(server)
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.send_message(f"✅ Unlinked **{server}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Server was not linked.", ephemeral=True)
        self.view.stop()

async def setup(bot):
    await bot.add_cog(ChatBridge(bot))