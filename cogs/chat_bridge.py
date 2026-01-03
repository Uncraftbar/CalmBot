import discord
from discord.ext import commands, tasks
from discord import app_commands
from ampapi import Bridge as AMPBridge, AMPControllerInstance
from ampapi.dataclass import APIParams
import config
from cogs.utils import load_json, save_json, CHAT_BRIDGE_FILE, has_admin_or_mod_permissions
import asyncio
import re
from collections import deque
import traceback

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
        self.last_processed = {}
        self.initialized_instances = set()
        
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

    @tasks.loop(seconds=2.0)
    async def sync_loop(self):
        if not self.bridge_data.get("groups"): return

        for group_name, group_data in list(self.bridge_data["groups"].items()):
            if not group_data.get("active", True): continue
            
            instance_names = group_data.get("servers", [])
            if len(instance_names) < 2: continue

            for source_name in instance_names:
                instance = self.instances.get(source_name)
                if not instance: continue

                try:
                    updates = await instance.get_updates(format_data=True)
                    
                    # Capture initialization state before processing this batch
                    is_initialized = source_name in self.initialized_instances
                    self.initialized_instances.add(source_name)

                    if not updates.console_entries: continue

                    new_messages = []
                    for entry in updates.console_entries:
                        user = str(getattr(entry, 'source', ''))
                        msg = str(getattr(entry, 'contents', ''))
                        msg_type = str(getattr(entry, 'type', '')).lower()
                        timestamp = str(getattr(entry, 'timestamp', ''))
                        
                        if not user or not msg: continue
                        
                        # --- FILTERS ---
                        
                        # Dedupe using Source + Msg + Timestamp for uniqueness
                        line_hash = hash(f"{source_name}:{user}:{msg}:{timestamp}")
                        
                        if source_name not in self.last_processed:
                            self.last_processed[source_name] = {"set": set(), "deque": deque()}
                        
                        # Handle case where old set-only structure might persist if hot-reloaded incorrectly (safety check)
                        if isinstance(self.last_processed[source_name], set):
                             self.last_processed[source_name] = {"set": self.last_processed[source_name], "deque": deque(self.last_processed[source_name])}

                        proc_data = self.last_processed[source_name]
                        if line_hash in proc_data["set"]: continue
                        
                        proc_data["set"].add(line_hash)
                        proc_data["deque"].append(line_hash)
                        
                        if len(proc_data["deque"]) > 500:
                            removed = proc_data["deque"].popleft()
                            if removed in proc_data["set"]:
                                proc_data["set"].remove(removed)

                        # If this is the first time we're seeing this instance, just populate cache and skip sending
                        if not is_initialized: continue

                        if "chat" not in msg_type: continue
                        
                        # Avoid looping bridge messages: [Source] <User> Msg
                        if re.match(r"^\[.+?\] <.+?> .+", msg): continue

                        if msg.startswith("[") and "]" in msg: continue
                        if len(user) < 3 or len(user) > 16: continue
                        
                        msg_lower = msg.lower()
                        if "tps" in msg_lower and "ms/tick" in msg_lower: continue
                        if msg_lower.startswith("private_for_"): continue
                        
                        system_users = {"server", "console", "rcon", "tip", "ftbteambases", "dimdungeons", "compactmachines", "storage", "twilight", "the", "overworld", "nether", "end", "irons_spellbooks", "ftb", "irregular_implements", "spatial"}
                        if user.lower() in system_users: continue

                        new_messages.append((user, msg))

                    if new_messages:
                        for user, msg in new_messages:
                            for target_name in instance_names:
                                if target_name == source_name: continue
                                target = self.instances.get(target_name)
                                if target:
                                    # Fix JSON injection: Escape backslashes first, then quotes
                                    safe_user = user.replace('\\', '\\\\').replace('"', '\\"')
                                    safe_msg = msg.replace('\\', '\\\\').replace('"', '\\"')
                                    safe_source = source_name.replace('\\', '\\\\').replace('"', '\\"')
                                    
                                    cmd = f'tellraw @a ["",{{"text":"[{safe_source}] ", "color": "aqua"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'
                                    try: 
                                        await target.send_console_message(cmd)
                                    except Exception:
                                        print(f"Failed to send message to {target_name}:")
                                        traceback.print_exc()
                except Exception:
                    print(f"Error processing updates for {source_name}:")
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
            embed.add_field(name=f"{name} ({status})", value=server_text, inline=False)
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

class GM_DeleteGroupButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Delete Group", style=discord.ButtonStyle.danger, row=1)
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