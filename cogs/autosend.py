import discord
from discord.ext import commands
from discord import app_commands
from cogs.utils import load_json, save_json
import os

AUTOSEND_FILE = "autosend.json"

class AutoSendListView(discord.ui.View):
    def __init__(self, autosend_data, bot):
        super().__init__(timeout=180)
        self.autosend_data = autosend_data
        self.bot = bot
        options = []
        for trigger_type, triggers in autosend_data.items():
            for trigger_value in triggers:
                label = f"{trigger_type}: {trigger_value}"
                options.append(discord.SelectOption(label=label, value=f"{trigger_type}|{trigger_value}"))
        self.select = discord.ui.Select(placeholder="Select an auto-send to edit or remove", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        trigger_type, trigger_value = self.select.values[0].split("|", 1)
        if trigger_type in self.autosend_data and trigger_value in self.autosend_data[trigger_type]:
            entry = self.autosend_data[trigger_type][trigger_value]
            # Show Edit/Delete options
            await interaction.response.send_message(
                f"What would you like to do with {trigger_type} '{trigger_value}'?",
                view=AutoSendEditDeleteView(self.bot, self.autosend_data, trigger_type, trigger_value, entry),
                ephemeral=True
            )
        else:
            await interaction.response.send_message("No such auto-send trigger found.", ephemeral=True)

class AutoSendEditDeleteView(discord.ui.View):
    def __init__(self, bot, autosend_data, trigger_type, trigger_value, entry):
        super().__init__(timeout=60)
        self.bot = bot
        self.autosend_data = autosend_data
        self.trigger_type = trigger_type
        self.trigger_value = trigger_value
        self.entry = entry
        self.add_item(self.EditButton(self))
        self.add_item(self.DeleteButton(self))

    class EditButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit", style=discord.ButtonStyle.primary)
        async def callback(self, interaction: discord.Interaction):
            # Launch the editor with pre-filled data
            state = {
                "trigger_type": self.view.trigger_type,
                "trigger_value": self.view.trigger_value,
                "message_type": "embed" if "embed" in self.view.entry else "plain",
                "plain_message": self.view.entry.get("message") if "message" in self.view.entry else None,
                "embed": self.view.entry.get("embed") if "embed" in self.view.entry else {
                    "title": self.view.entry.get("title"),
                    "description": self.view.entry.get("description"),
                    "color": self.view.entry.get("color"),
                    "footer": self.view.entry.get("footer"),
                    "footer_icon": self.view.entry.get("footer_icon"),
                    "thumbnail": self.view.entry.get("thumbnail"),
                    "image_url": self.view.entry.get("image_url"),
                    "url": self.view.entry.get("url"),
                    "timestamp": self.view.entry.get("timestamp"),
                    "fields": self.view.entry.get("fields"),
                }
            }
            live_edit_view = AutoSendLiveEditView(self.view.bot, self.view.autosend_data, state)
            if state["message_type"] == "plain":
                content = state.get("plain_message", "(empty)")
                await interaction.response.send_message(
                    content,
                    view=live_edit_view,
                    ephemeral=True
                )
            else:
                embed_data = state["embed"]
                embed = discord.Embed(
                    title=embed_data.get("title"),
                    description=embed_data.get("description"),
                    color=safe_embed_color(embed_data.get("color"))
                )
                if is_valid_url(embed_data.get("image_url")):
                    embed.set_image(url=embed_data["image_url"])
                if embed_data.get("footer"):
                    embed.set_footer(text=embed_data["footer"])
                await interaction.response.send_message(
                    embed=embed,
                    view=live_edit_view,
                    ephemeral=True
                )

    class DeleteButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Delete", style=discord.ButtonStyle.danger)
        async def callback(self, interaction: discord.Interaction):
            del self.view.autosend_data[self.view.trigger_type][self.view.trigger_value]
            save_json(AUTOSEND_FILE, self.view.autosend_data)
            await interaction.response.send_message(
                f"Removed auto-send for {self.view.trigger_type} '{self.view.trigger_value}'.",
                ephemeral=True
            )

class AutoSendCreateView(discord.ui.View):
    def __init__(self, bot, autosend_data, *, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.autosend_data = autosend_data
        self.state = {
            "trigger_type": None,
            "trigger_value": None,
            "message_type": None,
            "plain_message": None,
            "embed": {
                "title": None,
                "description": None,
                "color": None,
                "footer": None,
                "footer_icon": None,
                "thumbnail": None,
                "image_url": None,
                "url": None,
                "timestamp": False,
                "fields": None
            }
        }
        self.add_item(self.TriggerTypeSelect(self))
        self.add_item(self.MessageTypeSelect(self))
        self.add_item(self.PreviewButton(self))
        self.add_item(self.AdvancedButton(self))
        self.add_item(self.NextButton(self))

    class TriggerTypeSelect(discord.ui.Select):
        def __init__(self, parent_view):
            options = [
                discord.SelectOption(label="Keyword", value="keyword"),
                discord.SelectOption(label="Ping User", value="ping_user"),
                discord.SelectOption(label="Ping Role", value="ping_role")
            ]
            super().__init__(placeholder="Select trigger type...", options=options, row=0)
        async def callback(self, interaction: discord.Interaction):
            self.view.state["trigger_type"] = self.values[0]
            await interaction.response.send_modal(TriggerValueModal(self.view))

    class MessageTypeSelect(discord.ui.Select):
        def __init__(self, parent_view):
            options = [
                discord.SelectOption(label="Plain Message", value="plain"),
                discord.SelectOption(label="Embed", value="embed")
            ]
            super().__init__(placeholder="Select message type...", options=options, row=1)
        async def callback(self, interaction: discord.Interaction):
            self.view.state["message_type"] = self.values[0]
            if self.values[0] == "plain":
                await interaction.response.send_modal(AutoSendPlainModal(self.view))
            else:
                await interaction.response.send_message("Embed type selected! Now click Continue.", ephemeral=True)

    class PreviewButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Preview", style=discord.ButtonStyle.blurple, row=2)
        async def callback(self, interaction: discord.Interaction):
            if self.view.state["message_type"] == "plain":
                msg = self.view.state.get("plain_message", "")
                if not msg:
                    await interaction.response.send_message("Please enter a message before previewing.", ephemeral=True)
                    return
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                embed_data = self.view.state["embed"]
                if not embed_data.get("description"):
                    await interaction.response.send_message("Please enter an embed description before previewing.", ephemeral=True)
                    return
                embed = discord.Embed(
                    title=embed_data.get("title"),
                    description=embed_data.get("description"),
                    color=safe_embed_color(embed_data.get("color"))
                )
                if is_valid_url(embed_data.get("image_url")):
                    embed.set_image(url=embed_data["image_url"])
                if embed_data.get("footer"):
                    embed.set_footer(text=embed_data["footer"])
                await interaction.response.send_message(embed=embed, ephemeral=True)

    class AdvancedButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Advanced Options", style=discord.ButtonStyle.secondary, row=2)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(AutoSendEmbedAdvancedModal(self.view))

    class NextButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Save AutoSend", style=discord.ButtonStyle.green, row=3)
        async def callback(self, interaction: discord.Interaction):
            state = self.view.state
            if not (state["trigger_type"] and state["trigger_value"] and state["message_type"]):
                await interaction.response.send_message("Please select all required options and fill out the message.", ephemeral=True)
                return
            if state["message_type"] == "plain" and not state["plain_message"]:
                await interaction.response.send_message("Please enter a plain message.", ephemeral=True)
                return
            if state["message_type"] == "embed" and not state["embed"]["description"]:
                await interaction.response.send_message("Please enter an embed description.", ephemeral=True)
                return
            # Save
            if state["message_type"] == "plain":
                entry = {"message": state["plain_message"]}
            else:
                entry = {"embed": {k: v for k, v in state["embed"].items() if v}}
            self.view.autosend_data.setdefault(state["trigger_type"], {})[state["trigger_value"]] = entry
            save_json(AUTOSEND_FILE, self.view.autosend_data)
            await interaction.response.send_message(f"Auto-send for {state['trigger_type']} '{state['trigger_value']}' saved!", ephemeral=True)

class TriggerValueModal(discord.ui.Modal, title="Enter Trigger Value"):
    trigger_value = discord.ui.TextInput(label="Trigger Value", placeholder="e.g. 'hello' for keyword, user ID, or role ID", required=True, max_length=100)
    def __init__(self, parent_view):
        discord.ui.Modal.__init__(self)
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state["trigger_value"] = self.trigger_value.value.strip()
        await interaction.response.send_message(f"Trigger value set to: `{self.trigger_value.value.strip()}`", ephemeral=True)

class AutoSendPlainModal(discord.ui.Modal, title="Enter Plain Message"):
    plain_message = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state["plain_message"] = self.plain_message.value
        await interaction.response.send_message("Plain message set! Click 'Save AutoSend' to finish.", ephemeral=True)

class AutoSendEmbedAdvancedModal(discord.ui.Modal, title="Advanced Embed Options"):
    embed_thumbnail = discord.ui.TextInput(label="Thumbnail URL", required=False, max_length=500)
    embed_url = discord.ui.TextInput(label="Title URL", required=False, max_length=500)
    embed_timestamp = discord.ui.TextInput(label="Include Timestamp? (true/false)", required=False, max_length=5)
    embed_fields = discord.ui.TextInput(label="Fields (name:value;name:value)", required=False, max_length=1000)
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state["embed"]["thumbnail"] = self.embed_thumbnail.value or None
        self.parent_view.state["embed"]["url"] = self.embed_url.value or None
        self.parent_view.state["embed"]["timestamp"] = (self.embed_timestamp.value.lower() == "true") if self.embed_timestamp.value else False
        self.parent_view.state["embed"]["fields"] = self.embed_fields.value or None
        await interaction.response.send_message("Advanced embed options set! Click 'Save AutoSend' to finish.", ephemeral=True)

# In your AutoSend cog:
class AutoSend(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.autosend_data = load_json(AUTOSEND_FILE, {})

    autosend_group = app_commands.Group(name="autosend", description="Manage auto-send triggers")

    @autosend_group.command(name="add", description="Interactively add an auto-send trigger.")
    async def add(self, interaction: discord.Interaction):
        from cogs.utils import has_admin_or_mod_permissions
        if not await has_admin_or_mod_permissions(interaction):
            return
        await interaction.response.send_message(
            "Let's create a new auto-send! Select the event and message type, then continue:",
            view=AutoSendSetupView(self.bot, self.autosend_data),
            ephemeral=True
        )

    @autosend_group.command(name="list", description="List all current auto-send triggers and remove them via dropdown.")
    async def list(self, interaction: discord.Interaction):
        from cogs.utils import has_admin_or_mod_permissions
        if not await has_admin_or_mod_permissions(interaction):
            return
        if not self.autosend_data or not any(self.autosend_data.values()):
            await interaction.response.send_message("No auto-send triggers set.", ephemeral=True)
            return
        embed = discord.Embed(title="Auto-Send Triggers", color=discord.Color.blue())
        for trigger_type, triggers in self.autosend_data.items():
            def preview(v):
                if isinstance(v, dict) and (v.get("type") == "embed" or "embed" in v):
                    embed_data = v.get("embed", v)
                    desc = embed_data.get("description", "")
                    return f"[EMBED] {embed_data.get('title','')}: {desc[:60]}{'...' if len(desc)>60 else ''}"
                return str(v) if len(str(v)) < 100 else str(v)[:97]+'...'
            value = "\n".join([f"**{k}**: {preview(v)}" for k, v in triggers.items()])
            embed.add_field(name=trigger_type, value=value or "None", inline=False)
        await interaction.response.send_message(embed=embed, view=AutoSendListView(self.autosend_data, self.bot), ephemeral=True)

    @autosend_group.command(name="help", description="Show help and formatting options for auto-send.")
    async def help(self, interaction: discord.Interaction):
        from cogs.utils import has_admin_or_mod_permissions
        if not await has_admin_or_mod_permissions(interaction):
            return
        embed = discord.Embed(title="AutoSend Help & Guide", color=discord.Color.green())
        embed.add_field(
            name="Basic Usage",
            value=(
                "/autosend add — Create a new auto-send with an interactive menu.\n"
                "/autosend list — View, edit, or delete auto-sends with a dropdown and buttons.\n"
                "/autosend help — Show this help."
            ),
            inline=False
        )
        embed.add_field(
            name="Workflow",
            value=(
                "• Select trigger/event and message type.\n"
                "• Use the live editor to update fields, preview changes, and access advanced options.\n"
                "• Edit or delete existing auto-sends from the list."
            ),
            inline=False
        )
        embed.add_field(
            name="Trigger Types",
            value="keyword, ping_user, ping_role, (more coming soon!)",
            inline=False
        )
        embed.add_field(
            name="Embed & Message Formatting",
            value=(
                "• Edit title, description, color, footer, image, thumbnail, URL, timestamp, and fields.\n"
                "• Use the 'Advanced Options' button for extra embed fields.\n"
                "• Live preview updates after every change."
            ),
            inline=False
        )
        embed.add_field(
            name="Tips",
            value=(
                "• Before submitting a field (like description or title), copy its contents to your clipboard.\n"
                "• If you want to make changes, you can paste and edit instead of retyping everything.\n"
                "• This is a technical limitation of Discord modals—previous values are not auto-filled."
            ),
            inline=False
        )
        embed.set_footer(text="Tip: Use the interactive UI for everything!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        for trigger_type, triggers in self.autosend_data.items():
            for trigger_value, entry in triggers.items():
                # Support both old and new format
                if isinstance(entry, dict) and ("embed" in entry or "message" in entry):
                    msg = entry.get("embed") or entry.get("message")
                    conditions = entry.get("conditions", {})
                else:
                    msg = entry
                    conditions = {}
                # --- Condition checks ---
                if conditions.get("channel_id") and message.channel.id != conditions["channel_id"]:
                    continue
                if conditions.get("role_id") and not any(r.id == conditions["role_id"] for r in getattr(message.author, 'roles', [])):
                    continue
                if conditions.get("min_length") and len(message.content) < conditions["min_length"]:
                    continue
                if conditions.get("max_length") and len(message.content) > conditions["max_length"]:
                    continue
                if conditions.get("attachment_type") and not any(a.filename.lower().endswith(conditions["attachment_type"]) for a in message.attachments):
                    continue
                if conditions.get("regex"):
                    import re
                    if not re.search(conditions["regex"], message.content):
                        continue
                if conditions.get("first_time_user"):
                    if hasattr(message.author, 'joined_at') and message.author.joined_at:
                        history = [m async for m in message.channel.history(limit=1000) if m.author == message.author]
                        if len(history) > 1:
                            continue
                # --- Trigger checks ---
                if trigger_type == "keyword" and trigger_value.lower() in message.content.lower():
                    await self._send_auto(message.channel, msg)
                    return
                if trigger_type == "ping_user" and any(str(user.id) == trigger_value for user in message.mentions):
                    await self._send_auto(message.channel, msg)
                    return
                if trigger_type == "ping_role" and any(str(role.id) == trigger_value for role in message.role_mentions):
                    await self._send_auto(message.channel, msg)
                    return

    async def _send_auto(self, channel, auto_msg):
        # Always build and send a discord.Embed if auto_msg is an embed dict or has embed-like keys
        embed_data = None
        if isinstance(auto_msg, dict):
            if "embed" in auto_msg:
                embed_data = auto_msg["embed"]
            elif any(k in auto_msg for k in ("title", "description", "color", "footer")):
                embed_data = auto_msg
        if embed_data:
            embed = discord.Embed(
                title=embed_data.get("title"),
                description=embed_data.get("description"),
                color=safe_embed_color(embed_data.get("color"))
            )
            if is_valid_url(embed_data.get("image_url")):
                embed.set_image(url=embed_data["image_url"])
            if embed_data.get("footer"):
                embed.set_footer(text=embed_data["footer"], icon_url=embed_data.get("footer_icon"))
            if embed_data.get("thumbnail") and is_valid_url(embed_data.get("thumbnail")):
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
            await channel.send(embed=embed)
        else:
            await channel.send(auto_msg if not isinstance(auto_msg, dict) else str(auto_msg))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        # Reaction-based auto-send
        for trigger_type, triggers in self.autosend_data.items():
            for trigger_value, entry in triggers.items():
                if isinstance(entry, dict) and "conditions" in entry and entry["conditions"].get("reaction_emoji"):
                    emoji = entry["conditions"]["reaction_emoji"]
                    if str(payload.emoji) == emoji or getattr(payload.emoji, 'name', None) == emoji:
                        if entry["conditions"].get("channel_id") and payload.channel_id != entry["conditions"]["channel_id"]:
                            continue
                        channel = self.bot.get_channel(payload.channel_id)
                        if channel:
                            msg = entry.get("embed") or entry.get("message")
                            await self._send_auto(channel, msg)
                            return

    async def setup(self):
        self.bot.tree.add_command(self.autosend_group)

async def setup(bot):
    await bot.add_cog(AutoSend(bot))

class AutoSendSetupView(discord.ui.View):
    def __init__(self, bot, autosend_data, *, timeout=180):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.autosend_data = autosend_data
        self.state = {
            "trigger_type": None,
            "trigger_value": None,
            "message_type": None,
        }
        self.add_item(self.TriggerTypeSelect(self))
        self.add_item(self.MessageTypeSelect(self))
        self.add_item(self.ContinueButton(self))

    class TriggerTypeSelect(discord.ui.Select):
        def __init__(self, parent_view):
            options = [
                discord.SelectOption(label="Keyword", value="keyword"),
                discord.SelectOption(label="Ping User", value="ping_user"),
                discord.SelectOption(label="Ping Role", value="ping_role")
            ]
            super().__init__(placeholder="Select trigger type...", options=options, row=0)
        async def callback(self, interaction: discord.Interaction):
            self.view.state["trigger_type"] = self.values[0]
            await interaction.response.send_modal(TriggerValueModal(self.view))

    class MessageTypeSelect(discord.ui.Select):
        def __init__(self, parent_view):
            options = [
                discord.SelectOption(label="Plain Message", value="plain"),
                discord.SelectOption(label="Embed", value="embed")
            ]
            super().__init__(placeholder="Select message type...", options=options, row=1)
        async def callback(self, interaction: discord.Interaction):
            self.view.state["message_type"] = self.values[0]
            if self.values[0] == "plain":
                await interaction.response.send_modal(AutoSendPlainModal(self.view))
            else:
                await interaction.response.send_message("Embed type selected! Now click Continue.", ephemeral=True)

    class ContinueButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Continue", style=discord.ButtonStyle.green, row=2)
        async def callback(self, interaction: discord.Interaction):
            state = self.view.state
            if not (state["trigger_type"] and state["trigger_value"] and state["message_type"]):
                await interaction.response.send_message("Please select all options and enter a trigger value.", ephemeral=True)
                return
            # Ensure embed dict exists for embed type
            if state["message_type"] == "embed":
                if "embed" not in state:
                    state["embed"] = {
                        "title": None,
                        "description": None,
                        "color": None,
                        "footer": None,
                        "footer_icon": None,
                        "thumbnail": None,
                        "image_url": None,
                        "url": None,
                        "timestamp": False,
                        "fields": None
                    }
                # Ensure description is not empty for Discord embed
                if not state["embed"].get("description"):
                    state["embed"]["description"] = "(no description yet)"
            # Build the preview and send the live edit menu
            live_edit_view = AutoSendLiveEditView(self.view.bot, self.view.autosend_data, self.view.state)
            if state["message_type"] == "plain":
                content = state.get("plain_message", "(empty)")
                await interaction.response.send_message(
                    content,
                    view=live_edit_view,
                    ephemeral=True
                )
            else:
                embed_data = state["embed"]
                embed = discord.Embed(
                    title=embed_data.get("title"),
                    description=embed_data.get("description"),
                    color=safe_embed_color(embed_data.get("color"))
                )
                if is_valid_url(embed_data.get("image_url")):
                    embed.set_image(url=embed_data["image_url"])
                if embed_data.get("footer"):
                    embed.set_footer(text=embed_data["footer"])
                await interaction.response.send_message(
                    embed=embed,
                    view=live_edit_view,
                    ephemeral=True
                )

class AutoSendLiveEditView(discord.ui.View):
    def __init__(self, bot, autosend_data, state, *, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.autosend_data = autosend_data
        self.state = state
        # Store preview message info for editing
        self.preview_message_id = None
        self.preview_channel_id = None
        # Default values for editing
        if self.state["message_type"] == "embed":
            self.state.setdefault("embed", {
                "title": None,
                "description": None,
                "color": None,
                "footer": None,
                "footer_icon": None,
                "thumbnail": None,
                "image_url": None,
                "url": None,
                "timestamp": False,
                "fields": None
            })
        self.add_item(self.EditTitleButton(self))
        self.add_item(self.EditDescriptionButton(self))
        self.add_item(self.EditColorButton(self))
        self.add_item(self.EditFooterButton(self))
        self.add_item(self.EditImageButton(self))
        self.add_item(self.AdvancedButton(self))  # Add advanced options button to live editor
        self.add_item(self.SaveButton(self))
        # Add more edit buttons for advanced fields as needed

    class EditTitleButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Title", style=discord.ButtonStyle.secondary, row=0)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "title", "Edit Title"))

    class EditDescriptionButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Description", style=discord.ButtonStyle.secondary, row=0)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "description", "Edit Description", style=discord.TextStyle.paragraph))

    class EditColorButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Color", style=discord.ButtonStyle.secondary, row=1)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "color", "Edit Color (hex, e.g. #00ff00)"))

    class EditFooterButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Footer", style=discord.ButtonStyle.secondary, row=1)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "footer", "Edit Footer"))

    class EditImageButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Image", style=discord.ButtonStyle.secondary, row=2)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "image_url", "Edit Image URL"))

    class AdvancedButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Advanced Options", style=discord.ButtonStyle.secondary, row=3)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(AutoSendEmbedAdvancedModal(self.view))

    class SaveButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Save AutoSend", style=discord.ButtonStyle.green, row=4)
        async def callback(self, interaction: discord.Interaction):
            state = self.view.state
            if state["message_type"] == "plain":
                entry = {"message": state.get("plain_message", "")}
            else:
                entry = {"embed": {k: v for k, v in state["embed"].items() if v}}
            self.view.autosend_data.setdefault(state["trigger_type"], {})[state["trigger_value"]] = entry
            save_json(AUTOSEND_FILE, self.view.autosend_data)
            await interaction.response.send_message(f"Auto-send for {state['trigger_type']} '{state['trigger_value']}' saved!", ephemeral=True)

    async def send_initial(self, interaction):
        if self.state["message_type"] == "plain":
            await interaction.response.send_message(self.state.get("plain_message", "(empty)"), view=self, ephemeral=True)
        else:
            embed_data = self.state["embed"]
            embed = discord.Embed(
                title=embed_data.get("title"),
                description=embed_data.get("description"),
                color=safe_embed_color(embed_data.get("color"))
            )
            if is_valid_url(embed_data.get("image_url")):
                embed.set_image(url=embed_data["image_url"])
            if embed_data.get("footer"):
                embed.set_footer(text=embed_data["footer"])
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

# Utility function to build the current preview embed
async def update_live_preview(interaction, state, view):
    try:
        # Try to use the interaction's message if available
        msg = getattr(interaction, 'message', None)
        # If not, try to fetch the original preview message using the view's stored ids
        if msg is None and hasattr(view, 'preview_message_id') and hasattr(view, 'preview_channel_id'):
            channel = interaction.client.get_channel(view.preview_channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(view.preview_message_id)
                except Exception:
                    msg = None
        if msg:
            if state["message_type"] == "plain":
                content = state.get("plain_message", "(empty)")
                await msg.edit(content=content, embed=None, view=view)
            else:
                embed_data = state["embed"]
                embed = discord.Embed(
                    title=embed_data.get("title"),
                    description=embed_data.get("description"),
                    color=safe_embed_color(embed_data.get("color"))
                )
                if is_valid_url(embed_data.get("image_url")):
                    embed.set_image(url=embed_data["image_url"])
                if embed_data.get("footer"):
                    embed.set_footer(text=embed_data["footer"])
                await msg.edit(content=None, embed=embed, view=view)
            return
    except (discord.NotFound, AttributeError):
        pass
    # If the message is gone or not found, send a new ephemeral preview and update the view's ids
    if state["message_type"] == "plain":
        content = state.get("plain_message", "(empty)")
        sent = await interaction.response.send_message(content, view=view, ephemeral=True)
    else:
        embed_data = state["embed"]
        embed = discord.Embed(
            title=embed_data.get("title"),
            description=embed_data.get("description"),
            color=safe_embed_color(embed_data.get("color"))
        )
        if is_valid_url(embed_data.get("image_url")):
            embed.set_image(url=embed_data["image_url"])
        if embed_data.get("footer"):
            embed.set_footer(text=embed_data["footer"])
        sent = await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    # Try to store the new preview message id/channel id
    if hasattr(view, 'preview_message_id') and hasattr(view, 'preview_channel_id'):
        try:
            if hasattr(sent, 'id') and hasattr(sent, 'channel'):
                view.preview_message_id = sent.id
                view.preview_channel_id = sent.channel.id
        except Exception:
            pass

# When entering the live edit menu, send the preview embed immediately:
class AutoSendLiveEditView(discord.ui.View):
    def __init__(self, bot, autosend_data, state, *, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.autosend_data = autosend_data
        self.state = state
        # Store preview message info for editing
        self.preview_message_id = None
        self.preview_channel_id = None
        # Default values for editing
        if self.state["message_type"] == "embed":
            self.state.setdefault("embed", {
                "title": None,
                "description": None,
                "color": None,
                "footer": None,
                "footer_icon": None,
                "thumbnail": None,
                "image_url": None,
                "url": None,
                "timestamp": False,
                "fields": None
            })
        self.add_item(self.EditTitleButton(self))
        self.add_item(self.EditDescriptionButton(self))
        self.add_item(self.EditColorButton(self))
        self.add_item(self.EditFooterButton(self))
        self.add_item(self.EditImageButton(self))
        self.add_item(self.AdvancedButton(self))  # Add advanced options button to live editor
        self.add_item(self.SaveButton(self))
        # Add more edit buttons for advanced fields as needed
        # Send initial preview
        # self.bot.loop.create_task(self.send_initial(bot.get_interaction(self.message_id)))

    class EditTitleButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Title", style=discord.ButtonStyle.secondary, row=0)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "title", "Edit Title"))

    class EditDescriptionButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Description", style=discord.ButtonStyle.secondary, row=0)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "description", "Edit Description", style=discord.TextStyle.paragraph))

    class EditColorButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Color", style=discord.ButtonStyle.secondary, row=1)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "color", "Edit Color (hex, e.g. #00ff00)"))

    class EditFooterButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Footer", style=discord.ButtonStyle.secondary, row=1)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "footer", "Edit Footer"))

    class EditImageButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Image", style=discord.ButtonStyle.secondary, row=2)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EditFieldModal(self.view, "image_url", "Edit Image URL"))

    class AdvancedButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Advanced Options", style=discord.ButtonStyle.secondary, row=3)
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(AutoSendEmbedAdvancedModal(self.view))

    class SaveButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Save AutoSend", style=discord.ButtonStyle.green, row=4)
        async def callback(self, interaction: discord.Interaction):
            state = self.view.state
            if state["message_type"] == "plain":
                entry = {"message": state.get("plain_message", "")}
            else:
                entry = {"embed": {k: v for k, v in state["embed"].items() if v}}
            self.view.autosend_data.setdefault(state["trigger_type"], {})[state["trigger_value"]] = entry
            save_json(AUTOSEND_FILE, self.view.autosend_data)
            await interaction.response.send_message(f"Auto-send for {state['trigger_type']} '{state['trigger_value']}' saved!", ephemeral=True)

class EditFieldModal(discord.ui.Modal):
    def __init__(self, parent_view, field, label, style=discord.TextStyle.short):
        super().__init__(title=label)
        self.parent_view = parent_view
        self.field = field
        self.input = discord.ui.TextInput(label=label, style=style, required=False)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        if self.parent_view.state["message_type"] == "plain":
            self.parent_view.state["plain_message"] = self.input.value
        else:
            self.parent_view.state["embed"][self.field] = self.input.value
        await update_live_preview(interaction, self.parent_view.state, self.parent_view)
        # Removed extra send_message to avoid InteractionResponded error and ghost messages

def safe_embed_color(color_str):
    if not color_str:
        return discord.Color.green()
    try:
        return int(color_str.replace("#", ""), 16)
    except Exception:
        return discord.Color.green()

def is_valid_url(url):
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))
