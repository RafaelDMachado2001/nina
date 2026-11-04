import unittest

from tests.support import IsolatedHome  # noqa: F401  (sets up the offscreen Qt application)

from nina import chat


class FakeRunner:
    def __init__(self, emit, claude_bin):
        self.emit = emit
        self.started, self.sent, self.stopped, self.interrupted, self.resolved = [], [], [], [], []

    def start(self, role, cwd, prompt, account="", config_dir=None, resume=None):
        self.started.append({"role": role["id"], "cwd": cwd, "prompt": prompt, "account": account,
                             "config_dir": config_dir, "resume": resume})
        return f"agent-{len(self.started)}"

    def send(self, agent_id, text):
        self.sent.append((agent_id, text))

    def stop(self, agent_id):
        self.stopped.append(agent_id)

    def interrupt(self, agent_id):
        self.interrupted.append(agent_id)

    def resolve(self, req, ok, message=""):
        self.resolved.append((req, ok))

    def shutdown(self):
        pass


class ChatControllerTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.state = self.home / "chat.json"

    def make(self, **ctx):
        base = {"accounts": [], "account": "", "accounts_root": str(self.accounts), "default_dir": str(self.home),
                "roles_dir": None, "claude_bin": ""}
        self.ctx = {**base, **ctx}
        self.runner = None

        def factory(emit, claude_bin):
            self.runner = FakeRunner(emit, claude_bin)
            return self.runner

        c = chat.ChatController(lambda: self.ctx, runner_factory=factory, path=self.state)
        self.messages, self.states = [], []
        c.message.connect(lambda k, t: self.messages.append((k, t)))
        c.state_changed.connect(self.states.append)
        return c

    def test_first_message_starts_the_general_role_in_the_default_folder(self):
        c = self.make()
        self.assertTrue(c.ask("hello"))
        self.assertEqual(self.runner.started, [{"role": "general", "cwd": str(self.home), "prompt": "hello",
                                                "account": "", "config_dir": None, "resume": None}])
        self.assertEqual((c.status, self.messages), ("starting", [("user", "hello")]))

    def test_blank_and_busy_messages_are_not_sent(self):
        c = self.make()
        self.assertFalse(c.ask("   "))
        c.ask("one")
        self.assertFalse(c.ask("two"))        # still answering
        self.assertEqual(len(self.runner.started), 1)

    def test_follow_up_goes_to_the_same_agent(self):
        c = self.make()
        c.ask("one")
        c.on_event("agent-1", "status", "idle")
        self.assertTrue(c.ask("two"))
        self.assertEqual(self.runner.sent, [("agent-1", "two")])
        self.assertEqual(c.status, "working")
        self.assertEqual(len(self.runner.started), 1)

    def test_events_become_messages_and_the_session_is_saved(self):
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "session", "sess-1")
        c.on_event("agent-1", "text", "hello there")
        c.on_event("agent-1", "tool", {"name": "Read", "summary": "a.txt"})
        c.on_event("agent-1", "status", "idle")
        self.assertIn(("assistant", "hello there"), self.messages)
        self.assertIn(("tool", "Read: a.txt"), self.messages)
        self.assertEqual(chat.load_saved(self.state)["session"], "sess-1")

    def test_events_of_other_agents_are_ignored(self):
        c = self.make()
        c.ask("hi")
        c.on_event("someone-else", "text", "nope")
        self.assertNotIn(("assistant", "nope"), self.messages)

    def test_a_new_run_resumes_the_saved_conversation(self):
        chat.save_saved({"session": "sess-9"}, self.state)
        c = self.make()
        c.ask("again")
        self.assertEqual(self.runner.started[0]["resume"], "sess-9")

    def test_failed_resume_clears_the_saved_session(self):
        chat.save_saved({"session": "gone"}, self.state)
        c = self.make()
        c.ask("again")
        c.on_event("agent-1", "error", "no such session")
        self.assertEqual((c.status, chat.load_saved(self.state)), ("error", {}))
        c.ask("retry")
        self.assertIsNone(self.runner.started[1]["resume"])

    def test_closed_agent_restarts_resuming_the_session(self):
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "session", "sess-2")
        c.on_event("agent-1", "status", "closed")
        c.ask("back")
        self.assertEqual(self.runner.started[1]["resume"], "sess-2")

    def test_reset_forgets_the_conversation(self):
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "session", "sess-3")
        c.reset()
        self.assertEqual((c.status, chat.load_saved(self.state)), ("none", {}))
        self.assertEqual(self.runner.stopped, ["agent-1"])
        self.assertIn(("reset", ""), self.messages)
        c.ask("fresh")
        self.assertIsNone(self.runner.started[1]["resume"])

    def test_account_and_role_come_from_the_context(self):
        (self.accounts / "work").mkdir(parents=True)
        c = self.make(account="work", chat_role="architect")
        c.ask("hi")
        s = self.runner.started[0]
        self.assertEqual((s["role"], s["account"], s["config_dir"]), ("architect", "work", str(self.accounts / "work")))

    def test_unknown_role_falls_back_to_general(self):
        c = self.make(chat_role="nope")
        c.ask("hi")
        self.assertEqual(self.runner.started[0]["role"], "general")

    def test_interrupt_only_while_working(self):
        c = self.make()
        c.interrupt()
        c.ask("hi")
        c.on_event("agent-1", "status", "working")
        c.interrupt()
        self.assertEqual(self.runner.interrupted, ["agent-1"])

    def test_saved_file_is_private_and_damaged_file_is_ignored(self):
        chat.save_saved({"session": "s"}, self.state)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.state.write_text("nope")
        self.assertEqual(chat.load_saved(self.state), {})


class ChatBoxTest(IsolatedHome):
    def test_input_is_disabled_while_the_agent_works_and_the_window_stays_on_screen(self):
        from PySide6.QtWidgets import QWidget
        ctx = {"accounts": [], "account": "", "accounts_root": str(self.accounts), "default_dir": str(self.home),
               "roles_dir": None, "claude_bin": ""}
        c = chat.ChatController(lambda: ctx, runner_factory=FakeRunner, path=self.home / "chat.json")
        box = chat.ChatBox(c)
        self.addCleanup(box.close)
        self.assertTrue(box.input.isEnabled())
        c.set_status("working")
        self.assertFalse(box.input.isEnabled())
        self.assertTrue(box.stop_btn.isEnabled())
        c.set_status("idle")
        self.assertTrue(box.input.isEnabled())
        anchor = QWidget()
        anchor.setGeometry(0, 0, 100, 100)             # in the top-left corner: no room above
        box.show_near(anchor)
        self.assertGreaterEqual(box.y(), 0)
        self.assertGreaterEqual(box.x(), 0)


if __name__ == "__main__":
    unittest.main()
