import os
import unittest

from tests.support import IsolatedHome  # noqa: F401  (sets up the offscreen Qt application)

from nina import chat, chathistory


class FakeRunner:
    orchestration = None

    def __init__(self, emit, claude_bin):
        self.emit = emit
        self.started, self.sent, self.stopped, self.interrupted, self.resolved = [], [], [], [], []

    connections = None

    def start(self, role, cwd, prompt, account="", config_dir=None, resume=None, connection=None, model=""):
        self.started.append({"role": role["id"], "cwd": cwd, "prompt": prompt, "account": account,
                             "config_dir": config_dir, "resume": resume})
        if self.connections is None:
            self.connections = []
        self.connections.append({"id": (connection or {}).get("id"), "kind": (connection or {}).get("kind"), "model": model})
        return f"agent-{len(self.started)}"

    def send(self, agent_id, text):
        self.sent.append((agent_id, text))

    def stop(self, agent_id):
        self.stopped.append(agent_id)

    def interrupt(self, agent_id):
        self.interrupted.append(agent_id)

    def resolve(self, req, ok, message="", always=False):
        self.resolved.append((req, ok, True) if always else (req, ok))

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

    LOCAL = {"id": "local", "kind": "openai", "name": "Local AI", "base_url": "http://localhost:11434/v1", "model": "qwen"}

    def test_an_openai_connection_runs_the_chat(self):
        c = self.make(cfg={"connections": [self.LOCAL]})
        self.assertTrue(c.set_connection("local"))
        c.ask("hi")
        self.assertEqual(self.runner.connections[-1], {"id": "local", "kind": "openai", "model": "qwen"})
        self.assertEqual((self.runner.started[-1]["account"], self.runner.started[-1]["config_dir"]), ("", None))
        self.assertEqual(c.connection_name(), "Local AI")

    def test_without_a_choice_claude_is_used_as_before(self):
        c = self.make(cfg={"connections": [self.LOCAL]})
        c.ask("hi")
        self.assertEqual(self.runner.connections[-1]["id"], "claude")

    def test_the_chat_role_binding_picks_the_connection(self):
        c = self.make(cfg={"connections": [self.LOCAL], "role_connections": {"general": {"connection": "local", "model": "big"}}})
        c.ask("hi")
        self.assertEqual(self.runner.connections[-1], {"id": "local", "kind": "openai", "model": "big"})

    def test_a_deleted_connection_falls_back_with_a_note(self):
        c = self.make(cfg={"connections": []})
        c.connection = "gone"
        c.ask("hi")
        self.assertEqual(self.runner.connections[-1]["id"], "claude")
        self.assertTrue(any(k == "info" and "gone" in t for k, t in self.messages))

    def test_a_session_resumes_only_on_its_own_connection(self):
        c = self.make(cfg={"connections": [self.LOCAL]})
        c.set_connection("local")
        chat.save_saved({"session": "s1", "connection": "local"}, self.state)
        c.ask("again")
        self.assertEqual(self.runner.started[-1]["resume"], "s1")
        c2 = self.make(cfg={"connections": [self.LOCAL]})
        chat.save_saved({"session": "s1", "connection": "local"}, self.state)
        c2.ask("on claude")                                       # this chat follows the default (Claude)
        self.assertIsNone(self.runner.started[-1]["resume"])

    def test_turn_errors_keep_the_chat_ready(self):
        c = self.make()
        done = []
        c.finished.connect(lambda: done.append(1))
        c.ask("hi")
        c.on_event("agent-1", "status", "working")
        c.on_event("agent-1", "turn_error", "Quota reached")
        self.assertIn(("error", "Quota reached"), self.messages)
        self.assertEqual((c.status, done), ("idle", []))
        self.assertTrue(c.ask("again"))
        self.assertEqual(self.runner.sent, [("agent-1", "again")])

    def test_hook_events_are_forwarded(self):
        c = self.make()
        got = []
        c.hook_event.connect(got.append)
        c.ask("hi")
        c.on_event("agent-1", "hook", {"ev": "Stop", "sid": "s"})
        self.assertEqual(got, [{"ev": "Stop", "sid": "s"}])

    def test_connection_entries_list_claude_accounts_and_custom_connections(self):
        (self.accounts / "work").mkdir(parents=True)
        c = self.make(accounts=["work"], cfg={"connections": [self.LOCAL]})
        ids = [cid for cid, _label in c.connection_entries()]
        self.assertEqual(ids, [None, "claude", "claude:work", "local"])

    def test_a_bad_step_limit_in_the_config_does_not_break_the_chat(self):
        c = self.make(cfg={"max_tool_steps": "lots"})
        self.assertTrue(c.ask("hi"))
        self.assertEqual(self.runner.max_steps, 40)

    def test_deny_list_and_step_limit_reach_the_runner(self):
        c = self.make(cfg={"permissions": {"deny": ["Bash(rm:*)"]}, "max_tool_steps": 7})
        self.runner_deny = None
        c.ask("hi")
        self.assertEqual((self.runner.deny, self.runner.max_steps), (["Bash(rm:*)"], 7))

    # ---- orchestration ---------------------------------------------------------------------------------
    def test_the_role_follows_the_mode_unless_one_is_picked(self):
        c = self.make(cfg={"orchestration": {"mode": "coordinator"}})
        self.assertEqual(c.role_for_chat()["id"], "coordinator")
        self.assertTrue(c.set_role("architect"))
        self.assertEqual(c.role_for_chat()["id"], "architect")
        c.set_role(None)
        self.ctx["cfg"] = {}
        self.assertEqual(c.role_for_chat()["id"], "general")

    def test_role_entries_show_the_coordinator_only_when_it_can_work(self):
        ids = [r for r, _label in self.make().role_entries()]
        self.assertEqual(ids[0], None)
        self.assertIn("general", ids)
        self.assertNotIn("coordinator", ids)
        ids = [r for r, _label in self.make(cfg={"orchestration": {"mode": "collaborative"}}).role_entries()]
        self.assertIn("coordinator", ids)

    def test_switching_the_mode_starts_a_new_conversation_with_a_note(self):
        c = self.make()
        chat.save_saved({"session": "s1", "connection": "claude", "role": "general"}, self.state)
        self.ctx["cfg"] = {"orchestration": {"mode": "coordinator"}}
        c.ask("hi")
        self.assertIsNone(self.runner.started[-1]["resume"])
        self.assertEqual(self.runner.started[-1]["role"], "coordinator")
        self.assertTrue(any(k == "info" and "NINA (coordinator)" in t for k, t in self.messages))

    def test_a_session_of_the_same_role_resumes(self):
        c = self.make()
        chat.save_saved({"session": "s1", "connection": "claude", "role": "general"}, self.state)
        c.ask("hi")
        self.assertEqual(self.runner.started[-1]["resume"], "s1")
        c2 = self.make()
        chat.save_saved({"session": "s2", "connection": "claude"}, self.state)    # saved before roles were kept
        c2.ask("hi")
        self.assertEqual(self.runner.started[-1]["resume"], "s2")

    def test_the_runner_gets_orchestration_and_children_use_the_chat_ai(self):
        c = self.make(cfg={"connections": [self.LOCAL], "orchestration": {"mode": "coordinator"},
                           "role_connections": {"architect": "local"}})
        c.set_connection("claude")
        c.ask("hi")
        orch = self.runner.orchestration
        self.assertEqual(orch["settings"]["mode"], "coordinator")
        roles = orch["roles"]
        parent = {"id": "claude", "kind": "claude", "name": "Claude", "account": ""}
        conn, model, account, config_dir = orch["resolve"](roles["architect"], parent)
        self.assertEqual((conn["id"], model), ("local", "qwen"))                    # its own binding
        conn, _m, _a, _d = orch["resolve"](roles["writing-reviewer"], parent)
        self.assertEqual(conn["id"], "claude")                                        # no binding: the chat's AI
        self.assertEqual(orch["conn_name"]("architect"), "Local AI")

    # ---- final review (orchestration) --------------------------------------------------------------------
    def test_a_live_agent_is_restarted_when_the_mode_changes(self):
        self.ctx_cfg = {"orchestration": {"mode": "collaborative", "ask": False}}
        c = self.make(cfg=self.ctx_cfg)
        c.ask("one")
        c.on_event("agent-1", "session", "sess-1")
        c.on_event("agent-1", "status", "idle")
        self.ctx["cfg"] = {"orchestration": {"mode": "manual"}}               # the user picks Manual
        c.ask("two")
        self.assertIn("agent-1", self.runner.stopped)
        self.assertEqual(self.runner.sent, [])
        self.assertEqual(self.runner.started[-1]["resume"], "sess-1")         # same role: the conversation goes on
        self.assertEqual(self.runner.orchestration["settings"]["mode"], "manual")

    def test_a_live_agent_of_the_old_default_role_gives_way_to_the_new_one(self):
        c = self.make()
        c.ask("one")
        c.on_event("agent-1", "session", "sess-1")
        c.on_event("agent-1", "status", "idle")
        self.ctx["cfg"] = {"orchestration": {"mode": "coordinator"}}
        c.ask("two")
        self.assertEqual((self.runner.started[-1]["role"], self.runner.started[-1]["resume"]), ("coordinator", None))
        self.assertTrue(any(k == "info" and "NINA (coordinator)" in t for k, t in self.messages))

    def test_an_unchanged_live_agent_just_gets_the_message(self):
        c = self.make(cfg={"orchestration": {"mode": "collaborative"}})
        c.ask("one")
        c.on_event("agent-1", "status", "idle")
        c.ask("two")
        self.assertEqual(self.runner.sent, [("agent-1", "two")])

    def test_a_session_saved_before_roles_counts_as_the_general_role(self):
        c = self.make(cfg={"orchestration": {"mode": "coordinator"}})
        chat.save_saved({"session": "old", "connection": "claude"}, self.state)
        c.ask("hi")
        self.assertIsNone(self.runner.started[-1]["resume"])

    def test_the_coordinator_picked_in_a_tab_is_not_used_in_manual_mode(self):
        c = self.make(cfg={"orchestration": {"mode": "coordinator"}})
        c.set_role("coordinator")
        self.ctx["cfg"] = {}
        self.assertEqual(c.role_for_chat()["id"], "general")

    def test_an_interrupted_answer_is_not_reported_as_an_error(self):
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "status", "working")
        c.interrupt()
        c.on_event("agent-1", "result", {"cost": 0.01, "error": True,
                                         "errors": ["[ede_diagnostic] result_type=user stop_reason=tool_use"]})
        c.on_event("agent-1", "status", "idle")
        self.assertFalse([t for k, t in self.messages if k == "error"])
        self.assertIn(("info", "Interrupted."), self.messages)
        c.ask("again")                                    # a later failure is still shown
        c.on_event("agent-1", "result", {"cost": 0.02, "error": True, "errors": ["real failure"]})
        self.assertIn(("error", "real failure"), self.messages)

    def test_an_interrupted_answer_of_another_ai_is_not_an_error_either(self):
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "status", "working")
        c.interrupt()
        c.on_event("agent-1", "turn_error", "Interrupted.")
        self.assertFalse([t for k, t in self.messages if k == "error"])
        self.assertEqual(c.status, "idle")

    def test_delegated_cost_counts_in_the_chat_and_the_day(self):
        c = self.make()
        c.ask("hi")
        before = chat.chatinfo.load_day()["cost"]
        c.on_event("agent-1", "extra_cost", 0.25)
        self.assertAlmostEqual(c.info["cost"], 0.25)
        self.assertAlmostEqual(chat.chatinfo.load_day()["cost"] - before, 0.25)

    def test_withdrawn_permission_requests_leave_the_screen(self):
        c = self.make()
        cleared = []
        c.permission_cleared.connect(lambda: cleared.append(1))
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "X · Write", "detail": "d", "remember": []})
        c.on_event("agent-1", "permission", {"req": "r2", "tool": "Y", "detail": "d", "remember": []})
        c.on_event("agent-1", "permission_withdrawn", {"req": "r1"})
        self.assertEqual(c.current_req["req"], "r2")
        c.on_event("agent-1", "permission_withdrawn", {"req": "r2"})
        self.assertIsNone(c.current_req)
        self.assertTrue(cleared)

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

    def test_events_feed_model_context_usage_and_cost(self):
        c = self.make(account="work")
        changed = []
        c.info_changed.connect(lambda: changed.append(1))
        c.ask("hi")
        c.on_event("agent-1", "model", "claude-sonnet-5-5")
        c.on_event("agent-1", "usage", 1000)
        c.on_event("agent-1", "context", {"tokens": 5000, "max": 200000, "model": "claude-sonnet-5-5"})
        c.on_event("agent-1", "result", {"cost": 0.01, "error": False})
        c.on_event("agent-1", "result", {"cost": 0.025, "error": False})      # the CLI reports cost cumulatively
        self.assertEqual(c.info, {"model": "claude-sonnet-5-5", "ctx_tokens": 5000, "ctx_max": 200000,
                                  "cost": 0.025, "tokens": 1000})
        self.assertTrue(changed)
        c.on_event("agent-1", "ratelimit", {"windows": {"five_hour": {"utilization": 0.4}}})
        self.assertEqual(c.rate()["windows"]["five_hour"]["utilization"], 0.4)     # kept per account
        self.assertIsNone(self.make(account="other").rate())
        c.reset()
        self.assertEqual(c.info["tokens"], 0)

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
        if os.name == "posix":   # on Windows the user profile's ACL keeps it private
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.state.write_text("nope", encoding="utf-8")
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

    def test_allow_always_is_only_sent_when_the_request_offers_it(self):
        c = self.make()
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "Bash", "detail": "x",
                                             "remember": ["ssh servidor · UPDATE"]})
        c.on_event("agent-1", "permission", {"req": "r2", "tool": "Bash", "detail": "y", "remember": []})
        c.answer(True, always=True)
        c.answer(True, always=True)
        self.assertEqual(self.runner.resolved, [("r1", True, True), ("r2", True)])
        self.assertIn(("info", "Allowed always: ssh servidor · UPDATE"), self.messages)

    def test_images_go_with_the_message_and_text_alone_stays_a_string(self):
        c = self.make()
        self.assertTrue(c.ask("plain"))
        self.assertEqual(self.runner.started[0]["prompt"], "plain")
        c.on_event("agent-1", "status", "idle")
        img = {"media_type": "image/png", "data": "AAAA"}
        self.assertTrue(c.ask("look", [img]))
        self.assertEqual(self.runner.sent[-1], ("agent-1", {"text": "look", "images": [img]}))
        c.on_event("agent-1", "status", "idle")
        self.assertTrue(c.ask("", [img]))                    # an image without text is fine
        self.assertFalse(c.ask("", []))

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
        self.hist = self.home / "history"
        self.runners = []

        def new_controller(tab_id):
            def f(emit, claude_bin):
                r = FakeRunner(emit, claude_bin)
                self.runners.append(r)
                return r
            return chat.ChatController(lambda: ctx, runner_factory=f, path=self.home / f"chat-{tab_id}.json",
                                       tab_id=tab_id)

        c = chat.ChatController(lambda: ctx, runner_factory=factory, path=self.home / "chat.json")
        box = chat.ChatBox(c, theme, on_theme, factory=new_controller, tabs_path=self.home / "chats.json",
                          history_dir=self.hist)
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

    def test_role_chip_and_orchestration_menu(self):
        c1, box = self.make()
        modes = []
        box.on_orchestration = modes.append
        self.assertEqual(box.role_btn.text(), "General assistant")
        c1.set_role("architect")
        self.assertEqual(box.role_btn.text(), "Architect")
        saved = chat.load_tabs(self.home / "chats.json")
        self.assertEqual(saved["tabs"][0]["role"], "architect")
        box.set_orchestration("coordinator")
        self.assertEqual(modes, ["coordinator"])

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

    def test_info_line_shows_folder_model_context_and_clock(self):
        c, box = self.make()
        c.info.update(model="claude-sonnet-5-5", ctx_tokens=26000, ctx_max=1000000)
        box.infobar.setFixedWidth(2000)                              # wide: everything fits, whatever the font
        c.info_changed.emit()
        text = box.infobar.text()
        for part in ("Sonnet 5.5", "ctx 3%"):
            self.assertIn(part, text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} [AP]M")
        box.infobar.setFixedWidth(230)                               # narrow: the clock goes first
        box.update_infobar()
        self.assertNotRegex(box.infobar.text(), r"[AP]M")
        self.assertIn("ctx 3%", box.infobar.text())
        self.assertIn("Sonnet 5.5", box.details_html())

    def test_conversations_are_saved_and_closing_a_chat_keeps_it_in_the_history(self):
        c1, box = self.make()
        c1.ask("fix the login bug")
        c1.on_event("agent-1", "session", "sess-1")
        c1.on_event("agent-1", "text", "The token expires.")
        box.new_tab()                                     # the old chat goes to the background
        box.flush()
        rec = chathistory.load(chathistory.list_meta(self.hist)[0]["id"], self.hist)
        self.assertEqual((rec["title"], rec["session"], rec["cwd"]), ("fix the login bug", "sess-1", str(self.home)))
        self.assertEqual([k for k, _t in rec["entries"]], ["user", "assistant"])
        box.close_tab(box.tabs[0])                        # closing archives instead of forgetting
        self.assertEqual(len(box.tabs), 1)
        self.assertEqual(chathistory.list_meta(self.hist)[0]["session"], "sess-1")

    def test_closing_the_last_chat_starts_a_new_conversation_and_keeps_the_old_one(self):
        c1, box = self.make()
        c1.ask("first topic")
        old = box.tabs[0].conv
        box.close_current()
        self.assertNotEqual(box.tabs[0].conv, old)
        self.assertEqual([r["id"] for r in chathistory.list_meta(self.hist)], [old])
        c1.ask("second topic")
        box.flush()
        self.assertEqual(len(chathistory.list_meta(self.hist)), 2)

    def test_resuming_from_the_history_opens_a_tab_with_messages_account_and_session(self):
        c1, box = self.make()
        rec = {"id": "old1", "title": "Old chat", "account": "work", "cwd": str(self.home), "session": "sess-9",
               "created": 1.0, "updated": 2.0, "cost": 0.3, "tokens": 700,
               "entries": [["user", "remember the blue one"], ["assistant", "Noted."]]}
        chathistory.save(rec, self.hist)
        box.show_history()
        self.assertTrue(box.in_history)
        self.assertFalse(box.composer.isVisible())
        self.assertEqual(box.history.list.count(), 1)
        box.history.resume_selected()
        self.assertFalse(box.in_history)
        self.assertEqual((box.tabbar.count(), box.tabbar.tabText(1)), (2, "Old chat"))
        self.assertIn("remember the blue one", box.log.toPlainText())
        c = box.controller
        self.assertEqual((c.account, c.session, c.info["cost"]), ("work", "sess-9", 0.3))
        c.ask("and the red one?")                         # the next message resumes the saved Claude session
        started = self.runners[-1].started[0]
        self.assertEqual((started["resume"], started["account"], started["cwd"]), ("sess-9", "work", str(self.home)))
        box.show_history()                                # an already open conversation is only focused
        box.tabbar.setCurrentIndex(0)
        box.show_history()
        box.history.resume_selected()
        self.assertEqual(len(box.tabs), 2)

    def test_open_chats_come_back_with_their_messages_after_a_restart(self):
        c1, box = self.make()
        c1.ask("first topic")
        box.new_tab()
        box.controller.ask("second topic")
        box.flush()
        saved = chat.load_tabs(self.home / "chats.json")
        c = chat.ChatController(lambda: self.ctx, runner_factory=self.factory_fn, path=self.home / "chat.json")
        again = chat.ChatBox(c, "dark", None, factory=lambda tid: chat.ChatController(
            lambda: self.ctx, runner_factory=self.factory_fn, path=self.home / f"chat-{tid}.json", tab_id=tid),
            saved=saved, tabs_path=self.home / "chats2.json", history_dir=self.hist)
        self.addCleanup(again.close)
        self.assertTrue(again.tabs[0].lazy)                      # the background tab has not read its messages yet
        self.assertEqual(again.tabs[0].entries, [])
        self.assertFalse(again.tabs[1].lazy)                     # the one on screen has
        again.tabbar.setCurrentIndex(0)
        self.assertIn("first topic", again.log.toPlainText())
        again.tabbar.setCurrentIndex(1)
        self.assertIn("second topic", again.log.toPlainText())
        self.assertNotIn("first topic", again.log.toPlainText())

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

    def test_allow_always_button_and_auto_mode_switch(self):
        from nina import autoapprove
        c, box = self.make()
        c.auto = autoapprove.AutoMode(False, autoapprove.Rules(self.home / "rules.json"))
        c.ask("hi")
        c.on_event("agent-1", "permission", {"req": "r1", "tool": "Bash", "detail": "plain"})
        self.assertFalse(box.always_btn.isVisibleTo(box.sheet))
        c.answer(True)
        c.on_event("agent-1", "permission", {"req": "r2", "tool": "Bash", "detail": "upd",
                                             "remember": ["ssh servidor · UPDATE"]})
        self.assertTrue(box.always_btn.isVisibleTo(box.sheet))
        self.assertIn("ssh servidor · UPDATE", box.perm_remember.text())
        box.always_btn.click()
        self.assertEqual(self.runner.resolved[-1], ("r2", True, True))
        self.assertNotIn("auto", box.status.text())
        box.set_auto(True)
        self.assertTrue(c.auto.enabled())
        self.assertTrue(box.status.text().endswith("· auto"))

    def test_at_mention_suggests_files_and_completes_them(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        (self.home / "docs").mkdir()
        (self.home / "docs" / "guide.md").write_text("x", encoding="utf-8")
        (self.home / "README.md").write_text("x", encoding="utf-8")
        c, box = self.make()
        box.input.setText("read @gui")
        box.input.refresh_popup()
        self.assertEqual(box.mention_popup.items[0].value, "docs/guide.md")
        QTest.keyClick(box.input, Qt.Key_Tab)
        self.assertEqual(box.input.text(), "read @docs/guide.md ")
        box.input.setText("see @")
        box.input.refresh_popup()
        self.assertEqual([s.value for s in box.mention_popup.items], ["docs/", "README.md"])
        box.mention_popup.step(1)
        QTest.keyClick(box.input, Qt.Key_Return)             # accepts the suggestion, does not send
        self.assertEqual(box.input.text(), "see @README.md ")
        self.assertIsNone(self.runner)
        box.input.setText("see @do")
        box.input.refresh_popup()
        QTest.keyClick(box.input, Qt.Key_Escape)
        self.assertFalse(box.mention_popup.isVisible())
        box.input.setText("no mention here")
        box.input.refresh_popup()
        self.assertFalse(box.mention_popup.isVisible())

    def test_a_folder_suggestion_keeps_the_mention_open(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        (self.home / "docs").mkdir()
        (self.home / "docs" / "guide.md").write_text("x", encoding="utf-8")
        _c, box = self.make()
        box.input.setText("@doc")
        box.input.refresh_popup()
        QTest.keyClick(box.input, Qt.Key_Tab)
        self.assertEqual(box.input.text(), "@docs/")
        box.input.refresh_popup()
        self.assertEqual([s.value for s in box.mention_popup.items], ["docs/guide.md"])

    def test_pasted_image_is_attached_removed_and_sent_with_the_message(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QGuiApplication, QImage
        from PySide6.QtTest import QTest
        c, box = self.make()
        image = QImage(3000, 100, QImage.Format_RGB32)
        image.fill(QColor("red"))
        QGuiApplication.clipboard().setImage(image)
        QTest.keyClick(box.input, Qt.Key_V, Qt.ControlModifier)
        self.assertEqual(len(box.current.images), 1)
        self.assertTrue(box.strip.isVisibleTo(box.composer))
        self.assertEqual(QImage.fromData(__import__("base64").b64decode(box.current.images[0].payload["data"])).width(), 1568)
        QTest.keyClick(box.input, Qt.Key_V, Qt.ControlModifier)
        box.remove_image(1)
        box.input.setText("what is this? @~/x.md")
        box.submit()
        sent = self.runner.started[0]["prompt"]
        self.assertEqual(sent["images"][0]["media_type"], "image/png")
        self.assertRegex(sent["text"], r"^what is this\? @([A-Za-z]:)?/")       # the home folder, with "/" (also on Windows)
        self.assertNotIn("~", sent["text"])
        self.assertEqual((box.current.images, box.input.text(), box.strip.isVisible()), ([], "", False))
        self.assertEqual([k for k, _t in box.entries], ["user", "image"])
        self.assertIn("<img", box.log.toHtml())

    def test_an_image_alone_can_be_sent_and_the_limit_is_five(self):
        from PySide6.QtGui import QColor, QImage
        c, box = self.make()
        image = QImage(10, 10, QImage.Format_RGB32)
        image.fill(QColor("blue"))
        box.attach_images([image] * 7)
        self.assertEqual(len(box.current.images), 5)
        box.submit()
        self.assertEqual(len(self.runner.started[0]["prompt"]["images"]), 5)
        self.assertEqual(box.current.title, "Image")

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
