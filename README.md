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

## Optional LLM Responses

CalmBot can answer when directly mentioned or when a user replies to one of its messages. The mode is disabled by default and supports either an OpenAI-compatible Chat Completions endpoint or ChatGPT/Codex subscription credentials. Recent channel messages are supplied as conversational context.

Configuration can be managed without editing files. `/llm configure` selects the provider, model, reasoning effort, and OpenAI-compatible endpoint. `/llm openai_auth` privately stores an API key, while `/llm codex_auth` starts ChatGPT/Codex device authentication. Credentials are written under the Git-ignored `data/credentials/` directory with owner-only permissions and are never displayed back in Discord.

The file/environment settings below remain supported as initial defaults and for unattended deployments:

```python
# OpenAI-compatible mode
AI_CHAT_PROVIDER = "openai"
AI_CHAT_API_URL = "https://example.com/v1"  # or a full /chat/completions URL
AI_CHAT_API_KEY = "..."
AI_CHAT_MODEL = "model-name"

# Alternatively, ChatGPT/Codex subscription mode
AI_CHAT_PROVIDER = "codex"
AI_CHAT_CODEX_AUTH_PATH = "data/credentials/codex_auth.json"  # CalmBot-owned, chmod 600
AI_CHAT_CODEX_AUTH_INDEX = 0
AI_CHAT_MODEL = "gpt-5.6-luna"
AI_CHAT_REASONING_EFFORT = "low"
```

CalmBot reads, refreshes, and atomically saves its own Codex credential file. Do not point this at another application's live credential store. OAuth credentials and everything under `data/credentials/` are ignored by Git.

Moderator commands:

- `/llm enable` and `/llm disable` — toggle responses.
- `/llm status` — show provider readiness, model/reasoning, and limits without revealing credentials.
- `/llm configure` — select provider, model, reasoning effort, and endpoint.
- `/llm openai_auth` — save an OpenAI-compatible API key through an ephemeral modal.
- `/llm codex_auth` — authenticate CalmBot's own ChatGPT/Codex subscription through a device code.
- `/llm personality` — edit the personality text included in the system prompt.
- `/llm limits` — configure per-user cooldown, global requests/minute, concurrency, and context depth.

Rate-limited requests are silently suppressed with a clock reaction. Model output cannot ping users or roles.

## Commands

### AMP Management
- `/servers` - Show the public server-status dashboard.
- `/amp` - Open the server management dashboard.

The public dashboard can be restricted to selected AMP instances and its refresh cooldown can be adjusted in `config.py`:

```python
# Empty means all servers. Entries match AMP instance names or friendly names.
PUBLIC_SERVER_ALLOWLIST = ["Community SMP", "Creative"]
PUBLIC_SERVER_REFRESH_COOLDOWN_SECONDS = 15
```

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
## Multi-game AMP support

AMP lifecycle controls and public status/player metrics are game-neutral. Game-specific
features are declared in `cogs/game_profiles.py`; Dune: Awakening is included and can
be discovered and controlled through AMP's Generic Module. Spark buttons only appear
for Minecraft profiles.

Chat bridging is capability-gated. Dune chat is intentionally disabled until a stable,
verified Dune/AMP console receive format and outbound command are available; CalmBot
will not send Minecraft `tellraw` commands to it. Future games can normally be added
with `GAME_PROFILES` and `GAME_INSTANCE_OVERRIDES` configuration rather than cog edits:

```python
GAME_INSTANCE_OVERRIDES = {
    "Dune Dedicated": {"profile": "dune_awakening"},
    # Once a game's protocol is verified:
    # "Future Server": {"profile": "generic", "label": "Future Game",
    #                   "chat_send": True, "chat_command_template": "say {message}"},
}
```

The existing `/setup_modpack` command already accepts a game name and platform such as
`Steam`; its created category, channels, connection information, and notification role
work for Dune and other games. Legacy command names remain for compatibility.
