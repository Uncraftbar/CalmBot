"""
Embed builder for CalmBot.
Interactive embed creation and sending.
"""

import discord
from discord.ext import commands
from discord import app_commands

from cogs.utils import (
    get_logger,
    admin_only,
    is_valid_url,
    safe_embed_color,
    success_embed,
    error_embed
)

log = get_logger("embed_builder")


class EmbedInputModal(discord.ui.Modal):
    """Modal for editing a single embed field."""
    
    def __init__(self, parent_view, key: str, label: str, style=discord.TextStyle.short):
        super().__init__(title=label)
        self.parent_view = parent_view
        self.key = key
        
        self.input = discord.ui.TextInput(label=label, style=style, required=False)
        
        current = parent_view.state.get(key)
        if current:
            self.input.default = str(current)
        
        self.add_item(self.input)
    
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.state[self.key] = self.input.value or None
        await self.parent_view.update_preview(interaction)


class ChannelSelectView(discord.ui.View):
    """View for selecting a channel to send the embed to."""
    
    def __init__(self, state: dict, build_func):
        super().__init__(timeout=60)
        self.state = state
        self.build_func = build_func
    
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select channel...",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        min_values=1,
        max_values=1
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        selected = select.values[0]
        
        # Get the actual channel object
        if not interaction.guild:
            await interaction.response.send_message(
                embed=error_embed("Error", "Must be used in a server."),
                ephemeral=True
            )
            return
        
        channel = interaction.guild.get_channel(selected.id)
        if not channel:
            await interaction.response.send_message(
                embed=error_embed("Error", f"Could not find channel {selected.name}"),
                ephemeral=True
            )
            return
        
        embed = self.build_func(self.state)
        
        try:
            await channel.send(embed=embed)
            await interaction.response.send_message(
                embed=success_embed("Sent", f"Embed sent to {channel.mention}!"),
                ephemeral=True
            )
            log.info(f"Embed sent to {channel.name} by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("Permission Error", f"Cannot send to {channel.mention}"),
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                embed=error_embed("Error", str(e)),
                ephemeral=True
            )


class EmbedBuilderView(discord.ui.View):
    """Interactive embed builder view."""
    
    def __init__(self, state: dict, build_func):
        super().__init__(timeout=900)  # 15 minutes
        self.state = state
        self.build_func = build_func
    
    async def update_preview(self, interaction: discord.Interaction):
        """Update the message to show current preview."""
        embed = self.build_func(self.state)
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="Edit Title", style=discord.ButtonStyle.secondary, row=0)
    async def edit_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedInputModal(self, "title", "Edit Title"))
    
    @discord.ui.button(label="Edit Description", style=discord.ButtonStyle.secondary, row=0)
    async def edit_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            EmbedInputModal(self, "description", "Edit Description", discord.TextStyle.paragraph)
        )
    
    @discord.ui.button(label="Edit Color", style=discord.ButtonStyle.secondary, row=1)
    async def edit_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedInputModal(self, "color", "Hex Color (e.g. #FF0000)"))
    
    @discord.ui.button(label="Edit Footer", style=discord.ButtonStyle.secondary, row=1)
    async def edit_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedInputModal(self, "footer", "Footer Text"))
    
    @discord.ui.button(label="Edit Image", style=discord.ButtonStyle.secondary, row=2)
    async def edit_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedInputModal(self, "image_url", "Image URL"))
    
    @discord.ui.button(label="Send to Channel", style=discord.ButtonStyle.success, row=3)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select a channel to send this embed to:",
            view=ChannelSelectView(self.state, self.build_func),
            ephemeral=True
        )


class EmbedBuilder(commands.Cog):
    """Interactive embed creation and sending."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("EmbedBuilder cog initialized")
    
    def _build_embed(self, state: dict) -> discord.Embed:
        """Build a discord.Embed from state dict."""
        embed = discord.Embed(
            title=state.get("title"),
            description=state.get("description"),
            color=safe_embed_color(state.get("color")),
            url=state.get("url")
        )
        
        if state.get("footer"):
            embed.set_footer(text=state["footer"])
        
        if is_valid_url(state.get("image_url")):
            embed.set_image(url=state["image_url"])
        
        if is_valid_url(state.get("thumbnail")):
            embed.set_thumbnail(url=state["thumbnail"])
        
        return embed
    
    @app_commands.command(name="embed", description="Create and send a custom embed")
    @admin_only()
    async def embed_builder(self, interaction: discord.Interaction):
        """Open the interactive embed builder."""
        state = {
            "title": "New Embed",
            "description": "Edit this description...",
            "color": None,
            "footer": None,
            "image_url": None,
            "thumbnail": None,
            "url": None
        }
        
        embed = self._build_embed(state)
        
        await interaction.response.send_message(
            "**Embed Builder**\nUse the buttons below to edit. Click 'Send to Channel' when ready.",
            embed=embed,
            view=EmbedBuilderView(state, self._build_embed),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedBuilder(bot))
