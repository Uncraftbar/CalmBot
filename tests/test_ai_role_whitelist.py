import unittest
from types import SimpleNamespace

from cogs.ai_chat import AIChat


class RoleWhitelistTests(unittest.TestCase):
    def test_empty_whitelist_allows_everyone(self):
        cog = object.__new__(AIChat)
        cog.settings = {"allowed_role_ids": []}
        self.assertTrue(cog._role_allowed(SimpleNamespace(roles=[])))

    def test_matching_role_is_allowed(self):
        cog = object.__new__(AIChat)
        cog.settings = {"allowed_role_ids": [20, 30]}
        member = SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=30)])
        self.assertTrue(cog._role_allowed(member))

    def test_nonmatching_or_missing_roles_are_denied(self):
        cog = object.__new__(AIChat)
        cog.settings = {"allowed_role_ids": [20]}
        self.assertFalse(cog._role_allowed(SimpleNamespace(roles=[SimpleNamespace(id=10)])))
        self.assertFalse(cog._role_allowed(SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
