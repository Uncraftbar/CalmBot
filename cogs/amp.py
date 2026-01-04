import discord
from discord.ext import commands
from discord import app_commands
from ampapi import Bridge, AMPControllerInstance
from ampapi.dataclass import APIParams
import config
from cogs.utils import has_admin_or_mod_permissions, fetch_valid_instances

class InstanceActionView(discord.ui.View):
    def __init__(self, instances, bot):
        super().__init__(timeout=120)
        self.bot = bot
        self.instances = instances
        options = []
        for i in self.instances:
            label = i.friendly_name or i.instance_name
            options.append(discord.SelectOption(label=label, value=label))
        self.select = discord.ui.Select(placeholder="Select an instance to manage", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_label = self.select.values[0]
        instance = next((i for i in self.instances if (i.friendly_name or i.instance_name) == selected_label), None)
        if not instance:
            await interaction.response.send_message("Instance not found.", ephemeral=True)
            return
        # Get state
        state = "Unknown"
        try:
            status = await instance.get_instance_status()
            if hasattr(status, 'state') and status.state:
                state_str = str(status.state)
                if '.' in state_str:
                    state_val = state_str.split('.')[-1].replace('_', ' ').capitalize()
                else:
                    state_val = state_str.replace('_', ' ').capitalize()
                if state_val.lower() == 'ready':
                    state = 'Running'
                else:
                    state = state_val
            elif hasattr(status, 'running'):
                state = 'Running' if status.running else 'Stopped'
        except Exception:
            pass
        # Show action buttons based on state
        view = InstanceControlView(instance, state, self.instances, self.bot)
        await interaction.response.edit_message(content=f"**{selected_label}** is currently **{state}**.", embed=None, view=view)

class InstanceControlView(discord.ui.View):
    def __init__(self, instance, state, all_instances, bot):
        super().__init__(timeout=60)
        self.instance = instance
        self.state = state
        self.all_instances = all_instances
        self.bot = bot
        
        if state.lower() == 'running':
            self.add_item(RestartButton(instance))
            self.add_item(StopButton(instance))
            self.add_item(TPSButton(instance))
            self.add_item(ProfilerButton(instance))
        else:
            self.add_item(StartButton(instance))
        
        self.add_item(BackButton(all_instances, bot))

class BackButton(discord.ui.Button):
    def __init__(self, all_instances, bot):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, row=4)
        self.all_instances = all_instances
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        # Restore the original embed view (InstanceActionView)
        # We need to rebuild the main embed or just go back to the list
        # Since the original command had an embed, we should ideally try to restore it,
        # but for now let's just restore the View and a title.
        
        # Re-fetch instance states for the main embed if possible, or just use a generic message
        embed = discord.Embed(title="AMP Instances", color=discord.Color.blue())
        for i in self.all_instances:
            # We skip fetching fresh state to avoid lag, just list them
            embed.add_field(name=i.friendly_name or i.instance_name, value="Select to manage", inline=False)
        
        await interaction.response.edit_message(content=None, embed=embed, view=InstanceActionView(self.all_instances, self.bot))

class RestartButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Restart", style=discord.ButtonStyle.primary)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.instance.restart_application()
            await interaction.followup.send("Application is restarting.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to restart application: {e}", ephemeral=True)

class StopButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Stop", style=discord.ButtonStyle.danger)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.instance.stop_application()
            await interaction.followup.send("Application is stopping.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to stop application: {e}", ephemeral=True)

class StartButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Start", style=discord.ButtonStyle.success)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.instance.start_application()
            await interaction.followup.send("Application is starting.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to start application: {e}", ephemeral=True)

class TPSButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="TPS", style=discord.ButtonStyle.secondary)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            import asyncio
            await self.instance.send_console_message("spark tps")
            await interaction.followup.send("Sent 'spark tps' to the server. Waiting for output...", ephemeral=True)
            await asyncio.sleep(1)
            try:
                updates = await self.instance.get_updates(format_data=True)
                entries = getattr(updates, 'console_entries', None)
                if entries is None or not entries:
                    await interaction.followup.send("No console_entries found.", ephemeral=True)
                    return
                
                # Extract all lines using the working method (attribute access)
                all_lines = []
                for entry in entries:
                    if hasattr(entry, 'contents') and entry.contents:
                        all_lines.append(str(entry.contents))
                
                if not all_lines:
                    await interaction.followup.send("No content extracted.", ephemeral=True)
                    return
                
                # Find the last TPS block - search forward from the last "TPS from last" line
                tps_start_idx = None
                for i in range(len(all_lines) - 1, -1, -1):
                    if "[⚡]: TPS from last" in all_lines[i]:
                        tps_start_idx = i
                        break
                
                if tps_start_idx is not None:
                    # Collect all consecutive [⚡] lines starting from tps_start_idx
                    tps_lines = []
                    for i in range(tps_start_idx, len(all_lines)):
                        line = all_lines[i]
                        if line.startswith("[⚡]"):
                            tps_lines.append(line)
                        elif tps_lines:  # Stop when we hit a non-[⚡] line after collecting some
                            break
                else:
                    # Fallback: just get all recent [⚡] lines
                    tps_lines = [line for line in all_lines if "[⚡]" in line][-9:]
                
                # Create embed
                embed = discord.Embed(
                    title="⚡ Server TPS Report",
                    color=discord.Color.green(),
                    description=f"```\n{chr(10).join(tps_lines)}\n```"
                )
                embed.set_footer(text="Generated by Spark TPS profiler")
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except Exception as e:
                await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Command failed: {str(e)}", ephemeral=True)

class ProfilerButton(discord.ui.Button):
    def __init__(self, instance):
        super().__init__(label="Profiler", style=discord.ButtonStyle.secondary)
        self.instance = instance
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            import asyncio
            await self.instance.send_console_message("spark profiler start --timeout 30")
            await interaction.followup.send("Started 30-second profiler. Please wait for results...", ephemeral=True)
            
            # Wait 35 seconds to ensure the profiler has completed
            await asyncio.sleep(35)
            
            try:
                updates = await self.instance.get_updates(format_data=True)
                entries = getattr(updates, 'console_entries', None)
                if entries is None or not entries:
                    await interaction.followup.send("No console_entries found.", ephemeral=True)
                    return
                
                # Extract all lines using the working method (attribute access)
                all_lines = []
                for entry in entries:
                    if hasattr(entry, 'contents') and entry.contents:
                        all_lines.append(str(entry.contents))
                
                if not all_lines:
                    await interaction.followup.send("No content extracted.", ephemeral=True)
                    return
                
                # Look for the spark.lucko.me link in recent lines
                profiler_link = None
                for line in reversed(all_lines):
                    if "spark.lucko.me" in line:
                        profiler_link = line.strip()
                        break
                
                if profiler_link:
                    # Create embed with the profiler link
                    embed = discord.Embed(
                        title="⚡ Server Profiler Results",
                        color=discord.Color.blue(),
                        description=f"Profiler completed! View results:\n{profiler_link}"
                    )
                    embed.set_footer(text="Generated by Spark profiler (30s sample)")
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send("Profiler completed but no results link found. Check console manually.", ephemeral=True)
                
            except Exception as e:
                await interaction.followup.send(f"Error retrieving profiler results: {str(e)}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Command failed: {str(e)}", ephemeral=True)

class AMP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.amp_url = config.AMP_API_URL
        self.amp_user = config.AMP_USER
        self.amp_pass = config.AMP_PASS
        self.api_params = APIParams(url=self.amp_url, user=self.amp_user, password=self.amp_pass)
        self.bridge = Bridge(api_params=self.api_params)
        self.ads = AMPControllerInstance()

    @app_commands.command(name="amp", description="AMP server management commands")
    async def amp(self, interaction: discord.Interaction):
        if not await has_admin_or_mod_permissions(interaction):
            return
        try:
            instances = await fetch_valid_instances()
            if not instances:
                embed = discord.Embed(title="AMP Instances", description="No AMP instances found.", color=discord.Color.red())
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                embed = discord.Embed(title="AMP Instances", color=discord.Color.blue())
                for i in instances:
                    state = "Unknown"
                    try:
                        status = await i.get_instance_status()
                        # Try to get the most descriptive state
                        if hasattr(status, 'state') and status.state:
                            state_str = str(status.state)
                            if '.' in state_str:
                                state_val = state_str.split('.')[-1].replace('_', ' ').capitalize()
                            else:
                                state_val = state_str.replace('_', ' ').capitalize()
                            # Treat 'Ready' as 'Running' for user clarity
                            if state_val.lower() == 'ready':
                                state = 'Running'
                            else:
                                state = state_val
                        elif hasattr(i, 'app_state') and i.app_state:
                            state_str = str(i.app_state)
                            if '.' in state_str:
                                state_val = state_str.split('.')[-1].replace('_', ' ').capitalize()
                            else:
                                state_val = state_str.replace('_', ' ').capitalize()
                            if state_val.lower() == 'ready':
                                state = 'Running'
                            else:
                                state = state_val
                        elif hasattr(status, 'running'):
                            state = 'Running' if status.running else 'Stopped'
                    except Exception:
                        pass
                    desc = f"State: {state}"
                    embed.add_field(name=i.friendly_name or i.instance_name, value=desc, inline=False)
                await interaction.response.send_message(embed=embed, view=InstanceActionView(instances, self.bot), ephemeral=True)
        except Exception as e:
            embed = discord.Embed(title="AMP Error", description=f"Error fetching AMP instances: {e}", color=discord.Color.red())
            await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(AMP(bot))
