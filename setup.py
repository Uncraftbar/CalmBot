#!/usr/bin/env python3
"""
Setup script for the Discord bot project.
"""

import subprocess
import sys
from pathlib import Path


def install_requirements():
    """Install required packages."""
    print("Installing requirements...")
    
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)
        print("✅ Requirements installed successfully!")
    except subprocess.CalledProcessError:
        print("❌ Failed to install requirements")
        return False
    
    return True


def install_dev_requirements():
    """Install development requirements."""
    print("Installing development requirements...")
    
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements-dev.txt"], check=True)
        print("✅ Development requirements installed successfully!")
    except subprocess.CalledProcessError:
        print("❌ Failed to install development requirements")
        return False
    
    return True


def setup_config():
    """Setup configuration file."""
    config_file = Path("config.py")
    example_file = Path("config.example.py")
    
    if not config_file.exists() and example_file.exists():
        print("Creating config.py from example...")
        config_file.write_text(example_file.read_text())
        print("⚠️  Please edit config.py with your actual bot settings!")
    elif config_file.exists():
        print("✅ config.py already exists")
    else:
        print("❌ No config example found")


def main():
    """Main setup function."""
    print("🚀 Setting up Discord Bot project...")
    
    # Install requirements
    if not install_requirements():
        return
    
    # Install dev requirements
    install_dev_requirements()
    
    # Setup config
    setup_config()
    
    print("\n🎉 Setup complete!")
    print("\nNext steps:")
    print("1. Edit config.py with your bot token and guild IDs")
    print("2. Run tests: pytest")
    print("3. Start the bot: python main.py")


if __name__ == "__main__":
    main()
