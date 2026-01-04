"""
AMP (Application Management Panel) integration for CalmBot.
Provides server management commands for Minecraft instances.
"""

import asyncio
import discord
from discord.ext import commands
from discord import app_commands

from cogs.utils import (
    get_logger, 
    check_permissions, 
    admin_only,
    fetch_valid_instances, 
    get_instance_state,
    info_embed,
    error_embed,
    success_embed
)

log = get_logger("amp")


# =============================================================================
# VIEWS AND BUTTONS
# =============================================================================

class InstanceActionView(discord.ui.View):
    """Main view for selecting an AMP instance to manage."""
    
    def __init__(self, instances: list, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot
        self.instances = instances
        
        # Build select options
        options = []
        for inst in self.instances:
            label = inst.friendly_name or inst.instance_name
            options.append(discord.SelectOption(label=label[:100], value=label))
        
        if options:
            self.select = discord.ui.Select(
                placeholder="Select an instance to manage",
                options=options[:25]  # Discord limit
            )
            self.select.callback = self.select_callback
            self.add_item(self.select)
    
    async def select_callback(self, interaction: discord.Interaction):
        selected_label = self.select.values[0]
        instance = next(
            (i for i in self.instances if (i.friendly_name or i.instance_name) == selected_label),
            None
        )
        
        if not instance:
            await interaction.response.send_message(
                embed=error_embed("Not Found", "Instance not found."),
                ephemeral=True
            )
            return
        
        # Get current state
        state = "Unknown"
        try:
            status = await instance.get_instance_status()
            state = get_instance_state(status)
        except Exception as e:
            log.debug(f"Failed to get status for {selected_label}: {e}")
        
        # Show control view
        view = InstanceControlView(instance, state, self.instances, self.bot)
        await interaction.response.edit_message(
            content=f"**{selected_label}** is currently **{state}**.",
            embed=None,
            view=view
        )


class InstanceControlView(discord.ui.View):
    """Control view with action buttons for a specific instance."""
    
    def __init__(self, instance, state: str, all_instances: list, bot: commands.Bot):
        super().__init__(timeout=60)
        self.instance = instance
        self.state = state
        self.all_instances = all_instances
        self.bot = bot
        
        # Add appropriate buttons based on state
        if state.lower() == 'running':
            self.add_item(RestartButton(instance))
            self.add_item(StopButton(instance))
            self.add_item(TPSButton(instance))
            self.add_item(ProfilerButton(instance))
        else:
            self.add_item(StartButton(instance))
        
        self.add_item(BackButton(all_instances, bot))


class BackButton(discord.ui.Button):
    """Return to instance selection."""
    
    def __init__(self, all_instances: list, bot: commands.Bot):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, row=4)
        self.all_instances = all_instances
        self.bot = bot
    
    async def callback(self, interaction: discord.Interaction):
        embed = info_embed(
            "AMP Instances",
            "Select an instance to manage it."
        )
        
        for inst in self.all_instances:
            name = inst.friendly_name or inst.instance_name
            embed.add_field(name=name, value="Click to manage", inline=False)
        
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=InstanceActionView(self.all_instances, self.bot)
        )


class RestartButton(discord.ui.Button):
    """Restart the server application."""
    
    def __init__(self, instance):
        super().__init__(label="Restart", style=discord.ButtonStyle.primary)
        self.instance = instance
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.instance.restart_application()
            await interaction.followup.send(
                embed=success_embed("Restarting", "Application is restarting..."),
                ephemeral=True
            )
            log.info(f"{interaction.user} restarted {self.instance.friendly_name or self.instance.instance_name}")
        except Exception as e:
            log.error(f"Failed to restart: {e}")
            await interaction.followup.send(
                embed=error_embed("Restart Failed", str(e)),
                ephemeral=True
            )


class StopButton(discord.ui.Button):
    """Stop the server application."""
    
    def __init__(self, instance):
        super().__init__(label="Stop", style=discord.ButtonStyle.danger)
        self.instance = instance
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.instance.stop_application()
            await interaction.followup.send(
                embed=success_embed("Stopping", "Application is stopping..."),
                ephemeral=True
            )
            log.info(f"{interaction.user} stopped {self.instance.friendly_name or self.instance.instance_name}")
        except Exception as e:
            log.error(f"Failed to stop: {e}")
            await interaction.followup.send(
                embed=error_embed("Stop Failed", str(e)),
                ephemeral=True
            )


class StartButton(discord.ui.Button):
    """Start the server application."""
    
    def __init__(self, instance):
        super().__init__(label="Start", style=discord.ButtonStyle.success)
        self.instance = instance
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.instance.start_application()
            await interaction.followup.send(
                embed=success_embed("Starting", "Application is starting..."),
                ephemeral=True
            )
            log.info(f"{interaction.user} started {self.instance.friendly_name or self.instance.instance_name}")
        except Exception as e:
            log.error(f"Failed to start: {e}")
            await interaction.followup.send(
                embed=error_embed("Start Failed", str(e)),
                ephemeral=True
            )


class TPSButton(discord.ui.Button):
    """Get server TPS using Spark."""
    
    def __init__(self, instance):
        super().__init__(label="TPS", style=discord.ButtonStyle.secondary)
        self.instance = instance
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self.instance.send_console_message("spark tps")
            await interaction.followup.send(
                "Fetching TPS data...",
                ephemeral=True
            )
            
            await asyncio.sleep(1)
            
            updates = await self.instance.get_updates(format_data=True)
            entries = getattr(updates, 'console_entries', None)
            
            if not entries:
                await interaction.followup.send(
                    embed=error_embed("No Data", "No console output received."),
                    ephemeral=True
                )
                return
            
            # Extract TPS lines
            all_lines = []
            for entry in entries:
                if hasattr(entry, 'contents') and entry.contents:
                    all_lines.append(str(entry.contents))
            
            if not all_lines:
                await interaction.followup.send(
                    embed=error_embed("No Data", "No content extracted from console."),
                    ephemeral=True
                )
                return
            
            # Find the TPS block
            tps_start_idx = None
            for i in range(len(all_lines) - 1, -1, -1):
                if "[⚡]: TPS from last" in all_lines[i]:
                    tps_start_idx = i
                    break
            
            if tps_start_idx is not None:
                tps_lines = []
                for i in range(tps_start_idx, len(all_lines)):
                    line = all_lines[i]
                    if line.startswith("[⚡]"):
                        tps_lines.append(line)
                    elif tps_lines:
                        break
            else:
                tps_lines = [line for line in all_lines if "[⚡]" in line][-9:]
            
            if tps_lines:
                embed = discord.Embed(
                    title="⚡ Server TPS Report",
                    color=discord.Color.green(),
                    description=f"```\n{chr(10).join(tps_lines)}\n```"
                )
                embed.set_footer(text="Generated by Spark TPS profiler")
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(
                    embed=error_embed("No TPS Data", "Could not find TPS output. Is Spark installed?"),
                    ephemeral=True
                )
                
        except Exception as e:
            log.error(f"TPS command failed: {e}")
            await interaction.followup.send(
                embed=error_embed("Error", str(e)),
                ephemeral=True
            )


class ProfilerButton(discord.ui.Button):
    """Run a 30-second performance profile."""
    
    def __init__(self, instance):
        super().__init__(label="Profiler", style=discord.ButtonStyle.secondary)
        self.instance = instance
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self.instance.send_console_message("spark profiler start --timeout 30")
            await interaction.followup.send(
                "⏳ Started 30-second profiler. Please wait for results...",
                ephemeral=True
            )
            
            # Wait for profiler to complete
            await asyncio.sleep(35)
            
            updates = await self.instance.get_updates(format_data=True)
            entries = getattr(updates, 'console_entries', None)
            
            if not entries:
                await interaction.followup.send(
                    embed=error_embed("No Data", "No console output received."),
                    ephemeral=True
                )
                return
            
            # Extract lines and find profiler link
            all_lines = []
            for entry in entries:
                if hasattr(entry, 'contents') and entry.contents:
                    all_lines.append(str(entry.contents))
            
            profiler_link = None
            for line in reversed(all_lines):
                if "spark.lucko.me" in line:
                    profiler_link = line.strip()
                    break
            
            if profiler_link:
                embed = discord.Embed(
                    title="⚡ Server Profiler Results",
                    color=discord.Color.blue(),
                    description=f"Profiler completed! View results:\n{profiler_link}"
                )
                embed.set_footer(text="Generated by Spark profiler (30s sample)")
                await interaction.followup.send(embed=embed, ephemeral=True)
                log.info(f"Profiler completed for {self.instance.friendly_name or self.instance.instance_name}")
            else:
                await interaction.followup.send(
                    embed=error_embed(
                        "No Results",
                        "Profiler completed but no results link found. Check console manually."
                    ),
                    ephemeral=True
                )
                
        except Exception as e:
            log.error(f"Profiler command failed: {e}")
            await interaction.followup.send(
                embed=error_embed("Error", str(e)),
                ephemeral=True
            )


# =============================================================================
# COG
# =============================================================================

class AMP(commands.Cog):
    """AMP server management commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("AMP cog initialized")
    
    @app_commands.command(name="amp", description="AMP server management dashboard")
    @admin_only()
    async def amp(self, interaction: discord.Interaction):
        """Open the AMP server management interface."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            instances = await fetch_valid_instances()
            
            if not instances:
                await interaction.followup.send(
                    embed=error_embed(
                        "No Instances",
                        "No AMP instances found. Check your AMP configuration."
                    ),
                    ephemeral=True
                )
                return
            
            # Build status embed
            embed = info_embed(
                "AMP Instances",
                "Select an instance to manage it."
            )
            
            for inst in instances:
                name = inst.friendly_name or inst.instance_name
                state = "Unknown"
                
                try:
                    status = await inst.get_instance_status()
                    state = get_instance_state(status)
                except Exception:
                    pass
                
                # Color-coded status
                status_emoji = "🟢" if state.lower() == "running" else "🔴" if state.lower() == "stopped" else "🟡"
                embed.add_field(
                    name=f"{status_emoji} {name}",
                    value=f"State: **{state}**",
                    inline=False
                )
            
            await interaction.followup.send(
                embed=embed,
                view=InstanceActionView(instances, self.bot),
                ephemeral=True
            )
            
        except Exception as e:
            log.error(f"AMP command failed: {e}")
            await interaction.followup.send(
                embed=error_embed(
                    "AMP Error",
                    f"Failed to connect to AMP: {e}"
                ),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AMP(bot))
