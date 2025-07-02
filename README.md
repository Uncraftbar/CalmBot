# CalmBot

A feature-rich Discord bot with server management capabilities, including AMP (Application Management Panel) integration for Minecraft server control, auto-send functionality, modpack management, and role management.

## Features

### 🎮 AMP Server Management
- **Instance Control**: Start, stop, and restart Minecraft server applications
- **TPS Monitoring**: Real-time server performance reporting using Spark profiler
- **Performance Profiling**: 30-second server profiling with detailed reports
- **Instance Status**: View current state of all managed server instances

### 📝 Auto-Send System
- **Keyword Triggers**: Automatically send messages based on configurable triggers
- **Rich Embeds**: Support for custom embeds with titles, descriptions, colors, and images
- **Live Editing**: Interactive message editor with real-time preview

### 🎯 Modpack Management
- **Category Setup**: Automated Discord category and channel creation for modpacks
- **Connection Info**: Manage server connection details and modpack links
- **Role Integration**: Automatic role assignment and management

### 🛡️ Role Management
- **Interactive Role Board**: User-friendly role selection interface
- **Custom Emojis**: Configurable emoji reactions for role assignment

## Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd discord-bot
   ```

2. **Install dependencies**
   ```bash
   pip install discord.py cc-ampapi
   ```

3. **Configure the bot**
   Create a `config.py` file with the following structure:
   ```python
   # config.py
   
   GUILD_IDS = [YOUR_GUILD_ID_HERE]  # List of Discord server IDs
   BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Discord bot token
   AMP_API_URL = "http://your-amp-server:port"  # AMP server URL
   AMP_USER = "your_amp_username"  # AMP username
   AMP_PASS = "your_amp_password"  # AMP password
   ```

4. **Run main.py!**


## Commands

### AMP Commands
- `/amp` - Display all AMP instances with management controls

### Auto-Send Commands
- `/autosend` - Manage automatic message triggers and responses

### Modpack Commands
- `/create_modpack` - Set up a new modpack category with channels and roles
- `/edit_connection_info` - Update modpack connection details

### Role Commands
- `/roles_board` - Create an interactive role selection interface


## Permissions

The bot requires the following Discord permissions:
- Send Messages
- Use Slash Commands
- Manage Roles
- Manage Channels
- Embed Links
- Add Reactions
- Read Message History

# You can use this link to invite the bot with the right permissions
https://discord.com/oauth2/authorize?client_id=REPLACE_WITH_CLIENT_ID&permissions=1126194380794992&integration_type=0&scope=bot 

## Security Notes

- Never commit `config.py` to version control
- Keep your bot token and AMP credentials secure
- Restrict admin commands to appropriate roles/users

## Requirements

- Python 3.8+
- discord.py
- cc-ampapi (for AMP integration)
- AMP (Application Management Panel) server for Minecraft management features

## 🧪 Testing

### Running Tests
```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run with coverage
pytest --cov=src

# Run specific test file
pytest tests/test_permissions.py
```

### Test Coverage
- Unit tests for core utilities
- Service layer testing
- Permission system validation
- Storage functionality
- Input validation

# 📁 Project Structure

```
src/
├── core/           # Core functionality (storage, permissions)
├── cogs/           # Discord command handlers
├── services/       # Business logic
├── models/         # Data structures
└── utils/          # Utility functions

tests/
├── test_services/  # Service layer tests
├── test_cogs/      # Command handler tests
└── conftest.py     # Test configuration
```

## 🔧 Development

### Code Quality
```bash
# Format code
black src/ tests/

# Lint code
flake8 src/ tests/

# Type checking
mypy src/
```

### Adding Features
1. Create data models in `src/models/`
2. Implement business logic in `src/services/`
3. Add command handlers in `src/cogs/`
4. Write tests in `tests/`




## License

This project is provided as-is for educational and personal use.


