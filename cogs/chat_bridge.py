"""
Chat bridge for CalmBot.
Bridges chat between multiple Minecraft servers and Discord.
"""

import asyncio
import re
import traceback
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord.ext import commands, tasks
from discord import app_commands
from ampapi import Bridge as AMPBridge, AMPControllerInstance
from ampapi.dataclass import APIParams
from mcstatus import JavaServer

import config
from cogs.utils import (
    get_logger,
    load_json,
    save_json,
    admin_only,
    check_permissions,
    fetch_valid_instances,
    info_embed,
    success_embed,
    error_embed,
    CHAT_BRIDGE_FILE
)

log = get_logger("bridge")

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
        self.failure_counts = {}
        self.console_listeners = []
        
        self.sync_loop.start()

    async def cog_load(self):
        self.bridge_data = load_json(CHAT_BRIDGE_FILE, {"groups": {}, "instance_settings": {}})
        await self._refresh_instances()

    async def cog_unload(self):
        self.sync_loop.cancel()

    async def _refresh_instances(self):
        try:
            fetched_instances = await fetch_valid_instances()
            
            if not fetched_instances:
                log.debug("No instances returned from API")
                return

            self.instances = {}
            for inst in fetched_instances:
                name = inst.friendly_name or inst.instance_name
                self.instances[name] = inst
            
        except Exception as e:
            log.error(f"Error refreshing instances: {e}")

    async def _fetch_update_safe(self, name, instance):
        try:
            # Return tuple of (name, updates)
            # 5 second timeout to prevent hanging
            updates = await asyncio.wait_for(instance.get_updates(format_data=True), timeout=5.0)
            return name, updates
        except asyncio.TimeoutError:
            log.debug(f"Timeout fetching updates for {name}")
            return name, None
        except Exception as e:
            log.error(f"Error fetching updates for {name}: {e}")
            # traceback.print_exc() 
            return name, None

    def _sanitize_for_minecraft(self, text):
        if not text: return ""
        # 1. Remove newlines/returns to prevent console command injection
        text = text.replace('\n', ' ').replace('\r', '')
        # 2. Escape backslashes first, then quotes for valid JSON
        return text.replace('\\', '\\\\').replace('"', '\"')

    def _is_minecraft(self, instance):
        if not instance: return False
        # Check multiple attributes for robustness
        mod_disp = str(getattr(instance, 'module_display_name', '')).lower()
        mod_internal = str(getattr(instance, 'module', '')).lower()
        return "minecraft" in mod_disp or "minecraft" in mod_internal

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
            
            for target_name in instance_names:
                target = self.instances.get(target_name)
                if target:
                    if self._is_minecraft(target):
                        cmd = f'tellraw @a ["",{{"text":"[Discord] ", "color": "blue"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'
                    else:
                        # Hytale/Generic support
                        cmd = f'tellraw @a "[Discord] <{safe_user}> {safe_msg}"'
                    
                    asyncio.create_task(self._send_message_safe(target, cmd, target_name))
            
            # We found the group, no need to check others (assuming 1:1 mapping preference)
            break

    async def _get_online_players(self, group_data):
        instance_names = group_data.get("servers", [])
        if not instance_names: return {}
        
        # Parse AMP URL for hostname
        try:
            parsed_url = urlparse(self.amp_url)
            hostname = parsed_url.hostname
            if not hostname: hostname = "localhost"
        except:
            hostname = "localhost"

        online_data = {} # { "Server Alias": [player1, player2] }

        async def fetch_server_status(server_name):
            inst = self.instances.get(server_name)
            if not inst or not inst.running: return None
            
            # Get Display Name
            settings = self.bridge_data.get("instance_settings", {}).get(server_name, {})
            display_name = settings.get("alias", server_name)
            
            # Only check Minecraft servers via mcstatus
            if not self._is_minecraft(inst):
                # Try to get from AMP status for non-Minecraft servers
                try:
                    status = await inst.get_instance_status()
                    # AMP's active_users can be list of structs or names
                    if status and hasattr(status, 'active_users'):
                         raw_users = status.active_users
                         users = []
                         if isinstance(raw_users, list):
                             # Ensure we get strings
                             users = [str(u.user_name if hasattr(u, 'user_name') else u) for u in raw_users]
                         elif isinstance(raw_users, dict):
                             users = list(raw_users.keys())
                         
                         if users:
                             users.sort()
                             return display_name, users
                except Exception:
                    pass
                return None
            
            mc_port = None
            if hasattr(inst, 'application_endpoints'):
                 for ep in inst.application_endpoints:
                     if ep.get('display_name') == 'Minecraft Server Address':
                         endpoint_str = ep.get('endpoint', '')
                         if ':' in endpoint_str:
                             try:
                                 mc_port = int(endpoint_str.split(':')[-1])
                             except: pass
                         break
            
            if not mc_port: return None

            try:
                address = f"{hostname}:{mc_port}"
                server = await JavaServer.async_lookup(address)
                status = await server.async_status()
                
                players = []
                if status.players.sample:
                    players = [p.name for p in status.players.sample]
                
                # Sort players
                players.sort()
                return display_name, players
                
            except Exception:
                return None

        # Fetch all statuses in parallel
        tasks = [fetch_server_status(name) for name in instance_names]
        results = await asyncio.gather(*tasks)

        for result in results:
            if result:
                display_name, players = result
                online_data[display_name] = players
        
        return online_data

    async def _update_channel_topic(self, group_name, group_data):
        linked_channel_id = group_data.get("channel_id")
        if not linked_channel_id: return
        
        channel = self.bot.get_channel(linked_channel_id)
        if not channel or not isinstance(channel, discord.TextChannel): return

        # OPTIMIZATION: Check time before fetching data to prevent spamming the servers
        last_update = group_data.get("last_topic_update", 0)
        current_time = datetime.now().timestamp()
        
        if current_time - last_update < 300:
             return

        online_data = await self._get_online_players(group_data)
        
        total_players = 0
        all_player_names = set()

        for players in online_data.values():
            total_players += len(players)
            for p in players:
                all_player_names.add(p)

        # Construct Topic
        topic = f"Online Players ({total_players})"
        if all_player_names:
             sorted_names = sorted(list(all_player_names))
             names_str = ", ".join(sorted_names)
             topic += f": {names_str}"
        
        if len(topic) > 1000:
            topic = topic[:1000] + "..."

        if channel.topic != topic:
             try:
                 await channel.edit(topic=topic)
                 group_data["last_topic_update"] = current_time
             except Exception as e:
                 log.warning(f"Failed to update topic for {channel.name}: {e}")

    async def handle_minecraft_command(self, source_name, user, msg, group_data):
        command = msg.split(" ")[0].lower()
        
        target = self.instances.get(source_name)
        if not target: return
        
        is_minecraft = self._is_minecraft(target)
        
        if command == "!online":
            online_data = await self._get_online_players(group_data)
            
            if is_minecraft:
                # Construct Tellraw Message
                # Header
                json_msg = ['["",{"text":"[System] ", "color": "gold"}, {"text": "Online Players:", "color": "yellow"}']
                
                if not online_data:
                    json_msg.append(',{"text":"\\nNo players online.", "color": "gray"}]')
                else:
                    for server_alias, players in online_data.items():
                        p_list = ", ".join(players) if players else "None"
                        json_msg.append(f', {{"text": "\\n{server_alias}: ", "color": "aqua"}}, {{"text": "{p_list}", "color": "white"}}')
                    json_msg.append(']')
                
                full_cmd = "".join(json_msg)
                # Target the specific user
                final_cmd = f"tellraw {user} {full_cmd}"
            else:
                # Plain text for Hytale
                lines = ["[System] Online Players:"]
                if not online_data:
                    lines.append("No players online.")
                else:
                    for server_alias, players in online_data.items():
                        p_list = ", ".join(players) if players else "None"
                        lines.append(f"{server_alias}: {p_list}")
                
                full_msg = " | ".join(lines)
                final_cmd = f'tellraw @a "{full_msg}"'

            await self._send_message_safe(target, final_cmd, source_name)

        elif command == "!help":
            if is_minecraft:
                help_msg = '["",{"text":"[System] ", "color": "gold"}, {"text": "Available Commands:\\n", "color": "yellow"}, {"text": "!online ", "color": "aqua"}, {"text": "- List online players", "color": "white"}, {"text": "\\n!item ", "color": "aqua"}, {"text": "- Show held item", "color": "white"}]'
                final_cmd = f"tellraw {user} {help_msg}"
            else:
                final_cmd = 'tellraw @a "[System] Available Commands: !online - List online players"'
            
            await self._send_message_safe(target, final_cmd, source_name)

        elif command == "!item":
            if not is_minecraft: return # Not supported on non-minecraft

            target_inst = self.instances.get(source_name)
            if not target_inst: return

            # 1. Send data get command
            cmd_check = f"data get entity {user} SelectedItem.id"
            await self._send_message_safe(target_inst, cmd_check, source_name)
            
            # 2. Setup Listener
            # Pattern: PlayerName has the following entity data: "gtceu:tritanium_coil_block"
            pattern_str = f"{re.escape(user)} has the following entity data: \"(?:[^:]+:)?(.+?)\""
            regex = re.compile(pattern_str)
            
            fut = asyncio.Future()
            listener = {'source': source_name, 'regex': regex, 'future': fut}
            self.console_listeners.append(listener)
            
            try:
                # Wait for response (4 seconds to cover 2 sync loops)
                match = await asyncio.wait_for(fut, timeout=4.0)
                
                item_id = match.group(1)
                # cleanup: remove underscores, capitalize
                item_name = item_id.replace("_", " ").title()
                
                # 3. Broadcast to all servers in the group (including source)
                # Get Source Display Name & Color
                settings = self.bridge_data.get("instance_settings", {}).get(source_name, {})
                display_name = settings.get("alias", source_name)
                color = settings.get("color", "aqua")

                # Sanitize
                safe_user = self._sanitize_for_minecraft(user)
                safe_source = self._sanitize_for_minecraft(display_name)
                safe_item = self._sanitize_for_minecraft(item_name)

                # Construct Tellraw
                # Format: [Source] <User> [Item Name]
                cmd = f'tellraw @a ["",{{"text":"[{safe_source}] ", "color": "{color}"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "[{safe_item}]", "color": "light_purple" }}]'

                instance_names = group_data.get("servers", [])
                for target_name in instance_names:
                    target = self.instances.get(target_name)
                    if target:
                        asyncio.create_task(self._send_message_safe(target, cmd, target_name))
                
                # Send to Discord too if linked
                discord_channel_id = group_data.get("channel_id")
                if discord_channel_id:
                    channel = self.bot.get_channel(discord_channel_id)
                    if channel:
                         embed = discord.Embed(
                             title=f"{user} Shared an Item", 
                             description=item_name, 
                             color=discord.Color.blue()
                         )
                         asyncio.create_task(self._send_discord_message_webhook(channel, user, None, display_name, avatar_url=f"https://mc-heads.net/avatar/{user}", embed=embed))

            except asyncio.TimeoutError:
                pass
            except Exception:
                traceback.print_exc()
            finally:
                if listener in self.console_listeners:
                    self.console_listeners.remove(listener)

    @app_commands.command(name="online", description="List online players across the bridged servers.")
    async def online_command(self, interaction: discord.Interaction):
        # Determine group based on channel
        target_group = None
        target_group_name = None
        
        if self.bridge_data.get("groups"):
            for name, data in self.bridge_data["groups"].items():
                if data.get("channel_id") == interaction.channel_id:
                    target_group = data
                    target_group_name = name
                    break
        
        if not target_group:
            await interaction.response.send_message("This channel is not linked to any bridge group.", ephemeral=True)
            return

        await interaction.response.defer()
        
        online_data = await self._get_online_players(target_group)
        
        total_count = sum(len(p) for p in online_data.values())
        
        embed = discord.Embed(title=f"Online Players - {target_group_name}", description=f"**Total:** {total_count}", color=discord.Color.green())
        
        if not online_data:
            embed.description += "\nNo players online."
        else:
            for alias, players in online_data.items():
                if players:
                    # Discord fields have 1024 char limit
                    p_str = ", ".join(players)
                    if len(p_str) > 1000: p_str = p_str[:1000] + "..."
                    embed.add_field(name=f"{alias} ({len(players)})", value=f"`{p_str}`", inline=False)
                else:
                    embed.add_field(name=f"{alias} (0)", value="*No players*", inline=False)
        
        await interaction.followup.send(embed=embed)

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
        updates_map = {}
        
        for name, updates in results:
            if updates:
                updates_map[name] = updates
                self.failure_counts[name] = 0
            else:
                # Handle Failure
                count = self.failure_counts.get(name, 0) + 1
                self.failure_counts[name] = count
                
                if count >= 5:
                    if count == 5: # Log once at threshold
                        log.warning(f"Bridge connection to '{name}' is unstable (5 failures). Attempting session reset.")
                    
                    # Try to heal the connection by clearing the session
                    try:
                        inst = self.instances.get(name)
                        if inst and hasattr(inst, 'instance_id'):
                            # Access the shared bridge sessions
                            if inst.instance_id in self.amp_bridge._sessions:
                                del self.amp_bridge._sessions[inst.instance_id]
                                log.info(f"Cleared session for '{name}' to force re-login.")
                            
                            # Also reset count to give it time to recover
                            self.failure_counts[name] = 0
                    except Exception as e:
                        log.error(f"Failed to reset session for '{name}': {e}")

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

                # Check Listeners
                for listener in self.console_listeners[:]:
                    if listener['source'] == source_name:
                        match = listener['regex'].search(msg)
                        if match:
                            if not listener['future'].done():
                                listener['future'].set_result(match)
                            if listener in self.console_listeners:
                                self.console_listeners.remove(listener)

                # Filters
                msg_type = str(getattr(entry, 'type', '')).lower()
                if not user or not msg: continue
                if "chat" not in msg_type: continue
                if re.match(r"^\\[.+?\\] <.+?> .+", msg): continue
                if msg.startswith("[") and "]" in msg: continue
                if len(user) < 1 or len(user) > 32: continue
                
                msg_lower = msg.lower()
                if "tps" in msg_lower and "ms/tick" in msg_lower: continue
                if msg_lower.startswith("private_for_"): continue
                
                system_users = {"server", "console", "rcon", "tip", "ftbteambases", "dimdungeons", "compactmachines", "storage", "twilight", "the", "overworld", "nether", "end", "irons_spellbooks", "ftb", "irregular_implements", "spatial"}
                if user.lower() in system_users: continue

                valid_new.append((user, msg))
            
            if valid_new:
                new_messages_per_server[source_name] = valid_new

        # 3.5 Intercept Commands
        for source_name, messages in list(new_messages_per_server.items()):
            # Find the group for this server
            parent_group = None
            for group_name, group_data in self.bridge_data["groups"].items():
                if source_name in group_data.get("servers", []):
                    parent_group = group_data
                    break
            
            if not parent_group: continue

            filtered_messages = []
            valid_commands = {"!online", "!help", "!item"}
            
            for user, msg in messages:
                first_word = msg.split(" ")[0].lower()
                if msg.startswith("!") and first_word in valid_commands:
                    # It's a valid command
                    asyncio.create_task(self.handle_minecraft_command(source_name, user, msg, parent_group))
                else:
                    filtered_messages.append((user, msg))
            
            if filtered_messages:
                new_messages_per_server[source_name] = filtered_messages
            else:
                del new_messages_per_server[source_name]

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

                # Determine Source Type for Avatar
                source_inst = self.instances.get(source_name)
                is_minecraft_source = self._is_minecraft(source_inst)

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
                            
                            if self._is_minecraft(target):
                                cmd = f'tellraw @a ["",{{"text":"[{safe_source}] ", "color": "{color}"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'
                            else:
                                cmd = f'tellraw @a "[{safe_source}] <{safe_user}> {safe_msg}"'
                            
                            asyncio.create_task(self._send_message_safe(target, cmd, target_name))
                    
                    # Send to Discord
                    if discord_channel:
                        avatar_url = f"https://mc-heads.net/avatar/{user}" if is_minecraft_source else None
                        asyncio.create_task(self._send_discord_message_webhook(discord_channel, user, msg, display_name, avatar_url=avatar_url))

            # Update Topic for this group
            asyncio.create_task(self._update_channel_topic(group_name, group_data))

    async def _send_discord_message_webhook(self, channel, user, msg, source_name, avatar_url=None, embed=None):
        try:
            # Clean content
            safe_msg = discord.utils.escape_markdown(msg) if msg else None
            
            # Try to get or create a webhook
            webhook = await self._get_or_create_webhook(channel)
            
            if webhook:
                # Use webhook to impersonate player
                kwargs = {
                    "content": safe_msg,
                    "embed": embed,
                    "username": f"{user} [{source_name}]",
                    "allowed_mentions": discord.AllowedMentions.none()
                }
                if avatar_url:
                    kwargs["avatar_url"] = avatar_url

                await webhook.send(**kwargs)
            else:
                # Fallback if webhook creation failed
                safe_user = discord.utils.escape_markdown(user)
                prefix = f"**[{source_name}]** <{safe_user}>"
                if embed:
                    await channel.send(content=prefix, embed=embed)
                else:
                    await channel.send(f"{prefix} {safe_msg}")

        except Exception:
            # Fallback for any other error (permissions, rate limits)
            try:
                log.debug(f"Webhook failed for {channel.id}, falling back to standard message")
                safe_user = discord.utils.escape_markdown(user)
                prefix = f"**[{source_name}]** <{safe_user}>"
                if embed:
                    await channel.send(content=prefix, embed=embed)
                else:
                    safe_msg = discord.utils.escape_markdown(msg) if msg else ""
                    await channel.send(f"{prefix} {safe_msg}")
            except Exception:
                log.warning(f"Failed to send message to Discord channel {channel.id}")

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
            log.warning(f"Missing Manage Webhooks permission in {channel.name}")
            return None
        except Exception:
            return None

    async def _send_message_safe(self, target, cmd, target_name):
        try:
            # 5 second timeout for sending too
            await asyncio.wait_for(target.send_console_message(cmd), timeout=5.0)
        except asyncio.TimeoutError:
            log.debug(f"Timeout sending message to {target_name}")
        except Exception as e:
            log.warning(f"Failed to send message to {target_name}: {e}")
            traceback.print_exc()

    @sync_loop.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()
        await self._refresh_instances()

    @app_commands.command(name="bridge", description="Open the Chat Bridge Control Center")
    @admin_only()
    async def bridge_control(self, interaction: discord.Interaction):
        
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
        # Refresh the main view to show the new group in the dropdown
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=BridgeControlView(self.cog))

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
        self.current_alias = current_alias
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AliasModal(self.cog, self.server_name, self.current_alias))

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
        
        await interaction.response.edit_message(
            content=f"Configuring **{self.server_name}** (Color set to `{color}`):",
            view=InstanceEditView(self.cog, self.server_name)
        )

class AliasModal(discord.ui.Modal, title="Set Instance Alias"):
    alias = discord.ui.TextInput(label="Display Name", required=True, max_length=20)
    
    def __init__(self, cog, server_name, current_alias):
        super().__init__()
        self.cog = cog
        self.server_name = server_name
        self.alias.default = current_alias
        
    async def on_submit(self, interaction: discord.Interaction):
        alias = self.alias.value.strip()
        if "instance_settings" not in self.cog.bridge_data:
            self.cog.bridge_data["instance_settings"] = {}
        if self.server_name not in self.cog.bridge_data["instance_settings"]:
            self.cog.bridge_data["instance_settings"][self.server_name] = {}
            
        self.cog.bridge_data["instance_settings"][self.server_name]["alias"] = alias
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        await interaction.response.edit_message(
            content=f"Configuring **{self.server_name}** (Alias set to **{alias}**):",
            view=InstanceEditView(self.cog, self.server_name)
        )

async def setup(bot):
    await bot.add_cog(ChatBridge(bot))