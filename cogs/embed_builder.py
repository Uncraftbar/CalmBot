import discord
from discord.ext import commands
from discord import app_commands
from cogs.utils import has_admin_or_mod_permissions

class EmbedBuilder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="embed", description="Interactively create and send a custom embed to a channel")
    async def embed_builder(self, interaction: discord.Interaction):
        if not await has_admin_or_mod_permissions(interaction):
            return
        
        # Initial state
        state = {
            "title": "New Embed",
            "description": "Edit this description...",
            "color": None,
            "footer": None,
            "image_url": None,
            "thumbnail": None,
            "url": None,
            "fields": []
        }
        
        embed = self._build_embed(state)
        await interaction.response.send_message(
            "**Embed Builder**\nUse the buttons below to edit the embed. When ready, click 'Send to Channel'.",
            embed=embed,
            view=EmbedBuilderView(state, self._build_embed),
            ephemeral=True
        )

    def _build_embed(self, state):
        embed = discord.Embed(
            title=state["title"],
            description=state["description"],
            color=self._safe_color(state["color"]),
            url=state["url"]
        )
        if state["footer"]:
            embed.set_footer(text=state["footer"])
        if state["image_url"] and self._is_valid_url(state["image_url"]):
            embed.set_image(url=state["image_url"])
        if state["thumbnail"] and self._is_valid_url(state["thumbnail"]):
            embed.set_thumbnail(url=state["thumbnail"])
        
        return embed

    def _safe_color(self, color_str):
        if not color_str:
            return discord.Color.default()
        try:
            return int(color_str.replace("#", ""), 16)
        except:
            return discord.Color.default()

    def _is_valid_url(self, url):
        return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))

class EmbedBuilderView(discord.ui.View):
    def __init__(self, state, build_callback):
        super().__init__(timeout=900) # 15 minutes
        self.state = state
        self.build_callback = build_callback
        
        self.add_item(self.EditTitleButton(self))
        self.add_item(self.EditDescButton(self))
        self.add_item(self.EditColorButton(self))
        self.add_item(self.EditFooterButton(self))
        self.add_item(self.EditImageButton(self))
        self.add_item(self.SendButton(self))

    async def update_message(self, interaction: discord.Interaction):
        embed = self.build_callback(self.state)
        await interaction.response.edit_message(embed=embed, view=self)

    class EditTitleButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Title", style=discord.ButtonStyle.secondary, row=0)
            self.parent_view = parent_view
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EmbedInputModal(self.parent_view, "title", "Edit Title"))

    class EditDescButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Description", style=discord.ButtonStyle.secondary, row=0)
            self.parent_view = parent_view
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EmbedInputModal(self.parent_view, "description", "Edit Description", style=discord.TextStyle.paragraph))

    class EditColorButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Color", style=discord.ButtonStyle.secondary, row=1)
            self.parent_view = parent_view
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EmbedInputModal(self.parent_view, "color", "Hex Color (e.g. #FF0000)"))

    class EditFooterButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Footer", style=discord.ButtonStyle.secondary, row=1)
            self.parent_view = parent_view
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EmbedInputModal(self.parent_view, "footer", "Footer Text"))

    class EditImageButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Edit Image", style=discord.ButtonStyle.secondary, row=2)
            self.parent_view = parent_view
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(EmbedInputModal(self.parent_view, "image_url", "Image URL"))

    class SendButton(discord.ui.Button):
        def __init__(self, parent_view):
            super().__init__(label="Send to Channel", style=discord.ButtonStyle.success, row=3)
            self.parent_view = parent_view
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_message("Select a channel to send this embed to:", view=ChannelSelectView(self.parent_view.state, self.parent_view.build_callback), ephemeral=True)

class EmbedInputModal(discord.ui.Modal):
    def __init__(self, parent_view, key, label, style=discord.TextStyle.short):
        super().__init__(title=label)
        self.parent_view = parent_view
        self.key = key
        self.input = discord.ui.TextInput(label=label, style=style, required=False)
        if key in parent_view.state and parent_view.state[key]:
            self.input.default = str(parent_view.state[key])
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state[self.key] = self.input.value
        await self.parent_view.update_message(interaction)

class ChannelSelectView(discord.ui.View):
    def __init__(self, state, build_callback):
        super().__init__(timeout=60)
        self.state = state
        self.build_callback = build_callback
        
        self.add_item(self.ChannelSelect(self))

    class ChannelSelect(discord.ui.ChannelSelect):
        def __init__(self, parent_view):
            super().__init__(placeholder="Select channel...", channel_types=[discord.ChannelType.text, discord.ChannelType.news], min_values=1, max_values=1)
            self.parent_view = parent_view
        
        async def callback(self, interaction: discord.Interaction):
            # values[0] might be an AppCommandChannel which lacks .send()
            # Fetch the actual GuildChannel object
            selected_channel = self.values[0]
            channel = interaction.guild.get_channel(selected_channel.id)
            
            if not channel:
                await interaction.response.send_message(f"❌ Could not resolve channel {selected_channel.name}.", ephemeral=True)
                return

            embed = self.parent_view.build_callback(self.parent_view.state)
            try:
                await channel.send(embed=embed)
                await interaction.response.send_message(f"✅ Embed sent to {channel.mention}!", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message(f"❌ I don't have permission to send messages in {channel.mention}.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Error sending embed: {str(e)}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(EmbedBuilder(bot))