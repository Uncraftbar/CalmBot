# CalmBot

A feature-rich Discord bot designed for Minecraft server communities. It offers deep integration with **AMP (Application Management Panel)**, advanced auto-responders, and automated modpack category management.

## Features

### 🎮 AMP Server Management
- **Instance Control**: Start, stop, and restart Minecraft server instances directly from Discord.
- **TPS Monitoring**: Real-time server performance reporting using the Spark profiler.
- **Performance Profiling**: Run 30-second profiles and get detailed analysis links.
- **Instance Status**: View the live state of all managed server instances.

### 📝 Advanced Auto-Send
- **Interactive Editor**: Create and edit auto-responses with a live preview UI.
- **Rich Embeds**: Design beautiful messages with titles, colors, images, and footers.
- **Smart Triggers**: Trigger by keywords, user mentions, or role mentions.
- **Conditional Logic**: Restrict responses to specific **channels**, **roles**, **message lengths**, or use **Regex** patterns.

### 🎯 Modpack Management
- **One-Click Setup**: Automatically create categories, channels (`#general`, `#technical-help`, `#connection-info`), and notification roles for new modpacks.
- **Migration**: Easily convert existing manual categories into the bot's managed system.
- **Connection Info**: Manage server IP and modpack link embeds with a simple command.

### 🛡️ Role Management
- **Reaction Roles Board**: Create a self-updating "Roles Board" where users can react to get modpack update roles.
- **Auto-Sync**: The bot automatically adds new modpack roles to the board.
- **Robust Synchronization**: Detects and cleans up "orphaned" roles (where the modpack was deleted) to keep your roles board tidy.

## Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd discord-bot
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the bot**
   Create a `config.py` file in the root directory:
   ```python
   # config.py
   
   GUILD_IDS = [123456789012345678]  # Your Discord Server ID(s)
   BOT_TOKEN = "YOUR_DISCORD_BOT_TOKEN"
   
   # AMP Configuration
   AMP_API_URL = "http://localhost:8080"  # Your AMP instance URL
   AMP_USER = "admin"
   AMP_PASS = "password"
   ```

4. **Run the bot**
   ```bash
   python main.py
   ```

## Commands

### AMP Management
- `/amp` - Open the server management dashboard.

### Auto-Send
- `/autosend add` - Create a new auto-responder.
- `/autosend list` - View, edit, or delete existing auto-responders.
- `/autosend help` - View detailed help for the system.

### Modpack Tools
- `/setup_modpack` - Create a new modpack category with channels and a role.
- `/migrate_modpack` - Convert an existing category to be managed by the bot.
- `/delete_modpack` - safely delete a modpack category, channels, and role.
- `/edit_connection_info` - Update the connection info message in a modpack channel.

### Role System
- `/setup_roles_board` - Create or update the message for role reactions.
- `/sync_roles_board` - Scan for missing/deleted modpacks and clean up the roles board.

## Requirements

- Python 3.8+
- `discord.py`
- `cc-ampapi`
- `mcstatus`
- `dnspython`

## Security

- **Never** commit your `config.py` to GitHub or share it publicly.
- Ensure the bot has the `Administrator` permission or specific rights to Manage Channels, Manage Roles, and Manage Messages.

## License

This project is provided as-is for educational and personal use.