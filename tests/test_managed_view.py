import unittest

from tests.support import IsolatedHome  # noqa: F401  (sets up the offscreen Qt application)

from nina.agents import roles
from nina.agents import managed_store
from nina.agents.managed_view import ManagedTab, NewAgentDialog


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


class FakeRunner:
    def __init__(self):
        self.started = []

    def start(self, role, cwd, prompt, account="", config_dir=None, resume=None):
        self.started.append({"role": role["id"], "cwd": cwd, "prompt": prompt, "resume": resume})
        return "live-1"

    def shutdown(self):
        pass


class ManagedTabPersistenceTest(IsolatedHome):
    def make(self):
        self.store = self.home / "managed.json"
        ctx = {"accounts": [], "account": "", "accounts_root": str(self.accounts), "default_dir": str(self.home),
               "roles_dir": None, "claude_bin": ""}
        tab = ManagedTab(lambda: ctx, store_path=self.store)
        self.addCleanup(tab.close)
        tab.runner.shutdown()
        tab.runner = FakeRunner()
        return tab

    def seed(self, **kw):
        managed_store.save([{"role": "general", "cwd": str(self.home), "account": "", "session": "sess-1", "cost": 0.2,
                             "log": [("user", "hi"), ("assistant", "hello")], **kw}], self.store if hasattr(self, "store") else self.home / "managed.json")

    def test_saved_agents_come_back_closed_with_their_log(self):
        self.seed()
        tab = self.make()
        self.assertEqual(tab.listing.count(), 1)
        tab.listing.setCurrentRow(0)
        a = tab.agents[tab.current]
        self.assertEqual((a["status"], a["session"]), ("closed", "sess-1"))
        self.assertEqual(a["log"][0], ("user", "hi"))
        self.assertTrue(tab.resume_btn.isEnabled())

    def test_unknown_role_is_skipped(self):
        self.seed(role="no-such-role")
        self.assertEqual(self.make().listing.count(), 0)

    def test_resume_starts_the_same_session_without_a_new_prompt(self):
        self.seed()
        tab = self.make()
        tab.listing.setCurrentRow(0)
        tab.resume_agent()
        self.assertEqual(tab.runner.started, [{"role": "general", "cwd": str(self.home), "prompt": None, "resume": "sess-1"}])
        self.assertEqual(tab.listing.count(), 1)
        self.assertEqual(tab.current, "live-1")
        self.assertEqual(tab.agents["live-1"]["status"], "starting")
        self.assertEqual(tab.agents["live-1"]["session"], "sess-1")

    def test_remove_forgets_a_stopped_agent(self):
        self.seed()
        tab = self.make()
        tab.listing.setCurrentRow(0)
        self.assertEqual(tab.close_btn.text(), "Remove")
        tab.close_agent()
        self.assertEqual(tab.listing.count(), 0)
        self.assertEqual(managed_store.load(self.store), [])

    def test_events_are_saved(self):
        tab = self.make()
        tab.agents["x"] = {"role": __import__("nina.agents.roles", fromlist=["x"]).load_roles()["general"], "cwd": str(self.home),
                           "account": "", "status": "working", "log": [], "session": None, "cost": 0.0,
                           "item": __import__("PySide6.QtWidgets", fromlist=["x"]).QListWidgetItem()}
        tab.on_event("x", "session", "sess-7")
        self.assertEqual([a["session"] for a in managed_store.load(self.store)], ["sess-7"])


if __name__ == "__main__":
    unittest.main()
