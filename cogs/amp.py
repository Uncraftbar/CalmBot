"""
AMP (Application Management Panel) integration for CalmBot.
Provides server management commands for Minecraft instances.
"""

import asyncio
import re
import time

import config
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
        for index, inst in enumerate(self.instances[:25]):
            label = inst.friendly_name or inst.instance_name
            options.append(discord.SelectOption(label=label[:100], value=str(index)))
        
        if options:
            self.select = discord.ui.Select(
                placeholder="Select an instance to manage",
                options=options[:25]  # Discord limit
            )
            self.select.callback = self.select_callback
            self.add_item(self.select)
    
    async def select_callback(self, interaction: discord.Interaction):
        try:
            instance = self.instances[int(self.select.values[0])]
        except (ValueError, IndexError):
            instance = None
        
        if not instance:
            await interaction.response.send_message(
                embed=error_embed("Not Found", "Instance not found."),
                ephemeral=True
            )
            return
        
        selected_label = instance.friendly_name or instance.instance_name

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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await check_permissions(interaction):
            log.warning("AMP control permission denied for %s", interaction.user)
            return False
        return True

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
            self.add_item(TPSButton(instance, bot))
            self.add_item(ProfilerButton(instance, bot))
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


class ConfirmServerActionModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Reason", placeholder="Why is this action needed?", min_length=3, max_length=200)

    def __init__(self, instance, action):
        super().__init__(title=f"Confirm server {action}")
        self.instance = instance
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        if not await check_permissions(interaction):
            log.warning("AMP modal permission denied for %s", interaction.user)
            return
        await interaction.response.defer(ephemeral=True)
        name = self.instance.friendly_name or self.instance.instance_name
        method = {"restart": self.instance.restart_application, "stop": self.instance.stop_application,
                  "start": self.instance.start_application}[self.action]
        try:
            await method()
            reason = str(self.reason).strip()
            await interaction.followup.send(embed=success_embed(self.action.title() + " requested", f"**{name}**: {reason}"), ephemeral=True)
            log.warning(f"AMP AUDIT: {interaction.user} requested {self.action} for {name}; reason={reason!r}")
        except Exception as exc:
            log.error(f"Failed to {self.action} {name}: {exc}")
            await interaction.followup.send(embed=error_embed(self.action.title() + " failed", str(exc)), ephemeral=True)


class RestartButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Restart", style=discord.ButtonStyle.primary)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfirmServerActionModal(self.instance, "restart"))

class StopButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Stop", style=discord.ButtonStyle.danger)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfirmServerActionModal(self.instance, "stop"))

class StartButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Start", style=discord.ButtonStyle.success)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfirmServerActionModal(self.instance, "start"))


class TPSButton(discord.ui.Button):
    """Get server TPS using Spark."""
    
    def __init__(self, instance, bot):
        super().__init__(label="TPS", style=discord.ButtonStyle.secondary)
        self.instance = instance
        self.bot = bot
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            await interaction.followup.send("Fetching TPS data...", ephemeral=True)
            bridge = self.bot.get_cog("ChatBridge")
            if bridge is None:
                raise RuntimeError("Chat bridge event stream is unavailable")
            all_lines = await bridge.run_console_command(
                self.instance,
                "spark tps",
                re.compile(r"\[⚡\]: TPS from last"),
                timeout=8.0,
                quiet_period=0.75,
            )
            
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
    
    def __init__(self, instance, bot):
        super().__init__(label="Profiler", style=discord.ButtonStyle.secondary)
        self.instance = instance
        self.bot = bot
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            await interaction.followup.send(
                "⏳ Started 30-second profiler. Please wait for results...",
                ephemeral=True
            )
            bridge = self.bot.get_cog("ChatBridge")
            if bridge is None:
                raise RuntimeError("Chat bridge event stream is unavailable")
            all_lines = await bridge.run_console_command(
                self.instance,
                "spark profiler start --timeout 30",
                re.compile(r"spark\.lucko\.me"),
                timeout=45.0,
                quiet_period=0.5,
            )
            
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


class PublicServersView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.cog = cog
        self.refresh_lock = asyncio.Lock()
        self.last_refresh_at = 0.0
        self.refresh_cooldown = max(
            1.0, float(getattr(config, "PUBLIC_SERVER_REFRESH_COOLDOWN_SECONDS", 15))
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        # The cooldown is shared by this message. This prevents several users (or
        # one enthusiastic clicker) from hammering every AMP instance at once.
        retry_after = self.refresh_cooldown - (time.monotonic() - self.last_refresh_at)
        if self.refresh_lock.locked() or retry_after > 0:
            if self.refresh_lock.locked():
                message = "A server refresh is already in progress."
            else:
                message = f"Please wait {max(1, int(retry_after + 0.999))}s before refreshing again."
            await interaction.response.send_message(message, ephemeral=True)
            return

        async with self.refresh_lock:
            self.last_refresh_at = time.monotonic()
            await interaction.response.defer()
            embed = await self.cog.build_public_servers_embed()
            await interaction.edit_original_response(embed=embed, view=self)


# =============================================================================
# COG
# =============================================================================

class AMP(commands.Cog):
    """AMP server management commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("AMP cog initialized")
    
    async def build_public_servers_embed(self):
        instances = await fetch_valid_instances()

        # An empty allowlist preserves the historical behavior (show all). Values
        # can use either AMP's internal instance name or its friendly name.
        allowlist = {
            str(name).strip().casefold()
            for name in getattr(config, "PUBLIC_SERVER_ALLOWLIST", [])
            if str(name).strip()
        }
        if allowlist:
            instances = [
                inst for inst in instances
                if inst.instance_name.casefold() in allowlist
                or (inst.friendly_name and inst.friendly_name.casefold() in allowlist)
            ]

        embed = discord.Embed(title="Game Servers", color=discord.Color.blue())
        if not instances:
            embed.description = "No game servers are currently available."
            return embed
        for inst in instances[:25]:
            name = inst.friendly_name or inst.instance_name
            state, users = "Unknown", None
            try:
                status = await asyncio.wait_for(inst.get_instance_status(), timeout=8)
                state = get_instance_state(status)
                raw_users = getattr(status, "active_users", None)
                if isinstance(raw_users, (list, dict)):
                    users = len(raw_users)
            except Exception:
                pass
            emoji = "🟢" if state.lower() == "running" else "🔴" if state.lower() == "stopped" else "🟡"
            detail = f"Status: **{state}**" + (f" · Players: **{users}**" if users is not None else "")
            embed.add_field(name=f"{emoji} {name}"[:256], value=detail, inline=False)
        embed.set_footer(text="Live AMP status · use Refresh for current data")
        return embed

    @app_commands.command(name="servers", description="View the current status of community game servers")
    async def servers(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_public_servers_embed()
        await interaction.followup.send(embed=embed, view=PublicServersView(self))

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
