import json
import os
from ampapi import AMPControllerInstance

ROLES_BOARD_FILE = "data/roles_board.json"
CHAT_BRIDGE_FILE = "data/chat_bridge.json"


async def fetch_valid_instances():
    """
    Fetches and filters AMP instances, excluding ADS/Controller.
    Returns a list of valid instances.
    """
    ads = AMPControllerInstance()
    
    # Force session clear to prevent server-side caching per session
    if hasattr(ads, '_bridge') and hasattr(ads._bridge, '_sessions'):
        ads._bridge._sessions.clear()

    fetched_instances = await ads.get_instances(format_data=True)
    
    if not fetched_instances:
        return []

    valid_instances = []
    for inst in fetched_instances:
        # Use safe getattr
        mod_name = str(getattr(inst, 'module_display_name', '')).lower()
        friendly_name = str(getattr(inst, 'friendly_name', '')).strip().lower()
        
        # Filter ADS/Controller
        if (mod_name in ['application deployment service', 'ads module', 'controller']) or \
           (friendly_name == 'ads'):
            continue
        
        # Check required attributes
        if not hasattr(inst, 'instance_name'):
            continue
            
        valid_instances.append(inst)
        
    return valid_instances


def load_json(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {filename}: {e}")
    return default

def save_json(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Error saving {filename}: {e}")

async def has_admin_or_mod_permissions(interaction):
    if interaction.user.guild_permissions.administrator:
        return True
    mod_role_names = ["Moderators", "Admins"]
    for role in interaction.user.roles:
        if role.name in mod_role_names or role.permissions.manage_guild or role.permissions.manage_channels:
            return True
    await interaction.response.send_message("You don't have permission to use this command. Only administrators and moderators can use it.", ephemeral=True)
    return False

async def find_category_by_name(guild, input_name):
    category = next((c for c in guild.categories if c.name == input_name), None)
    if category:
        return category
    for category in guild.categories:
        name = category.name
        if "[" in name and "]" in name:
            base_name = name.split("[")[0].strip()
            if base_name.lower() == input_name.lower():
                return category
    return None
