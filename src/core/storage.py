import json
import os
from typing import Any, Dict, Optional
from pathlib import Path


class StorageManager:
    """Centralized storage management for JSON files."""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
    
    def load(self, filename: str, default: Any = None) -> Any:
        """Load data from JSON file with error handling."""
        filepath = self.data_dir / filename
        
        if not filepath.exists():
            return default
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error loading {filename}: {e}")
            return default
    
    def save(self, filename: str, data: Any) -> bool:
        """Save data to JSON file with error handling."""
        filepath = self.data_dir / filename
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except (IOError, TypeError) as e:
            print(f"Error saving {filename}: {e}")
            return False