import pytest
import json
from src.core.storage import StorageManager


class TestStorageManager:
    
    def test_load_nonexistent_file_returns_default(self, storage_manager):
        """Test loading non-existent file returns default value."""
        result = storage_manager.load("nonexistent.json", {"default": "value"})
        assert result == {"default": "value"}
    
    def test_save_and_load_data(self, storage_manager):
        """Test saving and loading data."""
        test_data = {"test": "data", "number": 42}
        
        # Save data
        success = storage_manager.save("test.json", test_data)
        assert success is True
        
        # Load data
        loaded_data = storage_manager.load("test.json")
        assert loaded_data == test_data
    
    def test_load_invalid_json_returns_default(self, storage_manager, tmp_path):
        """Test loading invalid JSON returns default value."""
        # Create invalid JSON file
        invalid_file = tmp_path / "invalid.json"
        invalid_file.write_text("{ invalid json")
        
        result = storage_manager.load("invalid.json", {"default": True})
        assert result == {"default": True}
    
    def test_save_handles_serialization_error(self, storage_manager):
        """Test saving handles serialization errors gracefully."""
        # Try to save non-serializable data
        class NonSerializable:
            pass
        
        result = storage_manager.save("test.json", NonSerializable())
        assert result is False