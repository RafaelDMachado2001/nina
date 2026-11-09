import unittest

from nina import orchestration as orch
from nina.agents import roles as roles_mod

ROLES = roles_mod.load_roles()


class SettingsTest(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        self.assertEqual(orch.settings({}), {"mode": "manual", "max_depth": 2, "max_delegations": 8, "ask": True, "allow": {}})
        st = orch.settings({"orchestration": {"mode": "chaos", "max_depth": "x", "max_delegations": 0, "ask": "no",
                                              "allow": {"general": "architect", "x": ["a", 3]}}})
        self.assertEqual(st, {"mode": "manual", "max_depth": 2, "max_delegations": 8, "ask": True,
                              "allow": {"x": ["a"]}})
        st = orch.settings({"orchestration": {"mode": "collaborative", "max_depth": 3, "max_delegations": 2, "ask": False}})
        self.assertEqual((st["mode"], st["max_depth"], st["max_delegations"], st["ask"]), ("collaborative", 3, 2, False))


class DelegatesForTest(unittest.TestCase):
    def st(self, mode, **kw):
        return orch.settings({"orchestration": {"mode": mode, **kw}})

    def test_manual_never_delegates(self):
        self.assertEqual(orch.delegates_for(self.st("manual"), "coordinator", 0, ROLES), [])
        self.assertEqual(orch.delegates_for(self.st("manual"), "general", 0, ROLES), [])

    def test_coordinator_mode_only_the_coordinator_at_the_top(self):
        st = self.st("coordinator")
        targets = orch.delegates_for(st, "coordinator", 0, ROLES)
        self.assertEqual(sorted(targets), sorted(r for r in ROLES if r != "coordinator"))
        self.assertEqual(orch.delegates_for(st, "general", 0, ROLES), [])
        self.assertEqual(orch.delegates_for(st, "coordinator", 1, ROLES), [])

    def test_collaborative_follows_depth_and_allow(self):
        st = self.st("collaborative", max_depth=2, allow={"general": ["writing-reviewer", "coordinator", "nope"]})
        self.assertEqual(orch.delegates_for(st, "general", 0, ROLES), ["writing-reviewer"])     # never the coordinator
        everyone = orch.delegates_for(st, "architect", 1, ROLES)
        self.assertNotIn("architect", everyone)
        self.assertNotIn("coordinator", everyone)
        self.assertIn("general", everyone)
        self.assertEqual(orch.delegates_for(st, "architect", 2, ROLES), [])                      # depth limit

    def test_chat_default_role(self):
        self.assertEqual(orch.chat_default_role({}), "general")
        self.assertEqual(orch.chat_default_role({"chat_role": "architect"}), "architect")
        self.assertEqual(orch.chat_default_role({"chat_role": "architect", "orchestration": {"mode": "coordinator"}}),
                         "coordinator")

    def test_the_prompt_lists_targets_with_their_ai(self):
        text = orch.delegation_prompt(["writing-reviewer", "code-reviewer"], ROLES,
                                      lambda role_id: {"writing-reviewer": "Gemini"}.get(role_id, "Claude"))
        self.assertIn("writing-reviewer", text)
        self.assertIn("Writing reviewer", text)
        self.assertIn("Gemini", text)
        self.assertIn("Delegate", text)


class CoordinatorRoleTest(unittest.TestCase):
    def test_builtin_and_read_only(self):
        role = ROLES["coordinator"]
        self.assertEqual(role["writes"], [])
        self.assertIn("Read", role["tools"])
        self.assertIn("delegate", role["prompt"].lower())


if __name__ == "__main__":
    unittest.main()
