import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_openai import FakeOpenAI, reply

from nina.agents import managed, sessions

READER = {"id": "reviewer", "title": "Reviewer", "prompt": "You review.", "tools": ["Read", "Glob", "Grep"], "writes": []}
WRITER = {"id": "general", "title": "General", "prompt": "You help.", "tools": ["Read", "Glob", "Grep"], "writes": ["**"]}


class OpenAIAgentTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeOpenAI()
        self.addCleanup(self.api.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)
        (Path(self.cwd) / "README.md").write_text("# Cosmos\nA blog.\n", encoding="utf-8")
        self.state = Path(self.cwd) / "state"
        p = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        p.start()
        self.addCleanup(p.stop)
        self.events, self.lock = [], threading.Lock()
        self.runner = managed.Runner(self.emit)
        self.addCleanup(self.runner.shutdown)
        self.conn = {"id": "fake", "kind": "openai", "name": "Fake", "base_url": self.api.base_url, "model": "fake-model",
                     "key": {"source": "none"}, "timeout_s": 10, "headers": {}, "context_window": 0,
                     "price_in": 0.0, "price_out": 0.0}

    def emit(self, aid, kind, payload):
        with self.lock:
            self.events.append((aid, kind, payload))

    def kinds(self):
        with self.lock:
            return [(k, p) for _a, k, p in self.events]

    def wait_for(self, kind, pred=lambda p: True, timeout=20.0, after=0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for k, p in self.kinds()[after:]:
                if k == kind and pred(p):
                    return p
            time.sleep(0.01)
        self.fail(f"event {kind} never arrived; received: {self.kinds()}")

    def start(self, prompt, role=WRITER, resume=None, conn=None):
        return self.runner.start(role, self.cwd, prompt, connection=conn or self.conn, model="fake-model", resume=resume)

    def idle_count(self):
        return sum(1 for k, p in self.kinds() if k == "status" and p == "idle")

    def wait_idle(self, n=1):
        t0 = time.monotonic()
        while self.idle_count() < n and time.monotonic() - t0 < 20:
            time.sleep(0.01)
        self.assertGreaterEqual(self.idle_count(), n, self.kinds())

    def test_plain_answer(self):
        self.api.script.append(reply("Hello!", usage=(100, 20)))
        self.start("hi")
        self.wait_idle()
        kinds = self.kinds()
        self.assertIn(("text", "Hello!"), kinds)
        self.assertIn(("usage", 120), kinds)
        self.assertEqual(self.wait_for("model"), "fake-model")
        sid = self.wait_for("session")
        req = self.api.chat_requests()[0]
        self.assertEqual(req["model"], "fake-model")
        self.assertEqual(req["messages"][0]["role"], "system")
        self.assertIn("You help.", req["messages"][0]["content"])
        self.assertIn(self.cwd, req["messages"][0]["content"])
        self.assertEqual(req["messages"][-1], {"role": "user", "content": "hi"})
        names = [t["function"]["name"] for t in req["tools"]]
        self.assertIn("Write", names)
        saved = sessions.load(sid)
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])

    def test_tool_call_then_answer_and_hook_events(self):
        self.api.script += [reply(tool_calls=[("c1", "Read", {"file_path": "README.md"})]), reply("It is a blog.")]
        self.start("what is this?")
        self.wait_idle()
        self.assertIn(("tool", {"name": "Read", "summary": "README.md"}), self.kinds())
        second = self.api.chat_requests()[1]["messages"]
        tool_msg = second[-1]
        self.assertEqual((tool_msg["role"], tool_msg["tool_call_id"]), ("tool", "c1"))
        self.assertIn("Cosmos", tool_msg["content"])
        hooks = [p["ev"] for k, p in self.kinds() if k == "hook"]
        self.assertEqual(hooks[:4], ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"])

    def test_read_only_role_never_gets_write_and_denials_reach_the_model(self):
        self.api.script += [reply(tool_calls=[("c1", "Write", {"file_path": "x.md", "content": "x"})]), reply("ok")]
        self.start("write it", role=READER)
        self.wait_idle()
        self.assertNotIn("Write", [t["function"]["name"] for t in self.api.chat_requests()[0]["tools"]])
        result = self.api.chat_requests()[1]["messages"][-1]["content"]
        self.assertTrue(result.startswith("Error:"), result)
        self.assertFalse((Path(self.cwd) / "x.md").exists())

    def test_write_asks_and_follows_the_answer(self):
        self.api.script += [reply(tool_calls=[("c1", "Write", {"file_path": "docs/a.md", "content": "olá"})]),
                            reply(tool_calls=[("c2", "Write", {"file_path": "docs/b.md", "content": "no"})]),
                            reply("done")]
        self.start("write two files")
        req = self.wait_for("permission")
        self.assertIn("docs/a.md", req["detail"])
        self.runner.resolve(req["req"], True)
        req2 = self.wait_for("permission", lambda p: p["req"] != req["req"])
        self.runner.resolve(req2["req"], False, "Not this one.")
        self.wait_idle()
        self.assertEqual((Path(self.cwd) / "docs" / "a.md").read_text(encoding="utf-8"), "olá")
        self.assertFalse((Path(self.cwd) / "docs" / "b.md").exists())
        self.assertIn("Not this one.", self.api.chat_requests()[2]["messages"][-1]["content"])

    def test_deny_list_blocks_without_asking(self):
        self.runner.deny = ["Read"]
        self.api.script += [reply(tool_calls=[("c1", "Read", {"file_path": "README.md"})]), reply("ok")]
        self.start("read")
        self.wait_idle()
        self.assertTrue(any(k == "denied" for k, _p in self.kinds()))
        self.assertNotIn("Cosmos", self.api.chat_requests()[1]["messages"][-1]["content"])

    def test_bad_tool_arguments_and_unknown_tools_become_errors(self):
        self.api.script += [reply(tool_calls=[("c1", "Read", "{not json"), ("c2", "Teleport", {})]), reply("sorry")]
        self.start("go")
        self.wait_idle()
        msgs = self.api.chat_requests()[1]["messages"]
        self.assertTrue(msgs[-2]["content"].startswith("Error:"))
        self.assertTrue(msgs[-1]["content"].startswith("Error:"))
        self.assertIn(("text", "sorry"), self.kinds())

    def test_step_limit_ends_the_turn(self):
        self.runner.max_steps = 2
        self.api.script += [reply(tool_calls=[(f"c{i}", "Glob", {"pattern": "*.md"})]) for i in range(5)]
        self.start("loop forever")
        self.wait_idle()
        self.assertEqual(len(self.api.chat_requests()), 2)
        self.assertTrue(any(k == "text" and "2" in p for k, p in self.kinds()))

    def test_provider_errors_keep_the_agent_and_the_history(self):
        self.api.script += [reply("first answer"),
                            (429, {"error": {"message": "Quota exceeded"}}, {"Retry-After": "30"}),
                            reply("second answer")]
        aid = self.start("one")
        self.wait_idle()
        self.runner.send(aid, "two")
        err = self.wait_for("turn_error")
        self.assertIn("Quota exceeded", err)
        self.wait_idle(2)
        self.runner.send(aid, "two again")
        self.wait_idle(3)
        last = self.api.chat_requests()[-1]["messages"]
        users = [m["content"] for m in last if m["role"] == "user"]
        self.assertEqual(users, ["one", "two again"])                  # the failed turn left nothing behind

    def test_interrupt_keeps_the_history_valid(self):
        self.api.script += [reply(tool_calls=[("c1", "Glob", {"pattern": "*.md"})]),
                            ("sleep", 3, reply("too late")), reply("after the interrupt")]
        aid = self.start("slow")
        self.wait_for("tool")
        time.sleep(0.2)
        self.runner.interrupt(aid)
        self.wait_idle()
        self.runner.send(aid, "next")
        self.wait_for("text", lambda p: p == "after the interrupt")
        msgs = self.api.chat_requests()[-1]["messages"]
        ids = {c["id"] for m in msgs if m.get("tool_calls") for c in m["tool_calls"]}
        answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
        self.assertEqual(ids, answered)

    def test_resume_sends_the_previous_conversation(self):
        sid = sessions.new_id()
        sessions.save(sid, [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "earlier answer"}])
        self.api.script.append(reply("welcome back"))
        self.start("again", resume=sid)
        self.wait_idle()
        roles = [m["role"] for m in self.api.chat_requests()[0]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(self.wait_for("session"), sid)

    def test_resume_waits_for_the_user_when_there_is_no_prompt(self):
        sid = sessions.new_id()
        sessions.save(sid, [{"role": "user", "content": "earlier"}])
        aid = self.start(None, resume=sid)
        self.wait_idle()
        self.assertEqual(self.api.chat_requests(), [])
        self.api.script.append(reply("ok"))
        self.runner.send(aid, "now")
        self.wait_for("text", lambda p: p == "ok")

    def test_provider_fields_in_tool_calls_are_sent_back_unchanged(self):
        extra = {"extra_content": {"google": {"thought_signature": "SIG=="}}}
        self.api.script += [reply(tool_calls=[("c1", "Glob", {"pattern": "*.md"}, extra)]), reply("ok")]
        self.start("go")
        self.wait_idle()
        sent = [m for m in self.api.chat_requests()[1]["messages"] if m.get("tool_calls")][0]
        self.assertEqual(sent["tool_calls"][0]["extra_content"], extra["extra_content"])

    def test_images_are_sent_as_data_urls(self):
        self.api.script.append(reply("a red square"))
        self.start({"text": "what is it?", "images": [{"media_type": "image/png", "data": "QUJD"}]})
        self.wait_idle()
        content = self.api.chat_requests()[0]["messages"][-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what is it?"})
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,QUJD")

    def test_cost_from_prices_and_context_window(self):
        conn = {**self.conn, "price_in": 2.0, "price_out": 10.0, "context_window": 1000}
        self.api.script.append(reply("x", usage=(1000, 100)))
        self.start("hi", conn=conn)
        self.wait_idle()
        result = self.wait_for("result")
        self.assertAlmostEqual(result["cost"], (1000 * 2.0 + 100 * 10.0) / 1e6)
        self.assertEqual(self.wait_for("context")["max"], 1000)

    def test_think_tags_are_hidden_from_the_text(self):
        self.api.script.append(reply("<think>let me think</think>\n\nThe answer."))
        self.start("hi")
        self.wait_idle()
        self.assertIn(("text", "The answer."), self.kinds())

    def test_close_ends_the_agent(self):
        self.api.script.append(reply("hi"))
        aid = self.start("hi")
        self.wait_idle()
        self.runner.stop(aid)
        self.wait_for("status", lambda p: p == "closed")
        self.assertEqual(self.wait_for("hook", lambda p: p["ev"] == "SessionEnd")["ev"], "SessionEnd")

    # ---- final review findings ----------------------------------------------------------------------
    def test_closing_during_a_tool_leaves_a_resumable_session(self):
        if not __import__("nina.agents.tools", fromlist=["x"]).find_bash():
            self.skipTest("no bash")
        self.runner.deny = []
        role = {**WRITER, "tools": ["Read", "Glob", "Grep", "Bash"]}
        self.api.script.append(reply(tool_calls=[("c1", "Bash", {"command": "sleep 30"})]))
        aid = self.runner.start(role, self.cwd, "slow", connection=self.conn, model="fake-model")
        sid = self.wait_for("session")
        self.wait_for("tool")
        time.sleep(0.3)
        self.runner.stop(aid)
        self.wait_for("status", lambda p: p == "closed", timeout=30)
        saved = sessions.load(sid)
        calls = {c["id"] for m in saved if m.get("tool_calls") for c in m["tool_calls"]}
        answered = {m["tool_call_id"] for m in saved if m["role"] == "tool"}
        self.assertEqual(calls, answered)

    def test_a_damaged_saved_session_is_repaired_on_resume(self):
        sid = sessions.new_id()
        sessions.save(sid, [{"role": "user", "content": "x"},
                            {"role": "assistant", "content": "", "tool_calls": [
                                {"id": "c9", "type": "function", "function": {"name": "Glob", "arguments": "{}"}}]}])
        self.api.script.append(reply("ok"))
        self.start("again", resume=sid)
        self.wait_idle()
        msgs = self.api.chat_requests()[0]["messages"]
        self.assertEqual([m.get("tool_call_id") for m in msgs if m["role"] == "tool"], ["c9"])

    def test_the_session_exists_before_the_first_answer(self):
        self.api.script.append(("sleep", 2, reply("late")))
        self.start("hi")
        sid = self.wait_for("session")
        time.sleep(0.3)
        self.assertEqual(sessions.load(sid), [])

    def test_arguments_sent_as_an_object_still_work(self):
        call = reply(tool_calls=[("c1", "Read", "x")])
        call["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {"file_path": "README.md"}
        self.api.script += [call, reply("done")]
        self.start("go")
        self.wait_idle()
        self.assertIn("Cosmos", self.api.chat_requests()[1]["messages"][-1]["content"])
        self.assertIn(("text", "done"), self.kinds())

    def test_connection_keys_are_hidden_from_shell_commands(self):
        from nina.agents import tools
        if not tools.find_bash():
            self.skipTest("no bash")
        role = {**WRITER, "tools": ["Read", "Glob", "Grep", "Bash"]}
        conn = {**self.conn, "key": {"source": "env", "name": "NINA_TEST_SECRET"}}
        self.runner.connections = [conn]
        with mock.patch.dict(os.environ, {"NINA_TEST_SECRET": "sk-leak-me"}):
            self.api.script += [reply(tool_calls=[("c1", "Bash", {"command": "echo [$NINA_TEST_SECRET]"})]), reply("ok")]
            self.runner.start(role, self.cwd, "show it", connection=conn, model="fake-model")
            self.wait_idle()
        out = self.api.chat_requests()[1]["messages"][-1]["content"]
        self.assertIn("[]", out)
        self.assertNotIn("sk-leak-me", out)


class SessionsTest(unittest.TestCase):
    def test_round_trip_ids_and_damaged_files(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": d}):
                sid = sessions.new_id()
                self.assertNotEqual(sid, sessions.new_id())
                sessions.save(sid, [{"role": "user", "content": "olá"}])
                self.assertEqual(sessions.load(sid), [{"role": "user", "content": "olá"}])
                self.assertIsNone(sessions.load("missing"))
                self.assertIsNone(sessions.load("../../etc/passwd"))
                path = sessions.path_of(sid)
                path.write_text("{broken", encoding="utf-8")
                self.assertIsNone(sessions.load(sid))
                self.assertEqual(json.loads(json.dumps(sessions.prune(0, keep=set()))), 1)


if __name__ == "__main__":
    unittest.main()
