import inspect
import unittest

from cogs.roles_board import RolesBoard, RolesBoardEditorView


class RolesBoardEditorSurfaceTests(unittest.TestCase):
    def test_editor_replaces_sync_command(self):
        self.assertEqual(RolesBoard.edit_roles_board.name, "edit_roles_board")
        self.assertFalse(hasattr(RolesBoard, "sync_roles_board"))

    def test_board_update_preserves_configured_order(self):
        source = inspect.getsource(RolesBoard.update_roles_board)
        self.assertNotIn("sorted(", source)
        self.assertIn('for role_data in self.roles_board["roles"]', source)

    def test_editor_supports_reorder_remove_and_reaction_rebuild(self):
        self.assertTrue(hasattr(RolesBoardEditorView, "_move_up"))
        self.assertTrue(hasattr(RolesBoardEditorView, "_move_down"))
        self.assertTrue(hasattr(RolesBoardEditorView, "_remove"))
        self.assertTrue(hasattr(RolesBoardEditorView, "_rebuild"))


if __name__ == "__main__":
    unittest.main()
