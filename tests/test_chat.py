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


class ChatPermissionTest(ChatControllerTest):
    def asked(self, c):
        got = []
        c.permission_asked.connect(got.append)
        cleared = []
        c.permission_cleared.connect(lambda: cleared.append(1))
        return got, cleared

    def test_requests_are_asked_one_at_a_time(self):
        c = self.make()
        got, cleared = self.asked(c)
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "Bash", "detail": "ls"})
        c.on_event("agent-1", "permission", {"req": "r2", "tool": "Edit", "detail": "a.txt"})
        self.assertEqual([g["req"] for g in got], ["r1"])
        c.answer(True)
        self.assertEqual([g["req"] for g in got], ["r1", "r2"])
        c.answer(False)
        self.assertEqual(self.runner.resolved, [("r1", True), ("r2", False)])
        self.assertIsNone(c.current_req)
        self.assertIn(("info", "Allowed: Bash"), self.messages)
        self.assertIn(("info", "Denied: Edit"), self.messages)

    def test_answer_without_a_request_does_nothing(self):
        c = self.make()
        c.ask("hi")
        c.answer(True)
        self.assertEqual(self.runner.resolved, [])

    def test_reset_and_errors_drop_pending_requests(self):
        c = self.make()
        got, cleared = self.asked(c)
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "Bash", "detail": "ls"})
        c.on_event("agent-1", "permission", {"req": "r2", "tool": "Bash", "detail": "pwd"})
        c.reset()
        self.assertEqual((c.current_req, c.queue, len(cleared)), (None, [], 1))
        c.ask("again")
        c.on_event("agent-2", "permission", {"req": "r3", "tool": "Bash", "detail": "ls"})
        c.on_event("agent-2", "error", "boom")
        self.assertIsNone(c.current_req)

    def test_retry_sends_the_last_message_again(self):
        c = self.make()
        c.ask("first try")
        c.on_event("agent-1", "error", "boom")
        self.assertTrue(c.retry())
        self.assertEqual(self.runner.started[1]["prompt"], "first try")
        self.assertFalse(self.make().retry())          # nothing to retry

    def test_each_chat_can_use_its_own_account_and_the_default_follows_the_context(self):
        (self.accounts / "work").mkdir(parents=True)
        (self.accounts / "personal").mkdir(parents=True)
        c = self.make(account="personal")
        self.assertEqual(c.effective_account(), "personal")
        c.set_account("work")
        c.ask("hi")
        self.assertEqual(self.runner.started[0]["account"], "work")
        self.assertEqual(self.runner.started[0]["config_dir"], str(self.accounts / "work"))
        c.on_event("agent-1", "status", "idle")
        c.set_account(None)                                 # back to the default account
        self.assertEqual(c.effective_account(), "personal")

    def test_changing_account_starts_a_new_session_and_is_blocked_while_busy(self):
        (self.accounts / "work").mkdir(parents=True)
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "session", "sess-1")
        self.assertFalse(c.set_account("work"))             # still answering
        c.on_event("agent-1", "status", "idle")
        changed = []
        c.account_changed.connect(lambda: changed.append(1))
        self.assertTrue(c.set_account("work"))
        self.assertEqual((self.runner.stopped, changed, c.status), (["agent-1"], [1], "none"))
        self.assertTrue(any(k == "info" and "work" in t for k, t in self.messages))
        c.ask("again")
        self.assertEqual((self.runner.started[1]["account"], self.runner.started[1]["resume"]), ("work", None))

    def test_a_saved_session_of_another_account_is_not_resumed(self):
        c = self.make(account="work")
        chat.save_saved({"session": "old", "account": "personal"}, self.state)
        c.ask("hi")
        self.assertIsNone(self.runner.started[0]["resume"])


class ChatBoxTest(IsolatedHome):
    def make(self, theme="dark", on_theme=None):
        ctx = {"accounts": [], "account": "", "accounts_root": str(self.accounts), "default_dir": str(self.home),
               "roles_dir": None, "claude_bin": ""}
        self.runner = None

        def factory(emit, claude_bin):
            self.runner = FakeRunner(emit, claude_bin)
            return self.runner

        self.ctx, self.factory_fn = ctx, factory
        self.runners = []

        def new_controller(tab_id):
            def f(emit, claude_bin):
                r = FakeRunner(emit, claude_bin)
                self.runners.append(r)
                return r
            return chat.ChatController(lambda: ctx, runner_factory=f, path=self.home / f"chat-{tab_id}.json",
                                       tab_id=tab_id)

        c = chat.ChatController(lambda: ctx, runner_factory=factory, path=self.home / "chat.json")
        box = chat.ChatBox(c, theme, on_theme, factory=new_controller, tabs_path=self.home / "chats.json")
        box.show()
        self.addCleanup(box.close)
        return c, box

    def test_starts_with_the_empty_state_and_switches_to_the_log(self):
        c, box = self.make()
        self.assertIs(box.body.currentWidget(), box.empty)
        box.suggestions[0].click()
        self.assertEqual(self.runner.started[0]["prompt"], "Explain this repository")
        self.assertIs(box.body.currentWidget(), box.log)
        c.reset()
        self.assertIs(box.body.currentWidget(), box.empty)

    def test_window_grows_to_large_maximizes_and_restores(self):
        _c, box = self.make()
        self.assertEqual((box.size_mode, box.width(), box.height()), ("normal", 400, 520))   # default is unchanged
        box.set_size_mode("large")
        self.assertGreater(box.width(), 400)
        self.assertGreater(box.height(), 520)
        box.set_size_mode("normal")
        self.assertEqual((box.width(), box.height()), (400, 520))
        before = box.geometry()
        box.size_btn.click()                                  # header button maximizes...
        self.assertEqual(box.size_mode, "maximized")
        self.assertEqual(box.geometry(), box.available_rect())
        self.assertFalse(box.grip.isVisible())
        box.size_btn.click()                                  # ...and restores
        self.assertEqual((box.size_mode, box.geometry()), ("normal", before))
        self.assertTrue(box.grip.isVisible())

    def test_new_chat_opens_a_tab_and_keeps_the_other_chat(self):
        c1, box = self.make()
        self.assertFalse(box.tabrow.isVisible())            # a single chat looks like before
        c1.ask("fix the login bug")
        box.input.setText("draft in chat one")
        box.new_tab()
        c2 = box.controller
        self.assertIsNot(c1, c2)
        self.assertTrue(box.tabrow.isVisible())
        self.assertTrue(all(t.close_btn.isVisible() for t in box.tabs))
        self.assertEqual([box.tabbar.tabText(i) for i in range(2)], ["fix the login bug", "New chat"])
        self.assertIs(box.body.currentWidget(), box.empty)  # the new chat starts empty
        self.assertEqual(box.input.text(), "")
        c2.ask("explain the repo")
        self.assertEqual(box.tabbar.tabText(1), "explain the repo")
        box.tabbar.setCurrentIndex(0)                       # back to the first one: history and draft are there
        self.assertIs(box.controller, c1)
        self.assertEqual([k for k, _t in box.entries], ["user"])
        self.assertIn("fix the login bug", box.log.toPlainText())
        self.assertNotIn("explain the repo", box.log.toPlainText())
        self.assertEqual(box.input.text(), "draft in chat one")

    def test_background_chat_answers_and_asks_permission_without_taking_the_screen(self):
        c1, box = self.make()
        c1.ask("one")
        box.new_tab()
        c2 = box.controller
        c2.ask("two")
        box.tabbar.setCurrentIndex(0)
        c2.on_event("agent-1", "text", "answer for two")
        self.assertNotIn("answer for two", box.log.toPlainText())
        c2.on_event("agent-1", "permission", {"req": "r9", "tool": "Bash", "detail": "ls"})
        self.assertFalse(box.sheet.isVisible())             # the sheet belongs to the chat on screen
        box.tabbar.setCurrentIndex(1)
        self.assertIn("answer for two", box.log.toPlainText())
        self.assertTrue(box.sheet.isVisible())
        self.assertEqual(box.perm_command.text(), "ls")

    def test_closing_a_chat_stops_it_and_the_last_one_is_only_cleared(self):
        c1, box = self.make()
        c1.ask("one")
        box.new_tab()
        c2 = box.controller
        c2.ask("two")
        box.close_current()
        self.assertEqual(len(box.tabs), 1)
        self.assertIs(box.controller, c1)
        self.assertEqual(self.runners[0].stopped, ["agent-1"])
        self.assertFalse(box.tabrow.isVisible())
        box.close_current()                                 # last one: cleared, not removed
        self.assertEqual(len(box.tabs), 1)
        self.assertIs(box.body.currentWidget(), box.empty)

    def test_open_chats_come_back_on_the_next_run(self):
        c1, box = self.make()
        c1.ask("first topic")
        box.new_tab()
        box.controller.ask("second topic")
        saved = chat.load_tabs(self.home / "chats.json")
        self.assertEqual([t["title"] for t in saved["tabs"]], ["first topic", "second topic"])
        second_id = saved["tabs"][1]["id"]
        self.assertEqual(saved["active"], second_id)
        c = chat.ChatController(lambda: self.ctx, runner_factory=self.factory_fn, path=self.home / "chat.json")
        again = chat.ChatBox(c, "dark", None, factory=lambda tid: chat.ChatController(
            lambda: self.ctx, runner_factory=self.factory_fn, path=self.home / f"chat-{tid}.json", tab_id=tid),
            saved=saved, tabs_path=self.home / "chats2.json")
        self.addCleanup(again.close)
        self.assertEqual([again.tabbar.tabText(i) for i in range(2)], ["first topic", "second topic"])
        self.assertEqual(again.controller.tab_id, second_id)

    def test_account_chip_shows_and_changes_the_chat_account(self):
        c1, box = self.make()
        self.assertEqual(box.account_btn.text(), "default")
        c1.set_account("work")
        self.assertEqual(box.account_btn.text(), "work")
        box.new_tab()
        self.assertEqual(box.account_btn.text(), "default")   # a new chat follows the default account
        box.tabbar.setCurrentIndex(0)
        self.assertEqual(box.account_btn.text(), "work")
        saved = chat.load_tabs(self.home / "chats.json")
        self.assertEqual([t["account"] for t in saved["tabs"]], ["work", None])
        c1.set_status("working")
        self.assertFalse(box.account_btn.isEnabled())

    def test_input_is_disabled_while_the_agent_works(self):
        c, box = self.make()
        self.assertTrue(box.input.isEnabled())
        self.assertFalse(box.stop_btn.isVisible())
        c.set_status("working")
        self.assertFalse(box.input.isEnabled())
        self.assertFalse(box.send_btn.isEnabled())
        self.assertTrue(box.stop_btn.isVisible())
        self.assertTrue(box.thinking.isVisible())
        self.assertEqual(box.status.text(), "thinking")
        c.set_status("idle")
        self.assertTrue(box.input.isEnabled())
        self.assertFalse(box.thinking.isVisible())
        self.assertEqual(box.status.text(), "ready")

    def test_permission_sheet_replaces_the_composer_and_deny_is_the_default(self):
        c, box = self.make()
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "Bash", "detail": "pytest -q"})
        self.assertTrue(box.sheet.isVisible())
        self.assertFalse(box.composer.isVisible())
        self.assertIn("Bash", box.perm_title.text())
        self.assertEqual(box.perm_command.text(), "pytest -q")
        self.assertEqual(box.status.text(), "needs permission")
        box.deny_btn.click()
        self.assertEqual(self.runner.resolved, [("r1", False)])
        self.assertFalse(box.sheet.isVisible())

    def test_enter_and_escape_deny_but_alt_p_allows(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        c, box = self.make()
        c.ask("hi")
        for key, expected in ((Qt.Key_Return, False), (Qt.Key_Escape, False)):
            c.on_event("agent-1", "permission", {"req": f"r{key}", "tool": "Bash", "detail": "ls"})
            box.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
            self.assertEqual(self.runner.resolved[-1], (f"r{key}", expected))
        c.on_event("agent-1", "permission", {"req": "rp", "tool": "Bash", "detail": "ls"})
        box.allow_shortcut()
        self.assertEqual(self.runner.resolved[-1], ("rp", True))

    def test_permission_request_opens_a_hidden_window(self):
        c, box = self.make()
        box.hide()
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "Bash", "detail": "ls"})
        self.assertTrue(box.isVisible())

    def test_messages_render_and_theme_switch_keeps_them(self):
        saved = []
        c, box = self.make(on_theme=saved.append)
        c.message.emit("user", "hello")
        c.message.emit("assistant", "**hi** there")
        c.message.emit("error", "boom")
        self.assertIn("hello", box.log.toPlainText())
        self.assertIn("Try again", box.log.toPlainText())
        box.set_theme("light")
        self.assertEqual((box.theme, saved), ("light", ["light"]))
        self.assertIn("hi there", box.log.toPlainText())
        self.assertIn("#e8e5ff", box.log.toHtml().lower())      # the light user bubble colour
        self.assertIn(chat.chat_theme.THEMES["light"]["bg"], box.styleSheet())

    def test_retry_link_resends(self):
        from PySide6.QtCore import QUrl
        c, box = self.make()
        c.ask("do it")
        c.on_event("agent-1", "error", "boom")
        box.on_link(QUrl("nina:retry"))
        self.assertEqual(len(self.runner.started), 2)

    def test_unknown_theme_falls_back_to_dark(self):
        self.assertEqual(self.make("neon")[1].theme, "dark")

    def test_window_stays_on_screen(self):
        from PySide6.QtWidgets import QWidget
        c, box = self.make()
        anchor = QWidget()
        anchor.setGeometry(0, 0, 100, 100)             # top-left corner: no room above
        box.show_near(anchor)
        self.assertGreaterEqual(box.y(), 0)
        self.assertGreaterEqual(box.x(), 0)


if __name__ == "__main__":
    unittest.main()
