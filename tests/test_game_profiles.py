import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.game_profiles import get_game_profile, plain_chat_command
from cogs.modpack import Modpack


class GameProfilesTests(unittest.TestCase):
    def instance(self, name, module="Generic Module"):
        return SimpleNamespace(instance_name=name, friendly_name=name, module_display_name=module)

    def test_dune_is_managed_but_chat_is_not_assumed(self):
        p = get_game_profile(self.instance("Dune: Awakening"))
        self.assertEqual(p.key, "dune_awakening")
        self.assertTrue(p.amp_management)
        self.assertTrue(p.player_metrics)
        self.assertFalse(p.chat_send)
        self.assertFalse(p.chat_receive)

    def test_minecraft_capabilities(self):
        p = get_game_profile(self.instance("Otherworld", "Minecraft Java Edition"))
        self.assertTrue(p.minecraft)
        self.assertTrue(p.spark)
        self.assertTrue(p.chat_send)

    def test_future_game_can_be_configured_without_cog_changes(self):
        override = {"Future Server": {"profile": "generic", "label": "Future Game", "chat_send": True,
                                      "chat_command_template": "say {message}"}}
        with patch("cogs.game_profiles.config.GAME_INSTANCE_OVERRIDES", override, create=True):
            p = get_game_profile(self.instance("Future Server"))
        self.assertEqual(p.label, "Future Game")
        self.assertEqual(plain_chat_command(p, "hello\nworld"), "say hello world")


if __name__ == "__main__":
    unittest.main()

class GameSetupSurfaceTests(unittest.TestCase):
    def test_future_facing_setup_command_exists(self):
        from cogs.modpack import Modpack
        self.assertEqual(Modpack.setup_game.name, "setup_game")


class ConnectionInfoLabelTests(unittest.TestCase):
    def test_edit_connection_info_offers_both_url_labels(self):
        choices = Modpack.edit_connection_info._params["url_type"].choices
        self.assertEqual({choice.value for choice in choices}, {"Game URL", "Modpack URL"})

    def test_setup_game_uses_shared_setup_command(self):
        import inspect
        source = inspect.getsource(Modpack.setup_modpack.callback)
        self.assertIn('command_name == "setup_game"', source)
        self.assertIn('link_label = "Game URL"', source)
