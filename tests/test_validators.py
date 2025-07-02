import pytest
from src.utils.validators import Validators


class TestValidators:
    
    @pytest.mark.parametrize("url,expected", [
        ("https://example.com", True),
        ("http://example.com", True),
        ("https://subdomain.example.com/path", True),
        ("http://localhost:8080", True),
        ("https://192.168.1.1:3000", True),
        ("ftp://example.com", False),
        ("example.com", False),
        ("", False),
        (None, False),
        ("not-a-url", False),
    ])
    def test_is_valid_url(self, url, expected):
        """Test URL validation."""
        assert Validators.is_valid_url(url) == expected
    
    @pytest.mark.parametrize("color,expected", [
        ("#FF0000", True),
        ("#ff0000", True),
        ("FF0000", True),
        ("#F00", True),
        ("F00", True),
        ("#GGGGGG", False),
        ("not-a-color", False),
        ("", False),
        (None, False),
    ])
    def test_is_valid_hex_color(self, color, expected):
        """Test hex color validation."""
        assert Validators.is_valid_hex_color(color) == expected
    
    @pytest.mark.parametrize("color,expected", [
        ("#FF0000", 0xFF0000),
        ("FF0000", 0xFF0000),
        ("#F00", 0xF00),
        ("invalid", None),
        (None, None),
    ])
    def test_sanitize_hex_color(self, color, expected):
        """Test hex color sanitization."""
        assert Validators.sanitize_hex_color(color) == expected
    
    @pytest.mark.parametrize("discord_id,expected", [
        ("123456789012345678", True),
        ("12345678901234567890", True),
        ("123", False),
        ("not-a-number", False),
        ("", False),
    ])
    def test_is_valid_discord_id(self, discord_id, expected):
        """Test Discord ID validation."""
        assert Validators.is_valid_discord_id(discord_id) == expected