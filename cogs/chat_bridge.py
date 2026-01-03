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
from urllib.parse import urlparse
from mcstatus import JavaServer

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
        self.bridge_data = load_json(CHAT_BRIDGE_FILE, {"groups": {}, "instance_settings": {}})
        await self._refresh_instances()

    async def cog_unload(self):
        self.sync_loop.cancel()

    async def _refresh_instances(self):
        try:
            # Re-create controller to ensure fresh state
            self.ads = AMPControllerInstance()
            
            # Force session clear to prevent server-side caching per session
            if hasattr(self.ads, '_bridge') and hasattr(self.ads._bridge, '_sessions'):
                self.ads._bridge._sessions.clear()

            # Capture the returned instances directly
            fetched_instances = await self.ads.get_instances(format_data=True)
            
            if not fetched_instances:
                print("[Bridge] No instances returned from API.")
                return

            self.instances = {}
            for inst in fetched_instances:
                # Exclude ADS/Controller instances
                # Use safe getattr just in case it's a raw dict or unexpected object
                mod_name = str(getattr(inst, 'module_display_name', '')).lower()
                friendly_name = str(getattr(inst, 'friendly_name', '')).strip().lower()
                
                if (mod_name in ['application deployment service', 'ads module', 'controller']) or \
                   (friendly_name == 'ads'):
                    continue
                
                # Check for required attributes
                if not hasattr(inst, 'instance_name'):
                    continue

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

    def _sanitize_for_minecraft(self, text):
        if not text: return ""
        # 1. Remove newlines/returns to prevent console command injection
        text = text.replace('\n', ' ').replace('\r', '')
        # 2. Escape backslashes first, then quotes for valid JSON
        return text.replace('\\', '\\\\').replace('"', '\"')

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
            
            # SECURITY: Sanitize to prevent command injection
            safe_user = self._sanitize_for_minecraft(user)
            safe_msg = self._sanitize_for_minecraft(msg)
            
            cmd = f'tellraw @a ["",{{"text":"[Discord] ", "color": "blue"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'

            for target_name in instance_names:
                target = self.instances.get(target_name)
                if target:
                    asyncio.create_task(self._send_message_safe(target, cmd, target_name))
            
            # We found the group, no need to check others (assuming 1:1 mapping preference)
            break

    async def _update_channel_topic(self, group_name, group_data):
        linked_channel_id = group_data.get("channel_id")
        if not linked_channel_id: return
        
        channel = self.bot.get_channel(linked_channel_id)
        if not channel or not isinstance(channel, discord.TextChannel): return

        instance_names = group_data.get("servers", [])
        if not instance_names: return
        
        # Parse AMP URL for hostname
        try:
            parsed_url = urlparse(self.amp_url)
            hostname = parsed_url.hostname
            if not hostname: hostname = "localhost"
        except:
            hostname = "localhost"

        total_players = 0
        all_player_names = set()

        for server_name in instance_names:
            inst = self.instances.get(server_name)
            if not inst or not inst.running: continue

            # Get port from endpoints if available, otherwise fallback to instance.port (management port - usually wrong for MC)
            # We rely on application_endpoints being populated.
            mc_port = None
            if hasattr(inst, 'application_endpoints'):
                 for ep in inst.application_endpoints:
                     # Check for "Minecraft Server Address" or similar if possible, but structure is list of dicts
                     # [{'display_name': 'Minecraft Server Address', 'endpoint': '0.0.0.0:25569', 'uri': ''}]
                     if ep.get('display_name') == 'Minecraft Server Address':
                         endpoint_str = ep.get('endpoint', '')
                         if ':' in endpoint_str:
                             try:
                                 mc_port = int(endpoint_str.split(':')[-1])
                             except: pass
                         break
            
            # If we couldn't find it in endpoints, we might have to skip or guess. 
            # Ideally AMP populates this. If not, we can't query via mcstatus easily without port.
            if not mc_port: continue

            try:
                # Resolve address
                address = f"{hostname}:{mc_port}"
                server = await JavaServer.async_lookup(address)
                status = await server.async_status()
                
                if status.players.online > 0:
                    total_players += status.players.online
                    if status.players.sample:
                        for p in status.players.sample:
                            all_player_names.add(p.name)
            except Exception:
                # Server might be starting up or unreachable
                pass

        # Construct Topic
        # Example: "Online Players (3): Player1, Player2, Player3"
        topic = f"Online Players ({total_players})"
        if all_player_names:
             # Sort and join
             sorted_names = sorted(list(all_player_names))
             # Truncate if too long for topic (1024 char limit usually, but keep it shorter)
             names_str = ", ".join(sorted_names)
             topic += f": {names_str}"
        
        if len(topic) > 1000:
            topic = topic[:1000] + "..."

        # Update Topic if changed
        # We need to be careful with rate limits (2 per 10 minutes per channel).
        # So we only update if it changed AND enough time has passed.
        # Check last update time
        last_update = group_data.get("last_topic_update", 0)
        current_time = datetime.now().timestamp()
        
        # 5 minutes cooldown (300 seconds) to be safe
        if current_time - last_update < 300:
             # Exception: if topic is drastically different? No, stick to rate limit safe.
             # Unless we cache the current topic locally to compare.
             if channel.topic == topic: return
             # If different, we still wait for cooldown.
             pass
        else:
             if channel.topic != topic:
                 try:
                     await channel.edit(topic=topic)
                     group_data["last_topic_update"] = current_time
                     # We don't save to file every time to avoid disk I/O spam, 
                     # but we could if we wanted persistence across restarts. 
                     # For now, memory cache is fine.
                 except Exception as e:
                     print(f"[Bridge] Failed to update topic for {channel.name}: {e}")

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
                if re.match(r"^\\[.+?\\] <.+?> .+", msg): continue
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
                
                # Get Instance Settings
                settings = self.bridge_data.get("instance_settings", {}).get(source_name, {})
                display_name = settings.get("alias", source_name)
                color = settings.get("color", "aqua")

                for user, msg in messages:
                    # Send to other Minecraft Servers
                    for target_name in instance_names:
                        if target_name == source_name: continue
                        target = self.instances.get(target_name)
                        if target:
                            # SECURITY: Sanitize to prevent command injection
                            safe_user = self._sanitize_for_minecraft(user)
                            safe_msg = self._sanitize_for_minecraft(msg)
                            safe_source = self._sanitize_for_minecraft(display_name)
                            
                            cmd = f'tellraw @a ["",{{"text":"[{safe_source}] ", "color": "{color}"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'
                            asyncio.create_task(self._send_message_safe(target, cmd, target_name))
                    
                    # Send to Discord
                    if discord_channel:
                        asyncio.create_task(self._send_discord_message_webhook(discord_channel, user, msg, display_name))

            # Update Topic for this group
            asyncio.create_task(self._update_channel_topic(group_name, group_data))

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
        self.add_item(BCC_InstanceSettingsButton(cog))
        self.add_item(BCC_GroupSelect(cog))
        self.add_item(BCC_StatusButton(cog))

class BCC_CreateGroupButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Create Group", style=discord.ButtonStyle.success, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateGroupModal(self.cog))

class BCC_InstanceSettingsButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Instance Settings", style=discord.ButtonStyle.secondary, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        await self.cog._refresh_instances()
        options = [discord.SelectOption(label=name[:100], value=name) for name in self.cog.instances.keys()]
        if not options:
            await interaction.response.send_message("No instances found.", ephemeral=True)
            return
        await interaction.response.send_message("Select an instance to configure:", view=InstanceSettingsSelector(self.cog, options[:25]), ephemeral=True)

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

class InstanceSettingsSelector(discord.ui.View):
    def __init__(self, cog, options):
        super().__init__(timeout=60)
        self.add_item(InstanceSelect(cog, options))

class InstanceSelect(discord.ui.Select):
    def __init__(self, cog, options):
        self.cog = cog
        super().__init__(placeholder="Select instance...", options=options)
    async def callback(self, interaction: discord.Interaction):
        server_name = self.values[0]
        await interaction.response.send_message(f"Configuring **{server_name}**:", view=InstanceEditView(self.cog, server_name), ephemeral=True)

class InstanceEditView(discord.ui.View):
    def __init__(self, cog, server_name):
        super().__init__(timeout=180)
        self.cog = cog
        self.server_name = server_name
        
        # Get current settings
        settings = self.cog.bridge_data.get("instance_settings", {}).get(server_name, {})
        alias = settings.get("alias", server_name)
        color = settings.get("color", "aqua")
        
        self.add_item(IE_SetAliasButton(cog, server_name, alias))
        self.add_item(IE_ColorSelect(cog, server_name, color))

class IE_SetAliasButton(discord.ui.Button):
    def __init__(self, cog, server_name, current_alias):
        super().__init__(label=f"Alias: {current_alias}", style=discord.ButtonStyle.primary, row=0)
        self.cog = cog
        self.server_name = server_name
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AliasModal(self.cog, self.server_name))

class IE_ColorSelect(discord.ui.Select):
    def __init__(self, cog, server_name, current_color):
        self.cog = cog
        self.server_name = server_name
        
        colors = ["black", "dark_blue", "dark_green", "dark_aqua", "dark_red", "dark_purple", "gold", "gray", "dark_gray", "blue", "green", "aqua", "red", "light_purple", "yellow", "white"]
        options = []
        for c in colors:
            options.append(discord.SelectOption(label=c.replace("_", " ").title(), value=c, default=(c==current_color)))
            
        super().__init__(placeholder="Select Name Color...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        color = self.values[0]
        if "instance_settings" not in self.cog.bridge_data:
            self.cog.bridge_data["instance_settings"] = {}
        if self.server_name not in self.cog.bridge_data["instance_settings"]:
            self.cog.bridge_data["instance_settings"][self.server_name] = {}
            
        self.cog.bridge_data["instance_settings"][self.server_name]["color"] = color
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        
        await interaction.response.send_message(f"🎨 Color for **{self.server_name}** set to `{color}`.", ephemeral=True)

class AliasModal(discord.ui.Modal, title="Set Instance Alias"):
    alias = discord.ui.TextInput(label="Display Name", required=True, max_length=20)
    
    def __init__(self, cog, server_name):
        super().__init__()
        self.cog = cog
        self.server_name = server_name
        
    async def on_submit(self, interaction: discord.Interaction):
        alias = self.alias.value.strip()
        if "instance_settings" not in self.cog.bridge_data:
            self.cog.bridge_data["instance_settings"] = {}
        if self.server_name not in self.cog.bridge_data["instance_settings"]:
            self.cog.bridge_data["instance_settings"][self.server_name] = {}
            
        self.cog.bridge_data["instance_settings"][self.server_name]["alias"] = alias
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        await interaction.response.send_message(f"🏷️ Alias for **{self.server_name}** set to **{alias}**.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ChatBridge(bot))