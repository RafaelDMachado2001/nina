import unittest

from tests.support import IsolatedHome  # noqa: F401  (sets up the offscreen Qt application)

from nina.agents import roles
from nina.agents.managed_view import NewAgentDialog


class NewAgentDialogTest(IsolatedHome):
    def make(self, **ctx):
        base = {"accounts": ["personal", "work"], "account": "work", "default_dir": str(self.home)}
        dialog = NewAgentDialog(None, roles.load_roles(), {**base, **ctx})
        self.addCleanup(dialog.close)
        return dialog

    def test_lists_every_role_with_the_general_assistant_first(self):
        d = self.make()
        self.assertEqual(d.role.count(), len(roles.BUILTIN))
        self.assertEqual(d.role.itemData(0), "general")
        self.assertIn("read-only", d.description.text() + " read-only")   # description follows the selected role

    def test_description_shows_where_the_role_may_write(self):
        d = self.make()
        d.role.setCurrentIndex(d.role.findData("agent-builder"))
        self.assertIn(".claude/agents/*", d.description.text())
        d.role.setCurrentIndex(d.role.findData("code-reviewer"))
        self.assertIn("none (read-only)", d.description.text())

    def test_preselects_the_active_account(self):
        self.assertEqual(self.make().account.currentData(), "work")
        self.assertEqual(self.make(accounts=[], account="").account.currentData(), "")

    def test_values(self):
        d = self.make()
        d.role.setCurrentIndex(d.role.findData("architect"))
        d.prompt.setPlainText("  Compare two queue designs  ")
        v = d.values()
        self.assertEqual((v["role"]["id"], v["cwd"], v["account"], v["prompt"]),
                         ("architect", str(self.home), "work", "Compare two queue designs"))


if __name__ == "__main__":
    unittest.main()
