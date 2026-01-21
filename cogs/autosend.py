"""
Auto-send functionality for CalmBot.
Automatically responds to messages based on keywords, mentions, and conditions.
"""

import re
import discord
from discord.ext import commands, tasks
from discord import app_commands

from cogs.utils import (
    get_logger,
    load_json,
    save_json,
    check_permissions,
    admin_only,
    is_valid_url,
    safe_embed_color,
    info_embed,
    success_embed,
    error_embed,
    AUTOSEND_FILE
)

log = get_logger("autosend")


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def build_auto_embed(embed_data: dict) -> discord.Embed:
    """Build a discord Embed from stored embed data."""
    embed = discord.Embed(
        title=embed_data.get("title"),
        description=embed_data.get("description"),
        color=safe_embed_color(embed_data.get("color"))
    )
    
    if is_valid_url(embed_data.get("image_url")):
        embed.set_image(url=embed_data["image_url"])
    
    if embed_data.get("footer"):
        embed.set_footer(
            text=embed_data["footer"],
            icon_url=embed_data.get("footer_icon") if is_valid_url(embed_data.get("footer_icon")) else None
        )
    
    if is_valid_url(embed_data.get("thumbnail")):
        embed.set_thumbnail(url=embed_data["thumbnail"])
    
    if embed_data.get("url"):
        embed.url = embed_data["url"]
    
    if embed_data.get("timestamp"):
        embed.timestamp = discord.utils.utcnow()
    
    if embed_data.get("fields"):
        for field in embed_data["fields"].split(";"):
            if ":" in field:
                name, value = field.split(":", 1)
                embed.add_field(name=name.strip(), value=value.strip(), inline=False)
    
    return embed


# =============================================================================
# MODALS
# =============================================================================

class GroupSelectView(discord.ui.View):
    """View for selecting a bridge group."""
    def __init__(self, parent_view, groups):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        
        options = [discord.SelectOption(label="All Groups", value="all")]
        for g in groups:
            options.append(discord.SelectOption(label=g, value=g))
            
        self.select = discord.ui.Select(placeholder="Select Target Group...", options=options[:25])
        self.select.callback = self.callback
        self.add_item(self.select)
    
    async def callback(self, interaction: discord.Interaction):
        group = self.select.values[0]
        self.parent_view.state["target_group"] = None if group == "all" else group
        # Continue to message input
        await interaction.response.send_modal(PlainMessageModal(self.parent_view))

class TriggerValueModal(discord.ui.Modal, title="Enter Trigger Value"):
    """Modal for entering the trigger value."""
    
    trigger_value = discord.ui.TextInput(
        label="Trigger Value",
        placeholder="e.g. 'hello', user ID, or 'hourly' for time",
        required=True,
        max_length=100
    )
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state["trigger_value"] = self.trigger_value.value.strip()
        await interaction.response.send_message(
            f"Trigger value set to: `{self.trigger_value.value.strip()}`",
            ephemeral=True
        )


class PlainMessageModal(discord.ui.Modal, title="Enter Message"):
    """Modal for entering plain text message."""
    
    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state["plain_message"] = self.message.value
        
        msg = "Message set! Click 'Save AutoSend' to finish."
        if self.parent_view.state.get("target_group"):
            msg += f"\nTarget Group: **{self.parent_view.state['target_group']}**"
            
        await interaction.response.send_message(msg, ephemeral=True)


class EmbedFieldModal(discord.ui.Modal):
    """Modal for editing a single embed field."""
    
    def __init__(self, parent_view, field: str, label: str, style=discord.TextStyle.short):
        super().__init__(title=label)
        self.parent_view = parent_view
        self.field = field
        
        self.input = discord.ui.TextInput(label=label, style=style, required=False)
        
        # Pre-fill with current value
        if parent_view.state["message_type"] in ("plain", "game_chat"):
            val = parent_view.state.get("plain_message")
        else:
            val = parent_view.state.get("embed", {}).get(field)
        
        if val:
            self.input.default = str(val)
        
        self.add_item(self.input)
    
    async def on_submit(self, interaction: discord.Interaction):
        if self.parent_view.state["message_type"] in ("plain", "game_chat"):
            self.parent_view.state["plain_message"] = self.input.value
        else:
            if "embed" not in self.parent_view.state:
                self.parent_view.state["embed"] = {}
            self.parent_view.state["embed"][self.field] = self.input.value or None
        
        await self.parent_view.update_preview(interaction)


class AdvancedOptionsModal(discord.ui.Modal, title="Advanced Embed Options"):
    """Modal for advanced embed settings."""
    
    thumbnail = discord.ui.TextInput(label="Thumbnail URL", required=False, max_length=500)
    url = discord.ui.TextInput(label="Title URL", required=False, max_length=500)
    timestamp = discord.ui.TextInput(label="Include Timestamp? (true/false)", required=False, max_length=5)
    fields = discord.ui.TextInput(label="Fields (name:value;name:value)", required=False, max_length=1000)
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        embed = self.parent_view.state.setdefault("embed", {})
        embed["thumbnail"] = self.thumbnail.value or None
        embed["url"] = self.url.value or None
        embed["timestamp"] = self.timestamp.value.lower() == "true" if self.timestamp.value else False
        embed["fields"] = self.fields.value or None
        
        await interaction.response.send_message(
            "Advanced options set! Click 'Save AutoSend' to finish.",
            ephemeral=True
        )


class RegexModal(discord.ui.Modal, title="Regex Condition"):
    """Modal for setting regex pattern condition."""
    
    pattern = discord.ui.TextInput(label="Regex Pattern", placeholder="e.g. ^Hello.*", required=False)
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        current = parent_view.state.get("conditions", {}).get("regex")
        if current:
            self.pattern.default = current
    
    async def on_submit(self, interaction: discord.Interaction):
        conditions = self.parent_view.state.setdefault("conditions", {})
        if self.pattern.value:
            conditions["regex"] = self.pattern.value
            await interaction.response.send_message(
                f"Regex pattern set: `{self.pattern.value}`",
                ephemeral=True
            )
        else:
            conditions.pop("regex", None)
            await interaction.response.send_message("Regex condition removed.", ephemeral=True)


class LengthModal(discord.ui.Modal, title="Message Length Conditions"):
    """Modal for setting min/max length conditions."""
    
    min_len = discord.ui.TextInput(label="Min Length", placeholder="0", required=False)
    max_len = discord.ui.TextInput(label="Max Length", placeholder="2000", required=False)
    
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        conditions = parent_view.state.get("conditions", {})
        if "min_length" in conditions:
            self.min_len.default = str(conditions["min_length"])
        if "max_length" in conditions:
            self.max_len.default = str(conditions["max_length"])
    
    async def on_submit(self, interaction: discord.Interaction):
        conditions = self.parent_view.state.setdefault("conditions", {})
        msgs = []
        
        if self.min_len.value and self.min_len.value.isdigit():
            conditions["min_length"] = int(self.min_len.value)
            msgs.append(f"Min: {self.min_len.value}")
        else:
            conditions.pop("min_length", None)
        
        if self.max_len.value and self.max_len.value.isdigit():
            conditions["max_length"] = int(self.max_len.value)
            msgs.append(f"Max: {self.max_len.value}")
        else:
            conditions.pop("max_length", None)
        
        if msgs:
            await interaction.response.send_message(f"Length conditions set: {', '.join(msgs)}", ephemeral=True)
        else:
            await interaction.response.send_message("Length conditions cleared.", ephemeral=True)


# =============================================================================
# VIEWS
# =============================================================================

class AutoSendListView(discord.ui.View):
    """View for listing and selecting existing auto-sends."""
    
    def __init__(self, autosend_data: dict, bot: commands.Bot):
        super().__init__(timeout=180)
        self.autosend_data = autosend_data
        self.bot = bot
        
        options = []
        for trigger_type, triggers in autosend_data.items():
            for trigger_value in triggers:
                label = f"{trigger_type}: {trigger_value}"[:100]
                options.append(discord.SelectOption(label=label, value=f"{trigger_type}|{trigger_value}"))
        
        if options:
            self.select = discord.ui.Select(
                placeholder="Select an auto-send to edit or remove",
                options=options[:25]
            )
            self.select.callback = self.select_callback
            self.add_item(self.select)
    
    async def select_callback(self, interaction: discord.Interaction):
        trigger_type, trigger_value = self.select.values[0].split("|", 1)
        
        if trigger_type in self.autosend_data and trigger_value in self.autosend_data[trigger_type]:
            entry = self.autosend_data[trigger_type][trigger_value]
            await interaction.response.send_message(
                f"What would you like to do with **{trigger_type}** `{trigger_value}`?",
                view=EditDeleteView(self.bot, self.autosend_data, trigger_type, trigger_value, entry),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=error_embed("Not Found", "Auto-send trigger not found."),
                ephemeral=True
            )


class EditDeleteView(discord.ui.View):
    """View with Edit and Delete buttons for an auto-send."""
    
    def __init__(self, bot, autosend_data, trigger_type, trigger_value, entry):
        super().__init__(timeout=60)
        self.bot = bot
        self.autosend_data = autosend_data
        self.trigger_type = trigger_type
        self.trigger_value = trigger_value
        self.entry = entry
    
    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Build state from existing entry
        msg_type = "embed" if "embed" in self.entry else "plain"
        if self.entry.get("type") == "game_chat":
            msg_type = "game_chat"

        state = {
            "trigger_type": self.trigger_type,
            "trigger_value": self.trigger_value,
            "message_type": msg_type,
            "plain_message": self.entry.get("message"),
            "embed": self.entry.get("embed", {}).copy() if "embed" in self.entry else {},
            "conditions": self.entry.get("conditions", {}).copy()
        }
        
        view = LiveEditView(self.bot, self.autosend_data, state)
        
        if state["message_type"] in ("plain", "game_chat"):
            await interaction.response.send_message(
                state.get("plain_message", "(empty)"),
                view=view,
                ephemeral=True
            )
        else:
            embed = build_auto_embed(state.get("embed", {}))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del self.autosend_data[self.trigger_type][self.trigger_value]
        
        # Clean up empty trigger types
        if not self.autosend_data[self.trigger_type]:
            del self.autosend_data[self.trigger_type]
        
        save_json(AUTOSEND_FILE, self.autosend_data)
        log.info(f"Deleted auto-send: {self.trigger_type}/{self.trigger_value}")
        
        await interaction.response.send_message(
            embed=success_embed(
                "Deleted",
                f"Removed auto-send for **{self.trigger_type}** `{self.trigger_value}`"
            ),
            ephemeral=True
        )


class SetupView(discord.ui.View):
    """Initial setup view for creating a new auto-send."""
    
    def __init__(self, bot, autosend_data):
        super().__init__(timeout=180)
        self.bot = bot
        self.autosend_data = autosend_data
        self.state = {
            "trigger_type": None,
            "trigger_value": None,
            "message_type": None,
            "plain_message": None,
            "embed": {},
            "conditions": {}
        }
    
    @discord.ui.select(
        placeholder="Select trigger type...",
        options=[
            discord.SelectOption(label="Keyword", value="keyword"),
            discord.SelectOption(label="Ping User", value="ping_user"),
            discord.SelectOption(label="Ping Role", value="ping_role"),
            discord.SelectOption(label="Time (Interval)", value="time")
        ],
        row=0
    )
    async def trigger_type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0]
        self.state["trigger_type"] = val
        
        if val == "time":
            self.state["trigger_value"] = "hourly"
            await interaction.response.send_message(
                "✅ Trigger set to **Hourly**. Now select a message type.",
                ephemeral=True
            )
        else:
            await interaction.response.send_modal(TriggerValueModal(self))
    
    @discord.ui.select(
        placeholder="Select message type...",
        options=[
            discord.SelectOption(label="Plain Message", value="plain"),
            discord.SelectOption(label="Embed", value="embed"),
            discord.SelectOption(label="Game Chat (Bridge)", value="game_chat")
        ],
        row=1
    )
    async def message_type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.state["message_type"] = select.values[0]
        
        if select.values[0] == "game_chat":
            # Fetch groups from ChatBridge
            bridge_cog = self.bot.get_cog("ChatBridge")
            groups = []
            if bridge_cog and hasattr(bridge_cog, "bridge_data"):
                groups = list(bridge_cog.bridge_data.get("groups", {}).keys())
            
            if groups:
                await interaction.response.send_message(
                    "Select a target group for the broadcast:",
                    view=GroupSelectView(self, groups),
                    ephemeral=True
                )
            else:
                self.state["target_group"] = None
                await interaction.response.send_modal(PlainMessageModal(self))
                
        elif select.values[0] == "plain":
            await interaction.response.send_modal(PlainMessageModal(self))
        else:
            await interaction.response.send_message(
                "Embed type selected! Click **Continue** to open the editor.",
                ephemeral=True
            )
    
    @discord.ui.button(label="Continue", style=discord.ButtonStyle.green, row=2)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.state
        
        if not all([state["trigger_type"], state["trigger_value"], state["message_type"]]):
            await interaction.response.send_message(
                embed=error_embed(
                    "Incomplete",
                    "Please select trigger type, enter trigger value, and select message type."
                ),
                ephemeral=True
            )
            return
        
        # Initialize embed if needed
        if state["message_type"] == "embed" and not state.get("embed", {}).get("description"):
            state["embed"]["description"] = "(no description yet)"
        
        view = LiveEditView(self.bot, self.autosend_data, state)
        
        if state["message_type"] in ("plain", "game_chat"):
            prefix = "[Game Chat Preview] " if state["message_type"] == "game_chat" else ""
            await interaction.response.send_message(
                f"{prefix}{state.get('plain_message', '(empty)')}",
                view=view,
                ephemeral=True
            )
        else:
            embed = build_auto_embed(state.get("embed", {}))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class LiveEditView(discord.ui.View):
    """Live editor view with preview updates."""
    
    def __init__(self, bot, autosend_data, state):
        super().__init__(timeout=300)
        self.bot = bot
        self.autosend_data = autosend_data
        self.state = state
        
        # Ensure embed dict exists
        if self.state["message_type"] == "embed":
            self.state.setdefault("embed", {})
    
    async def update_preview(self, interaction: discord.Interaction):
        """Update the message to show current preview."""
        if self.state["message_type"] in ("plain", "game_chat"):
            prefix = "[Game Chat Preview] " if self.state["message_type"] == "game_chat" else ""
            await interaction.response.edit_message(
                content=f"{prefix}{self.state.get('plain_message', '(empty)')}",
                embed=None,
                view=self
            )
        else:
            embed = build_auto_embed(self.state.get("embed", {}))
            await interaction.response.edit_message(content=None, embed=embed, view=self)
    
    @discord.ui.button(label="Edit Message", style=discord.ButtonStyle.secondary, row=0)
    async def edit_msg(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Determine if we are editing title/description or the plain message
        if self.state["message_type"] in ("plain", "game_chat"):
             await interaction.response.send_modal(EmbedFieldModal(self, "message", "Edit Message", discord.TextStyle.paragraph))
        else:
             await interaction.response.send_modal(EmbedFieldModal(self, "description", "Edit Description", discord.TextStyle.paragraph))

    @discord.ui.button(label="Edit Title", style=discord.ButtonStyle.secondary, row=0)
    async def edit_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state["message_type"] in ("plain", "game_chat"):
            await interaction.response.send_message("Titles are only for Embed type.", ephemeral=True)
            return
        await interaction.response.send_modal(EmbedFieldModal(self, "title", "Edit Title"))
    
    @discord.ui.button(label="Edit Color", style=discord.ButtonStyle.secondary, row=1)
    async def edit_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state["message_type"] in ("plain", "game_chat"):
            await interaction.response.send_message("Colors are only for Embed type.", ephemeral=True)
            return
        await interaction.response.send_modal(EmbedFieldModal(self, "color", "Hex Color (e.g. #FF0000)"))
    
    @discord.ui.button(label="Edit Footer", style=discord.ButtonStyle.secondary, row=1)
    async def edit_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state["message_type"] in ("plain", "game_chat"):
            await interaction.response.send_message("Footers are only for Embed type.", ephemeral=True)
            return
        await interaction.response.send_modal(EmbedFieldModal(self, "footer", "Footer Text"))
    
    @discord.ui.button(label="Edit Image", style=discord.ButtonStyle.secondary, row=2)
    async def edit_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state["message_type"] in ("plain", "game_chat"):
            await interaction.response.send_message("Images are only for Embed type.", ephemeral=True)
            return
        await interaction.response.send_modal(EmbedFieldModal(self, "image_url", "Image URL"))
    
    @discord.ui.button(label="Conditions", style=discord.ButtonStyle.primary, row=3)
    async def conditions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a condition to edit:",
            view=ConditionsView(self.bot, self.state),
            ephemeral=True
        )
    
    @discord.ui.button(label="Advanced", style=discord.ButtonStyle.secondary, row=3)
    async def advanced(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state["message_type"] in ("plain", "game_chat"):
            await interaction.response.send_message("Advanced options are only for Embed type.", ephemeral=True)
            return
        await interaction.response.send_modal(AdvancedOptionsModal(self))
    
    @discord.ui.button(label="Save AutoSend", style=discord.ButtonStyle.green, row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.state
        
        # Build entry
        if state["message_type"] in ("plain", "game_chat"):
            entry = {"message": state.get("plain_message", "")}
            if state["message_type"] == "game_chat":
                entry["type"] = "game_chat"
                if state.get("target_group"):
                    entry["group"] = state["target_group"]
        else:
            entry = {"embed": {k: v for k, v in state.get("embed", {}).items() if v}}
        
        # Add conditions if any
        if state.get("conditions"):
            entry["conditions"] = state["conditions"]
        
        # Save
        self.autosend_data.setdefault(state["trigger_type"], {})[state["trigger_value"]] = entry
        save_json(AUTOSEND_FILE, self.autosend_data)
        
        log.info(f"Saved auto-send: {state['trigger_type']}/{state['trigger_value']}")
        
        await interaction.response.send_message(
            embed=success_embed(
                "Saved",
                f"Auto-send for **{state['trigger_type']}** `{state['trigger_value']}` saved!"
            ),
            ephemeral=True
        )


class ConditionsView(discord.ui.View):
    """View for editing auto-send conditions."""
    
    def __init__(self, bot, state):
        super().__init__(timeout=180)
        self.bot = bot
        self.state = state
        self.state.setdefault("conditions", {})
    
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Restrict to channel...",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=0
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if select.values:
            self.state["conditions"]["channel_id"] = select.values[0].id
            await interaction.response.send_message(
                f"Restricted to channel: {select.values[0].mention}",
                ephemeral=True
            )
        else:
            self.state["conditions"].pop("channel_id", None)
            await interaction.response.send_message("Channel restriction removed.", ephemeral=True)
    
    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Restrict to role...",
        min_values=0,
        max_values=1,
        row=1
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if select.values:
            self.state["conditions"]["role_id"] = select.values[0].id
            await interaction.response.send_message(
                f"Restricted to role: {select.values[0].mention}",
                ephemeral=True
            )
        else:
            self.state["conditions"].pop("role_id", None)
            await interaction.response.send_message("Role restriction removed.", ephemeral=True)
    
    @discord.ui.button(label="Set Regex", style=discord.ButtonStyle.secondary, row=2)
    async def set_regex(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegexModal(self))
    
    @discord.ui.button(label="Set Length", style=discord.ButtonStyle.secondary, row=2)
    async def set_length(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LengthModal(self))
    
    @discord.ui.button(label="Clear All", style=discord.ButtonStyle.danger, row=3)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state["conditions"] = {}
        await interaction.response.send_message("All conditions cleared.", ephemeral=True)


# =============================================================================
# COG
# =============================================================================

class AutoSend(commands.Cog):
    """Automatic message responses based on triggers and conditions."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.autosend_data = load_json(AUTOSEND_FILE, {})
        log.info(f"AutoSend cog loaded with {sum(len(v) for v in self.autosend_data.values())} triggers")
    
    async def cog_load(self):
        self.time_loop.start()

    async def cog_unload(self):
        self.time_loop.cancel()

    @tasks.loop(minutes=1)
    async def time_loop(self):
        """Check for time-based triggers every minute."""
        now = discord.utils.utcnow()
        
        # Check 'hourly' triggers at minute 0
        if now.minute == 0:
            await self._process_time_triggers("hourly")

    async def _process_time_triggers(self, trigger_key):
        if "time" not in self.autosend_data: return
        
        triggers = self.autosend_data["time"]
        if trigger_key in triggers:
            entry = triggers[trigger_key]
            
            # For game_chat
            if isinstance(entry, dict) and entry.get("type") == "game_chat":
                msg = entry.get("message", "")
                group = entry.get("group")
                bridge = self.bot.get_cog("ChatBridge")
                if bridge:
                    await bridge.broadcast_system_message(msg, group)
                return

            # For Discord messages, we need a channel_id from conditions
            conditions = entry.get("conditions", {})
            channel_id = conditions.get("channel_id")
            if channel_id:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    msg_data = entry.get("embed") or entry.get("message")
                    await self._send_response(channel, msg_data)
    
    autosend_group = app_commands.Group(name="autosend", description="Manage auto-send triggers")
    
    @autosend_group.command(name="add", description="Create a new auto-send trigger")
    @admin_only()
    async def add(self, interaction: discord.Interaction):
        """Interactively create a new auto-send."""
        await interaction.response.send_message(
            "**Create New Auto-Send**\n"
            "Select the trigger type and message type, then click Continue:",
            view=SetupView(self.bot, self.autosend_data),
            ephemeral=True
        )
    
    @autosend_group.command(name="list", description="View and manage existing auto-sends")
    @admin_only()
    async def list(self, interaction: discord.Interaction):
        """List all auto-sends with edit/delete options."""
        # Reload from disk
        self.autosend_data = load_json(AUTOSEND_FILE, {})
        
        if not self.autosend_data or not any(self.autosend_data.values()):
            await interaction.response.send_message(
                embed=info_embed("No Auto-Sends", "No auto-send triggers configured yet."),
                ephemeral=True
            )
            return
        
        # Build summary embed
        embed = info_embed("Auto-Send Triggers", "Select one to edit or delete:")
        
        for trigger_type, triggers in self.autosend_data.items():
            if not triggers:
                continue
            
            entries = []
            for key, value in triggers.items():
                if isinstance(value, dict) and "embed" in value:
                    desc = value["embed"].get("description", "")[:40]
                    entries.append(f"• `{key}` → [Embed] {desc}...")
                else:
                    msg = value.get("message", str(value))[:40] if isinstance(value, dict) else str(value)[:40]
                    entries.append(f"• `{key}` → {msg}...")
            
            if entries:
                embed.add_field(
                    name=trigger_type.replace("_", " ").title(),
                    value="\n".join(entries[:5]),
                    inline=False
                )
        
        await interaction.response.send_message(
            embed=embed,
            view=AutoSendListView(self.autosend_data, self.bot),
            ephemeral=True
        )
    
    @autosend_group.command(name="help", description="Show help for the auto-send system")
    @admin_only()
    async def help(self, interaction: discord.Interaction):
        """Display help information."""
        embed = info_embed(
            "AutoSend Help",
            "Configure automatic responses to messages."
        )
        
        embed.add_field(
            name="Commands",
            value=(
                "`/autosend add` — Create a new auto-send\n"
                "`/autosend list` — View, edit, or delete existing\n"
                "`/autosend help` — Show this help"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Trigger Types",
            value=(
                "• **Keyword** — Responds when message contains word\n"
                "• **Ping User** — Responds when user is mentioned\n"
                "• **Ping Role** — Responds when role is mentioned"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Conditions",
            value=(
                "• **Channel** — Only trigger in specific channel\n"
                "• **Role** — Only trigger for users with role\n"
                "• **Regex** — Match pattern in message\n"
                "• **Length** — Min/max message length"
            ),
            inline=False
        )
        
        embed.set_footer(text="Tip: Use the interactive editor for live preview!")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Process messages for auto-send triggers."""
        if message.author.bot:
            return
        
        for trigger_type, triggers in self.autosend_data.items():
            for trigger_value, entry in triggers.items():
                # Parse entry
                if isinstance(entry, dict) and ("embed" in entry or "message" in entry):
                    msg_data = entry.get("embed") or entry.get("message")
                    conditions = entry.get("conditions", {})
                else:
                    msg_data = entry
                    conditions = {}
                
                # Check conditions
                if not self._check_conditions(message, conditions):
                    continue
                
                # Check trigger
                triggered = False
                
                if trigger_type == "keyword":
                    if trigger_value.lower() in message.content.lower():
                        triggered = True
                
                elif trigger_type == "ping_user":
                    if any(str(u.id) == trigger_value for u in message.mentions):
                        triggered = True
                
                elif trigger_type == "ping_role":
                    if any(str(r.id) == trigger_value for r in message.role_mentions):
                        triggered = True
                
                if triggered:
                    await self._send_response(message.channel, msg_data)
                    log.debug(f"Auto-send triggered: {trigger_type}/{trigger_value}")
                    return
    
    def _check_conditions(self, message: discord.Message, conditions: dict) -> bool:
        """Check if all conditions are met."""
        if not conditions:
            return True
        
        # Channel restriction
        if conditions.get("channel_id"):
            if message.channel.id != conditions["channel_id"]:
                return False
        
        # Role restriction
        if conditions.get("role_id"):
            member_roles = getattr(message.author, 'roles', [])
            if not any(r.id == conditions["role_id"] for r in member_roles):
                return False
        
        # Length restrictions
        if conditions.get("min_length"):
            if len(message.content) < conditions["min_length"]:
                return False
        
        if conditions.get("max_length"):
            if len(message.content) > conditions["max_length"]:
                return False
        
        # Regex
        if conditions.get("regex"):
            try:
                if not re.search(conditions["regex"], message.content):
                    return False
            except re.error:
                return False
        
        return True
    
    async def _send_response(self, channel, msg_data):
        """Send the auto-response."""
        # Check for game_chat type first
        if isinstance(msg_data, dict) and msg_data.get("type") == "game_chat":
             bridge = self.bot.get_cog("ChatBridge")
             if bridge:
                 await bridge.broadcast_system_message(msg_data.get("message", ""), msg_data.get("group"))
             return

        # Check if it's embed data
        if isinstance(msg_data, dict):
            if "embed" in msg_data:
                embed = build_auto_embed(msg_data["embed"])
            elif any(k in msg_data for k in ("title", "description", "color")):
                embed = build_auto_embed(msg_data)
            else:
                # Plain message in dict
                await channel.send(msg_data.get("message", str(msg_data)))
                return
            
            await channel.send(embed=embed)
        else:
            await channel.send(str(msg_data))
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reaction-based auto-sends."""
        for trigger_type, triggers in self.autosend_data.items():
            for trigger_value, entry in triggers.items():
                if not isinstance(entry, dict):
                    continue
                
                conditions = entry.get("conditions", {})
                reaction_emoji = conditions.get("reaction_emoji")
                
                if not reaction_emoji:
                    continue
                
                # Check emoji match
                emoji_str = str(payload.emoji) if payload.emoji.id else payload.emoji.name
                if emoji_str != reaction_emoji and payload.emoji.name != reaction_emoji:
                    continue
                
                # Check channel restriction
                if conditions.get("channel_id") and payload.channel_id != conditions["channel_id"]:
                    continue
                
                channel = self.bot.get_channel(payload.channel_id)
                if channel:
                    msg_data = entry.get("embed") or entry.get("message")
                    await self._send_response(channel, msg_data)
                    return


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoSend(bot))
